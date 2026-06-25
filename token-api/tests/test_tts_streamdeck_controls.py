"""Stream Deck TTS controls.

The Emperor wires physical Stream Deck buttons (web-request plugin) to drive the
TTS pause queue — the manual-play trigger the pause queue was always designed
around. This locks the two new/extended controls:

- "Play all"   → POST /api/tts/queue/play-all : drains the ENTIRE pause queue
                 into the hot queue in FIFO order, no per-item tmux focus.
- "Mute toggle"→ POST /api/tts/global-mode {"mode":"toggle"} : flips the global
                 mode verbose↔muted in one button press.

("Play next" / "Skip" / "Skip+clear" already have endpoints and tests.)
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path


def _load_tts():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _insert_voiced_instance(db_path: Path) -> str:
    """Insert an idle, voiced (non-persona) instance — a normal pause-queue sender."""
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


def _quiet_world(tts, monkeypatch):
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(tts, "play_sound", lambda *a, **k: {"success": True})
    monkeypatch.setattr(tts, "_custodes_state_event_handler", None)
    tts.pause_queue.clear()
    tts.hot_queue.clear()


class _FakeRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


def test_play_all_drains_pause_to_hot_in_order(app_env, monkeypatch):
    """Play all moves every pause item to the hot queue, preserving FIFO order,
    without setting per-item focus."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    iid = _insert_voiced_instance(app_env.db_path)

    async def drive():
        for n in range(4):
            result = await tts.queue_tts(iid, f"pause message {n}", queue_target="pause")
            assert result["queue"] == "pause"
        return await tts.play_all_from_pause()

    result = asyncio.run(drive())

    assert result == {"success": True, "promoted": 4}
    assert len(tts.pause_queue) == 0
    assert len(tts.hot_queue) == 4
    # FIFO order preserved (popleft → append).
    assert [item.message for item in tts.hot_queue] == [f"pause message {n}" for n in range(4)]
    # Bulk drain must not yank tmux focus per item.
    assert all(item.focus_on_playback is False for item in tts.hot_queue)
    assert all(item.queue_target == "hot" for item in tts.hot_queue)


def test_play_all_empty_pause_queue_is_noop(app_env, monkeypatch):
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)

    result = asyncio.run(tts.play_all_from_pause())

    assert result == {"success": True, "promoted": 0}
    assert len(tts.hot_queue) == 0


def test_global_mode_toggle_flips_verbose_and_muted(app_env, monkeypatch):
    """{"mode":"toggle"} flips verbose→muted, then muted→verbose."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    _insert_voiced_instance(app_env.db_path)

    tts.TTS_GLOBAL_MODE["mode"] = "verbose"

    first = asyncio.run(tts.set_global_tts_mode(_FakeRequest({"mode": "toggle"})))
    assert first["mode"] == "muted"
    assert tts.TTS_GLOBAL_MODE["mode"] == "muted"

    second = asyncio.run(tts.set_global_tts_mode(_FakeRequest({"mode": "toggle"})))
    assert second["mode"] == "verbose"
    assert tts.TTS_GLOBAL_MODE["mode"] == "verbose"
