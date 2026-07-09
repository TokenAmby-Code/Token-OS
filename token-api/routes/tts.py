"""
TTS/Notification route module — extracted from main.py.

Owns:
- TTS speech functions (Token-OS control plane + phone/WSL/Linux execution backends)
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
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote

import aiosqlite
import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import shared
from db_connections import connect_agents_db
from human_render import sanitize_human_render_text, sanitize_human_render_text_sync
from instance_mutation import update_instance
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


def _require_privileged_tts_probe_request(request: Request | None) -> None:
    """Allow the phone TTS probe only from local/operator-controlled callers.

    Unit tests call the route function directly with ``request=None``. Live use is
    via localhost after deploy; an optional bearer token can authorize non-local
    operator tooling without making the debug playback endpoint public.
    """
    if request is None:
        return
    host = request.client.host if request.client else ""
    if host in {"127.0.0.1", "::1", "localhost"}:
        return
    configured = os.environ.get("TOKEN_API_PRIVILEGED_ROUTE_TOKEN", "").strip()
    auth = request.headers.get("Authorization", "")
    presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if configured and presented and hmac.compare_digest(presented, configured):
        return
    raise HTTPException(status_code=403, detail="privileged route required")


def _phone_test_probe_conflicts_with_active_tts(state: dict) -> bool:
    control = dict(state.get("control") or {})
    if control.get("state") != "playing":
        return False
    if control.get("source") == "phone_test":
        return False
    return bool(state.get("current") or state.get("playback_id"))


router = APIRouter()


# ============ Late-bound Dependencies ============
# Functions from other main.py sections that haven't been extracted yet.
# Set by init_deps() called from main.py after import.

_send_to_phone = None
_custodes_state_event_handler = None
_audio_proxy_health_checker = None
TTS_LANGUISHING_THRESHOLD = 5
try:
    PHONE_AUDIO_PROXY_HEARTBEAT_TTL_SECONDS = max(
        1, int(os.environ.get("TOKEN_API_PHONE_AUDIO_PROXY_HEARTBEAT_TTL_SECONDS", "120"))
    )
except ValueError:
    PHONE_AUDIO_PROXY_HEARTBEAT_TTL_SECONDS = 120
# Which senders bypass the pause queue is now data, not a hardcoded slug: it is
# carried by ``personas.advisor`` and resolved by JOIN at queue time. See queue_tts.


def init_deps(
    *, send_to_phone=None, custodes_state_event_handler=None, audio_proxy_health_checker=None
) -> None:
    """Receive dependencies from main.py to avoid circular imports.

    Called once during app startup, before any requests are served.
    """
    global _send_to_phone, _custodes_state_event_handler, _audio_proxy_health_checker
    if send_to_phone is not None:
        _send_to_phone = send_to_phone
    if custodes_state_event_handler is not None:
        _custodes_state_event_handler = custodes_state_event_handler
    if audio_proxy_health_checker is not None:
        _audio_proxy_health_checker = audio_proxy_health_checker


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
    context: dict | None = None  # optional contract metadata, e.g. {"kind":"enforcement"}
    enforcement: bool = False  # explicit live-enforcement audio: bypass persona_silent or fail loud


class SoundRequest(BaseModel):
    sound_file: str | None = None  # Path to sound file


class QueueTTSRequest(BaseModel):
    instance_id: str
    message: str
    queue_target: str = "pause"  # "hot" or "pause"


class PlayPaneRequest(BaseModel):
    instance_id: str  # Promote all items from this instance to hot queue


class PlaybackCompleteRequest(BaseModel):
    playback_id: str  # echoed back by the phone when it finishes speaking a line


class TTSControlRequest(BaseModel):
    """Authoritative TTS control request.

    Overlay/backend controls must enter Token-OS here first. Token-OS records
    the control state, then echoes to the active execution backend.
    """

    command: str | None = None  # Track B phone field: pause, resume, skip, speed
    action: str | None = None  # Back-compat alias accepted during rollout
    speed: float | None = None
    playback_id: str | None = None
    session_id: str | None = None
    backend: str | None = None
    source: str | None = None


class TTSBackendAckRequest(BaseModel):
    """Backend acknowledgement for a dispatched chunk."""

    session_id: str | None = None
    playback_id: str
    chunk_id: str | None = None
    backend: str
    status: str = "ack"
    detail: dict | None = None


class TTSBackendErrorRequest(BaseModel):
    """Backend error report for a dispatched chunk/current playback."""

    session_id: str | None = None
    playback_id: str | None = None
    chunk_id: str | None = None
    backend: str
    error: str
    retryable: bool = False
    detail: dict | None = None


class TTSChunkEventRequest(BaseModel):
    """Phone/backend chunk lifecycle event."""

    event: str
    session_id: str | None = None
    playback_id: str | None = None
    chunk_id: str | None = None
    backend: str = "phone"
    current_index: int | None = None
    next_index: int | None = None
    detail: dict | None = None


class TTSChunkNextRequest(BaseModel):
    """Phone request for the next one-chunk backfill."""

    session_id: str
    playback_id: str
    utterance_id: str | None = None
    backend: str = "phone"
    last_consumed_index: int
    detail: dict | None = None


class TTSPhoneTestRequest(BaseModel):
    """Explicit phone-stream test request.

    This bypasses normal router device selection so WSL availability does not
    affect phone MacroDroid validation.
    """

    message: str
    rate: int = 0
    max_chars: int | None = None


# ============ TTS/Notification System ============

# Platform detection
IS_MACOS = sys.platform == "darwin"
DEFAULT_SOUND = "chimes.wav"
TTS_EXECUTION_BACKENDS = {"phone", "wsl", "linux"}


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


@dataclass(frozen=True)
class TTSSanitizationRule:
    """A deterministic, ordered token transform for spoken TTS text."""

    name: str
    transform_token: Callable[[str], str]


_TTS_TOKEN_RE = re.compile(r"\S+|\s+")
_TTS_TOKEN_EDGE_RE = re.compile(
    r"^(?P<prefix>[\"'“‘([{<]*)(?P<body>.*?)(?P<suffix>[\"'”’)\]}>.,!;:?]*)$"
)
_TTS_SEPARATOR_RE = re.compile(r"(?<=\w)[-_](?=\w)")
_TTS_SHA_RE = re.compile(r"(?=.*\d)[0-9a-f]{7,40}\Z")
_TTS_EXTENSION_RE = re.compile(r"(?<!^)\.[^.\s/]+$")


def _sanitize_tts_path_token(token: str) -> str:
    """Speak path-like tokens as the final segment only, without extension."""
    if "/" not in token:
        return token

    segments = [segment for segment in token.split("/") if segment]
    if len(segments) < 2:
        return token

    final_segment = segments[-1]
    return _TTS_EXTENSION_RE.sub("", final_segment) or final_segment


def _collapse_tts_separators(token: str) -> str:
    """Collapse word-internal kebab/snake separators into spoken spaces."""
    return _TTS_SEPARATOR_RE.sub(" ", token)


def _elide_tts_sha_token(token: str) -> str:
    """Replace obvious git commit hashes with a short spoken label."""
    if _TTS_SHA_RE.fullmatch(token):
        return "commit"
    return token


TTS_SANITIZATION_RULES: tuple[TTSSanitizationRule, ...] = (
    TTSSanitizationRule("paths", _sanitize_tts_path_token),
    TTSSanitizationRule("separator_collapse", _collapse_tts_separators),
    TTSSanitizationRule("sha_commit_elision", _elide_tts_sha_token),
)


def _sanitize_tts_token(token: str) -> str:
    edge = _TTS_TOKEN_EDGE_RE.fullmatch(token)
    if not edge:
        body = token
        prefix = suffix = ""
    else:
        prefix = edge.group("prefix")
        body = edge.group("body")
        suffix = edge.group("suffix")

    for rule in TTS_SANITIZATION_RULES:
        body = rule.transform_token(body)
    return f"{prefix}{body}{suffix}"


def sanitize_tts_for_speech(text: str | None) -> str:
    """Apply deterministic speech-only sanitizers before backend fan-out.

    This is intentionally token-local and ordered so URL/UUID/number rules can
    be added to ``TTS_SANITIZATION_RULES`` without changing the TTS router.
    """
    if not text:
        return ""
    return "".join(
        part if part.isspace() else _sanitize_tts_token(part)
        for part in _TTS_TOKEN_RE.findall(str(text))
    )


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
    """Mac is no longer a TTS execution backend."""
    return False


def _mac_sound_available() -> bool:
    """True only when a local `afplay` backend can plausibly render a sound."""
    return IS_MACOS and shutil.which("afplay") is not None


def _parse_audio_proxy_heartbeat(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def get_phone_audio_proxy_health() -> dict:
    """Return receiver-level phone audio health.

    The old router treated MacroDroid HTTP reachability as "phone TTS is live".
    That is a false-positive for the current phone audio path: speech is only
    audible when the phone audio-proxy receiver is connected, has a concrete
    receiver PID, and has heartbeated recently. Fail closed when the checker is
    not wired or any receiver-level signal is missing.
    """
    if _audio_proxy_health_checker is None:
        return {
            "available": False,
            "reason": "audio_proxy_health_checker_unwired",
            "phone_connected": False,
            "receiver_pid": None,
            "receiver_running": False,
            "last_heartbeat": None,
            "heartbeat_age_seconds": None,
        }

    try:
        health = dict(_audio_proxy_health_checker() or {})
    except Exception as exc:
        return {
            "available": False,
            "reason": "audio_proxy_health_check_failed",
            "error": str(exc),
            "phone_connected": False,
            "receiver_pid": None,
            "receiver_running": False,
            "last_heartbeat": None,
            "heartbeat_age_seconds": None,
        }

    phone_connected = bool(health.get("phone_connected"))
    receiver_pid = health.get("receiver_pid")
    receiver_running = bool(health.get("receiver_running"))
    last_heartbeat_raw = health.get("last_heartbeat")
    last_heartbeat = _parse_audio_proxy_heartbeat(last_heartbeat_raw)
    heartbeat_age_seconds = None
    heartbeat_fresh = False
    if last_heartbeat is not None:
        heartbeat_age_seconds = max(0, int((datetime.now() - last_heartbeat).total_seconds()))
        heartbeat_fresh = heartbeat_age_seconds <= PHONE_AUDIO_PROXY_HEARTBEAT_TTL_SECONDS

    reason = None
    if not phone_connected:
        reason = "audio_proxy_phone_disconnected"
    elif not receiver_pid:
        reason = "audio_proxy_receiver_pid_missing"
    elif not receiver_running:
        reason = "audio_proxy_receiver_not_running"
    elif last_heartbeat is None:
        reason = "audio_proxy_heartbeat_missing"
    elif not heartbeat_fresh:
        reason = "audio_proxy_heartbeat_stale"

    health.update(
        {
            "available": reason is None,
            "reason": reason,
            "phone_connected": phone_connected,
            "receiver_pid": receiver_pid,
            "receiver_running": receiver_running,
            "last_heartbeat": last_heartbeat_raw,
            "heartbeat_age_seconds": heartbeat_age_seconds,
            "heartbeat_ttl_seconds": PHONE_AUDIO_PROXY_HEARTBEAT_TTL_SECONDS,
        }
    )
    return health


def _phone_tts_available() -> bool:
    """Phone TTS is reachable when the MacroDroid HTTP server is up.

    The phone chunk transport speaks locally on the device and proves delivery
    via playback-complete/chunk events (watchdog-capped), so availability gates
    on coarse MacroDroid reachability — NOT the audio-proxy receiver heartbeat
    (#423), which governs a different transport and is surfaced only as a
    diagnostic.
    """
    return _send_to_phone is not None and is_phone_reachable()


def _phone_tts_unavailable_reason() -> str:
    if _send_to_phone is None:
        return "phone_transport_unavailable"
    return "phone_macrodroid_unreachable"


def _no_playback_backend(reason: str = "no_playback_backend") -> dict:
    return {
        "success": False,
        "error": reason,
        "reason": reason,
        "method": None,
        "route": None,
    }


def speak_tts_mac(message: str, voice: str = None, rate: int = 0) -> dict:
    """Retired compatibility shim.

    Mac was removed from the TTS execution backend set on 2026-07-03. This
    function intentionally does not call macOS ``say``; callers must use phone,
    WSL, or later Linux through the authoritative control plane.
    """
    return _no_playback_backend("mac_tts_backend_removed")


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
    biased_rate = max(-10, min(10, rate + TTS_WSL_RATE_BIAS))
    payload = {"message": message, "voice": voice, "rate": biased_rate}

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
    wpm = _wpm_for_rate(rate)

    try:
        resp = requests.post(
            f"{DISCORD_DAEMON_URL}/voice/tts",
            json={"message": message, "bot": bot_name, "voice": mac_voice, "rate": wpm},
            # Long lines legitimately block on the daemon's AudioPlayerStatus.Idle
            # (now serialized per-bot), so allow generous headroom over a single
            # spoken line before declaring a transport timeout.
            timeout=TTS_DISCORD_HTTP_TIMEOUT_S,
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

    Authoritative execution backends are WSL, phone, and later Linux. Mac is
    removed as a TTS backend: routing never returns ``mac`` and failures never
    demote to macOS ``say``. WSL is the desk/default backend again.

    Priority cascade:
    1. Discord voice — if the operator is actively in a voice channel, audio goes
       there (a deliberate live conversation surface, not ambient routing).
    2. WSL — first-class desk/default backend when satellite TTS is available.
    3. Phone — fallback execution backend if WSL is unavailable but phone is reachable.

    Returns:
        {"device": "discord"|"wsl"|"phone"|None, "reason": str, "discord_bot": str|None}
    """
    # 1. Discord voice channel — operator in VC means audio goes there.
    discord_bot = _get_discord_voice_bot()
    if discord_bot:
        return {
            "device": "discord",
            "reason": "operator in voice channel",
            "discord_bot": discord_bot,
        }

    # Phone routeability gates on coarse MacroDroid reachability. Audio-proxy
    # receiver health remains diagnostic, not a route predicate for /tts-chunk.
    # The canonical playback chain is Discord → WSL → phone; geofence/away state
    # no longer lets a reachable phone preempt a healthy WSL satellite.
    phone_health = get_phone_audio_proxy_health()
    phone_reachable = _send_to_phone is not None and is_phone_reachable()

    if is_satellite_tts_available():
        return {
            "device": "wsl",
            "reason": "wsl satellite available",
            "discord_bot": None,
            "phone_audio_proxy": phone_health,
        }

    if phone_reachable:
        return {
            "device": "phone",
            "reason": "wsl unavailable; phone MacroDroid reachable",
            "discord_bot": None,
            "phone_audio_proxy": phone_health,
        }

    return {
        "device": None,
        "reason": "no execution backend available",
        "discord_bot": None,
        "phone_audio_proxy": phone_health,
    }


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

    Dispatches to Discord voice or the selected execution backend
    (phone/WSL/later Linux). WSL args remain accepted for caller compatibility
    and are used by the WSL chunk dispatcher. It does not fall through to Mac
    on failed live delivery, and never reports success without a concrete
    playback method.

    Args:
        message: Text to speak
        voice: compatibility voice hint for Discord/WSL mapping
        rate: Compatibility rate hint
        instance_id: Optional instance ID for logging
        wsl_voice: Deprecated Windows SAPI voice name (ignored for routing)
        wsl_rate: Deprecated WSL TTS rate (ignored for routing)
        use_file_playback: Deprecated WSL playback flag (ignored for routing)
    """
    if not message:
        return {"success": False, "error": "No message provided"}

    # Clean markdown/public text, then apply speech-only sanitization once at
    # the single backend fan-out chokepoint.
    message = sanitize_tts_for_speech(_sanitize_public_text(clean_markdown_for_tts(message)))

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

    if device is None:
        result = _no_playback_backend("no_playback_backend")
        result["route_reason"] = routing.get("reason")
        return _finish(result)

    chunks = build_tts_chunk_handoff(message)

    # Dispatch without Mac fallthrough. The selected active backend either plays
    # or Token-OS records/returns an error state.
    if device == "discord":
        result = speak_tts_discord(message, routing["discord_bot"], voice, rate)
        if result.get("success"):
            return _finish(result)
        logger.info("TTS: Discord failed (%s); no Mac fallback", result.get("error"))
        return _finish(result)

    if device in TTS_EXECUTION_BACKENDS:
        result = dispatch_tts_chunks_to_backend(
            device,
            chunks,
            voice=voice,
            rate=rate,
            wsl_voice=wsl_voice,
            wsl_rate=wsl_rate if wsl_rate is not None else rate,
        )
        if not result.get("success"):
            logger.info("TTS: %s backend failed (%s); no Mac fallback", device, result.get("error"))
        return _finish(result)

    return _finish(_no_playback_backend("unknown_playback_backend"))


def send_discord_notification(message: str, level: str = "info") -> dict:
    """The one daemon HTTP hop for the Discord notification leg.

    POSTs to the daemon's /notify endpoint, which routes to the notifications
    channel. Callers never talk to the daemon directly — the router owns the
    transport (same contract as _send_to_phone for the tactile leg).
    """
    resp = requests.post(
        f"{DISCORD_DAEMON_URL}/notify",
        json={"message": message, "level": level},
        timeout=5,
    )
    resp.raise_for_status()
    return {"success": True, **resp.json()}


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
    discord: bool = False,
    enforcement: bool = False,
) -> dict:
    """Authoritative comms entry — the single front door to the router.

    Feature code calls this in-process to notify the Emperor. One call carries
    the whole intent (message + optional tactile/banner); the router owns:
      * authoritative TTS routing (Discord VC → situational phone/WSL/later Linux)
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

    context = context or {}
    enforcement = bool(
        enforcement
        or context.get("enforcement")
        or context.get("kind") == "enforcement"
        or context.get("type") == "enforcement"
    )

    if _is_quiet_hours() and not enforcement:
        logger.info(f"Notify suppressed (quiet hours): {(message or banner or '')[:80]}")
        return {
            "delivered": False,
            "suppressed": True,
            "reason": "quiet_hours",
            "route": "suppressed",
        }

    loop = asyncio.get_event_loop()

    # Audio leg: route through the SINGLE gated queue (the one speak_tts call site
    # is the worker). dispatch_notify NEVER speaks directly — it enqueues and
    # awaits a completion future the worker resolves when playback truly finishes.
    # The old fire-and-forget direct speak_tts here was the second playback engine
    # behind the drain bug and the "cockpit idle while a voice plays" report. A
    # notify with no named instance rides the synthetic ``system`` sender
    # (Custodes-voiced, hot): per the Emperor's two-part contract these terse pings
    # ARE "the Custodes are impending," and a recognized synthetic id keeps the
    # single-queue invariant without a parallel speak path. queue_tts owns voice +
    # the deny-by-default persona gate, so the inline voice lookup is gone.
    audio_requested = bool(tts and message)
    tts_result = None
    if audio_requested:
        completion: asyncio.Future = loop.create_future()
        audio_instance_id = instance_id or SYSTEM_INSTANCE_ID
        enqueue = await queue_tts(
            audio_instance_id,
            message,
            queue_target="hot",
            completion=completion,
            bypass_persona_silent=enforcement,
        )
        if enqueue.get("queued"):
            try:
                outcome = await asyncio.wait_for(
                    completion, timeout=NOTIFY_AUDIO_COMPLETION_TIMEOUT_S
                )
            except TimeoutError:
                logger.warning(
                    "notify: audio completion timed out (instance=%s, %ss); the "
                    "line may still finish out-of-band",
                    audio_instance_id,
                    NOTIFY_AUDIO_COMPLETION_TIMEOUT_S,
                )
                tts_result = {
                    "success": False,
                    "route": None,
                    "audio_delivered": False,
                    "reason": "audio_completion_timeout",
                }
            else:
                tts_result = {
                    "success": bool(outcome.get("success")),
                    "route": outcome.get("route"),
                    "audio_delivered": bool(outcome.get("audio_delivered")),
                    "reason": outcome.get("reason"),
                    "error": outcome.get("error"),
                }
        else:
            # Not queued = truthful non-delivery (persona_silent / no backend /
            # in_meeting / instance_not_found). NEVER a silent direct-speak.
            tts_result = {
                "success": False,
                "route": None,
                "audio_delivered": False,
                "reason": enqueue.get("reason"),
            }

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

    # Discord leg: device-agnostic notification channel via the daemon. Sits
    # AFTER the quiet-hours early-return (suppressed notifies never reach the
    # daemon) and is try/except-isolated so a dead daemon can never mask the
    # TTS/tactile legs.
    discord_result = None
    if discord and message:
        try:
            discord_result = await loop.run_in_executor(
                None, functools.partial(send_discord_notification, message)
            )
        except Exception as e:
            logger.warning(f"notify: discord leg failed (isolated): {e}")
            discord_result = {"success": False, "error": str(e)}

    tactile_delivered = bool(
        tactile_result and (tactile_result.get("success") or tactile_result.get("overall_success"))
    )
    audio_delivered = bool(tts_result and tts_result.get("audio_delivered"))
    # If the caller requested spoken audio, top-level delivery means true audio
    # playback. A phone banner/vibe must not mask a failed/no-backend TTS leg.
    delivered = audio_delivered if audio_requested else tactile_delivered
    route = tts_result.get("route") if tts_result else None
    reason = None
    if audio_requested and not audio_delivered:
        reason = (
            (tts_result or {}).get("reason")
            or (tts_result or {}).get("error")
            or "audio_not_delivered"
        )
        if enforcement and reason == "persona_silent":
            reason = "enforcement_persona_silent_contract_violation"
    result = {
        "delivered": delivered,
        "audio_delivered": audio_delivered,
        "route": route,
        "reason": reason,
        "tts": tts_result,
        "tactile": tactile_result,
        "discord": discord_result,
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
            "discord_delivered": bool(discord_result and discord_result.get("success")),
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
    name: str = ""
    tab_name: str | None = None  # compatibility alias for older queue tests/callers
    queue_target: str = "pause"  # "hot" or "pause"
    queued_at: datetime = field(default_factory=datetime.now)
    status: str = "queued"  # queued, playing, completed
    tmux_pane: str | None = None  # live-resolved pane id for @TTS_STATE tracking (set at playback)
    focus_on_playback: bool = False  # true only for explicit operator-initiated playback
    playback_target: str | None = None  # resolved non-null audio target at enqueue time
    persona_slug: str | None = None
    persona_display_name: str | None = None
    commander_type: str | None = None
    started_at: str | None = None  # ISO; stamped when the worker begins audible playback
    # Future resolved by the worker when this item reaches a terminal playback
    # state (success/skip/fail/muted/exception). The single front door
    # (dispatch_notify) awaits it for truthful delivery; agent-TTS enqueues leave
    # it None. Excluded from equality/repr so a pending Future never trips them.
    completion: "asyncio.Future | None" = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.name and self.tab_name:
            self.name = self.tab_name


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


def _resolve_completion(
    item: "TTSQueueItem",
    *,
    success: bool,
    route: str | None,
    audio_delivered: bool,
    reason: str | None = None,
    error: str | None = None,
) -> None:
    """Resolve a queue item's completion future, if one is attached and pending.

    The worker calls this from EVERY terminal branch (success / skip / failure /
    muted / exception) so the single front door (``dispatch_notify``) that awaits
    the future always unblocks with a truthful ``{success, route, audio_delivered}``
    outcome. No-ops when there is no future (agent-TTS enqueues) or it is already
    done/cancelled (e.g. the awaiter's ``wait_for`` timed out and cancelled it).
    Must run on the event-loop thread — the worker is a coroutine, so it is.
    """
    completion = getattr(item, "completion", None)
    if completion is None or completion.done():
        return
    try:
        result = {"success": success, "route": route, "audio_delivered": audio_delivered}
        if reason:
            result["reason"] = reason
        if error:
            result["error"] = error
        completion.set_result(result)
    except asyncio.InvalidStateError:  # raced to done between the guard and set
        pass


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


TTS_CHUNK_MAX_CHARS = _positive_int_env("TOKEN_API_TTS_CHUNK_MAX_CHARS", 360, minimum=80)


# Passive held-message drain policy.  Pause queue entries older than this are no
# longer actionable as speech; they are deliberately expired and logged when the
# authoritative languishing snapshot is read.  This is a source drain, not an
# alert debounce.
TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS = _positive_int_env(
    "TOKEN_API_TTS_PAUSE_HELD_MAX_AGE_SECONDS", 3600
)
TTS_PAUSE_QUEUE_SWEEP_TTL_SECONDS = _positive_int_env("TOKEN_API_TTS_PAUSE_SWEEP_TTL_SECONDS", 30)
_last_pause_queue_expiry_sweep = 0.0

# Synthetic, always-resolved sender for system/enforcement pings that carry no
# live persona instance (e.g. "distraction logged", "break exhausted", an
# AskUserQuestion with no wired instance). Voiced as Custodes (Microsoft George,
# hot/system policy): per the Emperor's two-part contract these terse pings ARE "the
# Custodes are impending." It is the ONE recognized synthetic id — it holds the
# single-queue invariant (still enqueues to the same worker, no parallel speak
# path) and can never fall to ``persona_unresolved``. This supersedes the earlier
# "fail loud / go silent on missing instance_id" plan: system notifies must still
# be heard, just through the gate like everything else.
SYSTEM_INSTANCE_ID = "system"
_SYSTEM_TTS_ROW: dict[str, object] = {
    "name": "System",
    "tts_voice": "Microsoft George",  # Custodes' reserved voice
    "notification_sound": "chimes.wav",
    "tts_mode": "verbose",
    "persona_slug": "system",
    "persona_display_name": "System",
    "commander_type": "system",
    "advisor": True,  # jump ahead of the pause queue, but the worker still
    # serializes — a system ping waits out whatever is currently audible.
    "tts_policy": "hot",
}

# Phone playback completion (real audio-finish, not accept). The device echoes
# the ``playback_id`` to POST /api/tts/playback-complete when it finishes
# speaking; ``_send_phone_tts`` (in a worker thread) blocks on the Event until
# then, or until this watchdog cap fires. The watchdog is a named safety cap for
# a *missed* callback (logged, never swallowed — [[no-suppress-debounce]]), not a
# duration estimate. Tune generously; phone TTS lines are short.
PHONE_PLAYBACK_WATCHDOG_S = _positive_int_env("TOKEN_API_PHONE_PLAYBACK_WATCHDOG_S", 30)
pending_phone_playbacks: dict[str, "threading.Event"] = {}
_tts_phone_streams_lock = threading.RLock()
TTS_PHONE_STREAMS: dict[tuple[str, str], dict[str, object]] = {}
_tts_authoritative_state_lock = threading.RLock()
TTS_AUTHORITATIVE_STATE: dict[str, object] = {
    "session_id": uuid.uuid4().hex,
    "queue": {"hot": 0, "pause": 0},
    "current": None,
    "next": None,
    "playback_id": None,
    "backend": None,
    "control": {
        "state": "idle",
        "last_action": None,
        "speed": 1.0,
        "source": None,
        "updated_at": None,
    },
    "last_backend_ack": None,
    "last_error": None,
    "updated_at": None,
}


def _now_iso() -> str:
    return datetime.now().isoformat()


def get_tts_authoritative_state() -> dict:
    """Return a snapshot of Token-OS authoritative TTS state."""
    with _tts_authoritative_state_lock:
        return json.loads(json.dumps(TTS_AUTHORITATIVE_STATE))


def _update_tts_authoritative_state(**updates) -> dict:
    with _tts_authoritative_state_lock:
        TTS_AUTHORITATIVE_STATE.update(updates)
        TTS_AUTHORITATIVE_STATE["queue"] = {"hot": len(hot_queue), "pause": len(pause_queue)}
        TTS_AUTHORITATIVE_STATE["updated_at"] = _now_iso()
        return get_tts_authoritative_state()


def _record_tts_backend_active(
    backend: str | None,
    *,
    playback_id: str | None = None,
    current: dict | None = None,
    next_chunk: dict | None = None,
) -> dict:
    """Record the active execution backend/chunk before dispatching to it."""
    if backend not in TTS_EXECUTION_BACKENDS:
        backend = None
    updates = {
        "backend": backend,
        "playback_id": playback_id,
        "current": current,
        "next": next_chunk,
        "last_error": None,
    }
    if backend and current:
        control = dict(TTS_AUTHORITATIVE_STATE.get("control") or {})
        if control.get("state") != "paused":
            control["state"] = "playing"
            control["updated_at"] = _now_iso()
            updates["control"] = control
    return _update_tts_authoritative_state(**updates)


def _record_tts_control_state(request: TTSControlRequest, backend: str | None) -> dict:
    action = (request.command or request.action or "").strip().lower()
    with _tts_authoritative_state_lock:
        control = dict(TTS_AUTHORITATIVE_STATE.get("control") or {})
        control["last_action"] = action
        control["source"] = request.source
        control["updated_at"] = _now_iso()
        if action == "pause":
            control["state"] = "paused"
        elif action == "resume":
            control["state"] = "playing"
        elif action == "skip":
            control["state"] = "skipping"
        elif action == "speed":
            control["state"] = "playing"
            control["speed"] = request.speed
        TTS_AUTHORITATIVE_STATE["control"] = control
        if backend in TTS_EXECUTION_BACKENDS:
            TTS_AUTHORITATIVE_STATE["backend"] = backend
        if request.playback_id:
            TTS_AUTHORITATIVE_STATE["playback_id"] = request.playback_id
        TTS_AUTHORITATIVE_STATE["queue"] = {"hot": len(hot_queue), "pause": len(pause_queue)}
        TTS_AUTHORITATIVE_STATE["updated_at"] = _now_iso()
        return get_tts_authoritative_state()


_TTS_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def build_tts_chunk_handoff(text: str, *, max_chars: int | None = None) -> list[dict]:
    """Build the backend chunk contract from already-sanitized speech text.

    Each chunk is a dict with ``playback_id``, ``chunk_id``, ``index``, ``total``,
    ``text``, and ``text_hash``. Dispatch payloads include exactly the current
    chunk plus one optional ``next`` chunk; backends must not run further ahead.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    max_chars = max_chars or TTS_CHUNK_MAX_CHARS
    parts: list[str] = []
    for sentence in _TTS_SENTENCE_SPLIT_RE.split(cleaned):
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) <= max_chars:
            parts.append(sentence)
            continue
        words = sentence.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if current and len(candidate) > max_chars:
                parts.append(current)
                current = word
            else:
                current = candidate
        if current:
            parts.append(current)

    utterance_id = uuid.uuid4().hex
    total = len(parts)
    chunks = []
    for index, chunk_text in enumerate(parts):
        playback_id = f"{utterance_id}-{index + 1}"
        chunk_id = f"{utterance_id}:{index + 1}/{total}"
        chunks.append(
            {
                "playback_id": playback_id,
                "utterance_id": utterance_id,
                "chunk_id": chunk_id,
                "index": index,
                "total": total,
                "text": chunk_text,
                "text_hash": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
            }
        )
    return chunks


def _chunk_public_payload(chunk: dict | None) -> dict | None:
    if not chunk:
        return None
    return {
        "playback_id": chunk.get("playback_id"),
        "chunk_id": chunk.get("chunk_id"),
        "index": chunk.get("index"),
        "total": chunk.get("total"),
        "text": chunk.get("text"),
        "text_hash": chunk.get("text_hash"),
    }


def _split_phone_utterance_text(text: str, *, max_chars: int | None = None) -> tuple[str, str]:
    """Split a phone utterance into MacroDroid's v1 current/next buffer.

    Phone v1 executes exactly one HTTP handoff containing ``current_chunk`` and
    ``next_chunk``. Short lines use an empty next chunk; long lines split near
    the midpoint on whitespace so the phone never has to reconstruct a queue.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return "", ""
    max_chars = max_chars or TTS_CHUNK_MAX_CHARS
    if len(cleaned) <= max_chars:
        return cleaned, ""

    midpoint = len(cleaned) // 2
    left = cleaned.rfind(" ", 0, midpoint + 1)
    right = cleaned.find(" ", midpoint)
    candidates = [idx for idx in (left, right) if idx > 0]
    split_at = min(candidates, key=lambda idx: abs(idx - midpoint)) if candidates else midpoint
    current = cleaned[:split_at].strip()
    next_text = cleaned[split_at:].strip()
    if not current:
        return cleaned, ""
    return current, next_text


def build_phone_tts_chunk_handoff(
    chunks: list[dict], *, max_chars: int | None = None
) -> list[dict]:
    """Build the phone streaming handoff from sanitized speech chunks.

    Phone streaming uses the same sentence/word chunks as the generic backend
    contract, but a single ``playback_id`` identifies the whole phone utterance.
    The initial /tts-chunk payload carries chunks 0 and 1; later
    /api/tts/chunk-next calls backfill chunk n+2 after chunk n is consumed.
    """
    del max_chars  # Kept for test/back-compat call sites; phone no longer re-splits here.
    cleaned_chunks = [chunk for chunk in chunks if str(chunk.get("text") or "").strip()]
    if not cleaned_chunks:
        return []

    utterance_id = str(cleaned_chunks[0].get("utterance_id") or uuid.uuid4().hex)
    playback_id = utterance_id
    total = len(cleaned_chunks)
    result = []
    for index, chunk in enumerate(cleaned_chunks):
        text = str(chunk.get("text") or "").strip()
        result.append(
            {
                "playback_id": playback_id,
                "utterance_id": utterance_id,
                "chunk_id": f"{utterance_id}:{index + 1}/{total}",
                "index": index,
                "total": total,
                "text": text,
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    return result


def _backend_chunk_payload(
    current_chunk: dict, next_chunk: dict | None, *, rate: int | float = 0
) -> dict:
    current_public = _chunk_public_payload(current_chunk) or {}
    next_public = _chunk_public_payload(next_chunk)
    payload = {
        "session_id": TTS_AUTHORITATIVE_STATE["session_id"],
        "playback_id": current_chunk["playback_id"],
        "utterance_id": current_chunk["utterance_id"],
        "chunk_id": current_chunk["chunk_id"],
        "chunk_index": current_chunk["index"],
        "chunk_count": current_chunk["total"],
        "current_index": current_chunk["index"],
        "next_index": next_chunk["index"] if next_chunk else None,
        "tts_text": current_chunk["text"],  # backward-compatible MacroDroid key
        "current_chunk": current_chunk["text"],  # Track B phone field
        "current_chunk_text": current_chunk["text"],
        "current_chunk_hash": current_chunk["text_hash"],
        "current_chunk_meta": json.dumps(current_public, ensure_ascii=False),
        "speed": rate,
        "rate": rate,
    }
    if next_public:
        payload.update(
            {
                "next_chunk_id": next_chunk["chunk_id"],
                "next_chunk": next_chunk["text"],  # Track B phone field
                "next_chunk_text": next_chunk["text"],
                "next_chunk_hash": next_chunk["text_hash"],
                "next_chunk_meta": json.dumps(next_public, ensure_ascii=False),
            }
        )
    else:
        payload.update(
            {
                "next_chunk_id": None,
                "next_chunk": "",
                "next_chunk_text": "",
                "next_chunk_hash": None,
                "next_chunk_meta": None,
            }
        )
    return payload


def _phone_stream_key(session_id: str | None, playback_id: str | None) -> tuple[str, str] | None:
    if not session_id or not playback_id:
        return None
    return (str(session_id), str(playback_id))


def _register_phone_tts_stream(
    *, session_id: str, playback_id: str, utterance_id: str, chunks: list[dict]
) -> dict:
    stream = {
        "session_id": session_id,
        "playback_id": playback_id,
        "utterance_id": utterance_id,
        "chunks": [_chunk_public_payload(chunk) for chunk in chunks],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "done": False,
    }
    with _tts_phone_streams_lock:
        TTS_PHONE_STREAMS[(session_id, playback_id)] = stream
    return stream


def _get_phone_tts_stream(session_id: str | None, playback_id: str | None) -> dict | None:
    key = _phone_stream_key(session_id, playback_id)
    if key is None:
        return None
    with _tts_phone_streams_lock:
        stream = TTS_PHONE_STREAMS.get(key)
        return dict(stream) if stream else None


def _complete_phone_tts_stream(session_id: str | None, playback_id: str | None) -> None:
    waiter = pending_phone_playbacks.get(str(playback_id))
    if waiter is not None:
        waiter.set()
    key = _phone_stream_key(session_id, playback_id)
    if key is None:
        return
    with _tts_phone_streams_lock:
        TTS_PHONE_STREAMS.pop(key, None)


def _chunk_next_payload(
    chunk: dict | None, *, done: bool = False, reason: str | None = None
) -> dict:
    public = _chunk_public_payload(chunk) if chunk else None
    return {
        "success": True,
        "done": bool(done),
        "reason": reason,
        "next_index": public.get("index") if public else None,
        "next_chunk_id": public.get("chunk_id") if public else None,
        "next_chunk": public.get("text") if public else "",
        "next_chunk_text": public.get("text") if public else "",
        "next_chunk_hash": public.get("text_hash") if public else None,
        "next_chunk_meta": json.dumps(public, ensure_ascii=False) if public else None,
        "chunk": public,
        "control_state": (TTS_AUTHORITATIVE_STATE.get("control") or {}).get("state"),
    }


def _send_phone_tts_chunk(payload: dict) -> dict:
    if _send_to_phone is None:
        return _no_playback_backend("phone_transport_unavailable")
    playback_id = str(payload["playback_id"])
    event = threading.Event()
    pending_phone_playbacks[playback_id] = event
    try:
        try:
            result = dict(_send_to_phone("/tts-chunk", payload) or {})
        except Exception as exc:
            logger.warning("TTS: Phone chunk send failed (%s)", exc)
            return {"success": False, "error": "phone_send_failed", "reason": str(exc)}
        if not result.get("success"):
            return result
        result.setdefault("method", "phone")
        confirmed = event.wait(timeout=PHONE_PLAYBACK_WATCHDOG_S)
        if not confirmed:
            logger.warning(
                "TTS phone: buffer_drained callback missed for %s after %ss; delivery unconfirmed",
                playback_id,
                PHONE_PLAYBACK_WATCHDOG_S,
            )
        result["playback_id"] = playback_id
        result["chunk_id"] = payload.get("chunk_id")
        result["playback_confirmed"] = confirmed
        if not confirmed:
            result["transport_success"] = bool(result.get("success"))
            result["success"] = False
            result["error"] = "phone_playback_unconfirmed"
            result["reason"] = "phone_playback_unconfirmed"
        return result
    finally:
        pending_phone_playbacks.pop(playback_id, None)


def _post_wsl_chunk(payload: dict, *, voice: str | None, rate: int = 0) -> dict:
    """Send one queued Token-API chunk through the WSL satellite's SAPI transport.

    The live satellite's real playback surface is /tts/speak; it writes the text
    to a temp file on the WSL side and returns rendered_chars/rendered_hash as an
    integrity ack. Keep Token-API's current/next chunk state here, but do not
    require a separate satellite /tts/chunk endpoint.
    """
    chunk_id = payload.get("chunk_id")
    playback_id = payload.get("playback_id")
    session_id = payload.get("session_id")
    message = payload["current_chunk_text"]
    result = speak_tts_wsl(message, voice or "Microsoft David", rate=rate)
    result = dict(result or {})
    result.setdefault("method", "wsl_sapi_chunk")
    result["chunk_id"] = chunk_id
    result["playback_id"] = playback_id
    result["session_id"] = session_id

    if result.get("success") and result.get("rendered_hash"):
        expected_hash = payload["current_chunk_hash"]
        if result["rendered_hash"] != expected_hash:
            logger.error(
                "TTS WSL chunk integrity mismatch: chunk_id=%s playback_id=%s "
                "session_id=%s method=%s expected_hash=%s rendered_hash=%s",
                chunk_id,
                playback_id,
                session_id,
                result.get("method"),
                expected_hash,
                result.get("rendered_hash"),
            )
            return {
                "success": False,
                "error": "satellite_text_integrity_check_failed",
                "method": result.get("method"),
                "chunk_id": chunk_id,
                "playback_id": playback_id,
                "session_id": session_id,
            }
    return result


def dispatch_tts_chunks_to_backend(
    backend: str,
    chunks: list[dict],
    *,
    voice: str | None = None,
    rate: int = 0,
    wsl_voice: str | None = None,
    wsl_rate: int | None = None,
) -> dict:
    """Dispatch sanitized chunks to the selected execution backend.

    Token-OS records the current/next handoff before every backend call. A
    backend failure records an authoritative error and stops; there is no Mac
    fallback. Phone v1 is a single two-slot MacroDroid handoff, not a server-side
    loop over every prepared sentence chunk.
    """
    if backend not in TTS_EXECUTION_BACKENDS:
        _record_tts_backend_active(None)
        return {"success": False, "error": "unknown_playback_backend", "method": None}
    if not chunks:
        return {"success": False, "error": "empty_audio_payload", "method": None}

    if backend == "phone":
        phone_chunks = build_phone_tts_chunk_handoff(chunks)
        if not phone_chunks:
            return {"success": False, "error": "empty_audio_payload", "method": None}
        current_chunk = phone_chunks[0]
        next_chunk = phone_chunks[1] if len(phone_chunks) > 1 else None
        session_id = str(TTS_AUTHORITATIVE_STATE["session_id"])
        _register_phone_tts_stream(
            session_id=session_id,
            playback_id=current_chunk["playback_id"],
            utterance_id=current_chunk["utterance_id"],
            chunks=phone_chunks,
        )
        _record_tts_backend_active(
            backend,
            playback_id=current_chunk["playback_id"],
            current=_chunk_public_payload(current_chunk),
            next_chunk=_chunk_public_payload(next_chunk),
        )
        payload = _backend_chunk_payload(current_chunk, next_chunk, rate=rate)
        result = dict(_send_phone_tts_chunk(payload) or {})
        result.setdefault("chunk_id", current_chunk["chunk_id"])
        real_chunks = len(phone_chunks)
        if not result.get("success"):
            _complete_phone_tts_stream(session_id, current_chunk["playback_id"])
            error = result.get("error") or "phone_backend_error"
            _update_tts_authoritative_state(
                last_error={
                    "backend": backend,
                    "playback_id": current_chunk["playback_id"],
                    "chunk_id": current_chunk["chunk_id"],
                    "error": error,
                    "reported_at": _now_iso(),
                }
            )
            return {
                "success": False,
                "error": error,
                "method": result.get("method") or backend,
                "chunks": real_chunks,
                "completed_chunks": 0,
                "playback_id": result.get("playback_id"),
                "playback_confirmed": result.get("playback_confirmed"),
                "results": [result],
                "reason": result.get("reason") or error,
            }
        _complete_phone_tts_stream(session_id, current_chunk["playback_id"])
        return {
            "success": True,
            "method": result.get("method") or backend,
            "backend": backend,
            "chunks": real_chunks,
            "completed_chunks": real_chunks,
            "playback_id": result.get("playback_id"),
            "playback_confirmed": result.get("playback_confirmed"),
            "results": [result],
        }

    results = []
    for index, chunk in enumerate(chunks):
        next_chunk = chunks[index + 1] if index + 1 < len(chunks) else None
        _record_tts_backend_active(
            backend,
            playback_id=chunk["playback_id"],
            current=_chunk_public_payload(chunk),
            next_chunk=_chunk_public_payload(next_chunk),
        )
        payload = _backend_chunk_payload(chunk, next_chunk, rate=rate)
        if backend == "phone":
            result = _send_phone_tts_chunk(payload)
        elif backend == "wsl":
            result = _post_wsl_chunk(payload, voice=wsl_voice or voice, rate=wsl_rate or rate)
        else:
            result = {"success": False, "error": "linux_backend_not_configured", "method": "linux"}

        result = dict(result or {})
        result.setdefault("chunk_id", chunk["chunk_id"])
        results.append(result)
        if result.get("skipped"):
            return {
                "success": False,
                "skipped": True,
                "method": result.get("method") or backend,
                "chunks": len(chunks),
                "completed_chunks": index,
                "results": results,
                "reason": "skipped",
            }
        if not result.get("success"):
            error = result.get("error") or "backend_chunk_failed"
            _update_tts_authoritative_state(
                last_error={
                    "backend": backend,
                    "playback_id": chunk["playback_id"],
                    "chunk_id": chunk["chunk_id"],
                    "error": error,
                    "reported_at": _now_iso(),
                }
            )
            return {
                "success": False,
                "error": error,
                "method": result.get("method") or backend,
                "chunks": len(chunks),
                "completed_chunks": index,
                "results": results,
                "reason": result.get("reason") or error,
            }

    return {
        "success": True,
        "method": results[-1].get("method") or backend,
        "backend": backend,
        "chunks": len(chunks),
        "completed_chunks": len(chunks),
        "playback_id": results[-1].get("playback_id"),
        "playback_confirmed": results[-1].get("playback_confirmed"),
        "results": results,
    }


# How long the awaited front door (``dispatch_notify`` / ``POST /api/notify``)
# waits for the worker to finish playing the line it enqueued before reporting
# ``audio_delivered=False`` truthfully. Generous cap > worst-case single playback
# (phone watchdog + render). On timeout the line may still finish out-of-band; we
# just stop blocking the HTTP caller. Not a duration estimate.
NOTIFY_AUDIO_COMPLETION_TIMEOUT_S = _positive_int_env(
    "TOKEN_API_NOTIFY_AUDIO_COMPLETION_TIMEOUT_S", 90
)

# Single base words-per-minute tunable shared by every speech backend. The SAPI
# rate scale (-10..10) biases around it: wpm = BASE if rate==0 else BASE-15 +
# rate*15, clamped 80..300. Bumped from the old hardcoded 190 for snappier speech.
TTS_RATE_BASE_WPM = _positive_int_env("TOKEN_API_TTS_RATE_BASE_WPM", 210, minimum=80)

# HTTP timeout for the Discord daemon /voice/tts call. The daemon blocks until
# playback reaches Idle (now per-bot serialized), so a long line legitimately
# holds the request open — keep this well above a single spoken line so long
# lines don't false-timeout (was a hardcoded 60s).
TTS_DISCORD_HTTP_TIMEOUT_S = _positive_int_env("TOKEN_API_TTS_DISCORD_HTTP_TIMEOUT_S", 180)

# Small positive SAPI rate bias for the WSL satellite path, from the
# same tunable family, so when it is reached speech is a touch snappier. Clamped
# into the SAPI -10..10 scale at the callsite.
TTS_WSL_RATE_BIAS = _positive_int_env("TOKEN_API_TTS_WSL_RATE_BIAS", 1, minimum=0)


def _wpm_for_rate(rate: int = 0) -> int:
    """Map the SAPI rate scale to a clamped WPM around TTS_RATE_BASE_WPM."""
    wpm = TTS_RATE_BASE_WPM if rate == 0 else (TTS_RATE_BASE_WPM - 15) + (rate * 15)
    return max(80, min(300, wpm))


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
            shared._tmuxctld_run_tmux(
                ("set-option", "-p", "-t", pane_id, "@TTS_STATE", state),
                timeout=2,
            )
        else:
            shared._tmuxctld_run_tmux(
                ("set-option", "-p", "-u", "-t", pane_id, "@TTS_STATE"),
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
    """Run a tmux command through tmuxctld, returning a CompletedProcess-like object.

    Centralizes invocation so the focus-snap path is monkeypatchable and never
    raises into playback. Run off the event loop via asyncio.to_thread.
    """
    try:
        result = shared._tmuxctld_run_tmux(args, timeout=timeout)
        if result is None:
            return None
        return SimpleNamespace(returncode=0, stdout=str(result.get("stdout") or ""), stderr="")
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
        async with connect_agents_db(DB_PATH) as db:
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

        async with connect_agents_db(DB_PATH) as db:
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
        # Local handle so completion is always resolvable even after the global
        # is cleared (and survives an exception below). This is the sole place
        # that calls speak_tts — the single-playback invariant.
        item: TTSQueueItem | None = None
        try:
            # Wait for items in hot queue
            async with tts_queue_lock:
                if hot_queue:
                    tts_current = hot_queue.popleft()
                else:
                    tts_current = None
                item = tts_current

            if item:
                # Playback focus snap is explicit-action only. Direct hot TTS
                # may fire from background hooks at arbitrary times; do not
                # steal focus for that. Promoting/playing from the pause queue
                # sets focus_on_playback=True because the operator pressed play.
                if item.message and (item.focus_on_playback or TTS_AUTO_FOCUS_ENABLED):
                    await _snap_focus_to_speaker(item)

                # Log TTS starting
                await log_event(
                    "tts_playing",
                    instance_id=item.instance_id,
                    details={
                        "message": item.message[:100],
                        "voice": item.voice,
                        "name": item.name,
                    },
                )

                # Resolve the source pane LIVE (oracle) at playback time and stash
                # it on the item for the speaking/clear @TTS_STATE writes below.
                # (None on a dead/unstamped pane -> _set_tts_state no-ops.)
                item.tmux_pane, _ = await resolve_instance_pane(item.instance_id)

                # Set @TTS_STATE on source pane
                _set_tts_state(item.tmux_pane, "speaking")

                # Stamp the audible-playback start so the cockpit shows a real
                # started_at for the WHOLE duration the line blocks below.
                item.started_at = datetime.now().isoformat()

                # Play notification sound first (run in executor to not block event loop)
                sound_result = None
                if item.sound:
                    loop = asyncio.get_event_loop()
                    sound_result = await loop.run_in_executor(None, play_sound, item.sound)
                    logger.info(f"TTS worker: sound result = {json.dumps(sound_result)}")
                    if not sound_result.get("success"):
                        logger.warning(f"Sound failed: {sound_result.get('error')}")
                    await asyncio.sleep(0.3)  # Brief pause after sound

                if item.message:
                    # Resolve persona playback settings by WSL voice
                    # (DB tts_voice stores the Windows voice name).
                    wsl_voice = item.voice
                    settings = voice_settings_for_tts_voice(wsl_voice)
                    mac_voice = settings["mac_voice"]
                    wsl_rate = settings["wsl_rate"]

                    # Speak the message (run in executor to allow skip API to interrupt)
                    # Queue items use file-based playback for transport controls (pause/resume/speed)
                    logger.info(
                        f"TTS worker: speaking {len(item.message)} chars with {wsl_voice} (mac={mac_voice}, file_playback=True)"
                    )
                    loop = asyncio.get_event_loop()
                    tts_result = await loop.run_in_executor(
                        None,
                        functools.partial(
                            speak_tts,
                            item.message,
                            mac_voice,
                            0,
                            item.instance_id,
                            wsl_voice,
                            wsl_rate,
                            use_file_playback=True,
                        ),
                    )
                    logger.info(f"TTS worker: speak result = {json.dumps(tts_result)}")

                    # Log completion, skip, or failure — and resolve the awaiting
                    # front door truthfully in EVERY branch.
                    if tts_result.get("skipped") or tts_result.get("method") == "skipped":
                        logger.info(f"TTS skipped for {item.instance_id}")
                        await log_event(
                            "tts_skipped",
                            instance_id=item.instance_id,
                            details={"message": item.message[:50], "voice": item.voice},
                        )
                        _resolve_completion(
                            item,
                            success=False,
                            route=tts_result.get("route"),
                            audio_delivered=False,  # a skip is not delivery
                            reason="skipped",
                        )
                    elif tts_result.get("success"):
                        await log_event(
                            "tts_completed",
                            instance_id=item.instance_id,
                            details={"message": item.message[:50], "voice": item.voice},
                        )
                        _resolve_completion(
                            item,
                            success=True,
                            route=tts_result.get("route"),
                            audio_delivered=True,
                        )
                    else:
                        logger.error(
                            f"TTS failed for {item.instance_id}: {tts_result.get('error')}"
                        )
                        await log_event(
                            "tts_failed",
                            instance_id=item.instance_id,
                            details={
                                "message": item.message[:50],
                                "voice": item.voice,
                                "error": tts_result.get("error", "Unknown error"),
                                "sound_result": sound_result,
                            },
                        )
                        _resolve_completion(
                            item,
                            success=False,
                            route=None,
                            audio_delivered=False,
                            reason=tts_result.get("reason")
                            or tts_result.get("error")
                            or "tts_failed",
                            error=tts_result.get("error"),
                        )
                else:
                    logger.info(f"TTS worker: muted mode, sound only for {item.instance_id}")
                    # Muted (sound-only): no spoken audio delivered. Resolve on the
                    # sound outcome so any awaiter unblocks rather than timing out.
                    _resolve_completion(
                        item,
                        success=bool(sound_result and sound_result.get("success")),
                        route="sound" if sound_result and sound_result.get("success") else None,
                        audio_delivered=False,
                        reason="muted_sound_only",
                    )

                # Clear @TTS_STATE on source pane
                _set_tts_state(item.tmux_pane, "")
                # Clear the current-playing marker UNDER the lock so a cockpit
                # poll can't catch the set→clear window.
                async with tts_queue_lock:
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
                if item is not None and getattr(item, "tmux_pane", None):
                    _set_tts_state(item.tmux_pane, "")
            except Exception:
                pass
            # Unblock any awaiting front door so an exception can't wedge it.
            if item is not None:
                _resolve_completion(
                    item,
                    success=False,
                    route=None,
                    audio_delivered=False,
                    reason="tts_worker_exception",
                    error=str(e),
                )
            async with tts_queue_lock:
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


async def queue_tts(
    instance_id: str,
    message: str,
    queue_target: str = "pause",
    completion: "asyncio.Future | None" = None,
    bypass_persona_silent: bool = False,
) -> dict:
    """Queue a TTS message for an instance, using their profile's voice/sound.

    This is THE gated single-queue enqueue: both the agent-TTS endpoint and the
    notify front door (``dispatch_notify``) feed it, so the worker is the only
    place audio plays. The deny-by-default persona gate (PR B) stays intact.

    Args:
        instance_id: The instance ID that triggered TTS. The synthetic
            ``SYSTEM_INSTANCE_ID`` ("system") is a recognized always-resolved
            sender (Custodes-voiced, hot) for system/enforcement pings with no
            live persona instance.
        message: The text to speak.
        queue_target: "hot" for immediate playback (VC/sync sessions),
                      "pause" for silent accumulation (default).
        completion: Optional future the worker resolves with
            ``{success, route, audio_delivered}`` when this item reaches a
            terminal playback state. Only the awaiting front door passes one.
    """
    message = await _sanitize_public_text_async(message)

    # Silence TTS during quiet hours (11 PM - 9 AM), except explicit live
    # enforcement where policy requires audible-or-named-failure semantics.
    if _is_quiet_hours() and not bypass_persona_silent:
        logger.info(f"TTS suppressed (quiet hours): {message[:80]}")
        return {"success": True, "queued": False, "reason": "quiet_hours"}

    # Silence TTS during meetings (Zoom/Google Meet)
    if DESKTOP_STATE.get("in_meeting"):
        logger.info(f"TTS suppressed (in meeting): {message[:80]}")
        return {"success": True, "queued": False, "reason": "in_meeting"}

    if instance_id == SYSTEM_INSTANCE_ID:
        # Synthetic sender: skip the DB lookup, use the fixed always-resolved
        # profile. Holds the single-queue invariant without a parallel path.
        row = _SYSTEM_TTS_ROW
    else:
        # Look up instance and resolve audio settings from its persona.
        async with connect_agents_db(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT i.name, p.tts_voice, p.notification_sound,
                          CASE WHEN i.interaction_mode = 'voice_chat'
                               THEN 'voice-chat' ELSE i.notification_mode END AS tts_mode,
                          p.slug AS persona_slug, p.display_name AS persona_display_name,
                          p.tts_policy AS tts_policy, i.commander_type AS commander_type,
                          COALESCE(p.advisor, 0) AS advisor
                   FROM instances i
                   LEFT JOIN personas p ON p.id = i.persona_id
                   WHERE i.id = ?""",
                (instance_id,),
            )
            row = await cursor.fetchone()

    if not row:
        # Fail loud (never silently swallow — [[anti-blind-dedup]]): an audio
        # request naming a nonexistent instance is a registration/wiring bug.
        logger.warning(
            "TTS denied (instance_not_found): instance=%s message=%r",
            instance_id,
            message[:80],
        )
        return {
            "success": False,
            "queued": False,
            "reason": "instance_not_found",
            "error": f"Instance {instance_id} not found",
        }

    # Per-persona TTS policy, deny-by-default (Emperor decree, 2026-06-28:
    # "one route, one authority, one serialized queue"). Submission is gated on a
    # RESOLVED persona's explicit policy. The needs-name Fabricator-General leaked
    # because the old code inferred silence from ``tts_voice IS NULL`` and only
    # ever forced "hot" for a literal custodes slug — an unresolved persona (LEFT
    # JOIN → NULL slug/policy) fell through and spoke. Now an absent persona row or
    # an unknown/NULL policy is SILENT + WARN: a visible registration failure, never
    # a leak (see [[anti-blind-dedup]] — never silently swallow). The voiced
    # Astartes already resolve, so normal speech is unaffected.
    tts_policy = row["tts_policy"]
    persona_slug = row["persona_slug"]
    if tts_policy not in ("silent", "hot", "pause"):
        logger.warning(
            "TTS denied (persona_unresolved): instance=%s persona_slug=%r "
            "tts_policy=%r — register the persona to grant a voice policy; "
            "message=%r",
            instance_id,
            persona_slug,
            tts_policy,
            message[:80],
        )
        return {"success": True, "queued": False, "reason": "persona_unresolved"}

    if tts_policy == "silent":
        if not bypass_persona_silent:
            logger.info(f"TTS suppressed (persona_silent): {message[:80]}")
            return {"success": True, "queued": False, "reason": "persona_silent"}
        logger.warning(
            "TTS enforcement bypassed persona_silent: instance=%s persona=%r message=%r",
            instance_id,
            persona_slug,
            message[:80],
        )
        row = _SYSTEM_TTS_ROW

    # Belt-and-suspenders: an independent silence guarantee. A voiced policy
    # without a resolved persona voice still must not speak unless this is an
    # enforcement notification, in which case use the system/Custodes voice.
    if row["tts_voice"] is None:
        if not bypass_persona_silent:
            return {"success": True, "queued": False, "reason": "persona_silent"}
        logger.warning(
            "TTS enforcement bypassed missing persona voice: instance=%s persona=%r message=%r",
            instance_id,
            persona_slug,
            message[:80],
        )
        row = _SYSTEM_TTS_ROW

    voice = row["tts_voice"]
    sound = row["notification_sound"]
    name = row["name"] or instance_id

    # Check TTS mode (per-instance and global, most restrictive wins)
    instance_mode = row["tts_mode"] or "verbose"
    # voice-chat forces hot queue — it's an active session
    is_voice_chat = instance_mode == "voice-chat"
    if is_voice_chat:
        instance_mode = "verbose"
        queue_target = "hot"

    # Advisor-driven queue target (replaces the hardcoded custodes-slug bypass).
    # Advisor is a persona capability resolved by JOIN at read time, never copied
    # onto instances. Custodes, Pax, and Malcador therefore get queue-bypass
    # parity without making "advisor" a rank or an instance-row projection.
    # Non-advisors respect the caller's queue_target (the default; no-op here).
    if bool(row["advisor"]):
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
            name=name,
            queue_target=queue_target,
            focus_on_playback=False,
            persona_slug=row["persona_slug"],
            persona_display_name=row["persona_display_name"],
            commander_type=row["commander_type"],
            completion=completion,
        )
    else:
        item = TTSQueueItem(
            instance_id=instance_id,
            message=message,
            voice=voice,
            sound=sound,
            name=name,
            queue_target=queue_target,
            focus_on_playback=False,
            persona_slug=row["persona_slug"],
            persona_display_name=row["persona_display_name"],
            commander_type=row["commander_type"],
            completion=completion,
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
            head.name or "",
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
    backend_null_expired_details = []
    for expired_item in expired:
        age_seconds = max(0, int((now - expired_item.queued_at).total_seconds()))
        detail = {
            "instance_id": expired_item.instance_id,
            "name": expired_item.name,
            "queue": "pause",
            "queued_at": expired_item.queued_at.isoformat(),
            "age_seconds": age_seconds,
            "held_max_age_seconds": TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS,
            "playback_target": expired_item.playback_target,
            "message_hash": hashlib.sha256(
                (expired_item.message or "").encode("utf-8")
            ).hexdigest()[:16],
            "message_length": len(expired_item.message or ""),
            "message_truncated": len(expired_item.message or "") > 300,
        }
        expired_details.append(detail)
        if expired_item.playback_target is None:
            backend_null_expired_details.append(detail)

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

    for detail in backend_null_expired_details:
        try:
            await log_event(
                "tts_backend_null_queue_stale",
                instance_id=detail["instance_id"],
                device_id="tts_queue",
                details={**detail, "reason": "stale_pause_queue_item_had_backend_null"},
            )
        except Exception:
            logger.warning("Failed to log backend-null stale TTS pause item", exc_info=True)

    if backend_null_expired_details and _custodes_state_event_handler is not None:
        oldest = max(backend_null_expired_details, key=lambda d: d["age_seconds"])
        try:
            await _custodes_state_event_handler(
                "tts_backend_null_queue_stale",
                "tts_queue",
                instance_id=oldest["instance_id"],
                severity=4,
                payload={
                    "app": "tts_queue",
                    "queue": "pause",
                    "backend_null_count": len(backend_null_expired_details),
                    "oldest_queued_at": oldest["queued_at"],
                    "oldest_age_seconds": oldest["age_seconds"],
                    "held_max_age_seconds": TTS_PAUSE_QUEUE_HELD_MAX_AGE_SECONDS,
                    "reason": "stale_pause_queue_item_had_backend_null",
                },
            )
        except Exception:
            logger.warning("Failed to emit backend-null stale TTS state event", exc_info=True)

    if expired_details:
        try:
            await log_event(
                "tts_pause_queue_expiry_sweep",
                device_id="tts_queue",
                details={
                    "expired": len(expired_details),
                    "backend_null_expired": len(backend_null_expired_details),
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
        "latest_name": item.name,
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
        "name": item.name,
        "message": item.message[:50] + "..." if len(item.message) > 50 else item.message,
        "voice": item.voice,
        "playback_target": item.playback_target,
        "persona_slug": item.persona_slug,
        "persona_display_name": item.persona_display_name,
        "commander_type": item.commander_type,
        "queue": item.queue_target,
        "queued_at": item.queued_at.isoformat(),
    }


def get_tts_queue_status() -> dict:
    """Get current TTS queue status for dashboard."""
    hot_list = [_queue_item_to_dict(item) for item in hot_queue]
    pause_list = [_queue_item_to_dict(item) for item in pause_queue]

    # Snapshot the current-playing item into a local first: every attribute below
    # reads from that single handle, so a concurrent worker clear (now done under
    # tts_queue_lock) can't blank it mid-build. Combined with playback truly
    # blocking the worker (steps 1-4), `current` now reflects audible state for the
    # whole line — the cockpit stops flashing "idle" mid-utterance.
    current_item = tts_current
    current = None
    if current_item:
        current = {
            "instance_id": current_item.instance_id,
            "name": current_item.name,
            "message": current_item.message[:50] + "..."
            if len(current_item.message) > 50
            else current_item.message,
            "voice": current_item.voice,
            "playback_target": current_item.playback_target,
            "persona_slug": current_item.persona_slug,
            "persona_display_name": current_item.persona_display_name,
            "commander_type": current_item.commander_type,
            "started_at": current_item.started_at,
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

    Routes skip to the active execution backend through the same control plane.
    Mac is not a TTS backend and is never targeted.

    Args:
        clear_queue: If True, also clear all pending items in the queue.

    Returns:
        Dict with skipped (bool) and cleared (int) counts.
    """
    global tts_current_process, tts_current, tts_skip_requested

    backend = _active_tts_backend()
    echo = await asyncio.to_thread(
        _echo_tts_control_to_backend,
        backend,
        {
            "action": "skip",
            "playback_id": TTS_AUTHORITATIVE_STATE.get("playback_id"),
            "source": "skip_tts",
        },
    )
    result = {"skipped": bool(echo.get("success")), "cleared": 0, "backend": backend, "echo": echo}

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


def _active_tts_backend(requested: str | None = None) -> str | None:
    if requested in TTS_EXECUTION_BACKENDS:
        return requested
    state_backend = TTS_AUTHORITATIVE_STATE.get("backend")
    if state_backend in TTS_EXECUTION_BACKENDS:
        return str(state_backend)
    current_backend = TTS_BACKEND.get("current")
    if current_backend in TTS_EXECUTION_BACKENDS:
        return str(current_backend)
    if tts_current and tts_current.playback_target in TTS_EXECUTION_BACKENDS:
        return tts_current.playback_target
    return None


def _echo_tts_control_to_backend(backend: str | None, payload: dict) -> dict:
    """Echo an already-recorded control to the active execution backend."""
    if backend not in TTS_EXECUTION_BACKENDS:
        return {"success": False, "backend": backend, "error": "no_active_tts_backend"}

    action = payload["action"]
    if backend == "phone":
        if _send_to_phone is None:
            return {"success": False, "backend": backend, "error": "phone_transport_unavailable"}
        params = {
            "command": action,
            "session_id": payload.get("session_id") or TTS_AUTHORITATIVE_STATE.get("session_id"),
            "playback_id": payload.get("playback_id"),
            "speed": payload.get("speed"),
        }
        try:
            result = dict(_send_to_phone("/tts-local-control", params) or {})
        except Exception as exc:
            return {"success": False, "backend": backend, "error": str(exc)}
        result.setdefault("backend", backend)
        return result

    if backend == "wsl":
        command = "stop" if action == "skip" else action
        body = {"command": command}
        if payload.get("speed") is not None:
            body["speed"] = payload.get("speed")
        try:
            resp = requests.post(
                f"http://{DESKTOP_CONFIG['host']}:{DESKTOP_CONFIG['port']}/tts/control",
                json=body,
                timeout=3,
            )
        except Exception as exc:
            return {"success": False, "backend": backend, "error": str(exc)}
        if resp.status_code == 200:
            result = dict(resp.json())
            result.setdefault("success", True)
            result.setdefault("backend", backend)
            return result
        return {"success": False, "backend": backend, "error": f"wsl returned {resp.status_code}"}

    return {"success": False, "backend": backend, "error": "linux_backend_not_configured"}


@router.post("/api/tts/control")
async def api_tts_control(request: TTSControlRequest) -> dict:
    """Single authoritative TTS control ingress.

    Contract for overlays/backends:
    1. POST here with ``action`` = pause|resume|skip|speed.
    2. Token-OS records authoritative state first.
    3. Token-OS echoes the same control to the active backend's local-control
       endpoint/mechanism and returns that echo/error without falling back to Mac.
    """
    action = (request.command or request.action or "").strip().lower()
    if action not in {"pause", "resume", "skip", "speed"}:
        raise HTTPException(status_code=400, detail="command must be pause, resume, skip, or speed")
    if action == "speed" and request.speed is None:
        raise HTTPException(status_code=400, detail="speed action requires speed")

    backend = _active_tts_backend(request.backend)
    recorded = _record_tts_control_state(request, backend)
    echo_payload = {
        "action": action,
        "command": action,
        "speed": request.speed,
        "playback_id": request.playback_id or recorded.get("playback_id"),
        "session_id": request.session_id or recorded.get("session_id"),
        "source": request.source,
    }
    backend_echo = await asyncio.to_thread(_echo_tts_control_to_backend, backend, echo_payload)
    success = bool(backend_echo.get("success"))
    if not success:
        _update_tts_authoritative_state(
            last_error={
                "backend": backend,
                "playback_id": echo_payload.get("playback_id"),
                "error": backend_echo.get("error") or "backend_control_failed",
                "reported_at": _now_iso(),
            }
        )
    await log_event(
        "tts_control",
        device_id="tts_control",
        details={
            "action": action,
            "speed": request.speed,
            "backend": backend,
            "success": success,
            "backend_echo": backend_echo,
        },
    )
    return {
        "success": success,
        "state": get_tts_authoritative_state(),
        "backend_echo": backend_echo,
    }


@router.post("/api/tts/backend/ack")
async def api_tts_backend_ack(request: TTSBackendAckRequest) -> dict:
    """Execution backend acknowledgement for current/next chunk dispatch."""
    ack = {
        "backend": request.backend,
        "session_id": request.session_id,
        "playback_id": request.playback_id,
        "chunk_id": request.chunk_id,
        "status": request.status,
        "detail": request.detail or {},
        "reported_at": _now_iso(),
    }
    _update_tts_authoritative_state(last_backend_ack=ack)
    await log_event("tts_backend_ack", device_id=request.backend, details=ack)
    return {"success": True, "state": get_tts_authoritative_state()}


@router.post("/api/tts/chunk-next")
async def api_tts_chunk_next(request: TTSChunkNextRequest) -> dict:
    """Return the next phone streaming backfill chunk, or explicit done.

    The phone starts with current chunk ``n`` and buffered chunk ``n+1``. After
    ``n`` is consumed it asks for ``n+2``. Token-OS remains authoritative: pause
    returns a paused response without advancing, skip completes the active phone
    stream, and resume allows normal backfill again.
    """
    stream = _get_phone_tts_stream(request.session_id, request.playback_id)
    if stream is None:
        return {
            "success": True,
            "done": True,
            "reason": "unknown_or_expired_stream",
            "next_chunk": "",
            "next_chunk_text": "",
            "next_index": None,
            "control_state": (TTS_AUTHORITATIVE_STATE.get("control") or {}).get("state"),
        }

    control = dict(TTS_AUTHORITATIVE_STATE.get("control") or {})
    control_state = control.get("state")
    if control_state == "paused":
        payload = _chunk_next_payload(None, done=False, reason="paused")
        payload.update(
            {"paused": True, "session_id": request.session_id, "playback_id": request.playback_id}
        )
        await log_event(
            "tts_chunk_next",
            device_id=request.backend,
            details={**payload, "last_consumed_index": request.last_consumed_index},
        )
        return payload
    if control_state == "skipping":
        _complete_phone_tts_stream(request.session_id, request.playback_id)
        payload = _chunk_next_payload(None, done=True, reason="skipped")
        payload.update(
            {"skipped": True, "session_id": request.session_id, "playback_id": request.playback_id}
        )
        await log_event(
            "tts_chunk_next",
            device_id=request.backend,
            details={**payload, "last_consumed_index": request.last_consumed_index},
        )
        return payload

    chunks = list(stream.get("chunks") or [])
    total = len(chunks)
    last_consumed = int(request.last_consumed_index)
    backfill_index = last_consumed + 2
    if backfill_index < total:
        chunk = chunks[backfill_index]
        current = chunks[last_consumed + 1] if last_consumed + 1 < total else None
        _record_tts_backend_active(
            "phone",
            playback_id=request.playback_id,
            current=current,
            next_chunk=chunk,
        )
        payload = _chunk_next_payload(chunk, done=False)
        with _tts_phone_streams_lock:
            key = _phone_stream_key(request.session_id, request.playback_id)
            if key in TTS_PHONE_STREAMS:
                TTS_PHONE_STREAMS[key]["updated_at"] = _now_iso()
                TTS_PHONE_STREAMS[key]["last_backfill_index"] = backfill_index
    else:
        current = chunks[last_consumed + 1] if last_consumed + 1 < total else None
        _record_tts_backend_active(
            "phone",
            playback_id=request.playback_id,
            current=current,
            next_chunk=None,
        )
        payload = _chunk_next_payload(None, done=True, reason="stream_exhausted")

    payload.update(
        {
            "session_id": request.session_id,
            "playback_id": request.playback_id,
            "utterance_id": stream.get("utterance_id"),
            "last_consumed_index": last_consumed,
            "backfill_index": backfill_index,
            "total": total,
        }
    )
    await log_event("tts_chunk_next", device_id=request.backend, details=payload)
    return payload


@router.post("/api/tts/chunk-event")
async def api_tts_chunk_event(request: TTSChunkEventRequest) -> dict:
    """Phone chunk lifecycle event ingress.

    Accepted Track B events:
    - ``current_complete_next_starting`` — phone finished current and is
      starting its one buffered next chunk.
    - ``buffer_drained`` — phone consumed its one-chunk write-ahead and needs
      Token-OS to dispatch the next handoff.
    """
    if request.event not in {"current_complete_next_starting", "buffer_drained"}:
        raise HTTPException(status_code=400, detail="unknown chunk event")
    event = {
        "event": request.event,
        "backend": request.backend,
        "session_id": request.session_id,
        "playback_id": request.playback_id,
        "chunk_id": request.chunk_id,
        "current_index": request.current_index,
        "next_index": request.next_index,
        "detail": request.detail or {},
        "reported_at": _now_iso(),
    }
    matched_playback = False
    if request.event == "buffer_drained" and request.playback_id:
        waiter = pending_phone_playbacks.get(request.playback_id)
        if waiter is not None:
            _complete_phone_tts_stream(request.session_id, request.playback_id)
            matched_playback = True
            logger.info(
                "tts chunk-event buffer_drained: playback_id=%s confirmed", request.playback_id
            )
        else:
            logger.warning(
                "tts chunk-event buffer_drained: unknown/expired playback_id=%s (no waiter); ignoring",
                request.playback_id,
            )
    event["matched_playback"] = matched_playback
    _update_tts_authoritative_state(last_backend_ack=event)
    await log_event("tts_chunk_event", device_id=request.backend, details=event)
    return {
        "success": True,
        "matched_playback": matched_playback,
        "state": get_tts_authoritative_state(),
    }


async def api_tts_phone_test(
    request: TTSPhoneTestRequest, http_request: Request | None = None
) -> dict:
    """Run a real phone-only streaming TTS probe.

    This endpoint is intentionally not part of the normal notification router:
    it exists to validate the phone MacroDroid chunk player while WSL remains
    healthy and independently available.
    """
    _require_privileged_tts_probe_request(http_request)
    state = get_tts_authoritative_state()
    if _phone_test_probe_conflicts_with_active_tts(state):
        raise HTTPException(status_code=409, detail="active TTS playback in progress")

    message = sanitize_tts_for_speech(
        _sanitize_public_text(clean_markdown_for_tts(request.message))
    )
    if not message.strip():
        raise HTTPException(status_code=400, detail="message required")

    session_id = f"phone-test-{uuid.uuid4().hex}"
    _update_tts_authoritative_state(
        session_id=session_id,
        control={
            "state": "playing",
            "last_action": None,
            "speed": 1.0,
            "source": "phone_test",
            "updated_at": _now_iso(),
        },
    )
    chunks = build_tts_chunk_handoff(message, max_chars=request.max_chars)
    result: dict = {}
    try:
        result = await asyncio.to_thread(
            dispatch_tts_chunks_to_backend,
            "phone",
            chunks,
            rate=request.rate,
        )
        result = dict(result or {})
    except Exception:
        _update_tts_authoritative_state(
            control={
                "state": "error",
                "last_action": "phone_test",
                "speed": 1.0,
                "source": "phone_test",
                "updated_at": _now_iso(),
            }
        )
        raise
    terminal_state = "idle" if result.get("success") else "error"
    _update_tts_authoritative_state(
        control={
            "state": terminal_state,
            "last_action": "phone_test",
            "speed": 1.0,
            "source": "phone_test",
            "updated_at": _now_iso(),
        }
    )
    result["session_id"] = session_id
    result["requested_backend"] = "phone"
    result["router_bypassed"] = True
    result["input_chunks"] = len(chunks)
    await log_event(
        "tts_phone_test",
        device_id="phone",
        details={
            "success": bool(result.get("success")),
            "playback_id": result.get("playback_id"),
            "chunks": result.get("chunks"),
            "input_chunks": len(chunks),
            "playback_confirmed": result.get("playback_confirmed"),
            "error": result.get("error"),
            "reason": result.get("reason"),
        },
    )
    return result


@router.post("/api/tts/phone-test")
async def api_tts_phone_test_route(request: TTSPhoneTestRequest, http_request: Request) -> dict:
    return await api_tts_phone_test(request, http_request)


@router.post("/api/tts/backend-error")
@router.post("/api/tts/backend/error")
async def api_tts_backend_error(request: TTSBackendErrorRequest) -> dict:
    """Execution backend error report. Token-OS records the error; no Mac fallback."""
    error = {
        "backend": request.backend,
        "session_id": request.session_id,
        "playback_id": request.playback_id,
        "chunk_id": request.chunk_id,
        "error": request.error,
        "retryable": request.retryable,
        "detail": request.detail or {},
        "reported_at": _now_iso(),
    }
    state = get_tts_authoritative_state()
    control = dict(state.get("control") or {})
    control["state"] = "error"
    control["last_action"] = "backend_error"
    control["updated_at"] = _now_iso()
    _update_tts_authoritative_state(last_error=error, control=control)
    await log_event("tts_backend_error", device_id=request.backend, details=error)
    return {"success": True, "state": get_tts_authoritative_state()}


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
            # Diagnostic only: phone chunk routing gates on MacroDroid reachability;
            # phone_audio_proxy describes a separate receiver transport.
            "phone_server_reachable": is_phone_reachable(),
            "phone_audio_proxy": get_phone_audio_proxy_health(),
            "discord_vc_active": routing["device"] == "discord",
        },
    }


@router.post("/api/notify")
async def send_notification(request: NotifyRequest):
    """Authoritative comms entry — the single public notification endpoint.

    Thin wrapper over the in-process `dispatch_notify` router core. Callers
    express intent (message + optional tactile/banner); the router owns
    authoritative TTS routing, quiet-hours gating, and device fanout. There is
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
        context=request.context,
        enforcement=request.enforcement,
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
async def queue_tts_message(request: QueueTTSRequest) -> dict:
    """Queue a TTS message for an instance. Uses the instance's profile voice/sound.

    Messages are played sequentially - if another TTS is playing, this will queue.
    Returns the queue position.
    """
    return await queue_tts(request.instance_id, request.message, queue_target=request.queue_target)


@router.get("/api/notify/queue/status")
async def get_queue_status() -> dict:
    """Get current TTS queue status."""
    return get_tts_queue_status()


@router.post("/api/tts/queue/promote")
async def promote_from_pause(request: Request, instance_id: str | None = None) -> dict:
    """Move item(s) from pause queue to the front of hot queue.

    Plugin-friendly: takes its argument as a query param OR a JSON body, and a
    bare POST (no body, or a body without a JSON Content-Type — common from
    Stream Deck web-request plugins) is fine.

    - no arg                       → promotes the next (oldest) item.
    - ?instance_id=xxx  (query)    → promotes all items from that instance.
    - {"instance_id":"xxx"} (body) → same, back-compat.
    """
    if instance_id is None:
        # Back-compat: accept {"instance_id": "..."} JSON body. Tolerate empty /
        # missing / non-JSON bodies rather than 422-ing the operator's button.
        try:
            body = await request.json()
            if isinstance(body, dict):
                instance_id = body.get("instance_id")
        except Exception:
            instance_id = None

    promoted = 0
    async with tts_queue_lock:
        if not pause_queue:
            return {"success": True, "promoted": 0, "reason": "pause_queue_empty"}

        if instance_id:
            # Promote all items from this instance, oldest first on playback.
            to_promote = [item for item in pause_queue if item.instance_id == instance_id]
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
async def play_all_from_pause() -> dict:
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
async def play_pane(request: PlayPaneRequest) -> dict:
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


@router.post("/api/tts/playback-complete")
async def tts_playback_complete(request: PlaybackCompleteRequest) -> dict:
    """Phone audio-finish callback — the real serialization signal.

    The device POSTs the ``playback_id`` it was handed (via ``/tts-chunk``)
    once it has finished speaking that chunk. We set the
    matching Event so the blocked worker thread advances to the next queued item.
    No server-side duration estimation: the phone alone decides when "done."

    Unknown / expired / duplicate ids are tolerated with 200 + a warning (never
    error the phone, never silently swallow — [[no-suppress-debounce]]).
    """
    playback_id = request.playback_id
    event = pending_phone_playbacks.get(playback_id)
    if event is None:
        logger.warning(
            "tts playback-complete: unknown/expired playback_id=%s (no waiter); ignoring",
            playback_id,
        )
        return {"success": True, "matched": False, "reason": "unknown_playback_id"}
    event.set()
    logger.info("tts playback-complete: playback_id=%s confirmed", playback_id)
    return {"success": True, "matched": True}


@router.post("/api/tts/skip")
async def api_tts_skip(clear_queue: bool = False) -> dict:
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
async def set_global_tts_mode(request: Request, mode: str | None = None) -> dict:
    """Set global TTS mode. Overrides all instances.

    Plugin-friendly: `mode` may come from a query param (?mode=toggle) OR a
    JSON body ({"mode":"toggle"}). Query params are the safest form for Stream
    Deck web-request plugins, which often omit the JSON Content-Type.
    """
    if mode is None:
        # Tolerate empty / missing / non-JSON bodies; fall back to verbose.
        try:
            body = await request.json()
            mode = body.get("mode", "verbose") if isinstance(body, dict) else "verbose"
        except Exception:
            mode = "verbose"
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
    async with connect_agents_db(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM instances WHERE status NOT IN ('stopped', 'archived') AND is_subagent = 0"
        )
        rows = await cursor.fetchall()
        for row in rows:
            await update_instance(
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
async def test_notification() -> dict:
    """Test the notification system with a simple message."""
    sound_result = play_sound()
    tts_result = speak_tts("Token API notification test")

    return {"sound": sound_result, "tts": tts_result, "message": "Test notification sent"}
