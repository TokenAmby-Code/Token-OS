import sqlite3

import pytest


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
async def test_temp_message_uses_btw_for_claude_and_side_for_codex(app_env, monkeypatch):
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
    assert payloads == {
        "claude-1": "/btw roll call",
        "codex-1": "/side roll call",
    }


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
    assert channel["command"] == "/side"
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
