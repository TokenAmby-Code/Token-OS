import json
import sqlite3
from datetime import datetime

import pytest

from instance_mutation import insert_instance_sync


@pytest.fixture(autouse=True)
def _enable_naming_nudge_interviews_for_existing_tests(app_env, monkeypatch):
    """Keep legacy behavior assertions explicit while production is gated off."""
    monkeypatch.setattr(app_env.main, "ENABLE_NAMING_NUDGE_INTERVIEWS", True)


def _insert_instance(
    db_path,
    *,
    instance_id="inst-naming",
    tab_name="Claude 13:13",
    session_doc_id=1,
    workflow_blocked_reason=None,
    persona_slug=None,
):
    conn = sqlite3.connect(db_path)
    if session_doc_id is not None:
        conn.execute(
            """
            INSERT INTO session_documents (id, file_path, title, project)
            VALUES (?, ?, ?, ?)
            """,
            (
                session_doc_id,
                "/Volumes/Imperium/Imperium-ENV/Mars/Sessions/2026-05-10-anti-archaeology-chunk-a-naming-nudge.md",
                "Anti-Archaeology Chunk A",
                "anti-archaeology-v2",
            ),
        )
    if persona_slug:
        conn.execute(
            """
            INSERT INTO instances (
                id, name, origin_type, device_id, status, session_doc_id,
                workflow_blocked_reason, persona_id, rank
            ) VALUES (?, ?, 'api', 'Mac-Mini', 'working', ?, ?,
                      (SELECT id FROM personas WHERE slug = ?),
                      (SELECT default_rank FROM personas WHERE slug = ?))
            """,
            (
                instance_id,
                tab_name,
                session_doc_id,
                workflow_blocked_reason,
                persona_slug,
                persona_slug,
            ),
        )
    else:
        now = datetime.now().isoformat()
        insert_instance_sync(
            conn,
            values={
                "id": instance_id,
                "name": "needs-name",
                "origin_type": "api",
                "device_id": "Mac-Mini",
                "status": "working",
                "session_doc_id": session_doc_id,
                "workflow_blocked_reason": workflow_blocked_reason,
                "created_at": now,
                "last_activity": now,
            },
            mutation_type="instance_registered",
            write_source="test",
            actor="test",
        )
        if tab_name != "needs-name":
            conn.execute("UPDATE instances SET name = ? WHERE id = ?", (tab_name, instance_id))
    conn.commit()
    conn.close()


def _patch_pane(app_env, monkeypatch, pane="%10", instance_id="inst-naming"):
    """The nudge pane is resolved live from the oracle now (no stored tmux_pane);
    pin it so the placeholder pane is addressable for the nudge enqueue. Asserts the
    code resolves the EXPECTED instance — a wrong-row resolve fails the test instead
    of silently passing."""

    async def _resolve(_instance_id):
        assert _instance_id == instance_id, f"unexpected instance resolved: {_instance_id!r}"
        return (pane, "main")

    monkeypatch.setattr(app_env.main.shared, "resolve_instance_pane", _resolve)


def _fetchone(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(query, params).fetchone()
    conn.close()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_naming_nudge_interview_gate_silences_delivery(app_env, monkeypatch):
    _insert_instance(app_env.db_path)
    monkeypatch.setattr(app_env.main, "ENABLE_NAMING_NUDGE_INTERVIEWS", False)

    async def fail_enqueue(**_kwargs):
        raise AssertionError("disabled naming interviews must not enqueue pane writes")

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fail_enqueue)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(session_id="inst-naming")
    )

    assert result["action"] == "disabled"
    assert result["reason"] == "temporary_session_doc_binding_registration_gate"
    assert result["instance_id"] == "inst-naming"
    assert (
        _fetchone(
            app_env.db_path,
            "SELECT event_type FROM events WHERE instance_id = 'inst-naming' "
            "AND event_type = 'naming_nudge_sent'",
        )
        is None
    )
    assert (
        _fetchone(
            app_env.db_path,
            "SELECT id FROM pane_write_queue WHERE instance_id = 'inst-naming' "
            "AND source = 'naming_nudge'",
        )
        is None
    )


@pytest.mark.asyncio
async def test_naming_nudge_sends_for_placeholder_and_derives_slug(app_env, monkeypatch):
    _insert_instance(app_env.db_path)
    _patch_pane(app_env, monkeypatch)
    enqueued = []

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return {"id": "queue-1", **kwargs, "status": "pending"}

    async def fake_process(queue_id):
        return [{"queue_id": queue_id, "status": "sent"}]

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fake_enqueue)
    monkeypatch.setattr(app_env.main, "process_pane_write_queue_once", fake_process)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(session_id="inst-naming")
    )

    assert result["action"] == "nudge_sent"
    assert result["slug"] == "anti-archaeology-chunk-a-naming-nudge"
    assert result["nudge_number"] == 1
    assert enqueued[0]["tmux_pane"] == "%10"
    assert enqueued[0]["source"] == "naming_nudge"
    assert 'session-doc-name "Your Descriptive Title"' in enqueued[0]["payload"]
    assert "Do not use dates" in enqueued[0]["payload"]
    assert "continue the original user task uninterrupted" in enqueued[0]["payload"]
    assert "do not chase it" in enqueued[0]["payload"]

    row = _fetchone(
        app_env.db_path,
        "SELECT workflow_blocked_reason FROM instances WHERE id = 'inst-naming'",
    )
    assert row["workflow_blocked_reason"] == "tab_name_placeholder"

    event = _fetchone(
        app_env.db_path,
        "SELECT event_type, details FROM events WHERE instance_id = 'inst-naming'",
    )
    assert event["event_type"] == "naming_nudge_sent"
    assert json.loads(event["details"])["nudge_number"] == 1


@pytest.mark.asyncio
async def test_naming_nudge_noops_after_instance_named(app_env, monkeypatch):
    _insert_instance(app_env.db_path, tab_name="active-naming-hook")

    async def fail_enqueue(**_kwargs):
        raise AssertionError("named panes must not be nudged")

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fail_enqueue)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(instance_id="inst-naming")
    )

    assert result["action"] == "noop_named"


@pytest.mark.asyncio
async def test_naming_nudge_noops_for_singleton_persona_pane(app_env, monkeypatch):
    """Persona seats keep their persona-managed identity and must not be
    interrupted with the interactive pane/session naming interview."""
    _insert_instance(
        app_env.db_path,
        tab_name="needs-name",
        session_doc_id=None,
        persona_slug="fabricator-general",
    )

    async def fail_resolve(_instance_id):
        raise AssertionError("persona panes should be exempt before pane resolution")

    async def fail_enqueue(**_kwargs):
        raise AssertionError("persona panes must not be naming-nudged")

    monkeypatch.setattr(app_env.main.shared, "resolve_instance_pane", fail_resolve)
    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fail_enqueue)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(instance_id="inst-naming")
    )

    assert result["action"] == "noop_persona_pane"
    assert result["persona_slug"] == "fabricator-general"


@pytest.mark.asyncio
async def test_naming_nudge_caps_at_three_and_marks_refused(app_env, monkeypatch):
    _insert_instance(app_env.db_path)
    _patch_pane(app_env, monkeypatch)
    conn = sqlite3.connect(app_env.db_path)
    for n in range(3):
        conn.execute(
            "INSERT INTO events (event_type, instance_id, details) VALUES (?, ?, ?)",
            ("naming_nudge_sent", "inst-naming", json.dumps({"nudge_number": n + 1})),
        )
    conn.commit()
    conn.close()

    async def fail_enqueue(**_kwargs):
        raise AssertionError("cap reached must not enqueue another nudge")

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fail_enqueue)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(session_id="inst-naming")
    )

    assert result["action"] == "cap_reached"
    assert result["workflow_blocked_reason"] == "naming_refused"
    row = _fetchone(
        app_env.db_path,
        "SELECT workflow_blocked_reason FROM instances WHERE id = 'inst-naming'",
    )
    assert row["workflow_blocked_reason"] == "naming_refused"


@pytest.mark.asyncio
async def test_naming_nudge_does_not_duplicate_pending_queue_item(app_env, monkeypatch):
    _insert_instance(app_env.db_path)
    _patch_pane(app_env, monkeypatch)
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """
        INSERT INTO pane_write_queue (
            id, instance_id, tmux_pane, source, purpose, payload, status
        ) VALUES ('queue-existing', 'inst-naming', '%10', 'naming_nudge', 'name_missing', 'rename', 'pending')
        """
    )
    conn.commit()
    conn.close()

    async def fail_enqueue(**_kwargs):
        raise AssertionError("pending nudge should suppress duplicate enqueue")

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fail_enqueue)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(session_id="inst-naming")
    )

    assert result["action"] == "noop_pending_nudge"


@pytest.mark.asyncio
async def test_naming_nudge_sends_for_numbered_placeholder(app_env, monkeypatch):
    """Numbered placeholder variants (needs-session-name-345) are now caught by
    the centralized regex and must be nudged, not silently treated as named."""
    _insert_instance(app_env.db_path, tab_name="needs-session-name-345")
    _patch_pane(app_env, monkeypatch)
    enqueued = []

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return {"id": "queue-1", **kwargs, "status": "pending"}

    async def fake_process(queue_id):
        return [{"queue_id": queue_id, "status": "sent"}]

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fake_enqueue)
    monkeypatch.setattr(app_env.main, "process_pane_write_queue_once", fake_process)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(session_id="inst-naming")
    )

    assert result["action"] == "nudge_sent"
    assert enqueued and enqueued[0]["source"] == "naming_nudge"


@pytest.mark.asyncio
async def test_naming_nudge_doc_less_instance_uses_instance_name_message(
    app_env, monkeypatch
) -> None:
    """A placeholder-named instance with NO session doc must be told to run
    `instance-name` (it owns its pane name directly), not `session-doc-name`."""
    _insert_instance(app_env.db_path, tab_name="needs-name", session_doc_id=None)
    _patch_pane(app_env, monkeypatch)
    enqueued = []

    async def fake_enqueue(**kwargs):
        enqueued.append(kwargs)
        return {"id": "queue-1", **kwargs, "status": "pending"}

    async def fake_process(queue_id):
        return [{"queue_id": queue_id, "status": "sent"}]

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fake_enqueue)
    monkeypatch.setattr(app_env.main, "process_pane_write_queue_once", fake_process)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(session_id="inst-naming")
    )

    assert result["action"] == "nudge_sent"
    payload = enqueued[0]["payload"]
    assert 'instance-name "your-title"' in payload
    assert "session-doc-name" not in payload
    assert "continue the original user task uninterrupted" in payload
    assert "do not chase it" in payload

    row = _fetchone(
        app_env.db_path,
        "SELECT workflow_blocked_reason FROM instances WHERE id = 'inst-naming'",
    )
    assert row["workflow_blocked_reason"] == "tab_name_placeholder"

    event = _fetchone(
        app_env.db_path,
        "SELECT event_type FROM events WHERE instance_id = 'inst-naming' AND event_type = 'naming_nudge_sent'",
    )
    assert event["event_type"] == "naming_nudge_sent"


@pytest.mark.asyncio
async def test_naming_nudge_noops_for_null_doc_instance(app_env, monkeypatch):
    """An automated launch left with NULL session_doc_id and no placeholder tab
    name must not be infinitely nudged — the gate keys on tab_name."""
    _insert_instance(app_env.db_path, tab_name="active-docless-instance", session_doc_id=None)

    async def fail_enqueue(**_kwargs):
        raise AssertionError("NULL-doc instance must not be nudged")

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fail_enqueue)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(session_id="inst-naming")
    )

    assert result["action"] == "noop_named"
