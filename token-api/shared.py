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
    # Meeting mode: suppresses TTS when in a Zoom/Google Meet call
    "in_meeting": False,
}


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
