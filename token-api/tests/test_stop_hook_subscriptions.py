import asyncio
import importlib.machinery
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path


def _insert_instance(
    db_path,
    instance_id,
    pane=None,
    parent=None,
    status="idle",
    pane_label=None,
    dispatch_target=None,
    dispatch_window=None,
    engine=None,
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status, tmux_pane,
            parent_instance_id, pane_label, dispatch_target, dispatch_window, engine)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'p', 'v', 's', ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            f"{instance_id}-session",
            instance_id,
            "/tmp",
            status,
            pane,
            parent,
            pane_label,
            dispatch_target,
            dispatch_window,
            engine,
        ),
    )
    conn.commit()
    conn.close()


def test_session_start_auto_subscribes_parent(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "parent-1", pane="%10")

    async def no_label(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": "child-1",
                "cwd": "/tmp",
                "env": {
                    "TMUX_PANE": "%11",
                    "TOKEN_API_PARENT_INSTANCE_ID": "parent-1",
                    "TOKEN_API_ENGINE": "claude",
                },
            }
        )
        assert result["success"] is True
        assert result["stop_subscription"]["subscriber_pane"] == "%10"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        """SELECT target_instance_id, target_pane, subscriber_instance_id, subscriber_pane, status
           FROM stop_hook_subscriptions"""
    ).fetchone()
    conn.close()
    assert row == ("child-1", "%11", "parent-1", "%10", "active")


def test_stop_hook_live_delivery_dedupes_and_suppresses_legacy(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "parent-2", pane="%20")
    _insert_instance(app_env.db_path, "child-2", pane="%21", parent="parent-2")
    sent = []

    async def fake_write(pane, payload):
        sent.append((pane, payload))
        return {"status": "sent", "operation": "fake"}

    monkeypatch.setattr(hooks, "_direct_pane_write", fake_write)

    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane, event, delivery, status)
           VALUES ('child-2', '%21', 'parent-2', '%20', 'stop', 'prompt', 'active')"""
    )
    conn.commit()
    conn.close()

    tail = json.dumps(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "STOP_HOOK_SMOKE_OK"}],
            },
        },
        separators=(",", ":"),
    )

    async def run():
        first = await hooks.handle_stop({"session_id": "child-2", "transcript_tail": tail})
        second = await hooks.handle_stop({"session_id": "child-2", "transcript_tail": tail})
        assert first["stop_subscriptions"][0]["status"] == "sent"
        assert second["stop_subscriptions"][0]["status"] == "duplicate"

    asyncio.run(run())

    assert len(sent) == 1
    assert sent[0][0] == "%20"
    assert "STOP_HOOK_SMOKE_OK" in sent[0][1]
    conn = sqlite3.connect(app_env.db_path)
    deliveries = conn.execute("SELECT status FROM stop_hook_deliveries").fetchall()
    injections = conn.execute("SELECT COUNT(*) FROM state_injections").fetchone()[0]
    conn.close()
    assert deliveries == [("sent",)]
    assert injections == 0


def test_explicit_subscribe_unsubscribe(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "target-1", pane="%30")
    _insert_instance(app_env.db_path, "sub-1", pane="%31")

    async def run():
        sub = await hooks.subscribe_hook(
            hooks.HookSubscribeRequest(target_pane="%30", subscriber_pane="%31")
        )
        assert sub["success"] is True
        listed = await hooks.list_hook_subscriptions()
        assert listed["count"] == 1
        unsub = await hooks.unsubscribe_hook(
            hooks.HookUnsubscribeRequest(target_pane="%30", subscriber_pane="%31")
        )
        assert unsub["count"] == 1
        listed2 = await hooks.list_hook_subscriptions()
        assert listed2["count"] == 0

    asyncio.run(run())


def test_mechanicus_worker_session_start_auto_subscribes_to_live_fg(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path,
        "fg-1",
        pane="%40",
        pane_label="mechanicus:fabricator-general",
    )

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": "worker-1",
                "cwd": "/tmp",
                "env": {
                    "TMUX_PANE": "%41",
                    "TOKEN_API_PANE_LABEL": "mechanicus:1",
                    "TOKEN_API_ENGINE": "claude",
                },
            }
        )
        mech = result["mechanicus_stop_subscription"]
        assert mech["created"] == 1
        assert mech["subscriptions"][0]["subscriber_pane"] == "%40"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        """SELECT target_instance_id, target_pane, subscriber_instance_id, subscriber_pane, status
           FROM stop_hook_subscriptions"""
    ).fetchone()
    conn.close()
    assert row == ("worker-1", "%41", "fg-1", "%40", "active")


def test_fg_session_start_reconciles_existing_mechanicus_workers(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "worker-2", pane="%42", pane_label="mechanicus:2")
    _insert_instance(app_env.db_path, "worker-3", pane="%43", pane_label="mechanicus:worker-7")

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": "fg-2",
                "cwd": "/tmp",
                "env": {
                    "TMUX_PANE": "%44",
                    "TOKEN_API_PANE_LABEL": "mechanicus:fabricator-general",
                    "TOKEN_API_ENGINE": "claude",
                },
            }
        )
        mech = result["mechanicus_stop_subscription"]
        assert mech["created"] == 2
        assert {row["target_instance_id"] for row in mech["subscriptions"]} == {
            "worker-2",
            "worker-3",
        }

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    rows = conn.execute(
        "SELECT target_instance_id, subscriber_instance_id, subscriber_pane FROM stop_hook_subscriptions ORDER BY target_instance_id"
    ).fetchall()
    conn.close()
    assert rows == [("worker-2", "fg-2", "%44"), ("worker-3", "fg-2", "%44")]


def test_mechanicus_spillover_worker_reconciles_without_numeric_label(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path,
        "fg-spill",
        pane="%45",
        pane_label="mechanicus:fabricator-general",
    )
    _insert_instance(
        app_env.db_path,
        "worker-spill",
        pane="%46",
        dispatch_target="mechanicus:new",
        dispatch_window="mechanicus-2",
    )

    async def no_label(_pane):
        return None

    async def run():
        result = await hooks.reconcile_hook_subscriptions(
            hooks.HookReconcileRequest(page="mechanicus")
        )
        assert result["created"] == 1
        assert result["subscriptions"][0]["target_instance_id"] == "worker-spill"

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    asyncio.run(run())


def test_mechanicus_fg_and_admin_are_never_subscription_targets(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path,
        "fg-3",
        pane="%50",
        pane_label="mechanicus:fabricator-general",
    )
    _insert_instance(app_env.db_path, "admin-1", pane="%51", pane_label="mechanicus:admin")

    async def run():
        result = await hooks.reconcile_hook_subscriptions(
            hooks.HookReconcileRequest(page="mechanicus")
        )
        assert result["created"] == 0
        assert result["skipped"] == 2

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    count = conn.execute("SELECT COUNT(*) FROM stop_hook_subscriptions").fetchone()[0]
    conn.close()
    assert count == 0


def test_mechanicus_reconcile_is_idempotent(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path,
        "fg-4",
        pane="%60",
        pane_label="mechanicus:fabricator-general",
    )
    _insert_instance(app_env.db_path, "worker-4", pane="%61", pane_label="mechanicus:1")

    async def run():
        first = await hooks.reconcile_hook_subscriptions(
            hooks.HookReconcileRequest(page="mechanicus")
        )
        second = await hooks.reconcile_hook_subscriptions(
            hooks.HookReconcileRequest(page="mechanicus")
        )
        assert first["created"] == 1
        assert second["existing"] == 1

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    count = conn.execute("SELECT COUNT(*) FROM stop_hook_subscriptions").fetchone()[0]
    conn.close()
    assert count == 1


def test_mechanicus_worker_start_without_fg_does_not_create_subscription(app_env):
    hooks = sys.modules["routes.hooks"]

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": "worker-no-fg",
                "cwd": "/tmp",
                "env": {
                    "TMUX_PANE": "%71",
                    "TOKEN_API_PANE_LABEL": "mechanicus:1",
                    "TOKEN_API_ENGINE": "claude",
                },
            }
        )
        assert result["mechanicus_stop_subscription"]["action"] == "no_live_fabricator_general"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    count = conn.execute("SELECT COUNT(*) FROM stop_hook_subscriptions").fetchone()[0]
    conn.close()
    assert count == 0


def test_hook_reconcile_api_reports_counts(app_env):
    from fastapi.testclient import TestClient

    _insert_instance(
        app_env.db_path,
        "fg-5",
        pane="%80",
        pane_label="mechanicus:fabricator-general",
    )
    _insert_instance(app_env.db_path, "worker-5", pane="%81", pane_label="mechanicus:5")
    client = TestClient(app_env.main.app)

    resp = client.post("/api/hooks/reconcile", json={"page": "mechanicus"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["created"] == 1
    assert data["existing"] == 0


def test_hook_reconcile_cli_calls_api(monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "cli-tools" / "bin" / "hook"
    loader = importlib.machinery.SourceFileLoader("hook_cli", str(path))
    spec = importlib.util.spec_from_loader("hook_cli", loader)
    hook_cli = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(hook_cli)
    calls = []

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        return {
            "success": True,
            "action": "reconciled",
            "page": "mechanicus",
            "created": 1,
            "existing": 2,
            "skipped": 3,
        }

    monkeypatch.setattr(hook_cli, "_request", fake_request)
    rc = hook_cli.main(["reconcile", "--page", "mechanicus"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [("POST", "/api/hooks/reconcile", {"page": "mechanicus"})]
    assert "created=1 existing=2 skipped=3" in out


def test_custom_oneshot_stop_subscription_delivers_payload_and_deactivates(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "self-plan", pane="%90")
    sent = []

    async def fake_write(pane, payload):
        sent.append((pane, payload))
        return {"status": "sent", "operation": "fake"}

    monkeypatch.setattr(hooks, "_direct_pane_write", fake_write)

    async def run():
        sub = await hooks.subscribe_hook(
            hooks.HookSubscribeRequest(
                target_pane="%90",
                subscriber_pane="%90",
                purpose="preplan_plan",
                payload="/plan create the plan",
                oneshot=True,
            )
        )
        assert sub["success"] is True
        result = await hooks.handle_stop({"session_id": "self-plan", "transcript_tail": ""})
        assert result["stop_subscriptions"][0]["status"] == "sent"

    asyncio.run(run())

    assert sent == [("%90", "/plan create the plan")]
    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        "SELECT status, purpose, payload, oneshot FROM stop_hook_subscriptions WHERE target_instance_id='self-plan'"
    ).fetchone()
    conn.close()
    assert row == ("delivered", "preplan_plan", "/plan create the plan", 1)


def test_planning_state_endpoint_cycles_and_projects_pane_var(app_env):
    from fastapi.testclient import TestClient

    _insert_instance(app_env.db_path, "planner-1", pane="%91", engine="codex")
    client = TestClient(app_env.main.app)

    resp = client.post(
        "/api/planning/state",
        json={"tmux_pane": "%91", "cycle": True, "source": "test"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["previous_state"] == "none"
    assert data["planning_state"] == "preplanning"
    assert data["instance_id"] == "planner-1"
    assert data["engine"] == "codex"

    conn = sqlite3.connect(app_env.db_path)
    inst = conn.execute(
        "SELECT planning_state, planning_source FROM claude_instances WHERE id='planner-1'"
    ).fetchone()
    queued = conn.execute(
        "SELECT variable, value, tmux_pane FROM pane_state_queue WHERE instance_id='planner-1' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    event_count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE instance_id='planner-1' AND event_type='planning_state_changed'"
    ).fetchone()[0]
    conn.close()

    assert inst == ("preplanning", "test")
    assert queued == ("@PLANNING_STATE", "preplanning", "%91")
    assert event_count == 1
