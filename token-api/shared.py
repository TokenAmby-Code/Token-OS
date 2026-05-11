"""
Shared state, configuration, and utilities for Token-API.

This module exists to break circular imports between main.py and route modules.
State dicts live here; both main.py and routes/* import from this module.

Phase 2 will convert these raw dicts into TypedDicts/dataclasses.
"""

import asyncio
import json
import logging
import os
import random
import sqlite3
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("token_api")
_LOG_EVENT_WRITE_LOCK = threading.Lock()

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
QUIET_HOURS_START = int(os.environ.get("TOKEN_API_QUIET_START_HOUR", "23"))
QUIET_HOURS_END = int(os.environ.get("TOKEN_API_QUIET_END_HOUR", "9"))
QUIET_HOURS_TIMEZONE = os.environ.get("TOKEN_API_QUIET_TIMEZONE", "America/Phoenix")
_DAY_STATE_CACHE_TTL_SECONDS = 60.0
_DAY_STATE_CACHE: dict[str, object] = {"date": None, "value": None, "monotonic": 0.0}


def _in_running_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


async def _refresh_day_state_cache(date_str: str) -> None:
    await asyncio.to_thread(get_day_state_sync, date_str)


def quiet_hours_local_now(now: datetime | None = None) -> datetime:
    """Return ``now`` normalized to the quiet-hours timezone."""
    tz = ZoneInfo(QUIET_HOURS_TIMEZONE)
    local_now = now or datetime.now(tz)
    if local_now.tzinfo is None:
        return local_now.replace(tzinfo=tz)
    return local_now.astimezone(tz)


def _quiet_hour_window_active(local_now: datetime) -> tuple[bool, str]:
    """Return whether the configured hour window is active and which segment fired."""
    start = QUIET_HOURS_START
    end = QUIET_HOURS_END
    hour_float = local_now.hour + local_now.minute / 60 + local_now.second / 3600

    if start == end:
        return True, "all_day"
    if start < end:
        active = start <= hour_float < end
        return active, "same_day" if active else "outside"

    if hour_float >= start:
        return True, "night_start"
    if hour_float < end:
        return True, "morning_latch"
    return False, "outside"


def ensure_day_state_table_sync(db_path: Path | None = None) -> None:
    """Create the day-state table used by the day-start hook if needed."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS day_state (
                date TEXT PRIMARY KEY,
                day_started_at TEXT,
                source TEXT,
                details_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


async def ensure_day_state_table(db_path: Path | None = None) -> None:
    """Async wrapper for creating the day-state table."""
    await asyncio.to_thread(ensure_day_state_table_sync, db_path)


def get_day_state_sync(date_str: str | None = None, db_path: Path | None = None) -> dict | None:
    """Return the persisted day-state row for ``date_str`` (local date by default)."""
    local_date = date_str or quiet_hours_local_now().date().isoformat()
    try:
        ensure_day_state_table_sync(db_path)
        with sqlite3.connect(db_path or DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM day_state WHERE date = ?", (local_date,)).fetchone()
        result = dict(row) if row else None
        if db_path is None or Path(db_path) == DB_PATH:
            _DAY_STATE_CACHE.update(
                {"date": local_date, "value": result, "monotonic": time.monotonic()}
            )
        return result
    except Exception as exc:
        logger.warning("day_state read failed: %s", exc)
        return None


async def get_day_state(date_str: str | None = None, db_path: Path | None = None) -> dict | None:
    """Async wrapper for reading the day-state row."""
    return await asyncio.to_thread(get_day_state_sync, date_str, db_path)


def set_day_started_at_sync(
    *,
    source: str = "manual",
    at: datetime | None = None,
    details: dict | None = None,
    force: bool = False,
    db_path: Path | None = None,
) -> dict:
    """Persist the local day's day-start timestamp and return the resulting state."""
    local_at = quiet_hours_local_now(at)
    date_str = local_at.date().isoformat()
    timestamp = local_at.isoformat()
    details_json = json.dumps(details or {}, sort_keys=True)

    ensure_day_state_table_sync(db_path)
    with sqlite3.connect(db_path or DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        existing = conn.execute("SELECT * FROM day_state WHERE date = ?", (date_str,)).fetchone()
        if existing and existing["day_started_at"] and not force:
            row = dict(existing)
            row["already_started"] = True
            row["updated"] = False
            return row

        if existing:
            conn.execute(
                """
                UPDATE day_state
                SET day_started_at = ?, source = ?, details_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE date = ?
                """,
                (timestamp, source, details_json, date_str),
            )
        else:
            conn.execute(
                """
                INSERT INTO day_state (date, day_started_at, source, details_json)
                VALUES (?, ?, ?, ?)
                """,
                (date_str, timestamp, source, details_json),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM day_state WHERE date = ?", (date_str,)).fetchone()

    result = dict(row)
    result["already_started"] = False
    result["updated"] = True
    if db_path is None or Path(db_path) == DB_PATH:
        _DAY_STATE_CACHE.update(
            {"date": date_str, "value": result, "monotonic": time.monotonic()}
        )
    return result


async def set_day_started_at(
    *,
    source: str = "manual",
    at: datetime | None = None,
    details: dict | None = None,
    force: bool = False,
    db_path: Path | None = None,
) -> dict:
    """Async wrapper for persisting day-start state."""
    return await asyncio.to_thread(
        set_day_started_at_sync,
        source=source,
        at=at,
        details=details,
        force=force,
        db_path=db_path,
    )


def get_quiet_hours_status(now: datetime | None = None) -> dict:
    """Return the canonical quiet-hours decision and context.

    The 23:00 sleep-start gate remains hour based. The morning end is event
    driven: once ``day_state.day_started_at`` exists for the local date, the
    morning quiet-hours latch is released before the configured fallback end.
    """
    local_now = quiet_hours_local_now(now)
    active, segment = _quiet_hour_window_active(local_now)
    day_state = None
    local_date = local_now.date().isoformat()
    cached_at = float(_DAY_STATE_CACHE.get("monotonic") or 0.0)
    if (
        _DAY_STATE_CACHE.get("date") == local_date
        and time.monotonic() - cached_at <= _DAY_STATE_CACHE_TTL_SECONDS
    ):
        day_state = _DAY_STATE_CACHE.get("value")
    elif _in_running_event_loop():
        # This function is used by many async hot paths (hooks, timer reads,
        # Golden Throne scheduling). Never perform synchronous SQLite on the
        # event loop; return the last known state and refresh in the background.
        if _DAY_STATE_CACHE.get("date") == local_date:
            day_state = _DAY_STATE_CACHE.get("value")
        try:
            asyncio.create_task(_refresh_day_state_cache(local_date))
        except RuntimeError:
            pass
    else:
        day_state = get_day_state_sync(local_date)
    day_started_at = day_state.get("day_started_at") if day_state else None

    if active and segment == "morning_latch" and day_started_at:
        active = False
        reason = "day_started"
    elif active:
        reason = "quiet_hours"
    else:
        reason = "outside_quiet_hours"

    return {
        "active": active,
        "reason": reason,
        "quiet_start": QUIET_HOURS_START,
        "quiet_end": QUIET_HOURS_END,
        "timezone": QUIET_HOURS_TIMEZONE,
        "local_time": local_now.isoformat(),
        "day_started_at": day_started_at,
        "day_state_date": local_now.date().isoformat(),
        "quiet_segment": segment,
    }


# ============ Voice Profiles ============

PROFILES = [
    {
        "name": "profile_1",
        "wsl_voice": "Microsoft George",
        "wsl_rate": 2,
        "mac_voice": "Daniel",
        "notification_sound": "chimes.wav",
        "color": "#66cccc",
        "cc_color": "cyan",
    },  # UK M
    {
        "name": "profile_2",
        "wsl_voice": "Microsoft Susan",
        "wsl_rate": 1,
        "mac_voice": "Karen",
        "notification_sound": "notify.wav",
        "color": "#ff66cc",
        "cc_color": "pink",
    },  # UK F
    {
        "name": "profile_3",
        "wsl_voice": "Microsoft Catherine",
        "wsl_rate": 1,
        "mac_voice": "Karen",
        "notification_sound": "ding.wav",
        "color": "#ffcc00",
        "cc_color": "yellow",
    },  # AU F
    {
        "name": "profile_5",
        "wsl_voice": "Microsoft Sean",
        "wsl_rate": 0,
        "mac_voice": "Moira",
        "notification_sound": "chord.wav",
        "color": "#ff9900",
        "cc_color": "orange",
    },  # IE M
    {
        "name": "profile_7",
        "wsl_voice": "Microsoft Heera",
        "wsl_rate": 1,
        "mac_voice": "Rishi",
        "notification_sound": "chimes.wav",
        "color": "#cc66ff",
        "cc_color": "purple",
    },  # IN F
    {
        "name": "profile_8",
        "wsl_voice": "Microsoft Ravi",
        "wsl_rate": 1,
        "mac_voice": "Rishi",
        "notification_sound": "notify.wav",
        "color": "#ff6666",
        "cc_color": "red",
    },  # IN M
]

FALLBACK_VOICES = [
    {
        "name": "fallback_1",
        "wsl_voice": "Microsoft David",
        "wsl_rate": 1,
        "mac_voice": "Daniel",
        "notification_sound": "tada.wav",
        "color": "#888888",
        "cc_color": "default",
    },
    {
        "name": "fallback_2",
        "wsl_voice": "Microsoft Zira",
        "wsl_rate": 1,
        "mac_voice": "Karen",
        "notification_sound": "chord.wav",
        "color": "#999999",
        "cc_color": "default",
    },
    {
        "name": "fallback_3",
        "wsl_voice": "Microsoft Mark",
        "wsl_rate": 1,
        "mac_voice": "Daniel",
        "notification_sound": "recycle.wav",
        "color": "#aaaaaa",
        "cc_color": "default",
    },
]

ULTIMATE_FALLBACK = {
    "name": "fallback_david",
    "wsl_voice": "Microsoft David",
    "wsl_rate": 1,
    "mac_voice": "Daniel",
    "notification_sound": "chimes.wav",
    "color": "#666666",
    "cc_color": "default",
}


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
    "current": None,  # "wsl" | "mac" | None — what's currently speaking
    "satellite_available": None,  # True/False/None (unknown)
    "last_health_check": 0,
    "health_check_ttl": 30,  # Re-probe satellite every 30s
}

# Global TTS mute state (in-memory, resets to "verbose" on server restart)
TTS_GLOBAL_MODE = {
    "mode": "verbose",  # "verbose" | "muted" | "silent"
}


def is_satellite_tts_available() -> bool:
    """Check if the WSL satellite TTS endpoint is reachable. Cached with 30s TTL."""
    import requests

    now = time.time()
    if (
        TTS_BACKEND["satellite_available"] is not None
        and now - TTS_BACKEND["last_health_check"] < TTS_BACKEND["health_check_ttl"]
    ):
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


_TMUX_PANE_RESOLVE_CACHE: dict[str, tuple[float, str | None]] = {}
_TMUX_PANE_RESOLVE_TTL = 0.75


async def _run_subprocess_offloop(
    args: list[str] | tuple[str, ...],
    *,
    timeout: float | None = None,
    stdout=None,
    stderr=None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run short tmux resolver subprocesses without forking on the event loop."""
    return await asyncio.to_thread(
        subprocess.run,
        list(args),
        stdout=stdout,
        stderr=stderr,
        env=env,
        timeout=timeout,
        check=False,
    )


async def _resolve_tmux_pane_direct(tmux_pane: str) -> str | None:
    try:
        proc = await _run_subprocess_offloop(
            ("tmux", "display-message", "-t", tmux_pane, "-p", "#{pane_id}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=1,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    pane_id = proc.stdout.decode(errors="ignore").strip()
    return pane_id or None


async def resolve_tmux_pane_id(tmux_pane: str | None) -> str | None:
    """Return the live %pane id for a tmux target, following tombstones when present."""
    if not tmux_pane:
        return None
    now = time.time()
    cached = _TMUX_PANE_RESOLVE_CACHE.get(tmux_pane)
    if cached and now - cached[0] < _TMUX_PANE_RESOLVE_TTL:
        return cached[1]
    if tmux_pane.startswith("%"):
        pane_id = await _resolve_tmux_pane_direct(tmux_pane)
        _TMUX_PANE_RESOLVE_CACHE[tmux_pane] = (now, pane_id)
        return pane_id
    cli_lib = Path(__file__).resolve().parents[1] / "cli-tools" / "lib"
    try:
        proc = await _run_subprocess_offloop(
            ("python3", "-m", "tmuxctl.cli", "resolve-pane", tmux_pane),
            env={
                **os.environ,
                "PYTHONPATH": f"{cli_lib}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=3,
        )
        if proc.returncode == 0:
            for line in proc.stdout.decode(errors="ignore").splitlines():
                if line.startswith("pane_id: "):
                    pane_id = line.split(": ", 1)[1].strip()
                    if pane_id:
                        _TMUX_PANE_RESOLVE_CACHE[tmux_pane] = (now, pane_id)
                        return pane_id
    except Exception:
        pass
    pane_id = await _resolve_tmux_pane_direct(tmux_pane)
    _TMUX_PANE_RESOLVE_CACHE[tmux_pane] = (now, pane_id)
    return pane_id


async def tmux_pane_exists(tmux_pane: str | None) -> bool:
    return await resolve_tmux_pane_id(tmux_pane) is not None


# Phone TTS routing config (MacroDroid HTTP server on phone via Tailscale)
PHONE_TTS_CONFIG = {
    "host": "100.102.92.24",
    "port": 7777,
    "timeout": 2,
    "reachable": None,  # True/False/None (unknown)
    "last_health_check": 0,
    "health_check_ttl": 30,  # Re-probe phone every 30s
}


def is_phone_reachable() -> bool:
    """Check if the phone's MacroDroid HTTP server is reachable. Cached with 30s TTL."""
    import requests

    now = time.time()
    if (
        PHONE_TTS_CONFIG["reachable"] is not None
        and now - PHONE_TTS_CONFIG["last_health_check"] < PHONE_TTS_CONFIG["health_check_ttl"]
    ):
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
    "distraction_ack_app": None,  # app currently covered by a phone_distraction expected ack
    "distraction_ack_id": None,
}

PHONE_HEARTBEAT = {
    "last_seen": None,  # datetime (UTC) or None
    "device_id": None,
    "alert_state": None,  # None, "beep", "zap"
}

PAVLOK_CONFIG = {
    "api_url": "https://api.pavlok.com/api/v5/stimulus/send",
    "token": os.getenv("PAVLOK_API_TOKEN"),
    "enabled": True,
    "cooldown_seconds": 30,
    "zap_cooldown_seconds": 20 * 60,
    "soft_cooldown_seconds": 3 * 60,
    "daily_zap_cap": 6,
    "default_zap_value": 50,
    "friday_zap_value": 30,
    "warning_value": 50,
}

PAVLOK_STATE = {
    "last_stimulus_at": None,
    "last_zap_at": None,
    "last_soft_at": None,
    "zap_count_date": None,
    "zap_count": 0,
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

# AskUserQuestion three-touch ladder state — engagement-pressure on per-instance questions.
# instance_id -> {
#   "question_text": str,
#   "options": list[str],
#   "started_at": float (monotonic),
#   "current_touch": int (1-3) | "bust",
#   "task": asyncio.Task | None  (the running ladder coroutine),
#   "tmux_pane": str | None,
#   "device_id": str | None,
#   "tts_voice": str | None,
# }
ASKQ_LADDER = {}

# Ladder sleep durations (seconds) between escalation levels.
# Compressed defaults aligned with the GT-compression session's expected_ack ladder
# (1.5 / 3 / 3 minutes). Total time to pavlok = T1 + T2 + T3 = 7.5 min.
ASKQ_T1_SECONDS = 90  # arm + initial TTS → Level 1 (TTS reminder + Discord nudge)
ASKQ_T2_SECONDS = 180  # Level 1 → Level 2 (enforcement cascade + persist Unanswered)
ASKQ_T3_SECONDS = 180  # Level 2 → Level 3 (pavlok shock + autonomous fallback prompt)

# Minimum zealotry for golden_throne instances to engage the ladder.
# Voice-chat sessions engage regardless of zealotry.
ASKQ_MIN_ZEALOTRY = 4

ASKQ_BUST_PROMPT = (
    "Question timed out. Move autonomously for a moment — update documentation, "
    "run tests, validate, pick low-hanging fruit. If this is blocking, use the "
    "notification protocol to escalate."
)

# Global dictation state — tracks whether Wispr Flow is currently active
# Updated by: AHK script-compiler (~^#Space keyboard toggle), ring-remap (right button),
#             voice-select-other (explicit on/off during voice chat)
DICTATION_STATE = {"active": False, "updated_at": None}

# Pedal state — tracks enter queue and double-tap timing for Stream Deck Pedal
PEDAL_STATE = {
    "last_tap_time": 0.0,  # monotonic time of last left-pedal tap
    "enter_queued": False,  # enter waiting for dictation buffer to expire
    "queued_task": None,  # asyncio.Task for delayed enter send
    "bypass_active": False,  # single-tap bypass window after buffered enter
    "bypass_start": 0.0,  # when bypass window started
}
PEDAL_DOUBLE_TAP_MS = 500  # double-tap window
PEDAL_BUFFER_MS = 1.0  # seconds to wait after dictation ends before sending queued enter
PEDAL_BYPASS_MS = 10.0  # seconds of single-tap bypass after buffered enter


# ============ Device Resolution ============

DEVICE_IPS = {
    "100.102.92.24": "Token-S24",  # Phone
    "100.69.198.87": "TokenPC",  # Windows PC
    "100.66.10.74": "TokenPC",  # WSL (same physical machine)
    "100.95.109.23": "Mac-Mini",  # Mac Mini (Tailscale)
    "127.0.0.1": "Mac-Mini",  # Mac Mini (localhost)
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


def get_parent_pid(pid: int) -> int | None:
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


def _log_event_sync_insert(
    event_type: str, instance_id: str = None, device_id: str = None, details: dict = None
) -> None:
    """Serialize event writes in a worker thread.

    `log_event()` is called by hook/heartbeat hot paths. Letting each call open
    its own aiosqlite worker creates write-lock storms under hook bursts; doing
    the sqlite write behind one process-local lock keeps the asyncio loop free
    and prevents the aiosqlite worker/thread pileup that was timing endpoints.
    """
    details_json = json.dumps(details, default=str) if details else None
    with _LOG_EVENT_WRITE_LOCK:
        for attempt in range(3):
            try:
                with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                    conn.execute("PRAGMA busy_timeout=5000")
                    conn.execute(
                        """INSERT INTO events (event_type, instance_id, device_id, details)
                           VALUES (?, ?, ?, ?)""",
                        (event_type, instance_id, device_id, details_json),
                    )
                    conn.commit()
                    return
            except sqlite3.OperationalError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))


async def log_event(
    event_type: str, instance_id: str = None, device_id: str = None, details: dict = None
):
    """Log an event to the events table."""
    try:
        await asyncio.to_thread(_log_event_sync_insert, event_type, instance_id, device_id, details)
    except Exception as exc:
        # Event logs are telemetry. Do not let a transient SQLite lock take down
        # hook/HTTP hot paths or trigger recursive hook_error logging.
        logger.warning("event log dropped (%s): %s", event_type, exc)


async def log_event_sync(
    event_type: str, instance_id: str = None, device_id: str = None, details: dict = None
):
    """Synchronous wrapper for logging events (for use in sync functions)."""
    await log_event(event_type, instance_id, device_id, details)


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
timer_engine = None  # token_api.timer.TimerEngine
scheduler = None  # apscheduler.schedulers.asyncio.AsyncIOScheduler


# ============ Timer Analytics ============


def _serialize_timer_shift_details(details):
    """Return a SQLite-safe value for timer_shifts.details."""
    if details is None:
        return None
    if isinstance(details, str):
        return details
    if isinstance(details, (dict, list, tuple)):
        return json.dumps(details, sort_keys=True, default=str)
    if isinstance(details, (bool, int, float)):
        return json.dumps(details)
    return str(details)


def _sync_log_shift(
    old_mode, new_mode: str, trigger: str, source: str, phone_app=None, details=None
):
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
        (
            _dt.now().isoformat(),
            old_mode,
            new_mode,
            trigger,
            source,
            timer_engine.break_balance_ms,
            abs(min(0, timer_engine.break_balance_ms)),
            timer_engine.total_work_time_ms,
            active_instances,
            phone_app,
            _serialize_timer_shift_details(details),
        ),
    )
    conn.commit()
    conn.close()


async def timer_log_shift(
    old_mode, new_mode: str, trigger: str, source: str, phone_app=None, details=None
):
    """Log a timer mode shift to the analytics table (async wrapper)."""
    import asyncio

    try:
        await asyncio.to_thread(
            _sync_log_shift, old_mode, new_mode, trigger, source, phone_app, details
        )
    except Exception as e:
        print(f"TIMER: Failed to log shift: {e}")
