"""Per-persona TTS policy — deny-by-default.

Decree (Emperor, 2026-06-28): "one route, one authority, one serialized queue".
TTS submission is gated on a RESOLVED persona's explicit ``personas.tts_policy``:

  * unresolved persona (needs-name / unregistered → NULL slug+policy) → DENIED,
    SILENT, and a registration WARNING is logged (a visible failure, never a leak);
  * ``silent`` policy → not queued;
  * ``hot``/``pause`` policy → may speak; advisor capability, not policy, forces
    the hot queue;

This is the structural fix for the needs-name Fabricator-General that leaked to
audio: silence is no longer inferred only from ``tts_voice IS NULL`` (which only
protects when the persona resolves), it is denied by default.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest


def _load_tts():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _insert_instance(db_path: Path, *, persona_slug: str | None, voiced: bool) -> str:
    """Insert an idle instance; optionally bind a seeded persona and a voice."""
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
    updates: dict[str, int] = {}
    # Voice/sound are persona attributes now; ``voiced`` is kept for call-site
    # readability but never writes an instance column.
    if persona_slug is not None:
        updates["persona_id"] = persona_id_for_slug(persona_slug)
    if updates:
        update_instance_sync(
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
    monkeypatch.setattr(
        tts,
        "render_openai_tts_artifact",
        lambda text, voice: {
            "success": True,
            "artifact_id": "a" * 32,
            "artifact_url": "http://localhost:7777/api/tts/artifacts/" + "a" * 32,
            "artifact_path": "/tmp/test.wav",
            "sha256": "b" * 64,
            "voice_id": voice,
            "text_hash": "c" * 64,
            "format": "wav",
        },
    )
    tts.pause_queue.clear()
    tts.hot_queue.clear()


def test_unresolved_persona_is_denied_and_warned(app_env, monkeypatch, caplog) -> None:
    """A voiced instance with NO resolved persona is denied (deny-by-default) and a
    registration warning is logged — the needs-name Fabricator-General leak case."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    iid = _insert_instance(app_env.db_path, persona_slug=None, voiced=True)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(tts.queue_tts(iid, "I am the Fabricator-General", queue_target="hot"))

    assert result == {"success": True, "queued": False, "reason": "persona_unresolved"}
    assert len(tts.hot_queue) == 0
    assert len(tts.pause_queue) == 0
    assert any("persona_unresolved" in rec.getMessage() for rec in caplog.records)


def test_silent_policy_persona_is_denied(app_env, monkeypatch) -> None:
    """The Fabricator-General persona resolves with the ``silent`` policy → not queued.
    FG and its mechanicus workers must never speak, even when bound to a persona."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    iid = _insert_instance(app_env.db_path, persona_slug="fabricator-general", voiced=False)

    result = asyncio.run(tts.queue_tts(iid, "build report", queue_target="hot"))

    assert result == {"success": True, "queued": False, "reason": "persona_silent"}
    assert len(tts.hot_queue) == 0
    assert len(tts.pause_queue) == 0


def test_advisor_capability_forces_hot_queue(app_env, monkeypatch) -> None:
    """Custodes carries advisor=True → a 'pause' request plays immediately."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)
    iid = _insert_instance(app_env.db_path, persona_slug="custodes", voiced=True)

    result = asyncio.run(tts.queue_tts(iid, "the Emperor must hear this", queue_target="pause"))

    assert result["queued"] is True
    assert result["queue"] == "hot"
    assert len(tts.hot_queue) == 1


def test_pause_policy_respects_caller_target(app_env, monkeypatch) -> None:
    """Blood Angels carries the ``pause`` policy → the caller's queue_target is honored
    in both directions (hot stays hot, pause stays pause)."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)

    hot_iid = _insert_instance(app_env.db_path, persona_slug="blood-angels", voiced=True)
    hot_result = asyncio.run(tts.queue_tts(hot_iid, "for Sanguinius", queue_target="hot"))
    assert hot_result["queue"] == "hot"

    _quiet_world(tts, monkeypatch)  # clear queues
    pause_iid = _insert_instance(app_env.db_path, persona_slug="blood-angels", voiced=True)
    pause_result = asyncio.run(tts.queue_tts(pause_iid, "for Sanguinius", queue_target="pause"))
    assert pause_result["queue"] == "pause"


def test_system_instance_enqueues_hot_custodes_voiced(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synthetic ``system`` sender short-circuits the DB lookup to a fixed,
    always-resolved profile: Custodes-voiced (ballad), advisor-hot. It
    enqueues to the hot queue WITHOUT any instance row — instance-less system pings
    SPEAK through the single gate, never go silent and never need a registration."""
    tts = _load_tts()
    _quiet_world(tts, monkeypatch)

    # A 'pause' request: advisor=True on the synthetic row must force it hot.
    result = asyncio.run(
        tts.queue_tts(tts.SYSTEM_INSTANCE_ID, "distraction logged", queue_target="pause")
    )

    assert result["queued"] is True
    assert result["queue"] == "hot"
    assert result["voice"] == "ballad"
    assert len(tts.hot_queue) == 1
    assert len(tts.pause_queue) == 0
    item = tts.hot_queue[0]
    assert item.instance_id == "system"
    assert item.voice == "ballad"


def test_wpm_for_rate_base_and_clamp() -> None:
    """The single TTS_RATE_BASE_WPM tunable drives rate 0 and clamps to 80..300."""
    tts = _load_tts()
    assert tts.TTS_RATE_BASE_WPM == 210
    assert tts._wpm_for_rate(0) == 210
    # SAPI scale biases around the base; extremes clamp to the audible band.
    assert tts._wpm_for_rate(-100) == 80
    assert tts._wpm_for_rate(100) == 300


def test_cockpit_status_never_null_while_item_playing() -> None:
    """``get_tts_queue_status`` snapshots ``tts_current`` once, so a concurrent
    worker clear can't blank ``current`` mid-build; ``started_at`` surfaces for the
    cockpit so a playing line never flashes 'idle' mid-utterance."""
    tts = _load_tts()
    item = tts.TTSQueueItem(
        instance_id="custodes",
        message="the Emperor must hear this",
        voice="ballad",
        sound="chimes.wav",
        name="Custodes",
        started_at="2026-06-28T10:00:00",
    )
    prev = tts.tts_current
    tts.tts_current = item
    try:
        status = tts.get_tts_queue_status()
        assert status["current"] is not None
        assert status["current"]["instance_id"] == "custodes"
        assert status["current"]["started_at"] == "2026-06-28T10:00:00"
    finally:
        tts.tts_current = prev
