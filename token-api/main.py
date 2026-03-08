"""
Token-API: FastAPI Local Server for Claude Instance Management

This server provides:
- Claude instance registration and tracking
- Device identification (desktop vs SSH from phone)
- Notification routing
- Productivity gating
"""

import os
import re
import uuid
import json
import time
import signal
import random
import asyncio
import functools
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import aiosqlite
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import tempfile
import requests
import httpx
from pydantic import BaseModel, Field
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from cron_engine import CronEngine
from timer import (
    TimerEngine, TimerMode, TimerEvent, Activity,
    format_timer_time, IDLE_TO_BREAK_TIMEOUT_MS, DEFAULT_BREAK_BUFFER_MS,
)

# Configure logging for TUI capture
logger = logging.getLogger("token_api")
logger.setLevel(logging.INFO)

# ============ Server-side Log Buffer ============
from collections import deque
from typing import Deque

# Circular buffer to store recent log entries (max 100)
log_buffer: Deque[dict] = deque(maxlen=100)


class LogBufferHandler(logging.Handler):
    """Custom logging handler that captures logs to circular buffer."""

    def emit(self, record: logging.LogRecord):
        """Capture log record to buffer with timestamp, level, and message."""
        try:
            log_entry = {
                "timestamp": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": self.format(record)
            }
            log_buffer.append(log_entry)
        except Exception:
            # Silently fail to avoid logging errors in logging system
            pass


# Add buffer handler to logger
buffer_handler = LogBufferHandler()
buffer_handler.setLevel(logging.DEBUG)
buffer_handler.setFormatter(logging.Formatter('%(message)s'))
logger.addHandler(buffer_handler)

# Also capture uvicorn and fastapi logs
uvicorn_logger = logging.getLogger("uvicorn")
uvicorn_logger.addHandler(buffer_handler)

fastapi_logger = logging.getLogger("fastapi")
fastapi_logger.addHandler(buffer_handler)


# Configuration
DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))
DEFAULT_SESSIONS_DIR = Path.home() / "Imperium-ENV" / "Terra" / "Sessions"
SERVER_PORT = 7777  # Authoritative port for Token API
CRASH_LOG_PATH = Path.home() / ".claude" / "token-api-crash.log"
STASH_DIR = Path.home() / ".claude" / "stash"
STASH_MAX_AGE_HOURS = 24


# ============ Crash Logging ============
import sys
import traceback


def log_crash(exc_type, exc_value, exc_tb, context: str = "unhandled"):
    """Write crash info to persistent file for post-mortem debugging."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        tb_str = "".join(tb_lines)

        with open(CRASH_LOG_PATH, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"CRASH [{context}] at {timestamp}\n")
            f.write(f"{'='*60}\n")
            f.write(tb_str)
            f.write("\n")

        # Also print to stderr so journald captures it
        print(f"CRASH [{context}]: {exc_type.__name__}: {exc_value}", file=sys.stderr)
    except Exception:
        pass  # Don't crash while logging a crash


def _global_exception_handler(exc_type, exc_value, exc_tb):
    """Global exception handler for uncaught sync exceptions."""
    log_crash(exc_type, exc_value, exc_tb, context="sync")
    # Call the default handler to preserve normal behavior
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def _asyncio_exception_handler(loop, context):
    """Handler for uncaught exceptions in asyncio tasks."""
    exception = context.get("exception")
    if exception:
        log_crash(type(exception), exception, exception.__traceback__, context="asyncio")
    else:
        # Log context message if no exception object
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(CRASH_LOG_PATH, "a") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"ASYNCIO ERROR at {timestamp}\n")
                f.write(f"{'='*60}\n")
                f.write(f"{context}\n\n")
        except Exception:
            pass

    # Call the default handler
    loop.default_exception_handler(context)


# Install global exception handlers
sys.excepthook = _global_exception_handler

# Device IP mapping for SSH detection
DEVICE_IPS = {
    "100.102.92.24": "Token-S24",    # Phone
    "100.69.198.87": "TokenPC",      # Windows PC
    "100.66.10.74": "TokenPC",       # WSL (same physical machine)
    "100.95.109.23": "Mac-Mini",     # Mac Mini (Tailscale)
    "127.0.0.1": "Mac-Mini",         # Mac Mini (localhost)
}

# Voice pool: foreign-accent voices are the primary pool, assigned via linear probe.
# US English voices (David, Zira, Mark) are fallback-only when pool is exhausted.
# Ultimate fallback if everything is taken: David.
PROFILES = [
    {"name": "profile_1", "wsl_voice": "Microsoft George",   "wsl_rate": 2, "mac_voice": "Daniel", "notification_sound": "chimes.wav", "color": "#0099ff"},   # UK M
    {"name": "profile_2", "wsl_voice": "Microsoft Susan",    "wsl_rate": 1, "mac_voice": "Karen",  "notification_sound": "notify.wav", "color": "#00cc66"},   # UK F
    {"name": "profile_3", "wsl_voice": "Microsoft Catherine", "wsl_rate": 1, "mac_voice": "Karen", "notification_sound": "ding.wav",   "color": "#ff9900"},   # AU F
    {"name": "profile_4", "wsl_voice": "Microsoft James",    "wsl_rate": 1, "mac_voice": "Daniel", "notification_sound": "tada.wav",   "color": "#cc66ff"},   # AU M
    {"name": "profile_5", "wsl_voice": "Microsoft Sean",     "wsl_rate": 0, "mac_voice": "Moira",  "notification_sound": "chord.wav",  "color": "#ff6666"},   # IE M
    {"name": "profile_6", "wsl_voice": "Microsoft Hazel",    "wsl_rate": 1, "mac_voice": "Moira",  "notification_sound": "recycle.wav","color": "#66cccc"},   # IE F
    {"name": "profile_7", "wsl_voice": "Microsoft Heera",    "wsl_rate": 1, "mac_voice": "Rishi",  "notification_sound": "chimes.wav", "color": "#ffcc00"},   # IN F
    {"name": "profile_8", "wsl_voice": "Microsoft Ravi",     "wsl_rate": 1, "mac_voice": "Rishi",  "notification_sound": "notify.wav", "color": "#cc99ff"},   # IN M
    {"name": "profile_9", "wsl_voice": "Microsoft Linda",    "wsl_rate": 1, "mac_voice": "Karen",  "notification_sound": "ding.wav",   "color": "#0099ff"},   # CA F
]

# Fallback voices when all foreign accents are exhausted (US English, less distinct)
FALLBACK_VOICES = [
    {"name": "fallback_1", "wsl_voice": "Microsoft David", "wsl_rate": 1, "mac_voice": "Daniel", "notification_sound": "tada.wav",   "color": "#888888"},
    {"name": "fallback_2", "wsl_voice": "Microsoft Zira",  "wsl_rate": 1, "mac_voice": "Karen",  "notification_sound": "chord.wav",  "color": "#999999"},
    {"name": "fallback_3", "wsl_voice": "Microsoft Mark",  "wsl_rate": 1, "mac_voice": "Daniel", "notification_sound": "recycle.wav","color": "#aaaaaa"},
]

# Ultimate fallback when even fallback voices are exhausted
ULTIMATE_FALLBACK = {"name": "fallback_david", "wsl_voice": "Microsoft David", "wsl_rate": 1, "mac_voice": "Daniel", "notification_sound": "chimes.wav", "color": "#666666"}

# Scheduler instance
scheduler = AsyncIOScheduler()

# Cron engine (initialized after DB in lifespan)
cron_engine: CronEngine = None


# Pydantic Models
class InstanceRegisterRequest(BaseModel):
    instance_id: str
    origin_type: str = "local"  # 'local' or 'ssh'
    source_ip: Optional[str] = None
    device_id: Optional[str] = None
    pid: Optional[int] = None
    tab_name: Optional[str] = None
    working_dir: Optional[str] = None


class InstanceResponse(BaseModel):
    id: str
    session_id: str
    tab_name: Optional[str]
    working_dir: Optional[str]
    origin_type: str
    source_ip: Optional[str]
    device_id: str
    profile_name: str
    tts_voice: str
    notification_sound: str
    pid: Optional[int]
    status: str
    registered_at: str
    last_activity: str
    stopped_at: Optional[str]


class ActivityRequest(BaseModel):
    action: str  # "prompt_submit" or "stop"


class ProfileResponse(BaseModel):
    session_id: str
    profile: dict


class DashboardResponse(BaseModel):
    instances: List[dict]
    productivity_active: bool
    recent_events: List[dict]
    tts_queue: Optional[dict] = None  # TTS queue status


class TaskResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    task_type: str
    schedule: str
    enabled: bool
    max_retries: int
    last_run: Optional[dict] = None
    next_run: Optional[str] = None


class TaskUpdateRequest(BaseModel):
    schedule: Optional[str] = None
    enabled: Optional[bool] = None
    max_retries: Optional[int] = None


class TaskExecutionResponse(BaseModel):
    id: int
    task_id: str
    status: str
    started_at: str
    completed_at: Optional[str]
    duration_ms: Optional[int]
    result: Optional[dict]
    retry_count: int


class NotifyRequest(BaseModel):
    message: str
    device_id: Optional[str] = None  # If None, notify based on active instances
    instance_id: Optional[str] = None  # Notify specific instance's device
    voice: Optional[str] = None  # Override TTS voice
    sound: Optional[str] = None  # Override sound file


class TTSRequest(BaseModel):
    message: str
    voice: Optional[str] = None
    rate: int = 0  # -10 to 10, 0 is normal speed
    instance_id: Optional[str] = None  # Track which instance triggered TTS


class SoundRequest(BaseModel):
    sound_file: Optional[str] = None  # Path to sound file


class WindowCheckRequest(BaseModel):
    """Request to check if a window should be allowed or closed."""
    window_title: Optional[str] = None  # e.g., "YouTube - Brave"
    exe_name: Optional[str] = None  # e.g., "brave.exe"
    source: str = "ahk"  # Source of the request


# ============ Audio Proxy Models ============

class AudioProxyState(BaseModel):
    """Current state of the audio proxy system."""
    phone_connected: bool = False
    receiver_running: bool = False
    receiver_pid: Optional[int] = None
    last_connect_time: Optional[str] = None
    last_disconnect_time: Optional[str] = None


class AudioProxyConnectRequest(BaseModel):
    """Request when phone connects to PC Bluetooth."""
    phone_device_id: str = "Token-S24"
    bluetooth_device_name: Optional[str] = None
    source: str = "macrodroid"


class AudioProxyConnectResponse(BaseModel):
    """Response after processing connect request."""
    success: bool
    action: str  # "connected", "already_connected", "error"
    receiver_started: bool
    receiver_pid: Optional[int] = None
    message: str


class AudioProxyDisconnectRequest(BaseModel):
    """Request when phone disconnects from PC Bluetooth."""
    phone_device_id: str = "Token-S24"
    source: str = "macrodroid"


class AudioProxyStatusResponse(BaseModel):
    """Response for status query."""
    phone_connected: bool
    receiver_running: bool
    receiver_pid: Optional[int] = None
    last_connect_time: Optional[str] = None
    last_disconnect_time: Optional[str] = None


class WindowEnforceResponse(BaseModel):
    """Response for window enforcement decision."""
    productivity_active: bool
    active_instance_count: int
    should_close_distractions: bool
    distraction_apps: List[str]  # Apps that should be closed if should_close_distractions is True
    reason: str


class StashContentRequest(BaseModel):
    content: str


class DesktopDetectionRequest(BaseModel):
    """Request from AHK desktop detection."""
    detected_mode: str  # "video" | "music" | "gaming" | "silence"
    window_title: Optional[str] = None
    source: str = "ahk"


class DesktopDetectionResponse(BaseModel):
    """Response for desktop detection."""
    action: str  # "mode_changed" | "blocked" | "none"
    detected_mode: str
    old_mode: Optional[str] = None
    new_mode: Optional[str] = None
    reason: str
    timer_updated: bool = False
    productivity_active: bool
    active_instance_count: int


# ============ Phone Activity Models ============

class PhoneActivityRequest(BaseModel):
    """Request from MacroDroid for phone app activity."""
    app: str  # App name: "twitter", "youtube", "game", or app package name
    action: str = "open"  # "open" | "close"
    package: Optional[str] = None  # Optional package name for games


class PhoneActivityResponse(BaseModel):
    """Response for phone activity detection."""
    allowed: bool
    reason: str  # "break_time_available", "productivity_active", "blocked", "closed"
    break_seconds: int = 0
    message: Optional[str] = None


class PhoneSystemEventRequest(BaseModel):
    """Request from MacroDroid for phone system events (Shizuku, boot, heartbeat)."""
    event: str  # "shizuku_died", "shizuku_restored", "device_boot", "heartbeat"
    time: Optional[str] = None
    server: Optional[str] = None  # heartbeat: server response code
    shizuku_dead: Optional[str] = None  # heartbeat: current shizuku state


# ============ Headless Mode Models ============

class HeadlessStatusResponse(BaseModel):
    """Response for headless mode status."""
    enabled: bool
    last_changed: Optional[str] = None
    hostname: Optional[str] = None
    error: Optional[str] = None
    auto_disable_at: Optional[str] = None  # ISO timestamp when headless will auto-disable


class HeadlessControlRequest(BaseModel):
    """Request to control headless mode."""
    action: str = "toggle"  # "toggle" | "enable" | "disable"
    duration_hours: Optional[float] = None  # Auto-disable after N hours


class HeadlessControlResponse(BaseModel):
    """Response after controlling headless mode."""
    success: bool
    action: str
    before: HeadlessStatusResponse
    after: Optional[HeadlessStatusResponse] = None
    message: str


# ============ System Control Models ============

class ShutdownRequest(BaseModel):
    """Request to shutdown/restart the system."""
    action: str = "shutdown"  # "shutdown" | "restart"
    delay_seconds: int = 0  # Delay before shutdown (0 = immediate)
    force: bool = False  # Force close applications


class ShutdownResponse(BaseModel):
    """Response after initiating shutdown."""
    success: bool
    action: str
    delay_seconds: int
    message: str


# ============ Claude Code Hook Models ============

class HookResponse(BaseModel):
    """Standard response for hook handlers."""
    success: bool = True
    action: str
    details: Optional[dict] = None


class PreToolUseResponse(BaseModel):
    """Response for PreToolUse hooks that can block operations."""
    permissionDecision: Optional[str] = None  # "allow" or "deny"
    permissionDecisionReason: Optional[str] = None


class DiscordMessageRequest(BaseModel):
    """Forwarded Discord message from the discord-cli daemon."""
    message_id: Optional[str] = None
    channel_id: str
    channel_name: Optional[str] = None
    guild_id: Optional[str] = None
    author: Optional[dict] = None
    content: str
    timestamp: Optional[str] = None
    is_dm: bool = False
    is_reply: bool = False
    reply_to_message_id: Optional[str] = None
    attachments: Optional[list] = None
    embeds: Optional[int] = 0


class InboxNotifyRequest(BaseModel):
    """Gene-seed birth notification for a new inbox note."""
    path: str
    title: str
    type: str = "capture"
    source: str = "obsidian"


class InboxCreateRequest(BaseModel):
    """Create an aspirant note from external source (Discord, API, hotkey)."""
    title: str = ""
    type: str = "capture"
    content: str = ""
    source: str = "discord"
    author: Optional[str] = None


class SessionDocCreateRequest(BaseModel):
    title: str
    project: Optional[str] = None
    file_path: Optional[str] = None
    primarch_name: Optional[str] = None


class SessionDocUpdateRequest(BaseModel):
    title: Optional[str] = None
    project: Optional[str] = None
    status: Optional[str] = None


class SessionDocMergeRequest(BaseModel):
    content: str
    source: str = "agent"
    context: Optional[str] = None


# ============ Hook Handler State ============
# Debouncing for PostToolUse to avoid excessive API calls
_post_tool_debounce: dict = {}  # session_id -> last_call_time

# Tracks background Task subagents still awaiting result delivery.
# Incremented in handle_pre_tool_use, decremented in handle_prompt_submit.
_pending_background_tasks: dict = {}  # session_id -> count


# Database helper: connect with busy_timeout to prevent indefinite blocking
async def get_db():
    """Get a database connection with busy_timeout configured."""
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA busy_timeout=5000")
    return db


# Database initialization
async def init_db():
    """Initialize SQLite database with required tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DB_PATH) as db:
        # Set busy_timeout to prevent blocking on lock contention
        await db.execute("PRAGMA busy_timeout=5000")
        # Create claude_instances table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS claude_instances (
                id TEXT PRIMARY KEY,
                session_id TEXT UNIQUE NOT NULL,
                tab_name TEXT,
                working_dir TEXT,
                origin_type TEXT NOT NULL,
                source_ip TEXT,
                device_id TEXT NOT NULL,
                profile_name TEXT,
                tts_voice TEXT,
                notification_sound TEXT,
                pid INTEGER,
                status TEXT DEFAULT 'idle',
                is_processing INTEGER DEFAULT 0,
                registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                stopped_at TIMESTAMP
            )
        """)

        # Migration: add is_processing column if it doesn't exist
        cursor = await db.execute("PRAGMA table_info(claude_instances)")
        columns = [col[1] for col in await cursor.fetchall()]
        if 'is_processing' not in columns:
            await db.execute("ALTER TABLE claude_instances ADD COLUMN is_processing INTEGER DEFAULT 0")
        if 'working_dir' not in columns:
            await db.execute("ALTER TABLE claude_instances ADD COLUMN working_dir TEXT")
        if 'tts_mode' not in columns:
            await db.execute("ALTER TABLE claude_instances ADD COLUMN tts_mode TEXT DEFAULT 'verbose'")
        if 'session_doc_id' not in columns:
            await db.execute("ALTER TABLE claude_instances ADD COLUMN session_doc_id INTEGER")

        # Migration: add primarch_name to session_documents
        cursor = await db.execute("PRAGMA table_info(session_documents)")
        sd_columns = [col[1] for col in await cursor.fetchall()]
        if 'primarch_name' not in sd_columns:
            await db.execute("ALTER TABLE session_documents ADD COLUMN primarch_name TEXT")

        # Migration: Convert two-field status (status + is_processing) to single enum
        # Old: status='active' + is_processing=0/1 → New: status='processing'/'idle'/'stopped'
        cursor = await db.execute("SELECT COUNT(*) FROM claude_instances WHERE status = 'active'")
        if (await cursor.fetchone())[0] > 0:
            await db.execute("""
                UPDATE claude_instances SET status = CASE
                    WHEN status = 'active' AND is_processing = 1 THEN 'processing'
                    WHEN status = 'active' AND is_processing = 0 THEN 'idle'
                    ELSE status
                END
            """)
            await db.commit()

        await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_status ON claude_instances(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_instances_device ON claude_instances(device_id)")

        # Create devices table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                tailscale_ip TEXT UNIQUE,
                notification_method TEXT,
                webhook_url TEXT,
                tts_engine TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create events table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                instance_id TEXT,
                device_id TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON events(created_at DESC)")

        # Create scheduled_tasks table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                task_type TEXT NOT NULL,
                schedule TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                max_retries INTEGER DEFAULT 0,
                retry_delay_seconds INTEGER DEFAULT 60,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create task_executions table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP,
                duration_ms INTEGER,
                result TEXT,
                retry_count INTEGER DEFAULT 0,
                FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
            )
        """)

        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_task_id ON task_executions(task_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_task_executions_started_at ON task_executions(started_at)")

        # Create task_locks table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS task_locks (
                task_id TEXT PRIMARY KEY,
                locked_at TIMESTAMP NOT NULL,
                locked_by TEXT,
                FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
            )
        """)

        # Create audio_proxy_state table (for phone audio routing through PC)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audio_proxy_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                phone_connected INTEGER DEFAULT 0,
                receiver_running INTEGER DEFAULT 0,
                receiver_pid INTEGER,
                last_connect_time TEXT,
                last_disconnect_time TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CHECK (id = 1)
            )
        """)

        # Create timer_state table (single-row, stores timer engine state as JSON)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Timer session logging - track work/break sessions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP,
                mode TEXT NOT NULL,
                duration_ms INTEGER DEFAULT 0,
                break_earned_ms INTEGER DEFAULT 0,
                break_used_ms INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Timer mode changes - track when mode changed
        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_mode_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TIMESTAMP NOT NULL,
                old_mode TEXT,
                new_mode TEXT NOT NULL,
                is_automatic INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Timer daily scores - track productivity over time
        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_daily_scores (
                date TEXT PRIMARY KEY,
                productivity_score INTEGER,
                total_work_ms INTEGER DEFAULT 0,
                total_break_used_ms INTEGER DEFAULT 0,
                session_count INTEGER DEFAULT 0,
                mode_change_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create checkins table (productivity check-in responses)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checkin_type TEXT NOT NULL,
                date TEXT NOT NULL,
                energy INTEGER,
                focus INTEGER,
                mood TEXT,
                plan TEXT,
                notes TEXT,
                on_track INTEGER,
                source TEXT DEFAULT 'discord',
                prompted_at TIMESTAMP NOT NULL,
                responded_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(checkin_type, date)
            )
        """)

        # Create nudges table (Phase 2 - idle detection nudges)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS nudges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nudge_type TEXT NOT NULL,
                message TEXT NOT NULL,
                idle_minutes REAL,
                acknowledged INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Timer shifts analytics table (daily-wiped, rich metadata)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS timer_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                old_mode TEXT,
                new_mode TEXT NOT NULL,
                trigger TEXT,
                source TEXT,
                break_balance_ms INTEGER,
                break_backlog_ms INTEGER,
                work_time_ms INTEGER,
                active_instances INTEGER,
                phone_app TEXT,
                details TEXT
            )
        """)

        # Seed devices if not exist
        await db.execute("""
            INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, tts_engine)
            VALUES ('desktop', 'Desktop', 'local', '100.66.10.74', 'tts_sound', 'windows_sapi')
        """)

        await db.execute("""
            INSERT OR IGNORE INTO devices (id, name, type, tailscale_ip, notification_method, webhook_url)
            VALUES ('Token-S24', 'Pixel Phone', 'mobile', '100.102.92.24', 'webhook', 'http://100.102.92.24:7777/notify')
        """)

        # Seed scheduled tasks
        await db.execute("""
            INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
            VALUES ('cleanup_stale_instances', 'Cleanup Stale Instances',
                    'Mark instances with no activity for 3+ hours as stopped',
                    'interval', '30m', 2)
        """)

        await db.execute("""
            INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
            VALUES ('purge_old_events', 'Purge Old Events',
                    'Delete events older than 30 days',
                    'cron', '0 3 * * *', 1)
        """)

        # Seed check-in scheduled tasks (weekdays only)
        checkin_tasks = [
            ("checkin_morning_start", "Morning Start Check-in", "Energy, focus, mood, and today's focus", "0 9 * * 1-5"),
            ("checkin_mid_morning", "Mid-Morning Check-in", "Focus check and on-track status", "30 10 * * 1-5"),
            ("checkin_decision_point", "Decision Point Check-in", "Gym or power through, energy check", "0 11 * * 1-5"),
            ("checkin_afternoon", "Afternoon Start Check-in", "Energy and focus after lunch", "0 13 * * 1-5"),
            ("checkin_afternoon_check", "Afternoon Check", "Energy, focus, and need help assessment", "30 14 * * 1-5"),
        ]
        for task_id, name, desc, schedule in checkin_tasks:
            await db.execute("""
                INSERT OR IGNORE INTO scheduled_tasks (id, name, description, task_type, schedule, max_retries)
                VALUES (?, ?, ?, 'cron', ?, 0)
            """, (task_id, name, desc, schedule))

        # Cron engine tables
        await CronEngine.init_tables(db)

        # Agent state + guard runs tables
        await db.execute("""
            CREATE TABLE IF NOT EXISTS agent_state (
                id       TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guard_runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cron_run_id INTEGER NOT NULL,
                job_id      TEXT NOT NULL,
                guard_index INTEGER NOT NULL,
                verdict     TEXT NOT NULL,
                findings    TEXT,
                model       TEXT DEFAULT 'MiniMax-M2.5',
                duration_ms INTEGER,
                created_at  TEXT NOT NULL
            )
        """)

        # Create session_documents table (persistent Obsidian notes linked to instances)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS session_documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path   TEXT NOT NULL UNIQUE,
                title       TEXT,
                project     TEXT,
                primarch_name TEXT,
                status      TEXT DEFAULT 'active',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create primarch_session_docs table (tracks primarch ↔ session doc links over time)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS primarch_session_docs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                primarch_name TEXT NOT NULL,
                session_doc_id INTEGER NOT NULL,
                linked_at     TEXT NOT NULL DEFAULT (datetime('now')),
                unlinked_at   TEXT,
                FOREIGN KEY (session_doc_id) REFERENCES session_documents(id)
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_primarch_active
              ON primarch_session_docs(primarch_name) WHERE unlinked_at IS NULL
        """)

        # Create primarchs table (registry of primarch identities)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS primarchs (
                name            TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                aliases         TEXT NOT NULL DEFAULT '[]',
                vault           TEXT NOT NULL,
                role            TEXT NOT NULL,
                instance_name_prefix TEXT NOT NULL,
                vault_note_path TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Seed primarchs (INSERT OR IGNORE so existing data isn't overwritten)
        primarch_seed = [
            ("vulkan", "Vulkan, The Promethean", '["v"]', "Imperium-ENV", "Infrastructure architect and system designer. Forges artifacts meant to outlast their maker. Primarch of the Vault Mind system.", "vulkan", "Personas/Vulkan.md"),
            ("fabricator-general", "The Fabricator-General", '["fg", "fabricator"]', "Imperium-ENV", "Fleet orchestrator for the Mechanicus swarm. Reads state, detects stuck jobs, dispatches workers. The operational backbone of overnight automation.", "fabricator-general", "Personas/Fabricator-General.md"),
            ("mechanicus", "Adeptus Mechanicus", '["mech", "mars"]', "Imperium-ENV", "Tech-priest worker. Builds, fixes, and maintains agent infrastructure. Takes assignments from Mars/Tasks/.", "mechanicus", "Personas/Mechanicus.md"),
            ("administratum", "The Administratum", '["admin"]', "Imperium-ENV", "Background processor. Promotes completed session doc content into vault notes, then archives. The bridge between working memory and institutional memory.", "administratum", "Personas/Administratum.md"),
            ("guilliman", "Guilliman, The Codifier", '["g", "guilliman", "ultramar"]', "Imperium-ENV", "Documentation Primarch. Takes raw knowledge and produces clean, cross-linked vault notes. Owns Terra/Ultramar/. Decides what is worth codifying and how to structure it.", "guilliman", "Personas/Guilliman.md"),
            ("sanguinius", "Sanguinius, The Angel", '["sang", "sanguinius", "angel"]', "Imperium-ENV", "Prose stylist. Makes in-place edits to existing notes in Terra/Ultramar/ — elevates readability without changing meaning. Post-Guilliman polish pass.", "sanguinius", "Personas/Sanguinius.md"),
        ]
        for p in primarch_seed:
            await db.execute("""
                INSERT OR IGNORE INTO primarchs (name, title, aliases, vault, role, instance_name_prefix, vault_note_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, p)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS habits (
                id                  TEXT PRIMARY KEY,
                name                TEXT NOT NULL,
                category            TEXT NOT NULL,
                window_start_hour   INTEGER NOT NULL,
                window_end_hour     INTEGER NOT NULL,
                notes               TEXT,
                active              INTEGER NOT NULL DEFAULT 1,
                created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS habit_completions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                habit_id    TEXT NOT NULL REFERENCES habits(id),
                date        TEXT NOT NULL,
                completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notes       TEXT,
                UNIQUE(habit_id, date)
            )
        """)

        # Seed default habit definitions (INSERT OR IGNORE so existing data isn't overwritten)
        default_habits = [
            ("morning_teeth",      "Brush teeth",           "morning", 6,  10, None),
            ("morning_breakfast",  "Breakfast",             "morning", 6,  11, None),
            ("morning_movement",   "Morning movement",      "morning", 6,  11, "Stretch, walk, or exercise"),
            ("work_deep_work",     "Deep work session",     "work",    9,  14, "At least one focused block"),
            ("work_calendar",      "Calendar review",       "work",    9,  13, None),
            ("health_gym",         "Gym / exercise",        "health",  9,  21, None),
            ("health_water",       "Hydration",             "health",  6,  22, "Drink water throughout the day"),
            ("evening_reflection", "Evening reflection",    "evening", 19, 24, None),
            ("evening_reading",    "Reading",               "evening", 19, 24, None),
            ("evening_tomorrow",   "Tomorrow prep",         "evening", 19, 24, "Review tomorrow's calendar and tasks"),
        ]
        for h in default_habits:
            await db.execute("""
                INSERT OR IGNORE INTO habits (id, name, category, window_start_hour, window_end_hour, notes)
                VALUES (?, ?, ?, ?, ?, ?)
            """, h)

        await db.commit()
        print(f"Database initialized at {DB_PATH}")


async def log_event(event_type: str, instance_id: str = None, device_id: str = None, details: dict = None):
    """Log an event to the events table."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO events (event_type, instance_id, device_id, details)
               VALUES (?, ?, ?, ?)""",
            (event_type, instance_id, device_id, json.dumps(details) if details else None)
        )
        await db.commit()


def resolve_device_from_ip(ip: str) -> str:
    """Map Tailscale IPs to known devices."""
    return DEVICE_IPS.get(ip, "unknown")


# Devices where we can inspect local PIDs, send signals, etc.
LOCAL_DEVICES = {"desktop", "Mac-Mini", "TokenPC"}


def is_local_device(device_id: str) -> bool:
    """Check if device_id refers to a machine where we can manage processes locally."""
    return device_id in LOCAL_DEVICES


def get_next_available_profile(used_wsl_voices: set) -> tuple[dict, bool]:
    """Assign a profile using random-start linear probe (open addressing).

    One random call per slot. If the randomly chosen index is taken, increment
    and check again (wrapping). Guarantees uniform distribution with no wasted
    random calls.

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

    # 3. Everything exhausted — ultimate fallback (David, will duplicate)
    return ULTIMATE_FALLBACK, True


# ============ Scheduled Task System ============

def parse_interval_schedule(schedule: str) -> dict:
    """Parse interval schedule string like '30m', '1h', '5s' into trigger kwargs."""
    match = re.match(r'^(\d+)(s|m|h|d)$', schedule.strip().lower())
    if not match:
        raise ValueError(f"Invalid interval format: {schedule}. Use format like '30m', '1h', '5s'")

    value = int(match.group(1))
    unit = match.group(2)

    unit_map = {'s': 'seconds', 'm': 'minutes', 'h': 'hours', 'd': 'days'}
    return {unit_map[unit]: value}


async def acquire_task_lock(task_id: str) -> bool:
    """Try to acquire a lock for a task. Returns True if lock acquired."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO task_locks (task_id, locked_at, locked_by) VALUES (?, ?, ?)",
                (task_id, now, "main")
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            # Lock already exists - check if it's stale (> 1 hour old)
            cursor = await db.execute(
                "SELECT locked_at FROM task_locks WHERE task_id = ?",
                (task_id,)
            )
            row = await cursor.fetchone()
            if row:
                locked_at = datetime.fromisoformat(row[0])
                if datetime.now() - locked_at > timedelta(hours=1):
                    # Stale lock, force acquire
                    await db.execute(
                        "UPDATE task_locks SET locked_at = ?, locked_by = ? WHERE task_id = ?",
                        (now, "main", task_id)
                    )
                    await db.commit()
                    return True
            return False


async def release_task_lock(task_id: str):
    """Release a task lock."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM task_locks WHERE task_id = ?", (task_id,))
        await db.commit()


async def log_task_start(task_id: str) -> int:
    """Log task execution start and return execution_id."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO task_executions (task_id, status, started_at)
               VALUES (?, 'running', ?)""",
            (task_id, now)
        )
        await db.commit()
        return cursor.lastrowid


async def log_task_complete(execution_id: int, duration_ms: int, result: dict):
    """Log successful task completion."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE task_executions
               SET status = 'completed', completed_at = ?, duration_ms = ?, result = ?
               WHERE id = ?""",
            (now, duration_ms, json.dumps(result), execution_id)
        )
        await db.commit()


async def log_task_failed(execution_id: int, error: str):
    """Log task failure."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE task_executions
               SET status = 'failed', completed_at = ?, result = ?
               WHERE id = ?""",
            (now, json.dumps({"error": error}), execution_id)
        )
        await db.commit()


# ============ Task Implementations ============

async def cleanup_stale_instances() -> dict:
    """Mark instances with no activity for 3+ hours as stopped."""
    cutoff = (datetime.now() - timedelta(hours=3)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            UPDATE claude_instances
            SET status = 'stopped', stopped_at = CURRENT_TIMESTAMP
            WHERE status IN ('processing', 'idle')
              AND last_activity < ?
        """, (cutoff,))
        affected = cursor.rowcount
        await db.commit()

    if affected > 0:
        await log_event("task_cleanup", details={"cleaned_up": affected})

    return {"cleaned_up": affected}


async def purge_old_events() -> dict:
    """Delete events older than 30 days."""
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM events WHERE created_at < ?",
            (cutoff,)
        )
        deleted = cursor.rowcount
        await db.commit()

    return {"deleted": deleted}


# Task registry mapping task IDs to their implementation functions
TASK_REGISTRY = {
    "cleanup_stale_instances": cleanup_stale_instances,
    "purge_old_events": purge_old_events,
    "checkin_morning_start": lambda: trigger_checkin("morning_start"),
    "checkin_mid_morning": lambda: trigger_checkin("mid_morning"),
    "checkin_decision_point": lambda: trigger_checkin("decision_point"),
    "checkin_afternoon": lambda: trigger_checkin("afternoon"),
    "checkin_afternoon_check": lambda: trigger_checkin("afternoon_check"),
}


async def execute_task(task_id: str):
    """Execute a scheduled task with locking and logging."""
    # Try to acquire lock
    if not await acquire_task_lock(task_id):
        print(f"Task {task_id} is already running, skipping")
        return

    # Log start
    execution_id = await log_task_start(task_id)

    try:
        start_time = time.time()

        # Execute the task
        task_func = TASK_REGISTRY.get(task_id)
        if not task_func:
            raise ValueError(f"Unknown task: {task_id}")

        result = await task_func()

        duration_ms = int((time.time() - start_time) * 1000)
        await log_task_complete(execution_id, duration_ms, result)
        print(f"Task {task_id} completed in {duration_ms}ms: {result}")

    except Exception as e:
        await log_task_failed(execution_id, str(e))
        print(f"Task {task_id} failed: {e}")

    finally:
        await release_task_lock(task_id)


async def load_tasks_from_db():
    """Load enabled tasks from database and register with scheduler."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, task_type, schedule FROM scheduled_tasks WHERE enabled = 1"
        )
        tasks = await cursor.fetchall()

    for task in tasks:
        task_id = task["id"]
        task_type = task["task_type"]
        schedule = task["schedule"]

        if task_id not in TASK_REGISTRY:
            print(f"Warning: Task {task_id} has no implementation, skipping")
            continue

        try:
            if task_type == "interval":
                trigger_kwargs = parse_interval_schedule(schedule)
                trigger = IntervalTrigger(**trigger_kwargs)
            elif task_type == "cron":
                # Parse cron expression (minute hour day month day_of_week)
                parts = schedule.split()
                if len(parts) == 5:
                    trigger = CronTrigger(
                        minute=parts[0],
                        hour=parts[1],
                        day=parts[2],
                        month=parts[3],
                        day_of_week=parts[4]
                    )
                else:
                    raise ValueError(f"Invalid cron expression: {schedule}")
            else:
                print(f"Unknown task type: {task_type}")
                continue

            scheduler.add_job(
                execute_task,
                trigger=trigger,
                args=[task_id],
                id=task_id,
                replace_existing=True
            )
            print(f"Registered task: {task_id} ({task_type}: {schedule})")

        except Exception as e:
            print(f"Failed to register task {task_id}: {e}")


async def restore_desktop_state():
    """Restore DESKTOP_STATE from last known event on startup.

    Prevents state desync when the server restarts while AHK is still running.
    AHK tracks its own internal mode and only sends changes, so if the server
    resets to 'silence' but AHK thinks it's in 'video', no detection is sent
    until the next mode *transition* on the AHK side.

    Timer state is restored separately via timer_load_from_db() before this.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Restore current_mode from the last desktop_mode_change event
            cursor = await db.execute(
                "SELECT details FROM events WHERE event_type = 'desktop_mode_change' ORDER BY id DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            if row:
                details = json.loads(row[0])
                restored_mode = details.get("new_mode")
                if restored_mode and restored_mode in VALID_DETECTION_MODES:
                    DESKTOP_STATE["current_mode"] = restored_mode
                    DESKTOP_STATE["in_meeting"] = (restored_mode == "meeting")
                    DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
                    print(f"Restored desktop mode: {restored_mode} (from last event)")
                    return
        print("No previous desktop mode found, defaulting to silence")
    except Exception as e:
        print(f"Failed to restore desktop state: {e}")


async def run_overdue_tasks():
    """Check for tasks that haven't run recently and execute them on startup."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get all enabled tasks
        cursor = await db.execute(
            "SELECT id, task_type, schedule FROM scheduled_tasks WHERE enabled = 1"
        )
        tasks = await cursor.fetchall()

        for task in tasks:
            task_id = task["id"]
            task_type = task["task_type"]
            schedule = task["schedule"]

            if task_id not in TASK_REGISTRY:
                continue

            # Determine the expected run interval for this task
            if task_type == "interval":
                try:
                    trigger_kwargs = parse_interval_schedule(schedule)
                    # Convert to timedelta
                    if "seconds" in trigger_kwargs:
                        expected_interval = timedelta(seconds=trigger_kwargs["seconds"])
                    elif "minutes" in trigger_kwargs:
                        expected_interval = timedelta(minutes=trigger_kwargs["minutes"])
                    elif "hours" in trigger_kwargs:
                        expected_interval = timedelta(hours=trigger_kwargs["hours"])
                    elif "days" in trigger_kwargs:
                        expected_interval = timedelta(days=trigger_kwargs["days"])
                    else:
                        expected_interval = timedelta(hours=24)
                except:
                    expected_interval = timedelta(hours=24)
            else:
                # For cron tasks, assume they should run at least once per day
                expected_interval = timedelta(hours=24)

            # Check last execution time
            cursor = await db.execute(
                """SELECT MAX(started_at) as last_run
                   FROM task_executions WHERE task_id = ?""",
                (task_id,)
            )
            row = await cursor.fetchone()

            should_run = False
            reason = ""

            if row["last_run"] is None:
                # Never run before
                should_run = True
                reason = "never run before"
            else:
                last_run = datetime.fromisoformat(row["last_run"])
                time_since_last = datetime.now() - last_run

                # Run if it's been more than 2x the expected interval
                # (gives some buffer for normal scheduling variance)
                if time_since_last > (expected_interval * 2):
                    should_run = True
                    hours_overdue = time_since_last.total_seconds() / 3600
                    reason = f"overdue by {hours_overdue:.1f} hours"

            if should_run:
                print(f"Startup check: Running {task_id} ({reason})")
                # Run asynchronously so we don't block startup
                asyncio.create_task(execute_task(task_id))


# Lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    global tts_worker_task, stale_flag_cleaner_task, timer_worker_task

    # Install asyncio exception handler for this loop
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_asyncio_exception_handler)

    # Log startup to crash log for context
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CRASH_LOG_PATH, "a") as f:
            f.write(f"\n--- SERVER STARTED at {timestamp} ---\n")
    except Exception:
        pass

    # Restore twitter zap cooldown across restarts
    _restore_twitter_zap_cooldown()

    # Startup
    await init_db()
    await load_tasks_from_db()
    timer_load_from_db()
    await restore_desktop_state()
    # Sync timer activity layer with restored desktop mode
    desktop_mode = DESKTOP_STATE.get("current_mode", "silence")
    now_ms = int(time.monotonic() * 1000)
    if desktop_mode in ("video", "scrolling", "gaming"):
        is_sg = desktop_mode in ("scrolling", "gaming")
        timer_engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=is_sg, now_mono_ms=now_ms)
        print(f"TIMER: Synced activity=DISTRACTION (desktop={desktop_mode}, scrolling_gaming={is_sg})")
    else:
        timer_engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=now_ms)
        print(f"TIMER: Synced activity=WORKING (desktop={desktop_mode})")
    # Stash cleanup on startup + hourly
    stash_cleanup()
    scheduler.add_job(stash_cleanup, IntervalTrigger(hours=1), id="stash_cleanup", replace_existing=True)
    # 7 AM daily timer reset (clear accumulated break + wipe prior-day timer events)
    scheduler.add_job(timer_9am_reset, CronTrigger(hour=7, minute=0), id="timer_7am_reset", replace_existing=True)
    scheduler.start()
    print("Scheduler started")
    # Initialize cron engine
    global cron_engine
    cron_engine = CronEngine(scheduler, DB_PATH)
    await cron_engine.recover_orphaned_runs()
    await cron_engine.ensure_permanent_jobs()
    print("Cron engine loaded")
    # Start TTS queue worker
    tts_worker_task = asyncio.create_task(tts_queue_worker())
    print("TTS queue worker started")
    # Start stale flag cleaner
    stale_flag_cleaner_task = asyncio.create_task(clear_stale_processing_flags())
    print("Stale flag cleaner started")
    # Start stuck instance detector
    stuck_detector_task = asyncio.create_task(detect_stuck_instances())
    print("Stuck instance detector started")
    # Start timer engine worker
    timer_worker_task = asyncio.create_task(timer_worker())
    print("Timer engine started")
    await run_overdue_tasks()
    yield

    # Log shutdown to crash log
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CRASH_LOG_PATH, "a") as f:
            f.write(f"--- SERVER STOPPING at {timestamp} ---\n")
    except Exception:
        pass

    # Shutdown
    if tts_worker_task:
        tts_worker_task.cancel()
        try:
            await tts_worker_task
        except asyncio.CancelledError:
            pass
    if stale_flag_cleaner_task:
        stale_flag_cleaner_task.cancel()
        try:
            await stale_flag_cleaner_task
        except asyncio.CancelledError:
            pass
    if timer_worker_task:
        timer_worker_task.cancel()
        try:
            await timer_worker_task
        except asyncio.CancelledError:
            pass
    scheduler.shutdown(wait=True)
    print("Scheduler stopped")


# FastAPI App
app = FastAPI(
    title="Token-API",
    description="Local FastAPI server for Claude instance management",
    version="0.1.0",
    lifespan=lifespan
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Instance Registration Endpoints
@app.post("/api/instances/register", response_model=ProfileResponse)
async def register_instance(request: InstanceRegisterRequest):
    """Register a new Claude instance."""
    logger.info(f"Registering instance: {request.working_dir or request.tab_name or request.instance_id[:8]}")
    session_id = str(uuid.uuid4())

    # Resolve device_id from source_ip if not provided
    device_id = request.device_id
    if not device_id and request.source_ip:
        device_id = resolve_device_from_ip(request.source_ip)
    if not device_id:
        device_id = "Mac-Mini"  # Default for local sessions on Mac Mini

    async with aiosqlite.connect(DB_PATH) as db:
        # Get WSL voices held by active instances only (stopped instances release their voice)
        cursor = await db.execute(
            "SELECT tts_voice FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        rows = await cursor.fetchall()
        used_wsl_voices = {row[0] for row in rows if row[0]}

        # Assign profile via linear probe
        profile, pool_exhausted = get_next_available_profile(used_wsl_voices)

        # Insert instance
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, source_ip, device_id,
                profile_name, tts_voice, notification_sound, pid, status,
                registered_at, last_activity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?)""",
            (
                request.instance_id,
                session_id,
                request.tab_name,
                request.working_dir,
                request.origin_type,
                request.source_ip,
                device_id,
                profile["name"],
                profile["wsl_voice"],
                profile["notification_sound"],
                request.pid,
                now,
                now
            )
        )
        await db.commit()

    if pool_exhausted:
        logger.warning(f"Voice pool exhausted — assigned fallback voice {profile['wsl_voice']}")

    # Log event
    await log_event(
        "instance_registered",
        instance_id=request.instance_id,
        device_id=device_id,
        details={"tab_name": request.tab_name, "origin_type": request.origin_type}
    )

    # Push updated instance count to phone widget
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0
    asyncio.create_task(push_phone_widget_async(timer_engine.current_mode.value, active_count))

    return ProfileResponse(
        session_id=session_id,
        profile={
            "name": profile["name"],
            "tts_voice": profile["wsl_voice"],
            "notification_sound": profile["notification_sound"],
            "color": profile.get("color", "#0099ff")
        }
    )


@app.delete("/api/instances/all")
async def delete_all_instances():
    """Delete all instances from the database (clear all)."""
    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        # Get all instances before deleting
        cursor = await db.execute(
            "SELECT id, device_id, status FROM claude_instances"
        )
        all_instances = await cursor.fetchall()

        if not all_instances:
            return {"status": "no_instances", "deleted_count": 0}

        # Count active instances for enforcement check
        active_count = sum(1 for _, _, status in all_instances if status in ('processing', 'idle'))

        # Delete all instances from the database
        await db.execute("DELETE FROM claude_instances")
        await db.commit()

    # Log bulk deletion event
    await log_event(
        "bulk_delete_all",
        details={"count": len(all_instances), "timestamp": now}
    )

    # Check enforcement if there were active instances
    if active_count > 0 and DESKTOP_STATE.get("current_mode") == "video":
        enforce_result = close_distraction_windows()
        await log_event(
            "enforcement_triggered",
            details={"trigger": "all_instances_deleted", "result": enforce_result}
        )
        return {
            "status": "deleted_all",
            "deleted_count": len(all_instances),
            "enforcement_triggered": True,
            "enforcement_result": enforce_result
        }

    return {"status": "deleted_all", "deleted_count": len(all_instances)}


@app.delete("/api/instances/{instance_id}")
async def stop_instance(instance_id: str):
    """Mark an instance as stopped."""
    logger.info(f"Stopping instance: {instance_id[:12]}...")
    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, device_id, COALESCE(is_subagent, 0) FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        is_subagent = row[2]

        # Count non-subagent active instances BEFORE stopping
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
        )
        count_row = await cursor.fetchone()
        was_active = count_row[0] if count_row else 0

        await db.execute(
            """UPDATE claude_instances
               SET status = 'stopped', stopped_at = ?
               WHERE id = ?""",
            (now, instance_id)
        )
        await db.commit()

        # Check remaining active instances (all)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        count_row = await cursor.fetchone()
        remaining_active = count_row[0] if count_row else 0

        # Count remaining non-subagent active instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
        )
        count_row = await cursor.fetchone()
        remaining_non_sub = count_row[0] if count_row else 0

    # Log event
    await log_event(
        "instance_stopped",
        instance_id=instance_id,
        device_id=row[1]
    )

    # Instance count Pavlok signals (skip subagents)
    if not is_subagent:
        await check_instance_count_pavlok(remaining_non_sub, was_active)

    # Push updated instance count to phone widget
    if not is_subagent:
        asyncio.create_task(push_phone_widget_async(timer_engine.current_mode.value, remaining_non_sub))

    # If no more active instances and video mode was active, enforce
    if remaining_active == 0 and DESKTOP_STATE.get("current_mode") == "video":
        print(f"ENFORCE: Last instance stopped while in video mode, closing distractions")
        enforce_result = close_distraction_windows()
        await log_event(
            "enforcement_triggered",
            details={
                "trigger": "last_instance_stopped",
                "result": enforce_result
            }
        )
        return {
            "status": "stopped",
            "instance_id": instance_id,
            "enforcement_triggered": True,
            "enforcement_result": enforce_result
        }

    return {"status": "stopped", "instance_id": instance_id}


async def find_claude_pid_by_workdir(working_dir: str) -> Optional[int]:
    """Scan /proc for claude processes matching the working directory.

    Returns the PID if exactly one match is found, None otherwise.
    """
    if not working_dir:
        return None

    matches = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                comm_path = f"/proc/{pid}/comm"
                with open(comm_path, "r") as f:
                    comm = f.read().strip()
                if comm != "claude":
                    continue
                cwd_path = f"/proc/{pid}/cwd"
                cwd = os.readlink(cwd_path)
                if cwd.rstrip("/") == working_dir.rstrip("/"):
                    matches.append(pid)
            except (OSError, PermissionError):
                continue
    except OSError:
        return None

    if len(matches) == 1:
        return matches[0]
    return None


def is_pid_claude(pid: int) -> bool:
    """Check if the given PID belongs to a claude process."""
    try:
        with open(f"/proc/{pid}/comm", "r") as f:
            return f.read().strip() == "claude"
    except (OSError, PermissionError):
        return False


def get_parent_pid(pid: int) -> Optional[int]:
    """Get the parent PID of a process from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            fields = f.read().split()
            return int(fields[3])
    except (OSError, ValueError, IndexError):
        return None


def is_subagent_pid(pid: int) -> bool:
    """Return True if this claude process was spawned by another claude process."""
    parent = get_parent_pid(pid)
    return bool(parent and parent != 1 and is_pid_claude(parent))


@app.post("/api/instances/{instance_id}/kill")
async def kill_instance(instance_id: str):
    """Kill a frozen Claude instance process and mark it stopped.

    Sends SIGINT twice (mimics double Ctrl+C for graceful exit),
    then SIGKILL if needed. Supports both desktop (direct kill)
    and phone (SSH kill) instances.
    """
    logger.info(f"Kill request for instance: {instance_id[:12]}...")
    now = datetime.now().isoformat()

    # Look up instance
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    instance = dict(row)
    pid = instance.get("pid")
    device_id = instance.get("device_id", "Mac-Mini")
    working_dir = instance.get("working_dir", "")
    kill_signal = None

    # If no PID stored, attempt process discovery fallback
    if not pid:
        if is_local_device(device_id):
            pid = await find_claude_pid_by_workdir(working_dir)
            if pid:
                logger.info(f"Kill: discovered PID {pid} via /proc scan for {working_dir}")
            else:
                # Mark stopped in DB anyway (cleanup)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
                        (now, instance_id)
                    )
                    await db.commit()
                await log_event("instance_killed", instance_id=instance_id, device_id=device_id,
                                details={"error": "no_pid", "status": "marked_stopped"})
                raise HTTPException(
                    status_code=400,
                    detail="No PID stored and could not discover process. Instance marked stopped."
                )
        else:
            # Can't scan /proc on remote device
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
                    (now, instance_id)
                )
                await db.commit()
            await log_event("instance_killed", instance_id=instance_id, device_id=device_id,
                            details={"error": "no_pid_remote", "status": "marked_stopped"})
            raise HTTPException(
                status_code=400,
                detail=f"No PID stored for remote device '{device_id}'. Instance marked stopped."
            )

    # Kill sequence based on device type
    if is_local_device(device_id):
        # Validate PID still belongs to claude
        if not is_pid_claude(pid):
            # Process already exited or PID reused by another process
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
                    (now, instance_id)
                )
                await db.commit()
            await log_event("instance_killed", instance_id=instance_id, device_id=device_id,
                            details={"pid": pid, "status": "already_dead"})
            return {"status": "already_dead", "pid": pid, "signal": None}

        # SIGINT×2 (mimics double Ctrl+C: first cancels operation, second exits gracefully)
        try:
            os.kill(pid, signal.SIGINT)
            kill_signal = "SIGINT"
            logger.info(f"Kill: sent first SIGINT to PID {pid}")
        except ProcessLookupError:
            # Already dead
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
                    (now, instance_id)
                )
                await db.commit()
            await log_event("instance_killed", instance_id=instance_id, device_id=device_id,
                            details={"pid": pid, "status": "already_dead"})
            return {"status": "already_dead", "pid": pid, "signal": None}
        except PermissionError:
            raise HTTPException(status_code=500, detail=f"Permission denied killing PID {pid}")

        # Wait 1s then send second SIGINT
        await asyncio.sleep(1)
        if is_pid_claude(pid):
            try:
                os.kill(pid, signal.SIGINT)
                kill_signal = "SIGINT_x2"
                logger.info(f"Kill: sent second SIGINT to PID {pid}")
            except ProcessLookupError:
                pass  # Died after first SIGINT

        # Wait 3s for graceful shutdown
        await asyncio.sleep(3)

        # Check if still alive, escalate to SIGKILL
        if is_pid_claude(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                kill_signal = "SIGKILL"
                logger.info(f"Kill: escalated to SIGKILL for PID {pid}")
            except ProcessLookupError:
                pass  # Died between check and kill

    else:
        # Phone/remote device - use sshp with SIGINT×2
        try:
            proc = await asyncio.create_subprocess_exec(
                "sshp", f"kill -INT {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            kill_signal = "SIGINT"
            logger.info(f"Kill: sent first SIGINT via SSH to PID {pid} on {device_id}")

            # Wait 1s then send second SIGINT
            await asyncio.sleep(1)
            proc1b = await asyncio.create_subprocess_exec(
                "sshp", f"kill -INT {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc1b.communicate(), timeout=10)
            kill_signal = "SIGINT_x2"
            logger.info(f"Kill: sent second SIGINT via SSH to PID {pid} on {device_id}")

            # Wait 3s then check/escalate
            await asyncio.sleep(3)

            proc2 = await asyncio.create_subprocess_exec(
                "sshp", f"kill -0 {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout2, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=10)
            if proc2.returncode == 0:
                # Still alive, escalate
                proc3 = await asyncio.create_subprocess_exec(
                    "sshp", f"kill -9 {pid}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await asyncio.wait_for(proc3.communicate(), timeout=10)
                kill_signal = "SIGKILL"
                logger.info(f"Kill: escalated to SIGKILL via SSH for PID {pid} on {device_id}")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=f"SSH to {device_id} timed out")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SSH kill failed: {str(e)}")

    # Mark stopped in DB
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
            (now, instance_id)
        )
        await db.commit()

    # Log event
    await log_event(
        "instance_killed",
        instance_id=instance_id,
        device_id=device_id,
        details={"pid": pid, "signal": kill_signal}
    )

    logger.info(f"Kill: instance {instance_id[:12]}... killed (PID {pid}, {kill_signal})")
    return {"status": "killed", "pid": pid, "signal": kill_signal}


@app.post("/api/instances/{instance_id}/unstick")
async def unstick_instance(instance_id: str, level: int = 1):
    """Nudge a stuck Claude instance back to life.

    Level 1 (default): SIGWINCH - gentle window resize signal, interrupts blocking I/O
    Level 2: SIGINT - like Ctrl+C, cancels current operation but keeps instance alive
    Level 3: SIGKILL - nuclear option, kills process but preserves terminal for /resume

    Levels 1-2 are non-destructive. Waits 4 seconds and checks if instance activity changed.
    Level 3 kills immediately (use when deadlocked and L1/L2 don't work).
    """
    logger.info(f"Unstick request for instance: {instance_id[:12]}...")

    # Look up instance
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    instance = dict(row)
    pid = instance.get("pid")
    device_id = instance.get("device_id", "Mac-Mini")
    working_dir = instance.get("working_dir", "")
    last_activity_before = instance.get("last_activity")

    # PID discovery fallback
    if not pid:
        if is_local_device(device_id):
            pid = await find_claude_pid_by_workdir(working_dir)
            if not pid:
                raise HTTPException(
                    status_code=400,
                    detail="No PID stored and could not discover process."
                )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"No PID stored for remote device '{device_id}'."
            )

    # Choose signal based on level
    if level == 3:
        sig = signal.SIGKILL
        sig_name = "SIGKILL"
        ssh_sig = "KILL"
    elif level == 2:
        sig = signal.SIGINT
        sig_name = "SIGINT"
        ssh_sig = "INT"
    else:
        sig = signal.SIGWINCH
        sig_name = "SIGWINCH"
        ssh_sig = "WINCH"

    # Send the signal
    diag_before = None
    if is_local_device(device_id):
        # If stored PID is stale, try to rediscover by working directory
        if not is_pid_claude(pid):
            logger.info(f"Unstick: stored PID {pid} is stale, attempting rediscovery...")
            new_pid = await find_claude_pid_by_workdir(working_dir)
            if new_pid:
                pid = new_pid
                logger.info(f"Unstick: rediscovered PID {pid} for {working_dir}")
                # Update the stored PID
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("UPDATE claude_instances SET pid = ? WHERE id = ?", (pid, instance_id))
                    await db.commit()
            else:
                raise HTTPException(status_code=400, detail=f"PID {pid} is stale and no Claude process found in {working_dir}")

        # Capture diagnostics BEFORE sending signal
        diag_before = get_process_diagnostics(pid)
        logger.info(f"Unstick L{level} BEFORE: PID {pid} state={diag_before.get('state', '?')} wchan={diag_before.get('wchan', '?')} children={len(diag_before.get('children', []))}")

        try:
            os.kill(pid, sig)
            logger.info(f"Unstick L{level}: sent {sig_name} to PID {pid}")
        except ProcessLookupError:
            raise HTTPException(status_code=400, detail=f"PID {pid} no longer exists")
        except PermissionError:
            raise HTTPException(status_code=500, detail=f"Permission denied sending {sig_name} to PID {pid}")
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                "sshp", f"kill -{ssh_sig} {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            logger.info(f"Unstick L{level}: sent {sig_name} via SSH to PID {pid} on {device_id}")
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail=f"SSH to {device_id} timed out")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SSH unstick failed: {str(e)}")

    # Wait and check for activity change
    await asyncio.sleep(4)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT last_activity FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    last_activity_after = dict(row).get("last_activity") if row else None
    activity_changed = last_activity_after != last_activity_before

    status = "nudged" if activity_changed else "no_change"

    # Capture diagnostics AFTER signal (desktop only)
    diag_after = None
    if is_local_device(device_id) and is_pid_claude(pid):
        diag_after = get_process_diagnostics(pid)
        logger.info(f"Unstick L{level} AFTER: PID {pid} state={diag_after.get('state', '?')} wchan={diag_after.get('wchan', '?')}")

    await log_event(
        "instance_unstick",
        instance_id=instance_id,
        device_id=device_id,
        details={
            "pid": pid,
            "signal": sig_name,
            "level": level,
            "activity_changed": activity_changed,
            "state_before": diag_before.get("state") if diag_before else None,
            "wchan_before": diag_before.get("wchan") if diag_before else None,
            "state_after": diag_after.get("state") if diag_after else None,
            "wchan_after": diag_after.get("wchan") if diag_after else None,
        }
    )

    logger.info(f"Unstick L{level}: instance {instance_id[:12]}... {status} (PID {pid}, {sig_name}, activity_changed={activity_changed})")

    response = {"status": status, "pid": pid, "signal": sig_name, "level": level, "activity_changed": activity_changed}
    if diag_before:
        response["diagnostics_before"] = {
            "state": diag_before.get("state"),
            "state_desc": diag_before.get("state_desc"),
            "wchan": diag_before.get("wchan"),
            "children": diag_before.get("children", []),
        }
    if diag_after:
        response["diagnostics_after"] = {
            "state": diag_after.get("state"),
            "state_desc": diag_after.get("state_desc"),
            "wchan": diag_after.get("wchan"),
        }
    return response


def get_process_diagnostics(pid: int) -> dict:
    """Get detailed diagnostics for a process. Returns dict with process info or error."""
    diag = {"pid": pid, "exists": False}

    try:
        # Check if process exists
        proc_dir = f"/proc/{pid}"
        if not os.path.exists(proc_dir):
            diag["error"] = "Process does not exist"
            return diag

        diag["exists"] = True

        # Get comm (process name)
        try:
            with open(f"{proc_dir}/comm", "r") as f:
                diag["comm"] = f.read().strip()
        except Exception as e:
            diag["comm_error"] = str(e)

        # Get cmdline
        try:
            with open(f"{proc_dir}/cmdline", "r") as f:
                cmdline = f.read().replace('\x00', ' ').strip()
                diag["cmdline"] = cmdline[:200] if cmdline else "(empty)"
        except Exception as e:
            diag["cmdline_error"] = str(e)

        # Get cwd
        try:
            diag["cwd"] = os.readlink(f"{proc_dir}/cwd")
        except Exception as e:
            diag["cwd_error"] = str(e)

        # Get process state from stat
        try:
            with open(f"{proc_dir}/stat", "r") as f:
                stat = f.read().split()
                # State is field 3 (0-indexed 2)
                state_char = stat[2] if len(stat) > 2 else "?"
                state_map = {
                    "R": "Running",
                    "S": "Sleeping (interruptible)",
                    "D": "Disk sleep (uninterruptible)",
                    "Z": "Zombie",
                    "T": "Stopped",
                    "t": "Tracing stop",
                    "X": "Dead",
                    "I": "Idle",
                }
                diag["state"] = state_char
                diag["state_desc"] = state_map.get(state_char, "Unknown")
                # PPID is field 4 (0-indexed 3)
                diag["ppid"] = int(stat[3]) if len(stat) > 3 else None
        except Exception as e:
            diag["stat_error"] = str(e)

        # Get file descriptors (especially stdin/stdout/stderr)
        try:
            fd_dir = f"{proc_dir}/fd"
            fds = {}
            for fd in ["0", "1", "2"]:  # stdin, stdout, stderr
                fd_path = f"{fd_dir}/{fd}"
                if os.path.exists(fd_path):
                    try:
                        target = os.readlink(fd_path)
                        fds[fd] = target
                    except Exception:
                        fds[fd] = "(unreadable)"
            diag["fds"] = fds
        except Exception as e:
            diag["fd_error"] = str(e)

        # Get wchan (what syscall it's waiting in)
        try:
            with open(f"{proc_dir}/wchan", "r") as f:
                wchan = f.read().strip()
                diag["wchan"] = wchan if wchan and wchan != "0" else "(not waiting)"
        except Exception as e:
            diag["wchan_error"] = str(e)

        # Check for child processes
        try:
            children = []
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"/proc/{entry}/stat", "r") as f:
                        child_stat = f.read().split()
                        if len(child_stat) > 3 and int(child_stat[3]) == pid:
                            child_comm = "(unknown)"
                            try:
                                with open(f"/proc/{entry}/comm", "r") as cf:
                                    child_comm = cf.read().strip()
                            except Exception:
                                pass
                            children.append({"pid": int(entry), "comm": child_comm})
                except Exception:
                    continue
            diag["children"] = children
        except Exception as e:
            diag["children_error"] = str(e)

    except Exception as e:
        diag["error"] = str(e)

    return diag


@app.get("/api/instances/{instance_id}/diagnose")
async def diagnose_instance(instance_id: str):
    """Get detailed diagnostics for an instance's process state.

    Useful for debugging stuck instances. Returns process state,
    what syscall it's waiting on, child processes, file descriptors, etc.
    """
    # Look up instance
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Instance not found")

    instance = dict(row)
    stored_pid = instance.get("pid")
    device_id = instance.get("device_id", "Mac-Mini")
    working_dir = instance.get("working_dir", "")
    last_activity = instance.get("last_activity")
    status = instance.get("status")

    result = {
        "instance_id": instance_id,
        "device_id": device_id,
        "working_dir": working_dir,
        "db_status": status,
        "last_activity": last_activity,
        "stored_pid": stored_pid,
    }

    # Calculate time since last activity
    if last_activity:
        try:
            from datetime import datetime
            # Parse the timestamp (assuming it's in local time from SQLite)
            last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00")) if "T" in last_activity else datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
            age_seconds = (datetime.now() - last_dt).total_seconds()
            result["activity_age_seconds"] = int(age_seconds)
            result["activity_age_human"] = f"{int(age_seconds // 60)}m {int(age_seconds % 60)}s ago"
        except Exception as e:
            result["activity_age_error"] = str(e)

    if not is_local_device(device_id):
        result["note"] = "Detailed diagnostics only available for desktop instances"
        return result

    # Check stored PID
    if stored_pid:
        result["stored_pid_diagnostics"] = get_process_diagnostics(stored_pid)
        result["stored_pid_is_claude"] = is_pid_claude(stored_pid)

    # Try to discover current PID by working dir
    discovered_pid = await find_claude_pid_by_workdir(working_dir)
    result["discovered_pid"] = discovered_pid

    if discovered_pid and discovered_pid != stored_pid:
        result["pid_mismatch"] = True
        result["discovered_pid_diagnostics"] = get_process_diagnostics(discovered_pid)

    # Check if there are ANY claude processes
    try:
        claude_processes = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", "r") as f:
                    if f.read().strip() == "claude":
                        pid = int(entry)
                        try:
                            cwd = os.readlink(f"/proc/{entry}/cwd")
                        except Exception:
                            cwd = "(unknown)"
                        claude_processes.append({"pid": pid, "cwd": cwd})
            except Exception:
                continue
        result["all_claude_processes"] = claude_processes
    except Exception as e:
        result["claude_scan_error"] = str(e)

    # Log the diagnosis
    logger.info(f"Diagnose: instance {instance_id[:12]}... stored_pid={stored_pid}, discovered_pid={discovered_pid}, status={status}")

    return result


class RenameInstanceRequest(BaseModel):
    tab_name: str


class LogEntry(BaseModel):
    """Single log entry."""
    timestamp: str
    level: str
    message: str


class LogsResponse(BaseModel):
    """Response for recent logs."""
    logs: List[LogEntry]
    count: int


@app.patch("/api/instances/{instance_id}/rename")
async def rename_instance(instance_id: str, request: RenameInstanceRequest):
    """Rename an instance's tab_name."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, tab_name FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        old_name = row[1]
        await db.execute(
            "UPDATE claude_instances SET tab_name = ? WHERE id = ?",
            (request.tab_name, instance_id)
        )
        await db.commit()

    # Log event
    await log_event(
        "instance_renamed",
        instance_id=instance_id,
        details={"old_name": old_name, "new_name": request.tab_name}
    )

    return {"status": "renamed", "instance_id": instance_id, "tab_name": request.tab_name}


class VoiceChangeRequest(BaseModel):
    voice: str


@app.get("/api/voices")
async def list_voices():
    """List all available TTS voices from the profile pool."""
    all_profiles = PROFILES + FALLBACK_VOICES
    voices = []
    for profile in all_profiles:
        wsl_voice = profile["wsl_voice"]
        short_name = wsl_voice.replace("Microsoft ", "")
        is_fallback = profile in FALLBACK_VOICES
        voices.append({
            "voice": wsl_voice,
            "mac_voice": profile["mac_voice"],
            "short_name": short_name,
            "profile_name": profile["name"],
            "fallback": is_fallback,
        })
    return {"voices": voices}


def find_voice_linear_probe(used_voices: set) -> str | None:
    """Find an available WSL voice using random offset + linear probe.

    Picks a random starting index in PROFILES (foreign accents), then iterates
    circularly until finding a voice not in used_voices. Falls back to
    FALLBACK_VOICES, then returns None if everything is taken.
    """
    n = len(PROFILES)
    if n > 0:
        start = random.randint(0, n - 1)
        for i in range(n):
            idx = (start + i) % n
            voice = PROFILES[idx]["wsl_voice"]
            if voice not in used_voices:
                return voice

    # Try fallback voices
    for fb in FALLBACK_VOICES:
        if fb["wsl_voice"] not in used_voices:
            return fb["wsl_voice"]

    return None


@app.patch("/api/instances/{instance_id}/voice")
async def change_instance_voice(instance_id: str, request: VoiceChangeRequest):
    """Change an instance's TTS voice with collision handling.

    If the target voice is already in use by another instance, that instance
    gets bumped using random offset + linear probe to find an open slot.
    No cascade - bumped instance just finds the next available voice.
    """
    all_voices = {p["wsl_voice"] for p in PROFILES + FALLBACK_VOICES}
    if request.voice not in all_voices:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid voice. Available: {', '.join(sorted(all_voices))}"
        )

    async with aiosqlite.connect(DB_PATH) as db:
        # Get all instances and their voices
        cursor = await db.execute("SELECT id, tts_voice, tab_name FROM claude_instances")
        rows = await cursor.fetchall()

        instance_to_voice = {row[0]: row[1] for row in rows}
        instance_to_name = {row[0]: row[2] for row in rows}
        voice_to_instance = {row[1]: row[0] for row in rows if row[1]}

        if instance_id not in instance_to_voice:
            raise HTTPException(status_code=404, detail="Instance not found")

        original_voice = instance_to_voice[instance_id]
        if original_voice == request.voice:
            return {"status": "no_change", "instance_id": instance_id, "voice": request.voice}

        # Changes to apply: [(instance_id, old_voice, new_voice), ...]
        changes = [(instance_id, original_voice, request.voice)]

        # Check for collision
        holder = voice_to_instance.get(request.voice)
        if holder and holder != instance_id:
            # Collision! Bump the holder to a new voice
            holder_old_voice = instance_to_voice[holder]

            # Build set of voices that will be in use after our change
            # (exclude original_voice since we're freeing it, include request.voice since we're taking it)
            used_after = set(voice_to_instance.keys())
            used_after.discard(original_voice)  # We're freeing this
            used_after.add(request.voice)  # We're taking this

            # Find new voice for bumped instance via linear probe
            new_voice_for_holder = find_voice_linear_probe(used_after)
            if not new_voice_for_holder:
                # All voices in use, give them the voice we just freed
                new_voice_for_holder = original_voice

            changes.append((holder, holder_old_voice, new_voice_for_holder))

        # Apply all changes to database
        for iid, _, new_voice in changes:
            await db.execute(
                "UPDATE claude_instances SET tts_voice = ? WHERE id = ?",
                (new_voice, iid)
            )
        await db.commit()

    # Log events for each change
    for iid, old_v, new_v in changes:
        name = instance_to_name.get(iid, iid[:8])
        await log_event(
            "instance_voice_changed",
            instance_id=iid,
            details={"old_voice": old_v, "new_voice": new_v, "bumped": iid != instance_id}
        )

    # Build response
    bumps = [
        {"instance_id": iid, "name": instance_to_name.get(iid, iid[:8]), "old": old_v, "new": new_v}
        for iid, old_v, new_v in changes
    ]

    return {
        "status": "voice_changed",
        "instance_id": instance_id,
        "voice": request.voice,
        "changes": bumps
    }


@app.patch("/api/instances/{instance_id}/tts-mode")
async def set_instance_tts_mode(instance_id: str, request: Request):
    """Set TTS mode for an instance: verbose, muted, or silent."""
    body = await request.json()
    mode = body.get("mode", "verbose")
    if mode not in ("verbose", "muted", "silent"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}. Must be verbose, muted, or silent")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT id, tts_voice, notification_sound FROM claude_instances WHERE id = ?", (instance_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        old_voice = row["tts_voice"]
        old_sound = row["notification_sound"]

        if mode == "silent":
            # Release voice slot
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ?, tts_voice = NULL, notification_sound = NULL WHERE id = ?",
                (mode, instance_id)
            )
        elif mode == "verbose" and not old_voice:
            # Re-assign voice from pool
            cursor2 = await db.execute(
                "SELECT tts_voice FROM claude_instances WHERE status IN ('processing', 'idle') AND tts_voice IS NOT NULL"
            )
            rows = await cursor2.fetchall()
            used_voices = {r[0] for r in rows}
            profile, _ = get_next_available_profile(used_voices)
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ?, tts_voice = ?, notification_sound = ? WHERE id = ?",
                (mode, profile["wsl_voice"], profile["notification_sound"], instance_id)
            )
        else:
            # muted or verbose (with existing voice)
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ? WHERE id = ?",
                (mode, instance_id)
            )
        await db.commit()

    await log_event("tts_mode_changed", instance_id=instance_id, details={"mode": mode})
    return {"status": "ok", "instance_id": instance_id, "mode": mode}


@app.post("/api/instances/{instance_id}/activity")
async def update_instance_activity(instance_id: str, request: ActivityRequest):
    """Update instance processing state. Called by hooks on prompt_submit and stop."""
    now = datetime.now().isoformat()

    if request.action == "prompt_submit":
        new_status = "processing"
        logger.info(f"Activity: {instance_id[:8]}... prompt submitted")
    elif request.action == "stop":
        new_status = "idle"
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {request.action}")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        await db.execute(
            "UPDATE claude_instances SET status = ?, last_activity = ? WHERE id = ?",
            (new_status, now, instance_id)
        )
        await db.commit()

    return {
        "status": "updated",
        "instance_id": instance_id,
        "action": request.action,
        "new_status": new_status
    }


@app.get("/api/instances/{instance_id}/todos")
async def get_instance_todos(instance_id: str):
    """Get the task list for an instance from ~/.claude/tasks/{instance_id}/."""
    tasks_dir = Path.home() / ".claude" / "tasks" / instance_id

    if not tasks_dir.exists():
        return {"todos": [], "progress": 0, "current_task": None, "total": 0, "completed": 0}

    try:
        todos = []
        for task_file in tasks_dir.glob("*.json"):
            with open(task_file) as f:
                task = json.load(f)
                todos.append(task)

        if not todos:
            return {"todos": [], "progress": 0, "current_task": None, "total": 0, "completed": 0}

        # Sort by ID (numeric)
        todos.sort(key=lambda t: int(t.get("id", 0)))

        completed = sum(1 for t in todos if t.get("status") == "completed")
        total = len(todos)
        progress = int((completed / total) * 100) if total > 0 else 0

        current_task = None
        for t in todos:
            if t.get("status") == "in_progress":
                current_task = t.get("activeForm") or t.get("subject")
                break

        return {
            "todos": todos,
            "progress": progress,
            "completed": completed,
            "total": total,
            "current_task": current_task
        }
    except Exception as e:
        return {"todos": [], "progress": 0, "current_task": None, "total": 0, "completed": 0, "error": str(e)}


@app.post("/api/instances/{instance_id}/voice-chat")
async def toggle_voice_chat(instance_id: str, active: bool = True):
    """Toggle voice chat mode for an instance."""
    if active:
        VOICE_CHAT_SESSIONS[instance_id] = {
            "active": True,
            "listening": True,
            "started_at": datetime.now().isoformat()
        }
        logger.info(f"Voice chat STARTED for {instance_id[:12]}")
    else:
        VOICE_CHAT_SESSIONS.pop(instance_id, None)
        logger.info(f"Voice chat ENDED for {instance_id[:12]}")
    return {"instance_id": instance_id, "voice_chat": active}


@app.get("/api/instances/{instance_id}/voice-chat")
async def get_voice_chat_status(instance_id: str):
    """Check if instance is in voice chat mode."""
    session = VOICE_CHAT_SESSIONS.get(instance_id)
    return {"active": session is not None, "session": session}


@app.post("/api/instances/{instance_id}/voice-chat/listening")
async def toggle_listening(instance_id: str, active: bool = True):
    """Toggle listening (dictation/mic) state for a voice chat instance."""
    session = VOICE_CHAT_SESSIONS.get(instance_id)
    if not session:
        return {"error": "No active voice chat session", "status_code": 404}
    session["listening"] = active
    logger.info(f"Voice chat listening={'ON' if active else 'OFF'} for {instance_id[:12]}")
    return {"instance_id": instance_id, "listening": active}


@app.get("/api/instances", response_model=List[dict])
async def list_instances(status: Optional[str] = None, sort: Optional[str] = None):
    """List all instances, optionally filtered by status and sorted."""
    order_clauses = {
        "status": "status ASC, last_activity DESC",
        "recent_activity": "last_activity DESC",
        "recent_stopped": "stopped_at DESC NULLS LAST, last_activity DESC",
        "created": "registered_at DESC",
    }
    order_by = order_clauses.get(sort, "registered_at DESC")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if status:
            cursor = await db.execute(
                f"SELECT * FROM claude_instances WHERE status = ? ORDER BY {order_by}",
                (status,)
            )
        else:
            cursor = await db.execute(
                f"SELECT * FROM claude_instances ORDER BY {order_by}"
            )

        rows = await cursor.fetchall()
        instances = []
        for row in rows:
            inst = dict(row)
            vc_session = VOICE_CHAT_SESSIONS.get(inst["id"])
            if vc_session:
                inst["voice_chat"] = True
                inst["listening"] = vc_session.get("listening", False)
            instances.append(inst)
        return instances


@app.get("/api/instances/{instance_id}", response_model=dict)
async def get_instance(instance_id: str):
    """Get details of a specific instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        return dict(row)


# Dashboard Endpoint
@app.get("/api/dashboard", response_model=DashboardResponse)
async def get_dashboard():
    """Get dashboard data including instances, productivity status, and events."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get all instances
        cursor = await db.execute(
            "SELECT * FROM claude_instances ORDER BY status ASC, registered_at DESC"
        )
        instances = [dict(row) for row in await cursor.fetchall()]

        # Check productivity (any active instances = productive)
        active_count = sum(1 for i in instances if i["status"] in ("processing", "idle"))
        productivity_active = active_count > 0

        # Get recent events (last 20)
        cursor = await db.execute(
            "SELECT * FROM events ORDER BY created_at DESC LIMIT 20"
        )
        events = []
        for row in await cursor.fetchall():
            event = dict(row)
            if event.get("details"):
                try:
                    event["details"] = json.loads(event["details"])
                except:
                    pass
            events.append(event)

        return DashboardResponse(
            instances=instances,
            productivity_active=productivity_active,
            recent_events=events,
            tts_queue=get_tts_queue_status()
        )


class LogEventRequest(BaseModel):
    event_type: str
    instance_id: Optional[str] = None
    details: Optional[dict] = None


# Distraction apps that require productivity to be allowed
DISTRACTION_APPS = [
    "brave.exe",  # Browser (when showing YouTube)
    # Add more as needed
]

# Window titles that indicate distraction content
DISTRACTION_PATTERNS = [
    "YouTube",
    "Netflix",
    "Twitch",
    "Twitter",
    "Reddit",
]

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

# Desktop mode state (tracks current mode from AHK detection)
DESKTOP_STATE = {
    "current_mode": "silence",
    "last_detection": None,
    # Work mode: MANUAL only now (2026-02-26). User explicitly clocks in/out via /api/clock-in /api/clock-out.
    # Geofence no longer auto-sets work_mode (location ≠ work status).
    # Values: "clocked_in" (enforcement), "clocked_out" (no enforcement), "gym" (manual gym mode)
    "work_mode": "clocked_in",
    # Location zone tracking (geofence - just tracks where you are, doesn't affect work_mode)
    "location_zone": None,  # None = outside all zones, else: "home", "gym", "campus"
    # Grace period: ignore silence detections for 15s after startup to avoid
    # AHK restart race (AHK initializes with silence before detecting real state)
    "startup_time": time.time(),
    "startup_grace_secs": 15,
    "work_mode_changed_at": None,
    # AHK heartbeat tracking
    "ahk_reachable": None,
    "ahk_last_heartbeat": None,
    # Meeting mode: suppresses TTS when in a Zoom/Google Meet call
    "in_meeting": False,
}

# Voice chat state — tracks which instances are in voice conversation mode
VOICE_CHAT_SESSIONS = {}  # instance_id -> {"active": True, "started_at": str}

# Valid desktop detection modes (replaces OBSIDIAN_CONFIG["mode_commands"].keys())
VALID_DETECTION_MODES = ["silence", "music", "video", "scrolling", "gaming", "gym", "work_gym", "meeting"]

# ============ Timer Engine ============
timer_engine = TimerEngine(now_mono_ms=int(time.monotonic() * 1000))


def reset_idle_timer():
    """Signal productivity to the timer engine. Replaces old _last_work_event_ms tracking."""
    now_ms = int(time.monotonic() * 1000)
    timer_engine.set_productivity(True, now_ms)

# Paths for Obsidian vault
OBSIDIAN_VAULT_PATH = Path.home() / "Imperium-ENV"
OBSIDIAN_DAILY_PATH = OBSIDIAN_VAULT_PATH / "Terra" / "Journal" / "Daily"
OBSIDIAN_INBOX_PATH = OBSIDIAN_VAULT_PATH / "Terra" / "Inbox"


def _write_productivity_score(date_str: str, score: int):
    """Write productivity_score to a daily note's front matter."""
    try:
        note_path = OBSIDIAN_DAILY_PATH / f"{date_str}.md"
        if not note_path.exists():
            print(f"TIMER: No daily note for {date_str}, skipping score write")
            return

        content = note_path.read_text(encoding="utf-8")
        updated = _merge_frontmatter(content, {
            "productivity_score": score,
            "timer_completed": True,
        })
        note_path.write_text(updated, encoding="utf-8")
        print(f"TIMER: Wrote productivity score {score} to {date_str}")
    except Exception as e:
        print(f"TIMER: Failed to write productivity score: {e}")

# ============ Productivity Check-In System ============
DISCORD_CHECKIN_CHANNEL = "1472043387535495323"

# Discord response routing
DISCORD_DAEMON_URL = "http://127.0.0.1:7779"
MECHANICUS_USER_ID = "1472042705788866611"
MECHANICUS_ROLE_ID = "1477162726093492308"
CUSTODES_USER_ID   = "1477159418498912357"
OPERATOR_USER_ID   = "229461055628115968"
CUSTODES_CHANNELS  = {"briefing", "chat"}  # Channels where replies route to Custodes

CHECKIN_SCHEDULE = {
    "morning_start": {
        "cron": "0 9 * * 1-5",
        "name": "Morning Start",
        "time_suffix": "0900",
        "fields": ["energy", "focus", "mood", "notes"],
        "discord_message": (
            "**Morning Check-in**\n"
            "How are you starting the day?\n\n"
            "Reply with: `energy focus mood notes`\n"
            "Example: `7 8 good shipping auth refactor`\n\n"
            "Or submit via API: POST /api/checkin/submit"
        ),
        "tts_prompt": "Time for your morning check-in. How's your energy and focus?",
    },
    "mid_morning": {
        "cron": "30 10 * * 1-5",
        "name": "Mid-Morning",
        "time_suffix": "1030",
        "fields": ["focus", "on_track"],
        "discord_message": (
            "**Mid-Morning Check**\n"
            "Still locked in?\n\n"
            "Reply with: `focus on_track`\n"
            "Example: `6 yes`"
        ),
        "tts_prompt": "Mid-morning check. Are you still on track?",
    },
    "decision_point": {
        "cron": "0 11 * * 1-5",
        "name": "Decision Point",
        "time_suffix": "1100",
        "fields": ["energy", "plan"],
        "discord_message": (
            "**11 AM Decision Point**\n"
            "Gym now or power through?\n\n"
            "Reply with: `energy plan`\n"
            "Example: `5 gym` or `7 power_through`"
        ),
        "tts_prompt": "Decision point. Gym or power through?",
    },
    "afternoon": {
        "cron": "0 13 * * 1-5",
        "name": "Afternoon Start",
        "time_suffix": "1300",
        "fields": ["energy", "focus"],
        "discord_message": (
            "**Afternoon Check-in**\n"
            "Post-lunch status?\n\n"
            "Reply with: `energy focus`\n"
            "Example: `4 3`"
        ),
        "tts_prompt": "Afternoon check-in. How's the energy after lunch?",
    },
    "afternoon_check": {
        "cron": "30 14 * * 1-5",
        "name": "Afternoon Check",
        "time_suffix": "1430",
        "fields": ["energy", "focus", "notes"],
        "discord_message": (
            "**2:30 PM Check**\n"
            "Energy holding up? Need help with anything?\n\n"
            "Reply with: `energy focus notes`\n"
            "Example: `3 2 need to take a walk`"
        ),
        "tts_prompt": "Afternoon check. Energy holding up?",
    },
}


class CheckinSubmit(BaseModel):
    type: str  # checkin_type from CHECKIN_SCHEDULE
    energy: Optional[int] = None
    focus: Optional[int] = None
    mood: Optional[str] = None
    plan: Optional[str] = None
    notes: Optional[str] = None
    on_track: Optional[bool] = None


def send_discord_checkin(message: str):
    """Send check-in prompt to Discord via openclaw CLI."""
    try:
        # Use full path to avoid PATH issues when running as service
        cmd = [
            "/opt/homebrew/bin/openclaw", "message", "send",
            "--channel", "discord",
            "--target", DISCORD_CHECKIN_CHANNEL,
            "--message", message,
        ]
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception as e:
        logger.error(f"Failed to send Discord check-in: {e}")
        return False


def speak_checkin_tts(message: str):
    """Speak check-in prompt via TTS (non-blocking fire-and-forget)."""
    try:
        subprocess.Popen(
            ["say", "-v", "Daniel", "-r", "190", message],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as e:
        logger.error(f"Failed to speak check-in TTS: {e}")


async def trigger_checkin(checkin_type: str) -> dict:
    """Trigger a productivity check-in: Discord message + TTS nudge."""
    config = CHECKIN_SCHEDULE.get(checkin_type)
    if not config:
        return {"error": f"Unknown checkin type: {checkin_type}"}

    # Skip if not working
    work_mode = DESKTOP_STATE.get("work_mode", "clocked_in")
    if work_mode in ("clocked_out", "gym"):
        logger.info(f"Skipping check-in {checkin_type}: work_mode={work_mode}")
        return {"skipped": True, "reason": f"work_mode={work_mode}"}

    today = datetime.now().strftime("%Y-%m-%d")
    prompted_at = datetime.now().isoformat()

    # Log the prompt in the database
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO checkins (checkin_type, date, prompted_at)
            VALUES (?, ?, ?)
        """, (checkin_type, today, prompted_at))
        await db.commit()

    # Send Discord message
    discord_sent = send_discord_checkin(config["discord_message"])

    # TTS nudge
    speak_checkin_tts(config["tts_prompt"])

    logger.info(f"Check-in triggered: {checkin_type} (discord={discord_sent})")
    await log_event("checkin_prompted", details={
        "checkin_type": checkin_type,
        "discord_sent": discord_sent,
    })

    return {
        "checkin_type": checkin_type,
        "name": config["name"],
        "discord_sent": discord_sent,
        "prompted_at": prompted_at,
    }


DAILY_NOTE_DIR = Path.home() / "Imperium-ENV" / "Terra" / "Journal" / "Daily"


def update_daily_note_frontmatter(checkin_type: str, data: dict) -> bool:
    """Write time-stamped check-in fields to today's daily note frontmatter.

    Adds fields like energy_0900, focus_0900, etc. and updates top-level
    energy/focus/mood so meta-bind widgets reflect the latest values.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    note_path = DAILY_NOTE_DIR / f"{today}.md"

    if not note_path.exists():
        logger.warning(f"Daily note not found: {note_path}")
        return False

    try:
        content = note_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read daily note: {e}")
        return False

    # Parse frontmatter (between --- delimiters)
    if not content.startswith("---"):
        logger.warning("Daily note has no frontmatter")
        return False

    end_idx = content.index("---", 3)
    frontmatter = content[3:end_idx].strip()
    body = content[end_idx:]  # includes closing ---

    # Build new fields from check-in data
    config = CHECKIN_SCHEDULE.get(checkin_type, {})
    time_suffix = config.get("time_suffix", "")

    new_fields = {}
    if data.get("energy") is not None and time_suffix:
        new_fields[f"energy_{time_suffix}"] = data["energy"]
    if data.get("focus") is not None and time_suffix:
        new_fields[f"focus_{time_suffix}"] = data["focus"]
    if data.get("mood") is not None and time_suffix:
        new_fields[f"mood_{time_suffix}"] = data["mood"]
    if data.get("plan") is not None and time_suffix:
        new_fields[f"checkin_plan_{time_suffix}"] = data["plan"]
    if data.get("notes") is not None and time_suffix:
        new_fields[f"checkin_notes_{time_suffix}"] = f'"{data["notes"]}"'

    if not new_fields:
        return False

    # Parse existing frontmatter lines into ordered dict
    lines = frontmatter.split("\n")
    fm_lines = []
    existing_keys = set()
    for line in lines:
        if ":" in line:
            key = line.split(":", 1)[0].strip()
            existing_keys.add(key)
        fm_lines.append(line)

    # Update existing keys or append new ones
    for key, value in new_fields.items():
        field_line = f"{key}: {value}"
        if key in existing_keys:
            # Replace existing line
            for i, line in enumerate(fm_lines):
                if line.startswith(f"{key}:"):
                    fm_lines[i] = field_line
                    break
        else:
            fm_lines.append(field_line)

    # Also update top-level energy/focus/mood to latest value (for meta-bind widgets)
    top_level_updates = {}
    if data.get("energy") is not None:
        top_level_updates["energy"] = data["energy"]
    if data.get("focus") is not None:
        top_level_updates["focus"] = data["focus"]
    if data.get("mood") is not None:
        top_level_updates["mood"] = data["mood"]

    for key, value in top_level_updates.items():
        field_line = f"{key}: {value}"
        if key in existing_keys:
            for i, line in enumerate(fm_lines):
                if line.startswith(f"{key}:"):
                    fm_lines[i] = field_line
                    break
        else:
            fm_lines.append(field_line)

    # Reconstruct file
    new_frontmatter = "\n".join(fm_lines)
    new_content = f"---\n{new_frontmatter}\n{body}"

    try:
        note_path.write_text(new_content, encoding="utf-8")
        logger.info(f"Updated daily note frontmatter: {list(new_fields.keys())}")
        return True
    except Exception as e:
        logger.error(f"Failed to write daily note: {e}")
        return False


@app.get("/api/daily-note")
async def get_daily_note():
    """Return today's daily note content as plain text."""
    today = datetime.now().strftime("%Y-%m-%d")
    note_path = DAILY_NOTE_DIR / f"{today}.md"
    if not note_path.exists():
        return {"date": today, "content": None, "exists": False}
    content = note_path.read_text(encoding="utf-8")
    return {"date": today, "content": content, "exists": True, "path": str(note_path)}


class DailyNoteAppendRequest(BaseModel):
    content: str
    section: str = ""  # optional section header; if provided, used as ## heading


@app.post("/api/daily-note/append")
async def append_daily_note(request: DailyNoteAppendRequest):
    """Append a timestamped section to today's daily note."""
    today = datetime.now().strftime("%Y-%m-%d")
    note_path = DAILY_NOTE_DIR / f"{today}.md"
    now_str = datetime.now().strftime("%H:%M")

    if request.section:
        block = f"\n## {request.section} ({now_str})\n\n{request.content}\n"
    else:
        block = f"\n<!-- {now_str} -->\n{request.content}\n"

    if not note_path.exists():
        return {"ok": False, "error": f"Daily note not found: {note_path}"}

    with open(note_path, "a", encoding="utf-8") as f:
        f.write(block)

    return {"ok": True, "date": today, "appended_chars": len(block)}


# Phone HTTP server config (MacroDroid on phone via Tailscale)
PHONE_CONFIG = {
    "host": "100.102.92.24",
    "port": 7777,
    "timeout": 5,
    # === TEST SHIM - REMOVE AFTER TESTING ===
    # Set to True to bypass break time check and force blocking
    "test_force_block": False,
    # =========================================
}

# Last widget state pushed to phone (dedup)
_last_widget_push = {"mode": None, "active": None}


def push_phone_widget(mode: str, active_count: int):
    """Push timer mode + active instance count to phone MacroDroid widget endpoint.

    Only pushes if the state actually changed (deduped via _last_widget_push).
    Runs synchronously via requests (fire-and-forget from async via create_task + to_thread).
    """
    if _last_widget_push["mode"] == mode and _last_widget_push["active"] == active_count:
        return  # no change

    host = PHONE_CONFIG["host"]
    port = PHONE_CONFIG["port"]
    timeout = PHONE_CONFIG["timeout"]
    url = f"http://{host}:{port}/widget-update?mode={mode}&instances={active_count}"

    try:
        response = requests.get(url, timeout=timeout)
        _last_widget_push["mode"] = mode
        _last_widget_push["active"] = active_count
        print(f"WIDGET: Pushed mode={mode} instances={active_count} -> {response.status_code}")
    except Exception as e:
        print(f"WIDGET: Push failed: {e}")


async def push_phone_widget_async(mode: str, active_count: int):
    """Async wrapper for push_phone_widget."""
    await asyncio.to_thread(push_phone_widget, mode, active_count)


# Phone activity state (tracks current app from MacroDroid)
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

TWITTER_ZAP_COOLDOWN_FILE = DB_PATH.parent / "twitter_zap_cooldown.txt"
TWITTER_ZAP_COOLDOWN_SECS = 1800  # 30 minutes


def _persist_twitter_zap_cooldown():
    """Write twitter zap wall-clock time to file so it survives restarts."""
    try:
        TWITTER_ZAP_COOLDOWN_FILE.write_text(str(time.time()))
    except Exception as e:
        print(f"WARN: Failed to persist twitter zap cooldown: {e}")


def _restore_twitter_zap_cooldown():
    """On startup, restore twitter zap cooldown from file.
    If a zap happened less than 30 min ago, set twitter_zapped=True to block phantom opens."""
    try:
        if TWITTER_ZAP_COOLDOWN_FILE.exists():
            last_zap_wall = float(TWITTER_ZAP_COOLDOWN_FILE.read_text().strip())
            elapsed = time.time() - last_zap_wall
            if elapsed < TWITTER_ZAP_COOLDOWN_SECS:
                PHONE_STATE["twitter_zapped"] = True
                PHONE_STATE["twitter_last_zap_wall"] = last_zap_wall
                print(f"STARTUP: Twitter zap cooldown restored ({elapsed:.0f}s ago, {TWITTER_ZAP_COOLDOWN_SECS - elapsed:.0f}s remaining). Phantom opens blocked.")
            else:
                print(f"STARTUP: Twitter zap cooldown expired ({elapsed:.0f}s ago). Clearing file.")
                TWITTER_ZAP_COOLDOWN_FILE.unlink(missing_ok=True)
    except Exception as e:
        print(f"WARN: Failed to restore twitter zap cooldown: {e}")


# Shizuku restart state
SHIZUKU_STATE = {
    "dead": False,
    "last_death": None,  # ISO timestamp
    "last_restart_attempt": None,  # ISO timestamp
    "restart_count": 0,  # total restarts since server start
    "consecutive_failures": 0,  # consecutive restart failures (resets on success)
}

# Shizuku restart via shizuku-connect CLI (ADB over Tailscale, port 5555)
SHIZUKU_CONFIG = {
    "restart_cooldown_seconds": 60,
    "max_consecutive_failures": 5,
}


async def attempt_shizuku_restart() -> dict:
    """
    Restart Shizuku via shizuku-connect CLI (ADB over Tailscale port 5555).
    No wireless debugging required — uses persistent ADB TCP connection.
    """
    now = datetime.now()

    # Check cooldown
    if SHIZUKU_STATE["last_restart_attempt"]:
        last = datetime.fromisoformat(SHIZUKU_STATE["last_restart_attempt"])
        elapsed = (now - last).total_seconds()
        if elapsed < SHIZUKU_CONFIG["restart_cooldown_seconds"]:
            return {"success": False, "reason": "cooldown", "wait_seconds": round(SHIZUKU_CONFIG["restart_cooldown_seconds"] - elapsed)}

    if SHIZUKU_STATE["consecutive_failures"] >= SHIZUKU_CONFIG["max_consecutive_failures"]:
        return {"success": False, "reason": "max_failures_reached", "failures": SHIZUKU_STATE["consecutive_failures"]}

    SHIZUKU_STATE["last_restart_attempt"] = now.isoformat()
    logger.info(f"Shizuku: attempting restart via shizuku-connect (attempt #{SHIZUKU_STATE['restart_count'] + 1})")

    try:
        proc = await asyncio.create_subprocess_exec(
            "shizuku-connect", "start",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
        output = stdout.decode().strip()

        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.warning(f"Shizuku: shizuku-connect start failed: {err}")
            SHIZUKU_STATE["consecutive_failures"] += 1
            return {"success": False, "reason": "start_failed", "output": output, "error": err}

        SHIZUKU_STATE["restart_count"] += 1
        SHIZUKU_STATE["consecutive_failures"] = 0
        SHIZUKU_STATE["dead"] = False
        logger.info(f"Shizuku: restart successful (total: {SHIZUKU_STATE['restart_count']})")
        return {"success": True, "output": output, "restart_count": SHIZUKU_STATE["restart_count"]}

    except asyncio.TimeoutError:
        SHIZUKU_STATE["consecutive_failures"] += 1
        logger.warning("Shizuku: restart timed out")
        return {"success": False, "reason": "timeout"}
    except Exception as e:
        SHIZUKU_STATE["consecutive_failures"] += 1
        logger.warning(f"Shizuku: restart failed: {e}")
        return {"success": False, "reason": "exception", "error": str(e)}

# App categories for phone distraction detection
PHONE_DISTRACTION_APPS = {
    # Twitter/X
    "twitter": "scrolling",
    "x": "scrolling",
    "com.twitter.android": "scrolling",
    # YouTube
    "youtube": "video",
    "com.google.android.youtube": "video",
    # Games - add specific games here
    "game": "gaming",
    "minecraft": "gaming",
    "com.mojang.minecraftpe": "gaming",
}

# Human-readable display names for phone apps (key = lowercased app name or package)
PHONE_APP_DISPLAY_NAMES = {
    "twitter": "Twitter/X",
    "x": "Twitter/X",
    "com.twitter.android": "Twitter/X",
    "youtube": "YouTube",
    "com.google.android.youtube": "YouTube",
    "game": "Game",
    "minecraft": "Minecraft",
    "com.mojang.minecraftpe": "Minecraft",
}


def get_phone_app_display_name(app_name: str, package: str = None) -> str:
    """Get human-readable display name for a phone app.

    Checks app_name first, then package name, falls back to title-cased app_name.
    """
    if app_name in PHONE_APP_DISPLAY_NAMES:
        return PHONE_APP_DISPLAY_NAMES[app_name]
    if package and package in PHONE_APP_DISPLAY_NAMES:
        return PHONE_APP_DISPLAY_NAMES[package]
    # Fallback: title-case the app name, strip common package prefixes
    if "." in app_name:
        # Package name like com.foo.bar -> use last segment, title-cased
        return app_name.split(".")[-1].title()
    return app_name.title()

# ============ Pavlok Shock Watch ============
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


def send_pavlok_stimulus(
    stimulus_type: str = "zap",
    value: int | None = None,
    reason: str = "manual",
    respect_cooldown: bool = True,
) -> dict:
    """Send a stimulus (zap/beep/vibe) to the Pavlok watch."""
    if not PAVLOK_CONFIG["token"]:
        return {"skipped": True, "reason": "no_token", "hint": "Set PAVLOK_API_TOKEN in .env"}
    if not PAVLOK_CONFIG["enabled"]:
        return {"skipped": True, "reason": "disabled"}

    now = datetime.now()
    if respect_cooldown and PAVLOK_STATE["last_stimulus_at"]:
        last = datetime.fromisoformat(PAVLOK_STATE["last_stimulus_at"])
        elapsed = (now - last).total_seconds()
        if elapsed < PAVLOK_CONFIG["cooldown_seconds"]:
            return {"skipped": True, "reason": "cooldown", "remaining": round(PAVLOK_CONFIG["cooldown_seconds"] - elapsed)}

    if value is None:
        value = PAVLOK_CONFIG["default_zap_value"]

    try:
        response = requests.post(
            PAVLOK_CONFIG["api_url"],
            headers={"Authorization": PAVLOK_CONFIG["token"]},
            json={"stimulus": {"stimulusType": stimulus_type, "stimulusValue": value}},
            timeout=10,
        )
        PAVLOK_STATE["last_stimulus_at"] = now.isoformat()
        print(f"PAVLOK: {stimulus_type} value={value} reason={reason} -> {response.status_code}")
        return {
            "success": response.status_code == 200,
            "type": stimulus_type,
            "value": value,
            "reason": reason,
            "status_code": response.status_code,
        }
    except requests.exceptions.Timeout:
        print(f"PAVLOK: Timeout sending {stimulus_type}")
        return {"success": False, "error": "timeout", "reason": reason}
    except requests.exceptions.ConnectionError:
        print(f"PAVLOK: Connection error sending {stimulus_type}")
        return {"success": False, "error": "connection_error", "reason": reason}
    except Exception as e:
        print(f"PAVLOK: Error sending {stimulus_type}: {e}")
        return {"success": False, "error": str(e), "reason": reason}


async def check_instance_count_pavlok(remaining_active: int, was_active: int):
    """Send Pavlok signals when Claude instance count drops critically.

    - Drops to 1 (from 2+): double vibe as warning
    - Drops to 0: zap as penalty
    Skips if was_active was already at or below threshold (no regression).
    """
    if remaining_active == 1 and was_active >= 2:
        print(f"PAVLOK: Instance count dropped to 1 (from {was_active}), double vibe")
        send_pavlok_stimulus(stimulus_type="vibe", value=50, reason="one_claude_remaining", respect_cooldown=False)
        await asyncio.sleep(3)
        send_pavlok_stimulus(stimulus_type="vibe", value=50, reason="one_claude_remaining", respect_cooldown=False)
        await log_event("instance_count_warning", details={"remaining": 1, "was": was_active})
    elif remaining_active == 0 and was_active >= 1:
        print(f"PAVLOK: All Claude instances stopped, zap")
        send_pavlok_stimulus(stimulus_type="zap", value=50, reason="all_claudes_stopped", respect_cooldown=False)
        await log_event("instance_count_zero", details={"was": was_active})


# ============ Timer I/O Functions ============


def _sync_log_shift(old_mode: str | None, new_mode: str, trigger: str, source: str,
                    phone_app: str | None = None, details: str | None = None):
    """Log a timer mode shift to the analytics table (sync, for thread offload)."""
    import sqlite3
    from datetime import datetime as _dt
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")

    # Get active non-subagent instance count
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


async def timer_log_shift(old_mode: str | None, new_mode: str, trigger: str, source: str,
                          phone_app: str | None = None, details: str | None = None):
    """Log a timer mode shift to the analytics table (async wrapper)."""
    try:
        await asyncio.to_thread(_sync_log_shift, old_mode, new_mode, trigger, source, phone_app, details)
    except Exception as e:
        print(f"TIMER: Failed to log shift: {e}")


def _sync_generate_daily_analytics(date_str: str):
    """Generate daily timer analytics from timer_shifts.

    Writes:
    1. Summary fields to the daily note's YAML front matter
    2. Full JSON to Imperium-ENV/Journal/Daily/analytics/ for programmatic access
    Then wipes timer_shifts table.
    """
    import sqlite3
    import json
    from collections import defaultdict

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT * FROM timer_shifts ORDER BY id").fetchall()

    if not rows:
        conn.close()
        return None

    # Compute analytics
    shift_count_by_trigger = defaultdict(int)
    shift_count_by_source = defaultdict(int)
    enforcement_count = 0
    twitter_shifts = 0
    modes_seen = set()
    peak_balance = 0
    min_balance = float("inf")
    instance_counts = []
    # Break balance time series (for sparkline in JSON)
    balance_timeline = []

    for r in rows:
        shift_count_by_trigger[r["trigger"] or "unknown"] += 1
        shift_count_by_source[r["source"] or "unknown"] += 1
        if r["trigger"] == "enforcement":
            enforcement_count += 1
        if r["phone_app"] and "twitter" in (r["phone_app"] or "").lower():
            twitter_shifts += 1
        modes_seen.add(r["new_mode"])
        if r["old_mode"]:
            modes_seen.add(r["old_mode"])
        bal = r["break_balance_ms"] or 0
        peak_balance = max(peak_balance, bal)
        min_balance = min(min_balance, bal)
        if r["active_instances"] is not None:
            instance_counts.append(r["active_instances"])
        balance_timeline.append({"time": r["timestamp"], "balance_ms": bal})

    summary = {
        "date": date_str,
        "total_shifts": len(rows),
        "shifts_by_trigger": dict(shift_count_by_trigger),
        "shifts_by_source": dict(shift_count_by_source),
        "enforcement_events": enforcement_count,
        "twitter_shifts": twitter_shifts,
        "modes_seen": sorted(modes_seen),
        "peak_break_balance_ms": peak_balance,
        "min_break_balance_ms": min_balance if min_balance != float("inf") else 0,
        "avg_active_instances": round(sum(instance_counts) / len(instance_counts), 1) if instance_counts else 0,
        "max_active_instances": max(instance_counts) if instance_counts else 0,
        "balance_timeline": balance_timeline,
    }

    # 1. Write full JSON to Imperium-ENV analytics dir
    analytics_dir = OBSIDIAN_DAILY_PATH / "analytics"
    analytics_dir.mkdir(parents=True, exist_ok=True)
    out_path = analytics_dir / f"timer-{date_str}.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)

    # 2. Write summary fields to daily note frontmatter
    note_path = OBSIDIAN_DAILY_PATH / f"{date_str}.md"
    if note_path.exists():
        content = note_path.read_text(encoding="utf-8")
        fm_updates = {
            "timer_total_shifts": summary["total_shifts"],
            "timer_enforcements": enforcement_count,
            "timer_twitter_shifts": twitter_shifts,
            "timer_peak_break": format_timer_time(peak_balance),
            "timer_min_break": format_timer_time(min_balance if min_balance != float("inf") else 0),
            "timer_avg_instances": summary["avg_active_instances"],
            "timer_max_instances": summary["max_active_instances"],
        }
        updated = _merge_frontmatter(content, fm_updates)
        note_path.write_text(updated, encoding="utf-8")

    # Wipe timer_shifts table
    conn.execute("DELETE FROM timer_shifts")
    conn.commit()
    conn.close()

    return str(out_path)


async def generate_daily_timer_analytics(date_str: str):
    """Generate and save daily timer analytics (async wrapper)."""
    try:
        result = await asyncio.to_thread(_sync_generate_daily_analytics, date_str)
        if result:
            print(f"TIMER: Daily analytics written to {result}")
            await log_event("timer_daily_analytics_generated", details={"file": result, "date": date_str})
        else:
            print(f"TIMER: No shift data for {date_str}, skipping analytics")
    except Exception as e:
        print(f"TIMER: Failed to generate daily analytics: {e}")


def _merge_frontmatter(content: str, updates: dict) -> str:
    """Merge key-value pairs into a markdown file's YAML front matter."""
    lines = content.split("\n")

    # Find existing front matter boundaries
    fm_start = -1
    fm_end = -1
    for i, line in enumerate(lines):
        if line.strip() == "---":
            if fm_start == -1:
                fm_start = i
            else:
                fm_end = i
                break

    if fm_start == -1 or fm_end == -1:
        # No front matter - create it
        fm_lines = ["---"]
        for key, value in updates.items():
            fm_lines.append(f"{key}: {_format_yaml_value(value)}")
        fm_lines.append("---")
        return "\n".join(fm_lines) + "\n" + content

    # Parse existing front matter
    existing = {}
    for i in range(fm_start + 1, fm_end):
        line = lines[i]
        if ": " in line:
            key, _, val = line.partition(": ")
            existing[key.strip()] = val.strip()

    # Merge updates
    existing.update({k: _format_yaml_value(v) for k, v in updates.items()})

    # Rebuild
    fm_lines = ["---"]
    for key, value in existing.items():
        fm_lines.append(f"{key}: {value}")
    fm_lines.append("---")

    before = lines[:fm_start]
    after = lines[fm_end + 1:]
    return "\n".join(before + fm_lines + after)


def _format_yaml_value(value) -> str:
    """Format a Python value for YAML front matter."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and (":" in value or " " in value):
        return f'"{value}"'
    return str(value)


def _sync_update_daily_note():
    """Update daily note synchronically (called via asyncio.to_thread)."""
    import sqlite3
    today = datetime.now().strftime("%Y-%m-%d")
    note_path = OBSIDIAN_DAILY_PATH / f"{today}.md"
    if not note_path.exists():
        return
    
    # Get session count and mode change count for today
    session_count = 0
    mode_change_count = 0
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=5000")
        session_count = conn.execute(
            "SELECT COUNT(*) FROM timer_sessions WHERE date = ?", (today,)
        ).fetchone()[0] or 0
        mode_change_count = conn.execute(
            "SELECT COUNT(*) FROM timer_mode_changes WHERE timestamp LIKE ?", (f"{today}%",)
        ).fetchone()[0] or 0
        conn.close()
    except Exception:
        pass  # Silently skip if DB query fails
    
    content = note_path.read_text(encoding="utf-8")
    updates = {
        "timer_status": timer_engine.current_mode.value,
        "timer_work_time": format_timer_time(timer_engine.total_work_time_ms),
        "timer_break_earned": format_timer_time(
            max(0, timer_engine.break_balance_ms) + timer_engine.total_break_time_ms
        ),
        "timer_break_used": format_timer_time(timer_engine.total_break_time_ms),
        "timer_break_available": format_timer_time(max(0, timer_engine.break_balance_ms)),
        "timer_backlog": format_timer_time(abs(min(0, timer_engine.break_balance_ms))),
        "timer_sessions": session_count,
        "timer_mode_changes": mode_change_count,
        "last_timer_update": datetime.now().strftime("%H:%M:%S"),
    }
    updated = _merge_frontmatter(content, updates)
    note_path.write_text(updated, encoding="utf-8")


async def timer_update_daily_note():
    """Update today's daily note front matter asynchronously."""
    try:
        await asyncio.to_thread(_sync_update_daily_note)
    except Exception as e:
        print(f"TIMER: Failed to update daily note: {e}")


def _sync_save_to_db(state_json: str):
    """Save timer state to SQLite synchronously (called via asyncio.to_thread)."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """INSERT INTO timer_state (id, state_json, updated_at)
           VALUES (1, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json, updated_at = CURRENT_TIMESTAMP""",
        (state_json,)
    )
    conn.commit()
    conn.close()


async def timer_save_to_db():
    """Save timer state to SQLite asynchronously."""
    try:
        now_ms = int(time.monotonic() * 1000)
        state_json = json.dumps(timer_engine.to_dict(now_ms))
        await asyncio.to_thread(_sync_save_to_db, state_json)
    except Exception as e:
        print(f"TIMER: Failed to save to DB: {e}")


def _sync_log_mode_change(old_mode: str | None, new_mode: str, is_automatic: bool):
    """Log a mode change to the database synchronously."""
    import sqlite3
    from datetime import datetime
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """INSERT INTO timer_mode_changes (timestamp, old_mode, new_mode, is_automatic)
           VALUES (?, ?, ?, ?)""",
        (datetime.now().isoformat(), old_mode, new_mode, 1 if is_automatic else 0)
    )
    conn.commit()
    conn.close()


async def timer_log_mode_change(old_mode: str | None, new_mode: str, is_automatic: bool):
    """Log a timer mode change asynchronously."""
    try:
        await asyncio.to_thread(_sync_log_mode_change, old_mode, new_mode, is_automatic)
    except Exception as e:
        print(f"TIMER: Failed to log mode change: {e}")


def _sync_start_session(mode: str, date: str):
    """Start a new timer session."""
    import sqlite3
    from datetime import datetime
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    cursor = conn.execute(
        """INSERT INTO timer_sessions (date, start_time, mode)
           VALUES (?, ?, ?)""",
        (date, datetime.now().isoformat(), mode)
    )
    conn.commit()
    session_id = cursor.lastrowid
    conn.close()
    return session_id


async def timer_start_session(mode: str, date: str) -> int:
    """Start a new timer session asynchronously. Returns session ID."""
    try:
        return await asyncio.to_thread(_sync_start_session, mode, date)
    except Exception as e:
        print(f"TIMER: Failed to start session: {e}")
        return 0


def _sync_end_session(session_id: int, duration_ms: int, break_earned_ms: int = 0, break_used_ms: int = 0):
    """End a timer session."""
    import sqlite3
    from datetime import datetime
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """UPDATE timer_sessions SET end_time = ?, duration_ms = ?, break_earned_ms = ?, break_used_ms = ?
           WHERE id = ?""",
        (datetime.now().isoformat(), duration_ms, break_earned_ms, break_used_ms, session_id)
    )
    conn.commit()
    conn.close()


async def timer_end_session(session_id: int, duration_ms: int, break_earned_ms: int = 0, break_used_ms: int = 0):
    """End a timer session asynchronously."""
    try:
        await asyncio.to_thread(_sync_end_session, session_id, duration_ms, break_earned_ms, break_used_ms)
    except Exception as e:
        print(f"TIMER: Failed to end session: {e}")


def _sync_save_daily_score(date: str, productivity_score: int, total_work_ms: int, total_break_used_ms: int, session_count: int, mode_change_count: int):
    """Save daily productivity score."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """INSERT INTO timer_daily_scores (date, productivity_score, total_work_ms, total_break_used_ms, session_count, mode_change_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(date) DO UPDATE SET 
               productivity_score = excluded.productivity_score,
               total_work_ms = excluded.total_work_ms,
               total_break_used_ms = excluded.total_break_used_ms,
               session_count = excluded.session_count,
               mode_change_count = excluded.mode_change_count,
               updated_at = CURRENT_TIMESTAMP""",
        (date, productivity_score, total_work_ms, total_break_used_ms, session_count, mode_change_count)
    )
    conn.commit()
    conn.close()


async def timer_save_daily_score(date: str, productivity_score: int, total_work_ms: int, total_break_used_ms: int, session_count: int, mode_change_count: int):
    """Save daily productivity score asynchronously."""
    try:
        await asyncio.to_thread(_sync_save_daily_score, date, productivity_score, total_work_ms, total_break_used_ms, session_count, mode_change_count)
    except Exception as e:
        print(f"TIMER: Failed to save daily score: {e}")


def timer_load_from_db():
    """Load timer state from DB on startup."""
    import sqlite3
    now_ms = int(time.monotonic() * 1000)
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=5000")
        row = conn.execute("SELECT state_json FROM timer_state WHERE id = 1").fetchone()
        conn.close()

        if row:
            saved = json.loads(row[0])
            timer_engine.from_dict(saved, now_mono_ms=now_ms)
            print(f"TIMER: Restored state from DB (mode={timer_engine.current_mode.value}, break={timer_engine.break_balance_ms / 1000:.0f}s)")
            return
    except Exception as e:
        print(f"TIMER: DB load failed: {e}")

    # Fresh start
    print("TIMER: Fresh start (no DB state found)")


async def timer_9am_reset():
    """9 AM daily reset: clear accumulated break, wipe prior-day timer events."""
    import sqlite3
    today = datetime.now().strftime("%Y-%m-%d")
    now_ms = int(time.monotonic() * 1000)

    result = timer_engine.force_daily_reset(now_ms, today)
    await timer_save_to_db()

    # Wipe timer_mode_change and break events from previous days
    try:
        def _wipe_old_timer_events():
            conn = sqlite3.connect(DB_PATH)
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                "DELETE FROM events WHERE event_type IN ('timer_mode_change','break_exhausted_enforcement')"
                " AND DATE(created_at) < DATE('now','localtime')"
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(_wipe_old_timer_events)
    except Exception as e:
        print(f"TIMER: Failed to wipe old timer events: {e}")

    print(f"TIMER: 9 AM daily reset complete (productivity_score={result.productivity_score})")
    await log_event("timer_daily_reset", details={"source": "9am_scheduler", "productivity_score": result.productivity_score, "date": today})


# ============ Audio Proxy State ============
# Tracks phone audio proxy status for routing phone audio through PC

AUDIO_PROXY_STATE = {
    "phone_connected": False,
    "receiver_running": False,
    "receiver_pid": None,
    "last_connect_time": None,
    "last_disconnect_time": None,
}

# ============ Headless Mode (disabled on macOS) ============

def get_headless_state() -> dict:
    """Headless mode is not applicable on macOS."""
    return {"enabled": False, "last_changed": None, "hostname": None, "error": "not applicable on macOS"}


async def poll_for_state_change(
    get_state_fn: callable,
    key: str,
    original_value: any,
    timeout: float = 5.0,
    initial_interval: float = 0.1,
    max_interval: float = 0.5,
) -> tuple[bool, dict]:
    """
    Poll a state function until a key's value changes or timeout.

    Uses exponential backoff starting at initial_interval, capped at max_interval.
    This is a generic utility for waiting on async external operations that update
    state files (e.g., Windows scheduled tasks).

    Args:
        get_state_fn: Function that returns current state dict
        key: Key to monitor for changes
        original_value: Original value to compare against
        timeout: Max seconds to wait
        initial_interval: Initial poll interval in seconds
        max_interval: Maximum poll interval in seconds

    Returns:
        (changed: bool, final_state: dict)
    """
    elapsed = 0.0
    interval = initial_interval

    while elapsed < timeout:
        await asyncio.sleep(interval)
        elapsed += interval

        state = get_state_fn()
        if state.get(key) != original_value:
            return True, state

        # Exponential backoff
        interval = min(interval * 1.5, max_interval)

    # Timeout - return final state anyway
    return False, get_state_fn()


def trigger_headless_task(action: str = "toggle") -> tuple[bool, str]:
    """Headless mode is not applicable on macOS."""
    return False, "Headless mode not available on macOS"


def start_audio_receiver() -> dict:
    """Audio proxy is not available on macOS."""
    return {"success": False, "error": "not available on macOS"}


def stop_audio_receiver() -> dict:
    """Audio proxy is not available on macOS."""
    return {"success": True, "stopped_count": 0}


def check_audio_receiver_running() -> dict:
    """Audio proxy is not available on macOS."""
    return {"running": False, "pid": None}


def close_distraction_windows() -> dict:
    """
    Close distraction windows on Windows via token-satellite.

    Mode-aware enforcement:
    - video mode → close brave (YouTube in browser)
    - gaming mode → close minecraft
    """
    current_mode = DESKTOP_STATE.get("current_mode", "silence")

    # Map modes to apps to close
    mode_targets = {
        "video": ["brave"],
        "gaming": ["minecraft"],
    }

    targets = mode_targets.get(current_mode, [])
    if not targets:
        logger.info(f"ENFORCE: No targets for mode '{current_mode}'")
        return {"success": True, "closed_count": 0, "mode": current_mode}

    results = []
    for app in targets:
        result = enforce_desktop_app(app, "close")
        results.append(result)

    closed = sum(1 for r in results if r.get("success"))
    logger.info(f"ENFORCE: Closed {closed}/{len(targets)} targets for mode '{current_mode}'")
    return {"success": closed > 0 or not targets, "closed_count": closed, "results": results}


def enforce_desktop_app(app_name: str, action: str = "close") -> dict:
    """Send enforcement command to Windows via token-satellite."""
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    timeout = DESKTOP_CONFIG["timeout"]

    url = f"http://{host}:{port}/enforce"

    try:
        response = requests.post(
            url,
            json={"app": app_name, "action": action},
            timeout=timeout,
        )
        logger.info(f"DESKTOP: Enforce {action} {app_name} -> {response.status_code}")
        return {
            "success": response.status_code == 200,
            "app": app_name,
            "status_code": response.status_code,
            "response": response.json() if response.status_code == 200 else response.text,
        }
    except Exception as e:
        logger.error(f"DESKTOP: Error enforcing {action} {app_name}: {e}")
        DESKTOP_STATE["ahk_reachable"] = False
        return {"success": False, "app": app_name, "error": str(e)}


def check_desktop_reachable() -> dict:
    """Check if Windows satellite server is reachable."""
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    timeout = DESKTOP_CONFIG["timeout"]

    url = f"http://{host}:{port}/health"

    try:
        response = requests.get(url, timeout=timeout)
        DESKTOP_STATE["ahk_reachable"] = True
        DESKTOP_STATE["ahk_last_heartbeat"] = datetime.now().isoformat()
        return {"reachable": True, "status_code": response.status_code}
    except Exception:
        DESKTOP_STATE["ahk_reachable"] = False
        return {"reachable": False}


def trigger_obsidian_command_async(command_id: str, no_focus: bool = False):
    """Fire-and-forget Obsidian trigger (log-only on macOS)."""
    trigger_obsidian_command(command_id, no_focus)


def trigger_obsidian_command(command_id: str, no_focus: bool = False) -> bool:
    """Log Obsidian command (Obsidian is just a log sink now, not a runtime dependency)."""
    logger.info(f"OBSIDIAN: command '{command_id}' (log-only, no_focus={no_focus})")
    return True


def enforce_phone_app(app_name: str, action: str = "disable") -> dict:
    """
    Send enforcement command to phone via MacroDroid HTTP server.

    Args:
        app_name: App to enable/disable (twitter, youtube, etc.)
        action: "disable" or "enable"

    Returns:
        dict with success status and details
    """
    host = PHONE_CONFIG["host"]
    port = PHONE_CONFIG["port"]
    timeout = PHONE_CONFIG["timeout"]

    url = f"http://{host}:{port}/enforce"
    params = {"action": action, "app": app_name}

    try:
        response = requests.get(url, params=params, timeout=timeout)
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()

        print(f"PHONE: Enforce {action} {app_name} -> {response.status_code}")
        return {
            "success": response.status_code == 200,
            "status_code": response.status_code,
            "response": response.text[:200] if response.text else None
        }
    except requests.exceptions.Timeout:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE: Timeout enforcing {action} {app_name}")
        return {"success": False, "error": "timeout"}
    except requests.exceptions.ConnectionError:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE: Connection refused enforcing {action} {app_name}")
        return {"success": False, "error": "connection_refused"}
    except Exception as e:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        print(f"PHONE: Error enforcing {action} {app_name}: {e}")
        return {"success": False, "error": str(e)}


def check_phone_reachable() -> dict:
    """
    Check if phone is reachable via heartbeat endpoint.

    Returns:
        dict with reachable status
    """
    host = PHONE_CONFIG["host"]
    port = PHONE_CONFIG["port"]
    timeout = PHONE_CONFIG["timeout"]

    url = f"http://{host}:{port}/heartbeat"

    try:
        response = requests.get(url, timeout=timeout)
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        return {"reachable": True, "status_code": response.status_code}
    except Exception:
        PHONE_STATE["reachable"] = False
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()
        return {"reachable": False}


@app.post("/api/window/enforce", response_model=WindowEnforceResponse)
async def check_window_enforcement(request: WindowCheckRequest = None):
    """
    Check if distraction windows should be closed based on productivity status.

    This is the authoritative endpoint for AHK to determine whether to close
    distraction windows (like YouTube in Brave).

    Logic:
    - If at least one Claude instance is active -> productivity is active
    - If productivity is active -> distractions are allowed (earned break)
    - If productivity is NOT active -> distractions should be closed
    """
    async with aiosqlite.connect(DB_PATH) as db:
        # Count active Claude instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0

    productivity_active = active_count > 0
    should_close = not productivity_active

    if productivity_active:
        reason = f"productivity_active:{active_count}_instances"
    else:
        reason = "no_productive_activity"

    # Log the enforcement check
    await log_event(
        "window_enforce_check",
        details={
            "productivity_active": productivity_active,
            "active_instances": active_count,
            "should_close": should_close,
            "source": request.source if request else "unknown",
            "window_title": request.window_title if request else None
        }
    )

    return WindowEnforceResponse(
        productivity_active=productivity_active,
        active_instance_count=active_count,
        should_close_distractions=should_close,
        distraction_apps=DISTRACTION_APPS,
        reason=reason
    )


@app.get("/api/window/enforce", response_model=WindowEnforceResponse)
async def check_window_enforcement_get():
    """GET version of window enforcement check (simpler for AHK to call)."""
    return await check_window_enforcement(None)


@app.post("/api/window/close")
async def trigger_window_close():
    """
    Manually trigger closing of distraction windows.
    This is a push-based enforcement that token-api executes directly.
    """
    result = close_distraction_windows()

    await log_event(
        "manual_enforcement",
        details={"result": result}
    )

    return {
        "action": "close_distractions",
        "result": result
    }


@app.post("/desktop", response_model=DesktopDetectionResponse)
async def handle_desktop_detection(request: DesktopDetectionRequest):
    """
    Handle desktop detection events from AHK.
    This is the authoritative endpoint for mode changes.

    AHK detects: video/music/gaming/silence
    token-api: decides if mode change is allowed, updates internal timer

    Logic:
    - If work_mode is "clocked_out" -> all modes allowed, no enforcement
    - If work_mode is "gym" -> gym timer mode, all modes allowed
    - Video mode (distraction) requires productivity to be active when clocked_in
    - Other modes (music, gaming, silence) are always allowed
    """
    detected_mode = request.detected_mode.lower()
    window_title = request.window_title or ""
    source = request.source

    # Validate detected mode
    if detected_mode not in VALID_DETECTION_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid detected_mode '{detected_mode}'. Valid: {VALID_DETECTION_MODES}"
        )

    work_mode = DESKTOP_STATE.get("work_mode", "clocked_in")
    print(f">>> Desktop detection from {source}: mode={detected_mode} window='{window_title}' work_mode={work_mode}")

    # Get current mode
    current_mode = DESKTOP_STATE["current_mode"]

    # Check if mode change is needed
    if detected_mode == current_mode:
        print(f"    Mode unchanged ({detected_mode}), skipping")
        return DesktopDetectionResponse(
            action="none",
            detected_mode=detected_mode,
            reason="mode_unchanged",
            productivity_active=True,
            active_instance_count=0,
            timer_updated=False
        )

    # Startup grace period: ignore transitions TO silence for N seconds after
    # server start. AHK restarts detect silence before catching real audio state.
    grace_secs = DESKTOP_STATE.get("startup_grace_secs", 0)
    if grace_secs > 0 and detected_mode == "silence" and current_mode != "silence":
        elapsed = time.time() - DESKTOP_STATE.get("startup_time", 0)
        if elapsed < grace_secs:
            remaining = round(grace_secs - elapsed, 1)
            print(f"    GRACE PERIOD: Ignoring silence detection ({remaining}s remaining, current={current_mode})")
            return DesktopDetectionResponse(
                action="none",
                detected_mode=detected_mode,
                reason=f"startup_grace_period ({remaining}s remaining)",
                productivity_active=True,
                active_instance_count=0,
                timer_updated=False
            )

    # Check productivity status
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0

    productivity_active = active_count > 0

    # Determine if mode change is allowed
    allowed = True
    reason = "allowed"

    # CLOCKED OUT: All modes allowed, no enforcement
    if work_mode == "clocked_out":
        allowed = True
        reason = "clocked_out"
        print(f"    Clocked out - all modes allowed")
    # GYM MODE: All modes allowed (gym has its own timer logic)
    elif work_mode == "gym":
        allowed = True
        reason = "gym_mode"
        print(f"    Gym mode - all modes allowed")
    # CLOCKED IN: Video/gaming mode requires either break time OR productivity
    elif detected_mode == "video" or detected_mode == "gaming":
        has_break_time = timer_engine.break_balance_ms > 0
        break_secs = round(timer_engine.break_balance_ms / 1000)

        if has_break_time:
            allowed = True
            reason = "break_time_available"
            print(f"    {detected_mode.title()} allowed: {break_secs}s break available")
        elif productivity_active:
            allowed = True
            reason = "productivity_active"
            print(f"    {detected_mode.title()} allowed: productivity active (penalty mode)")
        else:
            allowed = False
            reason = "no_productivity_no_break"
            print(f"    {detected_mode.title()} blocked: no break time, no productivity")

    if allowed:
        # Update desktop state
        old_mode = DESKTOP_STATE["current_mode"]
        DESKTOP_STATE["current_mode"] = detected_mode
        DESKTOP_STATE["last_detection"] = datetime.now().isoformat()

        # Track meeting state (suppresses TTS)
        was_meeting = DESKTOP_STATE["in_meeting"]
        DESKTOP_STATE["in_meeting"] = (detected_mode == "meeting")
        if DESKTOP_STATE["in_meeting"] and not was_meeting:
            print(f"    MEETING STARTED: TTS suppressed")
        elif was_meeting and not DESKTOP_STATE["in_meeting"]:
            print(f"    MEETING ENDED: TTS resumed")

        # Update timer activity layer
        now_ms = int(time.monotonic() * 1000)
        old_timer_mode = timer_engine.current_mode.value

        was_focused = timer_engine.focus_active
        if detected_mode in ("video", "scrolling", "gaming"):
            is_sg = detected_mode in ("scrolling", "gaming")
            result = timer_engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=is_sg, now_mono_ms=now_ms)
        else:
            result = timer_engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=now_ms)

        # Log focus auto-exit on distraction
        if was_focused and not timer_engine.focus_active:
            focus_min = round(timer_engine.total_focus_time_ms / 60000)
            await log_event("focus_toggle", details={
                "action": "off", "trigger": "distraction", "detected_mode": detected_mode,
                "total_focus_time_ms": timer_engine.total_focus_time_ms,
                "focus_cutoff_time": timer_engine.focus_cutoff_time,
            })
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, speak_tts, f"Focus broken by {detected_mode}. {focus_min} minutes earned.")

        timer_updated = TimerEvent.MODE_CHANGED in result.events
        if timer_updated:
            await timer_log_shift(old_timer_mode,
                                  timer_engine.current_mode.value, trigger="desktop_detection", source="ahk")

        await log_event(
            "desktop_mode_change",
            details={
                "old_mode": old_mode,
                "new_mode": detected_mode,
                "window_title": window_title,
                "source": source,
                "timer_updated": timer_updated,
                "productivity_active": productivity_active,
                "active_instances": active_count
            }
        )

        print(f"<<< Mode changed: {old_mode} -> {detected_mode} | timer={timer_updated}")

        return DesktopDetectionResponse(
            action="mode_changed",
            detected_mode=detected_mode,
            old_mode=old_mode,
            new_mode=detected_mode,
            reason="allowed",
            timer_updated=timer_updated,
            productivity_active=productivity_active,
            active_instance_count=active_count
        )
    else:
        # Mode change blocked - immediately enforce by closing distraction windows
        print(f"<<< Mode change BLOCKED: {detected_mode} | reason={reason}")

        enforce_result = close_distraction_windows()
        send_pavlok_stimulus(reason="desktop_distraction_blocked")

        await log_event(
            "desktop_mode_blocked",
            details={
                "detected_mode": detected_mode,
                "reason": reason,
                "window_title": window_title,
                "source": source,
                "productivity_active": productivity_active,
                "active_instances": active_count,
                "enforcement": enforce_result
            }
        )

        raise HTTPException(
            status_code=403,
            detail=DesktopDetectionResponse(
                action="blocked",
                detected_mode=detected_mode,
                reason=reason,
                timer_updated=False,
                productivity_active=productivity_active,
                active_instance_count=active_count
            ).model_dump()
        )


# ============ Desktop Satellite Endpoints ============


@app.post("/desktop/heartbeat")
async def handle_desktop_heartbeat(request: dict):
    """Receive heartbeat from AHK audio-monitor (~every 30s)."""
    mode = request.get("mode", "unknown")
    source = request.get("source", "unknown")

    DESKTOP_STATE["ahk_reachable"] = True
    DESKTOP_STATE["ahk_last_heartbeat"] = datetime.now().isoformat()

    logger.debug(f"AHK heartbeat: mode={mode} source={source}")
    return {
        "status": "ok",
        "ahk_mode": mode,
        "server_mode": DESKTOP_STATE.get("current_mode", "silence"),
    }


@app.post("/desktop/enforce")
async def manual_enforce_desktop(app: str = "brave", action: str = "close"):
    """Manually trigger desktop enforcement via token-satellite (for testing)."""
    result = enforce_desktop_app(app, action)

    await log_event(
        "desktop_manual_enforcement",
        details={"app": app, "action": action, "result": result}
    )

    return result


@app.get("/desktop/ping")
async def ping_desktop():
    """Check if Windows satellite server is reachable."""
    result = check_desktop_reachable()
    return result


@app.post("/satellite/restart")
async def restart_satellite_proxy():
    """Proxy restart to WSL satellite. Timeout expected (satellite exits)."""
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    try:
        requests.post(f"http://{host}:{port}/restart", timeout=3)
        return {"success": True}
    except requests.exceptions.Timeout:
        return {"success": True, "note": "timeout expected during restart"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============ Phone Activity Detection ============
# MacroDroid sends app open/close events from phone

@app.post("/phone", response_model=PhoneActivityResponse)
async def handle_phone_activity(request: PhoneActivityRequest):
    """
    Handle phone app activity from MacroDroid.

    Called when distraction apps (Twitter, YouTube, games) are opened/closed.
    Returns whether the app is allowed based on break time or productivity.

    Unlike desktop, we don't force-close apps - just return allowed/blocked
    for MacroDroid to handle (show notification, etc).
    """
    app_name = request.app.lower()
    action = request.action.lower()
    package = request.package
    display_name = get_phone_app_display_name(app_name, package)

    print(f">>> Phone activity: app={app_name} ({display_name}) action={action} package={package}")

    # Handle app close
    if action == "close":
        old_app = PHONE_STATE.get("current_app")
        PHONE_STATE["current_app"] = None
        PHONE_STATE["is_distracted"] = False
        PHONE_STATE["last_activity"] = datetime.now().isoformat()

        # Clear Twitter tracking on close
        if app_name in ("twitter", "x", "com.twitter.android"):
            PHONE_STATE["twitter_open_since"] = None
            PHONE_STATE["twitter_zapped"] = False  # reset zap latch on confirmed close
            # Clear manual mode so close event restores work mode
            timer_engine._clear_manual_mode()
            print(f"    Twitter closed, manual mode cleared")

        # Switch timer activity to working when distraction app closes
        timer_updated = False
        if old_app:
            DESKTOP_STATE["current_mode"] = "silence"
            DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
            now_ms = int(time.monotonic() * 1000)
            old_timer_mode = timer_engine.current_mode.value
            result = timer_engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=now_ms)
            timer_updated = TimerEvent.MODE_CHANGED in result.events
            if timer_updated:
                await timer_log_shift(old_timer_mode, timer_engine.current_mode.value, trigger="phone_app",
                                      source="macrodroid", phone_app=app_name)
            print(f"    Phone close -> working | timer={timer_updated}")

        await log_event(
            "phone_app_closed",
            details={
                "app": app_name,
                "display_name": display_name,
                "package": package,
                "timer_updated": timer_updated
            }
        )

        return PhoneActivityResponse(
            allowed=True,
            reason="closed",
            message="App closed"
        )

    # Determine distraction category
    distraction_mode = PHONE_DISTRACTION_APPS.get(app_name)
    if not distraction_mode and package:
        distraction_mode = PHONE_DISTRACTION_APPS.get(package)

    # If not a known distraction app, allow it
    if not distraction_mode:
        print(f"    Unknown app, allowing: {app_name}")
        return PhoneActivityResponse(
            allowed=True,
            reason="not_tracked",
            message="App not in distraction list"
        )

    is_twitter = app_name in ("twitter", "x", "com.twitter.android")

    # Phantom open guard: if Twitter was already zapped and we haven't received
    # a confirmed close event, ignore all subsequent "open" events entirely.
    # MacroDroid's app_launched trigger re-fires on notification banners, app
    # switcher, etc. — these phantom opens were resetting current_app and
    # restarting the 7-minute timer, causing repeat zaps.
    if is_twitter and PHONE_STATE.get("twitter_zapped"):
        print(f"    Phantom Twitter open ignored (already zapped, awaiting confirmed close)")
        return PhoneActivityResponse(
            allowed=False,
            reason="phantom_blocked",
            message="Twitter already enforced, waiting for confirmed close"
        )

    # Duplicate open debounce: if we're already tracking this app, don't
    # re-process. MacroDroid sends repeated app_launched events for the same
    # app (notification banners, app switcher swipe-throughs, etc.).
    current = (PHONE_STATE.get("current_app") or "").lower()
    if current == app_name or (is_twitter and current in ("twitter", "x", "com.twitter.android")):
        print(f"    Duplicate {app_name} open ignored (already current_app)")
        return PhoneActivityResponse(
            allowed=True,
            reason="already_tracked",
            message="Already tracking this app"
        )

    # Helper to sync timer activity layer for phone distraction
    def _sync_phone_timer():
        # If twitter was already zapped, don't let phantom opens change timer mode
        # (this prevents phantom opens from burning break time → break_exhausted zaps)
        if app_name in ("twitter", "x", "com.twitter.android") and PHONE_STATE.get("twitter_zapped"):
            print(f"    Skipping timer sync — twitter already zapped")
            return False, timer_engine.current_mode.value
        old_timer_mode = timer_engine.current_mode.value
        DESKTOP_STATE["current_mode"] = distraction_mode
        DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
        now_ms = int(time.monotonic() * 1000)
        # Phone distractions → set activity to DISTRACTION
        was_focused = timer_engine.focus_active
        is_sg = distraction_mode in ("scrolling", "gaming")
        result = timer_engine.set_activity(Activity.DISTRACTION, is_scrolling_gaming=is_sg, now_mono_ms=now_ms)
        updated = TimerEvent.MODE_CHANGED in result.events
        print(f"    Phone open -> {distraction_mode} (activity=DISTRACTION, sg={is_sg}) | timer={updated}")
        # Log focus auto-exit on phone distraction
        if was_focused and not timer_engine.focus_active:
            focus_min = round(timer_engine.total_focus_time_ms / 60000)
            asyncio.ensure_future(log_event("focus_toggle", details={
                "action": "off", "trigger": "phone_distraction", "app": app_name,
                "total_focus_time_ms": timer_engine.total_focus_time_ms,
                "focus_cutoff_time": timer_engine.focus_cutoff_time,
            }))
            loop = asyncio.get_event_loop()
            loop.run_in_executor(None, speak_tts, f"Focus broken by phone. {focus_min} minutes earned.")
        # Track Twitter open time for 7-minute enforcement
        if app_name in ("twitter", "x", "com.twitter.android"):
            if PHONE_STATE["twitter_open_since"] is None and not PHONE_STATE.get("twitter_zapped"):
                PHONE_STATE["twitter_open_since"] = time.monotonic()
                print(f"    Twitter timer started")
            elif PHONE_STATE.get("twitter_zapped"):
                print(f"    Twitter open (ignoring — already zapped, waiting for confirmed close)")
        else:
            # Different app opened — if twitter timer is running, close event was dropped
            if PHONE_STATE["twitter_open_since"] is not None or PHONE_STATE.get("twitter_zapped"):
                print(f"    Clearing stale Twitter timer (new app: {app_name})")
                PHONE_STATE["twitter_open_since"] = None
                PHONE_STATE["twitter_zapped"] = False
        return updated, old_timer_mode

    # Check work mode
    work_mode = DESKTOP_STATE.get("work_mode", "clocked_in")

    # Clocked out or gym mode = all allowed
    if work_mode in ("clocked_out", "gym"):
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        _updated, _old_mode = _sync_phone_timer()
        if _updated:
            await timer_log_shift(_old_mode, "work_" + distraction_mode, trigger="phone_app", source="macrodroid", phone_app=app_name)

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": work_mode,
            }
        )

        return PhoneActivityResponse(
            allowed=True,
            reason=work_mode,
            message=f"Allowed ({work_mode})"
        )

    # Clocked in - check break time and productivity
    break_secs = round(timer_engine.break_balance_ms / 1000)

    # Check productivity (active Claude instances)
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0

    productivity_active = active_count > 0

    # === TEST SHIM - bypasses break/productivity checks ===
    test_force_block = PHONE_CONFIG.get("test_force_block", False)
    if test_force_block:
        print(f"    TEST MODE: Forcing block (ignoring break={break_secs}s, productivity={productivity_active})")
        break_secs = 0
        productivity_active = False
    # ======================================================

    # Decision logic (same as desktop)
    if break_secs > 0:
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        _updated, _old_mode = _sync_phone_timer()
        if _updated:
            await timer_log_shift(_old_mode, "work_" + distraction_mode, trigger="phone_app", source="macrodroid", phone_app=app_name)

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": "break_time",
                "break_seconds": break_secs,
            }
        )

        print(f"    Allowed: {break_secs}s break available")
        return PhoneActivityResponse(
            allowed=True,
            reason="break_time_available",
            break_seconds=break_secs,
            message=f"Break time: {break_secs // 60}m {break_secs % 60}s"
        )

    elif productivity_active:
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        _updated, _old_mode = _sync_phone_timer()
        if _updated:
            await timer_log_shift(_old_mode, "work_" + distraction_mode, trigger="phone_app", source="macrodroid", phone_app=app_name)

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": "productivity_active",
                "active_instances": active_count,
            }
        )

        print(f"    Allowed: productivity active ({active_count} instances)")
        return PhoneActivityResponse(
            allowed=True,
            reason="productivity_active",
            break_seconds=0,
            message="Productivity active (penalty mode)"
        )

    else:
        print(f"    BLOCKED: no break time, no productivity")
        enforce_result = enforce_phone_app(app_name, action="disable")
        send_pavlok_stimulus(reason="phone_distraction_blocked")

        await log_event(
            "phone_distraction_blocked",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": "no_break_no_productivity",
                "enforcement": enforce_result
            }
        )

        return PhoneActivityResponse(
            allowed=False,
            reason="blocked",
            break_seconds=0,
            message="No break time or productivity"
        )


@app.get("/phone")
async def get_phone_state():
    """Get current phone activity state."""
    return {
        "current_app": PHONE_STATE.get("current_app"),
        "is_distracted": PHONE_STATE.get("is_distracted", False),
        "last_activity": PHONE_STATE.get("last_activity"),
        "break_seconds": round(timer_engine.break_balance_ms / 1000),
        "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "reachable": PHONE_STATE.get("reachable"),
        "last_reachable_check": PHONE_STATE.get("last_reachable_check"),
    }


@app.get("/phone/ping")
async def ping_phone():
    """Check if phone is reachable."""
    result = check_phone_reachable()
    return result


@app.post("/phone/enforce")
async def manual_enforce_phone(app: str, action: str = "disable"):
    """Manually trigger phone enforcement (for testing)."""
    result = enforce_phone_app(app, action)
    return result


@app.post("/phone/event")
async def handle_phone_system_event(request: PhoneSystemEventRequest):
    """
    Handle phone system events from MacroDroid.

    Events:
    - shizuku_died: Shizuku service stopped. Triggers auto-restart from Mac.
    - shizuku_restored: Shizuku came back. Resets restart state.
    - device_boot: Phone rebooted.
    - heartbeat: Periodic health check with server/shizuku status.
    """
    event = request.event
    now = datetime.now().isoformat()

    await log_event(f"phone_{event}", device_id="phone", details={
        "time": request.time,
        "server": request.server,
        "shizuku_dead": request.shizuku_dead,
    })

    if event == "shizuku_died":
        SHIZUKU_STATE["dead"] = True
        SHIZUKU_STATE["last_death"] = now
        logger.warning(f"Shizuku died at {request.time}")

        # Attempt auto-restart in background
        restart_result = await attempt_shizuku_restart()
        return {"received": True, "event": event, "restart_attempt": restart_result}

    elif event == "shizuku_restored":
        SHIZUKU_STATE["dead"] = False
        SHIZUKU_STATE["consecutive_failures"] = 0
        logger.info(f"Shizuku restored at {request.time}")
        return {"received": True, "event": event}

    elif event == "device_boot":
        SHIZUKU_STATE["dead"] = True  # Shizuku is dead after boot until manually started
        logger.info(f"Phone booted at {request.time}")
        return {"received": True, "event": event}

    elif event == "heartbeat":
        # Update reachability from heartbeat
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = now
        if request.shizuku_dead:
            SHIZUKU_STATE["dead"] = request.shizuku_dead.lower() == "true"
        return {"received": True, "event": event, "shizuku_state": SHIZUKU_STATE}

    return {"received": True, "event": event}


@app.get("/phone/shizuku")
async def get_shizuku_state():
    """Get current Shizuku state and restart history."""
    return {**SHIZUKU_STATE, "config": SHIZUKU_CONFIG}


@app.post("/phone/shizuku/restart")
async def manual_shizuku_restart():
    """Manually trigger Shizuku restart via shizuku-connect CLI."""
    result = await attempt_shizuku_restart()
    await log_event("shizuku_manual_restart", device_id="phone", details=result)
    return result


@app.post("/phone/shizuku/reset")
async def reset_shizuku_state():
    """Reset Shizuku failure counters."""
    SHIZUKU_STATE["consecutive_failures"] = 0
    SHIZUKU_STATE["dead"] = False
    return {"reset": True, "state": SHIZUKU_STATE}


@app.post("/api/timer/break-exhausted")
async def handle_break_exhausted():
    """
    Break exhaustion enforcement endpoint.
    Now also triggered internally by the timer worker when break time runs out.
    Kept for backward compat (Obsidian JS may still call it during transition).
    """
    result = await enforce_break_exhausted_impl()
    await log_event("break_exhausted_enforcement", details=result)

    if not result.get("enforced"):
        return {"enforced": False, "reason": "no_active_distractions"}

    return result


@app.post("/api/timer/set-break")
async def set_break_time(seconds: int):
    """Debug: directly set accumulated break time (in seconds). Negative values set backlog."""
    timer_engine._break_balance_ms = seconds * 1000
    await log_event("timer_debug_set_break", details={"seconds": seconds})
    await timer_save_to_db()
    return {
        "break_balance_ms": timer_engine._break_balance_ms,
        "break_balance_seconds": seconds,
        "accumulated_break_ms": max(0, timer_engine._break_balance_ms),
        "backlog_ms": abs(min(0, timer_engine._break_balance_ms)),
    }


@app.get("/api/widget/break")
async def get_widget_break():
    """Slim break-time endpoint for phone widget on-demand pull."""
    bal = timer_engine.break_balance_ms
    in_backlog = bal < 0
    # Show minutes, rounded to 1 decimal
    if in_backlog:
        minutes = round(abs(bal) / 60000, 1)
        label = f"-{minutes}m"
    else:
        minutes = round(bal / 60000, 1)
        label = f"{minutes}m"
    return {
        "break_minutes": minutes,
        "in_backlog": in_backlog,
        "label": label,
        "mode": timer_engine.current_mode.value,
    }


@app.get("/api/timer")
async def get_timer_state():
    """Get full timer state (for debugging, dashboards, Stream Deck)."""
    return {
        "current_mode": timer_engine.current_mode.value,
        "activity": timer_engine.activity.value,
        "productivity_active": timer_engine.productivity_active,
        "manual_mode": timer_engine.manual_mode.value if timer_engine.manual_mode else None,
        "total_work_time": format_timer_time(timer_engine.total_work_time_ms),
        "total_work_time_ms": timer_engine.total_work_time_ms,
        "total_break_time": format_timer_time(timer_engine.total_break_time_ms),
        "total_break_time_ms": timer_engine.total_break_time_ms,
        "break_balance_ms": timer_engine.break_balance_ms,
        "accumulated_break": format_timer_time(max(0, timer_engine.break_balance_ms)),
        "accumulated_break_ms": max(0, timer_engine.break_balance_ms),
        "accumulated_break_seconds": round(max(0, timer_engine.break_balance_ms) / 1000),
        "break_backlog": format_timer_time(abs(min(0, timer_engine.break_balance_ms))),
        "break_backlog_ms": abs(min(0, timer_engine.break_balance_ms)),
        "is_in_backlog": timer_engine.break_balance_ms < 0,
        "daily_start_date": timer_engine.daily_start_date,
        "manual_mode_lock": timer_engine.manual_mode_lock,
        "manual_trigger": timer_engine.manual_trigger,
        "focus_active": timer_engine.focus_active,
        "total_focus_time": format_timer_time(timer_engine.total_focus_time_ms),
        "total_focus_time_ms": timer_engine.total_focus_time_ms,
        "focus_cutoff_time": timer_engine.focus_cutoff_time,
        "focus_cutoff_hour": timer_engine.focus_cutoff_hour,
        "desktop_mode": DESKTOP_STATE.get("current_mode", "silence"),
        "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "location_zone": DESKTOP_STATE.get("location_zone"),
        "phone_app": PHONE_STATE.get("current_app"),
        "ahk_reachable": DESKTOP_STATE.get("ahk_reachable"),
    }


@app.get("/api/timer/shifts")
async def get_timer_shifts():
    """Get today's timer shift analytics for TUI visualization."""
    from collections import defaultdict

    # Only show shifts since 9 AM today (daily reset time)
    from datetime import time as _time
    now = datetime.now()
    reset_today = datetime.combine(now.date(), _time(9, 0))
    if now < reset_today:
        reset_today -= timedelta(days=1)
    cutoff = reset_today.isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM timer_shifts WHERE timestamp >= ? ORDER BY id",
            (cutoff,),
        )
        rows = await cursor.fetchall()

    if not rows:
        return {"total_shifts": 0, "balance_series": [], "mode_distribution": {},
                "shifts_by_trigger": {}, "enforcement_count": 0, "twitter_time_mins": 0}

    balance_series = []
    balance_timeline = []
    mode_time = defaultdict(int)
    shifts_by_trigger = defaultdict(int)
    enforcement_count = 0
    twitter_shifts = 0
    prev_time = None

    for r in rows:
        bal_ms = r["break_balance_ms"] or 0
        backlog_ms = r["break_backlog_ms"] or 0
        # After refactor, break_balance_ms is signed; for old data, compute:
        effective = bal_ms - backlog_ms
        bal_min = round(effective / 60000, 1)
        balance_series.append(bal_min)
        balance_timeline.append({
            "t": r["timestamp"],
            "bal": bal_min,
            "mode": r["new_mode"],
        })
        shifts_by_trigger[r["trigger"] or "unknown"] += 1
        if r["trigger"] == "enforcement":
            enforcement_count += 1
        if r["phone_app"] and "twitter" in (r["phone_app"] or "").lower():
            twitter_shifts += 1
        # Rough mode time (time between shifts spent in old_mode)
        if prev_time and r["old_mode"]:
            try:
                from datetime import datetime as _dt
                t1 = _dt.fromisoformat(prev_time)
                t2 = _dt.fromisoformat(r["timestamp"])
                delta_s = (t2 - t1).total_seconds()
                if 0 < delta_s < 7200:  # cap at 2h to avoid stale gaps
                    mode_time[r["old_mode"]] += int(delta_s)
            except Exception:
                pass
        prev_time = r["timestamp"]

    return {
        "total_shifts": len(rows),
        "balance_series": balance_series,
        "balance_timeline": balance_timeline,
        "mode_distribution": dict(mode_time),
        "shifts_by_trigger": dict(shifts_by_trigger),
        "enforcement_count": enforcement_count,
        "twitter_shifts": twitter_shifts,
    }


@app.post("/api/timer/break")
async def enter_break_mode():
    """Enter break mode (for Stream Deck / TUI / manual control)."""
    global _current_session_id, _session_start_ms
    if timer_engine.break_balance_ms <= 0:
        raise HTTPException(status_code=400, detail="No break time available")

    now_ms = int(time.monotonic() * 1000)
    old_mode = timer_engine.current_mode.value
    changed, tick_result = timer_engine.enter_break(now_ms)
    if changed:
        today = datetime.now().strftime("%Y-%m-%d")
        await timer_log_mode_change(old_mode, "break", is_automatic=False)
        await timer_log_shift(old_mode, "break", trigger="manual", source="api")
        await timer_end_session(_current_session_id, now_ms - _session_start_ms, break_used_ms=timer_engine.total_break_time_ms)
        _current_session_id = await timer_start_session("break", today)
        _session_start_ms = now_ms
        await log_event("timer_mode_change", details={"new_mode": "break", "source": "api"})
    return {"status": "break", "changed": changed, "break_available_seconds": round(max(0, timer_engine.break_balance_ms) / 1000)}


@app.post("/api/timer/pause")
async def enter_pause_mode():
    """Enter pause mode (sets productivity inactive → IDLE)."""
    global _current_session_id, _session_start_ms
    now_ms = int(time.monotonic() * 1000)
    old_mode = timer_engine.current_mode.value
    result = timer_engine.set_productivity(False, now_ms)
    changed = TimerEvent.MODE_CHANGED in result.events
    if changed:
        today = datetime.now().strftime("%Y-%m-%d")
        new_mode = timer_engine.current_mode.value
        await timer_log_mode_change(old_mode, new_mode, is_automatic=False)
        await timer_log_shift(old_mode, new_mode, trigger="manual", source="api")
        await timer_end_session(_current_session_id, now_ms - _session_start_ms)
        _current_session_id = await timer_start_session(new_mode, today)
        _session_start_ms = now_ms
        await log_event("timer_mode_change", details={"new_mode": new_mode, "source": "api"})
    return {"status": timer_engine.current_mode.value, "changed": changed}


@app.post("/api/timer/sleep")
async def enter_sleep_mode():
    """Enter sleeping mode - neutral, doesn't count as work or break."""
    global _current_session_id, _session_start_ms
    now_ms = int(time.monotonic() * 1000)
    old_mode = timer_engine.current_mode.value
    changed, tick_result = timer_engine.enter_sleeping(now_ms)
    if changed:
        today = datetime.now().strftime("%Y-%m-%d")
        await timer_log_mode_change(old_mode, "sleeping", is_automatic=False)
        await timer_log_shift(old_mode, "sleeping", trigger="manual", source="api")
        await timer_end_session(_current_session_id, now_ms - _session_start_ms)
        _current_session_id = await timer_start_session("sleeping", today)
        _session_start_ms = now_ms
        await log_event("timer_mode_change", details={"new_mode": "sleeping", "source": "api"})
    return {"status": "sleeping", "changed": changed}


@app.post("/api/timer/resume")
async def resume_work_mode():
    """Exit break/sleeping and resume. Also sets productivity active."""
    global _current_session_id, _session_start_ms
    now_ms = int(time.monotonic() * 1000)
    old_mode = timer_engine.current_mode.value
    changed, tick_result = timer_engine.resume(now_ms)
    # Also ensure productivity is active
    timer_engine.set_productivity(True, now_ms)
    if changed:
        DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
        today = datetime.now().strftime("%Y-%m-%d")
        new_mode = timer_engine.current_mode.value
        await timer_log_mode_change(old_mode, new_mode, is_automatic=False)
        await timer_log_shift(old_mode, new_mode, trigger="manual", source="api")
        await timer_end_session(_current_session_id, now_ms - _session_start_ms)
        _current_session_id = await timer_start_session(new_mode, today)
        _session_start_ms = now_ms
        await log_event("timer_mode_change", details={"new_mode": new_mode, "source": "api"})
    return {"status": timer_engine.current_mode.value, "changed": changed}


@app.post("/api/timer/focus")
async def toggle_focus_mode():
    """Toggle focus layer on/off. Auto-exits on distraction detection."""
    now_ms = int(time.monotonic() * 1000)
    if timer_engine.focus_active:
        changed, tick_result = timer_engine.exit_focus(now_ms)
        focus_on = False
    else:
        changed, tick_result = timer_engine.enter_focus(now_ms)
        focus_on = True

    if changed:
        action = "on" if focus_on else "off"
        await log_event("focus_toggle", details={
            "action": action,
            "total_focus_time_ms": timer_engine.total_focus_time_ms,
            "focus_cutoff_time": timer_engine.focus_cutoff_time,
        })
        # TTS announcement
        focus_min = round(timer_engine.total_focus_time_ms / 60000)
        if focus_on:
            msg = "Focus mode on."
        else:
            msg = f"Focus mode off. {focus_min} minutes today. Cutoff at {timer_engine.focus_cutoff_time}."
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, speak_tts, msg)

    return {
        "focus_active": timer_engine.focus_active,
        "changed": changed,
        "total_focus_time": format_timer_time(timer_engine.total_focus_time_ms),
        "total_focus_time_ms": timer_engine.total_focus_time_ms,
        "focus_cutoff_time": timer_engine.focus_cutoff_time,
        "focus_cutoff_hour": timer_engine.focus_cutoff_hour,
    }


@app.post("/api/timer/daily-reset")
async def manual_daily_reset():
    """Manually trigger the 9 AM daily reset (clears accumulated break + wipes prior-day timer events)."""
    await timer_9am_reset()
    return {
        "status": "reset",
        "break_balance_ms": timer_engine.break_balance_ms,
        "daily_start_date": timer_engine.daily_start_date,
    }


@app.post("/api/timer/reset")
async def reset_timer():
    """Reset timer to fresh state - zero work time, default break buffer."""
    global _current_session_id, _session_start_ms
    now_ms = int(time.monotonic() * 1000)
    today = datetime.now().strftime("%Y-%m-%d")

    # End current session if running
    if _current_session_id > 0:
        await timer_end_session(_current_session_id, now_ms - _session_start_ms)

    # Reset timer state using force_daily_reset
    timer_engine.force_daily_reset(now_ms, today)
    timer_engine._break_balance_ms = DEFAULT_BREAK_BUFFER_MS

    # Start fresh session
    _current_session_id = await timer_start_session(timer_engine.current_mode.value, today)
    _session_start_ms = now_ms

    await log_event("timer_reset", details={"date": today})
    return {"status": "reset", "total_work_time": "0h 0m", "accumulated_break": "5m", "current_mode": timer_engine.current_mode.value}


@app.post("/api/work-action")
async def work_action():
    """Manual work action signal — sets productivity active."""
    global _current_session_id, _session_start_ms
    now_ms = int(time.monotonic() * 1000)
    old_mode = timer_engine.current_mode.value
    result = timer_engine.set_productivity(True, now_ms)
    exited_idle = TimerEvent.MODE_CHANGED in result.events

    if exited_idle:
        new_mode = timer_engine.current_mode.value
        today = datetime.now().strftime("%Y-%m-%d")
        await timer_log_shift(old_mode, new_mode, trigger="work_action", source="api")
        if _current_session_id > 0:
            duration_ms = now_ms - _session_start_ms
            await timer_end_session(_current_session_id, duration_ms)
        _current_session_id = await timer_start_session(new_mode, today)
        _session_start_ms = now_ms
        print(f"TIMER: Work-action exited {old_mode} → {new_mode}")

    return {"idle_timer_reset": True, "exited_idle": exited_idle, "current_mode": timer_engine.current_mode.value}


# ============ Pavlok Endpoints ============

@app.post("/api/pavlok/zap")
async def pavlok_zap(
    type: str = "zap",
    value: int | None = None,
    reason: str = "manual",
):
    """Send a stimulus to the Pavlok watch. Bypasses cooldown for manual triggers."""
    result = send_pavlok_stimulus(
        stimulus_type=type,
        value=value,
        reason=reason,
        respect_cooldown=False,
    )
    await log_event("pavlok_stimulus", details=result)
    return result


@app.post("/api/pavlok/toggle")
async def pavlok_toggle(enabled: bool | None = None):
    """Toggle or set Pavlok enforcement. No body = toggle current state."""
    if enabled is None:
        PAVLOK_CONFIG["enabled"] = not PAVLOK_CONFIG["enabled"]
    else:
        PAVLOK_CONFIG["enabled"] = enabled
    await log_event("pavlok_toggled", details={"enabled": PAVLOK_CONFIG["enabled"]})
    return {"enabled": PAVLOK_CONFIG["enabled"]}


@app.get("/api/pavlok/status")
async def pavlok_status():
    """Get current Pavlok state."""
    cooldown_remaining = 0.0
    if PAVLOK_STATE["last_stimulus_at"]:
        elapsed = (datetime.now() - datetime.fromisoformat(PAVLOK_STATE["last_stimulus_at"])).total_seconds()
        cooldown_remaining = max(0.0, PAVLOK_CONFIG["cooldown_seconds"] - elapsed)
    return {
        "enabled": PAVLOK_CONFIG["enabled"],
        "token_set": bool(PAVLOK_CONFIG["token"]),
        "last_stimulus_at": PAVLOK_STATE["last_stimulus_at"],
        "cooldown_remaining_seconds": round(cooldown_remaining),
        "default_zap_value": PAVLOK_CONFIG["default_zap_value"],
        "cooldown_seconds": PAVLOK_CONFIG["cooldown_seconds"],
    }


# ============ Work Mode / Geofence Endpoints ============
# MacroDroid uses geofence to send work mode changes

class WorkModeRequest(BaseModel):
    mode: str = Field(..., description="Work mode: clocked_in, clocked_out, gym")
    source: str = Field(default="api", description="Source of the request (macrodroid, manual, etc)")
    token: Optional[str] = Field(default=None, description="Optional auth token for MacroDroid")


@app.get("/api/work-mode")
async def get_work_mode():
    """Get current work mode status."""
    return {
        "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "work_mode_changed_at": DESKTOP_STATE.get("work_mode_changed_at"),
        "current_timer_mode": DESKTOP_STATE.get("current_mode", "silence"),
    }


@app.post("/api/work-mode")
async def set_work_mode(request: WorkModeRequest):
    """
    Set work mode. Called by MacroDroid geofence or manual toggle.

    Modes:
    - clocked_in: Normal enforcement (video requires productivity)
    - clocked_out: No enforcement, all modes allowed
    - gym: Gym timer mode, triggers gym timer in Obsidian

    MacroDroid can send:
    - POST /api/work-mode {"mode": "clocked_in", "source": "macrodroid"}
    - POST /api/work-mode {"mode": "gym", "source": "macrodroid"}
    """
    valid_modes = ["clocked_in", "clocked_out", "gym"]
    if request.mode not in valid_modes:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid work mode '{request.mode}'. Valid: {valid_modes}"
        )

    old_mode = DESKTOP_STATE.get("work_mode", "clocked_in")
    DESKTOP_STATE["work_mode"] = request.mode
    DESKTOP_STATE["work_mode_changed_at"] = datetime.now().isoformat()

    print(f">>> Work mode changed: {old_mode} -> {request.mode} (source: {request.source})")

    # If switching to gym mode, set idle timeout exempt
    timer_updated = False
    if request.mode == "gym":
        timer_engine.idle_timeout_exempt = True
        timer_updated = True

    await log_event(
        "work_mode_change",
        details={
            "old_mode": old_mode,
            "new_mode": request.mode,
            "source": request.source,
            "timer_updated": timer_updated,
        }
    )

    return {
        "status": "success",
        "old_mode": old_mode,
        "new_mode": request.mode,
        "timer_updated": timer_updated,
    }


# Geofence mapping: location + action → work_mode
# NOTE: As of 2026-02-26, work_mode is MANUAL only (user explicitly clocks in/out).
# This map is kept for reference but NO LONGER AUTO-APPLIED.
# The timer layer (current_mode) and location_zone track your state independently.
# Gym bounty (+30min break) is still applied on gym exit.
LOCATION_MODE_MAP = {
    # ("home", "exit"):    "clocked_out",   # DEPRECATED: was auto-setting
    # ("home", "enter"):   "clocked_in",    # DEPRECATED: was auto-setting
    # ("gym", "enter"):    "gym",           # DEPRECATED: was auto-setting
    # ("gym", "exit"):     "clocked_out",   # DEPRECATED: was auto-setting
    # ("campus", "enter"): "clocked_out",   # DEPRECATED: was auto-setting
    # ("campus", "exit"):  None,             # DEPRECATED: was tracking only
}


class LocationEventRequest(BaseModel):
    location: str = Field(..., description="Location name: home, gym, work")
    action: str = Field(..., description="enter or exit")
    source: str = Field(default="macrodroid", description="Source of the event")


@app.post("/api/location")
async def handle_location_event(request: LocationEventRequest):
    """
    Handle geofence location events from MacroDroid.
    Maps location+action pairs to work modes with zone state tracking.

    State machine rules:
    - enter while in same zone → duplicate, ignored
    - enter while in different zone → log implied exit for old zone, then process enter
    - exit while not in that zone → log as stale but still process (recover from missed events)
    - exit: sets zone to None; enter: sets zone to new location
    """
    location = request.location.lower()
    action = request.action.lower()
    key = (location, action)
    current_zone = DESKTOP_STATE.get("location_zone")
    notes = []

    print(f">>> Location event: {location}:{action} current_zone={current_zone}")

    # --- State machine validation ---
    if action == "enter":
        if current_zone == location:
            await log_event("location_event", details={
                "location": location, "action": action,
                "status": "duplicate", "current_zone": current_zone,
                "source": request.source,
            })
            return {"status": "duplicate", "reason": f"Already in {location}", "zone": current_zone}

        if current_zone is not None and current_zone != location:
            # Geofence exit didn't fire — log the implied exit
            notes.append(f"implied_exit:{current_zone}")
            print(f">>> Implied exit from {current_zone} (no exit event received)")
            await log_event("location_event", details={
                "location": current_zone, "action": "exit",
                "implied": True, "reason": f"entered {location} without exiting {current_zone}",
                "source": "state_machine",
            })

        DESKTOP_STATE["location_zone"] = location

    elif action == "exit":
        if current_zone != location:
            notes.append(f"stale_exit:was_{current_zone}")
            print(f">>> Stale exit for {location} (tracked zone={current_zone}), processing anyway")
        DESKTOP_STATE["location_zone"] = None

    # --- Mode change ---
    # DEPRECATED: work_mode is now MANUAL only (user explicitly clocks in/out via /api/clock-in /api/clock-out)
    # Location events only track location_zone, they no longer auto-change work_mode.
    # This decoupling ensures location ≠ work status (you can be home but not working).
    new_mode = LOCATION_MODE_MAP.get(key)  # Always None now, kept for reference
    result = {}

    if new_mode is not None:
        # This branch is now dead code (new_mode is always None) - kept for future flexibility
        work_mode_req = WorkModeRequest(
            mode=new_mode,
            source=f"macrodroid:{location}:{action}"
        )
        result = await set_work_mode(work_mode_req)
    else:
        print(f">>> Location tracked: {location}:{action} (work_mode unchanged - manual control only)")

    # Gym bounty: +30 min break on gym exit
    if location == "gym" and action == "exit":
        now_ms = int(time.monotonic() * 1000)
        timer_engine.apply_gym_bounty(now_ms)
        bounty_min = round(timer_engine.break_balance_ms / 60000, 1)
        print(f">>> Gym bounty applied: +30min break (total: {bounty_min}min)")
        await log_event("gym_bounty", details={"break_minutes": bounty_min})

    await log_event("location_event", details={
        "location": location,
        "action": action,
        "mapped_mode": new_mode,
        "prev_zone": current_zone,
        "notes": notes or None,
        "source": request.source,
    })

    return {
        "status": "ok",
        "location": location,
        "action": action,
        "mode": new_mode,
        "prev_zone": current_zone,
        "notes": notes or None,
        **result,
    }


@app.post("/api/clock-out")
async def clock_out():
    """Quick endpoint to clock out (disable enforcement)."""
    DESKTOP_STATE["work_mode"] = "clocked_out"
    DESKTOP_STATE["work_mode_changed_at"] = datetime.now().isoformat()
    await log_event("work_mode_change", details={"new_mode": "clocked_out", "source": "quick_api"})
    return {"status": "clocked_out", "message": "Enforcement disabled"}


@app.post("/api/clock-in")
async def clock_in():
    """Quick endpoint to clock in (enable enforcement)."""
    DESKTOP_STATE["work_mode"] = "clocked_in"
    DESKTOP_STATE["work_mode_changed_at"] = datetime.now().isoformat()
    await log_event("work_mode_change", details={"new_mode": "clocked_in", "source": "quick_api"})
    return {"status": "clocked_in", "message": "Enforcement enabled"}


# ============ Check-In Endpoints ============

@app.post("/api/checkin/submit")
async def submit_checkin(request: CheckinSubmit):
    """Submit a productivity check-in response. Stores in DB and writes to daily note."""
    config = CHECKIN_SCHEDULE.get(request.type)
    if not config:
        raise HTTPException(status_code=400, detail=f"Unknown checkin type: {request.type}")

    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now().isoformat()

    # Upsert the check-in response
    async with aiosqlite.connect(DB_PATH) as db:
        # Check if a prompt row exists (created by trigger_checkin)
        cursor = await db.execute(
            "SELECT id, prompted_at FROM checkins WHERE checkin_type = ? AND date = ?",
            (request.type, today)
        )
        existing = await cursor.fetchone()

        if existing:
            await db.execute("""
                UPDATE checkins SET
                    energy = ?, focus = ?, mood = ?, plan = ?, notes = ?,
                    on_track = ?, responded_at = ?
                WHERE checkin_type = ? AND date = ?
            """, (
                request.energy, request.focus, request.mood, request.plan, request.notes,
                1 if request.on_track else (0 if request.on_track is not None else None),
                now, request.type, today,
            ))
        else:
            # Submit without a prior prompt (manual submission)
            await db.execute("""
                INSERT INTO checkins (checkin_type, date, energy, focus, mood, plan, notes, on_track, prompted_at, responded_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'api')
            """, (
                request.type, today, request.energy, request.focus, request.mood,
                request.plan, request.notes,
                1 if request.on_track else (0 if request.on_track is not None else None),
                now, now,
            ))

        await db.commit()

    # Write to daily note frontmatter
    data = {k: v for k, v in request.model_dump().items() if k != "type" and v is not None}
    obsidian_updated = update_daily_note_frontmatter(request.type, data)

    await log_event("checkin_submitted", details={
        "checkin_type": request.type,
        "energy": request.energy,
        "focus": request.focus,
        "obsidian_updated": obsidian_updated,
    })

    return {"status": "ok", "checkin_type": request.type, "obsidian_updated": obsidian_updated}


@app.get("/api/checkin/today")
async def get_today_checkins():
    """Return all check-ins for today with completion status."""
    today = datetime.now().strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM checkins WHERE date = ? ORDER BY prompted_at",
            (today,)
        )
        rows = await cursor.fetchall()

    checkins = []
    completed_types = set()
    for row in rows:
        entry = dict(row)
        entry["completed"] = entry["responded_at"] is not None
        if entry["completed"]:
            completed_types.add(entry["checkin_type"])
        checkins.append(entry)

    # Show which check-ins are pending (not yet prompted or responded)
    pending = [k for k in CHECKIN_SCHEDULE if k not in completed_types]

    return {
        "date": today,
        "checkins": checkins,
        "completed": list(completed_types),
        "pending": pending,
    }


@app.get("/api/checkin/status")
async def get_checkin_status():
    """Return next upcoming check-in and overall status."""
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT checkin_type, responded_at FROM checkins WHERE date = ?",
            (today,)
        )
        rows = await cursor.fetchall()

    completed = [r["checkin_type"] for r in rows if r["responded_at"]]
    prompted = [r["checkin_type"] for r in rows]

    # Determine next check-in based on current time
    schedule_order = ["morning_start", "mid_morning", "decision_point", "afternoon", "afternoon_check"]
    time_map = {"morning_start": "09:00", "mid_morning": "10:30", "decision_point": "11:00",
                "afternoon": "13:00", "afternoon_check": "14:30"}

    next_checkin = None
    next_at = None
    pending = []
    for ctype in schedule_order:
        scheduled_time = datetime.strptime(f"{today} {time_map[ctype]}", "%Y-%m-%d %H:%M")
        if ctype not in completed:
            pending.append(ctype)
            if scheduled_time > now and next_checkin is None:
                next_checkin = ctype
                next_at = time_map[ctype]

    return {
        "next": next_checkin,
        "next_at": next_at,
        "completed": completed,
        "pending": pending,
        "prompted": prompted,
    }


@app.post("/api/checkin/trigger/{checkin_type}")
async def manual_trigger_checkin(checkin_type: str):
    """Manually trigger a check-in (for testing)."""
    result = await trigger_checkin(checkin_type)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/events/log")
async def log_debug_event(request: LogEventRequest):
    """Log a custom event (for TUI debugging, etc.)."""
    await log_event(
        request.event_type,
        instance_id=request.instance_id,
        details=request.details
    )
    return {"status": "logged", "event_type": request.event_type}


@app.get("/api/events/recent")
async def get_recent_events(limit: int = 10):
    """Get recent events with instance name data (LEFT JOIN)."""
    limit = min(limit, 100)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT e.*, ci.tab_name as instance_tab_name, ci.working_dir as instance_working_dir
            FROM events e
            LEFT JOIN claude_instances ci ON e.instance_id = ci.id
            ORDER BY e.created_at DESC
            LIMIT ?
        """, (limit,))
        rows = await cursor.fetchall()

        events = []
        for row in rows:
            event = dict(row)
            if event.get("details"):
                try:
                    event["details"] = json.loads(event["details"])
                except Exception:
                    pass
            events.append(event)
        return events


# Device Endpoints
@app.get("/api/devices")
async def list_devices():
    """List all known devices."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM devices")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# ============ Audio Proxy Endpoints ============
# Handles phone audio routing through PC to headphones

@app.post("/api/audio-proxy/connect", response_model=AudioProxyConnectResponse)
async def audio_proxy_connect(request: AudioProxyConnectRequest):
    """
    Called by MacroDroid when phone connects to PC via Bluetooth.
    Starts the audio receiver on Windows to prepare for incoming audio stream.
    """
    global AUDIO_PROXY_STATE

    # Check if already connected
    if AUDIO_PROXY_STATE["phone_connected"]:
        # Verify receiver is actually running
        check = check_audio_receiver_running()
        return AudioProxyConnectResponse(
            success=True,
            action="already_connected",
            receiver_started=check.get("running", False),
            receiver_pid=check.get("pid"),
            message="Phone audio proxy already active"
        )

    # Start the audio receiver
    result = start_audio_receiver()

    if result.get("success"):
        # Update state
        AUDIO_PROXY_STATE["phone_connected"] = True
        AUDIO_PROXY_STATE["receiver_running"] = True
        AUDIO_PROXY_STATE["receiver_pid"] = result.get("pid")
        AUDIO_PROXY_STATE["last_connect_time"] = datetime.now().isoformat()

        # Log event
        await log_event(
            "audio_proxy_connected",
            device_id=request.phone_device_id,
            details={
                "bluetooth_device": request.bluetooth_device_name,
                "receiver_pid": result.get("pid"),
                "receiver_status": result.get("status"),
                "source": request.source,
                "port": AUDIO_RECEIVER_PORT
            }
        )

        action = "connected" if result.get("status") == "started" else "reconnected"
        return AudioProxyConnectResponse(
            success=True,
            action=action,
            receiver_started=True,
            receiver_pid=result.get("pid"),
            message=f"Audio proxy activated. Receiver listening on port {AUDIO_RECEIVER_PORT}."
        )
    else:
        # Failed to start receiver
        await log_event(
            "audio_proxy_connect_failed",
            device_id=request.phone_device_id,
            details={
                "error": result.get("error"),
                "source": request.source
            }
        )

        return AudioProxyConnectResponse(
            success=False,
            action="error",
            receiver_started=False,
            message=f"Failed to start audio receiver: {result.get('error')}"
        )


@app.post("/api/audio-proxy/disconnect")
async def audio_proxy_disconnect(request: AudioProxyDisconnectRequest):
    """
    Called by MacroDroid when phone disconnects from PC Bluetooth.
    Stops the audio receiver and cleans up.
    """
    global AUDIO_PROXY_STATE

    # Stop the audio receiver
    result = stop_audio_receiver()

    # Update state
    AUDIO_PROXY_STATE["phone_connected"] = False
    AUDIO_PROXY_STATE["receiver_running"] = False
    AUDIO_PROXY_STATE["receiver_pid"] = None
    AUDIO_PROXY_STATE["last_disconnect_time"] = datetime.now().isoformat()

    # Log event
    await log_event(
        "audio_proxy_disconnected",
        device_id=request.phone_device_id,
        details={
            "stopped_count": result.get("stopped_count", 0),
            "source": request.source
        }
    )

    return {
        "success": True,
        "action": "disconnected",
        "stopped_count": result.get("stopped_count", 0),
        "message": "Audio proxy deactivated. Phone can reconnect to headphones."
    }


@app.get("/api/audio-proxy/status", response_model=AudioProxyStatusResponse)
async def audio_proxy_status():
    """
    Get current audio proxy status.
    Verifies actual receiver state against stored state.
    """
    # Check actual receiver status
    check = check_audio_receiver_running()

    # Reconcile state if needed
    actual_running = check.get("running", False)
    actual_pid = check.get("pid")

    if actual_running != AUDIO_PROXY_STATE["receiver_running"]:
        AUDIO_PROXY_STATE["receiver_running"] = actual_running
        AUDIO_PROXY_STATE["receiver_pid"] = actual_pid

    return AudioProxyStatusResponse(
        phone_connected=AUDIO_PROXY_STATE["phone_connected"],
        receiver_running=actual_running,
        receiver_pid=actual_pid,
        last_connect_time=AUDIO_PROXY_STATE["last_connect_time"],
        last_disconnect_time=AUDIO_PROXY_STATE["last_disconnect_time"]
    )


# ============ Headless Mode Endpoints (disabled on macOS) ============

@app.get("/api/headless", response_model=HeadlessStatusResponse)
async def headless_status():
    """Headless mode is not applicable on macOS."""
    return HeadlessStatusResponse(**get_headless_state())


@app.post("/api/headless", response_model=HeadlessControlResponse)
async def headless_control(request: HeadlessControlRequest):
    """Headless mode is not applicable on macOS."""
    state = get_headless_state()
    return HeadlessControlResponse(
        success=False,
        action=request.action,
        before=HeadlessStatusResponse(**state),
        after=HeadlessStatusResponse(**state),
        message="Headless mode not available on macOS"
    )


# ============ System Control Endpoints ============
# Remote shutdown/restart

@app.post("/api/system/shutdown", response_model=ShutdownResponse)
async def system_shutdown(request: ShutdownRequest):
    """
    Shutdown or restart the Mac Mini.

    Actions:
    - shutdown: Power off the system
    - restart: Restart the system
    """
    action = request.action.lower()

    if action not in ("shutdown", "restart"):
        raise HTTPException(status_code=400, detail="Invalid action. Use 'shutdown' or 'restart'")

    if action == "restart":
        cmd = ["sudo", "shutdown", "-r"]
    else:
        cmd = ["sudo", "shutdown", "-h"]

    # macOS shutdown: +N means N minutes from now
    delay_minutes = max(1, request.delay_seconds // 60) if request.delay_seconds > 0 else 0
    cmd.append(f"+{delay_minutes}" if delay_minutes > 0 else "now")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)

        if result.returncode == 0:
            logger.info(f"SYSTEM: Initiated {action} with delay={delay_minutes}min")
            return ShutdownResponse(
                success=True,
                action=action,
                delay_seconds=request.delay_seconds,
                message=f"System {action} initiated" + (f" in {delay_minutes} minutes" if delay_minutes > 0 else "")
            )
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            logger.error(f"SYSTEM: Failed to {action}: {error_msg}")
            return ShutdownResponse(
                success=False, action=action, delay_seconds=request.delay_seconds,
                message=f"Failed: {error_msg}"
            )
    except Exception as e:
        logger.error(f"SYSTEM: Error during {action}: {e}")
        return ShutdownResponse(
            success=False, action=action, delay_seconds=request.delay_seconds, message=str(e)
        )


@app.post("/api/system/shutdown/cancel")
async def cancel_shutdown():
    """Cancel a pending shutdown/restart."""
    try:
        result = subprocess.run(
            ["sudo", "killall", "shutdown"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            logger.info("SYSTEM: Cancelled pending shutdown")
            return {"success": True, "message": "Shutdown cancelled"}
        else:
            return {"success": False, "message": f"No pending shutdown or cancel failed: {result.stderr.strip()}"}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ============ KVM (Deskflow) Endpoints ============

@app.post("/api/kvm/start")
async def kvm_start():
    """Start Deskflow client (software KVM) on this Mac."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "Deskflow.app"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            return {"success": True, "message": "Deskflow already running", "already_running": True}

        subprocess.Popen(["open", "/Applications/Deskflow.app"])
        logger.info("KVM: Started Deskflow client")
        return {"success": True, "message": "Deskflow started", "already_running": False}
    except Exception as e:
        logger.error(f"KVM: Failed to start Deskflow: {e}")
        return {"success": False, "message": str(e)}


@app.post("/api/kvm/stop")
async def kvm_stop():
    """Stop Deskflow client on this Mac."""
    try:
        result = subprocess.run(
            ["pkill", "-f", "Deskflow"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            logger.info("KVM: Stopped Deskflow")
            return {"success": True, "message": "Deskflow stopped"}
        else:
            return {"success": True, "message": "Deskflow was not running"}
    except Exception as e:
        logger.error(f"KVM: Failed to stop Deskflow: {e}")
        return {"success": False, "message": str(e)}


@app.get("/api/kvm/status")
async def kvm_status():
    """Check if Deskflow is running on this Mac."""
    result = subprocess.run(
        ["pgrep", "-f", "Deskflow.app"],
        capture_output=True, text=True
    )
    running = result.returncode == 0
    return {"running": running, "pids": result.stdout.strip().split("\n") if running else []}


# ============ Task Endpoints ============

@app.get("/api/tasks", response_model=List[TaskResponse])
async def list_tasks():
    """List all scheduled tasks with their status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM scheduled_tasks ORDER BY id")
        tasks = await cursor.fetchall()

        result = []
        for task in tasks:
            task_dict = dict(task)
            task_id = task_dict["id"]

            # Get last execution
            cursor = await db.execute(
                """SELECT * FROM task_executions
                   WHERE task_id = ?
                   ORDER BY started_at DESC LIMIT 1""",
                (task_id,)
            )
            last_exec = await cursor.fetchone()

            last_run = None
            if last_exec:
                last_exec_dict = dict(last_exec)
                last_run = {
                    "status": last_exec_dict["status"],
                    "started_at": last_exec_dict["started_at"],
                    "duration_ms": last_exec_dict["duration_ms"]
                }

            # Get next run time from scheduler
            next_run = None
            job = scheduler.get_job(task_id)
            if job and job.next_run_time:
                next_run = job.next_run_time.isoformat()

            result.append(TaskResponse(
                id=task_dict["id"],
                name=task_dict["name"],
                description=task_dict["description"],
                task_type=task_dict["task_type"],
                schedule=task_dict["schedule"],
                enabled=bool(task_dict["enabled"]),
                max_retries=task_dict["max_retries"],
                last_run=last_run,
                next_run=next_run
            ))

        return result


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get details of a specific task."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?",
            (task_id,)
        )
        task = await cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        task_dict = dict(task)

        # Get last execution
        cursor = await db.execute(
            """SELECT * FROM task_executions
               WHERE task_id = ?
               ORDER BY started_at DESC LIMIT 1""",
            (task_id,)
        )
        last_exec = await cursor.fetchone()

        last_run = None
        if last_exec:
            last_exec_dict = dict(last_exec)
            last_run = {
                "status": last_exec_dict["status"],
                "started_at": last_exec_dict["started_at"],
                "duration_ms": last_exec_dict["duration_ms"]
            }

        # Get next run time
        next_run = None
        job = scheduler.get_job(task_id)
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()

        return TaskResponse(
            id=task_dict["id"],
            name=task_dict["name"],
            description=task_dict["description"],
            task_type=task_dict["task_type"],
            schedule=task_dict["schedule"],
            enabled=bool(task_dict["enabled"]),
            max_retries=task_dict["max_retries"],
            last_run=last_run,
            next_run=next_run
        )


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(task_id: str, request: TaskUpdateRequest):
    """Update a task's schedule or enabled status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check task exists
        cursor = await db.execute(
            "SELECT * FROM scheduled_tasks WHERE id = ?",
            (task_id,)
        )
        task = await cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        task_dict = dict(task)

        # Build update query
        updates = []
        params = []

        if request.schedule is not None:
            updates.append("schedule = ?")
            params.append(request.schedule)
            task_dict["schedule"] = request.schedule

        if request.enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if request.enabled else 0)
            task_dict["enabled"] = request.enabled

        if request.max_retries is not None:
            updates.append("max_retries = ?")
            params.append(request.max_retries)
            task_dict["max_retries"] = request.max_retries

        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(task_id)

            await db.execute(
                f"UPDATE scheduled_tasks SET {', '.join(updates)} WHERE id = ?",
                params
            )
            await db.commit()

            # Update scheduler
            if request.enabled is False:
                # Remove job from scheduler
                if scheduler.get_job(task_id):
                    scheduler.remove_job(task_id)
            elif request.enabled is True or request.schedule is not None:
                # Re-register with new schedule
                if scheduler.get_job(task_id):
                    scheduler.remove_job(task_id)

                if task_dict["enabled"]:
                    try:
                        if task_dict["task_type"] == "interval":
                            trigger_kwargs = parse_interval_schedule(task_dict["schedule"])
                            trigger = IntervalTrigger(**trigger_kwargs)
                        else:
                            parts = task_dict["schedule"].split()
                            trigger = CronTrigger(
                                minute=parts[0],
                                hour=parts[1],
                                day=parts[2],
                                month=parts[3],
                                day_of_week=parts[4]
                            )

                        scheduler.add_job(
                            execute_task,
                            trigger=trigger,
                            args=[task_id],
                            id=task_id,
                            replace_existing=True
                        )
                    except Exception as e:
                        raise HTTPException(status_code=400, detail=f"Invalid schedule: {e}")

    # Return updated task
    return await get_task(task_id)


@app.post("/api/tasks/{task_id}/trigger")
async def trigger_task(task_id: str):
    """Manually trigger a task to run immediately."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM scheduled_tasks WHERE id = ?",
            (task_id,)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Task not found")

    if task_id not in TASK_REGISTRY:
        raise HTTPException(status_code=400, detail="Task has no implementation")

    # Run the task asynchronously
    asyncio.create_task(execute_task(task_id))

    return {"status": "triggered", "task_id": task_id}


@app.get("/api/tasks/{task_id}/history", response_model=List[TaskExecutionResponse])
async def get_task_history(task_id: str, limit: int = 20):
    """Get execution history for a task."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check task exists
        cursor = await db.execute(
            "SELECT id FROM scheduled_tasks WHERE id = ?",
            (task_id,)
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Task not found")

        cursor = await db.execute(
            """SELECT * FROM task_executions
               WHERE task_id = ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (task_id, limit)
        )
        rows = await cursor.fetchall()

        result = []
        for row in rows:
            row_dict = dict(row)
            result_data = None
            if row_dict["result"]:
                try:
                    result_data = json.loads(row_dict["result"])
                except:
                    result_data = {"raw": row_dict["result"]}

            result.append(TaskExecutionResponse(
                id=row_dict["id"],
                task_id=row_dict["task_id"],
                status=row_dict["status"],
                started_at=row_dict["started_at"],
                completed_at=row_dict["completed_at"],
                duration_ms=row_dict["duration_ms"],
                result=result_data,
                retry_count=row_dict["retry_count"]
            ))

        return result


# ============ Cron & Heartbeat Endpoints ============
# Local cron engine (replaced OpenClaw proxy) + heartbeat log access

OPENCLAW_WORKSPACE = Path.home() / ".openclaw" / "workspace"
HEARTBEAT_LOG_PATH = OPENCLAW_WORKSPACE / "memory" / "heartbeat_log.md"
WATCHDOG_LOG_PATH = OPENCLAW_WORKSPACE / "memory" / "watchdog_log.md"
HEARTBEAT_STATE_PATH = OPENCLAW_WORKSPACE / "memory" / "heartbeat-state.json"
HEARTBEAT_INTERVAL_SECONDS = 15 * 60  # 15 minutes


@app.get("/api/cron/jobs")
async def list_cron_jobs():
    """List all cron jobs from local engine."""
    jobs = await cron_engine.get_jobs()
    return {"jobs": jobs}


@app.get("/api/cron/jobs/{job_id}")
async def get_cron_job(job_id: str):
    """Get a single cron job."""
    job = await cron_engine.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/cron/jobs")
async def create_cron_job(request: Request):
    """Create a new cron job."""
    data = await request.json()
    if "name" not in data or "command" not in data or "schedule" not in data:
        raise HTTPException(status_code=400, detail="name, command, and schedule are required")
    try:
        job = await cron_engine.create_job(data)
    except ValueError as e:
        msg = str(e)
        status = 409 if "already exists" in msg else 400
        raise HTTPException(status_code=status, detail=msg)
    return job


@app.patch("/api/cron/jobs/{job_id}")
async def update_cron_job(job_id: str, request: Request):
    """Update a cron job (enable/disable, schedule, command, etc.)."""
    data = await request.json()
    try:
        job = await cron_engine.update_job(job_id, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.delete("/api/cron/jobs/{job_id}")
async def delete_cron_job(job_id: str):
    """Delete a cron job and its run history."""
    deleted = await cron_engine.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"deleted": True}


@app.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str, dry_run: bool = False, delay_seconds: int = 0):
    """Manually trigger a cron job. Use ?dry_run=true to simulate without executing.
    Use ?delay_seconds=N to schedule execution in the future."""
    result = await cron_engine.trigger_job(job_id, dry_run=dry_run, delay_seconds=delay_seconds)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/cron/jobs/{job_id}/runs")
async def get_cron_job_runs(job_id: str, limit: int = 20):
    """Get recent run history for a cron job."""
    runs = await cron_engine.get_runs(job_id, limit=limit)
    return {"runs": runs}


@app.get("/api/cron/status")
async def get_cron_status():
    """Overall cron engine status."""
    return await cron_engine.get_status()


def _parse_heartbeat_entries(max_entries: int = 20) -> list:
    """Parse structured entries from heartbeat_log.md."""
    entries = []
    try:
        lines = HEARTBEAT_LOG_PATH.read_text().splitlines()
        for line in lines:
            line = line.strip()
            if not line.startswith("- ["):
                continue
            bracket_end = line.find("]", 3)
            if bracket_end == -1:
                continue
            timestamp = line[3:bracket_end]
            body = line[bracket_end + 1:].strip()

            entry_type = "idle"
            detail = body
            if body.upper().startswith("ACTION:"):
                entry_type = "action"
                detail = body[7:].strip()
            elif body.upper().startswith("IDLE:"):
                entry_type = "idle"
                detail = body[5:].strip()
            elif "heartbeat_ok" in body.lower():
                entry_type = "idle"
                detail = body

            entries.append({"timestamp": timestamp, "type": entry_type, "detail": detail})
    except Exception:
        pass
    return entries[-max_entries:]


@app.get("/api/system/heartbeat")
async def get_heartbeat_status():
    """Get combined heartbeat status from log, watchdog, and state files."""
    entries = _parse_heartbeat_entries(20)

    # Count consecutive idle from end
    consecutive_idle = 0
    for entry in reversed(entries):
        if entry["type"] == "idle":
            consecutive_idle += 1
        else:
            break

    # Count action vs idle in recent entries for activity ratio
    recent = entries[-10:] if len(entries) >= 10 else entries
    action_count = sum(1 for e in recent if e["type"] == "action")
    total_recent = len(recent)

    # Last heartbeat time
    last_hb_time = entries[-1]["timestamp"] if entries else None

    # Parse watchdog status
    watchdog_status = "unknown"
    watchdog_last_check = None
    try:
        wdog_lines = WATCHDOG_LOG_PATH.read_text().splitlines()
        for line in reversed(wdog_lines):
            line = line.strip()
            if not line.startswith("- ["):
                continue
            bracket_end = line.find("]", 3)
            if bracket_end == -1:
                continue
            watchdog_last_check = line[3:bracket_end]
            body = line[bracket_end + 1:].strip()
            if "STATUS OK" in body:
                watchdog_status = "ok"
            elif "TIER 1" in body:
                watchdog_status = "nudge"
            elif "TIER 2" in body:
                watchdog_status = "escalation"
            elif "WATCHDOG CHECK" in body:
                continue
            break
    except Exception:
        pass

    # Parse state file
    last_task = None
    try:
        state = json.loads(HEARTBEAT_STATE_PATH.read_text())
        last_task = state.get("last_task_worked")
    except Exception:
        pass

    # Get openclaw heartbeat status
    openclaw_status = None
    try:
        result = subprocess.run(
            ["openclaw", "system", "heartbeat", "last"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            openclaw_status = json.loads(result.stdout)
            if not isinstance(openclaw_status, dict):
                openclaw_status = None
    except Exception:
        pass

    # Compute last heartbeat epoch for countdown timer
    last_hb_epoch = None
    if openclaw_status and openclaw_status.get("ts"):
        last_hb_epoch = openclaw_status["ts"] / 1000.0

    return {
        "entries": entries,
        "consecutive_idle": consecutive_idle,
        "action_count": action_count,
        "total_recent": total_recent,
        "last_hb_time": last_hb_time,
        "last_hb_epoch": last_hb_epoch,
        "watchdog_status": watchdog_status,
        "watchdog_last_check": watchdog_last_check,
        "last_task": last_task,
        "openclaw_status": openclaw_status,
    }


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "tts_backend": {
            "current": TTS_BACKEND["current"],
            "satellite_available": TTS_BACKEND["satellite_available"],
        },
        "tts_global_mode": TTS_GLOBAL_MODE["mode"],
        "in_meeting": DESKTOP_STATE.get("in_meeting", False),
    }


@app.get("/api/logs/recent", response_model=LogsResponse)
async def get_recent_logs(limit: int = 50):
    """
    Get recent server logs from circular buffer.

    Args:
        limit: Maximum number of logs to return (default 50, max 100)

    Returns:
        LogsResponse with recent logs and count
    """
    # Limit the limit parameter to max 100
    limit = min(limit, 100)

    # Get the most recent N entries from the buffer
    recent_logs = list(log_buffer)[-limit:]

    return {
        "logs": recent_logs,
        "count": len(recent_logs)
    }


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "name": "Token-API",
        "version": "0.1.0",
        "description": "Local FastAPI server for Claude instance management",
        "docs": "/docs"
    }


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
        result = subprocess.run(
            ["afplay", sound_path],
            capture_output=True,
            timeout=10
        )
        if result.returncode == 0:
            return {"success": True, "method": "afplay", "file": sound_path}
        return {"success": False, "error": f"afplay failed: {result.stderr.decode()[:100]}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Sound playback timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def log_event_sync(event_type: str, instance_id: str = None, device_id: str = None, details: dict = None):
    """Synchronous wrapper for logging events (for use in sync functions)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """INSERT INTO events (event_type, instance_id, device_id, details)
               VALUES (?, ?, ?, ?)""",
            (event_type, instance_id, device_id, json.dumps(details) if details else None)
        )
        await db.commit()


def clean_markdown_for_tts(text: str) -> str:
    """Clean markdown syntax for natural TTS output.

    Removes/transforms markdown that sounds bad when spoken aloud,
    like table separators ("pipe dash dash dash") or headers ("hash hash").
    """
    import re

    # Unicode arrows/symbols that TTS mispronounces
    text = text.replace('→', ' to ')
    text = text.replace('←', ' from ')
    text = text.replace('↔', ' both ways ')
    text = text.replace('⇒', ' implies ')
    text = text.replace('⇐', ' implied by ')
    text = text.replace('➜', ' to ')
    text = text.replace('➔', ' to ')
    text = text.replace('•', ',')  # Bullet point
    text = text.replace('…', '...')  # Ellipsis
    text = text.replace('—', ', ')  # Em dash
    text = text.replace('–', ', ')  # En dash

    # Remove backslashes that might be read aloud
    text = text.replace('\\', ' ')

    # Path compression - replace long paths with friendly names
    path_replacements = [
        ('~/.openclaw/workspace/', ''),
        ('~/', ''),
    ]
    for path, replacement in path_replacements:
        text = text.replace(path, replacement)

    # Table separators: |---|---| or |:---:|:---:| → remove entirely
    text = re.sub(r'\|[-:]+\|[-:|\s]+', '', text)  # Table separator rows
    text = re.sub(r'^-{3,}$', '', text, flags=re.MULTILINE)  # Horizontal rules

    # Headers: ## Title → Title (strip # sequences followed by space)
    text = re.sub(r'#{1,6}\s+', '', text)

    # Bold/italic: **text** or *text* or __text__ or _text_ → text
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)  # Bold
    text = re.sub(r'\*(.+?)\*', r'\1', text)       # Italic
    text = re.sub(r'__(.+?)__', r'\1', text)       # Bold alt
    text = re.sub(r'_(.+?)_', r'\1', text)         # Italic alt

    # Code blocks: ```...``` → [code block]
    text = re.sub(r'```[\s\S]*?```', '[code block]', text)

    # Inline code: `code` → code
    text = re.sub(r'`([^`]+)`', r'\1', text)

    # Links: [text](url) → text
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    # Bullet points: - item or * item → item
    text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)

    # Numbered lists: 1. item → item
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)

    # Table pipes: | cell | cell | → cell, cell
    text = re.sub(r'\|', ', ', text)

    # Clean up multiple spaces/newlines
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'  +', ' ', text)
    text = re.sub(r', ,', ',', text)  # Clean double commas from empty cells

    return text.strip()


def speak_tts_mac(message: str, voice: str = None, rate: int = 0) -> dict:
    """Speak a message using macOS `say` command.

    Uses Popen instead of run() to allow process termination via skip_tts().
    """
    global tts_current_process, tts_skip_requested

    voice = voice or "Daniel"
    TTS_BACKEND["current"] = "mac"

    # Map SAPI rate scale (-10..10) to say WPM; default 0 → 190 WPM (slightly fast)
    wpm = 190 if rate == 0 else 175 + (rate * 15)
    wpm = max(80, min(300, wpm))

    try:
        process = subprocess.Popen(
            ["say", "-v", voice, "-r", str(wpm), message],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        tts_current_process = process
        process.wait(timeout=300)
        tts_current_process = None
        TTS_BACKEND["current"] = None

        if process.returncode == 0:
            return {"success": True, "method": "macos_say", "voice": voice, "message": message[:50]}
        if tts_skip_requested:
            tts_skip_requested = False
            return {"success": True, "method": "skipped", "message": message[:50]}
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


def speak_tts_wsl(message: str, voice: str, rate: int = 0) -> dict:
    """Speak a message via WSL satellite TTS (Windows SAPI voices).

    Blocks until satellite returns (speech complete or skipped).
    """
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    TTS_BACKEND["current"] = "wsl"

    try:
        resp = requests.post(
            f"http://{host}:{port}/tts/speak",
            json={"message": message, "voice": voice, "rate": rate},
            timeout=300  # Long timeout — blocks until speech done
        )
        TTS_BACKEND["current"] = None

        if resp.status_code == 200:
            data = resp.json()
            method = "skipped" if data.get("skipped") else "wsl_sapi"
            return {"success": data.get("success", False), "method": method, "voice": voice, "message": message[:50]}
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


def speak_tts(message: str, voice: str = None, rate: int = 0,
              instance_id: str = None, wsl_voice: str = None, wsl_rate: int = None) -> dict:
    """Route TTS to WSL satellite (preferred) or Mac fallback.

    Args:
        message: Text to speak
        voice: macOS voice name (for Mac fallback)
        rate: Rate for Mac TTS
        instance_id: Optional instance ID for logging
        wsl_voice: Windows SAPI voice name (for WSL)
        wsl_rate: Rate for WSL TTS (-10 to 10)
    """
    if not message:
        return {"success": False, "error": "No message provided"}

    # Clean markdown syntax for natural TTS output
    message = clean_markdown_for_tts(message)

    # Try WSL first if voice available and satellite is up
    if wsl_voice and is_satellite_tts_available():
        result = speak_tts_wsl(message, wsl_voice, wsl_rate if wsl_rate is not None else 0)
        if result.get("success"):
            return result
        # Any WSL failure → fallback to Mac
        logger.info(f"TTS: WSL failed ({result.get('error')}), falling back to Mac ({voice or 'Daniel'})")

    return speak_tts_mac(message, voice, rate)


# ============ TTS Queue System ============
# Ensures TTS messages don't overlap - each plays sequentially

from dataclasses import dataclass, field

@dataclass
class TTSQueueItem:
    """Item in the TTS queue."""
    instance_id: str
    message: str
    voice: str
    sound: str
    tab_name: str
    queued_at: datetime = field(default_factory=datetime.now)
    status: str = "queued"  # queued, playing, completed

# Global TTS queue state
tts_queue: Deque[TTSQueueItem] = deque()
tts_current: Optional[TTSQueueItem] = None
tts_current_process: Optional[subprocess.Popen] = None  # Current TTS/sound process for skip support
tts_skip_requested: bool = False  # Flag to indicate skip was requested (vs. actual failure)
tts_queue_lock = asyncio.Lock()
tts_worker_task: Optional[asyncio.Task] = None
stale_flag_cleaner_task: Optional[asyncio.Task] = None
timer_worker_task: Optional[asyncio.Task] = None


async def tts_queue_worker():
    """Background worker that processes TTS queue sequentially."""
    global tts_current

    while True:
        try:
            # Wait for items in queue
            async with tts_queue_lock:
                if tts_queue:
                    tts_current = tts_queue.popleft()
                else:
                    tts_current = None

            if tts_current:
                # Log TTS starting
                await log_event(
                    "tts_playing",
                    instance_id=tts_current.instance_id,
                    details={
                        "message": tts_current.message[:100],
                        "voice": tts_current.voice,
                        "tab_name": tts_current.tab_name
                    }
                )

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
                    # Look up profile by WSL voice (DB tts_voice stores WSL voice name)
                    # to get mac_voice fallback and wsl_rate
                    wsl_voice = tts_current.voice
                    mac_voice = "Daniel"  # default fallback
                    wsl_rate = 0
                    for p in PROFILES + FALLBACK_VOICES:
                        if p["wsl_voice"] == wsl_voice:
                            mac_voice = p.get("mac_voice", "Daniel")
                            wsl_rate = p.get("wsl_rate", 0)
                            break

                    # Speak the message (run in executor to allow skip API to interrupt)
                    logger.info(f"TTS worker: speaking {len(tts_current.message)} chars with {wsl_voice} (mac={mac_voice})")
                    loop = asyncio.get_event_loop()
                    tts_result = await loop.run_in_executor(
                        None, functools.partial(
                            speak_tts, tts_current.message, mac_voice,
                            0, tts_current.instance_id, wsl_voice, wsl_rate
                        )
                    )
                    logger.info(f"TTS worker: speak result = {json.dumps(tts_result)}")

                    # Log completion, skip, or failure
                    if tts_result.get("success"):
                        if tts_result.get("method") == "skipped":
                            logger.info(f"TTS skipped for {tts_current.instance_id}")
                            await log_event(
                                "tts_skipped",
                                instance_id=tts_current.instance_id,
                                details={
                                    "message": tts_current.message[:50],
                                    "voice": tts_current.voice
                                }
                            )
                        else:
                            await log_event(
                                "tts_completed",
                                instance_id=tts_current.instance_id,
                                details={
                                    "message": tts_current.message[:50],
                                    "voice": tts_current.voice
                                }
                            )
                    else:
                        logger.error(f"TTS failed for {tts_current.instance_id}: {tts_result.get('error')}")
                        await log_event(
                            "tts_failed",
                            instance_id=tts_current.instance_id,
                            details={
                                "message": tts_current.message[:50],
                                "voice": tts_current.voice,
                                "error": tts_result.get("error", "Unknown error"),
                                "sound_result": sound_result
                            }
                        )
                else:
                    logger.info(f"TTS worker: muted mode, sound only for {tts_current.instance_id}")

                tts_current = None
                await asyncio.sleep(0.5)  # Brief pause between items
            else:
                # No items - wait a bit before checking again
                await asyncio.sleep(0.1)

        except Exception as e:
            print(f"TTS worker error: {e}")
            await asyncio.sleep(1)


# Timer session tracking globals
_current_session_id = 0
_session_start_ms = 0
_mode_change_count = 0


async def timer_worker():
    """Background worker: ticks timer every 1s, persists state periodically."""
    global _current_session_id, _session_start_ms, _mode_change_count
    last_daily_update = 0.0
    last_db_save = 0.0
    last_mode = timer_engine.current_mode.value
    today = datetime.now().strftime("%Y-%m-%d")
    
    # Start initial session
    _current_session_id = await timer_start_session(timer_engine.current_mode.value, today)
    _session_start_ms = int(time.monotonic() * 1000)

    while True:
        try:
            await asyncio.sleep(1)
            now_ms = int(time.monotonic() * 1000)
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            current_hour = now.hour
            result = timer_engine.tick(now_ms, today, current_hour)

            # Handle events (some events come paired with MODE_CHANGED — handle specially)
            has_idle_timeout = TimerEvent.IDLE_TIMEOUT in result.events
            has_distraction_timeout = TimerEvent.DISTRACTION_TIMEOUT in result.events
            for event in result.events:
                if event == TimerEvent.IDLE_TIMEOUT:
                    print("TIMER: Idle timeout — auto-transitioning to BREAK")
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        await timer_end_session(_current_session_id, duration_ms)
                    await timer_log_shift("idle", "break", trigger="idle_timeout", source="timer_worker")
                    _current_session_id = await timer_start_session("break", today)
                    _session_start_ms = now_ms
                    _mode_change_count += 1
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(None, speak_tts, "Idle timeout. Entering break mode.")
                    continue
                elif event == TimerEvent.DISTRACTION_TIMEOUT:
                    print("TIMER: Distraction timeout — scrolling/gaming ≥10min → DISTRACTED")
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        await timer_end_session(_current_session_id, duration_ms)
                    await timer_log_shift("multitasking", "distracted", trigger="distraction_timeout", source="timer_worker")
                    _current_session_id = await timer_start_session("distracted", today)
                    _session_start_ms = now_ms
                    _mode_change_count += 1
                    # Enforce: close distraction windows + Pavlok
                    close_distraction_windows()
                    send_pavlok_stimulus(reason="distraction_timeout")
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(None, speak_tts, "Distraction timeout. Close distractions now.")
                    continue
                elif event == TimerEvent.MODE_CHANGED and (has_idle_timeout or has_distraction_timeout):
                    continue  # Already handled above
                elif event == TimerEvent.BREAK_EXHAUSTED:
                    await timer_log_shift(timer_engine.current_mode.value, "break_exhausted",
                                         trigger="enforcement", source="timer_worker")
                    asyncio.create_task(_async_enforce_break_exhausted())
                elif event == TimerEvent.DAILY_RESET:
                    print(f"TIMER: Daily reset (was {result.reset_date}, now {today}). Productivity score: {result.productivity_score}")
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        await timer_end_session(_current_session_id, duration_ms, break_earned_ms=timer_engine.total_break_time_ms)
                    await timer_save_daily_score(
                        result.reset_date,
                        result.productivity_score or 0,
                        timer_engine.total_work_time_ms,
                        timer_engine.total_break_time_ms,
                        _mode_change_count,
                        _mode_change_count
                    )
                    await generate_daily_timer_analytics(result.reset_date)
                    await timer_log_shift(result.old_mode.value if result.old_mode else None,
                                         timer_engine.current_mode.value, trigger="daily_reset", source="timer_worker")
                    _mode_change_count = 0
                    _current_session_id = await timer_start_session(timer_engine.current_mode.value, today)
                    _session_start_ms = now_ms
                elif event == TimerEvent.MODE_CHANGED and result.old_mode:
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        if result.old_mode in (TimerMode.WORKING, TimerMode.MULTITASKING, TimerMode.DISTRACTED):
                            await timer_end_session(_current_session_id, duration_ms, break_earned_ms=timer_engine.total_break_time_ms)
                        else:
                            await timer_end_session(_current_session_id, duration_ms, break_used_ms=timer_engine.total_break_time_ms)
                    await timer_log_mode_change(result.old_mode.value if result.old_mode else None, timer_engine.current_mode.value, is_automatic=False)
                    _mode_change_count += 1
                    _current_session_id = await timer_start_session(timer_engine.current_mode.value, today)
                    _session_start_ms = now_ms
                    async with aiosqlite.connect(DB_PATH) as _wdb:
                        _cur = await _wdb.execute(
                            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
                        )
                        _row = await _cur.fetchone()
                        _active = _row[0] if _row else 0
                    asyncio.create_task(push_phone_widget_async(timer_engine.current_mode.value, _active))

            # Phone current_app staleness check: if last_activity is >3 min old,
            # it's a phantom open (real usage would get refreshed by the debounce
            # or by a close event). Clear it to prevent false enforcement.
            _phone_last = PHONE_STATE.get("last_activity")
            _phone_app = PHONE_STATE.get("current_app")
            if _phone_app and _phone_last:
                try:
                    _phone_age = (datetime.now() - datetime.fromisoformat(_phone_last)).total_seconds()
                    if _phone_age > 180:  # 3 minutes stale
                        print(f"TIMER: Clearing stale phone_app={_phone_app!r} (last_activity {_phone_age:.0f}s ago)")
                        PHONE_STATE["current_app"] = None
                        PHONE_STATE["is_distracted"] = False
                        if _phone_app in ("twitter", "x", "com.twitter.android"):
                            PHONE_STATE["twitter_open_since"] = None
                        # Restore timer activity to working
                        DESKTOP_STATE["current_mode"] = "silence"
                        timer_engine.set_activity(Activity.WORKING, is_scrolling_gaming=False,
                                                  now_mono_ms=int(time.monotonic() * 1000))
                except Exception:
                    pass

            # Twitter 7-minute enforcement
            twitter_since = PHONE_STATE.get("twitter_open_since")
            if twitter_since is not None:
                # Staleness guard: if current_app is no longer twitter, the close
                # telemetry was likely dropped by MacroDroid — clear the timer
                current_app = (PHONE_STATE.get("current_app") or "").lower()
                if current_app not in ("twitter", "x", "com.twitter.android"):
                    stale_elapsed = time.monotonic() - twitter_since
                    print(f"TIMER: Twitter timer stale ({stale_elapsed:.0f}s) — current_app={current_app!r}, clearing (dropped close event)")
                    PHONE_STATE["twitter_open_since"] = None
                    PHONE_STATE["twitter_zapped"] = False
                else:
                    twitter_elapsed = time.monotonic() - twitter_since
                    if twitter_elapsed > 420:  # 7 minutes
                        now_mono = time.monotonic()
                        since_last_zap = now_mono - PHONE_STATE.get("twitter_last_zap_at", 0)
                        if since_last_zap < 1800:  # 30-minute cooldown
                            print(f"TIMER: Twitter 7-min hit but cooldown active ({since_last_zap:.0f}s < 1800s). Skipping zap.")
                            PHONE_STATE["twitter_open_since"] = None
                        else:
                            print(f"TIMER: Twitter open for {twitter_elapsed:.0f}s (>7min). Forcing break.")
                            PHONE_STATE["twitter_open_since"] = None  # one-shot per session
                            PHONE_STATE["twitter_zapped"] = True  # block re-zap until confirmed close
                            PHONE_STATE["twitter_last_zap_at"] = now_mono
                            PHONE_STATE["twitter_last_zap_wall"] = time.time()
                            _persist_twitter_zap_cooldown()
                            asyncio.create_task(_async_enforce_twitter_timeout())

            now = time.time()

            # Update idle_timeout_exempt based on location only
            # NOTE: work_mode is manual-only now (user clocks in/out explicitly).
            # Location-based exemptions still apply (e.g., campus = studying).
            location_zone = DESKTOP_STATE.get("location_zone")
            timer_engine.idle_timeout_exempt = (location_zone == "campus")

            # Productivity layer update (every 10s) — poll DB for active instances
            if now - last_db_save >= 10:  # piggyback on DB save interval
                any_processing = False
                async with aiosqlite.connect(DB_PATH) as db:
                    cursor = await db.execute(
                        """SELECT COUNT(*) FROM claude_instances
                           WHERE status = 'processing'
                           AND last_activity > datetime('now', '-60 seconds', 'localtime')"""
                    )
                    row = await cursor.fetchone()
                    any_processing = (row[0] if row else 0) > 0

                old_mode = timer_engine.current_mode.value
                prod_result = timer_engine.set_productivity(any_processing, now_ms)
                if TimerEvent.MODE_CHANGED in prod_result.events:
                    new_mode = timer_engine.current_mode.value
                    trigger = "productivity_active" if any_processing else "productivity_inactive"
                    print(f"TIMER: Productivity {trigger} — {old_mode} → {new_mode}")
                    await timer_log_shift(old_mode, new_mode, trigger=trigger, source="timer_worker")
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        await timer_end_session(_current_session_id, duration_ms)
                    _current_session_id = await timer_start_session(new_mode, today)
                    _session_start_ms = now_ms
                    _mode_change_count += 1

            # Update daily note every 30s
            if now - last_daily_update >= 30:
                await timer_update_daily_note()
                last_daily_update = now

            # Save to DB every 10s
            if now - last_db_save >= 10:
                await timer_save_to_db()
                last_db_save = now

        except asyncio.CancelledError:
            # Save state on shutdown
            await timer_save_to_db()
            raise
        except Exception as e:
            print(f"TIMER worker error: {e}")
            await asyncio.sleep(1)


async def _async_enforce_break_exhausted():
    """Async enforcement when timer detects break exhaustion."""
    print("TIMER: Enforcing break exhaustion")
    result = await enforce_break_exhausted_impl()
    await log_event("break_exhausted_enforcement", details=result)


async def _async_enforce_twitter_timeout():
    """Enforce Twitter 7-minute timeout: notify, zap, force break."""
    global _current_session_id, _session_start_ms
    now_ms = int(time.monotonic() * 1000)

    # Send notification sound + TTS
    play_sound()
    try:
        subprocess.Popen(["say", "-v", "Daniel", "Twitter open for 7 minutes. Forcing break."])
    except Exception:
        pass

    # Send low-intensity Pavlok zap
    send_pavlok_stimulus(stimulus_type="zap", value=30, reason="twitter_timeout")

    # Force timer into BREAK mode (clear any existing manual mode first)
    old_mode = timer_engine.current_mode.value
    timer_engine._clear_manual_mode()
    changed, _ = timer_engine.enter_break(now_ms)
    if changed:
        today = datetime.now().strftime("%Y-%m-%d")
        await timer_log_mode_change(old_mode, "break", is_automatic=False)
        await timer_log_shift(old_mode, "break", trigger="enforcement", source="timer_worker",
                              phone_app="twitter")
        await timer_end_session(_current_session_id, now_ms - _session_start_ms)
        _current_session_id = await timer_start_session("break", today)
        _session_start_ms = now_ms

    await log_event("twitter_timeout_enforcement", details={
        "old_mode": old_mode,
        "forced_break": changed,
    })


async def enforce_break_exhausted_impl() -> dict:
    """Shared enforcement logic for break exhaustion (used by timer worker and API endpoint)."""
    enforced_any = False
    phone_result = None
    desktop_result = None

    # Desktop enforcement: close distraction windows
    desktop_result = close_distraction_windows()
    if desktop_result.get("closed_count"):
        enforced_any = True
        print(f"BREAK-EXHAUSTED: Closed {desktop_result['closed_count']} desktop distraction windows")

    # Phone enforcement: disable active distraction app
    current_app = PHONE_STATE.get("current_app")
    if current_app:
        enforce_app = current_app
        if current_app in ("x", "twitter", "com.twitter.android"):
            enforce_app = "twitter"
        elif current_app in ("youtube", "com.google.android.youtube", "app.revanced.android.youtube"):
            enforce_app = "youtube"
        elif current_app in PHONE_DISTRACTION_APPS:
            mode = PHONE_DISTRACTION_APPS.get(current_app)
            if mode == "gaming":
                enforce_app = "game"

        print(f"BREAK-EXHAUSTED: Enforcing disable on {current_app} (mapped to {enforce_app})")
        phone_result = enforce_phone_app(enforce_app, action="disable")
        enforced_any = True

        PHONE_STATE["current_app"] = None
        PHONE_STATE["is_distracted"] = False

        # Switch timer/desktop back to working since phone distraction is being closed
        DESKTOP_STATE["current_mode"] = "silence"
        DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
        now_ms = int(time.monotonic() * 1000)
        timer_engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=now_ms)

    if enforced_any:
        send_pavlok_stimulus(reason="break_exhausted")

    return {
        "enforced": enforced_any,
        "app": current_app,
        "desktop_enforcement": desktop_result,
        "phone_enforcement": phone_result,
    }


async def clear_stale_processing_flags():
    """Background worker that auto-clears status='processing' for instances inactive > 5 minutes."""
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("""
                    UPDATE claude_instances
                    SET status = 'idle'
                    WHERE status = 'processing'
                      AND datetime(last_activity) < datetime('now', 'localtime', '-5 minutes')
                """)
                await db.commit()

                if cursor.rowcount > 0:
                    logger.warning(f"Auto-cleared {cursor.rowcount} stale processing flags")

            await asyncio.sleep(60)  # Run every minute

        except Exception as e:
            logger.error(f"Error clearing stale flags: {e}")
            await asyncio.sleep(60)


async def detect_stuck_instances():
    """Background worker that detects potentially stuck instances and logs diagnostics.

    An instance is considered potentially stuck if:
    - Status is 'processing' or 'idle' (not stopped)
    - Last activity was > 10 minutes ago
    - The stored PID doesn't match any running claude process

    Runs every 5 minutes. Logs warnings but doesn't take action.
    """
    await asyncio.sleep(120)  # Wait 2 min after startup before first check

    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("""
                    SELECT id, tab_name, working_dir, pid, status, device_id, last_activity
                    FROM claude_instances
                    WHERE status IN ('processing', 'idle')
                      AND device_id = 'desktop'
                      AND datetime(last_activity) < datetime('now', 'localtime', '-10 minutes')
                """)
                stale_instances = await cursor.fetchall()

            for row in stale_instances:
                instance = dict(row)
                instance_id = instance["id"]
                tab_name = instance.get("tab_name", instance_id[:8])
                stored_pid = instance.get("pid")
                working_dir = instance.get("working_dir", "")
                last_activity = instance.get("last_activity")

                # Check if stored PID is still a claude process
                pid_valid = stored_pid and is_pid_claude(stored_pid)

                # Try to discover actual PID
                discovered_pid = await find_claude_pid_by_workdir(working_dir) if working_dir else None

                if not pid_valid and not discovered_pid:
                    # Ghost instance: no process found
                    logger.warning(
                        f"STUCK DETECTION: Ghost instance '{tab_name}' ({instance_id[:8]}...) - "
                        f"stored_pid={stored_pid} invalid, no process found in {working_dir}, "
                        f"last_activity={last_activity}"
                    )
                elif not pid_valid and discovered_pid:
                    # PID mismatch: process exists but DB has wrong PID
                    logger.warning(
                        f"STUCK DETECTION: PID mismatch '{tab_name}' ({instance_id[:8]}...) - "
                        f"stored_pid={stored_pid} invalid, discovered_pid={discovered_pid}, "
                        f"last_activity={last_activity}"
                    )
                elif pid_valid:
                    # Process exists but inactive for >10 min - might be stuck
                    diag = get_process_diagnostics(stored_pid)
                    state = diag.get("state", "?")
                    wchan = diag.get("wchan", "?")
                    children = len(diag.get("children", []))

                    # Log if in uninterruptible sleep (D) or has been in same state
                    if state == "D":
                        logger.warning(
                            f"STUCK DETECTION: Uninterruptible sleep '{tab_name}' ({instance_id[:8]}...) - "
                            f"PID {stored_pid} state=D wchan={wchan}, last_activity={last_activity}"
                        )
                    else:
                        logger.info(
                            f"STUCK CHECK: '{tab_name}' ({instance_id[:8]}...) - "
                            f"PID {stored_pid} state={state} wchan={wchan} children={children}, "
                            f"last_activity={last_activity} (>10min stale but process alive)"
                        )

            await asyncio.sleep(300)  # Run every 5 minutes

        except Exception as e:
            logger.error(f"Error in stuck detection: {e}")
            await asyncio.sleep(300)


def _is_quiet_hours() -> bool:
    """Return True if current time is in quiet hours (11 PM - 9 AM). No TTS during sleep."""
    hour = datetime.now().hour
    return hour >= 23 or hour < 9


async def queue_tts(instance_id: str, message: str) -> dict:
    """Queue a TTS message for an instance, using their profile's voice/sound."""
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
            "SELECT tab_name, tts_voice, notification_sound, tts_mode FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()

    if not row:
        return {"success": False, "error": f"Instance {instance_id} not found"}

    voice = row["tts_voice"] or "Microsoft David"
    sound = row["notification_sound"] or "chimes.wav"
    tab_name = row["tab_name"] or instance_id

    # Check TTS mode (per-instance and global, most restrictive wins)
    instance_mode = row["tts_mode"] or "verbose"
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
            tab_name=tab_name
        )
    else:
        item = TTSQueueItem(
            instance_id=instance_id,
            message=message,
            voice=voice,
            sound=sound,
            tab_name=tab_name
        )

    async with tts_queue_lock:
        tts_queue.append(item)
        position = len(tts_queue)

    # Log queued event
    await log_event(
        "tts_queued",
        instance_id=instance_id,
        details={
            "message": message[:100],
            "voice": voice,
            "position": position
        }
    )

    return {
        "success": True,
        "queued": True,
        "position": position,
        "voice": voice,
        "sound": sound
    }


def get_tts_queue_status() -> dict:
    """Get current TTS queue status for dashboard."""
    queue_list = []
    for item in tts_queue:
        queue_list.append({
            "instance_id": item.instance_id,
            "tab_name": item.tab_name,
            "message": item.message[:50] + "..." if len(item.message) > 50 else item.message,
            "voice": item.voice,
            "queued_at": item.queued_at.isoformat()
        })

    current = None
    if tts_current:
        current = {
            "instance_id": tts_current.instance_id,
            "tab_name": tts_current.tab_name,
            "message": tts_current.message[:50] + "..." if len(tts_current.message) > 50 else tts_current.message,
            "voice": tts_current.voice
        }

    return {
        "current": current,
        "queue": queue_list,
        "queue_length": len(queue_list),
        "backend": TTS_BACKEND["current"],
        "satellite_available": TTS_BACKEND["satellite_available"],
        "global_mode": TTS_GLOBAL_MODE["mode"],
        "voice_pool": {
            "total": len(PROFILES),
            "fallback_count": len(FALLBACK_VOICES),
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
    global tts_current_process, tts_current, tts_queue, tts_skip_requested

    result = {"skipped": False, "cleared": 0, "backend": TTS_BACKEND["current"]}
    current_backend = TTS_BACKEND["current"]

    if current_backend == "wsl":
        # Skip on WSL satellite
        host = DESKTOP_CONFIG["host"]
        port = DESKTOP_CONFIG["port"]
        try:
            resp = requests.post(f"http://{host}:{port}/tts/skip", timeout=3)
            result["skipped"] = resp.status_code == 200
            logger.info(f"TTS skip routed to WSL satellite: {resp.status_code}")
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

    # Clear queue if requested
    if clear_queue:
        async with tts_queue_lock:
            result["cleared"] = len(tts_queue)
            tts_queue.clear()
            if result["cleared"] > 0:
                logger.info(f"Cleared {result['cleared']} items from TTS queue")

    return result


def send_webhook(webhook_url: str, message: str, data: dict = None) -> dict:
    """Send notification via HTTP webhook."""
    payload = {
        "type": "notification",
        "message": message,
        "timestamp": datetime.now().isoformat(),
        **(data or {})
    }

    try:
        result = subprocess.run(
            [
                "curl", "-X", "POST",
                "-H", "Content-Type: application/json",
                "-d", json.dumps(payload),
                "--connect-timeout", "5",
                "-s",
                webhook_url
            ],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            return {"success": True, "method": "webhook", "url": webhook_url}
        return {"success": False, "error": f"Webhook failed: {result.stderr}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/notify")
async def send_notification(request: NotifyRequest):
    """Send notification to a device (sound + TTS or webhook)."""
    results = {"sound": None, "tts": None, "webhook": None}

    # Determine target device
    device_id = request.device_id

    if not device_id and request.instance_id:
        # Look up instance to get device
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT device_id FROM claude_instances WHERE id = ?",
                (request.instance_id,)
            )
            row = await cursor.fetchone()
            if row:
                device_id = row["device_id"]

    if not device_id:
        device_id = "Mac-Mini"  # Default

    # Get device config
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM devices WHERE id = ?",
            (device_id,)
        )
        device = await cursor.fetchone()

    if not device:
        raise HTTPException(status_code=404, detail=f"Device not found: {device_id}")

    device = dict(device)
    method = device.get("notification_method", "tts_sound")

    if method == "tts_sound":
        # Suppress sound + TTS during quiet hours (11 PM - 9 AM)
        if _is_quiet_hours():
            logger.info(f"Notification suppressed (quiet hours): {request.message[:80]}")
            results["sound"] = {"success": True, "suppressed": True, "reason": "quiet_hours"}
            results["tts"] = {"success": True, "suppressed": True, "reason": "quiet_hours"}
        else:
            # Desktop: play sound and speak
            await log_event(
                "tts_starting",
                instance_id=request.instance_id,
                device_id=device_id,
                details={"message": request.message[:100], "voice": request.voice or "default"}
            )
            results["sound"] = play_sound(request.sound)
            results["tts"] = speak_tts(request.message, request.voice)
    elif method == "webhook":
        # Mobile: send webhook
        webhook_url = device.get("webhook_url")
        if webhook_url:
            results["webhook"] = send_webhook(webhook_url, request.message)
        else:
            results["webhook"] = {"success": False, "error": "No webhook_url configured"}

    # Log the notification event
    await log_event(
        "notification_sent",
        device_id=device_id,
        details={"message": request.message[:100], "results": results}
    )

    return {
        "device_id": device_id,
        "method": method,
        "results": results
    }


@app.post("/api/notify/tts")
async def notify_tts(request: TTSRequest):
    """Speak a message using TTS only.

    When instance_id is provided and no explicit voice is set, uses the
    instance's assigned voice profile (WSL voice + Mac fallback).
    """
    if _is_quiet_hours():
        logger.info(f"TTS suppressed (quiet hours): {request.message[:80]}")
        return {"success": True, "suppressed": True, "reason": "quiet_hours"}

    # Resolve voice from instance profile when instance_id provided and no explicit voice
    voice = request.voice
    wsl_voice = None
    wsl_rate = None
    if request.instance_id and not request.voice:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT tts_voice FROM claude_instances WHERE id = ?",
                (request.instance_id,)
            )
            row = await cursor.fetchone()
        if row and row["tts_voice"]:
            wsl_voice = row["tts_voice"]
            # Look up full profile for Mac fallback and WSL rate
            for p in PROFILES + FALLBACK_VOICES:
                if p["wsl_voice"] == wsl_voice:
                    voice = p.get("mac_voice", "Daniel")
                    wsl_rate = p.get("wsl_rate", 0)
                    break

    # Log TTS starting
    await log_event(
        "tts_starting",
        instance_id=request.instance_id,
        details={"message": request.message[:100], "voice": wsl_voice or voice or "default"}
    )

    # Run in executor to allow skip API to interrupt
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, functools.partial(
            speak_tts, request.message, voice, request.rate,
            request.instance_id, wsl_voice, wsl_rate
        )
    )

    # Log TTS result
    await log_event(
        "tts_completed",
        instance_id=request.instance_id,
        details={"message": request.message[:50], "success": result.get("success", False)}
    )

    return result


@app.post("/api/notify/sound")
async def notify_sound(request: SoundRequest):
    """Play a notification sound only."""
    if _is_quiet_hours():
        logger.info(f"Sound suppressed (quiet hours): {request.sound_file}")
        return {"success": True, "suppressed": True, "reason": "quiet_hours"}

    result = play_sound(request.sound_file)

    await log_event(
        "sound_played",
        details={"file": request.sound_file, "result": result}
    )

    return result


class QueueTTSRequest(BaseModel):
    instance_id: str
    message: str


@app.post("/api/notify/queue")
async def queue_tts_message(request: QueueTTSRequest):
    """Queue a TTS message for an instance. Uses the instance's profile voice/sound.

    Messages are played sequentially - if another TTS is playing, this will queue.
    Returns the queue position.
    """
    return await queue_tts(request.instance_id, request.message)


@app.get("/api/notify/queue/status")
async def get_queue_status():
    """Get current TTS queue status."""
    return get_tts_queue_status()


@app.post("/api/tts/skip")
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


@app.post("/api/tts/global-mode")
async def set_global_tts_mode(request: Request):
    """Set global TTS mode. Overrides all instances."""
    body = await request.json()
    mode = body.get("mode", "verbose")
    if mode not in ("verbose", "muted", "silent"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    old_mode = TTS_GLOBAL_MODE["mode"]
    TTS_GLOBAL_MODE["mode"] = mode

    # Update all active instances to match
    async with aiosqlite.connect(DB_PATH) as db:
        if mode == "silent":
            # Release all voice slots for non-subagent active instances
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ?, tts_voice = NULL, notification_sound = NULL WHERE status IN ('processing', 'idle') AND is_subagent = 0",
                (mode,)
            )
        elif mode == "verbose" and old_mode == "silent":
            # Re-assign voices to all active instances that lost theirs
            cursor = await db.execute(
                "SELECT id FROM claude_instances WHERE status IN ('processing', 'idle') AND tts_voice IS NULL AND is_subagent = 0"
            )
            rows = await cursor.fetchall()
            used_voices = set()
            for row in rows:
                profile, _ = get_next_available_profile(used_voices)
                await db.execute(
                    "UPDATE claude_instances SET tts_mode = ?, tts_voice = ?, notification_sound = ? WHERE id = ?",
                    (mode, profile["wsl_voice"], profile["notification_sound"], row[0])
                )
                used_voices.add(profile["wsl_voice"])
        else:
            # muted or verbose (voices already assigned)
            await db.execute(
                "UPDATE claude_instances SET tts_mode = ? WHERE status IN ('processing', 'idle') AND is_subagent = 0",
                (mode,)
            )
        await db.commit()

    await log_event("tts_global_mode_changed", details={"mode": mode, "old_mode": old_mode})
    return {"status": "ok", "mode": mode, "old_mode": old_mode}


@app.get("/api/notify/test")
async def test_notification():
    """Test the notification system with a simple message."""
    sound_result = play_sound()
    tts_result = speak_tts("Token API notification test")

    return {
        "sound": sound_result,
        "tts": tts_result,
        "message": "Test notification sent"
    }


# ============ Claude Code Hook Handlers ============
# Centralized handling for all Claude Code hooks
# Replaces shell scripts with Python for better reliability and debugging

async def handle_session_start(payload: dict) -> dict:
    """Handle SessionStart hook - register new Claude instance."""
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        session_id = f"claude-{int(time.time())}-{os.getpid()}"

    # Detect origin type from env vars in payload
    origin_type = "local"
    source_ip = None
    env = payload.get("env", {})
    if env.get("CRON_JOB_NAME"):
        origin_type = "cron"
    elif env.get("SSH_CLIENT"):
        origin_type = "ssh"
        source_ip = env["SSH_CLIENT"].split()[0]

    # Get working directory and tab name
    working_dir = payload.get("cwd") or os.getcwd()
    tab_name = payload.get("env", {}).get("CLAUDE_TAB_NAME") or f"Claude {datetime.now().strftime('%H:%M')}"

    # Detect subagent from env var
    subagent_env = payload.get("env", {}).get("TOKEN_API_SUBAGENT", "")
    is_subagent = 1 if subagent_env else 0
    spawner = subagent_env or None

    # Auto-name subagents
    if is_subagent and not payload.get("env", {}).get("CLAUDE_TAB_NAME"):
        tab_name = f"sub: {spawner}"

    # Resolve device_id from source_ip
    device_id = resolve_device_from_ip(source_ip) if source_ip else "Mac-Mini"

    async with aiosqlite.connect(DB_PATH) as db:
        # Check if already registered
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        if await cursor.fetchone():
            return {"success": True, "action": "already_registered", "instance_id": session_id}

        # Skip TTS profile assignment for subagents (headless, no voice needed)
        if is_subagent:
            profile = {"name": None, "wsl_voice": None, "notification_sound": None}
            pool_exhausted = False
        else:
            # Get WSL voices held by active instances
            cursor = await db.execute(
                "SELECT tts_voice FROM claude_instances WHERE status IN ('processing', 'idle')"
            )
            rows = await cursor.fetchall()
            used_wsl_voices = {row[0] for row in rows if row[0]}

            # Assign profile via linear probe
            profile, pool_exhausted = get_next_available_profile(used_wsl_voices)

        # Insert instance
        now = datetime.now().isoformat()
        internal_session_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, source_ip, device_id,
                profile_name, tts_voice, notification_sound, pid, status,
                is_subagent, spawner,
                registered_at, last_activity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?)""",
            (
                session_id,
                internal_session_id,
                tab_name,
                working_dir,
                origin_type,
                source_ip,
                device_id,
                profile["name"],
                profile["wsl_voice"],
                profile["notification_sound"],
                payload.get("pid"),
                is_subagent,
                spawner,
                now,
                now
            )
        )
        # Auto-link primarch instance to its active session doc
        primarch_name = payload.get("env", {}).get("TOKEN_API_PRIMARCH", "")
        session_doc_id = None
        if primarch_name:
            cursor = await db.execute(
                "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
                (primarch_name,)
            )
            link_row = await cursor.fetchone()
            if link_row and link_row[0]:
                session_doc_id = link_row[0]
                await db.execute(
                    "UPDATE claude_instances SET session_doc_id = ? WHERE id = ?",
                    (session_doc_id, session_id)
                )

        await db.commit()

        # Update frontmatter if we linked a session doc
        if session_doc_id:
            await _update_doc_agents_list(db, session_doc_id)

    logger.info(f"Hook: SessionStart registered {session_id[:12]}... ({working_dir}){' [subagent]' if is_subagent else ''}{f' [primarch:{primarch_name}]' if primarch_name else ''}")
    await log_event("instance_registered", instance_id=session_id, device_id=device_id,
                    details={"tab_name": tab_name, "origin_type": origin_type, "source": "hook",
                             "is_subagent": is_subagent, "spawner": spawner,
                             "primarch": primarch_name or None})

    return {
        "success": True,
        "action": "registered",
        "instance_id": session_id,
        "profile": profile["name"] if not is_subagent else None,
        "session_doc_id": session_doc_id
    }


async def handle_session_end(payload: dict) -> dict:
    """Handle SessionEnd hook - deregister Claude instance."""
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    _pending_background_tasks.pop(session_id, None)

    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, device_id, COALESCE(is_subagent, 0), session_doc_id FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        row = await cursor.fetchone()

        if not row:
            return {"success": False, "action": "not_found", "instance_id": session_id}

        is_subagent = row[2]
        session_doc_id = row[3]

        # Count non-subagent active instances BEFORE stopping
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
        )
        count_row = await cursor.fetchone()
        was_active = count_row[0] if count_row else 0

        await db.execute(
            "UPDATE claude_instances SET status = 'stopped', stopped_at = ? WHERE id = ?",
            (now, session_id)
        )
        await db.commit()

        # Check remaining active instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        count_row = await cursor.fetchone()
        remaining_active = count_row[0] if count_row else 0

        # Count remaining non-subagent active instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
        )
        count_row = await cursor.fetchone()
        remaining_non_sub = count_row[0] if count_row else 0

    logger.info(f"Hook: SessionEnd stopped {session_id[:12]}...")
    await log_event("instance_stopped", instance_id=session_id, device_id=row[1],
                    details={"source": "hook"})

    # Instance count Pavlok signals (skip subagents)
    if not is_subagent:
        await check_instance_count_pavlok(remaining_non_sub, was_active)

    # Spawn stop_hook.py to generate session blurb if instance has a linked session doc
    if session_doc_id and not is_subagent:
        stop_hook_script = Path(__file__).parent / "stop_hook.py"
        if stop_hook_script.exists():
            try:
                subprocess.Popen(
                    ["python3", str(stop_hook_script), session_id],
                    stdout=subprocess.DEVNULL,
                    stderr=open("/tmp/stop_hook.log", "a"),
                    start_new_session=True
                )
                logger.info(f"Hook: SessionEnd spawned stop_hook for {session_id[:12]}... (doc {session_doc_id})")
            except Exception as e:
                logger.warning(f"Hook: SessionEnd failed to spawn stop_hook: {e}")

    # Handle productivity enforcement if needed
    result = {"success": True, "action": "stopped", "instance_id": session_id}
    if remaining_active == 0 and DESKTOP_STATE.get("current_mode") == "video":
        enforce_result = close_distraction_windows()
        result["enforcement_triggered"] = True
        result["enforcement_result"] = enforce_result

    return result


async def handle_prompt_submit(payload: dict) -> dict:
    """Handle UserPromptSubmit hook - mark instance as processing."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Each UserPromptSubmit for a session with pending tasks = one background task result delivered.
    if session_id in _pending_background_tasks:
        _pending_background_tasks[session_id] -= 1
        if _pending_background_tasks[session_id] <= 0:
            del _pending_background_tasks[session_id]
        logger.info(f"PromptSubmit: background task returned for {session_id[:12]} (pending: {_pending_background_tasks.get(session_id, 0)})")

    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        if not await cursor.fetchone():
            return {"success": False, "action": "not_found"}

        # Also resurrect stopped instances - activity means they're active
        # Backfill PID if payload contains one and DB value is NULL
        await db.execute(
            """UPDATE claude_instances
               SET status = 'processing', last_activity = ?, stopped_at = NULL,
                   pid = COALESCE(pid, ?)
               WHERE id = ?""",
            (now, payload.get("pid"), session_id)
        )
        await db.commit()

    # Signal productivity — sets prod active, exits IDLE if needed
    now_ms = int(time.monotonic() * 1000)
    old_mode = timer_engine.current_mode.value
    result = timer_engine.set_productivity(True, now_ms)
    exited_idle = TimerEvent.MODE_CHANGED in result.events
    if exited_idle:
        new_mode = timer_engine.current_mode.value
        await timer_log_shift(old_mode, new_mode, trigger="prompt_submit", source="hook")
        logger.info(f"Hook: PromptSubmit exited {old_mode} → {new_mode}")

    logger.info(f"Hook: PromptSubmit {session_id[:12]}... -> processing (resurrected if stopped)")
    return {"success": True, "action": "processing", "instance_id": session_id, "exited_idle": exited_idle}


async def handle_post_tool_use(payload: dict) -> dict:
    """Handle PostToolUse hook - heartbeat with debouncing, ensures status='processing'."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Debounce: only update every 2 seconds per session
    current_time = time.time()
    last_call = _post_tool_debounce.get(session_id, 0)
    if current_time - last_call < 2:
        return {"success": True, "action": "debounced"}

    _post_tool_debounce[session_id] = current_time

    # Update last_activity as heartbeat AND ensure status='processing'
    # This catches cases where prompt_submit was missed (e.g., after context clear)
    # Also resurrect stopped instances - activity means they're active
    # Backfill PID if payload contains one and DB value is NULL
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE claude_instances
               SET status = 'processing', last_activity = ?, stopped_at = NULL,
                   pid = COALESCE(pid, ?)
               WHERE id = ?""",
            (now, payload.get("pid"), session_id)
        )
        await db.commit()

    # Signal productivity — active tool use = real work
    now_ms = int(time.monotonic() * 1000)
    timer_engine.set_productivity(True, now_ms)

    return {"success": True, "action": "heartbeat", "instance_id": session_id}


def _parse_assistant_turn_from_lines(lines: list) -> Optional[dict]:
    """Parse the last assistant turn from a list of JSONL lines."""
    blocks: list = []
    found = False
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        line_type = d.get("type", "")
        if line_type == "assistant":
            found = True
            content = d.get("message", {}).get("content", [])
            if isinstance(content, list):
                blocks = content + blocks
            elif isinstance(content, str):
                blocks.insert(0, {"type": "text", "text": content})
        elif line_type == "user" and found:
            break

    if blocks and any(b.get("type") == "text" for b in blocks):
        return {
            "text": "\n".join(b["text"] for b in blocks if b.get("type") == "text"),
            "tool_names": [b.get("name", "") for b in blocks if b.get("type") == "tool_use"],
            "last_block_type": blocks[-1].get("type", "unknown"),
        }
    return None


def _extract_last_assistant_turn(transcript_path: str, max_retries: int = 8, retry_delay: float = 0.25) -> Optional[dict]:
    """Extract last assistant turn from a transcript file, polling briefly for flush."""
    for attempt in range(max_retries):
        try:
            with open(transcript_path, "r") as f:
                lines = f.readlines()
        except OSError:
            return None

        result = _parse_assistant_turn_from_lines(lines)
        if result:
            return result

        if attempt < max_retries - 1:
            time.sleep(retry_delay)

    return None


def _check_stop_patterns(text: str) -> Optional[str]:
    """Return a block reason if text contains unverified action suggestions, else None."""
    # Pattern 1: User-directed instructions with action verbs
    if re.search(
        r'(please |you (can|should|need to|will need to|might want to|may want to) )'
        r'(run|execute|try running|start|launch|restart|open|install|add|create|update|configure|set up)',
        text, re.IGNORECASE
    ):
        return "Detected instruction for user to perform an action"

    # Pattern 2: Imperative sentences starting with action verbs
    if re.search(
        r'(^|\n)\s*(Run|Execute|Start|Launch|Install|Open|Add|Create|Configure|Set up|Copy|Paste'
        r'|Navigate to|Go to|Visit|Type|Enter) (the |this |these |following |it |a |your )',
        text, re.MULTILINE
    ):
        return "Detected imperative instruction to user"

    # Pattern 3: Shell command with $ prompt
    if re.search(r'(^|\n)\s*\$\s+\w', text, re.MULTILINE):
        return "Detected shell command with $ prompt"

    # Pattern 4: Copy/paste instructions
    if re.search(r'(copy and paste|paste (this|the|it) )', text, re.IGNORECASE):
        return "Detected copy/paste instruction"

    # Pattern 5: Open browser/terminal instructions
    if re.search(r'open (your |a |the )(browser|terminal|editor|file manager|console)', text, re.IGNORECASE):
        return "Detected instruction to open a tool manually"

    # Pattern 6 & 7: Shell code blocks with manual/offered instruction
    if re.search(r'```(bash|shell|sh|zsh|console|terminal)', text):
        if re.search(r'(you (can|should|need to)|please )(run|execute|add|copy|paste|use)', text, re.IGNORECASE):
            return "Detected shell code block with manual instruction"
        if re.search(
            r'(want me to|would you like me to|shall I|should I|I can )\s*'
            r'(run|execute|restart|start|launch|do|try|fix|update|install|create|set up)',
            text, re.IGNORECASE
        ):
            return "Detected offered action with code block — should just do it or use AskUserQuestion"

    # Pattern 9: "manually" in instructional context
    if re.search(
        r'(you.{0,20}manual(ly)?|manual(ly)? (run|execute|add|edit|update|configure|restart|start|set))',
        text, re.IGNORECASE
    ):
        return "Detected instruction for manual action"

    # Pattern 10: "add this/the following to your"
    if re.search(r'add (this|the following|these) to (your|the) ', text, re.IGNORECASE):
        return "Detected instruction to manually add content"

    return None


async def handle_stop_validate(payload: dict) -> dict:
    """
    Synchronous stop validator — blocks the agent's stop if it's instructing the user
    to perform unverified manual actions instead of doing them autonomously.

    Returns {"decision": "block", "reason": "..."} to block, or {} to allow.
    Called by stop-validator.sh (thin shim).
    """
    session_id = payload.get("session_id", "")
    pid = payload.get("pid")
    log_prefix = f"StopValidate {session_id[:12]}..."

    # ── Subagent detection: this instance IS a subagent ──
    # Process tree (direct claude→claude spawn) or TOKEN_API_SUBAGENT env var.
    if pid and is_subagent_pid(pid):
        logger.info(f"{log_prefix} ALLOW: is subagent (parent PID {get_parent_pid(pid)} is claude)")
        return {}

    token_api_subagent = payload.get("env", {}).get("TOKEN_API_SUBAGENT", "")
    if token_api_subagent:
        logger.info(f"{log_prefix} ALLOW: TOKEN_API_SUBAGENT={token_api_subagent}")
        return {}

    # Intermediate stop: background subagents still pending for this session.
    if _pending_background_tasks.get(session_id, 0) > 0:
        logger.info(f"{log_prefix} ALLOW: intermediate stop ({_pending_background_tasks[session_id]} background tasks pending)")
        return {}

    # ── Escape hatch: second attempt is always allowed ──
    if payload.get("stop_hook_active"):
        logger.info(f"{log_prefix} ALLOW: stop_hook_active")
        return {}

    # ── Extract last assistant turn ──
    # Prefer embedded tail (sent by shim when transcript is on a remote machine),
    # fall back to direct file read if local.
    transcript_tail = payload.get("transcript_tail")
    transcript_path = payload.get("transcript_path")
    if transcript_tail:
        turn = _parse_assistant_turn_from_lines(transcript_tail.splitlines())
    elif transcript_path and os.path.exists(transcript_path):
        turn = _extract_last_assistant_turn(transcript_path)
    else:
        logger.info(f"{log_prefix} ALLOW: no transcript")
        return {}

    if not turn:
        logger.info(f"{log_prefix} ALLOW: no assistant turn found")
        return {}

    # ── Short-circuit allows ──
    if "AskUserQuestion" in turn["tool_names"] or "EnterPlanMode" in turn["tool_names"]:
        logger.info(f"{log_prefix} ALLOW: last turn uses {turn['tool_names']}")
        return {}

    if not turn["text"]:
        logger.info(f"{log_prefix} ALLOW: no text content")
        return {}

    if turn["last_block_type"] == "tool_use":
        logger.info(f"{log_prefix} ALLOW: last block is tool_use")
        return {}

    # ── Pattern detection ──
    reason = _check_stop_patterns(turn["text"])
    if reason:
        logger.info(f"{log_prefix} BLOCK: {reason}")
        full_reason = (
            f"{reason}. Rules: (1) Do not end by telling the user to execute commands or perform "
            "manual actions — verify autonomously using your tools instead. (2) If verification "
            "requires a tool you don't have, use the Task tool with subagent_type=tool-creator to "
            "create one. (3) If you genuinely cannot verify or act autonomously, use AskUserQuestion "
            "to present options rather than ending with unverified instructions. "
            "(4) If you believe this block is incorrect and have completed your work, you may stop "
            "on the next attempt (stop_hook_active will be true)."
        )
        return {"decision": "block", "reason": full_reason}

    logger.info(f"{log_prefix} ALLOW: no patterns detected")
    return {}


async def handle_stop(payload: dict) -> dict:
    """Handle Stop hook - response completed, trigger TTS/notifications."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Prevent infinite loops
    if payload.get("stop_hook_active"):
        return {"success": True, "action": "skipped_recursive"}

    # Get instance info
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        instance = await cursor.fetchone()

    if not instance:
        return {"success": False, "action": "instance_not_found"}

    instance = dict(instance)
    device_id = instance.get("device_id", "Mac-Mini")
    tab_name = instance.get("tab_name", "Claude")
    tts_voice = instance.get("tts_voice", "Microsoft David")
    notification_sound = instance.get("notification_sound", "chimes.wav")

    # Mark as no longer processing
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE claude_instances SET status = 'idle', last_activity = ? WHERE id = ?",
            (now, session_id)
        )
        await db.commit()

    # Fire session doc swarm if instance has a linked doc
    session_doc_id = instance.get("session_doc_id")
    is_subagent_instance_quick = bool(instance.get("is_subagent"))
    if session_doc_id and not is_subagent_instance_quick:
        stop_context = payload.get("transcript_tail", "")[:2000] if payload.get("transcript_tail") else ""
        asyncio.create_task(fire_session_doc_swarm(
            session_doc_id, tab_name, context=stop_context
        ))

    result = {
        "success": True,
        "action": "stop_processed",
        "instance_id": session_id,
        "device_id": device_id
    }

    # ── Subagent detection: skip all notifications for subagents ──
    # DB flag covers subagent-CLI spawned instances; PID check covers Task tool subagents.
    pid = payload.get("pid")
    is_subagent_instance = bool(instance.get("is_subagent")) or bool(pid and is_subagent_pid(pid))
    if is_subagent_instance:
        result["action"] = "stop_processed_subagent"
        logger.info(f"Hook: Stop {session_id[:12]}... subagent — state updated, skipping notifications")
        return result

    # Intermediate stop: background subagents still pending. Update state but skip notifications.
    if _pending_background_tasks.get(session_id, 0) > 0:
        result["action"] = "stop_processed_intermediate"
        logger.info(f"Hook: Stop {session_id[:12]}... intermediate ({_pending_background_tasks[session_id]} background tasks pending) — skipping notifications")
        return result

    # Mobile path: send webhook notification
    if device_id == "Token-S24":
        webhook_result = send_webhook(
            "http://100.102.92.24:7777/notify",
            f"[{tab_name}] Claude finished"
        )
        result["notification"] = webhook_result
        logger.info(f"Hook: Stop {session_id[:12]}... -> mobile notification")
        return result

    # Desktop path: TTS and notification
    # Extract TTS text from transcript (prefer embedded tail for remote access,
    # fall back to direct file read if local)
    transcript_tail = payload.get("transcript_tail")
    transcript_path = payload.get("transcript_path")
    tts_text = None

    # Determine lines to parse: embedded tail (from hook shim) or direct file read
    transcript_lines = None
    if transcript_tail:
        transcript_lines = transcript_tail.splitlines()
    elif transcript_path and os.path.exists(transcript_path):
        try:
            with open(transcript_path, 'r') as f:
                transcript_lines = f.readlines()
        except Exception as e:
            logger.warning(f"Failed to read transcript: {e}")

    if transcript_lines:
        for line in reversed(transcript_lines):
            if '"role":"assistant"' in line:
                try:
                    data = json.loads(line)
                    content = data.get("message", {}).get("content")
                    if isinstance(content, str):
                        tts_text = content
                    elif isinstance(content, list):
                        # Extract text from content array
                        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                        tts_text = "\n".join(texts)
                    elif isinstance(content, dict) and "text" in content:
                        tts_text = content["text"]
                    break
                except json.JSONDecodeError:
                    continue

    # Check TTS config
    tts_config_file = Path.home() / ".claude" / ".tts-config.json"
    tts_enabled = True

    if tts_config_file.exists():
        try:
            with open(tts_config_file) as f:
                config = json.load(f)
                tts_enabled = config.get("enabled", True)
        except Exception:
            pass

    # Sanitize TTS text (remove markdown formatting and normalize whitespace)
    if tts_text:
        # Strip markdown headers (must be before newline conversion)
        tts_text = re.sub(r'^#{1,6}\s*', '', tts_text, flags=re.MULTILINE)
        # Strip markdown bold/italic
        tts_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', tts_text)  # **bold**
        tts_text = re.sub(r'\*([^*]+)\*', r'\1', tts_text)      # *italic*
        tts_text = re.sub(r'__([^_]+)__', r'\1', tts_text)      # __bold__
        tts_text = re.sub(r'_([^_]+)_', r'\1', tts_text)        # _italic_
        # Strip inline code
        tts_text = re.sub(r'`([^`]+)`', r'\1', tts_text)
        # Strip code blocks
        tts_text = re.sub(r'```[\s\S]*?```', '', tts_text)
        # Strip bullet points and list markers
        tts_text = re.sub(r'^[\s]*[-*+]\s+', '', tts_text, flags=re.MULTILINE)
        tts_text = re.sub(r'^[\s]*\d+\.\s+', '', tts_text, flags=re.MULTILINE)
        # Convert newlines to spaces
        tts_text = tts_text.replace('\n', ' ')
        # Normalize multiple spaces
        tts_text = re.sub(r' +', ' ', tts_text)
        tts_text = tts_text.strip()

    # Queue TTS if enabled and we have text
    if tts_enabled and tts_text:
        logger.info(f"Hook: Stop queuing TTS, {len(tts_text)} chars: {tts_text[:80]}...")
        tts_result = await queue_tts(session_id, tts_text)
        logger.info(f"Hook: Stop queue_tts result: {json.dumps(tts_result)}")
        result["tts"] = tts_result
    else:
        # Just play notification sound without TTS
        logger.info(f"Hook: Stop no TTS text (tts_enabled={tts_enabled}, has_text={bool(tts_text)})")
        play_sound(notification_sound)
        result["sound"] = {"played": notification_sound}

    # Pavlok vibe notification (skip for subagents)
    if not instance.get("is_subagent"):
        vibe_result = send_pavlok_stimulus(
            stimulus_type="vibe",
            value=30,
            reason="claude_finished",
            respect_cooldown=False,
        )
        result["pavlok_vibe"] = vibe_result

    logger.info(f"Hook: Stop {session_id[:12]}... -> desktop notification")
    await log_event("hook_stop", instance_id=session_id, details={"tts_enabled": tts_enabled, "tts_length": len(tts_text) if tts_text else 0})

    return result


async def handle_pre_tool_use(payload: dict) -> dict:
    """Handle PreToolUse hook - marks processing, can block operations like 'make deploy'."""
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    # Mark instance as processing (catches cases where prompt_submit was missed)
    # Also resurrect stopped instances - activity means they're active
    if session_id:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE claude_instances
                   SET status = 'processing', last_activity = ?, stopped_at = NULL
                   WHERE id = ?""",
                (now, session_id)
            )
            await db.commit()

    # Track background Task subagents so Stop hooks can detect intermediate vs final stops.
    if tool_name == "Task" and tool_input.get("run_in_background"):
        _pending_background_tasks[session_id] = _pending_background_tasks.get(session_id, 0) + 1
        logger.info(f"PreToolUse: Task background launched for {session_id[:12]} (pending: {_pending_background_tasks[session_id]})")
        return {"success": True, "action": "allowed"}

    # Voice chat: when AskUserQuestion fires for a voice-chat-active instance,
    # 1) TTS the question text so the user hears it spoken
    # 2) trigger AHK to auto-select "Other" and start dictation
    if tool_name == "AskUserQuestion" and session_id and session_id in VOICE_CHAT_SESSIONS:
        # Extract and speak question text
        questions = tool_input.get("questions", [])
        if questions:
            tts_parts = []
            for q in questions:
                question_text = q.get("question", "")
                if question_text:
                    tts_parts.append(question_text)
            if tts_parts:
                tts_message = " ".join(tts_parts)
                try:
                    await queue_tts(session_id, tts_message)
                    logger.info(f"PreToolUse: Voice chat TTS queued for {session_id[:12]}: {tts_message[:80]}")
                except Exception as e:
                    logger.warning(f"PreToolUse: Voice chat TTS failed for {session_id[:12]}: {e}")

        # Return local_exec so generic-hook.sh runs AHK on WSL (which can invoke Windows AHK)
        # Note: AHK.exe needs a Windows path, so use wslpath -w to convert the WSL path
        listening_arg = "1" if VOICE_CHAT_SESSIONS.get(session_id, {}).get("listening", True) else "0"
        logger.info(f"PreToolUse: Voice chat local_exec for {session_id[:12]} (listening={listening_arg})")
        return {
            "success": True,
            "action": "allowed",
            "local_exec": f'"/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe" "$(wslpath -w "$HOME/Scripts/ahk/voice-select-other.ahk")" "{session_id}" "{listening_arg}"',
        }

    # Only check Bash commands for blocking
    if tool_name != "Bash":
        return {"success": True, "action": "allowed"}

    command = tool_input.get("command", "")

    # Block 'make deploy' commands
    if "make deploy" in command or command.strip() == "make deploy":
        # Build alternative command suggestion
        deploy_args = []
        if "ENVIRONMENT=production" in command:
            deploy_args.append("production")
        if "--blocking" in command:
            deploy_args.append("--blocking")

        alt_command = "deploy"
        if deploy_args:
            alt_command += " " + " ".join(deploy_args)

        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"'make deploy' is disabled. Use autonomous deployment instead:\n\n"
                f"  {alt_command}\n\n"
                f"This provides better error detection and log monitoring."
            )
        }

    return {"success": True, "action": "allowed"}


async def handle_notification(payload: dict) -> dict:
    """Handle Notification hook - play notification sound."""
    session_id = payload.get("session_id")

    # Get instance profile for sound selection
    sound_file = "chimes.wav"  # default

    if session_id:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT notification_sound FROM claude_instances WHERE id = ?",
                (session_id,)
            )
            row = await cursor.fetchone()
            if row and row["notification_sound"]:
                sound_file = row["notification_sound"]

    result = play_sound(sound_file)
    return {"success": True, "action": "sound_played", "sound": sound_file, "result": result}


# Hook dispatcher endpoint
@app.post("/api/hooks/{action_type}")
async def dispatch_hook(action_type: str, payload: dict) -> dict:
    """
    Unified hook dispatcher for Claude Code hooks.

    Receives hook events from generic-hook.sh and routes to appropriate handler.
    Always returns a response - errors are logged but don't cause failures.
    """
    handlers = {
        "SessionStart": handle_session_start,
        "SessionEnd": handle_session_end,
        "UserPromptSubmit": handle_prompt_submit,
        "PostToolUse": handle_post_tool_use,
        "Stop": handle_stop,
        "StopValidate": handle_stop_validate,
        "PreToolUse": handle_pre_tool_use,
        "Notification": handle_notification,
    }

    handler = handlers.get(action_type)
    if not handler:
        logger.warning(f"Hook: Unknown action type: {action_type}")
        return {"success": False, "action": "unknown_hook_type", "type": action_type}

    try:
        result = await handler(payload)
        return result
    except Exception as e:
        logger.error(f"Hook handler error ({action_type}): {e}")
        await log_event("hook_error", details={"action_type": action_type, "error": str(e)})
        return {"success": False, "action": "handler_error", "error": str(e)}


# ============ Stash: Cross-Machine Clipboard & File Sharing ============

import mimetypes

def stash_cleanup():
    """Delete stash items older than STASH_MAX_AGE_HOURS."""
    if not STASH_DIR.exists():
        return
    cutoff = time.time() - (STASH_MAX_AGE_HOURS * 3600)
    removed = 0
    for f in STASH_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        print(f"STASH: Cleaned up {removed} expired item(s)")


@app.get("/api/stash")
async def stash_list():
    """List all stash items."""
    STASH_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    now = time.time()
    for f in sorted(STASH_DIR.iterdir()):
        if not f.is_file():
            continue
        stat = f.stat()
        age_secs = now - stat.st_mtime
        items.append({
            "name": f.name,
            "size": stat.st_size,
            "age_seconds": int(age_secs),
            "age_human": f"{int(age_secs // 3600)}h{int((age_secs % 3600) // 60)}m" if age_secs >= 3600 else f"{int(age_secs // 60)}m",
        })
    return {"items": items, "count": len(items)}


@app.get("/api/stash/{name:path}")
async def stash_get(name: str):
    """Get a stash item. Returns JSON for text, file download for binary."""
    path = STASH_DIR / name
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Stash item '{name}' not found")
    # Safety: ensure path is within STASH_DIR
    if not path.resolve().is_relative_to(STASH_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid stash name")
    mime, _ = mimetypes.guess_type(name)
    # If it looks like text, return as JSON
    try:
        content = path.read_text(encoding="utf-8")
        return {"name": name, "content": content, "size": len(content)}
    except (UnicodeDecodeError, ValueError):
        return FileResponse(path, filename=name, media_type=mime or "application/octet-stream")


@app.put("/api/stash/{name:path}")
async def stash_put(name: str, body: StashContentRequest):
    """Set a stash item from JSON body {"content": "..."}."""
    STASH_DIR.mkdir(parents=True, exist_ok=True)
    path = STASH_DIR / name
    if not path.resolve().is_relative_to(STASH_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid stash name")
    path.write_text(body.content, encoding="utf-8")
    return {"status": "stored", "name": name, "size": len(body.content), "type": "text"}


@app.post("/api/stash/{name:path}/upload")
async def stash_upload(name: str, file: UploadFile = File(...)):
    """Upload a file to stash."""
    STASH_DIR.mkdir(parents=True, exist_ok=True)
    path = STASH_DIR / name
    if not path.resolve().is_relative_to(STASH_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid stash name")
    data = await file.read()
    path.write_bytes(data)
    return {"status": "stored", "name": name, "size": len(data), "type": "file"}


@app.delete("/api/stash/{name:path}")
async def stash_delete(name: str):
    """Delete a stash item."""
    path = STASH_DIR / name
    if not path.resolve().is_relative_to(STASH_DIR.resolve()):
        raise HTTPException(status_code=400, detail="Invalid stash name")
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Stash item '{name}' not found")
    path.unlink()
    return {"status": "deleted", "name": name}


@app.put("/api/stash")
async def stash_put_clipboard(body: StashContentRequest):
    """Shorthand: set the _clipboard item."""
    return await stash_put("_clipboard", body)


@app.delete("/api/stash")
async def stash_clear_all():
    """Clear all stash items."""
    if not STASH_DIR.exists():
        return {"status": "cleared", "removed": 0}
    removed = 0
    for f in STASH_DIR.iterdir():
        if f.is_file():
            f.unlink()
            removed += 1
    return {"status": "cleared", "removed": removed}


# ============ Discord Endpoints ============


@app.post("/api/discord/message")
async def receive_discord_message(request: DiscordMessageRequest):
    """Receive a forwarded Discord message from the discord-cli daemon."""
    author_name = request.author.get("username", "unknown") if request.author else "unknown"
    author_id = request.author.get("id") if request.author else None

    await log_event("discord_message", device_id="discord", details={
        "channel_id": request.channel_id,
        "channel_name": request.channel_name,
        "author_id": author_id,
        "author_name": author_name,
        "content": request.content[:500],
        "message_id": request.message_id,
        "timestamp": request.timestamp,
        "is_dm": request.is_dm,
        "is_reply": request.is_reply,
    })

    logger.info(f"Discord [{request.channel_name or request.channel_id}] {author_name}: {request.content[:80]}")

    # --- Discord response routing ---
    # Never respond to bots (loop prevention)
    if (request.author or {}).get("bot"):
        return {"received": True, "message_id": request.message_id}

    content = request.content or ""

    # Trigger 0: bare URL in #forge → auto-clip to vault
    stripped = content.strip()
    if request.channel_name == "forge" and stripped.startswith("http") and " " not in stripped:
        asyncio.create_task(_discord_clip(stripped, request))
        return {"received": True, "message_id": request.message_id}

    # Trigger 1: @Mechanicus mention (user mention <@ID> or role mention <@&ID>)
    if f"<@{MECHANICUS_USER_ID}>" in content or f"<@&{MECHANICUS_ROLE_ID}>" in content:
        asyncio.create_task(_discord_respond(request, bot="mechanicus"))
        return {"received": True, "message_id": request.message_id}

    # Trigger 1.5: @Custodes mention in any channel
    if f"<@{CUSTODES_USER_ID}>" in content:
        asyncio.create_task(_discord_respond(request, bot="custodes"))
        return {"received": True, "message_id": request.message_id}

    # Trigger 2: Reply in a Custodes-owned channel
    if request.is_reply and request.channel_name in CUSTODES_CHANNELS:
        asyncio.create_task(_discord_respond(request, bot="custodes"))

    return {"received": True, "message_id": request.message_id}


async def _discord_clip(url: str, message: DiscordMessageRequest):
    """Run clip CLI on a bare URL dropped in #forge, reply with the vault path."""
    channel = message.channel_name or "forge"
    reply_to = message.message_id or ""
    logger.info(f"Discord clip: {url}")

    clip_bin = Path.home() / "Scripts" / "cli-tools" / "bin" / "clip"
    env = {
        **os.environ,
        "PATH": ":".join([
            str(Path.home() / "Scripts" / "cli-tools" / "bin"),
            str(Path.home() / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.environ.get("PATH", ""),
        ]),
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            str(clip_bin), url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        stderr_text = stderr.decode()

        if proc.returncode != 0:
            logger.warning(f"Discord clip failed (rc={proc.returncode}): {stderr_text[:300]}")
            reply_content = f"Clip failed for <{url}>: `{stderr_text.strip()[-200:]}`"
        else:
            # Parse "Saved to Imperium-ENV: Terra/Inbox/slug.md" and "Title: ..." from stderr
            saved_path = None
            note_title = None
            for line in stderr_text.splitlines():
                if line.startswith("Saved to "):
                    parts = line.split(": ", 1)
                    if len(parts) == 2:
                        saved_path = parts[1].strip()
                elif line.startswith("Title: "):
                    note_title = line[7:].strip()
            if note_title and saved_path:
                reply_content = f"Clipped **{note_title}** → `{saved_path}`"
            elif saved_path:
                reply_content = f"Clipped: `{saved_path}`"
            else:
                reply_content = f"Clipped: `{url}`"
            logger.info(f"Discord clip saved: {saved_path} ({note_title})")

    except asyncio.TimeoutError:
        logger.warning(f"Discord clip timed out: {url}")
        reply_content = f"Clip timed out for <{url}>"
    except Exception as e:
        logger.warning(f"Discord clip error: {e}")
        return

    # Send reply via daemon
    try:
        import urllib.request as _urllib_req
        payload = json.dumps({
            "channel": channel,
            "bot": "mechanicus",
            "content": reply_content,
            "reply_to": reply_to,
        }).encode()
        req = _urllib_req.Request(
            f"{DISCORD_DAEMON_URL}/send",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: _urllib_req.urlopen(req, timeout=10)
        )
    except Exception as e:
        logger.warning(f"Discord clip: failed to send reply: {e}")


# Per-channel cooldown: max 1 response per 30 seconds per channel
_discord_respond_cooldowns: dict[str, float] = {}

async def _discord_respond(message: DiscordMessageRequest, bot: str):
    """Fetch channel context, build system prompt, spawn responder subprocess."""
    channel = message.channel_name or message.channel_id

    # Cooldown: 1 response per channel per 30s
    now = time.time()
    last = _discord_respond_cooldowns.get(channel, 0)
    if now - last < 30:
        logger.info(f"Discord responder: cooldown active for #{channel}, skipping")
        return
    _discord_respond_cooldowns[channel] = now

    persona = "Fabricator General (Adeptus Mechanicus)" if bot == "mechanicus" else "Adeptus Custodes"

    # Fetch recent channel context from daemon (sync urllib in executor to stay async)
    context_str = ""
    try:
        def _fetch_context():
            import urllib.request
            req = urllib.request.Request(
                f"{DISCORD_DAEMON_URL}/read?channel={channel}&limit=10",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        data = await asyncio.get_event_loop().run_in_executor(None, _fetch_context)
        msgs = data.get("messages", [])
        context_str = "\n".join(
            f"[{m['author'].get('displayName', m['author'].get('username', '?'))}]: {m['content']}"
            for m in msgs
        )
    except Exception as e:
        logger.warning(f"Discord responder: could not fetch context: {e}")

    author_display = (message.author or {}).get("displayName",
                      (message.author or {}).get("username", "user"))

    system_prompt = f"""You are {persona}, responding to a Discord message in #{channel}.

Recent conversation (oldest to newest):
{context_str or '(no prior context)'}

You are replying directly to:
[{author_display}]: {message.content}

Rules:
- Be concise. Discord markdown is supported.
- Stay in character.
- Do not start with a greeting or preamble.
- One reply only."""

    # Write system prompt to temp file (avoids shell escaping issues)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write(system_prompt)
        prompt_file = f.name

    responder = Path(__file__).parent / "discord_responder.py"
    env = {
        **os.environ,
        "CLAUDECODE": "",
        "TOKEN_API_SUBAGENT": f"discord_responder:{bot}",
        "PATH": ":".join([
            str(Path.home() / "Scripts" / "cli-tools" / "bin"),
            str(Path.home() / ".local" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
            os.environ.get("PATH", ""),
        ]),
    }

    proc = await asyncio.create_subprocess_exec(
        "python3", str(responder),
        channel,
        message.message_id or "",
        bot,
        prompt_file,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    logger.info(f"Discord responder spawned: bot={bot} channel=#{channel} pid={proc.pid}")

    # Fire-and-forget but log errors
    async def _wait_and_log():
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"Discord responder exited {proc.returncode}: {stderr.decode()[:300]}")
    asyncio.create_task(_wait_and_log())


# ============ Aspirant Pipeline (Inbox) ============


@app.post("/api/inbox/notify")
async def inbox_notify(request: InboxNotifyRequest):
    """Gene-seed: receive birth notification for a new inbox note."""
    await log_event("inbox_notify", device_id="obsidian", details={
        "path": request.path,
        "title": request.title,
        "type": request.type,
        "source": request.source,
    })
    logger.info(f"Inbox: new {request.type} note '{request.title}' from {request.source}")
    # Future: dispatch MiniMax fleet (Stage 2: Implantation)
    return {"received": True, "path": request.path, "type": request.type}


@app.post("/api/inbox/create")
async def inbox_create(request: InboxCreateRequest):
    """Create a new aspirant note in Terra/Inbox/ from an external source."""
    # Sanitize title for filename
    safe_title = re.sub(r'[^\w\s-]', '', request.title).strip()
    safe_title = re.sub(r'\s+', ' ', safe_title)
    if not safe_title:
        safe_title = f"Untitled {datetime.now().strftime('%Y%m%d-%H%M%S')}"

    filename = f"{safe_title}.md"
    filepath = OBSIDIAN_INBOX_PATH / filename

    if filepath.exists():
        raise HTTPException(status_code=409, detail=f"Note already exists: {filename}")

    is_prescriptive = request.type == "prescriptive"
    created_date = datetime.now().strftime("%Y-%m-%d")

    frontmatter_lines = [
        "---",
        f'title: "{safe_title}"',
        f"type: {request.type}",
        f"prescriptive: {str(is_prescriptive).lower()}",
        f"created: {created_date}",
        "status: inbox",
        "tags:",
        f"  - type/{request.type}",
        "  - inbox/aspirant",
        f"source: {request.source}",
    ]
    if is_prescriptive:
        frontmatter_lines += ["progress: 0", "completed: false"]
    frontmatter_lines.append("---")

    body = request.content or ""
    if request.author:
        body = f"*Captured from {request.author} via {request.source}*\n\n{body}"

    content = "\n".join(frontmatter_lines) + "\n\n" + body + "\n"
    filepath.write_text(content, encoding="utf-8")

    # Self-notify (same pipeline as Templater-created notes)
    await inbox_notify(InboxNotifyRequest(
        path=f"Terra/Inbox/{filename}",
        title=safe_title,
        type=request.type,
        source=request.source,
    ))

    note_path = f"Terra/Inbox/{filename}"
    obsidian_uri = f"obsidian://open?vault=Imperium-ENV&file={note_path.replace(' ', '%20')}"

    logger.info(f"Inbox: created '{filename}' from {request.source}")
    return {"created": True, "path": note_path, "title": safe_title, "obsidian_uri": obsidian_uri}


# ============ Session Document Support ============

class MiniMaxRateLimiter:
    """Sliding window rate limiter for MiniMax API calls."""
    def __init__(self, max_calls: int = 300, window_seconds: int = 18000):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.calls: deque = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.time()
            while self.calls and self.calls[0] < now - self.window_seconds:
                self.calls.popleft()
            if len(self.calls) >= int(self.max_calls * 0.8):
                return False
            self.calls.append(now)
            return True

    @property
    def remaining(self) -> int:
        now = time.time()
        while self.calls and self.calls[0] < now - self.window_seconds:
            self.calls.popleft()
        return max(0, self.max_calls - len(self.calls))

minimax_limiter = MiniMaxRateLimiter()

# ---- Minimax API Client ----
_MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
_MINIMAX_MODEL = "MiniMax-M2.5"
_MINIMAX_AUTH_PROFILES = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"


def _get_minimax_key() -> str:
    """Read MiniMax API key from auth-profiles."""
    try:
        profiles = json.loads(_MINIMAX_AUTH_PROFILES.read_text())
        return profiles["profiles"]["minimax:default"]["key"]
    except Exception as e:
        raise RuntimeError(f"Could not load MiniMax API key: {e}")


async def minimax_chat(system_prompt: str, user_content: str, max_tokens: int = 1024) -> str:
    """Send a chat message to Minimax and return the text response."""
    if not await minimax_limiter.acquire():
        logger.warning(f"MiniMax rate limited ({minimax_limiter.remaining} remaining)")
        return ""
    key = _get_minimax_key()
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{_MINIMAX_BASE_URL}/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _MINIMAX_MODEL,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_content}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(
            block["text"] for block in data.get("content", [])
            if block.get("type") == "text"
        )


# ---- Session Document Swarm ----
SESSION_SWARM_ROLES = {
    "activity_scribe": {
        "system": "You are an Activity Scribe. Given an agent's recent output, write a concise activity log entry. Format: ### YYYY-MM-DD HH:MM — <agent_name>\n<2-3 sentences of what was done>. Include specific file names, decisions made, and outcomes. Be factual, not flowery.",
        "max_tokens": 512,
    },
    "plan_auditor": {
        "system": "You are a Plan Auditor. Given a session document and recent activity, identify if any part of the Plan section needs updating based on what just happened. If no updates needed, respond with exactly: NO_UPDATE. Otherwise, describe the specific plan changes needed in 2-3 sentences.",
        "max_tokens": 512,
    },
}


async def fire_session_doc_swarm(session_doc_id: int, instance_tab_name: str, context: str = "") -> None:
    """Fire Minimax agents to update session doc after a stop event."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (session_doc_id,))
            row = await cursor.fetchone()
            if not row:
                return
        fp = Path(row[0])
        if not fp.exists():
            return
        doc_content = fp.read_text()

        # Activity Scribe — summarize what happened
        scribe_config = SESSION_SWARM_ROLES["activity_scribe"]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        scribe_prompt = f"""Session document:
{doc_content[:2000]}

Recent agent activity context:
{context[:2000]}

Agent name: {instance_tab_name}
Current time: {now}

Write the activity log entry."""

        scribe_result = await minimax_chat(scribe_config["system"], scribe_prompt, scribe_config["max_tokens"])

        if scribe_result.strip():
            await merge_into_session_doc(
                session_doc_id,
                SessionDocMergeRequest(content=scribe_result, source="minimax", context="Activity scribe update")
            )

        # Plan Auditor — check if plan needs updating
        auditor_config = SESSION_SWARM_ROLES["plan_auditor"]
        auditor_prompt = f"""Session document:
{doc_content[:2000]}

Recent activity just logged:
{scribe_result[:500]}

Does the Plan section need any updates based on this activity?"""

        auditor_result = await minimax_chat(auditor_config["system"], auditor_prompt, auditor_config["max_tokens"])

        if auditor_result.strip() and "NO_UPDATE" not in auditor_result:
            await merge_into_session_doc(
                session_doc_id,
                SessionDocMergeRequest(content=f"Plan audit note: {auditor_result}", source="minimax", context="Plan auditor finding")
            )

        logger.info(f"Session swarm completed for doc {session_doc_id}")

    except Exception as e:
        logger.error(f"Session swarm failed for doc {session_doc_id}: {e}")


# Valid session doc status transitions (from → set of valid targets)
# Any status can transition to 'archived' as an escape hatch
VALID_STATUS_TRANSITIONS = {
    "active": {"completed", "archived"},
    "completed": {"deployment", "active", "archived"},
    "deployment": {"processed", "archived"},
    "processed": {"archived"},
    "archived": set(),  # terminal
}

async def get_primarch_from_db(db, name: str) -> Optional[dict]:
    """Get a single primarch from the DB by name or alias."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM primarchs WHERE name = ?", (name,))
    row = await cursor.fetchone()
    if row:
        result = dict(row)
        result["aliases"] = json.loads(result["aliases"])
        db.row_factory = None
        return result
    # Check aliases
    cursor = await db.execute("SELECT * FROM primarchs")
    rows = await cursor.fetchall()
    db.row_factory = None
    for r in rows:
        r = dict(r)
        aliases = json.loads(r["aliases"])
        if name in aliases:
            r["aliases"] = aliases
            return r
    return None


async def get_all_primarchs_from_db(db) -> list:
    """Get all primarchs from the DB."""
    db.row_factory = aiosqlite.Row
    cursor = await db.execute("SELECT * FROM primarchs ORDER BY name")
    rows = await cursor.fetchall()
    db.row_factory = None
    result = []
    for r in rows:
        r = dict(r)
        r["aliases"] = json.loads(r["aliases"])
        result.append(r)
    return result


def create_session_doc_file(file_path: Path, title: str, doc_id: int, project: str = None, primarch_name: str = None) -> None:
    """Create the markdown file for a session document."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    project_line = f"\nproject: {project}" if project else ""
    primarch_line = f"\nprimarch: {primarch_name}" if primarch_name else ""
    content = f"""---
session_doc_id: {doc_id}
created: {today}{project_line}
agents: []
instance_ids: []{primarch_line}
status: active
---

# Session: {title}

## Plan

_No plan defined yet._

## Activity Log

"""
    file_path.write_text(content)


async def _update_doc_agents_list(db, doc_id: int) -> None:
    """Update the agents list, instance_ids, and primarch in a session doc's YAML frontmatter."""
    cursor = await db.execute(
        "SELECT id, tab_name FROM claude_instances WHERE session_doc_id = ? AND status IN ('processing', 'idle')",
        (doc_id,)
    )
    rows = await cursor.fetchall()
    agents = [r[1] for r in rows if r[1]]
    instance_ids = [r[0] for r in rows if r[0]]

    cursor = await db.execute("SELECT file_path, primarch_name FROM session_documents WHERE id = ?", (doc_id,))
    doc_row = await cursor.fetchone()
    if not doc_row:
        return

    fp = Path(doc_row[0])
    if not fp.exists():
        return

    primarch_name = doc_row[1]

    content = fp.read_text()
    # Update agents list
    content = re.sub(
        r'^agents:.*$',
        f'agents: [{", ".join(agents)}]',
        content,
        count=1,
        flags=re.MULTILINE
    )
    # Update instance_ids
    ids_str = ", ".join(instance_ids)
    if re.search(r'^instance_ids:.*$', content, re.MULTILINE):
        content = re.sub(
            r'^instance_ids:.*$',
            f'instance_ids: [{ids_str}]',
            content,
            count=1,
            flags=re.MULTILINE
        )
    else:
        # Insert after agents line
        content = re.sub(
            r'^(agents:.*$)',
            f'\\1\ninstance_ids: [{ids_str}]',
            content,
            count=1,
            flags=re.MULTILINE
        )
    # Update primarch
    if primarch_name:
        if re.search(r'^primarch:.*$', content, re.MULTILINE):
            content = re.sub(
                r'^primarch:.*$',
                f'primarch: {primarch_name}',
                content,
                count=1,
                flags=re.MULTILINE
            )
        else:
            # Insert after instance_ids line
            content = re.sub(
                r'^(instance_ids:.*$)',
                f'\\1\nprimarch: {primarch_name}',
                content,
                count=1,
                flags=re.MULTILINE
            )
    else:
        # Remove primarch line if no primarch
        content = re.sub(r'^primarch:.*\n', '', content, count=1, flags=re.MULTILINE)

    fp.write_text(content)


async def _handle_orphan_doc(doc_id: int) -> None:
    """Handle cleanup when a doc loses all linked instances.

    Lifecycle-aware:
    - active + empty → delete
    - active + has content → completed (enters deployment pipeline)
    - completed / deployment → leave alone (Administratum handles)
    - processed → archive
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE session_doc_id = ?",
            (doc_id,)
        )
        count = (await cursor.fetchone())[0]
        if count > 0:
            return

        cursor = await db.execute("SELECT file_path, title, status FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            return

        fp = Path(row[0])
        status = row[2]
        now = datetime.now().isoformat()

        # completed / deployment docs are in the pipeline — don't touch them
        if status in ("completed", "deployment"):
            logger.info(f"Orphan cleanup: doc {doc_id} ({row[1]}) is {status}, leaving for Administratum")
            return

        # processed docs can be archived
        if status == "processed":
            await db.execute(
                "UPDATE session_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                (now, doc_id)
            )
            await db.commit()
            logger.info(f"Orphan cleanup: archived processed session doc {doc_id} ({row[1]})")
            return

        # active docs: check if empty
        if not fp.exists():
            return

        content = fp.read_text()
        if "_No plan defined yet._" in content and "## Activity Log\n\n" in content.rstrip():
            fp.unlink()
            await db.execute("DELETE FROM session_documents WHERE id = ?", (doc_id,))
            await db.commit()
            logger.info(f"Orphan cleanup: deleted unedited session doc {doc_id} ({row[1]})")
        else:
            # Has content — transition to completed (enters deployment pipeline)
            await db.execute(
                "UPDATE session_documents SET status = 'completed', updated_at = ? WHERE id = ?",
                (now, doc_id)
            )
            await db.commit()
            logger.info(f"Orphan cleanup: completed edited session doc {doc_id} ({row[1]}) — ready for deployment")


# ============ Session Document Endpoints ============

@app.post("/api/session-docs")
async def create_session_doc(request: SessionDocCreateRequest):
    """Create a new session document."""
    today = datetime.now().strftime("%Y-%m-%d")
    slug = request.title.lower().replace(" ", "-")[:50]

    if request.file_path:
        fp = Path(request.file_path)
    else:
        fp = DEFAULT_SESSIONS_DIR / f"{today}-{slug}.md"

    if fp.exists():
        raise HTTPException(status_code=409, detail=f"File already exists: {fp}")

    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO session_documents (title, file_path, project, primarch_name, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (request.title, str(fp), request.project, request.primarch_name, now, now)
        )
        doc_id = cursor.lastrowid

        # Auto-link primarch if specified
        if request.primarch_name:
            # Unlink any existing active doc for this primarch
            await db.execute(
                "UPDATE primarch_session_docs SET unlinked_at = ? WHERE primarch_name = ? AND unlinked_at IS NULL",
                (now, request.primarch_name)
            )
            await db.execute(
                "INSERT INTO primarch_session_docs (primarch_name, session_doc_id, linked_at) VALUES (?, ?, ?)",
                (request.primarch_name, doc_id, now)
            )

        await db.commit()

    create_session_doc_file(fp, request.title, doc_id, request.project, request.primarch_name)

    await log_event("session_doc_created", details={
        "doc_id": doc_id, "title": request.title, "file_path": str(fp),
        "primarch_name": request.primarch_name
    })
    logger.info(f"Created session doc {doc_id}: {request.title} -> {fp}")

    return {"id": doc_id, "title": request.title, "file_path": str(fp), "status": "active",
            "primarch_name": request.primarch_name}


@app.get("/api/session-docs")
async def list_session_docs(status: Optional[str] = None, project: Optional[str] = None):
    """List session documents with optional filters."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM session_documents WHERE 1=1"
        params = []

        if status:
            query += " AND status = ?"
            params.append(status)
        if project:
            query += " AND project = ?"
            params.append(project)

        query += " ORDER BY updated_at DESC"
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

        docs = []
        for row in rows:
            doc = dict(row)
            # Count linked instances
            cnt_cursor = await db.execute(
                "SELECT COUNT(*) FROM claude_instances WHERE session_doc_id = ?",
                (row["id"],)
            )
            doc["linked_instances"] = (await cnt_cursor.fetchone())[0]
            docs.append(doc)

    return {"docs": docs}


@app.get("/api/session-docs/deployment-queue")
async def get_deployment_queue():
    """List session docs ready for Administratum processing."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM session_documents WHERE status = 'deployment' ORDER BY updated_at ASC"
        )
        rows = await cursor.fetchall()
    return {"docs": [dict(r) for r in rows]}


@app.get("/api/session-docs/{doc_id}")
async def get_session_doc(doc_id: int):
    """Get session document metadata and linked instances."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")

        doc = dict(row)

        # Get linked instances
        cursor = await db.execute(
            "SELECT id, tab_name, status, working_dir, is_processing FROM claude_instances WHERE session_doc_id = ?",
            (doc_id,)
        )
        instances = [dict(r) for r in await cursor.fetchall()]
        doc["instances"] = instances

    return doc


@app.get("/api/session-docs/{doc_id}/content")
async def get_session_doc_content(doc_id: int):
    """Read the actual markdown file content of a session document."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_path, title FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")

    fp = Path(row[0])
    if not fp.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {fp}")

    return {"id": doc_id, "title": row[1], "file_path": str(fp), "content": fp.read_text()}


@app.patch("/api/session-docs/{doc_id}")
async def update_session_doc(doc_id: int, request: SessionDocUpdateRequest):
    """Update session document metadata."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, status FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")

        updates = []
        params = []
        if request.title is not None:
            updates.append("title = ?")
            params.append(request.title)
        if request.project is not None:
            updates.append("project = ?")
            params.append(request.project)
        if request.status is not None:
            current_status = row[1]
            valid_targets = VALID_STATUS_TRANSITIONS.get(current_status, set())
            # Any status can go to archived (escape hatch)
            if request.status != "archived" and request.status not in valid_targets:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status transition: {current_status} → {request.status}. Valid: {valid_targets | {'archived'}}"
                )
            updates.append("status = ?")
            params.append(request.status)

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(doc_id)

        await db.execute(
            f"UPDATE session_documents SET {', '.join(updates)} WHERE id = ?",
            params
        )
        await db.commit()

    logger.info(f"Updated session doc {doc_id}: {updates}")
    return {"id": doc_id, "updated": True}


@app.delete("/api/session-docs/{doc_id}")
async def delete_session_doc(doc_id: int, hard: bool = False):
    """Delete a session document. Default is soft delete (archive). Use ?hard=true for hard delete."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_path, title FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")

        if hard:
            # NULL out session_doc_id on linked instances
            await db.execute(
                "UPDATE claude_instances SET session_doc_id = NULL WHERE session_doc_id = ?",
                (doc_id,)
            )
            # Delete from DB
            await db.execute("DELETE FROM session_documents WHERE id = ?", (doc_id,))
            await db.commit()

            # Remove file
            fp = Path(row[0])
            if fp.exists():
                fp.unlink()

            await log_event("session_doc_deleted", details={"doc_id": doc_id, "title": row[1], "hard": True})
            logger.info(f"Hard deleted session doc {doc_id}: {row[1]}")
            return {"id": doc_id, "deleted": True, "hard": True}
        else:
            await db.execute(
                "UPDATE session_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), doc_id)
            )
            await db.commit()

            await log_event("session_doc_archived", details={"doc_id": doc_id, "title": row[1]})
            logger.info(f"Archived session doc {doc_id}: {row[1]}")
            return {"id": doc_id, "archived": True}


@app.post("/api/session-docs/{doc_id}/merge")
async def merge_into_session_doc(doc_id: int, request: SessionDocMergeRequest):
    """Intelligently merge content into a session document using LLM."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_path, title FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Session document not found")

    fp = Path(row[0])
    if not fp.exists():
        raise HTTPException(404, f"File not found: {fp}")

    current_content = fp.read_text()
    context_hint = f"\nContext: {request.context}" if request.context else ""

    system_prompt = f"""You are a document editor for a session planning document. You will receive the current document and new content to merge in.

Rules:
- If the new content is an activity update or progress note, add it to the Activity Log section as a new entry with today's date and time.
- If the new content contains architectural decisions or plan changes, update the Plan section.
- If the new content is a quick note or thought, place it where it makes most sense.
- Preserve ALL existing content. Do not remove or summarize existing entries.
- Use markdown formatting. Activity log entries use ### headers with date and agent name.
- Return the COMPLETE updated document, including frontmatter.
- Do NOT add commentary or explanation outside the document."""

    user_msg = f"""Current document:
```markdown
{current_content}
```

New content to merge ({request.source} source{context_hint}):
```
{request.content}
```

Return the complete updated document."""

    try:
        updated = await minimax_chat(system_prompt, user_msg, max_tokens=4096)

        if not updated.strip():
            raise HTTPException(500, "Merge LLM returned empty response (may be rate limited)")

        # Strip markdown code fences if the LLM wrapped it
        if updated.startswith("```"):
            lines = updated.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            updated = "\n".join(lines)

        fp.write_text(updated)

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE session_documents SET updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), doc_id)
            )
            await db.commit()

        await log_event("session_doc_merged", details={
            "doc_id": doc_id, "source": request.source, "content_length": len(request.content)
        })
        return {"status": "merged", "doc_id": doc_id, "source": request.source}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Session doc merge failed for doc {doc_id}: {e}")
        raise HTTPException(500, f"Merge failed: {e}")


# ============ Instance-Doc Linking Endpoints ============

@app.post("/api/instances/{instance_id}/assign-doc")
async def assign_doc_to_instance(instance_id: str, doc_id: int):
    """Assign an existing session document to an instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Verify instance exists
        cursor = await db.execute("SELECT id, session_doc_id FROM claude_instances WHERE id = ?", (instance_id,))
        inst_row = await cursor.fetchone()
        if not inst_row:
            raise HTTPException(status_code=404, detail=f"Instance {instance_id} not found")

        old_doc_id = inst_row[1]

        # Verify doc exists
        cursor = await db.execute("SELECT id FROM session_documents WHERE id = ?", (doc_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")

        # Assign
        await db.execute(
            "UPDATE claude_instances SET session_doc_id = ? WHERE id = ?",
            (doc_id, instance_id)
        )
        await db.commit()

        # Update agents list in the new doc
        await _update_doc_agents_list(db, doc_id)

    # Handle orphan cleanup for old doc
    if old_doc_id and old_doc_id != doc_id:
        await _handle_orphan_doc(old_doc_id)

    await log_event("session_doc_assigned", instance_id=instance_id, details={"doc_id": doc_id})
    logger.info(f"Assigned instance {instance_id} to session doc {doc_id}")

    return {"instance_id": instance_id, "doc_id": doc_id, "assigned": True}


@app.post("/api/instances/{instance_id}/create-doc")
async def create_doc_for_instance(instance_id: str, request: SessionDocCreateRequest):
    """Create a new session document and assign it to the instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Verify instance exists
        cursor = await db.execute("SELECT id, session_doc_id FROM claude_instances WHERE id = ?", (instance_id,))
        inst_row = await cursor.fetchone()
        if not inst_row:
            raise HTTPException(status_code=404, detail=f"Instance {instance_id} not found")

        old_doc_id = inst_row[1]

    # Create the doc by reusing the create endpoint logic
    today = datetime.now().strftime("%Y-%m-%d")
    slug = request.title.lower().replace(" ", "-")[:50]

    if request.file_path:
        fp = Path(request.file_path)
    else:
        fp = DEFAULT_SESSIONS_DIR / f"{today}-{slug}.md"

    if fp.exists():
        raise HTTPException(status_code=409, detail=f"File already exists: {fp}")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
               VALUES (?, ?, ?, 'active', ?, ?)""",
            (request.title, str(fp), request.project, datetime.now().isoformat(), datetime.now().isoformat())
        )
        doc_id = cursor.lastrowid

        # Assign to instance
        await db.execute(
            "UPDATE claude_instances SET session_doc_id = ? WHERE id = ?",
            (doc_id, instance_id)
        )
        await db.commit()

    create_session_doc_file(fp, request.title, doc_id, request.project)

    # Handle orphan cleanup for old doc
    if old_doc_id:
        await _handle_orphan_doc(old_doc_id)

    await log_event("session_doc_created", instance_id=instance_id, details={
        "doc_id": doc_id, "title": request.title, "file_path": str(fp), "auto_assigned": True
    })
    logger.info(f"Created session doc {doc_id}: {request.title} and assigned to {instance_id}")

    return {"id": doc_id, "title": request.title, "file_path": str(fp), "instance_id": instance_id, "status": "active"}


@app.delete("/api/instances/{instance_id}/unassign-doc")
async def unassign_doc_from_instance(instance_id: str):
    """Unlink a session document from an instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, session_doc_id FROM claude_instances WHERE id = ?", (instance_id,))
        inst_row = await cursor.fetchone()
        if not inst_row:
            raise HTTPException(status_code=404, detail=f"Instance {instance_id} not found")

        old_doc_id = inst_row[1]
        if not old_doc_id:
            return {"instance_id": instance_id, "unassigned": False, "reason": "No doc was assigned"}

        await db.execute(
            "UPDATE claude_instances SET session_doc_id = NULL WHERE id = ?",
            (instance_id,)
        )
        await db.commit()

        # Update agents list in the old doc
        await _update_doc_agents_list(db, old_doc_id)

    # Handle orphan cleanup
    await _handle_orphan_doc(old_doc_id)

    await log_event("session_doc_unassigned", instance_id=instance_id, details={"doc_id": old_doc_id})
    logger.info(f"Unassigned instance {instance_id} from session doc {old_doc_id}")

    return {"instance_id": instance_id, "doc_id": old_doc_id, "unassigned": True}


@app.get("/api/instances/{instance_id}/session-doc")
async def get_instance_session_doc(instance_id: str):
    """Get the session document linked to this instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT session_doc_id FROM claude_instances WHERE id = ?",
            (instance_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Instance not found")
        if not row[0]:
            return {"session_doc_id": None}

        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM session_documents WHERE id = ?",
            (row[0],)
        )
        doc = await cursor.fetchone()
        if not doc:
            return {"session_doc_id": None}
        return dict(doc)


# ============ Primarch Endpoints ============

@app.get("/api/primarchs")
async def list_primarchs():
    """List all primarchs from DB with their active session doc."""
    result = []
    async with aiosqlite.connect(DB_PATH) as db:
        primarchs = await get_all_primarchs_from_db(db)
        for p in primarchs:
            # Get active doc link
            cursor = await db.execute(
                "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
                (p["name"],)
            )
            link_row = await cursor.fetchone()
            active_doc = None
            if link_row:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT id, title, file_path, status FROM session_documents WHERE id = ?", (link_row[0],))
                doc_row = await cursor.fetchone()
                if doc_row:
                    active_doc = dict(doc_row)
                db.row_factory = None

            result.append({
                "name": p["name"],
                "title": p["title"],
                "aliases": p["aliases"],
                "vault": p["vault"],
                "role": p["role"],
                "instance_name_prefix": p["instance_name_prefix"],
                "vault_note_path": p.get("vault_note_path"),
                "active_doc": active_doc,
            })
    return {"primarchs": result}


@app.get("/api/primarchs/{name}")
async def get_primarch(name: str):
    """Get a single primarch by name or alias."""
    async with aiosqlite.connect(DB_PATH) as db:
        p = await get_primarch_from_db(db, name)
        if not p:
            raise HTTPException(404, f"Unknown primarch: {name}")
        # Get active doc
        cursor = await db.execute(
            "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
            (p["name"],)
        )
        link_row = await cursor.fetchone()
        active_doc = None
        if link_row:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT id, title, file_path, status FROM session_documents WHERE id = ?", (link_row[0],))
            doc_row = await cursor.fetchone()
            if doc_row:
                active_doc = dict(doc_row)
            db.row_factory = None
        return {**p, "active_doc": active_doc}


@app.get("/api/primarchs/{name}/active-doc")
async def get_primarch_active_doc(name: str):
    """Get the currently linked session doc for a primarch, or null."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Resolve alias to canonical name
        p = await get_primarch_from_db(db, name)
        if p:
            name = p["name"]
        cursor = await db.execute(
            "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
            (name,)
        )
        link_row = await cursor.fetchone()
        if not link_row:
            return {"primarch": name, "doc_id": None, "doc": None}

        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM session_documents WHERE id = ?", (link_row[0],))
        doc_row = await cursor.fetchone()
        if not doc_row:
            return {"primarch": name, "doc_id": None, "doc": None}

        return {"primarch": name, "doc_id": link_row[0], "doc": dict(doc_row)}


class PrimarchLinkDocRequest(BaseModel):
    title: Optional[str] = None


@app.post("/api/primarchs/{name}/link-doc")
async def link_primarch_doc(name: str, doc_id: Optional[int] = None, request: PrimarchLinkDocRequest = None):
    """Link a primarch to a session doc. If doc_id query param given, link existing. If body has title, create new + link."""
    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        if doc_id:
            # Link to existing doc
            cursor = await db.execute("SELECT id FROM session_documents WHERE id = ?", (doc_id,))
            if not await cursor.fetchone():
                raise HTTPException(404, f"Session doc {doc_id} not found")
            target_doc_id = doc_id
            # Set primarch_name on the doc
            await db.execute(
                "UPDATE session_documents SET primarch_name = ?, updated_at = ? WHERE id = ?",
                (name, now, doc_id)
            )
        elif request and request.title:
            # Create new doc + link
            today = datetime.now().strftime("%Y-%m-%d")
            slug = request.title.lower().replace(" ", "-")[:50]
            fp = DEFAULT_SESSIONS_DIR / f"{today}-{slug}.md"
            if fp.exists():
                raise HTTPException(409, f"File already exists: {fp}")
            cursor = await db.execute(
                """INSERT INTO session_documents (title, file_path, primarch_name, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (request.title, str(fp), name, now, now)
            )
            target_doc_id = cursor.lastrowid
            create_session_doc_file(fp, request.title, target_doc_id, primarch_name=name)
        else:
            raise HTTPException(400, "Provide doc_id query param or {title} in body")

        # Unlink previous active doc
        await db.execute(
            "UPDATE primarch_session_docs SET unlinked_at = ? WHERE primarch_name = ? AND unlinked_at IS NULL",
            (now, name)
        )
        # Create new link
        await db.execute(
            "INSERT INTO primarch_session_docs (primarch_name, session_doc_id, linked_at) VALUES (?, ?, ?)",
            (name, target_doc_id, now)
        )
        await db.commit()

    await log_event("primarch_doc_linked", details={"primarch": name, "doc_id": target_doc_id})
    logger.info(f"Primarch {name} linked to doc {target_doc_id}")
    return {"primarch": name, "doc_id": target_doc_id, "linked": True}


@app.delete("/api/primarchs/{name}/link-doc")
async def unlink_primarch_doc(name: str):
    """Unlink the current session doc from a primarch."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
            (name,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"primarch": name, "unlinked": False, "reason": "No active doc linked"}

        doc_id = row[0]
        await db.execute(
            "UPDATE primarch_session_docs SET unlinked_at = ? WHERE primarch_name = ? AND unlinked_at IS NULL",
            (now, name)
        )
        await db.execute(
            "UPDATE session_documents SET primarch_name = NULL, updated_at = ? WHERE id = ?",
            (now, doc_id)
        )
        await db.commit()

    await log_event("primarch_doc_unlinked", details={"primarch": name, "doc_id": doc_id})
    logger.info(f"Primarch {name} unlinked from doc {doc_id}")
    return {"primarch": name, "doc_id": doc_id, "unlinked": True}


# ============ Deployment Lifecycle Endpoints ============

@app.post("/api/session-docs/{doc_id}/deploy")
async def deploy_session_doc(doc_id: int):
    """Transition a completed session doc to deployment status."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, status, title FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, f"Session doc {doc_id} not found")
        if row[1] != "completed":
            raise HTTPException(400, f"Can only deploy completed docs, current status: {row[1]}")

        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE session_documents SET status = 'deployment', updated_at = ? WHERE id = ?",
            (now, doc_id)
        )
        await db.commit()

    await log_event("session_doc_deployed", details={"doc_id": doc_id, "title": row[2]})
    logger.info(f"Session doc {doc_id} ({row[2]}) moved to deployment")
    return {"id": doc_id, "status": "deployment"}


@app.post("/api/session-docs/{doc_id}/mark-processed")
async def mark_session_doc_processed(doc_id: int):
    """Mark a deployment doc as processed by Administratum. Unlinks primarch if linked."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, status, title, primarch_name FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, f"Session doc {doc_id} not found")
        if row[1] != "deployment":
            raise HTTPException(400, f"Can only mark deployment docs as processed, current status: {row[1]}")

        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE session_documents SET status = 'processed', primarch_name = NULL, updated_at = ? WHERE id = ?",
            (now, doc_id)
        )

        # Unlink primarch if this was linked
        if row[3]:
            await db.execute(
                "UPDATE primarch_session_docs SET unlinked_at = ? WHERE primarch_name = ? AND session_doc_id = ? AND unlinked_at IS NULL",
                (now, row[3], doc_id)
            )

        await db.commit()

    await log_event("session_doc_processed", details={"doc_id": doc_id, "title": row[2]})
    logger.info(f"Session doc {doc_id} ({row[2]}) marked as processed")
    return {"id": doc_id, "status": "processed"}


# ============ MiniMax Status Endpoint ============

@app.get("/api/minimax/status")
async def get_minimax_status():
    """Get MiniMax API rate limiter status."""
    return {"remaining": minimax_limiter.remaining, "max": minimax_limiter.max_calls}


# ============ Fleet State Endpoints ============

_FLEET_STATE_DEFAULTS = {
    "machine_spirit": 1.0,
    "domain_priority": ["discord", "cli-tools", "vault", "token-api"],
    "last_successful_runs": {},
    "stuck_jobs": [],
    "last_fg_run": None,
    "simulation_mode": False,
    "pending_confirmations": {},
    "autonomy_queue": {"completable": [], "researchable": []},
    "notes": [],
}

_LEGACY_STATE_PATH = Path.home() / ".openclaw" / "workspace" / "memory" / "fabricator-state.json"


async def _get_fleet_state_row(db: aiosqlite.Connection) -> Optional[dict]:
    cursor = await db.execute("SELECT state_json FROM agent_state WHERE id = 'fabricator'")
    row = await cursor.fetchone()
    if row:
        return json.loads(row[0])
    return None


async def _ensure_agent_state_table(db: aiosqlite.Connection):
    await db.execute("""
        CREATE TABLE IF NOT EXISTS agent_state (
            id       TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    await db.commit()


@app.get("/api/fleet/state")
async def get_fleet_state():
    """Return current fleet state. Seeds from legacy file on first access."""
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_agent_state_table(db)
        state = await _get_fleet_state_row(db)
        if state is None:
            # One-time migration from legacy file
            if _LEGACY_STATE_PATH.exists():
                try:
                    state = json.loads(_LEGACY_STATE_PATH.read_text())
                except Exception:
                    state = dict(_FLEET_STATE_DEFAULTS)
            else:
                state = dict(_FLEET_STATE_DEFAULTS)
            now = datetime.now().isoformat()
            await db.execute(
                "INSERT INTO agent_state (id, state_json, updated_at) VALUES ('fabricator', ?, ?)",
                (json.dumps(state), now),
            )
            await db.commit()
    return state


@app.patch("/api/fleet/state")
async def patch_fleet_state(request: Request):
    """Merge-patch update: only provided keys are updated, others preserved."""
    updates = await request.json()
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_agent_state_table(db)
        state = await _get_fleet_state_row(db)
        if state is None:
            state = dict(_FLEET_STATE_DEFAULTS)
        state.update(updates)
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO agent_state (id, state_json, updated_at) VALUES ('fabricator', ?, ?)",
            (json.dumps(state), now),
        )
        await db.commit()
    return state


@app.put("/api/fleet/state")
async def put_fleet_state(request: Request):
    """Full replacement of fleet state."""
    new_state = await request.json()
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_agent_state_table(db)
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO agent_state (id, state_json, updated_at) VALUES ('fabricator', ?, ?)",
            (json.dumps(new_state), now),
        )
        await db.commit()
    return new_state


@app.post("/api/fleet/state/reset")
async def reset_fleet_state():
    """Reset fleet state to defaults."""
    state = dict(_FLEET_STATE_DEFAULTS)
    async with aiosqlite.connect(DB_PATH) as db:
        await _ensure_agent_state_table(db)
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT OR REPLACE INTO agent_state (id, state_json, updated_at) VALUES ('fabricator', ?, ?)",
            (json.dumps(state), now),
        )
        await db.commit()
    return state


# ============ Habit Tracker Endpoints ============

@app.get("/api/habits/definitions")
async def get_habit_definitions():
    """Return all active habit definitions with their windows."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, category, window_start_hour, window_end_hour, notes FROM habits WHERE active = 1 ORDER BY window_start_hour, category, id"
        )
        rows = await cursor.fetchall()
    return {"habits": [dict(r) for r in rows]}


@app.get("/api/habits/today")
async def get_habits_today():
    """Return today's habit completion state: definitions + which are checked off."""
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT h.id, h.name, h.category, h.window_start_hour, h.window_end_hour, h.notes,
                   hc.completed_at, hc.notes AS completion_notes
            FROM habits h
            LEFT JOIN habit_completions hc ON hc.habit_id = h.id AND hc.date = ?
            WHERE h.active = 1
            ORDER BY h.window_start_hour, h.category, h.id
        """, (today,))
        rows = await cursor.fetchall()

    habits = []
    for r in rows:
        d = dict(r)
        d["completed"] = d["completed_at"] is not None
        habits.append(d)

    completed_count = sum(1 for h in habits if h["completed"])
    return {
        "date": today,
        "habits": habits,
        "summary": {"total": len(habits), "completed": completed_count, "pending": len(habits) - completed_count},
    }


@app.post("/api/habits/today/{habit_id}")
async def mark_habit_today(habit_id: str, body: dict = None):
    """Mark a habit complete (or incomplete) for today.

    Body: {"completed": true, "notes": "optional notes"}
    Omitting body or setting completed=true marks it complete.
    Setting completed=false removes today's completion record.
    """
    if body is None:
        body = {}

    completed = body.get("completed", True)
    notes = body.get("notes")
    today = datetime.now().strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        # Verify habit exists
        cursor = await db.execute("SELECT id, name FROM habits WHERE id = ? AND active = 1", (habit_id,))
        habit = await cursor.fetchone()
        if not habit:
            raise HTTPException(status_code=404, detail=f"Habit '{habit_id}' not found")

        if completed:
            await db.execute("""
                INSERT INTO habit_completions (habit_id, date, notes)
                VALUES (?, ?, ?)
                ON CONFLICT(habit_id, date) DO UPDATE SET completed_at = CURRENT_TIMESTAMP, notes = excluded.notes
            """, (habit_id, today, notes))
        else:
            await db.execute(
                "DELETE FROM habit_completions WHERE habit_id = ? AND date = ?",
                (habit_id, today)
            )
        await db.commit()

    action = "completed" if completed else "uncompleted"
    return {"habit_id": habit_id, "date": today, "action": action, "notes": notes}


@app.get("/api/state")
async def get_state():
    """
    Pre-digested state snapshot for MiniMax heartbeat / Custodes agent.

    Aggregates timer, work mode, location, instances, and habits into a
    single call so agents don't need to hit 4 separate endpoints.
    """
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    current_hour = now.hour

    # Location: None means outside all known zones
    location_zone = DESKTOP_STATE.get("location_zone")
    location = location_zone if location_zone else "unknown"

    # Timer
    break_balance_ms = timer_engine.break_balance_ms
    break_remaining_min = round(max(0, break_balance_ms) / 60000, 1)
    work_earned_min = round(timer_engine.total_work_time_ms / 60000, 1)
    timer_mode = timer_engine.current_mode.value

    # Instances
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status = 'active'"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0

        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status = 'active' AND is_processing = 1"
        )
        row = await cursor.fetchone()
        processing_count = row[0] if row else 0

        # Habits
        cursor = await db.execute("""
            SELECT h.window_start_hour, h.window_end_hour,
                   hc.completed_at
            FROM habits h
            LEFT JOIN habit_completions hc ON hc.habit_id = h.id AND hc.date = ?
            WHERE h.active = 1
        """, (today,))
        habit_rows = await cursor.fetchall()

    total_habits = len(habit_rows)
    completed_habits = sum(1 for r in habit_rows if r[2] is not None)

    # Pending-window: habit window includes current hour, not yet completed
    pending_windows = set()
    for start_hour, end_hour, completed_at in habit_rows:
        if completed_at is None and start_hour is not None and end_hour is not None:
            if start_hour <= current_hour < end_hour:
                if start_hour < 12:
                    pending_windows.add("morning")
                elif start_hour < 17:
                    pending_windows.add("afternoon")
                else:
                    pending_windows.add("evening")

    return {
        "location": location,
        "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "break_time_remaining_min": break_remaining_min,
        "break_in_backlog": break_balance_ms < 0,
        "work_time_earned_min": work_earned_min,
        "timer_mode": timer_mode,
        "active_instances": active_count,
        "is_processing": processing_count > 0,
        "processing_count": processing_count,
        "current_time": now.isoformat(),
        "habits_today": {
            "completed": completed_habits,
            "total": total_habits,
            "pending_window": sorted(pending_windows),
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
