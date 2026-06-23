"""Per-instance hook_driven lifecycle: set before an autonomous wake, cleared on
Stop/SessionEnd, and never flipped to global productivity by the agent-lifecycle
hooks anymore.

These pin the redesign that moved productivity to a read-time calculus
(compute_work_state) and made hook_driven the durable per-row substrate:
  * handle_prompt_submit / handle_post_tool_use no longer call set_productivity
  * handle_stop / handle_session_end clear hook_driven=0
  * dispatch → worker registration flags the worker hook_driven=1 iff the
    dispatcher (parent) is a non-Custodes agent (FG); Custodes/direct-Emperor → 0
  * stop-subscription delivery flags the subscriber hook_driven=1
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys


def _insert_instance(
    db_path, instance_id, *, parent=None, legion="astartes", status="idle", hook_driven=0
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status,
            parent_instance_id, legion, hook_driven)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?, ?, ?)""",
        (
            instance_id,
            f"{instance_id}-session",
            instance_id,
            "/tmp",
            status,
            parent,
            legion,
            hook_driven,
        ),
    )
    conn.commit()
    conn.close()


def _hook_driven(db_path, instance_id) -> int:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT hook_driven FROM legacy_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _input_lock(db_path, instance_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT input_lock FROM legacy_instances WHERE id = ?", (instance_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ── Stop / SessionEnd clear the flag ───────────────────────────────────────────


def test_stop_clears_hook_driven(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "flagged-1", hook_driven=1)

    async def run():
        res = await hooks.handle_stop({"session_id": "flagged-1"})
        assert res["success"] is True

    asyncio.run(run())
    assert _hook_driven(app_env.db_path, "flagged-1") == 0


def test_session_end_clears_hook_driven(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "flagged-2", hook_driven=1)

    async def run():
        await hooks.handle_session_end({"session_id": "flagged-2"})

    asyncio.run(run())
    assert _hook_driven(app_env.db_path, "flagged-2") == 0


def test_session_end_clears_input_lock(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "locked-1")
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            "UPDATE legacy_instances SET input_lock = ? WHERE id = ?",
            ("claude-cmd", "locked-1"),
        )
        conn.commit()
    monkeypatch.setattr(hooks, "_spawn_session_end_assertion", lambda *a, **k: None)
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)

    async def run():
        await hooks.handle_session_end({"session_id": "locked-1"})

    asyncio.run(run())
    assert _input_lock(app_env.db_path, "locked-1") is None


# ── Agent-lifecycle hooks no longer flip global productivity ────────────────────


async def _never_dead(db, session_id, existing, actor):
    return False


def test_prompt_submit_does_not_flip_global_productivity(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    shared = app_env.shared
    _insert_instance(app_env.db_path, "live-1")
    monkeypatch.setattr(hooks, "_stop_if_dead_pane", _never_dead)

    calls: list = []
    monkeypatch.setattr(
        shared.timer_engine, "set_productivity", lambda *a, **k: calls.append((a, k))
    )

    async def run():
        res = await hooks.handle_prompt_submit({"session_id": "live-1"})
        assert res["success"] is True
        assert res["action"] == "processing"
        assert res["exited_idle"] is False

    asyncio.run(run())
    assert calls == [], "handle_prompt_submit must not flip global productivity"


def test_post_tool_use_does_not_flip_global_productivity(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    shared = app_env.shared
    _insert_instance(app_env.db_path, "live-2")
    monkeypatch.setattr(hooks, "_stop_if_dead_pane", _never_dead)

    calls: list = []
    monkeypatch.setattr(
        shared.timer_engine, "set_productivity", lambda *a, **k: calls.append((a, k))
    )

    async def run():
        res = await hooks.handle_post_tool_use({"session_id": "live-2", "tool_name": "Bash"})
        assert res["success"] is True
        assert res["action"] == "heartbeat"

    asyncio.run(run())
    assert calls == [], "handle_post_tool_use must not flip global productivity"


# ── dispatch → worker registration classification (parent legion) ──────────────


def _register_child(app_env, monkeypatch, child_id, child_pane, parent_id):
    hooks = sys.modules["routes.hooks"]

    async def no_label(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)

    async def run():
        return await hooks.handle_session_start(
            {
                "session_id": child_id,
                "cwd": "/tmp",
                "env": {
                    "TMUX_PANE": child_pane,
                    "TOKEN_API_PARENT_INSTANCE_ID": parent_id,
                    "TOKEN_API_ENGINE": "claude",
                },
            }
        )

    return asyncio.run(run())


def test_fg_dispatched_worker_is_hook_driven(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "fg-1", legion="fabricator")
    _register_child(app_env, monkeypatch, "worker-1", "%51", "fg-1")
    assert _hook_driven(app_env.db_path, "worker-1") == 1


def test_custodes_dispatched_worker_is_not_hook_driven(app_env, monkeypatch):
    _insert_instance(app_env.db_path, "cust-1", legion="custodes")
    _register_child(app_env, monkeypatch, "worker-2", "%53", "cust-1")
    assert _hook_driven(app_env.db_path, "worker-2") == 0


def test_direct_emperor_launch_is_not_hook_driven(app_env, monkeypatch):
    # No parent instance id → direct-Emperor launch → not flagged.
    hooks = sys.modules["routes.hooks"]

    async def no_label(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)

    async def run():
        return await hooks.handle_session_start(
            {"session_id": "solo-1", "cwd": "/tmp", "env": {"TMUX_PANE": "%54"}}
        )

    asyncio.run(run())
    assert _hook_driven(app_env.db_path, "solo-1") == 0


# ── stop-subscription delivery flags the subscriber ────────────────────────────


def test_stop_subscription_delivery_flags_subscriber(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "watched-1")
    _insert_instance(app_env.db_path, "subscriber-1")

    async def fake_write(pane, payload):
        return {"status": "sent", "operation": "fake"}

    monkeypatch.setattr(hooks, "_direct_pane_write", fake_write)

    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane,
            event, delivery, status)
           VALUES ('watched-1', '%60', 'subscriber-1', '%61', 'stop', 'prompt', 'active')"""
    )
    conn.commit()
    conn.close()

    async def run():
        await hooks.handle_stop({"session_id": "watched-1"})

    asyncio.run(run())
    assert _hook_driven(app_env.db_path, "subscriber-1") == 1
