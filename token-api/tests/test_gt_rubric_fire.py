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

    yield SimpleNamespace(db_path=db_path, main=main, docs_dir=tmp_path)

    main._golden_throne_fire_times.clear()
    if db_path.exists():
        db_path.unlink()


def _write_doc(docs_dir: Path, frontmatter: str) -> Path:
    """Write a session doc file with the given YAML frontmatter block."""
    doc = docs_dir / f"doc-{uuid.uuid4().hex[:8]}.md"
    doc.write_text(f"---\n{frontmatter}\n---\n\n# session\n", encoding="utf-8")
    return doc


def _insert(db_path: Path, *, device_id: str, doc_path: Path | None) -> str:
    """Insert a golden_throne instance, optionally linked to a session doc."""
    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    doc_id = None
    if doc_path is not None:
        cur = conn.execute(
            "INSERT INTO session_documents (file_path, status) VALUES (?, 'active')",
            (str(doc_path),),
        )
        doc_id = cur.lastrowid
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            tmux_pane, status, instance_type, zealotry, session_doc_id,
            registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', ?, '%10', 'idle', 'golden_throne', 4, ?, ?, ?)""",
        (iid, str(uuid.uuid4()), f"gt-{iid[:8]}", "/tmp", device_id, doc_id, now, now),
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

        async def fake_log_event(*a, **k):
            return None

        async def fake_dispatch_notify(message, vibe=None, banner=None, instance_id=None):
            self.notifies.append(
                {"message": message, "vibe": vibe, "banner": banner, "instance_id": instance_id}
            )
            return {"success": True}

        async def fake_record_resume(instance):
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
async def test_incomplete_rubric_fires_accountability_prompt_not_sop(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    doc = _write_doc(gt_env.docs_dir, "victory:\n  a: true\n  b: false")
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME + "-remote", doc_path=doc)

    await main.golden_throne_followup(iid)

    assert len(rec.posts) == 1, "incomplete instance should dispatch to the remote pane"
    prompt = rec.posts[0]["json"]["prompt"]
    # Names the specific unmet condition and is the accountability prompt...
    assert "`b`" in prompt
    assert "Unmet conditions" in prompt
    assert "accountability" in prompt.lower()
    # ...NOT the flat static SOP.
    assert prompt != main._load_golden_throne_sop()
    assert "Read your session doc. Assess what remains." not in prompt

    # Condition-specific TTS/banner (the _for_rubric variants), not generic.
    assert len(rec.notifies) == 1
    assert "needs b" in rec.notifies[0]["message"]
    assert "missing b" in rec.notifies[0]["banner"]


@pytest.mark.asyncio
async def test_incomplete_rubric_humanizes_underscored_condition(gt_env, monkeypatch):
    main = gt_env.main
    rec = _Recorder(main, monkeypatch)
    doc = _write_doc(gt_env.docs_dir, "victory:\n  deploy_done: true\n  tests_passing: false")
    iid = _insert(gt_env.db_path, device_id=main.LOCAL_DEVICE_NAME + "-remote", doc_path=doc)

    await main.golden_throne_followup(iid)

    prompt = rec.posts[0]["json"]["prompt"]
    assert "`tests_passing`" in prompt  # raw key in the prompt
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
