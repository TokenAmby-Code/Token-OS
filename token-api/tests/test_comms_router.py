"""Comms-router unification invariants.

The authoritative notify/TTS service is the single comms middleware. Feature
code expresses intent ("notify: message + optional tactile/banner") and the
router (`routes.tts.dispatch_notify` / `resolve_tts_device`) owns device
selection, quiet-hours gating, and fanout. Circumventing the router by sending
spoken text phone-direct via `_send_to_phone(tts_text=...)` is a violation.

These are unit/structural guards, not a substitute for the live-path validation
(GT ready-for-ack, break-exhausted, AskUserQuestion) the migration was
exercised against.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any

TOKEN_API_DIR = Path(__file__).resolve().parents[1]

# Modules that ARE the router internals — the only code allowed to reach the
# phone transport directly.
_ROUTER_INTERNALS = {"routes/tts.py", "notify.py", "phone_service.py"}

# Lines bearing this marker are documented, reviewed exceptions (e.g. delivering
# a phone-hosted session's own TTS to its host device — not a geofence-routable
# notification).
_ALLOW_MARKER = "comms-router-allow"


def _load(mod: str):
    if str(TOKEN_API_DIR) not in sys.path:
        sys.path.insert(0, str(TOKEN_API_DIR))
    return importlib.import_module(mod)


def _feature_source_files() -> list[Path]:
    files = [TOKEN_API_DIR / "main.py"]
    files += sorted((TOKEN_API_DIR / "routes").glob("*.py"))
    out: list[Path] = []
    for f in files:
        rel = str(f.relative_to(TOKEN_API_DIR))
        if rel in _ROUTER_INTERNALS:
            continue
        out.append(f)
    return out


# ---------------- Guard: no feature-code TTS phone-bypass ----------------


def test_no_feature_code_sends_tts_text_phone_direct():
    """No feature-code callsite may put a spoken `tts_text` payload onto the
    phone transport. Spoken text must go through `dispatch_notify` so the
    geofence-first router decides the audible device."""
    offenders: dict[str, list[tuple[int, str]]] = {}
    for f in _feature_source_files():
        bad: list[tuple[int, str]] = []
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if '"tts_text"' in line and _ALLOW_MARKER not in line:
                bad.append((i, line.strip()[:100]))
        if bad:
            offenders[str(f.relative_to(TOKEN_API_DIR))] = bad
    assert not offenders, (
        "Feature code must route spoken text through the comms router "
        "(dispatch_notify), not phone-direct via a tts_text payload. "
        f"Offenders: {offenders}"
    )


# ---------------- Endpoint surface ----------------


def test_notify_endpoint_surface(app_env):
    """`/api/notify` is the single authoritative entry. The TTS-only sibling
    `/api/notify/tts` is retired (CLIs repointed to /api/notify)."""
    paths = {getattr(r, "path", None) for r in app_env.main.app.routes}
    for route in app_env.main.app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is None:
            continue
        prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
        paths.update(prefix + r.path for r in getattr(original_router, "routes", []))
    assert "/api/notify" in paths
    assert "/api/notify/tts" not in paths


# ---------------- dispatch_notify core ----------------


def _recorders(monkeypatch, tts, *, speak_result=None):
    calls = {"speak": [], "phone": []}

    def fake_speak_tts(message, voice=None, rate=0, instance_id=None, *a, **kw):
        calls["speak"].append(message)
        return speak_result or {"success": True, "route": "wsl", "method": "wsl_sapi"}

    def fake_send_to_phone(endpoint, params):
        calls["phone"].append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "speak_tts", fake_speak_tts)
    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    return calls


def test_dispatch_notify_speaks_via_router_and_never_phone_direct(monkeypatch):
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    calls = _recorders(monkeypatch, tts)

    result = asyncio.run(tts.dispatch_notify("hello world", vibe=30, banner="hi"))

    # Spoken text went through the router (speak_tts → resolve_tts_device), once.
    assert calls["speak"] == ["hello world"]
    # Tactile/banner reached the phone as device-control — but NEVER a tts_text.
    assert len(calls["phone"]) == 1
    _ep, params = calls["phone"][0]
    assert "tts_text" not in params
    assert params.get("vibe") == 30
    assert params.get("banner_text") == "hi"
    assert result.get("delivered") is True


def test_dispatch_notify_tts_failure_is_not_masked_by_tactile(monkeypatch: Any) -> None:
    """For spoken notifications, top-level delivered means true audio playback.

    A successful banner/vibe leg must not recreate the false-success condition
    where /api/notify returns delivered:true while the TTS backend played nothing.
    """
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    calls = _recorders(
        monkeypatch,
        tts,
        speak_result={
            "success": False,
            "route": None,
            "method": None,
            "reason": "no_playback_backend",
        },
    )

    result = asyncio.run(tts.dispatch_notify("hello world", vibe=30, banner="hi"))

    assert calls["speak"] == ["hello world"]
    assert len(calls["phone"]) == 1
    assert result.get("delivered") is False
    assert result.get("audio_delivered") is False
    assert result.get("tactile", {}).get("success") is True


def test_dispatch_notify_tactile_only_does_not_speak(monkeypatch):
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: False)
    calls = _recorders(monkeypatch, tts)

    asyncio.run(tts.dispatch_notify("", tts=False, vibe=30, banner="blocked"))

    assert calls["speak"] == []
    assert len(calls["phone"]) == 1
    _ep, params = calls["phone"][0]
    assert "tts_text" not in params
    assert params.get("banner_text") == "blocked"


def test_dispatch_notify_suppressed_in_quiet_hours(monkeypatch):
    tts = _load("routes.tts")
    monkeypatch.setattr(tts, "_is_quiet_hours", lambda *a, **k: True)
    calls = _recorders(monkeypatch, tts)

    result = asyncio.run(tts.dispatch_notify("hi", vibe=30, banner="hi"))

    assert result.get("suppressed") is True
    assert calls["speak"] == []
    assert calls["phone"] == []


def test_phone_direct_tts_only_occurs_inside_the_router(monkeypatch):
    """The one legitimate phone-direct TTS leg lives INSIDE speak_tts (the
    router), reached only when resolve_tts_device selects the phone."""
    tts = _load("routes.tts")
    sent = []

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "phone", "reason": "geofence: gym", "discord_bot": None},
    )

    result = tts.speak_tts("away from home")

    assert any(p.get("tts_text") == "away from home" for _e, p in sent)
    assert result.get("success") is True
    assert result.get("method") == "phone"
    assert result.get("route") == "phone"


def test_discord_fallthrough_respects_geofence_phone_only(monkeypatch: Any) -> None:
    """If Discord VC fails while geofenced away, fallback is phone-only.

    Discord is intentionally checked before the geofence, but a failed Discord
    leg must not leak away-from-home speech to local WSL/Mac speakers.
    """
    tts = _load("routes.tts")
    sent = []

    monkeypatch.setitem(tts.DESKTOP_STATE, "location_zone", "gym")
    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {
            "device": "discord",
            "reason": "operator in voice channel",
            "discord_bot": "token-bot",
        },
    )
    monkeypatch.setattr(
        tts,
        "speak_tts_discord",
        lambda *a, **k: {
            "success": False,
            "error": "discord_voice_not_played",
            "reason": "bot_not_in_channel",
        },
    )
    monkeypatch.setattr(tts, "_phone_tts_available", lambda: True)

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    monkeypatch.setattr(
        tts,
        "is_satellite_tts_available",
        lambda: (_ for _ in ()).throw(AssertionError("WSL fallback bypassed geofence")),
    )
    monkeypatch.setattr(
        tts,
        "speak_tts_mac",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Mac fallback bypassed geofence")),
    )
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)

    result = tts.speak_tts("away discord fallback")

    assert result.get("success") is True
    assert result.get("method") == "phone"
    assert result.get("route") == "phone"
    assert any(p.get("tts_text") == "away discord fallback" for _e, p in sent)


def test_wsl_fallthrough_reports_phone_route(monkeypatch: Any) -> None:
    """When WSL falls through to phone, public route telemetry says phone."""
    tts = _load("routes.tts")
    sent = []

    monkeypatch.setattr(
        tts,
        "resolve_tts_device",
        lambda **kw: {"device": "wsl", "reason": "satellite healthy", "discord_bot": None},
    )
    monkeypatch.setattr(
        tts,
        "speak_tts_wsl",
        lambda *a, **k: {
            "success": False,
            "error": "wsl_failed",
            "reason": "wsl_failed",
        },
    )
    monkeypatch.setattr(tts, "_phone_tts_available", lambda: True)

    def fake_send_to_phone(endpoint, params):
        sent.append((endpoint, dict(params or {})))
        return {"success": True}

    monkeypatch.setattr(tts, "_send_to_phone", fake_send_to_phone)
    monkeypatch.setattr(tts, "_mac_tts_available", lambda: True)

    result = tts.speak_tts("wsl fallback")

    assert result.get("success") is True
    assert result.get("requested_device") == "wsl"
    assert result.get("method") == "phone"
    assert result.get("route") == "phone"
    assert any(p.get("tts_text") == "wsl fallback" for _e, p in sent)


# ---------------- Router consolidation: notify.py delegates ----------------


def test_dispatch_notification_delegates_to_router(monkeypatch):
    """notify.py is no longer a second router — it delegates to the single
    routing brain (dispatch_notify), gaining Discord + geofence parity."""
    notify = _load("notify")
    tts = _load("routes.tts")
    seen = {}

    async def fake_dispatch_notify(message, **kw):
        seen["message"] = message
        seen["kw"] = kw
        return {"delivered": True}

    monkeypatch.setattr(tts, "dispatch_notify", fake_dispatch_notify)

    res = asyncio.run(
        notify.dispatch_notification(notify.NotifyRequest(message="ping", type="tts"))
    )
    assert seen["message"] == "ping"
    assert res.get("delivered") is True


def test_notify_has_no_second_device_router():
    """The parallel WSL>Mac>phone device order is retired; one routing decision."""
    notify = _load("notify")
    assert not hasattr(notify, "DEFAULT_DEVICE_ORDER")
    assert not hasattr(notify, "_select_devices")


# ---------------- force_device / distraction_source dropped ----------------


def test_enforce_request_drops_device_overrides():
    enforce = _load("enforce")
    fields = enforce.EnforceRequest.model_fields
    assert "distraction_source" not in fields
    assert "force_device" not in fields


def test_notify_request_drops_device_overrides():
    notify = _load("notify")
    fields = notify.NotifyRequest.model_fields
    assert "distraction_source" not in fields
    assert "force_device" not in fields
