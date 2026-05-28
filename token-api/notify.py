"""Device-aware notify dispatcher.

Single endpoint with intelligent fallback routing. Caller asks "notify me,"
server picks the device. `distraction_source` excludes a device from being a
stimulus target (so an "off your phone" enforce never targets the phone).

Routing chain:
  1. force_device set -> target only that device
  2. Otherwise preference order: WSL > Mac > phone, removing distraction_source
  3. Probe reachability per device; fall through on failure
  4. Never fall back to a device matching distraction_source — even if all
     other devices are unreachable, return delivered=False rather than poke
     the distraction device.
"""

from __future__ import annotations

import asyncio
import functools
import logging

from pydantic import BaseModel

from shared import (
    FALLBACK_VOICES,
    PROFILES,
    ULTIMATE_FALLBACK,
    is_phone_reachable,
    is_satellite_tts_available,
    log_event,
)

logger = logging.getLogger("token_api")

DEFAULT_DEVICE_ORDER = ("wsl", "mac", "phone")


class NotifyRequest(BaseModel):
    message: str
    type: str = "tts"  # "tts" | "sound" | "banner" | "music"
    distraction_source: str | None = None
    force_device: str | None = None
    voice: str | None = None
    rate: int | None = None
    sound: str | None = None
    instance_id: str | None = None


_send_to_phone = None


def init_deps(*, send_to_phone=None) -> None:
    """Late-bind helpers from main.py / phone_service to avoid circular imports."""
    global _send_to_phone
    if send_to_phone is not None:
        _send_to_phone = send_to_phone


def _device_reachable(device: str) -> bool:
    if device == "wsl":
        return bool(is_satellite_tts_available())
    if device == "phone":
        return bool(is_phone_reachable())
    if device == "mac":
        return True
    return False


def _select_devices(req: NotifyRequest) -> list[str]:
    if req.force_device:
        return [req.force_device]
    return [d for d in DEFAULT_DEVICE_ORDER if d != req.distraction_source]


def _resolve_wsl_voice(
    instance_id: str | None, explicit_voice: str | None
) -> tuple[str | None, int | None]:
    """Pick a WSL voice + rate either from the explicit override or fall back."""
    if explicit_voice:
        for p in PROFILES + FALLBACK_VOICES:
            if p.get("wsl_voice") == explicit_voice:
                return explicit_voice, p.get("wsl_rate", 0)
        return explicit_voice, 0
    return ULTIMATE_FALLBACK.get("wsl_voice"), ULTIMATE_FALLBACK.get("wsl_rate", 0)


async def _dispatch_tts(device: str, req: NotifyRequest) -> dict:
    from routes.tts import speak_tts_mac, speak_tts_wsl

    loop = asyncio.get_event_loop()
    if device == "wsl":
        wsl_voice, wsl_rate = _resolve_wsl_voice(req.instance_id, req.voice)
        return await loop.run_in_executor(
            None,
            functools.partial(speak_tts_wsl, req.message, wsl_voice, wsl_rate or 0),
        )
    if device == "mac":
        return await loop.run_in_executor(
            None,
            functools.partial(speak_tts_mac, req.message, req.voice, req.rate or 0),
        )
    if device == "phone":
        if _send_to_phone is None:
            return {"success": False, "error": "phone sender not initialized"}
        return await loop.run_in_executor(
            None,
            functools.partial(
                _send_to_phone,
                "/notify",
                {
                    "tts_text": req.message[:300],
                    "banner_text": req.message[:100],
                    "vibe": 30,
                },
            ),
        )
    return {"success": False, "error": f"unknown device: {device}"}


async def _dispatch_sound(device: str, req: NotifyRequest) -> dict:
    from routes.tts import play_sound

    loop = asyncio.get_event_loop()
    if device == "phone":
        if _send_to_phone is None:
            return {"success": False, "error": "phone sender not initialized"}
        params = {"beep": 50, "banner_text": req.message[:100] if req.message else "beep"}
        return await loop.run_in_executor(
            None, functools.partial(_send_to_phone, "/notify", params)
        )
    return await loop.run_in_executor(None, functools.partial(play_sound, req.sound))


async def _dispatch_banner(device: str, req: NotifyRequest) -> dict:
    loop = asyncio.get_event_loop()
    if device == "phone":
        if _send_to_phone is None:
            return {"success": False, "error": "phone sender not initialized"}
        return await loop.run_in_executor(
            None,
            functools.partial(_send_to_phone, "/notify", {"banner_text": req.message[:100]}),
        )
    # Desktop devices fall back to TTS for the banner content.
    return await _dispatch_tts(device, req)


async def _dispatch_music(device: str, req: NotifyRequest) -> dict:
    # Music routing is left as a follow-up; see project_spotify_redirect_local_wsl.
    return {
        "success": False,
        "error": "music dispatch not yet implemented",
        "device": device,
    }


async def _dispatch_to_device(device: str, req: NotifyRequest) -> dict:
    if req.type == "tts":
        return await _dispatch_tts(device, req)
    if req.type == "sound":
        return await _dispatch_sound(device, req)
    if req.type == "banner":
        return await _dispatch_banner(device, req)
    if req.type == "music":
        return await _dispatch_music(device, req)
    return {"success": False, "error": f"unknown type: {req.type}"}


async def dispatch_notification(request: NotifyRequest) -> dict:
    targets = _select_devices(request)
    if not targets:
        return {"delivered": False, "reason": "no_target"}

    attempts: list[dict] = []
    for device in targets:
        # Never fall back to the distraction device, ever.
        if request.distraction_source and device == request.distraction_source:
            attempts.append({"device": device, "skipped": True, "reason": "distraction_source"})
            continue

        if not _device_reachable(device):
            attempts.append({"device": device, "skipped": True, "reason": "unreachable"})
            continue

        try:
            result = await _dispatch_to_device(device, request)
        except Exception as e:
            logger.warning(f"notify: dispatch to {device} failed: {e}")
            attempts.append({"device": device, "success": False, "error": str(e)})
            continue

        attempts.append({"device": device, **result})
        if result.get("success") or result.get("delivered"):
            await log_event(
                "notify",
                details={
                    "type": request.type,
                    "device": device,
                    "distraction_source": request.distraction_source,
                    "force_device": request.force_device,
                    "message": request.message[:200],
                    "attempts": attempts,
                },
            )
            return {
                "delivered": True,
                "device": device,
                "attempts": attempts,
                "result": result,
            }

    reason = (
        "all_non_distraction_devices_unreachable"
        if request.distraction_source
        else "all_devices_unreachable"
    )
    await log_event(
        "notify",
        details={
            "type": request.type,
            "device": None,
            "distraction_source": request.distraction_source,
            "force_device": request.force_device,
            "message": request.message[:200],
            "delivered": False,
            "reason": reason,
            "attempts": attempts,
        },
    )
    return {"delivered": False, "reason": reason, "attempts": attempts}
