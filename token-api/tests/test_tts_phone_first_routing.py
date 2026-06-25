"""Phone-first TTS routing invariants.

Decree (Emperor, 2026-06-25): the phone is first-contact for ALL TTS. Mac `say`
(local speakers) is a deep fallback only — reached when phone delivery is
unreachable/fails. This supersedes the old "geofence(away) → WSL satellite →
phone → Mac" ordering (the WSL-satellite era is over; geofence no longer gates
device selection).

`resolve_tts_device` must therefore:
  * NEVER select the WSL satellite, even when it probes healthy;
  * select the phone whenever it is reachable, regardless of geofence zone;
  * fall back to Mac only when the phone is unreachable;
  * still pre-empt to Discord voice when the operator is in a voice channel.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


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
    monkeypatch.setattr(tts, "is_satellite_tts_available", lambda *a, **k: satellite_healthy)
    monkeypatch.setattr(tts, "_get_discord_voice_bot", lambda *a, **k: discord_bot)
    monkeypatch.setitem(tts.DESKTOP_STATE, "location_zone", location_zone)


def test_phone_first_when_home_and_satellite_healthy(monkeypatch):
    """At home WITH a healthy WSL satellite, the phone still wins — the satellite
    is never selected. (This is the core inversion vs. the old WSL-first cascade.)"""
    tts = _load_tts()
    _patch_world(
        tts, monkeypatch, phone_reachable=True, satellite_healthy=True, location_zone="home"
    )
    routing = tts.resolve_tts_device()
    assert routing["device"] == "phone"


def test_wsl_satellite_never_selected(monkeypatch):
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


def test_phone_first_regardless_of_geofence(monkeypatch):
    """Phone is the default device whether the user is 'home' or 'away'."""
    tts = _load_tts()
    for zone in (None, "home", "gym", "campus"):
        _patch_world(tts, monkeypatch, phone_reachable=True, location_zone=zone)
        routing = tts.resolve_tts_device()
        assert routing["device"] == "phone", (zone, routing)


def test_mac_is_deep_fallback_when_phone_unreachable(monkeypatch):
    """Mac local speakers are reached ONLY when the phone is unreachable."""
    tts = _load_tts()
    _patch_world(tts, monkeypatch, phone_reachable=False, satellite_healthy=True)
    routing = tts.resolve_tts_device()
    assert routing["device"] == "mac"


def test_discord_voice_still_preempts(monkeypatch):
    """An operator actively in a Discord voice channel still wins over the phone."""
    tts = _load_tts()
    _patch_world(tts, monkeypatch, phone_reachable=True, discord_bot="custodes-bot")
    routing = tts.resolve_tts_device()
    assert routing["device"] == "discord"
    assert routing["discord_bot"] == "custodes-bot"
