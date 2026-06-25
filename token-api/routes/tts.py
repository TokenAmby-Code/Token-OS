"""
TTS/Notification route module — extracted from main.py.

Owns:
- TTS speech functions (Mac + WSL satellite routing)
- TTS queue system (sequential playback, skip, mute)
- Notification endpoints (/api/notify/*, /api/tts/*)
- Webhook sender

Does NOT own:
- Morning enforce / unified enforce (stays in main.py)
- Timer worker (stays in main.py)
- Phone/Pavlok enforcement (stays in main.py)
"""

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

import aiosqlite
import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from human_render import sanitize_human_render_text, sanitize_human_render_text_sync
from instance_mutation import sanctioned_update_instance
from personas import (
    BACKUP_ASTARTES,
    PRIMARY_ASTARTES,
    voice_settings_for_tts_voice,
)
from shared import (
    DB_PATH,
    DESKTOP_CONFIG,
    DESKTOP_STATE,
    DISCORD_DAEMON_URL,
    TTS_BACKEND,
    TTS_GLOBAL_MODE,
    get_quiet_hours_status,
    is_phone_reachable,
    is_satellite_tts_available,
    log_event,
    resolve_instance_pane,
)

logger = logging.getLogger("token_api")

# TTS playback focus snap is explicit-action only. Direct-to-surface hot TTS
# must not steal tmux focus; queued items promoted/played by the operator may
# snap because pressing play is the focus intent. TOKEN_API_TTS_AUTO_FOCUS is an
# emergency switch for restoring broad snap behavior.
TTS_AUTO_FOCUS_ENABLED = os.environ.get("TOKEN_API_TTS_AUTO_FOCUS", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}


def _sanitize_public_text(value: str | None) -> str:
    """Translate/redact raw tmux pane ids at synchronous human boundaries."""
    if not value:
        return ""
    return sanitize_human_render_text_sync(str(value)) or ""


async def _sanitize_public_text_async(value: str | None) -> str:
    """Async/offloaded variant for event-loop notification/TTS paths."""
    if not value:
        return ""
    return await sanitize_human_render_text(str(value)) or ""


router = APIRouter()


# ============ Late-bound Dependencies ============
# Functions from other main.py sections that haven't been extracted yet.
# Set by init_deps() called from main.py after import.

_send_to_phone = None
_custodes_state_event_handler = None
TTS_LANGUISHING_THRESHOLD = 5
# The sender whose TTS innately bypasses the pause queue (plays immediately).
CUSTODES_PERSONA_SLUG = "custodes"


def init_deps(*, send_to_phone=None, custodes_state_event_handler=None):
    """Receive dependencies from main.py to avoid circular imports.

    Called once during app startup, before any requests are served.
    """
    global _send_to_phone, _custodes_state_event_handler
    if send_to_phone is not None:
        _send_to_phone = send_to_phone
    if custodes_state_event_handler is not None:
        _custodes_state_event_handler = custodes_state_event_handler


# ============ Pydantic Models ============


class NotifyRequest(BaseModel):
    """Unified comms intent for the authoritative `POST /api/notify` entry.

    Callers express intent — message + optional tactile/banner — and the router
    (`dispatch_notify` → `resolve_tts_device`) owns device selection,
    quiet-hours gating, and fanout. Callers do NOT pick a device/transport; the
    retired `device_id`/`force_device`/`distraction_source` knobs let feature
    code circumvent the geofence-first router, which is always a violation.
    """

    message: str = ""
    tts: bool = True  # speak `message` via the geofence-first router
    vibe: int | None = None  # phone/Pavlok tactile attention signal
    beep: int | None = None  # phone/Pavlok tactile attention signal
    banner: str | None = None  # phone banner text (defaults to message head)
    voice: str | None = None  # optional TTS voice override
    instance_id: str | None = None  # instance whose voice profile to use


class SoundRequest(BaseModel):
    sound_file: str | None = None  # Path to sound file


class QueueTTSRequest(BaseModel):
    instance_id: str
    message: str
    queue_target: str = "pause"  # "hot" or "pause"


class PromoteRequest(BaseModel):
    instance_id: str | None = None  # If set, promote that instance's items


class PlayPaneRequest(BaseModel):
    instance_id: str  # Promote all items from this instance to hot queue


# ============ TTS/Notification System ============

# Platform detection
IS_MACOS = sys.platform == "darwin"
DEFAULT_SOUND = "chimes.wav"


SOUND_MAP = {
    "chimes.wav": "/System/Library/Sounds/Glass.aiff",
    "notify.wav": "/System/Library/Sounds/Ping.aiff",
    "ding.wav": "/System/Library/Sounds/Tink.aiff",
    "tada.wav": "/System/Library/Sounds/Hero.aiff",
}


def play_sound(sound_file: str = None) -> dict:
    """Play a notification sound using macOS afplay."""
    sound_name = sound_file or DEFAULT_SOUND
    sound_path = SOUND_MAP.get(sound_name, SOUND_MAP["chimes.wav"])

    try:
        result = subprocess.run(["afplay", sound_path], capture_output=True, timeout=10)
        if result.returncode == 0:
            return {"success": True, "method": "afplay", "file": sound_path}
        return {"success": False, "error": f"afplay failed: {result.stderr.decode()[:100]}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Sound playback timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def clean_markdown_for_tts(text: str) -> str:
    """Clean markdown syntax for natural TTS output.

    Removes/transforms markdown that sounds bad when spoken aloud,
    like table separators ("pipe dash dash dash") or headers ("hash hash").
    """
    # Unicode arrows/symbols that TTS mispronounces
    text = text.replace("\u2192", " to ")
    text = text.replace("\u2190", " from ")
    text = text.replace("\u2194", " both ways ")
    text = text.replace("\u21d2", " implies ")
    text = text.replace("\u21d0", " implied by ")
    text = text.replace("\u279c", " to ")
    text = text.replace("\u2794", " to ")
    text = text.replace("\u2022", ",")  # Bullet point
    text = text.replace("\u2026", "...")  # Ellipsis
    text = text.replace("\u2014", ", ")  # Em dash
    text = text.replace("\u2013", ", ")  # En dash

    # Remove backslashes that might be read aloud
    text = text.replace("\\", " ")

    # Path compression - replace long paths with friendly names
    path_replacements = [
        ("~/.claude/", ""),
        ("~/", ""),
    ]
    for path, replacement in path_replacements:
        text = text.replace(path, replacement)

    # Table separators: |---|---| or |:---:|:---:| → remove entirely
    text = re.sub(r"\|[-:]+\|[-:|\s]+", "", text)  # Table separator rows

    # Remove remaining markdown separators (---) on their own line
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)  # Horizontal rules

    # Headers: ## Title → Title (strip # sequences followed by space)
    text = re.sub(r"#{1,6}\s+", "", text)

    # Bold/italic: **text** or *text* or __text__ or _text_ → text
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # Bold
    text = re.sub(r"\*(.+?)\*", r"\1", text)  # Italic
    text = re.sub(r"__(.+?)__", r"\1", text)  # Bold alt
    text = re.sub(r"_(.+?)_", r"\1", text)  # Italic alt

    # Code blocks: ```...``` → [code block]
    text = re.sub(r"```[\s\S]*?```", "[code block]", text)

    # Inline code: `code` → code
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Links: [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Bullet points: - item or * item → item
    text = re.sub(r"^[\-\*]\s+", "", text, flags=re.MULTILINE)

    # Numbered lists: 1. item → item
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)

    # Table pipes: | cell | cell | → cell, cell
    text = re.sub(r"\|", ", ", text)

    # Clean up multiple spaces/newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"  +", " ", text)
    text = re.sub(r", ,", ",", text)  # Clean double commas from empty cells

    return text.strip()


def _mac_tts_available() -> bool:
    """True only when a local `say` backend can plausibly render audible speech."""
    return IS_MACOS and shutil.which("say") is not None


def _mac_sound_available() -> bool:
    """True only when a local `afplay` backend can plausibly render a sound."""
    return IS_MACOS and shutil.which("afplay") is not None


def _phone_tts_available() -> bool:
    """Phone TTS is a real playback target only when the transport is initialized
    and its reachability probe is green."""
    return _send_to_phone is not None and is_phone_reachable()


def _no_playback_backend(reason: str = "no_playback_backend") -> dict:
    return {
        "success": False,
        "error": reason,
        "reason": reason,
        "method": None,
        "route": None,
    }


def speak_tts_mac(message: str, voice: str = None, rate: int = 0) -> dict:
    """Speak a message using macOS `say` command.

    Uses Popen instead of run() to allow process termination via skip_tts().
    """
    global tts_current_process, tts_skip_requested

    if not _mac_tts_available():
        return _no_playback_backend("mac_tts_unavailable")

    voice = voice or "Daniel"
    TTS_BACKEND["current"] = "mac"

    # Map SAPI rate scale (-10..10) to say WPM; default 0 → 190 WPM (slightly fast)
    wpm = 190 if rate == 0 else 175 + (rate * 15)
    wpm = max(80, min(300, wpm))

    try:
        process = subprocess.Popen(
            ["say", "-v", voice, "-r", str(wpm), message],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tts_current_process = process
        process.wait(timeout=300)
        tts_current_process = None
        TTS_BACKEND["current"] = None

        if process.returncode == 0:
            return {"success": True, "method": "macos_say", "voice": voice, "message": message[:50]}
        if tts_skip_requested:
            tts_skip_requested = False
            return {
                "success": False,
                "skipped": True,
                "method": "skipped",
                "reason": "skipped",
                "message": message[:50],
            }
        return {"success": False, "error": f"say failed with code {process.returncode}"}
    except subprocess.TimeoutExpired:
        if tts_current_process:
            tts_current_process.kill()
            tts_current_process = None
        TTS_BACKEND["current"] = None
        return {"success": False, "error": "TTS timed out"}
    except Exception as e:
        tts_current_process = None
        TTS_BACKEND["current"] = None
        return {"success": False, "error": str(e)}


def speak_tts_wsl(message: str, voice: str, rate: int = 0, use_file_playback: bool = False) -> dict:
    """Speak a message via WSL satellite TTS (Windows SAPI voices).

    Blocks until satellite returns (speech complete or skipped).
    When use_file_playback=True, uses synthesize-to-file + WMP playback
    (supports pause/resume/speed). Otherwise uses direct SpeakAsync.
    """
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    TTS_BACKEND["current"] = "wsl"

    endpoint = "/tts/synth-and-play" if use_file_playback else "/tts/speak"
    payload = {"message": message, "voice": voice, "rate": rate}

    try:
        resp = requests.post(
            f"http://{host}:{port}{endpoint}",
            json=payload,
            timeout=300,  # Long timeout — blocks until speech/playback done
        )
        TTS_BACKEND["current"] = None

        if resp.status_code == 200:
            data = resp.json()
            method = "skipped" if data.get("skipped") else data.get("method", "wsl_sapi")
            expected_hash = hashlib.sha256(message.encode("utf-8")).hexdigest()
            rendered_hash = data.get("rendered_hash")
            if data.get("success") and not data.get("skipped") and not rendered_hash:
                return {
                    "success": False,
                    "error": "satellite_missing_text_integrity_ack",
                    "method": method,
                    "voice": voice,
                    "message_chars": len(message),
                }
            if data.get("success") and rendered_hash and rendered_hash != expected_hash:
                return {
                    "success": False,
                    "error": "satellite_text_integrity_check_failed",
                    "method": method,
                    "voice": voice,
                    "message_chars": len(message),
                    "rendered_chars": data.get("rendered_chars"),
                }
            skipped = bool(data.get("skipped"))
            return {
                # A skip is controlled interruption, but it is not true playback.
                "success": bool(data.get("success", False)) and not skipped,
                "skipped": skipped,
                "method": method,
                "voice": voice,
                "message": message[:50],
                "message_chars": len(message),
                "rendered_chars": data.get("rendered_chars"),
                "rendered_hash": rendered_hash,
                "transport": data.get("transport"),
                "reason": "skipped" if skipped else data.get("reason"),
            }
        elif resp.status_code == 409:
            return {"success": False, "error": "satellite_busy"}
        else:
            return {"success": False, "error": f"satellite returned {resp.status_code}"}

    except (requests.ConnectionError, requests.Timeout) as e:
        TTS_BACKEND["current"] = None
        TTS_BACKEND["satellite_available"] = False
        TTS_BACKEND["last_health_check"] = time.time()
        logger.warning(f"TTS WSL: Satellite unreachable: {e}")
        return {"success": False, "error": "satellite_unreachable"}
    except Exception as e:
        TTS_BACKEND["current"] = None
        logger.error(f"TTS WSL: Unexpected error: {e}")
        return {"success": False, "error": str(e)}


def _get_discord_voice_bot() -> str | None:
    """Return a bot whose voice connection can actually deliver audio, else None.

    A bot is a usable consumer only when the daemon reports it both `connected`
    AND `connectionState == "ready"`. The daemon's multi-bot status historically
    set `connected = !!state.connection`, which stays truthy for a destroyed /
    half-open connection. Trusting that flag let TTS route to a dead pipe and
    claim success — the `feedback_anti_blind_dedup` failure mode. We require the
    `ready` connection state so a stale connection no longer masquerades as a
    live voice consumer. Cached for 5s.
    """
    now = time.time()
    cache = _get_discord_voice_bot
    if hasattr(cache, "_result") and now - cache._checked < 5:
        return cache._result

    result = None
    try:
        resp = requests.get(f"{DISCORD_DAEMON_URL}/voice/status", timeout=1)
        if resp.status_code == 200:
            statuses = resp.json()
            for bot_name, status in statuses.items():
                if status.get("connected") and status.get("connectionState") == "ready":
                    result = bot_name
                    break
    except Exception:
        pass

    cache._result = result
    cache._checked = now
    return result


def speak_tts_discord(message: str, bot_name: str, voice: str = None, rate: int = 0) -> dict:
    """Route TTS through Discord voice channel. Device-agnostic — audio plays wherever the operator is listening."""
    mac_voice = voice or "Daniel"
    wpm = 190 if rate == 0 else 175 + (rate * 15)
    wpm = max(80, min(300, wpm))

    try:
        resp = requests.post(
            f"{DISCORD_DAEMON_URL}/voice/tts",
            json={"message": message, "bot": bot_name, "voice": mac_voice, "rate": wpm},
            timeout=60,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("played"):
                return {
                    "success": True,
                    "method": "discord_voice",
                    "bot": bot_name,
                    "voice": mac_voice,
                    "message": message[:50],
                }
            # 200 but no confirmed playback: the daemon accepted the request but
            # did not deliver audio to a live channel. Do not claim success.
            return {
                "success": False,
                "error": "discord_voice_not_played",
                "reason": "bot_not_in_channel",
                "bot": bot_name,
            }
        if resp.status_code == 409:
            return {"success": False, "error": "discord_voice_busy", "reason": "discord_voice_busy"}
        return {
            "success": False,
            "error": f"Discord TTS returned {resp.status_code}",
            "reason": "discord_daemon_error",
        }
    except requests.Timeout:
        return {"success": False, "error": "discord_tts_timeout", "reason": "discord_tts_timeout"}
    except Exception as e:
        logger.warning(f"TTS Discord: failed ({e}), will fall through to local")
        return {"success": False, "error": str(e), "reason": "discord_unreachable"}


def resolve_tts_device(instance_id: str = None, wsl_voice: str = None) -> dict:
    """Determine which device should receive TTS output.

    Phone-first doctrine (Emperor decree, 2026-06-25): the phone is first-contact
    for ALL TTS, regardless of geofence. Mac `say` (local speakers) is a deep
    fallback ONLY — reached when phone delivery is unreachable. This supersedes
    the old geofence(away) → WSL-satellite → phone → Mac ordering; the WSL
    satellite era is over and geofence no longer gates device selection.

    Priority cascade:
    1. Discord voice — if the operator is actively in a voice channel, audio goes
       there (a deliberate live conversation surface, not ambient routing).
    2. Phone — first-contact whenever reachable.
    3. Mac — deep fallback, only when the phone is unreachable.

    The WSL satellite is intentionally NOT in this cascade. ``wsl_voice`` is kept
    in the signature for caller compatibility but no longer influences routing.

    Returns:
        {"device": "discord"|"phone"|"mac"|None, "reason": str, "discord_bot": str|None}
    """
    # 1. Discord voice channel — operator in VC means audio goes there.
    discord_bot = _get_discord_voice_bot()
    if discord_bot:
        return {
            "device": "discord",
            "reason": "operator in voice channel",
            "discord_bot": discord_bot,
        }

    # 2. Phone — first-contact for all TTS, regardless of geofence zone.
    if _phone_tts_available():
        return {"device": "phone", "reason": "phone-first", "discord_bot": None}

    # 3. Mac — deep fallback, only when phone delivery is unreachable.
    if _mac_tts_available():
        return {
            "device": "mac",
            "reason": "deep fallback: phone unreachable",
            "discord_bot": None,
        }

    return {"device": None, "reason": "no playback backend", "discord_bot": None}


def speak_tts(
    message: str,
    voice: str = None,
    rate: int = 0,
    instance_id: str = None,
    wsl_voice: str = None,
    wsl_rate: int = None,
    use_file_playback: bool = False,
) -> dict:
    """Route TTS to the best available device via resolve_tts_device().

    Dispatches to Discord voice, phone TTS, or Mac local speech based on the
    resolved device. WSL args remain accepted for caller compatibility but are no
    longer part of the phone-first routing cascade. Falls through on failed live
    delivery, and never reports success without a concrete playback method.

    Args:
        message: Text to speak
        voice: macOS voice name (for Mac fallback / Discord TTS voice)
        rate: Rate for Mac TTS
        instance_id: Optional instance ID for logging
        wsl_voice: Deprecated Windows SAPI voice name (ignored for routing)
        wsl_rate: Deprecated WSL TTS rate (ignored for routing)
        use_file_playback: Deprecated WSL playback flag (ignored for routing)
    """
    if not message:
        return {"success": False, "error": "No message provided"}

    # Clean markdown syntax for natural TTS output and fail-closed on raw tmux ids.
    message = _sanitize_public_text(clean_markdown_for_tts(message))

    routing = resolve_tts_device(instance_id=instance_id, wsl_voice=wsl_voice)
    device = routing.get("device")
    logger.info(f"TTS: Routing to {device} ({routing['reason']})")

    def _finish(result: dict) -> dict:
        """Stamp truthful routing telemetry so callers can see where audio
        actually went (or why it failed) without trusting the requested device.
        `route` is the device that produced a successful delivery; on failure it
        is None and `reason` carries an actionable code."""
        result = dict(result or {})
        result.setdefault("success", False)
        result.setdefault("method", None)
        result["requested_device"] = device
        if result.get("success"):
            result["route"] = result.get("method") or device
            result.setdefault("reason", None)
        else:
            result["route"] = None
            result.setdefault("reason", result.get("error") or "tts_delivery_failed")
        return result

    def _send_phone_tts() -> dict:
        """Send spoken text to the phone transport and stamp truthful routing.

        Phone can be either the selected device or a fallthrough from Discord.
        The transport returns only success/failure, so stamp `method=phone`
        before `_finish()` derives the public `route`.
        """
        if _send_to_phone is None:
            return _no_playback_backend("phone_transport_unavailable")
        result = dict(_send_to_phone("/notify", {"tts_text": message}) or {})
        if result.get("success"):
            result.setdefault("method", "phone")
        return result

    if device is None:
        result = _no_playback_backend("no_playback_backend")
        result["route_reason"] = routing.get("reason")
        return _finish(result)

    # Dispatch with fallthrough on failure. Phone-first: every non-Discord path
    # starts at the phone when reachable and demotes only to a real Mac backend.
    if device == "discord":
        result = speak_tts_discord(message, routing["discord_bot"], voice, rate)
        if result.get("success"):
            return _finish(result)
        logger.info(f"TTS: Discord failed ({result.get('error')}), falling through to phone/Mac")
        if _phone_tts_available():
            result = _send_phone_tts()
            if result.get("success"):
                return _finish(result)
        if _mac_tts_available():
            return _finish(speak_tts_mac(message, voice, rate))
        return _finish(_no_playback_backend("no_fallthrough_playback_backend"))

    if device == "phone":
        # Reaching this branch means the router selected phone. The router may be
        # monkeypatched in invariant tests; do not re-probe reachability here.
        result = _send_phone_tts()
        if result.get("success"):
            return _finish(result)
        logger.info(f"TTS: Phone failed ({result.get('error')})")
        if _mac_tts_available():
            return _finish(speak_tts_mac(message, voice, rate))
        return _finish(_no_playback_backend("phone_failed_no_fallthrough_backend"))

    if device == "mac":
        return _finish(speak_tts_mac(message, voice, rate))

    return _finish(_no_playback_backend("unknown_playback_backend"))

async def dispatch_notify(
    message: str,
    *,
    tts: bool = True,
    vibe: int | None = None,
    beep: int | None = None,
    banner: str | None = None,
    voice: str | None = None,
    instance_id: str | None = None,
    context: dict | None = None,
) -> dict:
    """Authoritative comms entry — the single front door to the router.

    Feature code calls this in-process to notify the Emperor. One call carries
    the whole intent (message + optional tactile/banner); the router owns:
      * geofence-first TTS routing (Discord VC → WSL/phone-by-geofence → Mac)
        via speak_tts()/resolve_tts_device();
      * tactile (vibe/beep) + banner delivery as the phone attention signal;
      * quiet-hours gating across the whole notification.

    Spoken text NEVER goes phone-direct from here — it always flows through the
    router, which decides the audible device. The phone leg below carries only
    tactile + banner (device-control), never a tts_text payload. Splitting "TTS
    to the router, banner straight to the phone" at a callsite, or reaching the
    transport internals (_send_to_phone, speak_tts_{mac,wsl,discord}) directly,
    circumvents this middleware and is always a violation.
    """
    message = await _sanitize_public_text_async(message)
    banner = await _sanitize_public_text_async(banner) if banner is not None else None

    if _is_quiet_hours():
        logger.info(f"Notify suppressed (quiet hours): {(message or banner or '')[:80]}")
        return {
            "delivered": False,
            "suppressed": True,
            "reason": "quiet_hours",
            "route": "suppressed",
        }

    loop = asyncio.get_event_loop()

    # Resolve the instance's voice profile when one is named and no explicit
    # voice override was given (mirrors the retired /api/notify/tts behavior).
    wsl_voice = None
    wsl_rate = None
    if tts and message and instance_id and not voice:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT tts_voice FROM instances WHERE id = ?", (instance_id,)
                )
                row = await cursor.fetchone()
            if row and row["tts_voice"] is None:
                tts = False
            elif row and row["tts_voice"]:
                wsl_voice = row["tts_voice"]
                settings = voice_settings_for_tts_voice(wsl_voice)
                voice = settings["mac_voice"]
                wsl_rate = settings["wsl_rate"]
        except Exception as e:
            logger.warning(f"notify: voice profile lookup failed for {instance_id}: {e}")

    audio_requested = bool(tts and message)
    tts_result = None
    if audio_requested:
        tts_result = await loop.run_in_executor(
            None,
            functools.partial(speak_tts, message, voice, 0, instance_id, wsl_voice, wsl_rate),
        )

    # Tactile + banner are the phone attention signal. This is a *router*
    # policy (the phone is currently the only tactile/banner surface), kept in
    # the middleware — never decided at a callsite. When notif routing grows
    # (e.g. Discord banners), it changes here, not at every caller.
    phone_params: dict = {}
    if vibe is not None:
        phone_params["vibe"] = vibe
    if beep is not None:
        phone_params["beep"] = beep
    banner_text = banner if banner is not None else (message[:100] if message else None)
    if banner_text:
        phone_params["banner_text"] = banner_text

    tactile_result = None
    if phone_params and _send_to_phone is not None:
        tactile_result = await loop.run_in_executor(
            None,
            functools.partial(_send_to_phone, "/notify", phone_params),
        )

    tactile_delivered = bool(
        tactile_result and (tactile_result.get("success") or tactile_result.get("overall_success"))
    )
    audio_delivered = bool(tts_result and tts_result.get("success"))
    # If the caller requested spoken audio, top-level delivery means true audio
    # playback. A phone banner/vibe must not mask a failed/no-backend TTS leg.
    delivered = audio_delivered if audio_requested else tactile_delivered
    route = tts_result.get("route") if tts_result else None
    result = {
        "delivered": delivered,
        "audio_delivered": audio_delivered,
        "route": route,
        "tts": tts_result,
        "tactile": tactile_result,
    }
    await log_event(
        "notify",
        instance_id=instance_id,
        details={
            "message": (message or "")[:200],
            "tts": bool(tts and message),
            "vibe": vibe,
            "beep": beep,
            "banner": banner_text,
            "route": route,
            "delivered": delivered,
            "audio_delivered": audio_delivered,
            "tactile_delivered": tactile_delivered,
            "context": context,
        },
    )
    return result


# ============ TTS Queue System ============
# Ensures TTS messages don't overlap - each plays sequentially


@dataclass
class TTSQueueItem:
    """Item in the TTS queue."""

    instance_id: str
    message: str
    voice: str
    sound: str
    tab_name: str
    queue_target: str = "pause"  # "hot" or "pause"
    queued_at: datetime = field(default_factory=datetime.now)
    status: str = "queued"  # queued, playing, completed
    tmux_pane: str | None = None  # live-resolved pane id for @TTS_STATE tracking (set at playback)
    focus_on_playback: bool = False  # true only for explicit operator-initiated playback
    playback_target: str | None = None  # resolved non-null audio target at enqueue time


# Global TTS queue state — two-queue model
# Hot queue: auto-plays immediately (VC/sync sessions, promoted items)
# Pause queue: accumulates silently, requires explicit promote to play
hot_queue: deque[TTSQueueItem] = deque()
pause_queue: deque[TTSQueueItem] = deque()
tts_current: TTSQueueItem | None = None
tts_current_process: subprocess.Popen | None = None  # Current TTS/sound process for skip support
tts_skip_requested: bool = False  # Flag to indicate skip was requested (vs. actual failure)
tts_queue_lock = asyncio.Lock()
tts_worker_task: asyncio.Task | None = None


def _positive_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer env var with a safe fallback."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is invalid; using %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%r is below minimum %s; using %s", name, raw, minimum, default)
        return default
    return value


# Passive held-message drain policy.  Pause queue entries older than this are no
# longer actionable as speech; they are deliberately expired and logged when the
# authoritative languishing snapshot is read.  This is a source drain, not an
# alert debounce.
TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS = _positive_int_env(
    "TOKEN_API_TTS_PAUSE_HELD_MAX_AGE_SECONDS", 3600
)
TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS = _positive_int_env("TOKEN_API_TTS_PAUSE_SWEEP_TTL_SECONDS", 30)
_last_pause_queue_expiry_sweep = 0.0

# Languishing emit latch: record once for a stuck head, then only re-record when
# the same head genuinely worsens (depth increases) or a different head reaches
# languishing.  Cleared on negative edge.  The label is internal/diagnostic only;
# it must not declare or route as enforcement.
_tts_languishing_emit_latch: dict[str, object] = {}


def _set_tts_state(pane_id: str | None, state: str):
    """Set @TTS_STATE on a tmux pane. Fire-and-forget."""
    if not pane_id:
        return
    try:
        if state:
            subprocess.run(
                ["tmux", "set-option", "-p", "-t", pane_id, "@TTS_STATE", state],
                capture_output=True,
                timeout=2,
            )
        else:
            subprocess.run(
                ["tmux", "set-option", "-p", "-u", "-t", pane_id, "@TTS_STATE"],
                capture_output=True,
                timeout=2,
            )
    except Exception:
        pass  # fire and forget


# ============ Playback Focus Snap ============
# When a TTS item begins playback (the moment the dispatcher transitions
# tts_current None -> item), snap the operator's tmux focus to the originating
# pane and zoom it. Speaking implies "respond to me" — focus should follow.
# See: Mars/Tasks/TTS Playback Focus Snap.md. Local-only (v1): only the machine
# that owns the pane snaps; cross-machine speakers do not trigger remote focus.


@functools.lru_cache(maxsize=1)
def _local_device_name() -> str | None:
    """This machine's device identity (e.g. "Mac-Mini", "TokenPC").

    Lazily imported from imperium_config: routes.tts is imported before main.py
    inserts cli-tools/lib onto sys.path, so a module-level import would fail.
    """
    try:
        from imperium_config import cfg

        return cfg("device_name")
    except Exception:
        return None


def _tmux(args: list[str], timeout: float = 2) -> subprocess.CompletedProcess | None:
    """Run a tmux command, returning the CompletedProcess (or None on error).

    Centralizes tmux invocation so the focus-snap path is monkeypatchable and
    never raises into playback. Run off the event loop via asyncio.to_thread.
    """
    try:
        return subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "IMPERIUM_TMUX_RAW": "1"},
        )
    except Exception:
        return None


async def _focus_and_zoom_pane(pane_id: str) -> dict:
    """Focus `pane_id` and ensure it is the zoomed pane in its window.

    Zoom-dedup rules (ticket): if the speaker is already the zoomed pane, leave
    it (don't toggle off). If a *different* pane is zoomed, unzoom it first, then
    zoom the speaker — never stack zooms.
    """
    actions: list[str] = []

    # Inspect the target's window: which pane is active, and is it zoomed.
    info = await asyncio.to_thread(
        _tmux,
        ["list-panes", "-t", pane_id, "-F", "#{pane_active} #{pane_id} #{window_zoomed_flag}"],
    )
    active_pane: str | None = None
    zoomed = False
    if info is not None and getattr(info, "returncode", 1) == 0:
        for line in (info.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                is_active, pid, zflag = parts[0], parts[1], parts[2]
                if zflag == "1":
                    zoomed = True
                if is_active == "1":
                    active_pane = pid

    already_zoomed_on_target = zoomed and active_pane == pane_id

    # A different pane holds the zoom — unzoom it before focusing the speaker.
    if zoomed and not already_zoomed_on_target:
        await asyncio.to_thread(_tmux, ["resize-pane", "-Z", "-t", active_pane or pane_id])
        actions.append("unzoom_other")

    await asyncio.to_thread(_tmux, ["select-pane", "-t", pane_id])
    actions.append("select")

    if not already_zoomed_on_target:
        await asyncio.to_thread(_tmux, ["resize-pane", "-Z", "-t", pane_id])
        actions.append("zoom")

    return {"focused": True, "actions": actions}


# ============ Selected-instance marker ============
# The Ops Cockpit "select + expand an instance" primitive (feature A: manual
# double-click selection; feature B: talking-instance overlay) shares ONE tmux
# realization — `_focus_zoom_and_mark`: focus + zoom the pane, then stamp
# @OPS_SELECTED on it so the selection is visible on the pane border too. Only
# one pane is ever selected; stamping a new one clears the marker from any other
# pane. The border render lives in cli-tools/tmux/tmux-base.conf (the @TTS_STATE
# idiom). Fire-and-forget: the marker is cosmetic and never blocks focus.


def _set_ops_selected(pane_id: str | None) -> None:
    """Stamp @OPS_SELECTED=1 on `pane_id`, clearing it from every other pane.

    Routed through `_tmux` (so it never raises and stays monkeypatchable). Exactly
    one pane carries the marker after this returns. Synchronous; async callers on
    the event loop wrap it in asyncio.to_thread.
    """
    listed = _tmux(["list-panes", "-a", "-F", "#{pane_id} #{@OPS_SELECTED}"])
    if listed is not None and getattr(listed, "returncode", 1) == 0:
        for line in (listed.stdout or "").splitlines():
            parts = line.split()
            # Clear the marker anywhere it lingers except the pane we're selecting.
            if len(parts) >= 2 and parts[1] and parts[0] != pane_id:
                _tmux(["set-option", "-p", "-u", "-t", parts[0], "@OPS_SELECTED"])
    if pane_id:
        _tmux(["set-option", "-p", "-t", pane_id, "@OPS_SELECTED", "1"])


async def _focus_zoom_and_mark(pane_id: str) -> dict:
    """The shared 'select + expand a pane' primitive for both cockpit features.

    Focus + zoom the pane, then mark it @OPS_SELECTED. The talking auto-snap
    (feature B) and the manual focus-pane endpoint (feature A) both funnel
    through here, so there is exactly one expand mechanism — not two forks.
    """
    result = await _focus_and_zoom_pane(pane_id)
    await asyncio.to_thread(_set_ops_selected, pane_id)
    return result


async def _snap_focus_to_speaker(item: "TTSQueueItem") -> dict:
    """Snap tmux focus + zoom to the pane that originated this TTS item.

    Fire-and-forget: NEVER raises into the playback path. A snap miss (dead
    pane, remote machine, no pane, voice-chat/Discord backend) silently skips
    while playback proceeds. Resolves the pane via the standard pane-identity
    surface (canonical target -> live %id), not raw %NN hand-rolling.
    """
    try:
        if item is None or not getattr(item, "instance_id", None):
            return {"snapped": False, "reason": "no_instance"}

        # Look up the live instance identity at playback time (handles the
        # "instance died between queue and playback" case naturally).
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT device_id,
                          CASE WHEN interaction_mode = 'voice_chat'
                               THEN 'voice-chat' ELSE notification_mode END AS tts_mode
                   FROM instances WHERE id = ?""",
                (item.instance_id,),
            )
            row = await cursor.fetchone()

        if not row:
            return {"snapped": False, "reason": "instance_gone"}

        device_id = row["device_id"]
        tts_mode = row["tts_mode"]

        # Voice-chat: the operator is conversing by voice, not reading the pane.
        if tts_mode == "voice-chat":
            return {"snapped": False, "reason": "voice_chat"}

        # Local-only snap: only the machine that owns the pane may snap focus.
        local = _local_device_name()
        if not local or device_id != local:
            return {"snapped": False, "reason": "remote_pane", "device_id": device_id}

        # Discord voice backend: audio plays in the VC, not at a tmux pane.
        try:
            routing = resolve_tts_device(instance_id=item.instance_id)
            if routing.get("device") == "discord":
                return {"snapped": False, "reason": "discord_backend"}
        except Exception:
            pass  # routing probe is best-effort; never block the snap on it

        # Live oracle: resolve the originating instance to its current pane.
        # Custodes/cron-originated TTS with no live pane yields (None, _) -> no_pane.
        pane_id, _role = await resolve_instance_pane(item.instance_id)
        if not pane_id:
            return {"snapped": False, "reason": "no_pane"}

        result = await _focus_zoom_and_mark(pane_id)
        logger.info(f"TTS focus snap -> {pane_id} ({result.get('actions')})")
        return {"snapped": True, "pane_id": pane_id, **result}
    except Exception as e:
        logger.warning(f"TTS focus snap failed (non-fatal): {e}")
        return {"snapped": False, "reason": "error", "error": str(e)}


async def select_instance_pane(instance_id: str) -> dict:
    """Manual 'select + expand' of an instance's pane (Ops Cockpit feature A).

    The operator double-clicked a fleet row; reflect that selection in tmux by
    running the SAME focus+zoom+mark primitive the talking auto-snap uses. Local
    machine ownership and a live pane are hard gates (you cannot focus a remote
    or dead pane). Unlike the talking snap, a manual selection deliberately
    bypasses the voice-chat / discord-backend gates — the operator explicitly
    asked for this pane. Never raises; returns {snapped, reason, ...}.
    """
    try:
        if not instance_id:
            return {"snapped": False, "reason": "no_instance"}

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT device_id FROM instances WHERE id = ?",
                (instance_id,),
            )
            row = await cursor.fetchone()

        if not row:
            return {"snapped": False, "reason": "instance_gone"}

        device_id = row["device_id"]

        # Local-only: only the machine that owns the pane can focus it.
        local = _local_device_name()
        if not local or device_id != local:
            return {"snapped": False, "reason": "remote_pane", "device_id": device_id}

        # Live oracle: resolve the instance to its current pane.
        pane_id, _role = await resolve_instance_pane(instance_id)
        if not pane_id:
            return {"snapped": False, "reason": "no_pane"}

        result = await _focus_zoom_and_mark(pane_id)
        logger.info(f"Ops manual focus -> {pane_id} ({result.get('actions')})")
        return {"snapped": True, "reason": None, "pane_id": pane_id, **result}
    except Exception as e:
        logger.warning(f"Ops manual focus failed (non-fatal): {e}")
        return {"snapped": False, "reason": "error", "error": str(e)}


@router.post("/api/instances/{instance_id}/focus-pane")
async def focus_instance_pane(instance_id: str) -> dict:
    """Human-initiated tmux focus for the Ops Cockpit fleet table.

    Selects + zooms + marks the instance's pane (server-resolved by id; raw %pane
    ids never reach the browser). Mirrors the talking auto-snap onto an explicit
    operator action. Returns FocusResult {snapped, reason} (web/ops/src/api.ts).
    """
    return await select_instance_pane(instance_id)


async def tts_queue_worker() -> None:
    """Background worker that processes TTS hot queue sequentially.

    Only drains from hot_queue. Pause queue items must be promoted to hot
    queue via /api/tts/queue/promote or /api/tts/queue/play-pane before
    they will play.
    """
    global tts_current

    while True:
        try:
            # Wait for items in hot queue
            async with tts_queue_lock:
                if hot_queue:
                    tts_current = hot_queue.popleft()
                else:
                    tts_current = None

            if tts_current:
                # Playback focus snap is explicit-action only. Direct hot TTS
                # may fire from background hooks at arbitrary times; do not
                # steal focus for that. Promoting/playing from the pause queue
                # sets focus_on_playback=True because the operator pressed play.
                if tts_current.message and (
                    tts_current.focus_on_playback or TTS_AUTO_FOCUS_ENABLED
                ):
                    await _snap_focus_to_speaker(tts_current)

                # Log TTS starting
                await log_event(
                    "tts_playing",
                    instance_id=tts_current.instance_id,
                    details={
                        "message": tts_current.message[:100],
                        "voice": tts_current.voice,
                        "tab_name": tts_current.tab_name,
                    },
                )

                # Resolve the source pane LIVE (oracle) at playback time and stash
                # it on the item for the speaking/clear @TTS_STATE writes below.
                # (None on a dead/unstamped pane -> _set_tts_state no-ops.)
                tts_current.tmux_pane, _ = await resolve_instance_pane(tts_current.instance_id)

                # Set @TTS_STATE on source pane
                _set_tts_state(tts_current.tmux_pane, "speaking")

                # Play notification sound first (run in executor to not block event loop)
                sound_result = None
                if tts_current.sound:
                    loop = asyncio.get_event_loop()
                    sound_result = await loop.run_in_executor(None, play_sound, tts_current.sound)
                    logger.info(f"TTS worker: sound result = {json.dumps(sound_result)}")
                    if not sound_result.get("success"):
                        logger.warning(f"Sound failed: {sound_result.get('error')}")
                    await asyncio.sleep(0.3)  # Brief pause after sound

                if tts_current.message:
                    # Resolve persona playback settings by WSL voice
                    # (DB tts_voice stores the Windows voice name).
                    wsl_voice = tts_current.voice
                    settings = voice_settings_for_tts_voice(wsl_voice)
                    mac_voice = settings["mac_voice"]
                    wsl_rate = settings["wsl_rate"]

                    # Speak the message (run in executor to allow skip API to interrupt)
                    # Queue items use file-based playback for transport controls (pause/resume/speed)
                    logger.info(
                        f"TTS worker: speaking {len(tts_current.message)} chars with {wsl_voice} (mac={mac_voice}, file_playback=True)"
                    )
                    loop = asyncio.get_event_loop()
                    tts_result = await loop.run_in_executor(
                        None,
                        functools.partial(
                            speak_tts,
                            tts_current.message,
                            mac_voice,
                            0,
                            tts_current.instance_id,
                            wsl_voice,
                            wsl_rate,
                            use_file_playback=True,
                        ),
                    )
                    logger.info(f"TTS worker: speak result = {json.dumps(tts_result)}")

                    # Log completion, skip, or failure
                    if tts_result.get("skipped") or tts_result.get("method") == "skipped":
                        logger.info(f"TTS skipped for {tts_current.instance_id}")
                        await log_event(
                            "tts_skipped",
                            instance_id=tts_current.instance_id,
                            details={
                                "message": tts_current.message[:50],
                                "voice": tts_current.voice,
                            },
                        )
                    elif tts_result.get("success"):
                        await log_event(
                            "tts_completed",
                            instance_id=tts_current.instance_id,
                            details={
                                "message": tts_current.message[:50],
                                "voice": tts_current.voice,
                            },
                        )
                    else:
                        logger.error(
                            f"TTS failed for {tts_current.instance_id}: {tts_result.get('error')}"
                        )
                        await log_event(
                            "tts_failed",
                            instance_id=tts_current.instance_id,
                            details={
                                "message": tts_current.message[:50],
                                "voice": tts_current.voice,
                                "error": tts_result.get("error", "Unknown error"),
                                "sound_result": sound_result,
                            },
                        )
                else:
                    logger.info(f"TTS worker: muted mode, sound only for {tts_current.instance_id}")

                # Clear @TTS_STATE on source pane
                _set_tts_state(tts_current.tmux_pane, "")
                tts_current = None
                await asyncio.sleep(0.5)  # Brief pause between items
            else:
                # No items - wait a bit before checking again
                await asyncio.sleep(0.1)

        except Exception as e:
            logger.error(f"TTS worker error: {e}")
            # Never leave the source pane stuck "speaking" when playback raises
            # after @TTS_STATE was set: clear it before looping (best-effort).
            try:
                if tts_current is not None and getattr(tts_current, "tmux_pane", None):
                    _set_tts_state(tts_current.tmux_pane, "")
            except Exception:
                pass
            tts_current = None
            await asyncio.sleep(1)


# ============ TTS Helpers ============


def _resolve_queue_playback_target(
    *, message: str, sound: str | None, instance_id: str, voice: str | None
) -> dict:
    """Return the real backend a queued item could play through, or a refusal.

    Queue entries are delayed work. Accepting one when every playback backend is
    absent creates the `backend:null` burn-loop: usage piles up, but no audio can
    ever render. Speech uses the same router as live notify. Muted/sound-only
    items require a real local sound backend because `play_sound()` is Mac afplay.
    """
    if message:
        routing = resolve_tts_device(instance_id=instance_id, wsl_voice=voice)
        device = routing.get("device")
        if device:
            return {"success": True, "playback_target": device, "routing": routing}
        return {
            "success": False,
            "reason": "no_playback_target",
            "routing": routing,
            "playback_target": None,
        }

    if sound:
        if _mac_sound_available():
            return {
                "success": True,
                "playback_target": "mac_sound",
                "routing": {"device": "mac_sound", "reason": "local sound backend"},
            }
        return {
            "success": False,
            "reason": "no_sound_playback_target",
            "routing": {"device": None, "reason": "no local sound backend"},
            "playback_target": None,
        }

    return {
        "success": False,
        "reason": "empty_audio_payload",
        "routing": {"device": None, "reason": "empty audio payload"},
        "playback_target": None,
    }


def _is_quiet_hours(now: datetime | None = None) -> bool:
    """Return True when TTS/sound should be suppressed for quiet hours."""
    return bool(get_quiet_hours_status(now).get("active"))


async def queue_tts(instance_id: str, message: str, queue_target: str = "pause") -> dict:
    """Queue a TTS message for an instance, using their profile's voice/sound.

    Args:
        instance_id: The instance ID that triggered TTS.
        message: The text to speak.
        queue_target: "hot" for immediate playback (VC/sync sessions),
                      "pause" for silent accumulation (default).
    """
    message = await _sanitize_public_text_async(message)

    # Silence TTS during quiet hours (11 PM - 9 AM)
    if _is_quiet_hours():
        logger.info(f"TTS suppressed (quiet hours): {message[:80]}")
        return {"success": True, "queued": False, "reason": "quiet_hours"}

    # Silence TTS during meetings (Zoom/Google Meet)
    if DESKTOP_STATE.get("in_meeting"):
        logger.info(f"TTS suppressed (in meeting): {message[:80]}")
        return {"success": True, "queued": False, "reason": "in_meeting"}

    # Look up instance to get their profile
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT i.name AS tab_name, i.tts_voice, i.notification_sound,
                      CASE WHEN i.interaction_mode = 'voice_chat'
                           THEN 'voice-chat' ELSE i.notification_mode END AS tts_mode,
                      p.slug AS persona_slug
               FROM instances i
               LEFT JOIN personas p ON p.id = i.persona_id
               WHERE i.id = ?""",
            (instance_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return {"success": False, "error": f"Instance {instance_id} not found"}

    if row["tts_voice"] is None:
        return {"success": True, "queued": False, "reason": "persona_silent"}

    voice = row["tts_voice"]
    sound = row["notification_sound"]
    tab_name = row["tab_name"] or instance_id

    # Check TTS mode (per-instance and global, most restrictive wins)
    instance_mode = row["tts_mode"] or "verbose"
    # voice-chat forces hot queue — it's an active session
    is_voice_chat = instance_mode == "voice-chat"
    if is_voice_chat:
        instance_mode = "verbose"
        queue_target = "hot"

    # Custodes-sender bypass (Emperor decree, 2026-06-25): TTS originating from
    # the Custodes persona never enqueues to the silent pause queue — it plays
    # immediately. Bypass is a property of the SENDER, not the message: because
    # enforcement TTS only ever originates from Custodes, keying on the sender's
    # persona subsumes "enforcement bypass" and keeps the queue free of any
    # opinion about message type.
    if row["persona_slug"] == CUSTODES_PERSONA_SLUG:
        queue_target = "hot"
    global_mode = TTS_GLOBAL_MODE["mode"]
    # Restrictiveness order: silent > muted > verbose
    mode_rank = {"verbose": 0, "muted": 1, "silent": 2}
    effective_mode = max(instance_mode, global_mode, key=lambda m: mode_rank.get(m, 0))

    if effective_mode == "silent":
        logger.info(f"TTS suppressed (silent mode): {message[:80]}")
        return {"success": True, "queued": False, "reason": "silent"}

    if effective_mode == "muted":
        # Sound only, no TTS speech
        item = TTSQueueItem(
            instance_id=instance_id,
            message="",  # Empty message = no speech
            voice=voice,
            sound=sound,
            tab_name=tab_name,
            queue_target=queue_target,
            focus_on_playback=False,
        )
    else:
        item = TTSQueueItem(
            instance_id=instance_id,
            message=message,
            voice=voice,
            sound=sound,
            tab_name=tab_name,
            queue_target=queue_target,
            focus_on_playback=False,
        )

    target = _resolve_queue_playback_target(
        message=item.message,
        sound=item.sound,
        instance_id=instance_id,
        voice=voice,
    )
    if not target.get("success"):
        reason = target.get("reason") or "no_playback_target"
        await log_event(
            "tts_enqueue_refused",
            instance_id=instance_id,
            device_id="tts_queue",
            details={
                "message": message[:100],
                "voice": voice,
                "queue": queue_target,
                "reason": reason,
                "routing": target.get("routing"),
            },
        )
        return {
            "success": False,
            "queued": False,
            "reason": reason,
            "playback_target": None,
            "routing": target.get("routing"),
        }
    item.playback_target = target.get("playback_target")

    async with tts_queue_lock:
        if queue_target == "hot":
            hot_queue.append(item)
            position = len(hot_queue)
        else:
            pause_queue.append(item)
            position = len(pause_queue)

    if queue_target == "pause":
        await _maybe_emit_tts_languishing_enforcement(position=position, item=item)

    # Log queued event
    await log_event(
        "tts_queued",
        instance_id=instance_id,
        details={
            "message": message[:100],
            "voice": voice,
            "position": position,
            "queue": queue_target,
            "focus_on_playback": item.focus_on_playback,
            "playback_target": item.playback_target,
        },
    )

    # Chime notification for pause queue arrivals so user knows something landed
    if queue_target == "pause":
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, play_sound, "chimes.wav")
        except Exception:
            pass

    return {
        "success": True,
        "queued": True,
        "position": position,
        "queue": queue_target,
        "voice": voice,
        "sound": sound,
        "playback_target": item.playback_target,
    }


def _pause_queue_head_key_locked() -> str | None:
    """Return a stable identity for the current pause-queue head.

    Caller must hold ``tts_queue_lock``.  The key uses the object at the head of
    the queue, not the just-appended item, so dedup follows the stuck episode.
    """
    if not pause_queue:
        return None
    head = pause_queue[0]
    message_hash = hashlib.sha256((head.message or "").encode("utf-8")).hexdigest()[:16]
    return "|".join(
        (
            head.instance_id or "",
            head.tab_name or "",
            head.queued_at.isoformat(),
            message_hash,
        )
    )


async def _sweep_stale_pause_queue_items_for_snapshot() -> list[dict]:
    """Expire stale held pause-queue items during authoritative snapshot reads.

    The sweep is passive: no worker, no polling loop.  It only runs when callers
    ask for the live languishing state, and it follows the existing TTL-cache
    pattern used for TTS backend probes so repeated reads don't churn the deque.
    """
    global _last_pause_queue_expiry_sweep

    now_ts = time.time()
    if (
        _last_pause_queue_expiry_sweep
        and now_ts - _last_pause_queue_expiry_sweep < TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS
    ):
        return []
    _last_pause_queue_expiry_sweep = now_ts

    now = datetime.now()
    expired: list[TTSQueueItem] = []
    async with tts_queue_lock:
        kept: deque[TTSQueueItem] = deque()
        for queued in pause_queue:
            age_seconds = (now - queued.queued_at).total_seconds()
            if age_seconds >= TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS:
                expired.append(queued)
            else:
                kept.append(queued)
        if expired:
            pause_queue.clear()
            pause_queue.extend(kept)

    expired_details = []
    for expired_item in expired:
        age_seconds = max(0, int((now - expired_item.queued_at).total_seconds()))
        detail = {
            "instance_id": expired_item.instance_id,
            "tab_name": expired_item.tab_name,
            "queue": "pause",
            "queued_at": expired_item.queued_at.isoformat(),
            "age_seconds": age_seconds,
            "held_max_age_seconds": TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS,
            "message_hash": hashlib.sha256(
                (expired_item.message or "").encode("utf-8")
            ).hexdigest()[:16],
            "message_length": len(expired_item.message or ""),
            "message_truncated": len(expired_item.message or "") > 300,
        }
        expired_details.append(detail)

    if expired_details:
        _tts_languishing_emit_latch.clear()

    for expired_item, detail in zip(expired, expired_details, strict=False):
        try:
            await log_event(
                "tts_pause_queue_item_expired",
                instance_id=expired_item.instance_id,
                device_id="tts_queue",
                details=detail,
            )
        except Exception:
            logger.warning("Failed to log expired TTS pause item", exc_info=True)

    if expired_details:
        try:
            await log_event(
                "tts_pause_queue_expiry_sweep",
                device_id="tts_queue",
                details={
                    "expired": len(expired_details),
                    "held_max_age_seconds": TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS,
                    "per_item_events_logged": len(expired_details),
                },
            )
        except Exception:
            logger.warning("Failed to log TTS pause expiry sweep", exc_info=True)

    return expired_details


async def get_pause_queue_languishing_snapshot(*, threshold: int | None = None) -> dict:
    """Return the live pause-queue state used for languishing enforcement.

    This is intentionally read under ``tts_queue_lock``. Languishing checks must
    use the current deque state, not a queue position captured when an item was
    appended and later baked into an immutable event payload.
    """
    expired = await _sweep_stale_pause_queue_items_for_snapshot()
    effective_threshold = (
        TTS_LANGUISHING_THRESHOLD
        if threshold is None
        else max(TTS_LANGUISHING_THRESHOLD, threshold)
    )
    async with tts_queue_lock:
        pause_queue_length = len(pause_queue)
        oldest_queued_at = pause_queue[0].queued_at if pause_queue else None
        head_key = _pause_queue_head_key_locked()

    return {
        "pause_queue_length": pause_queue_length,
        "threshold": effective_threshold,
        "oldest_queued_at": oldest_queued_at.isoformat() if oldest_queued_at else None,
        "head_key": head_key,
        "languishing": pause_queue_length > effective_threshold,
        "expired_count": len(expired),
    }


async def _maybe_emit_tts_languishing_enforcement(*, position: int, item: TTSQueueItem) -> None:
    """Record when the manually-played TTS pause queue starts languishing.

    Pause-queue length above the threshold means speech is accumulating but not
    being heard. Re-read the live pause queue at evaluation time so a drained
    queue cannot fire from a stale queue-add snapshot. This is internal /
    diagnostic only: it deliberately declares state, not enforcement, and must
    not page Custodes. The state-event router owns dedupe; this helper stays
    best-effort and never blocks queueing.
    """
    if _custodes_state_event_handler is None:
        return

    snapshot = await get_pause_queue_languishing_snapshot()
    live_pause_queue_length = snapshot["pause_queue_length"]
    if not snapshot["languishing"]:
        _tts_languishing_emit_latch.clear()
        return
    head_key = snapshot.get("head_key")
    if not head_key:
        _tts_languishing_emit_latch.clear()
        return

    previous_head = _tts_languishing_emit_latch.get("head_key")
    previous_depth = int(_tts_languishing_emit_latch.get("max_depth", 0) or 0)
    if previous_head == head_key and live_pause_queue_length <= previous_depth:
        try:
            await log_event(
                "tts_languishing_enforcement_deduped",
                instance_id=item.instance_id,
                device_id="tts_queue",
                details={
                    "reason": "same_head_not_worse",
                    "head_key": head_key,
                    "pause_queue_length": live_pause_queue_length,
                    "previous_depth": previous_depth,
                    "threshold": snapshot["threshold"],
                },
            )
        except Exception:
            pass
        return

    latched_depth = live_pause_queue_length
    _tts_languishing_emit_latch["head_key"] = head_key
    _tts_languishing_emit_latch["max_depth"] = latched_depth

    payload = {
        "app": "tts_queue",
        "queue": "pause",
        "pause_queue_length": live_pause_queue_length,
        "threshold": snapshot["threshold"],
        "head_key": head_key,
        "latest_instance_id": item.instance_id,
        "latest_tab_name": item.tab_name,
        "oldest_queued_at": snapshot["oldest_queued_at"],
    }
    try:
        # No event_class here: pause-queue length/severity/payload are
        # observational signals, not a classification. The state-event policy
        # (custodes_state_policy.classify_trigger) is the single authority that
        # classifies tts_queue_languishing as "enforcement". TTS holds no
        # opinion about enforcement.
        await _custodes_state_event_handler(
            "tts_queue_languishing",
            "tts_queue",
            instance_id=item.instance_id,
            severity=4 if live_pause_queue_length >= 10 else 3,
            payload=payload,
        )
    except Exception as exc:
        if (
            _tts_languishing_emit_latch.get("head_key") == head_key
            and _tts_languishing_emit_latch.get("max_depth") == latched_depth
        ):
            _tts_languishing_emit_latch.clear()
        logger.warning("TTS languishing enforcement emit failed: %s", exc)
        try:
            await log_event(
                "tts_languishing_enforcement_failed",
                instance_id=item.instance_id,
                device_id="tts_queue",
                details={"error": str(exc), **payload},
            )
        except Exception:
            pass


def _queue_item_to_dict(item: TTSQueueItem) -> dict:
    """Serialize a TTSQueueItem for API responses."""
    return {
        "instance_id": item.instance_id,
        "tab_name": item.tab_name,
        "message": item.message[:50] + "..." if len(item.message) > 50 else item.message,
        "voice": item.voice,
        "playback_target": item.playback_target,
        "queue": item.queue_target,
        "queued_at": item.queued_at.isoformat(),
    }


def get_tts_queue_status() -> dict:
    """Get current TTS queue status for dashboard."""
    hot_list = [_queue_item_to_dict(item) for item in hot_queue]
    pause_list = [_queue_item_to_dict(item) for item in pause_queue]

    current = None
    if tts_current:
        current = {
            "instance_id": tts_current.instance_id,
            "tab_name": tts_current.tab_name,
            "message": tts_current.message[:50] + "..."
            if len(tts_current.message) > 50
            else tts_current.message,
            "voice": tts_current.voice,
            "playback_target": tts_current.playback_target,
        }

    return {
        "current": current,
        "hot_queue": hot_list,
        "hot_queue_length": len(hot_list),
        "pause_queue": pause_list,
        "pause_queue_length": len(pause_list),
        # Backward compat: "queue" = combined, "queue_length" = total
        "queue": hot_list + pause_list,
        "queue_length": len(hot_list) + len(pause_list),
        "backend": TTS_BACKEND["current"],
        "satellite_available": TTS_BACKEND["satellite_available"],
        "global_mode": TTS_GLOBAL_MODE["mode"],
        "voice_pool": {
            "total": len(PRIMARY_ASTARTES),
            "fallback_count": len(BACKUP_ASTARTES),
        },
    }


async def skip_tts(clear_queue: bool = False) -> dict:
    """Skip current TTS and optionally clear the queue.

    Routes skip to the correct backend: WSL satellite or local Mac process.

    Args:
        clear_queue: If True, also clear all pending items in the queue.

    Returns:
        Dict with skipped (bool) and cleared (int) counts.
    """
    global tts_current_process, tts_current, tts_skip_requested

    result = {"skipped": False, "cleared": 0, "backend": TTS_BACKEND["current"]}
    current_backend = TTS_BACKEND["current"]

    if current_backend == "wsl":
        # Skip on WSL satellite — try both file playback stop and direct speak skip
        host = DESKTOP_CONFIG["host"]
        port = DESKTOP_CONFIG["port"]
        try:
            # First try stopping file playback (WMP)
            resp = requests.post(
                f"http://{host}:{port}/tts/control", json={"command": "stop"}, timeout=3
            )
            if resp.status_code == 200:
                result["skipped"] = True
                logger.info("TTS skip routed to WSL satellite (file playback stop)")
            else:
                # Fall back to direct speak skip
                resp = requests.post(f"http://{host}:{port}/tts/skip", timeout=3)
                result["skipped"] = resp.status_code == 200
                logger.info(f"TTS skip routed to WSL satellite (direct): {resp.status_code}")
        except Exception as e:
            logger.warning(f"TTS skip to WSL satellite failed (non-fatal): {e}")

    elif current_backend == "mac":
        # Kill local Mac `say` process
        if tts_current_process and tts_current_process.poll() is None:
            tts_skip_requested = True
            try:
                tts_current_process.kill()
                tts_current_process.wait(timeout=1.0)
                result["skipped"] = True
                logger.info("TTS process killed via skip (Mac)")
            except Exception as e:
                logger.warning(f"Error killing TTS process: {e}")
            tts_current_process = None

    # else: nothing playing, no-op

    # Clear both queues if requested
    if clear_queue:
        async with tts_queue_lock:
            cleared = len(hot_queue) + len(pause_queue)
            hot_queue.clear()
            pause_queue.clear()
            result["cleared"] = cleared
            if cleared > 0:
                logger.info(f"Cleared {cleared} items from TTS queues (hot + pause)")

    # Clear @TTS_STATE if we skipped the current item
    if result["skipped"] and tts_current:
        _set_tts_state(tts_current.tmux_pane, "")

    return result


def send_webhook(webhook_url: str, message: str, data: dict = None) -> dict:
    """Send notification via HTTP webhook.

    Sends message as query parameter (for MacroDroid {http_query_string})
    and as JSON body (for structured consumers).
    """
    payload = {
        "type": "notification",
        "message": message,
        "timestamp": datetime.now().isoformat(),
        **(data or {}),
    }

    # Append message as query param so MacroDroid {http_query_string} picks it up
    separator = "&" if "?" in webhook_url else "?"
    url_with_params = f"{webhook_url}{separator}message={quote(message)}"

    try:
        result = subprocess.run(
            [
                "curl",
                "-X",
                "POST",
                "-H",
                "Content-Type: application/json",
                "-d",
                json.dumps(payload),
                "--connect-timeout",
                "5",
                "-s",
                url_with_params,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode == 0:
            return {"success": True, "method": "webhook", "url": webhook_url}
        return {"success": False, "error": f"Webhook failed: {result.stderr}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============ TTS Endpoints ============


@router.get("/api/tts/routing")
async def get_tts_routing():
    """Return the current TTS routing target and reasoning.

    Useful for debugging and TUI display — shows which device would
    receive TTS right now and why.
    """
    routing = resolve_tts_device()
    location_zone = DESKTOP_STATE.get("location_zone")
    in_meeting = DESKTOP_STATE.get("in_meeting", False)
    global_mode = TTS_GLOBAL_MODE.get("mode", "verbose")

    return {
        "routing": routing,
        "context": {
            "location_zone": location_zone,
            "in_meeting": in_meeting,
            "global_mode": global_mode,
            "satellite_available": is_satellite_tts_available(),
            "phone_reachable": is_phone_reachable(),
            "discord_vc_active": routing["device"] == "discord",
        },
    }


@router.post("/api/notify")
async def send_notification(request: NotifyRequest):
    """Authoritative comms entry — the single public notification endpoint.

    Thin wrapper over the in-process `dispatch_notify` router core. Callers
    express intent (message + optional tactile/banner); the router owns
    geofence-first TTS routing, quiet-hours gating, and device fanout. There is
    no caller-picks-a-device knob and no TTS-only sibling endpoint — speech
    always goes through the same routing brain.
    """
    return await dispatch_notify(
        request.message,
        tts=request.tts,
        vibe=request.vibe,
        beep=request.beep,
        banner=request.banner,
        voice=request.voice,
        instance_id=request.instance_id,
    )


@router.post("/api/notify/sound")
async def notify_sound(request: SoundRequest):
    """Play a notification sound only."""
    if _is_quiet_hours():
        logger.info(f"Sound suppressed (quiet hours): {request.sound_file}")
        return {"success": True, "suppressed": True, "reason": "quiet_hours"}

    result = play_sound(request.sound_file)

    await log_event("sound_played", details={"file": request.sound_file, "result": result})

    return result


@router.post("/api/notify/queue")
async def queue_tts_message(request: QueueTTSRequest):
    """Queue a TTS message for an instance. Uses the instance's profile voice/sound.

    Messages are played sequentially - if another TTS is playing, this will queue.
    Returns the queue position.
    """
    return await queue_tts(request.instance_id, request.message, queue_target=request.queue_target)


@router.get("/api/notify/queue/status")
async def get_queue_status():
    """Get current TTS queue status."""
    return get_tts_queue_status()


@router.post("/api/tts/queue/promote")
async def promote_from_pause(request: PromoteRequest):
    """Move item(s) from pause queue to the front of hot queue.

    Body: {} → promotes the next item.
    Body: {"instance_id": "xxx"} → promotes all items from that instance.
    """
    promoted = 0
    async with tts_queue_lock:
        if not pause_queue:
            return {"success": True, "promoted": 0, "reason": "pause_queue_empty"}

        if request.instance_id:
            # Promote all items from this instance, oldest first on playback.
            to_promote = [item for item in pause_queue if item.instance_id == request.instance_id]
            for item in to_promote:
                pause_queue.remove(item)
                item.queue_target = "hot"
                item.focus_on_playback = True
            # `to_promote` is in pause order (oldest→newest). The hot queue drains
            # left→right (popleft, FIFO), so to make the batch play oldest-first at
            # the FRONT of the hot queue we appendleft in REVERSE: the oldest ends
            # up leftmost. appendleft-in-forward-order would reverse the cascade —
            # the Emperor heard it backwards ("draining ... playing in reverse").
            for item in reversed(to_promote):
                hot_queue.appendleft(item)
            promoted += len(to_promote)
        else:
            # Promote the next (oldest) item
            item = pause_queue.popleft()
            item.queue_target = "hot"
            item.focus_on_playback = True
            hot_queue.appendleft(item)
            promoted = 1

    logger.info(f"Promoted {promoted} item(s) from pause to hot queue")
    return {"success": True, "promoted": promoted}


@router.post("/api/tts/queue/play-all")
async def play_all_from_pause():
    """Drain the entire pause queue into the hot queue, preserving FIFO order.

    Operator "Play all" control (e.g. a Stream Deck button): empties the
    accumulated pause buffer so everything plays. Unlike promote/play-pane,
    this is a bulk drain and must NOT yank tmux focus per item, so each item
    keeps focus_on_playback=False.

    Body: {} (empty/ignored).
    """
    promoted = 0
    async with tts_queue_lock:
        while pause_queue:
            item = pause_queue.popleft()
            item.queue_target = "hot"
            item.focus_on_playback = False
            hot_queue.append(item)
            promoted += 1

    logger.info(f"play-all: Drained {promoted} item(s) from pause to hot queue")
    return {"success": True, "promoted": promoted}


@router.post("/api/tts/queue/play-pane")
async def play_pane(request: PlayPaneRequest):
    """Promote all items from a specific instance to the front of hot queue.

    Equivalent to promote with instance_id, provided as a convenience endpoint.
    """
    promoted = 0
    async with tts_queue_lock:
        to_promote = [item for item in pause_queue if item.instance_id == request.instance_id]
        for item in to_promote:
            pause_queue.remove(item)
            item.queue_target = "hot"
            item.focus_on_playback = True
        # Preserve chronological playback: oldest first. See promote_from_pause —
        # appendleft in reverse so the batch drains oldest→newest, not reversed.
        for item in reversed(to_promote):
            hot_queue.appendleft(item)
        promoted += len(to_promote)

    logger.info(f"play-pane: Promoted {promoted} item(s) for {request.instance_id} to hot queue")
    return {"success": True, "promoted": promoted, "instance_id": request.instance_id}


@router.post("/api/tts/skip")
async def api_tts_skip(clear_queue: bool = False):
    """Skip current TTS playback and optionally clear the queue.

    Args:
        clear_queue: Query param - if true, also clears all pending items.

    Returns:
        Dict with 'skipped' (bool) and 'cleared' (int count).
    """
    result = await skip_tts(clear_queue)
    await log_event("tts_skipped", details=result)
    return result


@router.post("/api/tts/global-mode")
async def set_global_tts_mode(request: Request):
    """Set global TTS mode. Overrides all instances."""
    body = await request.json()
    mode = body.get("mode", "verbose")
    # One-button operator mute toggle (e.g. a Stream Deck button): resolve
    # "toggle" to the opposite of the current global mode before validation.
    if mode == "toggle":
        mode = "muted" if TTS_GLOBAL_MODE["mode"] == "verbose" else "verbose"
    if mode not in ("verbose", "muted", "silent"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    old_mode = TTS_GLOBAL_MODE["mode"]
    TTS_GLOBAL_MODE["mode"] = mode

    # Update only the global override field on active instances. Do not
    # mutate per-instance persona voice/sound or interaction state here.
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM instances WHERE status NOT IN ('stopped', 'archived') AND is_subagent = 0"
        )
        rows = await cursor.fetchall()
        for row in rows:
            await sanctioned_update_instance(
                db,
                instance_id=row[0],
                updates={"notification_mode": mode},
                mutation_type="instance_updated",
                write_source="api",
                actor="tts-global-mode",
            )
        await db.commit()

    await log_event("tts_global_mode_changed", details={"mode": mode, "old_mode": old_mode})
    return {"status": "ok", "mode": mode, "old_mode": old_mode}


@router.get("/api/notify/test")
async def test_notification():
    """Test the notification system with a simple message."""
    sound_result = play_sound()
    tts_result = speak_tts("Token API notification test")

    return {"sound": sound_result, "tts": tts_result, "message": "Test notification sent"}
