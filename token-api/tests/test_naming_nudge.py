import json
import sqlite3

import pytest


def _insert_instance(
    db_path,
    *,
    instance_id="inst-naming",
    tab_name="Claude 13:13",
    tmux_pane="%10",
    session_doc_id=1,
    workflow_blocked_reason=None,
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
    conn.execute(
        """
        INSERT INTO claude_instances (
            id, session_id, tab_name, origin_type, device_id, status,
            tmux_pane, session_doc_id, workflow_blocked_reason
        ) VALUES (?, ?, ?, 'hook', 'Mac-Mini', 'processing', ?, ?, ?)
        """,
        (
            instance_id,
            f"session-{instance_id}",
            tab_name,
            tmux_pane,
            session_doc_id,
            workflow_blocked_reason,
        ),
    )
    conn.commit()
    conn.close()


def _fetchone(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(query, params).fetchone()
    conn.close()
    return dict(row) if row else None


@pytest.mark.asyncio
async def test_naming_nudge_sends_for_placeholder_and_derives_slug(app_env, monkeypatch):
    _insert_instance(app_env.db_path)
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

    row = _fetchone(
        app_env.db_path,
        "SELECT workflow_blocked_reason FROM claude_instances WHERE id = 'inst-naming'",
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
async def test_naming_nudge_caps_at_three_and_marks_refused(app_env, monkeypatch):
    _insert_instance(app_env.db_path)
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
        "SELECT workflow_blocked_reason FROM claude_instances WHERE id = 'inst-naming'",
    )
    assert row["workflow_blocked_reason"] == "naming_refused"


@pytest.mark.asyncio
async def test_naming_nudge_does_not_duplicate_pending_queue_item(app_env, monkeypatch):
    _insert_instance(app_env.db_path)
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
async def test_naming_nudge_doc_less_instance_uses_instance_name_message(app_env, monkeypatch) -> None:
    """A placeholder-named instance with NO session doc must be told to run
    `instance-name` (it owns its pane name directly), not `session-doc-name`."""
    _insert_instance(app_env.db_path, tab_name="needs-name", session_doc_id=None)
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

    row = _fetchone(
        app_env.db_path,
        "SELECT workflow_blocked_reason FROM claude_instances WHERE id = 'inst-naming'",
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
    _insert_instance(app_env.db_path, tab_name=None, session_doc_id=None)

    async def fail_enqueue(**_kwargs):
        raise AssertionError("NULL-doc instance must not be nudged")

    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fail_enqueue)

    result = await app_env.main.orchestrator_naming_nudge(
        app_env.main.NamingNudgeRequest(session_id="inst-naming")
    )

    assert result["action"] == "noop_named"
