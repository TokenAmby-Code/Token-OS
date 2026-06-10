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
                    "TOKEN_API_PARENT_INSTANCE_ID": "fg-1",
                    "TOKEN_API_ENGINE": "claude",
                },
            }
        )
        mech = result["mechanicus_stop_subscription"]
        # A verified FG child subscribes to FG. The parent→child auto-subscribe
        # may create the row first, so accept created OR existing.
        assert mech["created"] + mech["existing"] == 1
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
    _insert_instance(
        app_env.db_path, "worker-2", pane="%42", pane_label="mechanicus:2", parent="fg-2"
    )
    _insert_instance(
        app_env.db_path,
        "worker-3",
        pane="%43",
        pane_label="mechanicus:worker-7",
        parent="fg-2",
    )

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
        parent="fg-spill",
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
    _insert_instance(
        app_env.db_path, "worker-4", pane="%61", pane_label="mechanicus:1", parent="fg-4"
    )

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
    _insert_instance(
        app_env.db_path, "worker-5", pane="%81", pane_label="mechanicus:5", parent="fg-5"
    )
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


def test_hook_gc_cli_calls_prune_api(monkeypatch, capsys):
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
            "action": "prune_preview",
            "confirmed": False,
            "count": 2,
            "active_remaining": 5,
            "removed": [],
        }

    monkeypatch.setattr(hook_cli, "_request", fake_request)
    rc = hook_cli.main(["gc"])
    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [("POST", "/api/hooks/prune", {"confirm": False, "event": "stop"})]
    assert "would remove=2" in out


def test_custom_oneshot_stop_subscription_delivers_payload_and_deactivates(app_env, monkeypatch):
    monkeypatch.setenv("TOKEN_API_TEST_ALLOW_STAMPED_PANE_FALLBACK", "1")
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


def _insert_subscription(
    db_path,
    *,
    target_instance_id,
    target_pane,
    subscriber_instance_id,
    subscriber_pane,
    status="active",
    purpose="generic",
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane,
            event, delivery, status, purpose)
           VALUES (?, ?, ?, ?, 'stop', 'prompt', ?, ?)""",
        (
            target_instance_id,
            target_pane,
            subscriber_instance_id,
            subscriber_pane,
            status,
            purpose,
        ),
    )
    conn.commit()
    conn.close()


def _active_subscription_ids(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id FROM stop_hook_subscriptions WHERE status='active' ORDER BY id"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


# ---- Bug B: reconcile subscribes ONLY verified live FG children -------------


def test_reconcile_skips_worker_with_dead_parent(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path, "fg-dead", pane="%70", pane_label="mechanicus:fabricator-general"
    )
    # parent FK points at a phantom uuid with no instance row.
    _insert_instance(
        app_env.db_path,
        "worker-orphan",
        pane="%71",
        pane_label="mechanicus:3",
        parent="ghost-uuid",
    )

    async def run():
        result = await hooks.reconcile_hook_subscriptions(
            hooks.HookReconcileRequest(page="mechanicus")
        )
        assert result["created"] == 0
        reasons = {s["reason"] for s in result["skipped_targets"]}
        assert "parent_not_live" in reasons

    asyncio.run(run())
    assert _active_subscription_ids(app_env.db_path) == []


def test_reconcile_skips_worker_parented_to_other_live_instance(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path, "fg-other", pane="%72", pane_label="mechanicus:fabricator-general"
    )
    # A different LIVE commander owns this worker — it is NOT an FG child.
    _insert_instance(app_env.db_path, "custodes-live", pane="%73", pane_label="legion:custodes")
    _insert_instance(
        app_env.db_path,
        "worker-elsewhere",
        pane="%74",
        pane_label="mechanicus:6",
        parent="custodes-live",
    )

    async def run():
        result = await hooks.reconcile_hook_subscriptions(
            hooks.HookReconcileRequest(page="mechanicus")
        )
        assert result["created"] == 0
        reasons = {s["reason"] for s in result["skipped_targets"]}
        assert "parent_not_fg" in reasons

    asyncio.run(run())
    assert _active_subscription_ids(app_env.db_path) == []


def test_reconcile_skips_parentless_worker(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path, "fg-lonely", pane="%75", pane_label="mechanicus:fabricator-general"
    )
    _insert_instance(app_env.db_path, "worker-parentless", pane="%76", pane_label="mechanicus:7")

    async def run():
        result = await hooks.reconcile_hook_subscriptions(
            hooks.HookReconcileRequest(page="mechanicus")
        )
        assert result["created"] == 0
        reasons = {s["reason"] for s in result["skipped_targets"]}
        assert "parent_not_live" in reasons

    asyncio.run(run())
    assert _active_subscription_ids(app_env.db_path) == []


# ---- Bug A: unsubscribe matches by instance-id OR pane ----------------------


def test_unsubscribe_matches_by_instance_uuid_in_pane_slot(app_env):
    """`unsubscribe --pane <uuid> --notify <uuid>` must match+delete.

    The CLI feeds UUIDs into the pane slots; the watched pane is live and the
    notify UUID is exact. The old clause only matched the *_pane columns (which
    hold %NN), so it removed nothing.
    """
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "watched-live", pane="%30")
    _insert_instance(app_env.db_path, "notify-live", pane="%31")
    _insert_subscription(
        app_env.db_path,
        target_instance_id="watched-live",
        target_pane="%30",
        subscriber_instance_id="notify-live",
        subscriber_pane="%31",
    )

    async def run():
        unsub = await hooks.unsubscribe_hook(
            hooks.HookUnsubscribeRequest(target_pane="watched-live", subscriber_pane="notify-live")
        )
        assert unsub["count"] == 1

    asyncio.run(run())
    assert _active_subscription_ids(app_env.db_path) == []


def test_unsubscribe_matches_phantom_notify_uuid(app_env):
    """Notify target is a phantom UUID with no instance row — still unsubscribable."""
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "watched-2", pane="%36")
    _insert_subscription(
        app_env.db_path,
        target_instance_id="watched-2",
        target_pane="%36",
        subscriber_instance_id="phantom-uuid",
        subscriber_pane="%32",
    )

    async def run():
        unsub = await hooks.unsubscribe_hook(
            hooks.HookUnsubscribeRequest(target_pane="watched-2", subscriber_pane="phantom-uuid")
        )
        assert unsub["count"] == 1

    asyncio.run(run())
    assert _active_subscription_ids(app_env.db_path) == []


# ---- Prune: GC dangling watched/notify references ---------------------------


def test_prune_removes_dangling_keeps_live(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "live-a", pane="%14")
    _insert_instance(app_env.db_path, "live-b", pane="%10")
    # keep: both endpoints live
    _insert_subscription(
        app_env.db_path,
        target_instance_id="live-a",
        target_pane="%14",
        subscriber_instance_id="live-b",
        subscriber_pane="%10",
    )
    # remove: watched instance is dead/stopped
    _insert_subscription(
        app_env.db_path,
        target_instance_id="dead-watched",
        target_pane="%99",
        subscriber_instance_id="live-b",
        subscriber_pane="%10",
    )
    # remove: notify instance is a phantom uuid
    _insert_subscription(
        app_env.db_path,
        target_instance_id="live-a",
        target_pane="%14",
        subscriber_instance_id="phantom",
        subscriber_pane="%32",
    )

    async def run():
        import aiosqlite

        async with aiosqlite.connect(app_env.db_path) as db:
            preview = await hooks._prune_dangling_stop_subscriptions(db, confirm=False)
            assert preview["action"] == "prune_preview"
            assert preview["count"] == 2
            applied = await hooks._prune_dangling_stop_subscriptions(db, confirm=True)
            assert applied["action"] == "pruned"
            assert applied["count"] == 2
            assert applied["active_remaining"] == 1

    # dry-run leaves all 3 active; confirm leaves only the live one
    asyncio.run(run())
    assert len(_active_subscription_ids(app_env.db_path)) == 1


def test_prune_endpoint_dry_run_default(app_env):
    from fastapi.testclient import TestClient

    _insert_instance(app_env.db_path, "live-c", pane="%10")
    _insert_subscription(
        app_env.db_path,
        target_instance_id="dead-x",
        target_pane="%99",
        subscriber_instance_id="live-c",
        subscriber_pane="%10",
    )
    client = TestClient(app_env.main.app)

    resp = client.post("/api/hooks/prune", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["action"] == "prune_preview"
    assert data["confirmed"] is False
    assert data["count"] == 1
    # nothing removed on dry-run
    assert len(_active_subscription_ids(app_env.db_path)) == 1


def test_planning_state_endpoint_cycles_and_projects_pane_var(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("TOKEN_API_TEST_ALLOW_STAMPED_PANE_FALLBACK", "1")
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
