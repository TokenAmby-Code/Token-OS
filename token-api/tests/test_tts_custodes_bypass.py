"""Advisor sender pause-queue bypass.

Decree (Emperor, 2026-06-25): Custodes-originated TTS bypasses the pause queue
innately and plays immediately. The bypass is a property of the SENDER
(Custodes), not the message type — enforcement TTS only ever originates from
Custodes, so sender-based bypass subsumes "enforcement bypass" entirely. The TTS
queue therefore has no opinion about "enforcement": it inspects the sender's
persona, nothing about the message.

So `queue_tts(<custodes-instance>, msg, queue_target="pause")` must land the item
on the HOT queue (immediate playback), while a non-Custodes sender's "pause"
request is untouched.

Emperor directive (2026-07-03): the capability is generic advisor parity, not a
Custodes special case. The advisor set is Custodes, Pax, and Malcador; the
bypass keys on personas.advisor via an instance->persona JOIN and does not
change each persona's voice/routing.
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
    from instance_mutation import insert_instance_sync, update_instance_sync
    from personas import persona_id_for_slug

    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    insert_instance_sync(
        conn,
        values={
            "id": iid,
            "working_dir": "/tmp/test",
            "origin_type": "local",
            "device_id": "Mac-Mini",
            "status": "idle",
            "last_activity": now,
        },
        mutation_type="instance_registered",
        write_source="test",
        actor="test",
    )
    if persona_slug is not None:
        update_instance_sync(
            conn,
            instance_id=iid,
            updates={"persona_id": persona_id_for_slug(persona_slug)},
            mutation_type="instance_test_profile_update",
            write_source="test",
            actor="test",
        )
    conn.commit()
    conn.close()
    return iid


def _set_persona_voice(
    db_path: Path,
    *,
    persona_slug: str,
    tts_voice: str = "Microsoft Zira",
    notification_sound: str = "notify.wav",
    tts_policy: str | None = None,
) -> None:
    """Give a seeded persona a test voice without changing its queue policy."""
    conn = sqlite3.connect(db_path)
    if tts_policy is None:
        conn.execute(
            "UPDATE personas SET tts_voice = ?, notification_sound = ? WHERE slug = ?",
            (tts_voice, notification_sound, persona_slug),
        )
    else:
        conn.execute(
            """
            UPDATE personas
            SET tts_voice = ?, notification_sound = ?, tts_policy = ?
            WHERE slug = ?
            """,
            (tts_voice, notification_sound, tts_policy, persona_slug),
        )
    conn.commit()
    conn.close()


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


def test_custodes_advisor_sender_bypasses_pause_queue(app_env, monkeypatch) -> None:
    """A Custodes advisor sender's 'pause' request is forced onto the hot queue."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    iid = _insert_voiced_instance(app_env.db_path, persona_slug="custodes")

    result = asyncio.run(tts.queue_tts(iid, "the Emperor must hear this now", queue_target="pause"))

    assert result["queued"] is True
    assert result["queue"] == "hot"
    assert len(tts.hot_queue) == 1
    assert len(tts.pause_queue) == 0


def test_pax_advisor_sender_bypasses_pause_queue_with_own_voice(app_env, monkeypatch) -> None:
    """A council:pax sender's pause request is forced hot, preserving Pax voice."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    # Set Pax to a non-hot speech policy so this proves advisor, not tts_policy,
    # owns the queue bypass.
    _set_persona_voice(
        app_env.db_path,
        persona_slug="pax",
        tts_voice="Microsoft Zira",
        tts_policy="pause",
    )
    iid = _insert_voiced_instance(app_env.db_path, persona_slug="pax")

    result = asyncio.run(tts.queue_tts(iid, "civic update now", queue_target="pause"))

    assert result["queued"] is True
    assert result["queue"] == "hot"
    assert len(tts.hot_queue) == 1
    assert len(tts.pause_queue) == 0
    assert tts.hot_queue[0].voice == "Microsoft Zira"


def test_malcador_advisor_sender_bypasses_pause_queue_when_voiced(app_env, monkeypatch) -> None:
    """Malcador is an advisor too; if given a voice, it bypasses generically."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    _set_persona_voice(
        app_env.db_path,
        persona_slug="malcador",
        tts_voice="Microsoft David",
        tts_policy="pause",
    )
    iid = _insert_voiced_instance(app_env.db_path, persona_slug="malcador")

    result = asyncio.run(tts.queue_tts(iid, "advisory update now", queue_target="pause"))

    assert result["queued"] is True
    assert result["queue"] == "hot"
    assert len(tts.hot_queue) == 1
    assert len(tts.pause_queue) == 0
    assert tts.hot_queue[0].voice == "Microsoft David"


def test_non_custodes_sender_still_pauses(app_env, monkeypatch) -> None:
    """A normal non-advisor sender's 'pause' request is untouched — still the pause
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


def test_advisor_bypass_is_sender_keyed_not_message_keyed(app_env, monkeypatch) -> None:
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


def test_queue_status_includes_sender_persona_metadata(app_env, monkeypatch) -> None:
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    iid = _insert_voiced_instance(app_env.db_path, persona_slug="blood-angels")

    result = asyncio.run(tts.queue_tts(iid, "metadata line", queue_target="pause"))

    assert result["queued"] is True
    status = tts.get_tts_queue_status()
    item = status["pause_queue"][0]
    assert item["instance_id"] == iid
    assert item["persona_slug"] == "blood-angels"
    assert item["persona_display_name"]
    assert item["commander_type"]
    assert item["playback_target"] == "mac"
