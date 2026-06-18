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
        """INSERT INTO legacy_instances
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

    async def run() -> None:
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

    async def run() -> None:
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

    async def run() -> None:
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


def test_same_pane_subscribe_resolves_pane_once(app_env, monkeypatch) -> None:
    # The live preplan subscribe sends target_pane == subscriber_pane == the same
    # %id. _resolve_instance_for_pane is the expensive leg (tmux show-options + a
    # SQLite lookup); it must run ONCE for the shared pane, not twice. Force the
    # pane-stamp probe to miss so resolution is deterministic (falls to the
    # tmux_pane fallback that finds the inserted row).
    hooks = sys.modules["routes.hooks"]
    shared = sys.modules["shared"]
    _insert_instance(app_env.db_path, "selfpane-1", pane="%55")

    async def no_stamp(_pane):
        return None

    monkeypatch.setattr(shared, "instance_id_for_pane", no_stamp)

    original = hooks._resolve_instance_for_pane
    calls = {"n": 0}

    async def counting(db, pane):
        calls["n"] += 1
        return await original(db, pane)

    monkeypatch.setattr(hooks, "_resolve_instance_for_pane", counting)

    async def run() -> None:
        sub = await hooks.subscribe_hook(
            hooks.HookSubscribeRequest(target_pane="%55", subscriber_pane="%55")
        )
        assert sub["success"] is True
        assert sub["target_pane"] == "%55"
        assert sub["subscriber_pane"] == "%55"
        assert sub["target_instance_id"] == "selfpane-1"
        assert sub["subscriber_instance_id"] == "selfpane-1"

    asyncio.run(run())

    assert calls["n"] == 1  # resolved once for the shared pane, not twice


def test_distinct_pane_subscribe_resolves_each_pane(app_env, monkeypatch) -> None:
    # When the two roles are different panes, each is still resolved (the cache
    # must not collapse distinct panes to one resolution).
    hooks = sys.modules["routes.hooks"]
    shared = sys.modules["shared"]
    _insert_instance(app_env.db_path, "tgt-x", pane="%60")
    _insert_instance(app_env.db_path, "sub-x", pane="%61")

    async def no_stamp(_pane):
        return None

    monkeypatch.setattr(shared, "instance_id_for_pane", no_stamp)

    original = hooks._resolve_instance_for_pane
    seen = []

    async def counting(db, pane):
        seen.append(pane)
        return await original(db, pane)

    monkeypatch.setattr(hooks, "_resolve_instance_for_pane", counting)

    async def run() -> None:
        sub = await hooks.subscribe_hook(
            hooks.HookSubscribeRequest(target_pane="%60", subscriber_pane="%61")
        )
        assert sub["success"] is True
        assert sub["target_pane"] == "%60"
        assert sub["subscriber_pane"] == "%61"

    asyncio.run(run())

    assert sorted(seen) == ["%60", "%61"]  # both panes resolved exactly once each


def test_mechanicus_worker_session_start_auto_subscribes_to_live_fg(app_env):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path,
        "fg-1",
        pane="%40",
        pane_label="mechanicus:fabricator-general",
    )

    async def run() -> None:
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

    async def run() -> None:
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
        assert mech["created"] == 0

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    rows = conn.execute(
        "SELECT target_instance_id, subscriber_instance_id, subscriber_pane FROM stop_hook_subscriptions ORDER BY target_instance_id"
    ).fetchall()
    conn.close()
    assert rows == []


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

    async def run() -> None:
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

    async def run() -> None:
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

    async def run() -> None:
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

    async def run() -> None:
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
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "self-plan", pane="%90")
    sent = []

    async def fake_write(pane, payload):
        sent.append((pane, payload))
        return {"status": "sent", "operation": "fake"}

    monkeypatch.setattr(hooks, "_direct_pane_write", fake_write)

    async def run() -> None:
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


def test_gated_preplan_oneshot_consumes_sub_but_requeues_plan_for_redrain(app_env, monkeypatch):
    """A typing-gated Stop must not drop the /preplan -> /plan handoff.

    When the universal send gate suppresses the byte-issue at Stop time, the
    preplan_plan one-shot is still consumed (status='delivered', so it does not
    re-arm on the next Stop), but the '/plan create the plan' payload survives
    as a *pending* pane_write_queue row. The periodic drainer then flushes that
    exact payload to the pane once the gate clears — the typing guard queues the
    handoff, it never bounces it.
    """
    hooks = sys.modules["routes.hooks"]
    main = sys.modules["main"]
    _insert_instance(app_env.db_path, "self-plan", pane="%90")

    async def gated_write(pane, payload):
        return {"status": "gated", "gate_reason": "typing_guard"}

    monkeypatch.setattr(hooks, "_direct_pane_write", gated_write)

    async def arm_and_stop() -> dict:
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
        return await hooks.handle_stop({"session_id": "self-plan", "transcript_tail": ""})

    result = asyncio.run(arm_and_stop())
    # Gate suppressed the live send (no bytes issued).
    assert result["stop_subscriptions"][0]["status"] == "gated"

    conn = sqlite3.connect(app_env.db_path)
    sub_row = conn.execute(
        "SELECT status, oneshot FROM stop_hook_subscriptions WHERE target_instance_id='self-plan'"
    ).fetchone()
    queue_row = conn.execute(
        "SELECT id, status, source, purpose, payload, instance_id "
        "FROM pane_write_queue WHERE purpose='stop_subscription'"
    ).fetchone()
    conn.close()

    # One-shot is consumed so the next Stop does not re-arm it...
    assert sub_row == ("delivered", 1)
    # ...but the handoff payload is parked pending for the periodic re-drain.
    assert queue_row is not None
    queue_id, q_status, q_source, q_purpose, q_payload, q_instance = queue_row
    assert (q_status, q_source, q_purpose, q_payload) == (
        "pending",
        "hook",
        "stop_subscription",
        "/plan create the plan",
    )

    # Gate clears: the drainer flushes the SAME pending row to the pane.
    sent: list = []

    async def resolve(instance_id):
        return ("%90", None) if instance_id == q_instance else (None, None)

    async def no_pending_input(_pane):
        return False

    async def ok_send(pane, payload, *, clear_prompt=False):
        sent.append((pane, payload))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "unverified",
            "verified_by": None,
        }

    monkeypatch.setattr(main.shared, "resolve_instance_pane", resolve)
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", no_pending_input)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", ok_send)

    drained = asyncio.run(main.process_pane_write_queue_once(queue_id))

    assert sent == [("%90", "/plan create the plan")]
    assert drained[0]["status"] == main.PANE_WRITE_SENT
    conn = sqlite3.connect(app_env.db_path)
    final_status = conn.execute(
        "SELECT status FROM pane_write_queue WHERE id = ?", (queue_id,)
    ).fetchone()[0]
    conn.close()
    assert final_status == "sent"


def test_mark_for_close_stop_subscription_retires_after_closing_pane(
    app_env: object, monkeypatch: object
) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "close-me", pane="%91")
    closed = []

    async def fake_close(pane: str) -> dict:
        closed.append(pane)
        return {"status": "closed", "pane": pane}

    monkeypatch.setattr(hooks, "_close_tmux_pane_for_mark", fake_close)

    async def run() -> None:
        sub = await hooks.mark_instance_for_close(
            "close-me",
            hooks.MarkForCloseRequest(mode="after-stop", lifecycle="retire", pane="%91"),
        )
        assert sub["success"] is True
        result = await hooks.handle_stop({"session_id": "close-me", "transcript_tail": ""})
        assert result["stop_subscriptions"][0]["status"] == "closed"

    asyncio.run(run())

    assert closed == ["%91"]
    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        "SELECT status, rank, golden_throne FROM instances WHERE id='close-me'"
    ).fetchone()
    sub_row = conn.execute(
        "SELECT status, purpose, delivery, oneshot FROM stop_hook_subscriptions WHERE target_instance_id='close-me'"
    ).fetchone()
    conn.close()
    assert row == ("stopped", "retired", None)
    assert sub_row == ("delivered", "mark_for_close", "close-pane", 1)


def test_mark_for_close_stop_subscription_can_archive_session_doc(
    app_env: object, monkeypatch: object, tmp_path: Path
) -> None:
    hooks = sys.modules["routes.hooks"]
    doc = tmp_path / "close-doc.md"
    doc.write_text("---\nstatus: active\n---\n# Close Doc\n", encoding="utf-8")
    _insert_instance(app_env.db_path, "archive-me", pane="%92")

    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        "INSERT INTO session_documents (file_path, title, status) VALUES (?, 'Close Doc', 'active')",
        (str(doc),),
    )
    doc_id = conn.execute("SELECT id FROM session_documents WHERE title='Close Doc'").fetchone()[0]
    conn.commit()
    conn.close()

    async def bind_doc() -> None:
        async with hooks.aiosqlite.connect(app_env.db_path, timeout=5.0) as db:
            await hooks.sanctioned_update_instance(
                db,
                instance_id="archive-me",
                updates={"session_doc_id": doc_id},
                mutation_type="instance_updated",
                write_source="test",
                actor="test",
            )
            await db.commit()

    asyncio.run(bind_doc())

    async def fake_close(pane: str) -> dict:
        return {"status": "closed", "pane": pane}

    monkeypatch.setattr(hooks, "_close_tmux_pane_for_mark", fake_close)

    async def run() -> None:
        armed = await hooks.mark_instance_for_close(
            "archive-me",
            hooks.MarkForCloseRequest(
                mode="after-stop", lifecycle="archive-session-doc", pane="%92"
            ),
        )
        assert armed["success"] is True
        result = await hooks.handle_stop({"session_id": "archive-me", "transcript_tail": ""})
        assert result["stop_subscriptions"][0]["lifecycle"]["status"] == "archived"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute("SELECT status, rank FROM instances WHERE id='archive-me'").fetchone()
    doc_status = conn.execute(
        "SELECT status FROM session_documents WHERE id=?", (doc_id,)
    ).fetchone()[0]
    conn.close()
    assert row == ("archived", "retired")
    assert doc_status == "archived"
    assert "status: archived" in doc.read_text(encoding="utf-8")


def test_public_hook_subscribe_rejects_close_pane_delivery(app_env: object) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "public-close", pane="%93")

    async def run() -> None:
        result = await hooks.subscribe_hook(
            hooks.HookSubscribeRequest(
                target_pane="%93",
                subscriber_pane="%93",
                delivery="close-pane",
                purpose="mark_for_close",
            )
        )
        assert result == {
            "success": False,
            "action": "unsupported_delivery",
            "delivery": "close-pane",
        }

    asyncio.run(run())


def test_mark_for_close_endpoint_rejects_pane_instance_mismatch(app_env: object) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "pane-owner", pane="%96")

    async def run() -> None:
        result = await hooks.mark_instance_for_close(
            "pane-owner",
            hooks.MarkForCloseRequest(mode="after-stop", lifecycle="retire", pane="%97"),
        )
        assert result["success"] is False
        assert result["action"] == "pane_instance_mismatch"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    sub_count = conn.execute("SELECT COUNT(*) FROM stop_hook_subscriptions").fetchone()[0]
    conn.close()
    assert sub_count == 0


def test_mark_for_close_endpoint_refuses_protected_persona_pane(
    app_env: object, monkeypatch: object
) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "custodes-pane", pane="%95")

    async def fake_role(pane: str, option: str) -> str:
        assert pane == "%95"
        assert option == "@PANE_ID"
        return "legion:custodes"

    monkeypatch.setattr(hooks, "_tmux_show_pane_option", fake_role)

    async def run() -> None:
        result = await hooks.mark_instance_for_close(
            "custodes-pane",
            hooks.MarkForCloseRequest(mode="now", lifecycle="retire", pane="%95"),
        )
        assert result["success"] is False
        assert result["action"] == "protected_pane"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    sub_count = conn.execute("SELECT COUNT(*) FROM stop_hook_subscriptions").fetchone()[0]
    row = conn.execute("SELECT status, rank FROM instances WHERE id='custodes-pane'").fetchone()
    conn.close()
    assert sub_count == 0
    assert row == ("idle", "astartes")


def test_mark_for_close_now_uses_executor(app_env: object, monkeypatch: object) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "close-now", pane="%94")
    closed = []

    async def fake_close(pane: str) -> dict:
        closed.append(pane)
        return {"status": "closed", "pane": pane}

    monkeypatch.setattr(hooks, "_close_tmux_pane_for_mark", fake_close)

    async def run() -> None:
        result = await hooks.mark_instance_for_close(
            "close-now",
            hooks.MarkForCloseRequest(mode="now", lifecycle="retire", pane="%94"),
        )
        assert result["success"] is True
        assert result["action"] == "closed"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        "SELECT status, rank, golden_throne FROM instances WHERE id='close-now'"
    ).fetchone()
    sub_row = conn.execute(
        "SELECT status, delivery FROM stop_hook_subscriptions WHERE target_instance_id='close-now'"
    ).fetchone()
    conn.close()
    assert closed == ["%94"]
    assert row == ("stopped", "retired", None)
    assert sub_row == ("delivered", "close-pane")


def test_mark_for_close_archive_fails_when_session_doc_row_missing(
    app_env: object, monkeypatch: object
) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "missing-doc", pane="%99")

    async def bind_missing_doc() -> None:
        async with hooks.aiosqlite.connect(app_env.db_path, timeout=5.0) as db:
            await hooks.sanctioned_update_instance(
                db,
                instance_id="missing-doc",
                updates={"session_doc_id": 424242},
                mutation_type="instance_updated",
                write_source="test",
                actor="test",
            )
            await db.commit()

    asyncio.run(bind_missing_doc())

    async def fake_close(pane: str) -> dict:
        return {"status": "closed", "pane": pane}

    monkeypatch.setattr(hooks, "_close_tmux_pane_for_mark", fake_close)

    async def run() -> None:
        armed = await hooks.mark_instance_for_close(
            "missing-doc",
            hooks.MarkForCloseRequest(
                mode="after-stop", lifecycle="archive-session-doc", pane="%99"
            ),
        )
        assert armed["success"] is True
        result = await hooks.handle_stop({"session_id": "missing-doc", "transcript_tail": ""})
        lifecycle = result["stop_subscriptions"][0]["lifecycle"]
        assert lifecycle["status"] == "failed"
        assert lifecycle["reason"] == "session_doc_not_found"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute("SELECT status, rank FROM instances WHERE id='missing-doc'").fetchone()
    conn.close()
    assert row == ("idle", "astartes")


def test_mark_for_close_checks_resolved_protected_pane(
    app_env: object, monkeypatch: object
) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "malcador-pane", pane="%100", pane_label="legion:malcador")

    async def fake_resolve(db: object, pane: str | None) -> dict:
        assert pane == "legion:malcador"
        return {"id": "malcador-pane", "tmux_pane": "%100"}

    async def fake_role(pane: str, option: str) -> str:
        assert pane == "%100"
        assert option == "@PANE_ID"
        return "legion:malcador"

    monkeypatch.setattr(hooks, "_resolve_instance_for_pane", fake_resolve)
    monkeypatch.setattr(hooks, "_tmux_show_pane_option", fake_role)

    async def run() -> None:
        result = await hooks.mark_instance_for_close(
            "malcador-pane",
            hooks.MarkForCloseRequest(
                mode="after-stop", lifecycle="retire", pane="legion:malcador"
            ),
        )
        assert result["success"] is False
        assert result["action"] == "protected_pane"
        assert result["pane"] == "%100"
        assert result["pane_role"] == "legion:malcador"

    asyncio.run(run())


def test_mark_for_close_refuses_stored_protected_pane_label_when_pane_omitted(
    app_env: object, monkeypatch: object
) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "pax-pane", pane="%101", pane_label="koronus:pax")

    async def fake_role(pane: str, option: str) -> str:
        assert pane == "%101"
        assert option == "@PANE_ID"
        return ""

    monkeypatch.setattr(hooks, "_tmux_show_pane_option", fake_role)

    async def run() -> None:
        result = await hooks.mark_instance_for_close(
            "pax-pane",
            hooks.MarkForCloseRequest(mode="after-stop", lifecycle="retire"),
        )
        assert result["success"] is False
        assert result["action"] == "protected_pane"
        assert result["pane_role"] == "koronus:pax"

    asyncio.run(run())

    # Refusal must leave persistence untouched: no subscription armed/deactivated
    # and no lifecycle mutation on the protected pax singleton.
    conn = sqlite3.connect(app_env.db_path)
    sub_count = conn.execute(
        "SELECT COUNT(*) FROM stop_hook_subscriptions WHERE target_instance_id='pax-pane'"
    ).fetchone()[0]
    row = conn.execute("SELECT status, rank FROM instances WHERE id='pax-pane'").fetchone()
    conn.close()
    assert sub_count == 0
    assert row == ("idle", "astartes")


def test_refused_close_pane_oneshot_deactivates(app_env: object, monkeypatch: object) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "refuse-close", pane="%98")
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane, event, delivery, status, purpose, oneshot)
           VALUES ('refuse-close', '%98', 'refuse-close', '%98', 'stop', 'close-pane', 'active', 'mark_for_close', 1)"""
    )
    conn.commit()
    conn.close()

    async def fake_close(pane: str) -> dict:
        return {"status": "refused", "reason": "static_persona_pane"}

    monkeypatch.setattr(hooks, "_close_tmux_pane_for_mark", fake_close)

    async def run() -> None:
        result = await hooks.handle_stop({"session_id": "refuse-close", "transcript_tail": ""})
        assert result["stop_subscriptions"][0]["status"] == "refused"

    asyncio.run(run())

    conn = sqlite3.connect(app_env.db_path)
    sub_status = conn.execute(
        "SELECT status FROM stop_hook_subscriptions WHERE target_instance_id='refuse-close'"
    ).fetchone()[0]
    conn.close()
    assert sub_status == "delivered"


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

    async def run() -> None:
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

    async def run() -> None:
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

    async def run() -> None:
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

    async def run() -> None:
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

    async def run() -> None:
        unsub = await hooks.unsubscribe_hook(
            hooks.HookUnsubscribeRequest(target_pane="watched-2", subscriber_pane="phantom-uuid")
        )
        assert unsub["count"] == 1

    asyncio.run(run())
    assert _active_subscription_ids(app_env.db_path) == []


# ---- Prune: GC dangling watched/notify references ---------------------------


def test_prune_extra_live_ids_protects_swept_but_live(app_env):
    """``extra_live_ids`` spares a subscription whose watched row is stopped but
    whose pane is genuinely live (the tmux liveness oracle the sweep passes in)."""
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "notify", pane="%10")  # DB-live subscriber
    _insert_instance(app_env.db_path, "swept", pane=None, status="stopped")  # DB-dead
    _insert_subscription(
        app_env.db_path,
        target_instance_id="swept",
        target_pane="%21",
        subscriber_instance_id="notify",
        subscriber_pane="%10",
    )

    async def run() -> None:
        import aiosqlite

        async with aiosqlite.connect(app_env.db_path) as db:
            # Without the oracle, "swept" is dangling and would be pruned.
            bare = await hooks._prune_dangling_stop_subscriptions(db, confirm=False)
            assert bare["count"] == 1
            # With the oracle naming "swept" as live, nothing is pruned.
            guarded = await hooks._prune_dangling_stop_subscriptions(
                db, confirm=True, extra_live_ids={"swept"}
            )
            assert guarded["count"] == 0

    asyncio.run(run())
    assert len(_active_subscription_ids(app_env.db_path)) == 1


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

    async def run() -> None:
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
        "SELECT planning_state, planning_source FROM legacy_instances WHERE id='planner-1'"
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

    get_resp = client.get("/api/planning/state", params={"tmux_pane": "%91"})
    assert get_resp.status_code == 200
    get_data = get_resp.json()
    assert get_data["success"] is True
    assert get_data["planning_state"] == "preplanning"
    assert get_data["instance_id"] == "planner-1"
    assert get_data["engine"] == "codex"
