import sqlite3

import pytest
from fastapi.testclient import TestClient


def _insert_instance(
    db_path,
    *,
    instance_id,
    session_id,
    pane,
    engine,
    tab_name,
    status="idle",
):
    # ``pane`` is accepted for caller readability but no longer stored: pane
    # geometry is resolved live from the tmuxctl oracle. Selection is driven by
    # the faked ``_read_tmux_panes`` dict keyed by pane -> instance_id.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO legacy_instances (
            id, session_id, tab_name, origin_type, device_id, engine, status
        ) VALUES (?, ?, ?, 'local', 'mac', ?, ?)
        """,
        (instance_id, session_id, tab_name, engine, status),
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selector", "expected_ids"),
    [
        ("all", ["claude-1", "codex-1", "claude-2"]),
        ("engine=claude", ["claude-1", "claude-2"]),
        ("session=palace&window=NW", ["claude-1"]),
        ("tab_name~=codex", ["codex-1"]),
    ],
)
async def test_broadcast_temp_message_selector_grammar(
    app_env, monkeypatch, selector, expected_ids
):
    temp_message = app_env.main.temp_message_service
    _insert_instance(
        app_env.db_path,
        instance_id="claude-1",
        session_id="s-claude-1",
        pane="%1",
        engine="claude",
        tab_name="naming-nudge-worker",
    )
    _insert_instance(
        app_env.db_path,
        instance_id="codex-1",
        session_id="s-codex-1",
        pane="%2",
        engine="codex",
        tab_name="codex-temp-message",
    )
    _insert_instance(
        app_env.db_path,
        instance_id="claude-2",
        session_id="s-claude-2",
        pane="%3",
        engine="claude",
        tab_name="instance-name-cli",
    )

    async def fake_tmux_panes():
        return {
            "%1": {
                "tmux_session": "palace",
                "tmux_window": "NW",
                "tmux_session_window": "palace:NW",
                "instance_id": "claude-1",
            },
            "%2": {
                "tmux_session": "palace",
                "tmux_window": "NE",
                "tmux_session_window": "palace:NE",
                "instance_id": "codex-1",
            },
            "%3": {
                "tmux_session": "mechanicus",
                "tmux_window": "1",
                "tmux_session_window": "mechanicus:1",
                "instance_id": "claude-2",
            },
        }

    queued = []

    async def fake_queue_sender(**kwargs):
        queued.append(kwargs)
        return {"id": f"q-{kwargs['instance_id']}", "status": "pending"}

    async def fake_drainer(queue_id):
        return [{"queue_id": queue_id, "status": "sent"}]

    monkeypatch.setattr(temp_message, "_read_tmux_panes", fake_tmux_panes)

    receipts = await temp_message.broadcast_temp_message(
        selector,
        "roll call",
        idempotency_key=f"poll-{selector}",
        db_path=app_env.db_path,
        queue_sender=fake_queue_sender,
        queue_drainer=fake_drainer,
    )

    assert sorted(receipt["instance_id"] for receipt in receipts) == sorted(expected_ids)
    assert sorted(item["instance_id"] for item in queued) == sorted(expected_ids)


@pytest.mark.asyncio
async def test_temp_message_queues_semantic_ethereal_for_claude_and_codex(app_env, monkeypatch):
    temp_message = app_env.main.temp_message_service
    _insert_instance(
        app_env.db_path,
        instance_id="claude-1",
        session_id="s-claude-1",
        pane="%1",
        engine="claude",
        tab_name="claude-worker",
    )
    _insert_instance(
        app_env.db_path,
        instance_id="codex-1",
        session_id="s-codex-1",
        pane="%2",
        engine="codex",
        tab_name="codex-worker",
    )

    async def no_tmux():
        return {}

    queued = []

    async def fake_queue_sender(**kwargs):
        queued.append(kwargs)
        return {"id": f"q-{kwargs['instance_id']}", "status": "pending"}

    async def fake_drainer(queue_id):
        return [{"queue_id": queue_id, "status": "sent"}]

    monkeypatch.setattr(temp_message, "_read_tmux_panes", no_tmux)

    await temp_message.broadcast_temp_message(
        "all",
        "roll call",
        idempotency_key="poll-prefix",
        db_path=app_env.db_path,
        queue_sender=fake_queue_sender,
        queue_drainer=fake_drainer,
    )

    payloads = {item["instance_id"]: item["payload"] for item in queued}
    purposes = {item["instance_id"]: item["purpose"] for item in queued}
    assert payloads == {"claude-1": "roll call", "codex-1": "roll call"}
    assert purposes == {"claude-1": "ethereal", "codex-1": "ethereal"}


@pytest.mark.asyncio
async def test_codex_side_runtime_caveats_surface_in_receipt(app_env, monkeypatch):
    temp_message = app_env.main.temp_message_service
    _insert_instance(
        app_env.db_path,
        instance_id="codex-1",
        session_id="s-codex-1",
        pane="%2",
        engine="codex",
        tab_name="codex-worker",
    )

    async def no_tmux():
        return {}

    async def fake_queue_sender(**kwargs):
        return {"id": f"q-{kwargs['instance_id']}", "status": "pending"}

    async def fake_drainer(queue_id):
        return [{"queue_id": queue_id, "status": "sent"}]

    monkeypatch.setattr(temp_message, "_read_tmux_panes", no_tmux)

    receipts = await temp_message.broadcast_temp_message(
        "engine=codex",
        "roll call",
        idempotency_key="poll-codex-caveat",
        db_path=app_env.db_path,
        queue_sender=fake_queue_sender,
        queue_drainer=fake_drainer,
    )

    channel = receipts[0]["channel"]
    assert channel["kind"] == "ethereal"
    assert channel["command"] is None
    assert channel["availability"] == "not_preflighted"
    assert channel["tool_calls_inert"] is False
    assert "requires_started_conversation" in channel["caveats"]
    assert "unavailable_during_code_review" in channel["caveats"]


@pytest.mark.asyncio
async def test_temp_message_deferral_keeps_pending_poll(app_env, monkeypatch):
    temp_message = app_env.main.temp_message_service
    _insert_instance(
        app_env.db_path,
        instance_id="claude-1",
        session_id="s-claude-1",
        pane="%1",
        engine="claude",
        tab_name="claude-worker",
    )

    async def no_tmux():
        return {}

    async def fake_queue_sender(**kwargs):
        return {"id": "q-1", "status": "pending"}

    async def fake_drainer(queue_id):
        return [{"queue_id": queue_id, "status": "pending", "reason": "dispatch_deferred"}]

    monkeypatch.setattr(temp_message, "_read_tmux_panes", no_tmux)

    receipts = await temp_message.broadcast_temp_message(
        "engine=claude",
        "roll call",
        idempotency_key="poll-deferred",
        db_path=app_env.db_path,
        queue_sender=fake_queue_sender,
        queue_drainer=fake_drainer,
    )

    assert receipts[0]["status"] == "pending"
    conn = sqlite3.connect(app_env.db_path)
    rows = conn.execute("SELECT poll_id, instance_id, status FROM pending_polls").fetchall()
    conn.close()
    assert rows == [("poll-deferred", "claude-1", "pending")]


def test_temp_message_route_dry_run_previews_without_dispatch(app_env, monkeypatch) -> None:
    temp_message = app_env.main.temp_message_service
    _insert_instance(
        app_env.db_path,
        instance_id="claude-dry-run",
        session_id="s-claude-dry-run",
        pane="%1",
        engine="claude",
        tab_name="dry-run-target",
    )

    async def fake_tmux_panes():
        return {
            "%1": {
                "tmux_session": "palace",
                "tmux_window": "NW",
                "tmux_session_window": "palace:NW",
                "instance_id": "claude-dry-run",
            }
        }

    send_calls = []
    drain_calls = []

    async def fake_queue_sender(**kwargs):
        send_calls.append(kwargs)
        return {"id": "q-dry-run", "status": "pending"}

    async def fake_drainer(queue_id):
        drain_calls.append(queue_id)
        return [{"queue_id": queue_id, "status": "sent"}]

    monkeypatch.setattr(temp_message, "_read_tmux_panes", fake_tmux_panes)
    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fake_queue_sender)
    monkeypatch.setattr(app_env.main, "process_pane_write_queue_once", fake_drainer)

    client = TestClient(app_env.main.app)
    response = client.post(
        "/api/orchestrator/temp_message",
        json={"selector": "engine=claude", "payload": "roll call", "dry_run": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["target_count"] == 1
    receipt = body["receipts"][0]
    assert receipt["status"] == "previewed"
    assert receipt["dispatch"] == {"status": "skipped_dry_run"}
    assert receipt["payload"] == "roll call"
    assert receipt["kind"] == "ethereal"
    assert receipt["instance_id"] == "claude-dry-run"
    assert send_calls == []
    assert drain_calls == []


def test_temp_message_route_without_dry_run_dispatches_normally(app_env, monkeypatch) -> None:
    temp_message = app_env.main.temp_message_service
    _insert_instance(
        app_env.db_path,
        instance_id="claude-live-path",
        session_id="s-claude-live-path",
        pane="%1",
        engine="claude",
        tab_name="live-path-target",
    )

    async def fake_tmux_panes():
        return {
            "%1": {
                "tmux_session": "palace",
                "tmux_window": "NW",
                "tmux_session_window": "palace:NW",
                "instance_id": "claude-live-path",
            }
        }

    send_calls = []
    drain_calls = []

    async def fake_queue_sender(**kwargs):
        send_calls.append(kwargs)
        return {"id": "q-live-path", "status": "pending"}

    async def fake_drainer(queue_id):
        drain_calls.append(queue_id)
        return [{"queue_id": queue_id, "status": "sent"}]

    monkeypatch.setattr(temp_message, "_read_tmux_panes", fake_tmux_panes)
    monkeypatch.setattr(app_env.main, "enqueue_pane_write", fake_queue_sender)
    monkeypatch.setattr(app_env.main, "process_pane_write_queue_once", fake_drainer)

    client = TestClient(app_env.main.app)
    response = client.post(
        "/api/orchestrator/temp_message",
        json={"selector": "engine=claude", "payload": "roll call"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_count"] == 1
    receipt = body["receipts"][0]
    assert receipt["status"] == "sent"
    assert len(send_calls) == 1
    assert len(drain_calls) == 1
    assert send_calls[0]["payload"] == "roll call"
    assert send_calls[0]["purpose"] == "ethereal"
    assert send_calls[0]["instance_id"] == "claude-live-path"
