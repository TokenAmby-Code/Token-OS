"""Phone-first TTS routing invariants.

Decree (Emperor, 2026-06-25): the phone is first-contact for ALL TTS. Mac `say`
(local speakers) is a deep fallback only — reached when phone delivery is
unreachable/fails. This supersedes the old "geofence(away) → WSL satellite →
phone → Mac" ordering (the WSL-satellite era is over; geofence no longer gates
device selection).

`resolve_tts_device` must therefore:
  * NEVER select the WSL satellite, even when it probes healthy;
  * select the phone whenever its MacroDroid HTTP server is reachable, regardless
    of geofence zone (Emperor decree 2026-06-28). The `/speak` transport speaks
    locally on the device and proves delivery via the playback-complete callback,
    so routing no longer gates on the audio-proxy *receiver* heartbeat (#423) —
    that subsystem governs a different transport and is now a surfaced diagnostic
    only, not a routing gate;
  * fall back to Mac only when the phone is unreachable;
  * still pre-empt to Discord voice when the operator is in a voice channel.
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
        (
            lambda: {
                "phone_connected": True,
                "receiver_running": True,
                "receiver_pid": 1234,
                "last_heartbeat": datetime.now().isoformat(),
            }
        )
        if phone_reachable
        else (
            lambda: {
                "phone_connected": False,
                "receiver_running": False,
                "receiver_pid": None,
                "last_heartbeat": None,
            }
        ),
    )
    monkeypatch.setattr(tts, "is_satellite_tts_available", lambda *a, **k: satellite_healthy)
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda *a, **k: discord_bot)
    monkeypatch.setitem(tts.DESKTOP_STATE, "location_zone", location_zone)


def test_phone_first_when_home_and_satellite_healthy(monkeypatch) -> None:
    """At home WITH a healthy WSL satellite, the phone still wins — the satellite
    is never selected. (This is the core inversion vs. the old WSL-first cascade.)"""
    tts = _load_tts()
    _patch_world(
        tts, monkeypatch, phone_reachable=True, satellite_healthy=True, location_zone="home"
    )
    routing = tts.resolve_tts_device()
    assert routing["device"] == "phone"


def test_wsl_satellite_never_selected(monkeypatch) -> None:
    """No geofence/reachability combination may route to the WSL satellite."""
    tts = _load_tts()
    for zone in (None, "home", "gym", "campus"):
        for reachable in (True, False):
            _patch_world(
                tts,
                monkeypatch,
                phone_reachable=reachable,
                satellite_healthy=True,
                location_zone=zone,
            )
            routing = tts.resolve_tts_device()
            assert routing["device"] != "wsl", (zone, reachable, routing)


def test_phone_first_regardless_of_geofence(monkeypatch) -> None:
    """Phone is the default device whether the user is 'home' or 'away'."""
    tts = _load_tts()
    for zone in (None, "home", "gym", "campus"):
        _patch_world(tts, monkeypatch, phone_reachable=True, location_zone=zone)
        routing = tts.resolve_tts_device()
        assert routing["device"] == "phone", (zone, routing)


def test_speak_transport_routes_on_macrodroid_reachability_not_audio_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decree 2026-06-28: the /speak transport routes on MacroDroid reachability.

    Reverses the prior #423 routing gate. The dead-audio-proxy false-success was a
    failure mode of the *receiver-based* transport (which played nothing → false
    success into backend:null). The /speak transport speaks LOCALLY on the device
    and proves delivery via the playback-complete callback, so a down audio-proxy
    receiver must NOT block phone selection. The audio-proxy health is still
    surfaced as a diagnostic (available=False), but it is no longer a routing gate.
    """
    tts = _load_tts()
    monkeypatch.setattr(tts, "is_phone_reachable", lambda *a, **k: True)
    monkeypatch.setattr(tts, "_send_to_phone", lambda *a, **k: {"success": True})
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
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: False)
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda *a, **k: None)

    routing = tts.resolve_tts_device()

    assert routing["device"] == "phone"
    # The audio-proxy health rides along as a diagnostic but did not gate routing.
    assert routing["phone_audio_proxy"]["available"] is False
    assert routing["phone_audio_proxy"]["reason"] == "audio_proxy_phone_disconnected"


def test_unreachable_macrodroid_deep_falls_to_mac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the MacroDroid server is unreachable, the phone is NOT selected even if
    the transport is wired — routing deep-falls to Mac (the genuine no-phone case)."""
    tts = _load_tts()
    monkeypatch.setattr(tts, "is_phone_reachable", lambda *a, **k: False)
    monkeypatch.setattr(tts, "_send_to_phone", lambda *a, **k: {"success": True})
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda *a, **k: None)

    routing = tts.resolve_tts_device()

    assert routing["device"] == "mac"


def test_audio_proxy_health_reports_missing_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_phone_audio_proxy_health()` still fails closed on a missing heartbeat.

    The #423 health function is unchanged — it is just no longer a routing gate.
    A connected receiver with no heartbeat reports ``audio_proxy_heartbeat_missing``
    and available=False, so the diagnostic stays honest for any future
    receiver-based transport / observability.
    """
    tts = _load_tts()
    monkeypatch.setattr(
        tts,
        "_audio_proxy_health_checker",
        lambda: {
            "phone_connected": True,
            "receiver_running": True,
            "receiver_pid": 1234,
            "last_heartbeat": None,
        },
    )

    health = tts.get_phone_audio_proxy_health()

    assert health["available"] is False
    assert health["reason"] == "audio_proxy_heartbeat_missing"


def test_mac_is_deep_fallback_when_phone_unreachable(monkeypatch) -> None:
    """Mac local speakers are reached ONLY when the phone is unreachable."""
    tts = _load_tts()
    _patch_world(tts, monkeypatch, phone_reachable=False, satellite_healthy=True)
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)
    routing = tts.resolve_tts_device()
    assert routing["device"] == "mac"


def test_discord_voice_still_preempts(monkeypatch) -> None:
    """An operator actively in a Discord voice channel still wins over the phone."""
    tts = _load_tts()
    _patch_world(tts, monkeypatch, phone_reachable=True, discord_bot="custodes-bot")
    routing = tts.resolve_tts_device()
    assert routing["device"] == "discord"
    assert routing["discord_bot"] == "custodes-bot"


def test_discord_failure_demotes_to_phone_first(monkeypatch) -> None:
    """A failed Discord voice attempt falls through to phone before Mac."""
    tts = _load_tts()
    sent = []
    _patch_world(tts, monkeypatch, phone_reachable=True, discord_bot="custodes-bot")
    # The phone leg now blocks on the playback-complete callback up to
    # PHONE_PLAYBACK_WATCHDOG_S; no callback fires here, so shrink it to keep the
    # test fast (a missed callback is still a phone success — what we assert).
    monkeypatch.setattr(tts, "PHONE_PLAYBACK_WATCHDOG_S", 0.05)
    monkeypatch.setattr(
        tts,
        "speak_tts_discord",
        lambda *a, **k: {"success": False, "error": "discord_voice_not_played"},
    )

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    monkeypatch.setattr(
        tts,
        "speak_tts_mac",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Mac ran before phone")),
    )

    result = tts.speak_tts("discord fallback")

    assert result["success"] is True
    assert result["requested_device"] == "discord"
    assert result["method"] == "phone"
    assert result["route"] == "phone"
    assert any(params.get("tts_text") == "discord fallback" for _endpoint, params in sent)


def test_discord_failure_falls_back_to_mac_after_phone_failure(monkeypatch) -> None:
    """If Discord and phone both fail, Mac is the deep fallback."""
    tts = _load_tts()
    _patch_world(tts, monkeypatch, phone_reachable=True, discord_bot="custodes-bot")
    monkeypatch.setattr(
        tts,
        "speak_tts_discord",
        lambda *a, **k: {"success": False, "error": "discord_voice_not_played"},
    )
    monkeypatch.setattr(
        tts, "_send_to_phone", lambda *a, **k: {"success": False, "error": "phone_down"}
    )
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)
    monkeypatch.setattr(
        tts,
        "speak_tts_mac",
        lambda message, voice=None, rate=0: {
            "success": True,
            "method": "macos_say",
            "voice": voice or "Daniel",
            "message": message[:50],
        },
    )

    result = tts.speak_tts("mac fallback")

    assert result["success"] is True
    assert result["requested_device"] == "discord"
    assert result["method"] == "macos_say"
    assert result["route"] == "macos_say"
