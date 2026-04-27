"""
Shared state, configuration, and utilities for Token-API.

This module exists to break circular imports between main.py and route modules.
State dicts live here; both main.py and routes/* import from this module.

Phase 2 will convert these raw dicts into TypedDicts/dataclasses.
"""

import os
import json
import time
import random
import logging
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger("token_api")

# ============ Configuration ============

DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))
_imperium_root = Path(os.environ.get("IMPERIUM", "/Volumes/Imperium"))
if not _imperium_root.exists():
    _imperium_root = Path.home()
DEFAULT_SESSIONS_DIR = _imperium_root / "Imperium-ENV" / "Terra" / "Sessions"
MARS_SESSIONS_DIR = _imperium_root / "Imperium-ENV" / "Mars" / "Sessions"
SERVER_PORT = 7777
CRASH_LOG_PATH = Path.home() / ".claude" / "token-api-crash.log"
STASH_DIR = Path.home() / ".claude" / "stash"
STASH_MAX_AGE_HOURS = 24


# ============ Voice Profiles ============

PROFILES = [
    {"name": "profile_1", "wsl_voice": "Microsoft George",    "wsl_rate": 2, "mac_voice": "Daniel", "notification_sound": "chimes.wav", "color": "#66cccc", "cc_color": "cyan"},     # UK M
    {"name": "profile_2", "wsl_voice": "Microsoft Susan",     "wsl_rate": 1, "mac_voice": "Karen",  "notification_sound": "notify.wav", "color": "#ff66cc", "cc_color": "pink"},     # UK F
    {"name": "profile_3", "wsl_voice": "Microsoft Catherine",  "wsl_rate": 1, "mac_voice": "Karen", "notification_sound": "ding.wav",   "color": "#ffcc00", "cc_color": "yellow"},   # AU F
    {"name": "profile_5", "wsl_voice": "Microsoft Sean",      "wsl_rate": 0, "mac_voice": "Moira",  "notification_sound": "chord.wav",  "color": "#ff9900", "cc_color": "orange"},   # IE M
    {"name": "profile_7", "wsl_voice": "Microsoft Heera",     "wsl_rate": 1, "mac_voice": "Rishi",  "notification_sound": "chimes.wav", "color": "#cc66ff", "cc_color": "purple"},   # IN F
    {"name": "profile_8", "wsl_voice": "Microsoft Ravi",      "wsl_rate": 1, "mac_voice": "Rishi",  "notification_sound": "notify.wav", "color": "#ff6666", "cc_color": "red"},      # IN M
]

FALLBACK_VOICES = [
    {"name": "fallback_1", "wsl_voice": "Microsoft David", "wsl_rate": 1, "mac_voice": "Daniel", "notification_sound": "tada.wav",   "color": "#888888", "cc_color": "default"},
    {"name": "fallback_2", "wsl_voice": "Microsoft Zira",  "wsl_rate": 1, "mac_voice": "Karen",  "notification_sound": "chord.wav",  "color": "#999999", "cc_color": "default"},
    {"name": "fallback_3", "wsl_voice": "Microsoft Mark",  "wsl_rate": 1, "mac_voice": "Daniel", "notification_sound": "recycle.wav","color": "#aaaaaa", "cc_color": "default"},
]

ULTIMATE_FALLBACK = {"name": "fallback_david", "wsl_voice": "Microsoft David", "wsl_rate": 1, "mac_voice": "Daniel", "notification_sound": "chimes.wav", "color": "#666666", "cc_color": "default"}


def get_next_available_profile(used_wsl_voices: set) -> tuple[dict, bool]:
    """Assign a profile using random-start linear probe (open addressing).

    Args:
        used_wsl_voices: Set of WSL voice names currently held by active instances.

    Returns:
        (profile_dict, pool_exhausted) — pool_exhausted is True if we had to
        dip into fallback voices (David/Zira/Mark) or the ultimate fallback.
    """
    # 1. Try foreign-accent pool with linear probe
    n = len(PROFILES)
    start = random.randint(0, n - 1)
    for i in range(n):
        idx = (start + i) % n
        if PROFILES[idx]["wsl_voice"] not in used_wsl_voices:
            return PROFILES[idx], False

    # 2. Foreign pool exhausted — try fallback voices (David, Zira, Mark)
    for fb in FALLBACK_VOICES:
        if fb["wsl_voice"] not in used_wsl_voices:
            return fb, True

    # 3. All exhausted — ultimate fallback (David again, shared slot)
    return ULTIMATE_FALLBACK, True


# ============ TTS State ============

# Windows satellite server config (token-satellite on WSL via Tailscale)
DESKTOP_CONFIG = {
    "host": "100.66.10.74",  # WSL Tailscale IP
    "port": 7777,
    "timeout": 5,
}

# TTS backend routing state (WSL-first with Mac fallback)
TTS_BACKEND = {
    "current": None,          # "wsl" | "mac" | None — what's currently speaking
    "satellite_available": None,  # True/False/None (unknown)
    "last_health_check": 0,
    "health_check_ttl": 30,   # Re-probe satellite every 30s
}

# Global TTS mute state (in-memory, resets to "verbose" on server restart)
TTS_GLOBAL_MODE = {
    "mode": "verbose",  # "verbose" | "muted" | "silent"
}


def is_satellite_tts_available() -> bool:
    """Check if the WSL satellite TTS endpoint is reachable. Cached with 30s TTL."""
    import requests

    now = time.time()
    if (TTS_BACKEND["satellite_available"] is not None
            and now - TTS_BACKEND["last_health_check"] < TTS_BACKEND["health_check_ttl"]):
        return TTS_BACKEND["satellite_available"]

    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    try:
        resp = requests.get(f"http://{host}:{port}/health", timeout=2)
        available = resp.status_code == 200
    except Exception:
        available = False

    TTS_BACKEND["satellite_available"] = available
    TTS_BACKEND["last_health_check"] = now
    if available:
        logger.info("TTS: Satellite available for WSL TTS")
    return available


# Phone TTS routing config (MacroDroid HTTP server on phone via Tailscale)
PHONE_TTS_CONFIG = {
    "host": "100.102.92.24",
    "port": 7777,
    "timeout": 2,
    "reachable": None,          # True/False/None (unknown)
    "last_health_check": 0,
    "health_check_ttl": 30,     # Re-probe phone every 30s
}


def is_phone_reachable() -> bool:
    """Check if the phone's MacroDroid HTTP server is reachable. Cached with 30s TTL."""
    import requests

    now = time.time()
    if (PHONE_TTS_CONFIG["reachable"] is not None
            and now - PHONE_TTS_CONFIG["last_health_check"] < PHONE_TTS_CONFIG["health_check_ttl"]):
        return PHONE_TTS_CONFIG["reachable"]

    host = PHONE_TTS_CONFIG["host"]
    port = PHONE_TTS_CONFIG["port"]
    try:
        # Use /heartbeat — hitting /notify fires the Notify macro (vibrate + TTS)
        # which produced spurious vibrations on every health probe.
        resp = requests.get(f"http://{host}:{port}/heartbeat", timeout=2)
        available = resp.status_code == 200
    except Exception:
        available = False

    PHONE_TTS_CONFIG["reachable"] = available
    PHONE_TTS_CONFIG["last_health_check"] = now
    if available:
        logger.info("TTS: Phone reachable for TTS routing")
    return available


# ============ Phone / Pavlok State ============

PHONE_CONFIG = {
    "host": "100.102.92.24",
    "port": 7777,
    "timeout": 5,
    # === TEST SHIM - REMOVE AFTER TESTING ===
    # Set to True to bypass break time check and force blocking
    "test_force_block": False,
    # =========================================
}

PHONE_STATE = {
    "current_app": None,  # Current distraction app or None
    "last_activity": None,
    "is_distracted": False,
    "reachable": None,  # Last known reachability status
    "last_reachable_check": None,
    "twitter_open_since": None,  # monotonic time when Twitter/X was opened, None when closed
    "twitter_zapped": False,  # True after 7-min zap fires; blocks re-zap until confirmed close
    "twitter_last_zap_at": 0,  # monotonic time of last twitter zap (30-min cooldown)
    "twitter_last_zap_wall": 0,  # wall-clock time.time() of last zap (survives restarts via file)
}

PHONE_HEARTBEAT = {
    "last_seen": None,      # datetime (UTC) or None
    "device_id": None,
    "alert_state": None,    # None, "beep", "zap"
}

PAVLOK_CONFIG = {
    "api_url": "https://api.pavlok.com/api/v5/stimulus/send",
    "token": os.getenv("PAVLOK_API_TOKEN"),
    "enabled": True,
    "cooldown_seconds": 30,
    "default_zap_value": 50,
}

PAVLOK_STATE = {
    "last_stimulus_at": None,
}


# ============ Desktop State ============

DESKTOP_STATE = {
    "current_mode": "silence",
    "last_detection": None,
    # Work mode: MANUAL only (2026-02-26). User explicitly clocks in/out.
    # Values: "clocked_in" (enforcement), "clocked_out" (no enforcement), "gym" (manual gym mode)
    "work_mode": "clocked_in",
    # Location zone tracking (geofence - just tracks where you are, doesn't affect work_mode)
    "location_zone": None,  # None = outside all zones, else: "home", "gym", "campus"
    # Grace period: ignore silence detections for 15s after startup
    "startup_time": time.time(),
    "startup_grace_secs": 15,
    "work_mode_changed_at": None,
    # AHK heartbeat tracking
    "ahk_reachable": None,
    "ahk_last_heartbeat": None,
    # Steam game metadata for the current desktop gaming mode
    "steam_app_id": None,
    "steam_app_name": None,
    "steam_exe": None,
    # Meeting mode: suppresses TTS when in a Zoom/Google Meet call
    "in_meeting": False,
}


# ============ Voice Chat & Dictation State ============
# These live in shared.py so both main.py and routes/voice.py can access them.

# Voice chat state — tracks which instances are in voice conversation mode
VOICE_CHAT_SESSIONS = {}  # instance_id -> {"active": True, "started_at": str}

# Global dictation state — tracks whether Wispr Flow is currently active
# Updated by: AHK script-compiler (~^#Space keyboard toggle), ring-remap (right button),
#             voice-select-other (explicit on/off during voice chat)
DICTATION_STATE = {"active": False, "updated_at": None}

# Pedal state — tracks enter queue and double-tap timing for Stream Deck Pedal
PEDAL_STATE = {
    "last_tap_time": 0.0,          # monotonic time of last left-pedal tap
    "enter_queued": False,          # enter waiting for dictation buffer to expire
    "queued_task": None,            # asyncio.Task for delayed enter send
    "bypass_active": False,         # single-tap bypass window after buffered enter
    "bypass_start": 0.0,           # when bypass window started
}
PEDAL_DOUBLE_TAP_MS = 500          # double-tap window
PEDAL_BUFFER_MS = 1.0              # seconds to wait after dictation ends before sending queued enter
PEDAL_BYPASS_MS = 10.0             # seconds of single-tap bypass after buffered enter


# ============ Device Resolution ============

DEVICE_IPS = {
    "100.102.92.24": "Token-S24",    # Phone
    "100.69.198.87": "TokenPC",      # Windows PC
    "100.66.10.74": "TokenPC",       # WSL (same physical machine)
    "100.95.109.23": "Mac-Mini",     # Mac Mini (Tailscale)
    "127.0.0.1": "Mac-Mini",         # Mac Mini (localhost)
}

LOCAL_DEVICES = {"desktop", "Mac-Mini", "TokenPC"}


def resolve_device_from_ip(ip: str) -> str:
    """Map Tailscale IPs to known devices."""
    return DEVICE_IPS.get(ip, "unknown")


def is_local_device(device_id: str) -> bool:
    """Check if device_id refers to a machine where we can manage processes locally."""
    return device_id in LOCAL_DEVICES


# ============ Process Utilities ============

def is_pid_claude(pid: int) -> bool:
    """Check if the given PID belongs to a claude process."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip() == "claude"
    except (OSError, PermissionError):
        return False


def get_parent_pid(pid: int) -> Optional[int]:
    """Get the parent PID of a process from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().split()
            return int(fields[3])
    except (OSError, ValueError, IndexError):
        return None


def is_subagent_pid(pid: int) -> bool:
    """Return True if this claude process was spawned by another claude process."""
    parent = get_parent_pid(pid)
    return bool(parent and parent != 1 and is_pid_claude(parent))


# ============ Discord ============

DISCORD_DAEMON_URL = "http://127.0.0.1:7779"


# ============ Event Logging ============

async def log_event(event_type: str, instance_id: str = None, device_id: str = None, details: dict = None):
    """Log an event to the events table."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO events (event_type, instance_id, device_id, details)
               VALUES (?, ?, ?, ?)""",
            (event_type, instance_id, device_id, json.dumps(details) if details else None)
        )
        await db.commit()


async def log_event_sync(event_type: str, instance_id: str = None, device_id: str = None, details: dict = None):
    """Synchronous wrapper for logging events (for use in sync functions)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO events (event_type, instance_id, device_id, details)
               VALUES (?, ?, ?, ?)""",
            (event_type, instance_id, device_id, json.dumps(details) if details else None)
        )
        await db.commit()


async def append_workflow_event(
    db,
    *,
    instance_id: str,
    event_type: str,
    workflow_state: str | None = None,
    event_owner: str | None = None,
    details: dict | None = None,
):
    """Append a machine-readable workflow event using an existing DB connection."""
    await db.execute(
        """INSERT INTO workflow_events (instance_id, workflow_state, event_type, event_owner, details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (
            instance_id,
            workflow_state,
            event_type,
            event_owner,
            json.dumps(details) if details else None,
        ),
    )


# ============ App Singletons ============
# Set by main.py after module-level initialization.
# hooks.py and other route modules import via `import shared; shared.timer_engine.xxx`
# instead of reaching back through the _main() lazy import.
timer_engine = None   # token_api.timer.TimerEngine
scheduler = None      # apscheduler.schedulers.asyncio.AsyncIOScheduler


# ============ Timer Analytics ============

def _sync_log_shift(old_mode, new_mode: str, trigger: str, source: str,
                    phone_app=None, details=None):
    """Log a timer mode shift to the analytics table (sync, for thread offload)."""
    import sqlite3
    from datetime import datetime as _dt
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")

    cursor = conn.execute(
        "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
    )
    active_instances = cursor.fetchone()[0]

    conn.execute(
        """INSERT INTO timer_shifts (timestamp, old_mode, new_mode, trigger, source,
           break_balance_ms, break_backlog_ms, work_time_ms, active_instances, phone_app, details)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_dt.now().isoformat(), old_mode, new_mode, trigger, source,
         timer_engine.break_balance_ms, abs(min(0, timer_engine.break_balance_ms)),
         timer_engine.total_work_time_ms, active_instances, phone_app, details)
    )
    conn.commit()
    conn.close()


async def timer_log_shift(old_mode, new_mode: str, trigger: str, source: str,
                          phone_app=None, details=None):
    """Log a timer mode shift to the analytics table (async wrapper)."""
    import asyncio
    try:
        await asyncio.to_thread(_sync_log_shift, old_mode, new_mode, trigger, source, phone_app, details)
    except Exception as e:
        print(f"TIMER: Failed to log shift: {e}")
