from __future__ import annotations

import asyncio
import hashlib
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
    tts.TTS_AUTHORITATIVE_STATE["control"] = {"state": "idle", "source": None, "updated_at": None}
    tts.TTS_AUTHORITATIVE_STATE["current"] = None
    tts.TTS_AUTHORITATIVE_STATE["playback_id"] = None
    sent = []

    def fake_send(endpoint, params):
        sent.append((endpoint, dict(params)))
        waiter = tts.pending_phone_playbacks.get(str(params["playback_id"]))
        assert waiter is not None
        waiter.set()
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send)
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.01)
    monkeypatch.setattr(tts, "TTS_CHUNK_MAX_CHARS", 20)
    chunks = tts.build_tts_chunk_handoff("first sentence. second sentence.", max_chars=20)

    result = tts.dispatch_tts_chunks_to_backend("phone", chunks, rate=2)

    assert result["success"] is True
    assert len(sent) == 1
    first = sent[0][1]
    assert first["current_chunk_text"] == "first sentence. second sentence."
    assert first["next_chunk_text"] == ""
    assert first["current_index"] == 0
    assert first["next_index"] is None
    assert first["playback_id"]
    assert "next_next" not in first
    assert tts.TTS_AUTHORITATIVE_STATE["next"] is None
    assert result["chunks"] == 1
    assert result["completed_chunks"] == 1

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
                playback_id=first["playback_id"],
                chunk_id=first["chunk_id"],
                backend="phone",
                error="macro failed",
            )
        )
    )
    assert error["success"] is True
    assert error["state"]["last_error"]["error"] == "macro failed"
    assert error["state"]["control"]["state"] == "error"


def test_buffer_drained_chunk_event_completes_pending_phone_playback() -> None:
    tts = _load_tts()
    waiter = tts.threading.Event()
    tts.pending_phone_playbacks["phone-play-1"] = waiter
    try:
        result = asyncio.run(
            tts.api_tts_chunk_event(
                tts.TTSChunkEventRequest(
                    event="buffer_drained",
                    backend="phone",
                    playback_id="phone-play-1",
                    session_id="sess-1",
                    current_index=0,
                    next_index=1,
                )
            )
        )
        assert result["success"] is True
        assert result["matched_playback"] is True
        assert waiter.is_set()
        assert result["state"]["last_backend_ack"]["matched_playback"] is True
    finally:
        tts.pending_phone_playbacks.pop("phone-play-1", None)


def test_phone_short_utterance_sends_empty_next_chunk() -> None:
    tts = _load_tts()
    chunks = tts.build_tts_chunk_handoff("short line")

    phone_chunks = tts.build_phone_tts_chunk_handoff(chunks)
    payload = tts._backend_chunk_payload(phone_chunks[0], None)

    assert payload["current_index"] == 0
    assert payload["next_index"] is None
    assert payload["current_chunk"] == "short line"
    assert payload["next_chunk"] == ""
    assert payload["playback_id"] == phone_chunks[0]["playback_id"]


def test_wsl_chunk_dispatch_uses_satellite_speak_text_file_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    chunks = tts.build_tts_chunk_handoff("WSL chunk dispatch integrity line")
    observed = {}

    def fake_speak_tts_wsl(message: str, voice: str, rate: int = 0, **_kwargs):
        observed["message"] = message
        observed["voice"] = voice
        observed["rate"] = rate
        return {
            "success": True,
            "method": "wsl_sapi",
            "transport": "wsl_sapi_text_file",
            "rendered_hash": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            "rendered_chars": len(message),
        }

    monkeypatch.setattr(tts, "speak_tts_wsl", fake_speak_tts_wsl)

    result = tts.dispatch_tts_chunks_to_backend("wsl", chunks, voice="Microsoft David", rate=1)

    assert result["success"] is True
    assert result["backend"] == "wsl"
    assert result["completed_chunks"] == 1
    assert result["method"] == "wsl_sapi"
    assert observed == {
        "message": "WSL chunk dispatch integrity line",
        "voice": "Microsoft David",
        "rate": 1,
    }
    assert result["results"][0]["chunk_id"] == chunks[0]["chunk_id"]
    assert result["results"][0]["playback_id"] == chunks[0]["playback_id"]
    assert result["results"][0]["transport"] == "wsl_sapi_text_file"


def test_wsl_chunk_dispatch_stops_on_text_integrity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    chunks = tts.build_tts_chunk_handoff("first sentence. second sentence.", max_chars=20)
    calls = []

    def fake_speak_tts_wsl(message: str, voice: str, rate: int = 0, **_kwargs):
        calls.append(message)
        return {
            "success": True,
            "method": "wsl_sapi",
            "transport": "wsl_sapi_text_file",
            "rendered_hash": "0" * 64,
            "rendered_chars": len(message),
        }

    monkeypatch.setattr(tts, "speak_tts_wsl", fake_speak_tts_wsl)

    result = tts.dispatch_tts_chunks_to_backend("wsl", chunks, voice="Microsoft David", rate=1)

    assert result["success"] is False
    assert result["error"] == "satellite_text_integrity_check_failed"
    assert result["completed_chunks"] == 0
    assert calls == ["first sentence."]
    assert len(result["results"]) == 1
    assert result["results"][0]["chunk_id"] == chunks[0]["chunk_id"]


def test_phone_chunk_next_is_compatibility_done() -> None:
    tts = _load_tts()
    done = asyncio.run(
        tts.api_tts_chunk_next(
            tts.TTSChunkNextRequest(
                session_id="sess-stream",
                playback_id="playback",
                last_consumed_index=0,
            )
        )
    )
    assert done["success"] is True
    assert done["done"] is True
    assert done["reason"] == "streaming_retired"
    assert done["next_chunk"] == ""
    assert done["next_index"] is None


def test_phone_test_endpoint_bypasses_router_and_registers_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    tts.TTS_AUTHORITATIVE_STATE["control"] = {"state": "idle", "source": None, "updated_at": None}
    tts.TTS_AUTHORITATIVE_STATE["current"] = None
    tts.TTS_AUTHORITATIVE_STATE["playback_id"] = None
    sent = []

    async def noop_log(*_args, **_kwargs):
        return None

    def fail_router(**_kwargs):
        raise AssertionError("phone-test must bypass normal WSL/phone router")

    def fake_send(endpoint, params):
        sent.append((endpoint, dict(params)))
        waiter = tts.pending_phone_playbacks.get(str(params["playback_id"]))
        assert waiter is not None
        waiter.set()
        return {"success": True, "method": "phone"}

    monkeypatch.setattr(tts, "log_event", noop_log)
    monkeypatch.setattr(tts, "resolve_tts_device", fail_router)
    monkeypatch.setattr(tts, "_send_to_phone", fake_send)
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.01)

    result = asyncio.run(
        tts.api_tts_phone_test(tts.TTSPhoneTestRequest(message="one. two. three.", max_chars=10))
    )

    assert result["success"] is True
    assert result["requested_backend"] == "phone"
    assert result["router_bypassed"] is True
    assert result["playback_confirmed"] is True
    assert result["input_chunks"] == 3
    assert result["chunks"] == 1
    assert len(sent) == 1
    endpoint, payload = sent[0]
    assert endpoint == "/tts-chunk"
    assert payload["session_id"] == result["session_id"]
    assert payload["current_chunk"] == "one. two. three."
    assert payload["next_chunk"] == ""
    assert payload["current_index"] == 0
    assert payload["next_index"] is None


def test_phone_test_endpoint_refuses_to_clobber_active_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    tts.TTS_AUTHORITATIVE_STATE["control"] = {
        "state": "playing",
        "source": "queue",
        "updated_at": "2026-07-08T00:00:00",
    }
    tts.TTS_AUTHORITATIVE_STATE["current"] = {"text": "operator speech"}
    tts.TTS_AUTHORITATIVE_STATE["playback_id"] = "active-playback"

    def fail_dispatch(*_args, **_kwargs):
        raise AssertionError("phone-test must not dispatch over active TTS")

    monkeypatch.setattr(tts, "dispatch_tts_chunks_to_backend", fail_dispatch)

    with pytest.raises(tts.HTTPException) as exc:
        asyncio.run(tts.api_tts_phone_test(tts.TTSPhoneTestRequest(message="probe", max_chars=10)))

    assert exc.value.status_code == 409
    assert tts.TTS_AUTHORITATIVE_STATE["control"]["source"] == "queue"


def test_phone_controls_truthfully_report_pause_resume_unsupported_and_skip_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    tts.TTS_AUTHORITATIVE_STATE["backend"] = "phone"
    tts.TTS_AUTHORITATIVE_STATE["session_id"] = None
    tts.TTS_AUTHORITATIVE_STATE["playback_id"] = None
    sent = []

    async def noop_log(*_args, **_kwargs):
        return None

    def fake_send(endpoint, params):
        sent.append((endpoint, dict(params)))
        return {"success": True, "method": "phone"}

    monkeypatch.setattr(tts, "log_event", noop_log)
    monkeypatch.setattr(tts, "_send_to_phone", fake_send)

    paused = asyncio.run(
        tts.api_tts_control(tts.TTSControlRequest(command="pause", backend="phone"))
    )
    assert paused["success"] is False
    assert paused["backend_echo"]["error"] == "phone_pause_unsupported"

    resumed = asyncio.run(
        tts.api_tts_control(tts.TTSControlRequest(command="resume", backend="phone"))
    )
    assert resumed["success"] is False
    assert resumed["backend_echo"]["error"] == "phone_pause_unsupported"

    skipped = asyncio.run(
        tts.api_tts_control(tts.TTSControlRequest(command="stop", backend="phone"))
    )
    assert skipped["success"] is True
    assert sent == [
        ("/tts-local-control", {"command": "skip", "session_id": None, "playback_id": None})
    ]


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
