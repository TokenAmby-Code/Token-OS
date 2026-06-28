"""Custodes-sender pause-queue bypass.

Decree (Emperor, 2026-06-25): Custodes-originated TTS bypasses the pause queue
innately and plays immediately. The bypass is a property of the SENDER
(Custodes), not the message type — enforcement TTS only ever originates from
Custodes, so sender-based bypass subsumes "enforcement bypass" entirely. The TTS
queue therefore has no opinion about "enforcement": it inspects the sender's
persona, nothing about the message.

So `queue_tts(<custodes-instance>, msg, queue_target="pause")` must land the item
on the HOT queue (immediate playback), while a non-Custodes sender's "pause"
request is untouched.
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


def _insert_voiced_instance(db_path: Path, *, persona_slug: str | None) -> str:
    """Insert an idle, voiced instance; optionally bind it to a seeded persona."""
    from instance_mutation import sanctioned_insert_instance_sync, sanctioned_update_instance_sync
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
    updates = {"tts_voice": "Microsoft George"}
    if persona_slug is not None:
        updates["persona_id"] = persona_id_for_slug(persona_slug)
    sanctioned_update_instance_sync(
        conn,
        instance_id=iid,
        updates=updates,
        mutation_type="instance_test_profile_update",
        write_source="test",
        actor="test",
    )
    conn.commit()
    conn.close()
    return iid


def _quiet_world(tts, monkeypatch):
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(tts, "play_sound", lambda *a, **k: {"success": True})
    monkeypatch.setattr(
        tts,
        "_resolve_queue_playback_target",
        lambda **kw: {
            "success": True,
            "playback_target": "mac",
            "routing": {"device": "mac", "reason": "test backend"},
        },
    )
    monkeypatch.setattr(tts, "_custodes_state_event_handler", None)
    tts.pause_queue.clear()
    tts.hot_queue.clear()


def test_custodes_sender_bypasses_pause_queue(app_env, monkeypatch) -> None:
    """A Custodes-persona sender's 'pause' request is forced onto the hot queue."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    iid = _insert_voiced_instance(app_env.db_path, persona_slug="custodes")

    result = asyncio.run(tts.queue_tts(iid, "the Emperor must hear this now", queue_target="pause"))

    assert result["queued"] is True
    assert result["queue"] == "hot"
    assert len(tts.hot_queue) == 1
    assert len(tts.pause_queue) == 0


def test_non_custodes_sender_still_pauses(app_env, monkeypatch) -> None:
    """A non-Custodes voiced sender's 'pause' request is untouched — still the pause
    queue. Blood Angels carries the ``pause`` policy (respects the caller's target).
    """
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    iid = _insert_voiced_instance(app_env.db_path, persona_slug="blood-angels")

    result = asyncio.run(tts.queue_tts(iid, "routine status update", queue_target="pause"))

    assert result["queued"] is True
    assert result["queue"] == "pause"
    assert len(tts.pause_queue) == 1
    assert len(tts.hot_queue) == 0


def test_bypass_is_sender_keyed_not_message_keyed(app_env, monkeypatch) -> None:
    """The same innocuous message bypasses from Custodes but pauses from a normal
    sender — proving the bypass keys on the SENDER, never the message text."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    custodes = _insert_voiced_instance(app_env.db_path, persona_slug="custodes")
    normal = _insert_voiced_instance(app_env.db_path, persona_slug="blood-angels")

    msg = "identical text"
    cust_result = asyncio.run(tts.queue_tts(custodes, msg, queue_target="pause"))
    norm_result = asyncio.run(tts.queue_tts(normal, msg, queue_target="pause"))

    assert cust_result["queue"] == "hot"
    assert norm_result["queue"] == "pause"


def test_quiet_hours_win_over_custodes_bypass(app_env, monkeypatch) -> None:
    """Quiet-hours suppression happens before Custodes hot-queue bypass."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: True)
    iid = _insert_voiced_instance(app_env.db_path, persona_slug="custodes")

    result = asyncio.run(tts.queue_tts(iid, "hold until morning", queue_target="pause"))

    assert result == {"success": True, "queued": False, "reason": "quiet_hours"}
    assert len(tts.hot_queue) == 0
    assert len(tts.pause_queue) == 0
