"""Tests for Golden Throne Stage 2 — The Voice.

The fire callback ``golden_throne_followup`` reads the linked session doc's
rubric and routes on its state instead of always firing a flat SOP:

  - ``incomplete``     → accountability prompt naming the unmet conditions,
                         plus condition-specific TTS/banner.
  - ``legacy``         → the exact static SOP (behavior preserved).
  - ``ready_for_ack``  → notify-only; never re-prompts the agent.
  - ``victorious_bug`` → Pavlok enforcement; never re-prompts the agent.
  - ``acknowledged``   → skipped entirely.

These tests drive the real callback against an isolated DB and real session
doc files, mocking only the transport/notify/enforce seams so the prompt
selection and routing are exercised end to end.
"""

import importlib
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db.close()


@pytest.fixture
def gt_env(monkeypatch, tmp_path):
    """Reload Token-API modules against an isolated DB for each test.

    Mirrors the reload strategy in ``test_gt_rate_limit`` so main-level mutable
    globals (the fire-times deque, scheduler) resolve fresh per test.
    """
    db_path = Path(_test_db.name)
    if db_path.exists():
        db_path.unlink()
    monkeypatch.setenv("TOKEN_API_DB", str(db_path))

    for name in ("shared", "db_schema", "init_db", "main"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    init_db = sys.modules["init_db"]
    main = sys.modules["main"]
    init_db.init_database()
    main._golden_throne_fire_times.clear()

    # Deterministic: never let real quiet-hours config defer a fire.
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda: {"active": False})

    # tmuxctl owns instance_id -> pane resolution. Default to "pane gone" so GT
    # tests are hermetic (no live tmux server in CI); local fires fail closed on
    # this. Individual tests override to simulate a live, resolvable pane.
    async def _pane_gone(_instance_id):
        return (None, None)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _pane_gone)

    # Remote instances resolve on their OWN host via the satellite (panes are
    # host-local). Default to a live, resolvable remote pane so the rubric-routing
    # tests — which use a remote device to exercise the satellite dispatch path —
    # keep delivering. The remote-fail-closed test overrides this to (None, None).
    async def _remote_resolved(_session_id, _device_id):
        return ("%10", "kreig:N")

    monkeypatch.setattr(main, "_resolve_remote_instance_pane", _remote_resolved)

    yield SimpleNamespace(db_path=db_path, main=main, docs_dir=tmp_path)

    main._golden_throne_fire_times.clear()
    if db_path.exists():
        db_path.unlink()


def _write_doc(docs_dir: Path, frontmatter: str) -> Path:
    """Write a session doc file with the given YAML frontmatter block."""
    doc = docs_dir / f"doc-{uuid.uuid4().hex[:8]}.md"
    doc.write_text(f"---\n{frontmatter}\n---\n\n# session\n", encoding="utf-8")
    return doc


def _insert(
    db_path: Path,
    *,
    device_id: str,
    doc_path: Path | None,
    engine: str = "claude",
) -> str:
    """Insert a golden_throne instance, optionally linked to a session doc.

    There is no stored pane column anymore — the fire path resolves the pane live
    by UUID (tmuxctl owns resolution), which the tests drive by monkeypatching
    ``resolve_instance_pane`` / ``_resolve_remote_instance_pane``.
    """
    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR IGNORE INTO golden_throne (id, zealotry) VALUES (1, 4)")
    doc_id = None
    if doc_path is not None:
        cur = conn.execute(
            "INSERT INTO session_documents (file_path, status) VALUES (?, 'active')",
            (str(doc_path),),
        )
        doc_id = cur.lastrowid
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id, status,
            golden_throne, zealotry, session_doc_id, created_at, last_activity, engine)
           VALUES (?, ?, ?, 'local', ?, 'idle', '1', 4, ?, ?, ?, ?)""",
        (
            iid,
            f"gt-{iid[:8]}",
            "/tmp",
            device_id,
            doc_id,
            now,
            now,
            engine,
        ),
    )
    conn.commit()
    conn.close()
    return iid


class _Recorder:
    """Bundles the standard transport/notify/enforce mocks and their captures."""

    def __init__(self, main, monkeypatch, *, remote_success=True):
        self.posts = []
        self.notifies = []
        self.enqueues = []
        self.enforcements = []
        self.state_events = []
        self.resumes = []

        async def fake_log_event(*a, **k):
            return None

        async def fake_dispatch_notify(message, vibe=None, banner=None, instance_id=None):
            self.notifies.append(
                {"message": message, "vibe": vibe, "banner": banner, "instance_id": instance_id}
            )
            return {"success": True}

        async def fake_record_resume(instance):
            self.resumes.append(instance)
            return {"resume_count": 1, "window_started_at": "x", "enforced": False}

        async def fake_enqueue_pane_write(**kwargs):
            self.enqueues.append(kwargs)
            return {"id": 1}

        async def fake_enforce(req):
            self.enforcements.append(req)
            return {"enforced": True}

        async def fake_state_event(*a, **k):
            self.state_events.append((a, k))
            return None

        # httpx.AsyncClient(...) async-context-manager whose .post records the body.
        recorder = self

        class _Resp:
            status_code = 200

            def json(self_inner):
                return {"success": remote_success, "transport": "send-keys", "pane": "%10"}

            @property
            def text(self_inner):
                return ""

        class _FakeClient:
            def __init__(self_inner, *a, **k):
                pass

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def post(self_inner, url, json=None):
                recorder.posts.append({"url": url, "json": json})
                return _Resp()

        monkeypatch.setattr(main, "log_event", fake_log_event)
        monkeypatch.setattr(main, "dispatch_notify", fake_dispatch_notify)
        monkeypatch.setattr(main, "record_golden_throne_resume", fake_record_resume)
        monkeypatch.setattr(main, "enqueue_pane_write", fake_enqueue_pane_write)
        monkeypatch.setattr(main, "enforce", fake_enforce)
        monkeypatch.setattr(main, "handle_custodes_state_event", fake_state_event)
        monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)


@pytest.mark.asyncio
async def test_incomplete_rubric_fires_skill_invocation_not_sop(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    doc = _write_doc(gt_env.docs_dir, "victory:\n  a: true\n  b: false")
    iid = _insert(
        gt_env.db_path,
        device_id=main.LOCAL_DEVICE_NAME + "-remote",
        doc_path=doc,
        engine="codex",
    )

    await main.golden_throne_followup(iid)

    assert len(rec.posts) == 1, "incomplete instance should dispatch to the remote pane"
    prompt = rec.posts[0]["json"]["prompt"]
    prompt_summary = rec.posts[0]["json"]["prompt_summary"]
    # Names the specific unmet condition via the explicit GT skill invocation...
    assert prompt == '$golden-throne-sop victory condition "needs b" is unmet'
    # ...NOT the flat static SOP.
    assert prompt != main._load_golden_throne_sop()
    assert "Read your session doc. Assess what remains." not in prompt

    # Condition-specific TTS/banner (the _for_rubric variants), not generic.
    assert "needs b" in prompt_summary
    assert len(rec.notifies) == 1
    assert "needs b" in rec.notifies[0]["message"]
    assert "missing b" in rec.notifies[0]["banner"]


@pytest.mark.asyncio
async def test_incomplete_rubric_humanizes_underscored_condition(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    doc = _write_doc(gt_env.docs_dir, "victory:\n  deploy_done: true\n  tests_passing: false")
    iid = _insert(
        gt_env.db_path,
        device_id=main.LOCAL_DEVICE_NAME + "-remote",
        doc_path=doc,
        engine="codex",
    )

    await main.golden_throne_followup(iid)

    prompt = rec.posts[0]["json"]["prompt"]
    assert prompt == ('$golden-throne-sop victory condition "needs tests passing" is unmet')
    # Spoken form humanizes the underscore.
    assert "needs tests passing" in rec.notifies[0]["message"]


@pytest.mark.asyncio
async def test_legacy_no_rubric_fires_exact_static_sop(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    # No linked session doc at all → legacy fallback.
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME + "-remote", doc_path=None)

    await main.golden_throne_followup(iid)

    assert len(rec.posts) == 1
    assert rec.posts[0]["json"]["prompt"] == main._load_golden_throne_sop()
    # Generic resume notification, not the rubric variant.
    assert len(rec.notifies) == 1
    assert "needs" not in rec.notifies[0]["message"]
    assert rec.notifies[0]["banner"].startswith("GT resume:")


@pytest.mark.asyncio
async def test_legacy_scalar_rubric_fires_exact_static_sop(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    # Scalar-string victory → legacy_string → legacy branch (unchanged SOP).
    doc = _write_doc(gt_env.docs_dir, "victory: pending")
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME + "-remote", doc_path=doc)

    await main.golden_throne_followup(iid)

    assert rec.posts[0]["json"]["prompt"] == main._load_golden_throne_sop()


@pytest.mark.asyncio
async def test_ready_for_ack_is_notify_only_no_reprompt(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    # Complete rubric, never notified → ready_for_ack.
    doc = _write_doc(gt_env.docs_dir, "victory:\n  a: true\n  b: true")
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME, doc_path=doc)

    await main.golden_throne_followup(iid)

    # Notify-only: the agent is NOT re-prompted via any transport.
    assert rec.posts == []
    assert rec.enqueues == []
    assert rec.enforcements == []
    # A single ready-for-ack notification went out.
    assert len(rec.notifies) == 1
    assert "ready for ack" in rec.notifies[0]["message"]
    # notified_at was stamped so the next fire escalates to a bug-event.
    from session_doc_helpers import read_rubric

    assert read_rubric(doc).notified_at is not None


@pytest.mark.asyncio
async def test_victorious_bug_fires_enforcement_no_reprompt(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    # Complete rubric, already notified → victorious_bug (shock, not re-prompt).
    doc = _write_doc(
        gt_env.docs_dir,
        "victory:\n  a: true\n  b: true\nvictory_notified_at: '2026-05-31T00:00:00'",
    )
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME, doc_path=doc)

    await main.golden_throne_followup(iid)

    assert rec.posts == []
    assert rec.enqueues == []
    # Enforcement (Pavlok) fired, plus a cascade state event.
    assert len(rec.enforcements) == 1
    assert rec.state_events, "victorious_bug should emit an enforcement cascade state event"


@pytest.mark.asyncio
async def test_acknowledged_rubric_is_skipped(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    # Emperor already acked → must not fire at all.
    doc = _write_doc(
        gt_env.docs_dir,
        "victory:\n  a: true\n  b: true\nvictory_acknowledged_at: '2026-05-31T00:00:00'",
    )
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME, doc_path=doc)

    await main.golden_throne_followup(iid)

    assert rec.posts == []
    assert rec.enqueues == []
    assert rec.enforcements == []
    assert rec.notifies == []


# --- Phase 3 Tier 1: tmuxctl owns instance_id -> pane resolution -------------


@pytest.mark.asyncio
async def test_local_fire_fails_closed_when_pane_unresolved(gt_env, monkeypatch):
    """A local instance whose pane no longer resolves must fail closed: no send,
    no notify, no resume counted, and the instance is marked stopped. This is the
    structural fix for the stale-position ("palace:NE") ghost — there is no stored pane
    perspective left to send to or speak."""
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    # gt_env default: resolve_instance_pane -> (None, None) [pane gone]. Stored
    # column carries a stale %N that must NOT be used as a fallback.
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME, doc_path=None)

    await main.golden_throne_followup(iid)

    # Fail closed: no transport of any kind, no notification, no resume counted.
    assert rec.enqueues == []
    assert rec.posts == []
    assert rec.notifies == []
    assert rec.resumes == [], "a vanished pane must not be counted as a resume"

    # Marked stopped so the GT timer stops re-firing at a vanished pane.
    conn = sqlite3.connect(gt_env.db_path)
    row = conn.execute(
        "SELECT status, gt_resume_count FROM instances WHERE id = ?", (iid,)
    ).fetchone()
    conn.close()
    assert row[0] == "stopped"
    assert (row[1] or 0) == 0


@pytest.mark.asyncio
async def test_local_fire_targets_live_resolved_pane_not_stored_column(gt_env, monkeypatch):
    """A local fire sends to the pane resolved live by UUID and speaks the live
    role's position — never the stored tmux_pane/pane_label column."""
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)

    seen = {}

    async def _resolved(instance_id):
        seen["instance_id"] = instance_id
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolved)

    async def _alive(*a, **k):
        return True

    monkeypatch.setattr(main, "_tmux_pane_has_agent_process", _alive)

    async def _sent(*a, **k):
        return [{"status": main.PANE_WRITE_SENT, "returncode": 0, "stdout": "", "stderr": ""}]

    monkeypatch.setattr(main, "process_pane_write_queue_once", _sent)

    async def _pane_exists(_pane):
        return True

    monkeypatch.setattr(main, "_tmux_pane_exists", _pane_exists)

    # Stored column is deliberately stale; live resolution must win.
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME, doc_path=None)

    await main.golden_throne_followup(iid)

    # Resolution was keyed by the instance UUID, not read from the column.
    assert seen.get("instance_id") == iid
    # The send targeted the live-resolved pane (%77), not the stored %999.
    assert rec.enqueues, "live-resolved pane should receive a guarded send"
    assert rec.enqueues[0]["tmux_pane"] == "%77"
    # A real delivery counts a resume.
    assert rec.resumes, "a delivered local fire must count a resume"
    # The spoken surface uses the LIVE public role.
    assert rec.notifies, "a delivered local fire notifies the Emperor"
    assert "palace:N" in rec.notifies[0]["message"]


@pytest.mark.asyncio
async def test_local_live_agent_receives_skill_invocation_for_missing_condition(
    gt_env, monkeypatch
):
    """Regression guard for the bad behavior where TTS said "needs X" but the
    agent pane only received generic prose telling it to execute an SOP."""
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)

    async def _resolved(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolved)

    async def _alive(*a, **k):
        return True

    monkeypatch.setattr(main, "_tmux_pane_has_agent_process", _alive)

    async def _sent(*a, **k):
        return [{"status": main.PANE_WRITE_SENT, "returncode": 0, "stdout": "", "stderr": ""}]

    monkeypatch.setattr(main, "process_pane_write_queue_once", _sent)

    async def _pane_exists(_pane):
        return True

    monkeypatch.setattr(main, "_tmux_pane_exists", _pane_exists)

    doc = _write_doc(gt_env.docs_dir, "victory:\n  deploy_done: true\n  tests_passing: false")
    iid = _insert(
        gt_env.db_path,
        device_id=main.LOCAL_DEVICE_NAME,
        doc_path=doc,
        engine="codex",
    )

    await main.golden_throne_followup(iid)

    assert rec.enqueues, "live agent should receive a guarded pane write"
    payload = rec.enqueues[0]["payload"]
    assert rec.enqueues[0]["purpose"] == "skill:golden-throne-sop"
    assert payload == ('victory condition "needs tests passing" is unmet')
    assert "execute that SOP" not in payload


# --- Phase 3 Tier 1b: satellite owns remote instance_id -> pane resolution ---


@pytest.mark.asyncio
async def test_remote_fire_fails_closed_when_satellite_cannot_resolve(gt_env, monkeypatch):
    """A remote instance whose pane the satellite cannot resolve must fail closed:
    no satellite dispatch, no notify, no resume counted, instance marked stopped.

    tmux panes are host-local — token-api keeps no stored remote-pane perspective
    and never resolves a remote pane against its own tmux server. Resolution is
    owned by the pane's own host (the satellite); a miss there is "instance gone"."""
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)

    async def _gone(_session_id, _device_id):
        return (None, None)

    monkeypatch.setattr(main, "_resolve_remote_instance_pane", _gone)

    # Stored column carries a stale %N that must NOT be used as a fallback.
    iid = _insert(
        gt_env.db_path,
        device_id=main.LOCAL_DEVICE_NAME + "-remote",
        doc_path=None,
    )

    await main.golden_throne_followup(iid)

    assert rec.posts == [], "must not dispatch to a satellite for an unresolved remote pane"
    assert rec.notifies == []
    assert rec.resumes == [], "a vanished remote pane must not be counted as a resume"

    conn = sqlite3.connect(gt_env.db_path)
    row = conn.execute(
        "SELECT status, gt_resume_count FROM instances WHERE id = ?", (iid,)
    ).fetchone()
    conn.close()
    assert row[0] == "stopped"
    assert (row[1] or 0) == 0


@pytest.mark.asyncio
async def test_remote_fire_resolves_via_satellite_not_stored_column(gt_env, monkeypatch):
    """A remote fire resolves the pane on the satellite host by UUID and dispatches
    to the live-resolved pane — never the stored tmux_pane column."""
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)

    seen = {}

    async def _resolved(session_id, device_id):
        seen["session_id"] = session_id
        seen["device_id"] = device_id
        return ("%88", "kreig:N")

    monkeypatch.setattr(main, "_resolve_remote_instance_pane", _resolved)

    # Stored column is deliberately stale; live remote resolution must win.
    iid = _insert(
        gt_env.db_path,
        device_id=main.LOCAL_DEVICE_NAME + "-remote",
        doc_path=None,
    )

    await main.golden_throne_followup(iid)

    # Resolution was keyed by the instance UUID on its own host, not read from the column.
    assert seen.get("session_id") == iid
    # Dispatched to the satellite carrying the live-resolved pane (%88), not stored %999.
    assert len(rec.posts) == 1
    assert rec.posts[0]["json"].get("tmux_pane") == "%88"
    # A real remote delivery counts a resume.
    assert rec.resumes, "a delivered remote fire must count a resume"


# --- CodeRabbit-aware poke rendering ----------------------------------------


@pytest.mark.asyncio
async def test_coderabbit_finding_poke_names_body_and_location_not_bare_key(gt_env, monkeypatch):
    """An unmet coderabbit_<id> condition invokes the GT skill with a humanized
    criterion, never the bare numeric rubric key."""
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    fm = (
        "victory:\n"
        "  a: true\n"
        "  coderabbit_555: false\n"
        "coderabbit_review_state: complete\n"
        "coderabbit_comments:\n"
        "  - id: 555\n"
        "    key: coderabbit_555\n"
        "    category: actionable\n"
        "    path: token-api/main.py\n"
        "    line: 42\n"
        '    body: "Potential issue: this dereferences foo before the null guard"\n'
        "    addressed: false\n"
    )
    doc = _write_doc(gt_env.docs_dir, fm)
    iid = _insert(
        gt_env.db_path,
        device_id=main.LOCAL_DEVICE_NAME + "-remote",
        doc_path=doc,
        engine="codex",
    )

    await main.golden_throne_followup(iid)

    assert len(rec.posts) == 1
    prompt = rec.posts[0]["json"]["prompt"]
    assert prompt == ('$golden-throne-sop victory condition "needs a CodeRabbit finding" is unmet')
    # The bare numeric rubric key is never surfaced in the wake prompt.
    assert "coderabbit_555" not in prompt


@pytest.mark.asyncio
async def test_coderabbit_pending_review_is_benign_hold_not_sisyphus(gt_env, monkeypatch):
    """When the sole unmet condition is coderabbit_passed and the bot is still
    reviewing, the poke is a benign HOLD — never a Sisyphus accusation."""
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    fm = "victory:\n  a: true\n  coderabbit_passed: false\ncoderabbit_review_state: pending\n"
    doc = _write_doc(gt_env.docs_dir, fm)
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME + "-remote", doc_path=doc)

    await main.golden_throne_followup(iid)

    assert len(rec.posts) == 1
    prompt = rec.posts[0]["json"]["prompt"]
    assert "HOLD" in prompt
    assert "Sit tight" in prompt
    # No accountability / Sisyphus accusation language.
    assert "This session is not done" not in prompt
    assert "Silently rolling over is not an option" not in prompt


@pytest.mark.asyncio
async def test_coderabbit_tts_and_banner_do_not_speak_numeric_ids(gt_env, monkeypatch):
    """TTS/banner humanize a coderabbit_<id> key into a phrase; the numeric id is
    never spoken."""
    main = gt_env.main
    # Direct unit assertions on the humanizer.
    assert main._humanize_condition_key("coderabbit_777") == "a CodeRabbit finding"
    assert main._humanize_condition_key(main.CODERABBIT_NITPICK_KEY) == "CodeRabbit nitpicks"
    assert main._humanize_condition_key(main.CODERABBIT_PASSED_KEY) == "CodeRabbit review"

    rec = _Recorder(main, monkeypatch)
    fm = (
        "victory:\n"
        "  coderabbit_777: false\n"
        "coderabbit_review_state: complete\n"
        "coderabbit_comments:\n"
        "  - id: 777\n"
        "    key: coderabbit_777\n"
        "    category: actionable\n"
        "    path: a.py\n"
        "    line: 9\n"
        '    body: "Potential issue"\n'
        "    addressed: false\n"
    )
    doc = _write_doc(gt_env.docs_dir, fm)
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME + "-remote", doc_path=doc)

    await main.golden_throne_followup(iid)

    assert len(rec.notifies) >= 1
    msg = rec.notifies[0]["message"]
    assert "a CodeRabbit finding" in msg
    assert "coderabbit_777" not in msg  # raw key never spoken
