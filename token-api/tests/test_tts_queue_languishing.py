import asyncio
import importlib
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from custodes_state_policy import StateEvent, classify_trigger, evaluate_state_event


def _load_tts():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _insert_tts_instance(db_path: Path) -> str:
    from instance_mutation import sanctioned_insert_instance_sync
    from personas import persona_id_for_slug

    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    sanctioned_insert_instance_sync(
        conn,
        values={
            "id": iid,
            "session_id": str(uuid.uuid4()),
            "tab_name": f"tts-{iid[:8]}",
            "working_dir": "/tmp/test",
            "origin_type": "local",
            "device_id": "Mac-Mini",
            "status": "idle",
            "tts_mode": "verbose",
            "registered_at": now,
            "last_activity": now,
        },
        mutation_type="instance_registered",
        write_source="test",
        actor="test",
    )
    # Bind a voiced persona with the ``pause`` policy (Blood Angels). queue_tts now
    # denies submissions from instances with no resolved persona (deny-by-default),
    # so these queue-mechanics fixtures must carry an explicit voiced policy. ``pause``
    # respects the caller's queue_target, preserving the prior queue semantics.
    conn.execute(
        "UPDATE instances SET tts_voice = 'Microsoft George', persona_id = ? WHERE id = ?",
        (persona_id_for_slug("blood-angels"), iid),
    )
    conn.commit()
    conn.close()
    return iid


def test_queue_tts_languishing_emits_custodes_enforcement(app_env: Any, monkeypatch: Any) -> None:
    """Pause queue length > 5 emits a recognized Custodes state event.

    The emitter passes only observational signals (event_type/source/severity/
    payload) and deliberately does NOT self-declare event_class — the policy
    (custodes_state_policy.classify_trigger) is the sole authority that
    classifies tts_queue_languishing as enforcement.
    """
    tts = _load_tts()
    iid = _insert_tts_instance(app_env.db_path)
    calls = []

    async def fake_state_event(event_type, source, **kwargs):
        calls.append((event_type, source, kwargs))
        return {"received": True, "classification": "state"}

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)
    monkeypatch.setattr(tts, "play_sound", lambda *a, **k: {"success": True})
    tts._tts_languishing_emit_latch.clear()
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    async def drive():
        for n in range(6):
            result = await tts.queue_tts(iid, f"queued message {n}", queue_target="pause")
            assert result["queued"] is True

    asyncio.run(drive())

    assert len(tts.pause_queue) == 6
    assert len(calls) == 1
    event_type, source, kwargs = calls[0]
    assert event_type == "tts_queue_languishing"
    assert source == "tts_queue"
    # Doctrine lock: the emitter must NOT self-declare classification.
    assert "event_class" not in kwargs
    assert kwargs["severity"] == 3
    assert kwargs["payload"]["pause_queue_length"] == 6
    assert kwargs["payload"]["threshold"] == 5


def test_queue_tts_languishing_ignores_direct_hot_tts(app_env: Any, monkeypatch: Any) -> None:
    """Direct hot TTS should not trip pause-queue languishing enforcement."""
    tts = _load_tts()
    iid = _insert_tts_instance(app_env.db_path)
    calls = []

    async def fake_state_event(*args, **kwargs):
        calls.append((args, kwargs))
        return {"received": True}

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)
    tts._tts_languishing_emit_latch.clear()
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    async def drive():
        for n in range(6):
            result = await tts.queue_tts(iid, f"hot message {n}", queue_target="hot")
            assert result["queued"] is True

    asyncio.run(drive())

    assert len(tts.hot_queue) == 6
    assert calls == []


def test_queue_tts_refuses_when_no_playback_target(app_env: Any, monkeypatch: Any) -> None:
    """Do not accept queue work when routing resolves to backend:null."""
    tts = _load_tts()
    iid = _insert_tts_instance(app_env.db_path)
    logs = []

    async def fake_log_event(*args, **kwargs):
        logs.append((args, kwargs))

    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda: None)
    monkeypatch.setattr(tts, "is_satellite_tts_available", lambda: False)
    monkeypatch.setattr(tts, "is_phone_reachable", lambda: False)
    monkeypatch.setattr(tts, "_send_to_phone", None)
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: False)
    monkeypatch.setattr(tts, "log_event", fake_log_event)
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    result = asyncio.run(tts.queue_tts(iid, "unplayable", queue_target="pause"))

    assert result["success"] is False
    assert result["queued"] is False
    assert result["reason"] == "no_playback_target"
    assert result["playback_target"] is None
    assert len(tts.pause_queue) == 0
    assert len(tts.hot_queue) == 0
    assert [args[0] for args, _ in logs] == ["tts_enqueue_refused"]
    refused_details = logs[0][1]["details"]
    assert refused_details["reason"] == "no_playback_target"
    assert refused_details["routing"]["device"] is None


def test_queue_tts_accepts_phone_via_speak_when_macrodroid_reachable(
    app_env: Any, monkeypatch: Any
) -> None:
    """Decree 2026-06-28: a reachable MacroDroid is a real playback target via /speak.

    Reverses the prior #423 queue-level gate. The phone speaks LOCALLY via /speak
    (delivery proven by the playback-complete callback), so a down audio-proxy
    *receiver* must NOT make queue_tts refuse — the phone is a valid backend and the
    line is queued. The audio-proxy health rides along as a diagnostic, not a gate.
    (Genuine dead-end refusal is covered by test_queue_tts_refuses_when_no_playback_target.)
    """
    tts = _load_tts()
    iid = _insert_tts_instance(app_env.db_path)

    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda: None)
    monkeypatch.setattr(tts, "is_satellite_tts_available", lambda: False)
    monkeypatch.setattr(tts, "is_phone_reachable", lambda: True)
    monkeypatch.setattr(tts, "_send_to_phone", lambda *a, **k: {"success": True})
    monkeypatch.setattr(
        tts,
        "_audio_proxy_health_checker",
        lambda: {
            "phone_connected": False,
            "receiver_running": False,
            "receiver_pid": None,
            "last_heartbeat": None,
        },
    )
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: False)
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    result = asyncio.run(tts.queue_tts(iid, "speaks via /speak", queue_target="pause"))

    assert result["success"] is True
    assert result["queued"] is True
    # The phone resolved as the playback target (device string), despite the dead
    # audio-proxy receiver — proving routing gates on /speak reachability, not the
    # #423 heartbeat.
    assert result["playback_target"] == "phone"
    assert len(tts.pause_queue) == 1


def test_tts_queue_languishing_is_internal_state_trigger() -> None:
    event = StateEvent(
        event_type="tts_queue_languishing",
        source="tts_queue",
        severity=3,
        payload={"app": "tts_queue", "pause_queue_length": 6, "threshold": 5},
    )

    intervention = evaluate_state_event(event, {})

    assert intervention is not None
    assert intervention.event_type == "tts_queue_languishing"
    assert classify_trigger("tts_queue_languishing") == "state"
    assert "internal diagnostics only" in intervention.behavioral_prompt


def test_tts_languishing_emit_reads_live_pause_queue(monkeypatch) -> None:
    """A stale queue-add position cannot fire after the live pause queue drained."""
    tts = _load_tts()
    calls = []

    async def fake_state_event(*args, **kwargs):
        calls.append((args, kwargs))
        return {"received": True}

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "TTS_LANGUISHING_THRESHOLD", 2)
    tts._tts_languishing_emit_latch.clear()
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    stale_item = tts.TTSQueueItem(
        instance_id="stale-iid",
        message="stale message",
        voice="Daniel",
        sound="none",
        tab_name="stale-tab",
        queue_target="pause",
    )

    async def drive():
        # Simulates an old add-time snapshot saying position=3 after the deque
        # has already drained. The helper must re-read the live deque and no-op.
        await tts._maybe_emit_tts_languishing_enforcement(position=3, item=stale_item)

    asyncio.run(drive())

    assert calls == []


def test_tts_languishing_emit_dedups_unchanged_stuck_head(monkeypatch) -> None:
    """Same stuck head and same depth alerts exactly once."""
    tts = _load_tts()
    calls = []
    logs = []

    async def fake_state_event(*args, **kwargs):
        calls.append((args, kwargs))
        return {"received": True}

    async def fake_log_event(*args, **kwargs):
        logs.append((args, kwargs))

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "log_event", fake_log_event)
    monkeypatch.setattr(tts, "TTS_LANGUISHING_THRESHOLD", 2)
    monkeypatch.setattr(tts, "TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS", 0)
    tts._tts_languishing_emit_latch.clear()
    tts._last_pause_queue_expiry_sweep = 0.0
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    head = tts.TTSQueueItem(
        instance_id="stuck-head",
        message="first stuck",
        voice="Daniel",
        sound="none",
        tab_name="stuck-tab",
        queue_target="pause",
    )

    async def drive():
        async with tts.tts_queue_lock:
            tts.pause_queue.extend(
                [
                    head,
                    tts.TTSQueueItem("iid-2", "second", "Daniel", "none", "tab-2"),
                    tts.TTSQueueItem("iid-3", "third", "Daniel", "none", "tab-3"),
                ]
            )
        await tts._maybe_emit_tts_languishing_enforcement(position=3, item=head)
        await tts._maybe_emit_tts_languishing_enforcement(position=3, item=head)

    asyncio.run(drive())

    assert len(calls) == 1
    assert any(args[0] == "tts_languishing_enforcement_deduped" for args, _ in logs)


def test_tts_languishing_realerts_when_depth_worsens(monkeypatch) -> None:
    """Depth escalation still re-alerts for the same stuck head."""
    tts = _load_tts()
    calls = []

    async def fake_state_event(*args, **kwargs):
        calls.append((args, kwargs))
        return {"received": True}

    async def fake_log_event(*args, **kwargs):
        return None

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "log_event", fake_log_event)
    monkeypatch.setattr(tts, "TTS_LANGUISHING_THRESHOLD", 2)
    monkeypatch.setattr(tts, "TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS", 0)
    tts._tts_languishing_emit_latch.clear()
    tts._last_pause_queue_expiry_sweep = 0.0
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    head = tts.TTSQueueItem("stuck-head", "first stuck", "Daniel", "none", "stuck-tab")

    async def drive():
        async with tts.tts_queue_lock:
            tts.pause_queue.extend(
                [
                    head,
                    tts.TTSQueueItem("iid-2", "second", "Daniel", "none", "tab-2"),
                    tts.TTSQueueItem("iid-3", "third", "Daniel", "none", "tab-3"),
                ]
            )
        await tts._maybe_emit_tts_languishing_enforcement(position=3, item=head)
        async with tts.tts_queue_lock:
            tts.pause_queue.append(tts.TTSQueueItem("iid-4", "fourth", "Daniel", "none", "tab-4"))
        await tts._maybe_emit_tts_languishing_enforcement(position=4, item=head)

    asyncio.run(drive())

    assert len(calls) == 2
    assert calls[0][1]["payload"]["pause_queue_length"] == 3
    assert calls[1][1]["payload"]["pause_queue_length"] == 4


def test_tts_languishing_failed_emit_does_not_latch(monkeypatch) -> None:
    """A failed enforcement delivery must not suppress the next retry."""
    tts = _load_tts()
    attempts = 0

    async def flaky_state_event(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient dispatch failure")
        return {"received": True}

    async def fake_log_event(*args, **kwargs):
        return None

    monkeypatch.setattr(tts, "_custodes_state_event_handler", flaky_state_event)
    monkeypatch.setattr(tts, "log_event", fake_log_event)
    monkeypatch.setattr(tts, "TTS_LANGUISHING_THRESHOLD", 2)
    monkeypatch.setattr(tts, "TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS", 0)
    tts._tts_languishing_emit_latch.clear()
    tts._last_pause_queue_expiry_sweep = 0.0
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    head = tts.TTSQueueItem("stuck-head", "first stuck", "Daniel", "none", "stuck-tab")

    async def drive():
        async with tts.tts_queue_lock:
            tts.pause_queue.extend(
                [
                    head,
                    tts.TTSQueueItem("iid-2", "second", "Daniel", "none", "tab-2"),
                    tts.TTSQueueItem("iid-3", "third", "Daniel", "none", "tab-3"),
                ]
            )
        await tts._maybe_emit_tts_languishing_enforcement(position=3, item=head)
        assert tts._tts_languishing_emit_latch == {}
        await tts._maybe_emit_tts_languishing_enforcement(position=3, item=head)

    asyncio.run(drive())

    assert attempts == 2
    assert tts._tts_languishing_emit_latch["max_depth"] == 3


def test_tts_languishing_stops_firing_when_live_queue_empty(monkeypatch) -> None:
    """A latched previous stuck episode resets and does not fire on an empty queue."""
    tts = _load_tts()
    calls = []

    async def fake_state_event(*args, **kwargs):
        calls.append((args, kwargs))
        return {"received": True}

    async def fake_log_event(*args, **kwargs):
        return None

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "log_event", fake_log_event)
    monkeypatch.setattr(tts, "TTS_LANGUISHING_THRESHOLD", 2)
    monkeypatch.setattr(tts, "TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS", 0)
    tts._tts_languishing_emit_latch.clear()
    tts._last_pause_queue_expiry_sweep = 0.0
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    item = tts.TTSQueueItem("stuck-head", "first stuck", "Daniel", "none", "stuck-tab")

    async def drive():
        async with tts.tts_queue_lock:
            tts.pause_queue.extend(
                [
                    item,
                    tts.TTSQueueItem("iid-2", "second", "Daniel", "none", "tab-2"),
                    tts.TTSQueueItem("iid-3", "third", "Daniel", "none", "tab-3"),
                ]
            )
        await tts._maybe_emit_tts_languishing_enforcement(position=3, item=item)
        async with tts.tts_queue_lock:
            tts.pause_queue.clear()
        await tts._maybe_emit_tts_languishing_enforcement(position=3, item=item)

    asyncio.run(drive())

    assert len(calls) == 1
    assert tts._tts_languishing_emit_latch == {}


def test_pause_queue_languishing_snapshot_expires_stale_held_items(monkeypatch) -> None:
    """Passive snapshot sweep drains stale held messages and logs the expiry."""
    tts = _load_tts()
    logs = []

    async def fake_log_event(*args, **kwargs):
        logs.append((args, kwargs))

    monkeypatch.setattr(tts, "log_event", fake_log_event)
    monkeypatch.setattr(tts, "TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS", 60)
    monkeypatch.setattr(tts, "TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS", 0)
    tts._last_pause_queue_expiry_sweep = 0.0
    tts._tts_languishing_emit_latch.clear()
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    stale = tts.TTSQueueItem("old-iid", "old held speech", "Daniel", "none", "old-tab")
    stale.queued_at = datetime.now() - timedelta(seconds=120)
    fresh = tts.TTSQueueItem("fresh-iid", "fresh held speech", "Daniel", "none", "fresh-tab")

    async def drive():
        async with tts.tts_queue_lock:
            tts.pause_queue.extend([stale, fresh])
        return await tts.get_pause_queue_languishing_snapshot(threshold=1)

    snapshot = asyncio.run(drive())

    assert snapshot["pause_queue_length"] == 1
    assert snapshot["expired_count"] == 1
    assert [item.instance_id for item in tts.pause_queue] == ["fresh-iid"]
    assert any(args[0] == "tts_pause_queue_item_expired" for args, _ in logs)
    assert any(args[0] == "tts_pause_queue_expiry_sweep" for args, _ in logs)
    sweep = next(
        kwargs["details"] for args, kwargs in logs if args[0] == "tts_pause_queue_expiry_sweep"
    )
    assert "items" not in sweep
    assert sweep["per_item_events_logged"] == 1


def test_stale_pause_queue_backend_null_surfaces_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy backend:null items must surface loudly when discovered stale."""
    tts = _load_tts()
    logs = []
    state_events = []

    async def fake_log_event(*args, **kwargs):
        logs.append((args, kwargs))

    async def fake_state_event(*args, **kwargs):
        state_events.append((args, kwargs))
        return {"received": True}

    monkeypatch.setattr(tts, "log_event", fake_log_event)
    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS", 60)
    monkeypatch.setattr(tts, "TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS", 0)
    tts._last_pause_queue_expiry_sweep = 0.0
    tts._tts_languishing_emit_latch.clear()
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    stale = tts.TTSQueueItem("old-iid", "old backend null", "Daniel", "none", "old-tab")
    stale.playback_target = None
    stale.queued_at = datetime.now() - timedelta(seconds=120)

    async def drive():
        async with tts.tts_queue_lock:
            tts.pause_queue.append(stale)
        return await tts.get_pause_queue_languishing_snapshot(threshold=1)

    snapshot = asyncio.run(drive())

    assert snapshot["expired_count"] == 1
    assert any(args[0] == "tts_backend_null_queue_stale" for args, _ in logs)
    alarm_args, alarm_kwargs = state_events[0]
    assert alarm_args == ("tts_backend_null_queue_stale", "tts_queue")
    assert alarm_kwargs["severity"] == 4
    assert alarm_kwargs["payload"]["backend_null_count"] == 1


def test_languishing_alert_stop_tts_does_not_feed_pause_queue(
    app_env: Any, monkeypatch: Any
) -> None:
    """The autonomous alert response is logged but excluded from the alerted queue."""
    hooks = sys.modules["routes.hooks"]
    tts = _load_tts()
    iid = _insert_tts_instance(app_env.db_path)
    logs = []

    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute("UPDATE instances SET hook_driven = 1 WHERE id = ?", (iid,))
        conn.commit()

    async def fake_log_event(*args, **kwargs):
        logs.append((args, kwargs))

    async def fail_queue_tts(*args, **kwargs):  # pragma: no cover - assertion path
        raise AssertionError("languishing alert TTS must not enqueue into pause_queue")

    monkeypatch.setattr(hooks, "log_event", fake_log_event)
    monkeypatch.setattr(hooks, "queue_tts", fail_queue_tts)
    monkeypatch.setattr(hooks, "_is_dev_worktree_dir", lambda *a, **k: False)

    async def fake_stop_subscriptions(*args, **kwargs):
        return []

    async def fake_child_fanout(*args, **kwargs):
        return None

    monkeypatch.setattr(hooks, "_fanout_stop_subscriptions", fake_stop_subscriptions)
    monkeypatch.setattr(hooks, "_enqueue_child_stop_fanout", fake_child_fanout)
    monkeypatch.setattr(hooks, "play_sound", lambda *a, **k: {"success": True})
    tts.pause_queue.clear()
    tts.hot_queue.clear()
    hooks.shared.note_hook_driven_actor(iid, "state-hook-fanout:tts_queue_languishing")

    transcript_tail = json_line = (
        '{"message":{"role":"assistant","content":"Handled the TTS pause queue languishing alert."}}'
    )
    assert json_line

    async def drive():
        return await hooks.handle_stop({"session_id": iid, "transcript_tail": transcript_tail})

    result = asyncio.run(drive())

    assert result["tts"]["queued"] is False
    assert result["tts"]["reason"] == "languishing_alert_tts_excluded_from_pause_queue"
    assert len(tts.pause_queue) == 0
    assert any(args[0] == "tts_languishing_alert_tts_bypassed" for args, _ in logs)


def test_tts_languishing_state_event_rechecks_live_queue_before_routing(
    app_env, monkeypatch
) -> None:
    """An immutable old payload with length=3 is stale if live pause_queue is empty."""
    main = app_env.main
    tts = _load_tts()
    tts.pause_queue.clear()
    tts.hot_queue.clear()
    monkeypatch.setattr(tts, "TTS_LANGUISHING_THRESHOLD", 2)
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)

    async def fail_dispatch(*args, **kwargs):  # pragma: no cover - assertion path
        raise AssertionError("stale tts_queue_languishing must not dispatch")

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fail_dispatch)
    monkeypatch.setattr(main, "_dispatch_administratum_record", fail_dispatch)

    stale_payload = {
        "app": "tts_queue",
        "queue": "pause",
        "pause_queue_length": 3,
        "threshold": 2,
        "latest_instance_id": "stale-iid",
        "latest_tab_name": "stale-tab",
    }

    async def drive():
        # Queue then drain before the state-event router evaluates the old payload.
        async with tts.tts_queue_lock:
            for n in range(3):
                tts.pause_queue.append(
                    tts.TTSQueueItem(
                        instance_id=f"stale-{n}",
                        message=f"queued {n}",
                        voice="Daniel",
                        sound="none",
                        tab_name=f"tab-{n}",
                        queue_target="pause",
                    )
                )
            tts.pause_queue.clear()

        return await main.handle_custodes_state_event(
            "tts_queue_languishing",
            "tts_queue",
            severity=3,
            payload=stale_payload,
            event_class="enforcement",
        )

    result = asyncio.run(drive())

    assert result["intervention_dispatched"] is False
    assert result["classification"] == "stale"
    assert result["reason"] == "pause_queue_empty"
    assert result["live_tts_queue"]["pause_queue_length"] == 0


def test_tts_languishing_state_event_drops_near_empty_live_queue(
    app_env: Any, monkeypatch: Any
) -> None:
    """A stale languishing payload must not page when the live pause queue has one item."""
    main = app_env.main
    tts = _load_tts()
    tts.pause_queue.clear()
    tts.hot_queue.clear()
    monkeypatch.setattr(tts, "TTS_LANGUISHING_THRESHOLD", 2)
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)

    async def fail_dispatch(*args, **kwargs):  # pragma: no cover - assertion path
        raise AssertionError("near-empty tts_queue_languishing must not dispatch/page")

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fail_dispatch)
    monkeypatch.setattr(main, "_dispatch_administratum_record", fail_dispatch)

    stale_payload = {
        "app": "tts_queue",
        "queue": "pause",
        "pause_queue_length": 6,
        "threshold": 2,
        "latest_instance_id": "stale-iid",
        "latest_tab_name": "stale-tab",
    }

    async def drive():
        async with tts.tts_queue_lock:
            tts.pause_queue.clear()
            tts.pause_queue.append(
                tts.TTSQueueItem(
                    instance_id="only-live-item",
                    message="still queued",
                    voice="Daniel",
                    sound="none",
                    tab_name="only-tab",
                    queue_target="pause",
                )
            )

        return await main.handle_custodes_state_event(
            "tts_queue_languishing",
            "tts_queue",
            severity=3,
            payload=stale_payload,
            event_class="enforcement",
        )

    try:
        result = asyncio.run(drive())
    finally:

        async def cleanup():
            async with tts.tts_queue_lock:
                tts.pause_queue.clear()

        asyncio.run(cleanup())

    assert result["intervention_dispatched"] is False
    assert result["classification"] == "stale"
    assert result["reason"] == "pause_queue_below_languishing_threshold"
    assert result["live_tts_queue"]["pause_queue_length"] == 1
