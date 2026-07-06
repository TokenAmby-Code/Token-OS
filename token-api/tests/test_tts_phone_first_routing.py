"""TTS execution routing invariants after WSL restoration.

Canonical playback chain is Discord → WSL → phone. Mac is not a TTS backend.
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime
from pathlib import Path

import pytest


def _load_tts():
    token_api_dir = Path(__file__).resolve().parents[1]
    if str(token_api_dir) not in sys.path:
        sys.path.insert(0, str(token_api_dir))
    return sys.modules.get("routes.tts") or importlib.import_module("routes.tts")


def _patch_world(
    tts,
    monkeypatch,
    *,
    phone_reachable: bool,
    satellite_healthy: bool = True,
    discord_bot=None,
    location_zone=None,
):
    monkeypatch.setattr(tts, "is_phone_reachable", lambda *a, **k: phone_reachable)
    monkeypatch.setattr(
        tts,
        "_send_to_phone",
        (lambda *a, **k: {"success": True}) if phone_reachable else None,
    )
    monkeypatch.setattr(
        tts,
        "_audio_proxy_health_checker",
        lambda: {
            "phone_connected": phone_reachable,
            "receiver_running": phone_reachable,
            "receiver_pid": 1234 if phone_reachable else None,
            "last_heartbeat": datetime.now().isoformat() if phone_reachable else None,
        },
    )
    monkeypatch.setattr(tts, "is_satellite_tts_available", lambda *a, **k: satellite_healthy)
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda *a, **k: discord_bot)
    monkeypatch.setitem(tts.DESKTOP_STATE, "location_zone", location_zone)


def test_wsl_first_class_at_home_when_satellite_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = _load_tts()
    _patch_world(
        tts, monkeypatch, phone_reachable=True, satellite_healthy=True, location_zone="home"
    )

    routing = tts.resolve_tts_device()

    assert routing["device"] == "wsl"


def test_wsl_preempts_phone_even_when_away_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = _load_tts()
    _patch_world(
        tts, monkeypatch, phone_reachable=True, satellite_healthy=True, location_zone="gym"
    )

    routing = tts.resolve_tts_device()

    assert routing["device"] == "wsl"


def test_mac_is_never_selected_even_when_monkeypatched_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    _patch_world(
        tts, monkeypatch, phone_reachable=False, satellite_healthy=False, location_zone="home"
    )
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)

    routing = tts.resolve_tts_device()

    assert routing["device"] is None
    assert routing["device"] != "mac"


def test_phone_fallback_uses_macrodroid_reachability_not_audio_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    monkeypatch.setitem(tts.DESKTOP_STATE, "location_zone", "gym")
    monkeypatch.setattr(tts, "is_phone_reachable", lambda *a, **k: True)
    monkeypatch.setattr(tts, "_send_to_phone", lambda *a, **k: {"success": True})
    monkeypatch.setattr(tts, "is_satellite_tts_available", lambda *a, **k: False)
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda *a, **k: None)
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

    routing = tts.resolve_tts_device()

    assert routing["device"] == "phone"
    assert routing["phone_audio_proxy"]["available"] is False


def test_discord_voice_still_preempts(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = _load_tts()
    _patch_world(tts, monkeypatch, phone_reachable=True, discord_bot="custodes-bot")
    routing = tts.resolve_tts_device()
    assert routing["device"] == "discord"
    assert routing["discord_bot"] == "custodes-bot"


def test_discord_failure_reports_error_without_backend_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = _load_tts()
    _patch_world(tts, monkeypatch, phone_reachable=True, discord_bot="custodes-bot")
    monkeypatch.setattr(
        tts,
        "speak_tts_discord",
        lambda *a, **k: {"success": False, "error": "discord_voice_not_played"},
    )
    monkeypatch.setattr(
        tts,
        "dispatch_tts_chunks_to_backend",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("backend fallback must not run")),
    )
    monkeypatch.setattr(
        tts,
        "speak_tts_mac",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Mac fallback must not run")),
    )

    result = tts.speak_tts("discord failure")

    assert result["success"] is False
    assert result["requested_device"] == "discord"
    assert result["route"] is None


def test_phone_failure_reports_error_without_mac_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    tts = _load_tts()
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "phone", "reason": "unit", "discord_bot": None},
    )
    monkeypatch.setattr(
        tts, "_send_to_phone", lambda *a, **k: {"success": False, "error": "phone_down"}
    )
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.01)
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)
    monkeypatch.setattr(
        tts,
        "speak_tts_mac",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Mac fallback must not run")),
    )

    result = tts.speak_tts("phone failure")

    assert result["success"] is False
    assert result["requested_device"] == "phone"
    assert result["route"] is None


def test_mac_backend_shim_is_removed() -> None:
    tts = _load_tts()
    result = tts.speak_tts_mac("must not say")
    assert result["success"] is False
    assert result["reason"] == "mac_tts_backend_removed"
