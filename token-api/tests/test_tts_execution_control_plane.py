from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest

TOKEN_API_DIR = Path(__file__).resolve().parents[1]


def _load_tts():
    if str(TOKEN_API_DIR) not in sys.path:
        sys.path.insert(0, str(TOKEN_API_DIR))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def test_resolve_tts_device_uses_wsl_at_home_and_never_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = _load_tts()
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda *a, **k: None)
    monkeypatch.setitem(tts.DESKTOP_STATE, "location_zone", "home")
    monkeypatch.setattr(tts, "is_phone_reachable", lambda *a, **k: True)
    monkeypatch.setattr(tts, "_send_to_phone", lambda *a, **k: {"success": True})
    monkeypatch.setattr(tts, "is_satellite_tts_available", lambda *a, **k: True)
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)

    routing = tts.resolve_tts_device()

    assert routing["device"] == "wsl"
    assert routing["device"] != "mac"


def test_speak_tts_phone_failure_reports_error_without_mac_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "phone", "reason": "unit", "discord_bot": None},
    )
    monkeypatch.setattr(
        tts, "_send_to_phone", lambda *a, **k: {"success": False, "error": "phone_down"}
    )
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)
    monkeypatch.setattr(
        tts,
        "speak_tts_mac",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Mac fallback must not run")),
    )

    result = tts.speak_tts("line")

    assert result["success"] is False
    assert result["requested_device"] == "phone"
    assert result["route"] is None
    assert result["reason"] in {"phone_down", "phone_backend_error"}


def test_tts_control_records_state_before_backend_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = _load_tts()
    observed = {}

    def fake_echo(backend, payload):
        observed["state_before_echo"] = tts.get_tts_authoritative_state()
        observed["backend"] = backend
        return {"success": True, "backend": backend, "echoed": payload["action"]}

    monkeypatch.setattr(tts, "_echo_tts_control_to_backend", fake_echo)
    tts._record_tts_backend_active("phone", playback_id="play-1")

    result = asyncio.run(
        tts.api_tts_control(
            tts.TTSControlRequest(
                command="pause",
                source="phone_overlay",
                backend="phone",
                session_id="sess-1",
                playback_id="play-1",
            )
        )
    )

    assert result["success"] is True
    assert observed["backend"] == "phone"
    assert observed["state_before_echo"]["control"]["state"] == "paused"
    assert observed["state_before_echo"]["control"]["last_action"] == "pause"
    assert result["state"]["control"]["state"] == "paused"


def test_tts_control_backend_echo_error_is_returned_coherently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    monkeypatch.setattr(
        tts,
        "_echo_tts_control_to_backend",
        lambda backend, payload: {"success": False, "backend": backend, "error": "backend offline"},
    )
    tts._record_tts_backend_active("wsl", playback_id="play-2")

    result = asyncio.run(tts.api_tts_control(tts.TTSControlRequest(action="resume")))

    assert result["success"] is False
    assert result["backend_echo"]["backend"] == "wsl"
    assert result["backend_echo"]["error"] == "backend offline"
    assert result["state"]["last_error"]["error"] == "backend offline"


def test_chunk_dispatch_payload_has_current_next_handoff_and_ack_error_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    sent = []

    def fake_send(endpoint, params):
        sent.append((endpoint, dict(params)))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send)
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.01)
    chunks = tts.build_tts_chunk_handoff("first sentence. second sentence.", max_chars=20)

    result = tts.dispatch_tts_chunks_to_backend("phone", chunks, rate=2)

    assert result["success"] is True
    assert len(sent) == 2
    first = sent[0][1]
    second = sent[1][1]
    assert first["current_chunk_text"] == "first sentence."
    assert first["next_chunk_text"] == "second sentence."
    assert first["next_chunk_id"] == second["chunk_id"]
    assert "next_next" not in first

    ack = asyncio.run(
        tts.api_tts_backend_ack(
            tts.TTSBackendAckRequest(
                playback_id=first["playback_id"],
                chunk_id=first["chunk_id"],
                backend="phone",
                status="played",
            )
        )
    )
    assert ack["success"] is True
    assert ack["state"]["last_backend_ack"]["chunk_id"] == first["chunk_id"]

    event = asyncio.run(
        tts.api_tts_chunk_event(
            tts.TTSChunkEventRequest(
                event="current_complete_next_starting",
                session_id=first["session_id"],
                playback_id=first["playback_id"],
                chunk_id=first["chunk_id"],
                backend="phone",
                current_index=first["current_index"],
                next_index=first["next_index"],
            )
        )
    )
    assert event["success"] is True
    assert event["state"]["last_backend_ack"]["event"] == "current_complete_next_starting"

    error = asyncio.run(
        tts.api_tts_backend_error(
            tts.TTSBackendErrorRequest(
                playback_id=second["playback_id"],
                chunk_id=second["chunk_id"],
                backend="phone",
                error="macro failed",
            )
        )
    )
    assert error["success"] is True
    assert error["state"]["last_error"]["error"] == "macro failed"
    assert error["state"]["control"]["state"] == "error"


def test_speak_tts_sanitizes_then_chunks_then_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = _load_tts()
    seen = {}
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "wsl", "reason": "unit", "discord_bot": None},
    )

    def fake_dispatch(backend, chunks, **kwargs):
        seen["backend"] = backend
        seen["chunks"] = chunks
        return {"success": True, "method": backend, "chunks": len(chunks)}

    monkeypatch.setattr(tts, "dispatch_tts_chunks_to_backend", fake_dispatch)

    result = tts.speak_tts("Review /Volumes/Imperium/Mars/Bugs/tmux-foo_bar.md after 566b697")

    assert result["success"] is True
    assert seen["backend"] == "wsl"
    assert "tmux foo bar" in seen["chunks"][0]["text"]
    assert "566b697" not in seen["chunks"][0]["text"]
    assert "commit" in seen["chunks"][0]["text"]
