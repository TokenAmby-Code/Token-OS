"""Per-persona TTS policy — deny-by-default.

Decree (Emperor, 2026-06-28): "one route, one authority, one serialized queue".
TTS submission is gated on a RESOLVED persona's explicit ``personas.tts_policy``:

  * unresolved persona (needs-name / unregistered → NULL slug+policy) → DENIED,
    SILENT, and a registration WARNING is logged (a visible failure, never a leak);
  * ``silent`` policy → not queued;
  * ``hot`` policy → forced onto the hot queue (Custodes/enforcement);
  * ``pause`` policy → respects the caller's queue_target.

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


def _load_tts():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _insert_instance(db_path: Path, *, persona_slug: str | None, voiced: bool) -> str:
    """Insert an idle instance; optionally bind a seeded persona and a voice."""
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
    updates: dict[str, str] = {}
    if voiced:
        updates["tts_voice"] = "Microsoft George"
    if persona_slug is not None:
        updates["persona_id"] = persona_id_for_slug(persona_slug)
    if updates:
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


def test_hot_policy_forces_hot_queue(app_env, monkeypatch) -> None:
    """Custodes carries the ``hot`` policy → a 'pause' request plays immediately."""
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
