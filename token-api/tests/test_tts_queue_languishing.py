import asyncio
import importlib
import sqlite3
import sys
import uuid
from pathlib import Path

from custodes_state_policy import StateEvent, evaluate_state_event


def _load_tts():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _insert_tts_instance(db_path: Path) -> str:
    iid = str(uuid.uuid4())
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, tts_mode, registered_at, last_activity)
           VALUES (?, ?, ?, '/tmp/test', 'local', 'Mac-Mini', 'idle',
                   'verbose', datetime('now'), datetime('now'))""",
        (iid, str(uuid.uuid4()), f"tts-{iid[:8]}"),
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
