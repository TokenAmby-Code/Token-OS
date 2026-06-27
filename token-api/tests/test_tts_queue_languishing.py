import asyncio
import importlib
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

from custodes_state_policy import StateEvent, evaluate_state_event


def _load_tts():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _insert_tts_instance(db_path: Path) -> str:
    from instance_mutation import sanctioned_insert_instance_sync

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
    conn.execute("UPDATE instances SET tts_voice = 'Microsoft George' WHERE id = ?", (iid,))
    conn.commit()
    conn.close()
    return iid


def test_queue_tts_languishing_emits_custodes_enforcement(app_env, monkeypatch) -> None:
    """Pause queue length > 5 emits a recognized Custodes enforcement event."""
    tts = _load_tts()
    iid = _insert_tts_instance(app_env.db_path)
    calls = []

    async def fake_state_event(event_type, source, **kwargs):
        calls.append((event_type, source, kwargs))
        return {"received": True, "classification": "enforcement"}

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(tts, "play_sound", lambda *a, **k: {"success": True})
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
    assert kwargs["event_class"] == "enforcement"
    assert kwargs["severity"] == 3
    assert kwargs["payload"]["pause_queue_length"] == 6
    assert kwargs["payload"]["threshold"] == 5


def test_queue_tts_languishing_ignores_direct_hot_tts(app_env, monkeypatch) -> None:
    """Direct hot TTS should not trip pause-queue languishing enforcement."""
    tts = _load_tts()
    iid = _insert_tts_instance(app_env.db_path)
    calls = []

    async def fake_state_event(*args, **kwargs):
        calls.append((args, kwargs))
        return {"received": True}

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    tts.pause_queue.clear()
    tts.hot_queue.clear()

    async def drive():
        for n in range(6):
            result = await tts.queue_tts(iid, f"hot message {n}", queue_target="hot")
            assert result["queued"] is True

    asyncio.run(drive())

    assert len(tts.hot_queue) == 6
    assert calls == []


def test_tts_queue_languishing_is_enforcement_trigger() -> None:
    event = StateEvent(
        event_type="tts_queue_languishing",
        source="tts_queue",
        severity=3,
        payload={"app": "tts_queue", "pause_queue_length": 6, "threshold": 5},
    )

    intervention = evaluate_state_event(event, {})

    assert intervention is not None
    assert intervention.event_type == "tts_queue_languishing"
    assert "TTS pause queue is languishing" in intervention.behavioral_prompt


def test_tts_languishing_emit_reads_live_pause_queue(monkeypatch) -> None:
    """A stale queue-add position cannot fire after the live pause queue drained."""
    tts = _load_tts()
    calls = []

    async def fake_state_event(*args, **kwargs):
        calls.append((args, kwargs))
        return {"received": True}

    monkeypatch.setattr(tts, "_custodes_state_event_handler", fake_state_event)
    monkeypatch.setattr(tts, "TTS_LANGUISHING_THRESHOLD", 2)
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
