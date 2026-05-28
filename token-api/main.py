"""
Token-API: FastAPI Local Server for Claude Instance Management

This server provides:
- Claude instance registration and tracking
- Device identification (desktop vs SSH from phone)
- Notification routing
- Productivity gating
"""

import asyncio
import inspect
import json
import logging
import mimetypes
import os
import re
import shlex
import signal
import socket
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# Canonical Scripts root — derived from this file's location (token-api/ is one level down)
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
UI_DIR = Path(__file__).resolve().parent / "ui"

import subprocess
import tempfile

import aiosqlite
import httpx
import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import shared
import talk as talk_service
import temp_message as temp_message_service
from cron_engine import CronEngine
from custodes_state_policy import (
    StateEvent,
    build_dedupe_key,
    evaluate_state_event,
    normalize_severity,
)
from dailynote_callout import (
    ALLOWED_CALLOUT_TYPES,
    CALLOUT_ID_RE,
    MAX_CONTENT_BYTES,
    CalloutConflictError,
    CalloutError,
    apply_callout,
)
from db_schema import init_database_async
from instance_mutation import (
    RECONCILIATION_SUSPICIOUS,
    get_instance_mutations,
    reconcile_instance,
    sanctioned_update_instance,
)
from pane_surface import (
    DEFAULT_TAB_NAME_RX,
)
from pane_surface import (
    human_pane_surface as _format_human_pane_surface,
)
from pane_surface import (
    is_meaningful_tab_name as _is_meaningful_surface_name,
)
from pane_surface import (
    sanitize_human_surface as _sanitize_human_surface,
)
from phone_service import (
    _persist_twitter_zap_cooldown,
    _restore_twitter_zap_cooldown,
    _send_to_phone,
    check_instance_count_pavlok,
    push_phone_widget_async,
    send_pavlok_stimulus,
)
from questions_gate import trials_clear
from routes.day_start import fire_day_start_internal, sync_day_start_schedule_from_daily_note
from routes.day_start import router as day_start_router
from routes.hooks import (
    NUDGE_COOLDOWN_SECONDS,
    _recently_nudged,
)
from routes.hooks import (
    init_deps as hooks_init_deps,
)
from routes.hooks import (
    router as hooks_router,
)
from routes.tts import (
    _is_quiet_hours,
    get_tts_queue_status,
    play_sound,
    speak_tts,
    tts_queue_worker,
)
from routes.tts import (
    router as tts_router,
)
from routes.voice import router as voice_router
from schedule import router as schedule_router
from session_doc_helpers import (
    DEFAULT_RUBRIC_KEY,
    RubricStatus,
    _update_doc_agents_list,
    bump_session_doc_up_to_date,
    create_session_doc_file,
    human_filename_stem,
    mark_rubric_acknowledged,
    mark_rubric_notified,
    read_frontmatter,
    read_rubric,
    unique_human_path,
    update_frontmatter,
    update_rubric_field,
)
from shared import (
    CRASH_LOG_PATH,
    DB_PATH,
    DEFAULT_SESSIONS_DIR,
    DESKTOP_CONFIG,
    DESKTOP_STATE,
    DICTATION_STATE,
    DISCORD_DAEMON_URL,
    FALLBACK_VOICES,
    PAVLOK_CONFIG,
    PAVLOK_STATE,
    PEDAL_BUFFER_MS,
    PEDAL_BYPASS_MS,
    PEDAL_DOUBLE_TAP_MS,
    PEDAL_STATE,
    PHONE_CONFIG,
    PHONE_HEARTBEAT,
    PHONE_STATE,
    PROFILES,
    SERVER_PORT,
    STASH_DIR,
    STASH_MAX_AGE_HOURS,
    TTS_BACKEND,
    TTS_GLOBAL_MODE,
    ULTIMATE_FALLBACK,
    VOICE_CHAT_SESSIONS,
    get_next_available_profile,
    is_local_device,
    is_pid_claude,
    log_event,
    resolve_device_from_ip,
)
from timer import (
    DEFAULT_BREAK_BUFFER_MS,
    Activity,
    TimerEngine,
    TimerEvent,
    TimerMode,
    format_timer_time,
)

DESKFLOW_SERVER_PORT = 24800
DESKFLOW_CLIENT_CONFIG_PATH = Path.home() / "Library" / "Deskflow" / "Deskflow.conf"
DESKFLOW_KEYMAP_GUARD = SCRIPTS_DIR / "Shell" / "deskflow-keymap-guard.sh"
MAC_KVM_BACKOFF_SECONDS = [30, 60, 120, 300, 900]

# Configure logging for TUI capture
logger = logging.getLogger("token_api")
logger.setLevel(logging.INFO)

# ============ Server-side Log Buffer ============
from collections import deque

# Circular buffer to store recent log entries (max 500)
log_buffer: deque[dict] = deque(maxlen=500)


class LogBufferHandler(logging.Handler):
    """Custom logging handler that captures logs to circular buffer."""

    def emit(self, record: logging.LogRecord):
        """Capture log record to buffer with timestamp, level, and message."""
        try:
            log_entry = {
                "timestamp": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": self.format(record),
            }
            log_buffer.append(log_entry)
        except Exception:
            # Silently fail to avoid logging errors in logging system
            pass


# Add buffer handler to logger
buffer_handler = LogBufferHandler()
buffer_handler.setLevel(logging.DEBUG)
buffer_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(buffer_handler)

# Also capture uvicorn and fastapi logs
uvicorn_logger = logging.getLogger("uvicorn")
uvicorn_logger.addHandler(buffer_handler)

fastapi_logger = logging.getLogger("fastapi")
fastapi_logger.addHandler(buffer_handler)


# Configuration
# [MOVED to shared.py / routes/tts.py] — was: DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path


# ============ Crash Logging ============
import sys
import traceback

# Machine identity from centralized config
sys.path.insert(0, str(SCRIPTS_DIR / "cli-tools" / "lib"))
from imperium_config import cfg
from tmuxctl.focus_guard import preserve_focus as _tmuxctl_preserve_focus
from tmuxctl.tmux_adapter import TmuxAdapter as _TmuxCtlAdapter

LOCAL_DEVICE_NAME = cfg("device_name")  # "Mac-Mini" on mac, "TokenPC" on wsl, etc.
ASSERT_PERSONA_PANE_LABELS = {
    "legion:custodes",
    "mechanicus:fabricator-general",
    "mechanicus:admin",
}


def _is_assert_persona_label(value: str | None) -> bool:
    return (value or "") in ASSERT_PERSONA_PANE_LABELS


def _run_tmux_focus_preserved(
    args: tuple[str, ...] | list[str],
    *,
    source: str,
    attempted_target: str = "",
    allow_failure: bool = True,
) -> str:
    """Run a tmux operation from Token-API without leaving the client focused elsewhere."""
    argv = tuple(args)
    if not argv:
        return ""
    tmux_args = argv[1:] if Path(argv[0]).name == "tmux" else argv
    adapter = _TmuxCtlAdapter()
    with _tmuxctl_preserve_focus(
        adapter,
        source=source,
        attempted_target=attempted_target,
    ):
        return adapter.run(*tmux_args, allow_failure=allow_failure)


def spawn_tmux_assert_instance(
    pane_target: str | None, instance_id: str = "", source: str = "system"
) -> None:
    """Run close-down pane assertion out-of-band and log stdout/stderr.

    This is intentionally shared by hook-adjacent fallback paths (pane-state
    projection, dead-pane cleanup, reconciler). SessionEnd is preferred, but
    real exits can be observed first by these workers; stale pane chrome must
    still converge through tmuxctl's single assert-instance path.
    """
    if not pane_target:
        return
    tmuxctl = SCRIPTS_DIR / "cli-tools" / "bin" / "tmuxctl"
    if not tmuxctl.exists():
        logger.warning(
            "%s: assert-instance skipped for %s — tmuxctl not found", source, pane_target
        )
        return
    code = r"""
import os
import subprocess
import sys
import time

tmuxctl, pane, instance_id, source = sys.argv[1:5]
env = os.environ.copy()
env.setdefault("IMPERIUM_TMUX_AUTOMATION", "1")
try:
    time.sleep(2)
    proc = subprocess.run(
        [tmuxctl, "assert-instance", "--pane", pane],
        text=True,
        capture_output=True,
        timeout=75,
        check=False,
        env=env,
    )
    sys.stdout.write(f"[{source}] pane={pane} instance={instance_id}\n")
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    raise SystemExit(proc.returncode)
except subprocess.TimeoutExpired as exc:
    sys.stderr.write(f"[{source}] assert-instance timeout pane={pane} instance={instance_id}: {exc}\n")
    raise SystemExit(124)
"""
    log_path = Path("/tmp/session-end-assert-instance.log")
    log_handle = None
    try:
        log_handle = log_path.open("a")
        subprocess.Popen(
            ["python3", "-c", code, str(tmuxctl), pane_target, instance_id, source],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            close_fds=True,
        )
        logger.info(
            "%s: spawned assert-instance for %s (%s)", source, pane_target, instance_id[:12]
        )
    except Exception as exc:
        logger.warning("%s: failed to spawn assert-instance for %s: %s", source, pane_target, exc)
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass


def log_crash(exc_type, exc_value, exc_tb, context: str = "unhandled"):
    """Write crash info to persistent file for post-mortem debugging."""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        tb_str = "".join(tb_lines)

        with open(CRASH_LOG_PATH, "a") as f:
            f.write(f"\n{'=' * 60}\n")
            f.write(f"CRASH [{context}] at {timestamp}\n")
            f.write(f"{'=' * 60}\n")
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
                f.write(f"\n{'=' * 60}\n")
                f.write(f"ASYNCIO ERROR at {timestamp}\n")
                f.write(f"{'=' * 60}\n")
                f.write(f"{context}\n\n")
        except Exception:
            pass

    # Call the default handler
    loop.default_exception_handler(context)


# Install global exception handlers
sys.excepthook = _global_exception_handler

# [MOVED to shared.py] — DEVICE_IPS, LOCAL_DEVICES, resolve_device_from_ip, is_local_device


# ── Legion Pane Recolor ──────────────────────────────────────
# Dark-tinted tmux backgrounds per legion. Subtle but unmistakable.
# "default" means no custom bg (reset to terminal default).
LEGION_PANE_COLORS = {
    "custodes": "#302800",  # dark gold
    "mechanicus": "#300808",  # dark red
    "fabricator": "#300808",  # FG shares the 4:mechanicus page tint. Without this
    # entry the recolor worker resolves .get("fabricator",
    # "default") and overwrites the bg=#300808 that
    # _assert_persona_color sets — the two systems fight.
    "civic": "#083010",  # dark green
    "astartes": "default",  # no tint (default legion)
}


# Scheduler instance. Jobs stay in memory; restart recovery is driven from the
# application DB by recover_expected_ack_jobs() and
# recover_recent_stopped_golden_throne_timers(). Avoid APScheduler's synchronous
# SQLite job store on the asyncio thread.
scheduler = AsyncIOScheduler()
shared.scheduler = scheduler
APP_LOOP: asyncio.AbstractEventLoop | None = None

# Cron engine (initialized after DB in lifespan)
cron_engine: CronEngine = None
mac_kvm_supervisor_task = None

MAC_KVM_STATE = {
    "state": "starting",
    "server_host": None,
    "server_reachable": None,
    "client_running": None,
    "retry_attempts": 0,
    "next_probe_at": 0.0,
    "last_action": None,
    "last_changed": None,
}


# Pydantic Models
class InstanceRegisterRequest(BaseModel):
    instance_id: str
    origin_type: str = "local"  # 'local' or 'ssh'
    source_ip: str | None = None
    device_id: str | None = None
    pid: int | None = None
    tab_name: str | None = None
    working_dir: str | None = None


class InstanceResponse(BaseModel):
    id: str
    session_id: str
    tab_name: str | None
    working_dir: str | None
    origin_type: str
    source_ip: str | None
    device_id: str
    profile_name: str
    tts_voice: str
    notification_sound: str
    pid: int | None
    status: str
    registered_at: str
    last_activity: str
    stopped_at: str | None


class ActivityRequest(BaseModel):
    action: str  # "prompt_submit" or "stop"


class TempMessageRequest(BaseModel):
    selector: str = Field(..., min_length=1)
    payload: str = Field(..., min_length=1)
    idempotency_key: str | None = None


class TalkSendRequest(BaseModel):
    caller_pane: str = Field(..., min_length=1)
    target_pane: str = Field(..., min_length=1)
    payload: str = Field(..., min_length=1)


class TalkReturnRequest(BaseModel):
    caller_pane: str = Field(..., min_length=1)
    target_pane: str = Field(..., min_length=1)
    payload: str = Field(..., min_length=1)


class BriefSendRequest(BaseModel):
    caller_pane: str | None = None
    panes: list[str] = Field(default_factory=list)
    pages: list[str] = Field(default_factory=list)
    payload: str = Field(..., min_length=1)
    ephemeral: bool = False


class ProfileResponse(BaseModel):
    session_id: str
    profile: dict


class DashboardResponse(BaseModel):
    instances: list[dict]
    productivity_active: bool
    recent_events: list[dict]
    tts_queue: dict | None = None  # TTS queue status


class AgentRuntime(BaseModel):
    id: str | None = None
    name: str | None = None
    status: str
    engine: str | None = None
    working_dir: str | None = None
    tmux_pane: str | None = None
    device_id: str | None = None
    last_activity: str | None = None
    registered: bool = True
    live_pane: bool | None = None


class ActivityIconState(BaseModel):
    key: str
    icon: str
    label: str
    active: bool
    source: str


class WorkStateResponse(BaseModel):
    productivity_active: bool
    reason: str
    active_instance_count: int
    processing_recent_count: int
    observed_agent_count: int
    active_instances: list[AgentRuntime]
    observed_agents: list[AgentRuntime]
    activity_icons: list[ActivityIconState]
    timer_mode: str
    activity: str
    desktop_mode: str
    phone_app: str | None = None
    generated_at: str


class TaskResponse(BaseModel):
    id: str
    name: str
    description: str | None
    task_type: str
    schedule: str
    enabled: bool
    max_retries: int
    last_run: dict | None = None
    next_run: str | None = None


class TaskUpdateRequest(BaseModel):
    schedule: str | None = None
    enabled: bool | None = None
    max_retries: int | None = None


class TaskExecutionResponse(BaseModel):
    id: int
    task_id: str
    status: str
    started_at: str
    completed_at: str | None
    duration_ms: int | None
    result: dict | None
    retry_count: int


class CustodesStateEventRequest(BaseModel):
    event_type: str
    source: str
    instance_id: str | None = None
    severity: int | None = None
    payload: dict | None = None


# [MOVED to shared.py / routes/tts.py] — was: class NotifyRequest(BaseModel):


class WindowCheckRequest(BaseModel):
    """Request to check if a window should be allowed or closed."""

    window_title: str | None = None  # e.g., "YouTube - Brave"
    exe_name: str | None = None  # e.g., "brave.exe"
    source: str = "ahk"  # Source of the request


# ============ Audio Proxy Models ============


class AudioProxyState(BaseModel):
    """Current state of the audio proxy system."""

    phone_connected: bool = False
    receiver_running: bool = False
    receiver_pid: int | None = None
    last_connect_time: str | None = None
    last_disconnect_time: str | None = None


class AudioProxyConnectRequest(BaseModel):
    """Request when phone connects to PC Bluetooth."""

    phone_device_id: str = "Token-S24"
    bluetooth_device_name: str | None = None
    source: str = "macrodroid"


class AudioProxyConnectResponse(BaseModel):
    """Response after processing connect request."""

    success: bool
    action: str  # "connected", "already_connected", "error"
    receiver_started: bool
    receiver_pid: int | None = None
    message: str


class AudioProxyDisconnectRequest(BaseModel):
    """Request when phone disconnects from PC Bluetooth."""

    phone_device_id: str = "Token-S24"
    source: str = "macrodroid"


class MediaPauseRequest(BaseModel):
    """Request from a desktop media pause key."""

    source: str = "desktop"


class AudioProxyStatusResponse(BaseModel):
    """Response for status query."""

    phone_connected: bool
    receiver_running: bool
    receiver_pid: int | None = None
    last_connect_time: str | None = None
    last_disconnect_time: str | None = None


class WindowEnforceResponse(BaseModel):
    """Response for window enforcement decision."""

    productivity_active: bool
    active_instance_count: int
    should_close_distractions: bool
    distraction_apps: list[str]  # Apps that should be closed if should_close_distractions is True
    reason: str


class StashContentRequest(BaseModel):
    content: str


class DesktopDetectionRequest(BaseModel):
    """Request from AHK desktop detection."""

    detected_mode: str  # "video" | "music" | "gaming" | "silence"
    window_title: str | None = None
    source: str = "ahk"
    steam_app_id: str | None = None
    steam_app_name: str | None = None
    steam_exe: str | None = None


class DesktopDetectionResponse(BaseModel):
    """Response for desktop detection."""

    action: str  # "mode_changed" | "blocked" | "none"
    detected_mode: str
    old_mode: str | None = None
    new_mode: str | None = None
    reason: str
    timer_updated: bool = False
    productivity_active: bool
    active_instance_count: int


class GameTurnRequest(BaseModel):
    """Observational turn-end event from game-specific AHK hooks."""

    game: str
    steam_app_id: str | None = None
    steam_app_name: str | None = None
    steam_exe: str | None = None
    source: str = "ahk"


class GameTurnResponse(BaseModel):
    recorded: bool
    block: bool = False
    reason: str = "observational_only"
    ack_id: str | None = None


class MewgenicsSpaceTelemetryRequest(BaseModel):
    """Policy-free Mewgenics Space key telemetry from AHK."""

    event: str = "mewgenics_space"
    source: str = "ahk"
    ts: str | None = None


class MewgenicsSpaceTelemetryResponse(BaseModel):
    recorded: bool
    reason: str
    zap_fired: bool = False


class EnforcementAckRequest(BaseModel):
    ack_id: str | None = None
    source: str | None = None
    instance_id: str | None = None


class EnforcementExpectRequest(BaseModel):
    reason: str
    source: str = "manual"
    instance_id: str | None = None
    details: dict | None = None


class EnforcementBailoutRequest(BaseModel):
    reason: str
    ack_id: str | None = None
    source: str | None = None
    instance_id: str | None = None


class WorkActionRequest(BaseModel):
    source: str = "api"
    note: str | None = None


class StateValidateRequest(BaseModel):
    state: str | None = None
    var: str | None = None
    name: str | None = None
    app: str | None = None
    assert_: str | bool | int | float | None = Field(default=None, alias="assert")


# ============ Phone Activity Models ============


class PhoneActivityRequest(BaseModel):
    """Request from MacroDroid for phone app activity."""

    app: str  # App name: "twitter", "youtube", "game", or app package name
    action: str = "open"  # "open" | "close"
    package: str | None = None  # Optional package name for games


class PhoneActivityResponse(BaseModel):
    """Response for phone activity detection."""

    allowed: bool
    reason: str  # "break_time_available", "productivity_active", "blocked", "closed"
    break_seconds: int = 0
    message: str | None = None


class PhoneSystemEventRequest(BaseModel):
    """Request from MacroDroid for phone system events (Shizuku, boot, heartbeat, telemetry).

    Supports two formats:
      Full:    {"event": "app_open", "app": "Application Launched (X)"}
      Minimal: {"app": "Application Launched (X)"}  (event inferred from trigger name)
    """

    event: str | None = None  # Optional — inferred from trigger name if absent
    time: str | None = None
    server: str | None = None  # heartbeat: server response code
    shizuku_dead: str | None = None  # heartbeat: current shizuku state
    app: str | None = None  # trigger name (e.g. "Application Launched (X)")
    notification: str | None = None  # discord_fallback_received: original notification text


# ============ Headless Mode Models ============


class HeadlessStatusResponse(BaseModel):
    """Response for headless mode status."""

    enabled: bool
    last_changed: str | None = None
    hostname: str | None = None
    error: str | None = None
    auto_disable_at: str | None = None  # ISO timestamp when headless will auto-disable


class HeadlessControlRequest(BaseModel):
    """Request to control headless mode."""

    action: str = "toggle"  # "toggle" | "enable" | "disable"
    duration_hours: float | None = None  # Auto-disable after N hours


class HeadlessControlResponse(BaseModel):
    """Response after controlling headless mode."""

    success: bool
    action: str
    before: HeadlessStatusResponse
    after: HeadlessStatusResponse | None = None
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

# [MOVED to routes/hooks.py or shared.py] — was: class HookResponse(BaseModel):


class DiscordMessageRequest(BaseModel):
    """Forwarded Discord message from the discord-cli daemon."""

    message_id: str | None = None
    channel_id: str
    channel_name: str | None = None
    guild_id: str | None = None
    author: dict | None = None
    content: str
    timestamp: str | None = None
    is_dm: bool = False
    is_reply: bool = False
    is_voice: bool = False
    bot_name: str | None = None
    target_tmux_pane: str | None = None
    voice_no_submit: bool = False
    voice_append_submit: bool = False
    reply_to_message_id: str | None = None
    attachments: list | None = None
    embeds: int | None = 0


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
    author: str | None = None


class SessionDocCreateRequest(BaseModel):
    title: str
    project: str | None = None
    file_path: str | None = None
    primarch_name: str | None = None


class SessionDocUpdateRequest(BaseModel):
    title: str | None = None
    project: str | None = None
    status: str | None = None


class SessionDocMergeRequest(BaseModel):
    content: str
    source: str = "agent"
    context: str | None = None


class NamingNudgeRequest(BaseModel):
    """Stop-hook payload for active tab-name enforcement."""

    session_id: str | None = None
    instance_id: str | None = None


# [MOVED to routes/hooks.py or shared.py] — was: # ============ Hook Handler State ============


# Database helper: connect with busy_timeout to prevent indefinite blocking
async def get_db():
    """Get a database connection with busy_timeout configured."""
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("PRAGMA busy_timeout=5000")
    return db


TOKEN_API_HEARTBEAT_PATH = Path.home() / ".claude" / "token-api-heartbeat.json"


def _write_token_api_heartbeat() -> None:
    """Update the watchdog heartbeat file atomically."""
    payload = {
        "pid": os.getpid(),
        "timestamp": datetime.now().isoformat(),
        "service": "ai.openclaw.tokenapi",
    }
    TOKEN_API_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = TOKEN_API_HEARTBEAT_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(payload, sort_keys=True))
    tmp_path.replace(TOKEN_API_HEARTBEAT_PATH)


async def token_api_heartbeat_worker() -> None:
    """Emit the file heartbeat consumed by tokenapi-watchdog."""
    while True:
        try:
            await asyncio.to_thread(_write_token_api_heartbeat)
        except Exception as exc:
            logger.warning(f"Token-API heartbeat write failed: {exc}")
        await asyncio.sleep(30)


# [MOVED to shared.py / routes/tts.py] — was: async def log_event(event_type: str, instance_id:

# [MOVED to shared.py] — resolve_device_from_ip, LOCAL_DEVICES, is_local_device

# ============ Scheduled Task System ============


def parse_interval_schedule(schedule: str) -> dict:
    """Parse interval schedule string like '30m', '1h', '5s' into trigger kwargs."""
    match = re.match(r"^(\d+)(s|m|h|d)$", schedule.strip().lower())
    if not match:
        raise ValueError(f"Invalid interval format: {schedule}. Use format like '30m', '1h', '5s'")

    value = int(match.group(1))
    unit = match.group(2)

    unit_map = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    return {unit_map[unit]: value}


async def acquire_task_lock(task_id: str) -> bool:
    """Try to acquire a lock for a task. Returns True if lock acquired."""
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO task_locks (task_id, locked_at, locked_by) VALUES (?, ?, ?)",
                (task_id, now, "main"),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            # Lock already exists - check if it's stale (> 1 hour old)
            cursor = await db.execute(
                "SELECT locked_at FROM task_locks WHERE task_id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            if row:
                locked_at = datetime.fromisoformat(row[0])
                if datetime.now() - locked_at > timedelta(hours=1):
                    # Stale lock, force acquire
                    await db.execute(
                        "UPDATE task_locks SET locked_at = ?, locked_by = ? WHERE task_id = ?",
                        (now, "main", task_id),
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
            (task_id, now),
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
            (now, duration_ms, json.dumps(result), execution_id),
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
            (now, json.dumps({"error": error}), execution_id),
        )
        await db.commit()


# ============ Task Implementations ============


async def cleanup_stale_instances() -> dict:
    """Mark instances with no activity for 3+ hours as stopped."""
    cutoff = (datetime.now() - timedelta(hours=3)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT id
               FROM claude_instances
               WHERE status IN ('processing', 'idle')
                 AND last_activity < ?""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        for row in rows:
            await sanctioned_update_instance(
                db,
                instance_id=row[0],
                updates={
                    "status": "stopped",
                    "synced": 0,
                    "stopped_at": datetime.now().isoformat(),
                },
                mutation_type="instance_stopped",
                write_source="task",
                actor="cleanup-stale",
            )
        await db.commit()
        affected = len(rows)

    if affected > 0:
        await log_event("task_cleanup", details={"cleaned_up": affected})

    return {"cleaned_up": affected}


async def purge_old_events() -> dict:
    """Delete events older than 30 days."""
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
        deleted = cursor.rowcount
        await db.commit()

    return {"deleted": deleted}


# Task registry mapping task IDs to their implementation functions
TASK_REGISTRY = {
    "cleanup_stale_instances": cleanup_stale_instances,
    "purge_old_events": purge_old_events,
    "day_start_schedule_fallback": lambda: fire_day_start_internal(
        source="schedule_fallback",
        details={"schedule": "wake_anchor"},
    ),
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
                        day_of_week=parts[4],
                    )
                else:
                    raise ValueError(f"Invalid cron expression: {schedule}")
            else:
                print(f"Unknown task type: {task_type}")
                continue

            scheduler.add_job(
                execute_task, trigger=trigger, args=[task_id], id=task_id, replace_existing=True
            )
            print(f"Registered task: {task_id} ({task_type}: {schedule})")

        except Exception as e:
            print(f"Failed to register task {task_id}: {e}")


RESTART_STATE_PATH = Path(__file__).parent / "restart_state.json"

# Keys that must NOT survive a restart — derived from live signals or boot-time config.
_RESTART_STATE_DENYLIST = {
    "startup_time",  # reset per boot
    "startup_grace_secs",  # config, reset per boot
    "ahk_reachable",  # live heartbeat — unknown until AHK checks in
    "ahk_last_heartbeat",  # live heartbeat
}


def save_restart_state() -> None:
    """Dump DESKTOP_STATE to disk for pragma-once consumption on next startup.

    Called during graceful shutdown. Only surviveable keys are persisted
    (see _RESTART_STATE_DENYLIST). Failure is non-fatal — next boot just
    starts fresh.
    """
    try:
        persistable = {k: v for k, v in DESKTOP_STATE.items() if k not in _RESTART_STATE_DENYLIST}
        payload = {
            "saved_at": datetime.now().isoformat(),
            "desktop_state": persistable,
        }
        RESTART_STATE_PATH.write_text(json.dumps(payload, indent=2))
        print(f"Restart state saved: {sorted(persistable.keys())}")
    except Exception as e:
        print(f"Failed to save restart state: {e}")


def restore_restart_state() -> None:
    """Read restart_state.json, apply to DESKTOP_STATE, then delete it.

    Pragma-once: the file is consumed and removed so a subsequent crash-reboot
    (which didn't go through graceful shutdown) boots fresh rather than
    restoring potentially-stale state. If no file exists, this is a no-op.
    """
    if not RESTART_STATE_PATH.exists():
        print("No restart state found, starting fresh")
        return
    try:
        payload = json.loads(RESTART_STATE_PATH.read_text())
        persisted = payload.get("desktop_state", {})
        saved_at = payload.get("saved_at", "unknown")
        applied = []
        for key, value in persisted.items():
            if key in _RESTART_STATE_DENYLIST:
                continue
            DESKTOP_STATE[key] = value
            applied.append(key)
        print(f"Restored restart state (saved {saved_at}): {sorted(applied)}")
    except Exception as e:
        print(f"Failed to restore restart state: {e}")
    finally:
        try:
            RESTART_STATE_PATH.unlink(missing_ok=True)
        except Exception as e:
            print(f"Failed to delete restart state: {e}")


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
                (task_id,),
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
    global stale_flag_cleaner_task, timer_worker_task, mac_kvm_supervisor_task, APP_LOOP
    import routes.tts as _tts_mod  # For mutable tts_worker_task assignment

    # Install asyncio exception handler for this loop
    loop = asyncio.get_running_loop()
    APP_LOOP = loop
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
    await init_database_async(DB_PATH)
    try:
        day_start_schedule = await sync_day_start_schedule_from_daily_note()
        print(
            "Day-start schedule fallback synced "
            f"to wake_anchor={day_start_schedule['wake_anchor']} "
            f"({day_start_schedule['cron']})"
        )
    except Exception as exc:
        logger.warning(f"Day-start schedule fallback sync failed: {exc}")
    await load_tasks_from_db()
    timer_load_from_db()
    restore_restart_state()
    recovered_phone_distraction = await recover_recent_phone_distraction_state()
    # Sync timer activity layer with restored desktop mode
    desktop_mode = DESKTOP_STATE.get("current_mode", "silence")
    now_ms = int(time.monotonic() * 1000)
    if recovered_phone_distraction:
        print(f"TIMER: Recovered phone distraction state (desktop={desktop_mode})")
    elif desktop_mode in ("video", "scrolling", "gaming"):
        is_sg = desktop_mode in ("scrolling", "gaming")
        timer_engine.set_activity(
            Activity.DISTRACTION, is_scrolling_gaming=is_sg, now_mono_ms=now_ms
        )
        print(
            f"TIMER: Synced activity=DISTRACTION (desktop={desktop_mode}, scrolling_gaming={is_sg})"
        )
    else:
        timer_engine.set_activity(Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=now_ms)
        print(f"TIMER: Synced activity=WORKING (desktop={desktop_mode})")
    # Stash cleanup on startup + hourly
    stash_cleanup()
    scheduler.add_job(
        stash_cleanup, IntervalTrigger(hours=1), id="stash_cleanup", replace_existing=True
    )
    # 7 AM daily timer reset (clear accumulated break + wipe prior-day timer events)
    scheduler.add_job(
        timer_9am_reset, CronTrigger(hour=7, minute=0), id="timer_7am_reset", replace_existing=True
    )
    scheduler.add_job(
        _scheduled_quiet_enter_sync,
        CronTrigger(hour=shared.QUIET_HOURS_START, minute=0),
        id="timer_quiet_enter",
        replace_existing=True,
    )
    scheduler.add_job(
        _scheduled_quiet_exit_sync,
        CronTrigger(hour=shared.QUIET_HOURS_END, minute=0),
        id="timer_quiet_exit",
        replace_existing=True,
    )
    if os.environ.get("TOKEN_API_ENABLE_PANE_WRITE_QUEUE_WORKER") == "1":
        scheduler.add_job(
            _process_pane_write_queue_sync,
            IntervalTrigger(seconds=5),
            id="pane_write_queue_worker",
            replace_existing=True,
            max_instances=1,
        )
    scheduler.start()
    print("Scheduler started")
    await recover_expected_ack_jobs()
    recovered_gt = await recover_recent_stopped_golden_throne_timers()
    if recovered_gt:
        print(f"Golden Throne recovered {len(recovered_gt)} stopped timer(s)")
    # Initialize cron engine
    global cron_engine
    cron_engine = CronEngine(scheduler, DB_PATH)
    await cron_engine.recover_orphaned_runs()
    await cron_engine.ensure_permanent_jobs()
    cron_engine.register_now_widget_job(DB_PATH, DAILY_NOTE_DIR)
    print("Cron engine loaded")
    # Start TTS queue worker
    _tts_mod.tts_worker_task = asyncio.create_task(tts_queue_worker())
    print("TTS queue worker started")
    asyncio.create_task(token_api_heartbeat_worker())
    print("Token-API watchdog heartbeat started")
    # Start stale flag cleaner
    stale_flag_cleaner_task = asyncio.create_task(clear_stale_processing_flags())
    print("Stale flag cleaner started")
    # Start stuck instance detector
    stuck_detector_task = asyncio.create_task(detect_stuck_instances())
    print("Stuck instance detector started")
    # Start timer engine worker
    timer_worker_task = asyncio.create_task(timer_worker())
    print("Timer engine started")
    # Start phone heartbeat monitor
    asyncio.create_task(phone_heartbeat_worker())
    print("Phone heartbeat monitor started")
    # Start Mac-side Deskflow backoff supervisor
    mac_kvm_supervisor_task = asyncio.create_task(mac_kvm_supervisor())
    print("Mac KVM supervisor started")
    # Start legion pane recolor worker
    asyncio.create_task(legion_pane_recolor_worker())
    print("Legion pane recolor worker started")
    # Start pane state worker (@CC_STATE)
    asyncio.create_task(pane_state_worker())
    print("Pane state worker started")
    # Start session doc sync worker
    asyncio.create_task(session_doc_sync_worker())
    print("Session doc sync worker started")
    # Start tmux↔DB reconciler worker
    asyncio.create_task(tmux_db_reconciler_worker())
    print("tmux↔DB reconciler worker started")
    await run_overdue_tasks()
    yield

    # Log shutdown to crash log
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CRASH_LOG_PATH, "a") as f:
            f.write(f"--- SERVER STOPPING at {timestamp} ---\n")
    except Exception:
        pass

    # Persist ephemeral state for next startup (pragma-once consumption).
    save_restart_state()

    # Shutdown
    if _tts_mod.tts_worker_task:
        _tts_mod.tts_worker_task.cancel()
        try:
            await _tts_mod.tts_worker_task
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
    if mac_kvm_supervisor_task:
        mac_kvm_supervisor_task.cancel()
        try:
            await mac_kvm_supervisor_task
        except asyncio.CancelledError:
            pass
    scheduler.shutdown(wait=True)
    print("Scheduler stopped")


# FastAPI App
app = FastAPI(
    title="Token-API",
    description="Local FastAPI server for Claude instance management",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Slaanesh scheduling routes (Black Ships booking portal)
app.include_router(schedule_router)
app.include_router(tts_router)
app.include_router(voice_router)
app.include_router(hooks_router)
app.include_router(day_start_router)


NAMING_NUDGE_MAX_PER_INSTANCE = 3
NAMING_NUDGE_EVENT_TYPE = "naming_nudge_sent"
NAMING_NUDGE_QUEUE_SOURCE = "naming_nudge"
NAMING_NUDGE_QUEUE_PURPOSE = "name_missing"


async def _count_naming_nudges(db, instance_id: str) -> int:
    cursor = await db.execute(
        "SELECT COUNT(*) FROM events WHERE instance_id = ? AND event_type = ?",
        (instance_id, NAMING_NUDGE_EVENT_TYPE),
    )
    row = await cursor.fetchone()
    return int(row[0] or 0) if row else 0


async def _has_pending_naming_nudge(db, instance_id: str) -> bool:
    cursor = await db.execute(
        """
        SELECT 1
        FROM pane_write_queue
        WHERE instance_id = ?
          AND source = ?
          AND purpose = ?
          AND status = 'pending'
        LIMIT 1
        """,
        (instance_id, NAMING_NUDGE_QUEUE_SOURCE, NAMING_NUDGE_QUEUE_PURPOSE),
    )
    return bool(await cursor.fetchone())


def _build_naming_nudge_message(slug: str | None) -> str:
    derived = (slug or "").strip()
    hint = f" Current rough doc slug is `{derived}`." if derived else ""
    return (
        "Your session document still needs a descriptive name. "
        "Choose a 3-6 word title that describes the work, then run "
        '`session-doc-name "Your Descriptive Title"`. '
        "Do not use dates, timestamps, UUIDs, pane IDs, model names, or generic project roots."
        f"{hint}"
    )


@app.post("/api/orchestrator/naming_nudge")
async def orchestrator_naming_nudge(request: NamingNudgeRequest):
    """Nudge a stopped pane that still has a placeholder tab name.

    This endpoint is intentionally idempotent for renamed panes and capped to
    three nudges per instance. Durable writes go through sanctioned mutation
    helpers; the nudge count is derived from the append-only events table.
    """

    instance_id = request.instance_id or request.session_id
    if not instance_id:
        return {"success": False, "action": "missing_instance_id"}

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT ci.id, ci.tab_name, ci.tmux_pane, ci.workflow_blocked_reason,
                   ci.session_doc_id, ci.dispatch_session_doc_path,
                   sd.file_path AS session_doc_path
            FROM claude_instances ci
            LEFT JOIN session_documents sd ON ci.session_doc_id = sd.id
            WHERE ci.id = ?
            """,
            (instance_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"success": False, "action": "instance_not_found", "instance_id": instance_id}

        instance = dict(row)
        if not _is_placeholder_tab_name(instance.get("tab_name")):
            return {
                "success": True,
                "action": "noop_named",
                "instance_id": instance_id,
                "tab_name": instance.get("tab_name"),
            }

        if instance.get("workflow_blocked_reason") == "naming_refused":
            return {
                "success": True,
                "action": "noop_cap_reached",
                "instance_id": instance_id,
                "nudges": NAMING_NUDGE_MAX_PER_INSTANCE,
            }

        tmux_pane = (instance.get("tmux_pane") or "").strip()
        if not tmux_pane:
            return {"success": False, "action": "missing_tmux_pane", "instance_id": instance_id}

        nudge_count = await _count_naming_nudges(db, instance_id)
        if nudge_count >= NAMING_NUDGE_MAX_PER_INSTANCE:
            await sanctioned_update_instance(
                db,
                instance_id=instance_id,
                updates={"workflow_blocked_reason": "naming_refused"},
                mutation_type="instance_updated",
                write_source="api",
                actor="naming-nudge",
            )
            await db.commit()
            await log_event(
                "naming_nudge_cap_reached",
                instance_id=instance_id,
                details={"nudges": nudge_count, "tmux_pane": tmux_pane},
            )
            return {
                "success": True,
                "action": "cap_reached",
                "instance_id": instance_id,
                "nudges": nudge_count,
                "workflow_blocked_reason": "naming_refused",
            }

        if await _has_pending_naming_nudge(db, instance_id):
            return {
                "success": True,
                "action": "noop_pending_nudge",
                "instance_id": instance_id,
                "nudges": nudge_count,
            }

        if instance.get("workflow_blocked_reason") != "tab_name_placeholder":
            await sanctioned_update_instance(
                db,
                instance_id=instance_id,
                updates={"workflow_blocked_reason": "tab_name_placeholder"},
                mutation_type="instance_updated",
                write_source="api",
                actor="naming-nudge",
            )
            await db.commit()

    session_doc_path = instance.get("session_doc_path") or instance.get("dispatch_session_doc_path")
    slug = _derive_session_doc_slug(session_doc_path)
    message = _build_naming_nudge_message(slug)
    queued = await enqueue_pane_write(
        instance_id=instance_id,
        tmux_pane=tmux_pane,
        source=NAMING_NUDGE_QUEUE_SOURCE,
        purpose=NAMING_NUDGE_QUEUE_PURPOSE,
        payload=message,
    )
    queue_results = await process_pane_write_queue_once(queued["id"])
    queue_result = queue_results[0] if queue_results else queued
    next_count = nudge_count + 1
    await log_event(
        NAMING_NUDGE_EVENT_TYPE,
        instance_id=instance_id,
        details={
            "tmux_pane": tmux_pane,
            "slug": slug,
            "queue_id": queued["id"],
            "queue_status": queue_result.get("status"),
            "nudge_number": next_count,
        },
    )

    return {
        "success": True,
        "action": "nudge_sent",
        "instance_id": instance_id,
        "tmux_pane": tmux_pane,
        "slug": slug,
        "nudge_number": next_count,
        "queue_id": queued["id"],
        "queue_status": queue_result.get("status"),
        "defer_reason": queue_result.get("reason"),
    }


# Instance Registration Endpoints
@app.post("/api/instances/register", response_model=ProfileResponse)
async def register_instance(request: InstanceRegisterRequest):
    """Register a new Claude instance."""
    logger.info(
        f"Registering instance: {request.working_dir or request.tab_name or request.instance_id[:8]}"
    )
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
                now,
            ),
        )
        await db.commit()

    if pool_exhausted:
        logger.warning(f"Voice pool exhausted — assigned fallback voice {profile['wsl_voice']}")

    # Log event
    await log_event(
        "instance_registered",
        instance_id=request.instance_id,
        device_id=device_id,
        details={"tab_name": request.tab_name, "origin_type": request.origin_type},
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
            "color": profile.get("color", "#0099ff"),
            "cc_color": profile.get("cc_color", "default"),
        },
    )


@app.delete("/api/instances/all")
async def delete_all_instances():
    """Delete all instances from the database (clear all)."""
    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        # Get all instances before deleting
        cursor = await db.execute("SELECT id, device_id, status FROM claude_instances")
        all_instances = await cursor.fetchall()

        if not all_instances:
            return {"status": "no_instances", "deleted_count": 0}

        # Count active instances for enforcement check
        active_count = sum(1 for _, _, status in all_instances if status in ("processing", "idle"))

        # Delete all instances from the database
        await db.execute("DELETE FROM claude_instances")
        await db.commit()

    # Log bulk deletion event
    await log_event("bulk_delete_all", details={"count": len(all_instances), "timestamp": now})

    # Check enforcement if there were active instances
    if active_count > 0 and DESKTOP_STATE.get("current_mode") == "video":
        enforce_result = close_distraction_windows()
        await log_event(
            "enforcement_triggered",
            details={"trigger": "all_instances_deleted", "result": enforce_result},
        )
        return {
            "status": "deleted_all",
            "deleted_count": len(all_instances),
            "enforcement_triggered": True,
            "enforcement_result": enforce_result,
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
            (instance_id,),
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

        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"status": "stopped", "synced": 0, "stopped_at": now},
            mutation_type="instance_stopped",
            write_source="api",
            actor="stop-instance",
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
    await log_event("instance_stopped", instance_id=instance_id, device_id=row[1])

    # Instance count Pavlok signals (skip subagents)
    if not is_subagent:
        await check_instance_count_pavlok(remaining_non_sub, was_active)

    # Push updated instance count to phone widget
    if not is_subagent:
        asyncio.create_task(
            push_phone_widget_async(timer_engine.current_mode.value, remaining_non_sub)
        )

    # If no more active instances and video mode was active, enforce
    if remaining_active == 0 and DESKTOP_STATE.get("current_mode") == "video":
        print("ENFORCE: Last instance stopped while in video mode, closing distractions")
        enforce_result = close_distraction_windows()
        await log_event(
            "enforcement_triggered",
            details={"trigger": "last_instance_stopped", "result": enforce_result},
        )
        return {
            "status": "stopped",
            "instance_id": instance_id,
            "enforcement_triggered": True,
            "enforcement_result": enforce_result,
        }

    return {"status": "stopped", "instance_id": instance_id}


async def find_claude_pid_by_workdir(working_dir: str) -> int | None:
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
                with open(comm_path) as f:
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


# [MOVED to shared.py] — is_pid_claude, get_parent_pid, is_subagent_pid


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
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
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
                    await sanctioned_update_instance(
                        db,
                        instance_id=instance_id,
                        updates={"status": "stopped", "synced": 0, "stopped_at": now},
                        mutation_type="instance_stopped",
                        write_source="api",
                        actor="kill-instance",
                    )
                    await db.commit()
                await log_event(
                    "instance_killed",
                    instance_id=instance_id,
                    device_id=device_id,
                    details={"error": "no_pid", "status": "marked_stopped"},
                )
                raise HTTPException(
                    status_code=400,
                    detail="No PID stored and could not discover process. Instance marked stopped.",
                )
        else:
            # Can't scan /proc on remote device
            async with aiosqlite.connect(DB_PATH) as db:
                await sanctioned_update_instance(
                    db,
                    instance_id=instance_id,
                    updates={"status": "stopped", "synced": 0, "stopped_at": now},
                    mutation_type="instance_stopped",
                    write_source="api",
                    actor="kill-instance",
                )
                await db.commit()
            await log_event(
                "instance_killed",
                instance_id=instance_id,
                device_id=device_id,
                details={"error": "no_pid_remote", "status": "marked_stopped"},
            )
            raise HTTPException(
                status_code=400,
                detail=f"No PID stored for remote device '{device_id}'. Instance marked stopped.",
            )

    # Kill sequence based on device type
    if is_local_device(device_id):
        # Validate PID still belongs to claude
        if not is_pid_claude(pid):
            # Process already exited or PID reused by another process
            async with aiosqlite.connect(DB_PATH) as db:
                await sanctioned_update_instance(
                    db,
                    instance_id=instance_id,
                    updates={"status": "stopped", "synced": 0, "stopped_at": now},
                    mutation_type="instance_stopped",
                    write_source="api",
                    actor="kill-instance",
                )
                await db.commit()
            await log_event(
                "instance_killed",
                instance_id=instance_id,
                device_id=device_id,
                details={"pid": pid, "status": "already_dead"},
            )
            return {"status": "already_dead", "pid": pid, "signal": None}

        # SIGINT×2 (mimics double Ctrl+C: first cancels operation, second exits gracefully)
        try:
            os.kill(pid, signal.SIGINT)
            kill_signal = "SIGINT"
            logger.info(f"Kill: sent first SIGINT to PID {pid}")
        except ProcessLookupError:
            # Already dead
            async with aiosqlite.connect(DB_PATH) as db:
                await sanctioned_update_instance(
                    db,
                    instance_id=instance_id,
                    updates={"status": "stopped", "synced": 0, "stopped_at": now},
                    mutation_type="instance_stopped",
                    write_source="api",
                    actor="kill-instance",
                )
                await db.commit()
            await log_event(
                "instance_killed",
                instance_id=instance_id,
                device_id=device_id,
                details={"pid": pid, "status": "already_dead"},
            )
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
                "sshp",
                f"kill -INT {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            kill_signal = "SIGINT"
            logger.info(f"Kill: sent first SIGINT via SSH to PID {pid} on {device_id}")

            # Wait 1s then send second SIGINT
            await asyncio.sleep(1)
            proc1b = await asyncio.create_subprocess_exec(
                "sshp",
                f"kill -INT {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc1b.communicate(), timeout=10)
            kill_signal = "SIGINT_x2"
            logger.info(f"Kill: sent second SIGINT via SSH to PID {pid} on {device_id}")

            # Wait 3s then check/escalate
            await asyncio.sleep(3)

            proc2 = await asyncio.create_subprocess_exec(
                "sshp",
                f"kill -0 {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=10)
            if proc2.returncode == 0:
                # Still alive, escalate
                proc3 = await asyncio.create_subprocess_exec(
                    "sshp",
                    f"kill -9 {pid}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc3.communicate(), timeout=10)
                kill_signal = "SIGKILL"
                logger.info(f"Kill: escalated to SIGKILL via SSH for PID {pid} on {device_id}")
        except TimeoutError:
            raise HTTPException(status_code=504, detail=f"SSH to {device_id} timed out")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SSH kill failed: {str(e)}")

    # Mark stopped in DB
    async with aiosqlite.connect(DB_PATH) as db:
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"status": "stopped", "synced": 0, "stopped_at": now},
            mutation_type="instance_stopped",
            write_source="api",
            actor="kill-instance",
        )
        await db.commit()

    # Log event
    await log_event(
        "instance_killed",
        instance_id=instance_id,
        device_id=device_id,
        details={"pid": pid, "signal": kill_signal},
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
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
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
                    status_code=400, detail="No PID stored and could not discover process."
                )
        else:
            raise HTTPException(
                status_code=400, detail=f"No PID stored for remote device '{device_id}'."
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
                    await sanctioned_update_instance(
                        db,
                        instance_id=instance_id,
                        updates={"pid": pid},
                        mutation_type="instance_updated",
                        write_source="api",
                        actor="unstick-instance",
                    )
                    await db.commit()
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"PID {pid} is stale and no Claude process found in {working_dir}",
                )

        # Capture diagnostics BEFORE sending signal
        diag_before = get_process_diagnostics(pid)
        logger.info(
            f"Unstick L{level} BEFORE: PID {pid} state={diag_before.get('state', '?')} wchan={diag_before.get('wchan', '?')} children={len(diag_before.get('children', []))}"
        )

        try:
            os.kill(pid, sig)
            logger.info(f"Unstick L{level}: sent {sig_name} to PID {pid}")
        except ProcessLookupError:
            raise HTTPException(status_code=400, detail=f"PID {pid} no longer exists")
        except PermissionError:
            raise HTTPException(
                status_code=500, detail=f"Permission denied sending {sig_name} to PID {pid}"
            )
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                "sshp",
                f"kill -{ssh_sig} {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10)
            logger.info(f"Unstick L{level}: sent {sig_name} via SSH to PID {pid} on {device_id}")
        except TimeoutError:
            raise HTTPException(status_code=504, detail=f"SSH to {device_id} timed out")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SSH unstick failed: {str(e)}")

    # Wait and check for activity change
    await asyncio.sleep(4)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT last_activity FROM claude_instances WHERE id = ?", (instance_id,)
        )
        row = await cursor.fetchone()

    last_activity_after = dict(row).get("last_activity") if row else None
    activity_changed = last_activity_after != last_activity_before

    status = "nudged" if activity_changed else "no_change"

    # Capture diagnostics AFTER signal (desktop only)
    diag_after = None
    if is_local_device(device_id) and is_pid_claude(pid):
        diag_after = get_process_diagnostics(pid)
        logger.info(
            f"Unstick L{level} AFTER: PID {pid} state={diag_after.get('state', '?')} wchan={diag_after.get('wchan', '?')}"
        )

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
        },
    )

    logger.info(
        f"Unstick L{level}: instance {instance_id[:12]}... {status} (PID {pid}, {sig_name}, activity_changed={activity_changed})"
    )

    response = {
        "status": status,
        "pid": pid,
        "signal": sig_name,
        "level": level,
        "activity_changed": activity_changed,
    }
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
            with open(f"{proc_dir}/comm") as f:
                diag["comm"] = f.read().strip()
        except Exception as e:
            diag["comm_error"] = str(e)

        # Get cmdline
        try:
            with open(f"{proc_dir}/cmdline") as f:
                cmdline = f.read().replace("\x00", " ").strip()
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
            with open(f"{proc_dir}/stat") as f:
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
            with open(f"{proc_dir}/wchan") as f:
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
                    with open(f"/proc/{entry}/stat") as f:
                        child_stat = f.read().split()
                        if len(child_stat) > 3 and int(child_stat[3]) == pid:
                            child_comm = "(unknown)"
                            try:
                                with open(f"/proc/{entry}/comm") as cf:
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
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
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
            last_dt = (
                datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                if "T" in last_activity
                else datetime.strptime(last_activity, "%Y-%m-%d %H:%M:%S")
            )
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
                with open(f"/proc/{entry}/comm") as f:
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
    logger.info(
        f"Diagnose: instance {instance_id[:12]}... stored_pid={stored_pid}, discovered_pid={discovered_pid}, status={status}"
    )

    return result


class RenameInstanceRequest(BaseModel):
    tab_name: str


class PaneRenameRequest(BaseModel):
    tmux_pane: str
    tab_name: str


INSTANCE_NAME_MAX_CHARS = 40
INSTANCE_NAME_SPINNER_PREFIXES = "✳⠐⠸ "
INSTANCE_NAME_PLACEHOLDER_RX = re.compile(r"^Claude \d{2}:\d{2}$")


def _validate_instance_name_slug(tab_name: str | None) -> str:
    clean = (tab_name or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    if len(clean) > INSTANCE_NAME_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Name must be {INSTANCE_NAME_MAX_CHARS} characters or fewer",
        )
    placeholder_candidate = clean.lstrip(INSTANCE_NAME_SPINNER_PREFIXES).strip()
    if INSTANCE_NAME_PLACEHOLDER_RX.match(placeholder_candidate):
        raise HTTPException(
            status_code=400,
            detail="Name cannot be a placeholder like 'Claude HH:MM'",
        )
    return clean


async def _refresh_tmux_pane_label(tmux_pane: str | None) -> None:
    """Best-effort refresh for DB-backed tmux pane border labels."""
    if not tmux_pane:
        return
    try:
        cache_dir = Path(
            os.environ.get("TMUX_PANE_LABEL_CACHE", "~/.claude/tmux-pane-label-cache")
        ).expanduser()
        (cache_dir / tmux_pane.replace("%", "")).unlink(missing_ok=True)
    except Exception:
        pass
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "refresh-client",
            "-S",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=2)
    except Exception as exc:
        logger.debug(f"Pane label refresh failed for {tmux_pane}: {exc}")


class LogEntry(BaseModel):
    """Single log entry."""

    timestamp: str
    level: str
    message: str


class LogsResponse(BaseModel):
    """Response for recent logs."""

    logs: list[LogEntry]
    count: int


@app.patch("/api/instances/{instance_id}/rename")
async def rename_instance(instance_id: str, request: RenameInstanceRequest):
    """Rename an instance's tab_name. Auto-generated docs may mirror the title.

    Enforces kebab-case but preserves slot-style identifiers: strips ✳ artifacts,
    converts spaces to hyphens, drops most non-alphanumerics but keeps `:` and
    case so pane-slot names like `palace:NW` survive intact. Truncates to 4 words.
    """
    import re as _re

    clean = (request.tab_name or "").lstrip("✳ ").strip()
    clean = _re.sub(r"[^A-Za-z0-9: -]", "", clean)
    clean = _re.sub(r"[ -]+", "-", clean).strip("-")
    clean = "-".join(clean.split("-")[:4])
    if not clean:
        raise HTTPException(status_code=400, detail="Name cannot be empty after normalization")
    request.tab_name = clean

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, tab_name, session_doc_id, session_doc_policy FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        old_name = row[1]
        session_doc_id = row[2]
        session_doc_policy = row[3]
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"tab_name": request.tab_name},
            mutation_type="instance_updated",
            write_source="api",
            actor="rename-instance",
        )

        # Instance renames are instance-local. Session doc naming is doc-owned
        # (`PATCH /api/session-docs/{doc_id}` or creation title), because a
        # session doc may eventually have multiple attached instances.
        session_doc_updated = False

        await db.commit()

    # Log event
    await log_event(
        "instance_renamed",
        instance_id=instance_id,
        details={
            "old_name": old_name,
            "new_name": request.tab_name,
            "session_doc_updated": session_doc_updated,
            "session_doc_policy": session_doc_policy,
        },
    )

    return {"status": "renamed", "instance_id": instance_id, "tab_name": request.tab_name}


@app.post("/api/instance/rename")
async def rename_instance_by_pane(request: PaneRenameRequest):
    """Rename the active instance attached to a tmux pane.

    Used by the `instance-name` CLI from inside an agent pane. This route is
    intentionally pane-scoped so agents do not need to know their instance id.
    """
    tmux_pane = (request.tmux_pane or "").strip()
    if not tmux_pane:
        raise HTTPException(status_code=400, detail="tmux_pane is required")
    tab_name = _validate_instance_name_slug(request.tab_name)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, tab_name, tmux_pane
               FROM claude_instances
               WHERE tmux_pane = ?
                 AND COALESCE(status, '') != 'stopped'
               ORDER BY datetime(COALESCE(last_activity, registered_at, '1970-01-01')) DESC,
                        registered_at DESC
               LIMIT 1""",
            (tmux_pane,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No active instance found for tmux pane")

        old_name = row["tab_name"]
        instance_id = row["id"]
        result = await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"tab_name": tab_name},
            mutation_type="instance_updated",
            write_source="api",
            actor="instance-name-cli",
        )
        await db.commit()

    await _refresh_tmux_pane_label(tmux_pane)
    await log_event(
        "instance_renamed",
        instance_id=instance_id,
        details={
            "old_name": old_name,
            "new_name": tab_name,
            "tmux_pane": tmux_pane,
            "source": "instance-name-cli",
        },
    )
    return {
        "status": "renamed",
        "instance_id": instance_id,
        "tmux_pane": tmux_pane,
        "tab_name": tab_name,
        "changed_fields": result.get("changed_fields", []),
    }


@app.patch("/api/instances/{instance_id}/transplant-pending")
async def mark_transplant_pending(instance_id: str, target_session: str):
    """Mark an instance as about to be transplanted to a new session ID.

    Called by the transplant CLI before killing Claude. The new session's
    SessionStart handler checks for this marker to find the supplant source,
    enabling cross-device transplants where file-based handoff doesn't work.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM claude_instances WHERE id = ?", (instance_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"transplant_target_session": target_session},
            mutation_type="instance_updated",
            write_source="api",
            actor="transplant-pending",
        )
        await db.commit()
        cursor = await db.execute("SELECT 1 FROM claude_instances WHERE id = ?", (instance_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")

    logger.info(
        f"Transplant pending: {instance_id[:12]}... → target session {target_session[:12]}..."
    )
    return {"status": "pending", "instance_id": instance_id, "target_session": target_session}


@app.post("/api/instances/{instance_id}/input-lock")
async def acquire_input_lock(instance_id: str, locker: str = "claude-cmd"):
    """Acquire input lock for an instance's tmux pane.

    Prevents concurrent tmux send-keys from interleaving in the PTY buffer.
    Uses atomic UPDATE with WHERE to ensure only one caller wins.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")
        if row["input_lock"] is not None:
            return {"acquired": False, "held_by": row["input_lock"]}
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"input_lock": locker},
            mutation_type="instance_updated",
            write_source="api",
            actor="input-lock-acquire",
            where_clause="id = ? AND input_lock IS NULL",
            where_params=(instance_id,),
        )
        await db.commit()
        return {"acquired": True, "locker": locker}


@app.delete("/api/instances/{instance_id}/input-lock")
async def release_input_lock(instance_id: str, locker: str = "claude-cmd"):
    """Release input lock for an instance's tmux pane."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT input_lock FROM claude_instances WHERE id = ?", (instance_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")
        if row["input_lock"] == locker:
            await sanctioned_update_instance(
                db,
                instance_id=instance_id,
                updates={"input_lock": None},
                mutation_type="instance_updated",
                write_source="api",
                actor="input-lock-release",
                where_clause="id = ? AND input_lock = ?",
                where_params=(instance_id, locker),
            )
        await db.commit()
    return {"released": True}


@app.post("/api/instances/{instance_id}/activity")
async def update_instance_activity(instance_id: str, request: ActivityRequest):
    """Update instance processing state. Called by hooks on prompt_submit and stop."""
    now = datetime.now().isoformat()

    if request.action == "prompt_submit":
        await bust_quiet_state(
            "api",
            "prompt_submit",
            {"instance_id": instance_id, "action": request.action},
        )
        new_status = "processing"
        logger.info(f"Activity: {instance_id[:8]}... prompt submitted")
        acknowledged_acks = await acknowledge_pending_acks_for_instance(instance_id)
        acknowledged_acks += await acknowledge_pending_work_action_acks()
        _mark_mewgenics_work_action("prompt_submit", f"instance_id={instance_id}")
        stop_enforcement_cascade(reason="prompt_submit")
    elif request.action == "stop":
        new_status = "idle"
        acknowledged_acks = 0
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {request.action}")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        tmux_pane = row["tmux_pane"]
        device_id = row["device_id"]
        if device_id == LOCAL_DEVICE_NAME and tmux_pane and not await _tmux_pane_exists(tmux_pane):
            await sanctioned_update_instance(
                db,
                instance_id=instance_id,
                updates={
                    "status": "stopped",
                    "synced": 0,
                    "stopped_at": now,
                },
                mutation_type="instance_stopped",
                write_source="api",
                actor=f"activity-{request.action}-dead-pane",
            )
            await db.commit()
            await log_event(
                "activity_ignored_dead_pane",
                instance_id=instance_id,
                details={"action": request.action, "tmux_pane": tmux_pane},
            )
            return {
                "status": "ignored_dead_pane",
                "instance_id": instance_id,
                "action": request.action,
                "new_status": "stopped",
                "acknowledged_expected_acks": 0,
            }

        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"status": new_status, "last_activity": now},
            mutation_type="status_changed",
            write_source="api",
            actor=f"activity-{request.action}",
        )
        await db.commit()

    if request.action == "stop":
        stopped_instance = dict(row)
        stopped_instance.update({"status": new_status, "last_activity": now})
        try:
            await schedule_golden_throne_followup(stopped_instance, reason="stop_hook")
        except Exception as exc:
            logger.warning(
                f"Golden Throne: failed to schedule stop-hook follow-up "
                f"for {instance_id[:12]}: {exc}"
            )
        # Trinity Chunk 1 slash-copy is handled in routes/hooks.py:handle_stop,
        # which has the Stop payload's transcript_path (authoritative JSONL
        # location for the just-stopped session). Do not duplicate here.

    return {
        "status": "updated",
        "instance_id": instance_id,
        "action": request.action,
        "new_status": new_status,
        "acknowledged_expected_acks": acknowledged_acks,
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
            "current_task": current_task,
        }
    except Exception as e:
        return {
            "todos": [],
            "progress": 0,
            "current_task": None,
            "total": 0,
            "completed": 0,
            "error": str(e),
        }


# ============ Golden Throne API ============
# Thread persistence engine — zealotry controls follow-up frequency

# Zealotry-to-delay mapping (seconds)
ZEALOTRY_DELAY_MAP = {4: 1800, 5: 1200, 6: 900, 7: 600, 8: 420, 9: 300, 10: 60}
GT_ENFORCEMENT_RESUME_THRESHOLD = 2
GT_RESUME_WINDOW = timedelta(hours=24)
GOLDEN_THRONE_QUIET_HOURS_BUFFER = timedelta(minutes=5)

EXPECTED_ACK_PENDING = "pending"
EXPECTED_ACK_TERMINAL_STATUSES = {
    "acknowledged",
    "bailed_out",
    "expired",
    "blocked_by_guardrail",
}

# Temporary shock-test tuning for Golden Throne accountability loops.
EXPECTED_ACK_DEFAULT_ACK_DELAY = timedelta(seconds=90)
EXPECTED_ACK_DEFAULT_LEVEL2_DELAY = timedelta(minutes=3)
EXPECTED_ACK_DEFAULT_PAVLOK_DELAY = timedelta(minutes=3)
ENFORCEMENT_JOB_MISFIRE_GRACE_SECONDS = 300

# Only the terminal enforce stage survives the cascade collapse. Notify/warn
# tiers are gone — Golden Throne owns any softer escalation cadence by polling
# /api/enforcement/status and firing /api/enforce per missed ack.
EXPECTED_ACK_STAGE_ENFORCE = "enforce"
EXPECTED_ACK_STAGE_BY_LEVEL = {
    3: EXPECTED_ACK_STAGE_ENFORCE,
}
EXPECTED_ACK_LEVEL_BY_STAGE = {stage: level for level, stage in EXPECTED_ACK_STAGE_BY_LEVEL.items()}
EXPECTED_ACK_DUE_FIELD_BY_STAGE = {
    EXPECTED_ACK_STAGE_ENFORCE: "pavlok_due_at",
}
EXPECTED_ACK_POLICY_DEFAULT = (EXPECTED_ACK_STAGE_ENFORCE,)
EXPECTED_ACK_POLICY_BY_SOURCE = {}


def _agent_engine(instance: dict) -> str:
    engine = (instance.get("engine") or "").strip().lower()
    if engine in {"codex", "claude"}:
        return engine
    launcher = (instance.get("launcher") or "").strip().lower()
    if "codex" in launcher:
        return "codex"
    return "claude"


def _agent_is_alive_command(engine: str, current_cmd: str) -> bool:
    current = (current_cmd or "").lower()
    if engine == "codex":
        return "codex" in current
    return "claude" in current or (current[:1].isdigit() and "." in current)


async def _run_subprocess_offloop(
    args: list[str] | tuple[str, ...],
    *,
    timeout: float | None = None,
    stdout=None,
    stderr=None,
    text: bool = False,
) -> subprocess.CompletedProcess:
    """Run short utility subprocesses in a worker thread.

    On macOS, `asyncio.create_subprocess_exec()` performs the fork/exec setup on
    the event-loop thread before the awaitable yields. These small tmux/ps/CLI
    calls are frequent enough that samples can catch the main loop in
    `_posixsubprocess`. Keep process creation off-loop.
    """
    return await asyncio.to_thread(
        subprocess.run,
        list(args),
        stdout=stdout,
        stderr=stderr,
        text=text,
        timeout=timeout,
        check=False,
    )


async def _tmux_pane_pid(tmux_pane: str | None) -> int | None:
    if not tmux_pane:
        return None
    try:
        proc = await _run_subprocess_offloop(
            ("tmux", "display-message", "-t", tmux_pane, "-p", "#{pane_pid}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        raw = proc.stdout.decode().strip()
        return int(raw) if raw else None
    except Exception:
        return None


async def _tmux_pane_has_agent_process(tmux_pane: str | None, engine: str) -> bool:
    """Detect live agents hidden below a pane shell.

    Codex dispatch often leaves tmux's pane_current_command as "bash" while
    the actual Codex TUI is a descendant process. GT must not paste a resume
    command into that live prompt.
    """
    pane_pid = await _tmux_pane_pid(tmux_pane)
    if not pane_pid:
        return False
    try:
        proc = await _run_subprocess_offloop(
            ("ps", "-axo", "pid=,ppid=,command="),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=5,
        )
        if proc.returncode != 0:
            return False
    except Exception:
        return False

    children: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    for line in proc.stdout.decode(errors="replace").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        commands[pid] = parts[2].lower()
        children.setdefault(ppid, []).append(pid)

    stack = list(children.get(pane_pid, []))
    seen: set[int] = set()
    needles = ("codex",) if engine == "codex" else ("claude",)
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        command = commands.get(pid, "")
        if any(needle in command for needle in needles):
            return True
        stack.extend(children.get(pid, []))
    return False


async def _tmux_pane_current_command(tmux_pane: str | None) -> str:
    if not tmux_pane:
        return ""
    try:
        proc = await _run_subprocess_offloop(
            ("tmux", "display-message", "-t", tmux_pane, "-p", "#{pane_current_command}"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            timeout=5,
        )
        if proc.returncode != 0:
            return ""
        return proc.stdout.decode(errors="replace").strip()
    except Exception:
        return ""


async def _golden_throne_recovery_blocked_by_stale_pane(instance: dict) -> str | None:
    """Fail closed on startup recovery when the recorded pane is now just a shell.

    Normal stop-hook scheduling still owns fresh GT timers. Startup recovery is
    only a repair path; if the original local pane exists but no longer contains
    the target agent, resuming from that stale row risks driving the wrong pane.
    """
    if instance.get("status") != "stopped":
        return None
    if instance.get("device_id") != LOCAL_DEVICE_NAME:
        return None
    tmux_pane = instance.get("tmux_pane")
    if not tmux_pane:
        return None
    if not await _tmux_pane_exists(tmux_pane):
        return None
    engine = _agent_engine(instance)
    current_cmd = await _tmux_pane_current_command(tmux_pane)
    if _agent_is_alive_command(engine, current_cmd):
        return None
    if await _tmux_pane_has_agent_process(tmux_pane, engine):
        return None
    return "stale_reused_or_empty_pane"


def _agent_resume_command(engine: str, session_id: str, working_dir: str, sop_file: str) -> str:
    quoted_working_dir = shlex.quote(working_dir)
    quoted_session_id = shlex.quote(session_id)
    quoted_sop_file = shlex.quote(sop_file)
    if engine == "codex":
        dispatch_bin = shlex.quote(
            os.environ.get("CODEX_DISPATCH_BIN")
            or str(Path(__file__).resolve().parents[1] / "cli-tools" / "bin" / "codex-dispatch")
        )
        return (
            f"cd {quoted_working_dir} && {dispatch_bin} "
            f"--resume-session {quoted_session_id} "
            f"--launcher golden-throne --launch-mode golden-throne-resume "
            f"{quoted_working_dir} "
            f'"$(cat {quoted_sop_file})"'
        )
    return (
        f'cd {quoted_working_dir} && claude -p "$(cat {quoted_sop_file})" '
        f"--resume {quoted_session_id} --dangerously-skip-permissions"
    )


def _quiet_hour_datetime(local_now: datetime, hour_float: float) -> datetime:
    total_seconds = int(round((hour_float % 24) * 3600)) % 86400
    midnight = datetime.combine(
        local_now.date(),
        datetime.min.time(),
        tzinfo=local_now.tzinfo,
    )
    return midnight + timedelta(seconds=total_seconds)


def _golden_throne_quiet_hours_fire_at(quiet_hours: dict) -> datetime:
    local_now = datetime.fromisoformat(quiet_hours["local_time"])
    quiet_start = float(quiet_hours["quiet_start"])
    quiet_end = float(quiet_hours["quiet_end"])
    hour_float = local_now.hour + local_now.minute / 60 + local_now.second / 3600
    quiet_end_at = _quiet_hour_datetime(local_now, quiet_end)
    if quiet_start > quiet_end and hour_float >= quiet_start:
        quiet_end_at += timedelta(days=1)
    elif quiet_end_at <= local_now:
        quiet_end_at += timedelta(days=1)
    return quiet_end_at + GOLDEN_THRONE_QUIET_HOURS_BUFFER


async def record_golden_throne_resume(instance: dict) -> dict:
    """Increment per-instance GT resume count and enforce on the second resume."""
    session_id = instance["id"]
    now = datetime.now()
    window_started_raw = instance.get("gt_resume_window_started_at")
    try:
        window_started = datetime.fromisoformat(window_started_raw) if window_started_raw else None
    except Exception:
        window_started = None
    if not window_started or now - window_started > GT_RESUME_WINDOW:
        window_started = now
        count = 0
    else:
        count = int(instance.get("gt_resume_count") or 0)
    count += 1
    updates = {
        "gt_resume_count": count,
        "gt_resume_window_started_at": window_started.isoformat(),
        "gt_last_resume_at": now.isoformat(),
    }
    async with aiosqlite.connect(DB_PATH) as db:
        await sanctioned_update_instance(
            db,
            instance_id=session_id,
            updates=updates,
            mutation_type="instance_updated",
            write_source="golden_throne",
            actor="golden-throne-followup",
        )
        await db.commit()

    enforced = count >= GT_ENFORCEMENT_RESUME_THRESHOLD
    result = {
        "resume_count": count,
        "window_started_at": window_started.isoformat(),
        "enforced": enforced,
    }
    await log_event(
        "golden_throne_resume_counted",
        instance_id=session_id,
        details={**result, "threshold": GT_ENFORCEMENT_RESUME_THRESHOLD},
    )
    if enforced:
        tab_name = instance.get("tab_name") or "session"
        pane_surface = (
            instance.get("pane_surface") or instance.get("pane_label") or instance.get("tmux_pane")
        )
        human_surface = instance.get("human_pane_surface") or _golden_throne_human_surface(
            tab_name,
            instance.get("tmux_pane"),
            instance.get("pane_label"),
        )
        payload = _enforcement_state_payload(
            source="golden_throne",
            ack_source="golden_throne",
            trigger="second_resume",
            instance_id=session_id,
            tab_name=tab_name,
            tmux_pane=instance.get("tmux_pane"),
            pane_label=instance.get("pane_label"),
            pane_surface=pane_surface,
            human_pane_surface=human_surface,
            resume_count=count,
        )
        await handle_custodes_state_event(
            "enforcement_cascade_started",
            "golden_throne",
            instance_id=session_id,
            severity=4,
            payload=payload,
        )
        enforcement = await enforce(
            EnforceRequest(
                message=f"Golden Throne second resume: {human_surface}",
                intensity=int(PAVLOK_CONFIG.get("friday_zap_value", 30)),
                source="golden_throne",
            )
        )
        await log_event(
            "golden_throne_second_resume_enforced",
            instance_id=session_id,
            details={**payload, "enforcement": enforcement},
        )
        result["enforcement"] = enforcement
    return result


async def golden_throne_user_activity(instance_id: str, source: str = "prompt_submit") -> dict:
    """Treat real activity in a Golden Throne pane as the authoritative reset."""
    cancelled_writes = await cancel_pending_pane_writes(
        instance_id,
        source="golden_throne",
    )
    try:
        scheduler.remove_job(f"golden-throne-{instance_id}")
    except Exception:
        pass
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={
                "gt_resume_count": 0,
                "gt_resume_window_started_at": None,
                "gt_last_resume_at": now,
            },
            mutation_type="instance_updated",
            write_source="golden_throne",
            actor=f"golden-throne-{source}",
        )
        await db.commit()
    result = {
        "cancelled_pane_writes": cancelled_writes,
        "gt_resume_count": 0,
        "source": source,
    }
    await log_event("golden_throne_user_activity_reset", instance_id=instance_id, details=result)
    return result


def _golden_throne_followup_sync(instance_id: str) -> dict:
    try:
        if APP_LOOP and APP_LOOP.is_running():
            future = asyncio.run_coroutine_threadsafe(golden_throne_followup(instance_id), APP_LOOP)
            future.result(timeout=60)
        else:
            asyncio.run(golden_throne_followup(instance_id))
        return {"success": True, "instance_id": instance_id}
    except Exception as exc:
        logger.exception(f"Golden Throne: scheduled follow-up failed for {instance_id[:12]}")
        return {"success": False, "instance_id": instance_id, "error": str(exc)}


async def _load_instance_session_doc(instance: dict) -> dict:
    """Resolve linked session doc metadata for an instance.

    Returns dict with keys: doc_id, file_path (Path|None), doc_status. Empty
    dict if the instance has no linked doc. Cheap — single DB read, no YAML.
    """
    doc_id = instance.get("session_doc_id")
    if not doc_id:
        return {}
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, file_path, status FROM session_documents WHERE id = ?",
            (doc_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return {}
    fp = Path(row[1]) if row[1] else None
    return {"doc_id": row[0], "file_path": fp, "doc_status": row[2]}


async def _read_instance_session_doc_rubric(
    instance: dict,
) -> tuple[RubricStatus | None, dict]:
    """Read the rubric from the linked session doc, if any.

    Returns (RubricStatus|None, doc_meta_dict). Status is None when no doc is
    linked, the file is missing, or the read fails. Callers should treat
    None as "no rubric" and fall back to legacy GT behavior.
    """
    meta = await _load_instance_session_doc(instance)
    fp = meta.get("file_path")
    if not fp or not fp.exists():
        return None, meta
    try:
        status = await asyncio.to_thread(read_rubric, fp)
    except Exception as exc:
        logger.warning(f"GT: rubric read failed for {fp}: {exc}")
        return None, meta
    return status, meta


def _golden_throne_rubric_state(status: RubricStatus | None) -> str:
    """Classify a RubricStatus into one of four GT dispatch states.

    Returns one of:
      - 'legacy'         → no rubric / scalar-string rubric → fire static SOP
      - 'incomplete'     → rubric present, conditions unmet → adaptive accountability fire
      - 'ready_for_ack'  → rubric complete, Emperor not yet notified → notify-only
      - 'victorious_bug' → rubric complete, Emperor notified, GT still firing → bug-event
      - 'acknowledged'   → Emperor already acked; should never fire (skip)
    """
    if status is None or not status.present or status.legacy_string:
        return "legacy"
    if status.acknowledged_at:
        return "acknowledged"
    if not status.complete:
        return "incomplete"
    if status.notified_at:
        return "victorious_bug"
    return "ready_for_ack"


async def schedule_golden_throne_followup(instance: dict, reason: str = "stop_hook") -> dict:
    """Arm the one-shot Golden Throne follow-up timer for an idle instance.

    The gate is the linked session doc's rubric — specifically whether the
    Emperor has already acknowledged it. Acked docs are archived and never
    re-fire. Pre-ack states (incomplete / ready_for_ack / victorious_bug) all
    schedule normally; the fire callback differentiates them.
    """
    instance_id = instance["id"]
    instance_type = instance.get("instance_type", "one_off")
    zealotry = int(instance.get("zealotry") or 4)
    if instance_type != "golden_throne":
        return {"scheduled": False, "reason": "not_golden_throne"}

    status, doc_meta = await _read_instance_session_doc_rubric(instance)
    rubric_state = _golden_throne_rubric_state(status)
    if rubric_state == "acknowledged" or doc_meta.get("doc_status") == "archived":
        try:
            scheduler.remove_job(f"golden-throne-{instance_id}")
        except Exception:
            pass
        return {
            "scheduled": False,
            "reason": "session_doc_acknowledged",
            "doc_id": doc_meta.get("doc_id"),
        }

    if zealotry < 4:
        try:
            scheduler.remove_job(f"golden-throne-{instance_id}")
        except Exception:
            pass
        return {"scheduled": False, "reason": "zealotry_below_threshold", "zealotry": zealotry}

    delay_seconds = ZEALOTRY_DELAY_MAP.get(zealotry, ZEALOTRY_DELAY_MAP[4])
    original_fire_at = datetime.now() + timedelta(seconds=delay_seconds)
    fire_at = original_fire_at
    quiet_hours = shared.get_quiet_hours_status()
    quiet_hours_shifted = False
    if quiet_hours.get("active"):
        fire_at = _golden_throne_quiet_hours_fire_at(quiet_hours)
        quiet_hours_shifted = True
    scheduler.add_job(
        _golden_throne_followup_sync,
        DateTrigger(run_date=fire_at),
        args=[instance_id],
        id=f"golden-throne-{instance_id}",
        replace_existing=True,
        misfire_grace_time=ENFORCEMENT_JOB_MISFIRE_GRACE_SECONDS,
    )
    details = {
        "zealotry": zealotry,
        "delay_seconds": delay_seconds,
        "fire_at": fire_at.isoformat(),
        "reason": reason,
        "engine": _agent_engine(instance),
        "quiet_hours": quiet_hours,
        "quiet_hours_shifted": quiet_hours_shifted,
        "rubric_state": rubric_state,
        "doc_id": doc_meta.get("doc_id"),
        "missing_conditions": (status.missing if status else None),
    }
    if quiet_hours_shifted:
        details["original_fire_at"] = original_fire_at.isoformat()
    await log_event("golden_throne_scheduled", instance_id=instance_id, details=details)
    logger.info(
        f"Golden Throne: scheduled {instance_id[:12]} zealotry={zealotry} "
        f"delay={delay_seconds}s fire_at={fire_at.isoformat()} "
        f"quiet_hours_shifted={quiet_hours_shifted} rubric_state={rubric_state}"
    )
    return {"scheduled": True, **details}


async def recover_recent_stopped_golden_throne_timers(
    *,
    lookback_minutes: int = 24 * 60,
) -> list[dict]:
    """Arm GT timers for quiet sessions that missed or lost scheduling.

    Golden Throne state is persisted in SQLite, but APScheduler date jobs are a
    runtime concern. A restart, a manual promotion to golden_throne, or a missed
    stop-hook can leave an idle GT instance with no in-memory follow-up job. Use
    a day-scale recovery window so active panes restored from tmux are enforced
    after restart instead of silently sitting idle.
    """
    recovered: list[dict] = []
    now = datetime.now()
    lookback = timedelta(minutes=lookback_minutes)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT ci.*
            FROM claude_instances ci
            LEFT JOIN session_documents sd ON ci.session_doc_id = sd.id
            WHERE ci.status IN ('idle', 'stopped')
              AND ci.instance_type = 'golden_throne'
              AND COALESCE(ci.zealotry, 4) >= 4
              AND (sd.status IS NULL OR sd.status != 'archived')
              AND COALESCE(ci.stopped_at, ci.last_activity) IS NOT NULL
            ORDER BY COALESCE(ci.stopped_at, ci.last_activity) DESC
            """,
        )
        rows = await cursor.fetchall()

    for row in rows:
        instance = dict(row)
        instance_id = instance["id"]
        quiet_at_raw = instance.get("stopped_at") or instance.get("last_activity")
        try:
            quiet_at = datetime.fromisoformat(quiet_at_raw)
        except Exception:
            continue
        if now - quiet_at > lookback:
            continue
        if instance.get("status") == "stopped":
            gt_last_resume_raw = instance.get("gt_last_resume_at")
            try:
                gt_last_resume_at = (
                    datetime.fromisoformat(gt_last_resume_raw) if gt_last_resume_raw else None
                )
            except Exception:
                gt_last_resume_at = None
            if gt_last_resume_at and gt_last_resume_at >= quiet_at:
                continue
        stale_pane_reason = await _golden_throne_recovery_blocked_by_stale_pane(instance)
        if stale_pane_reason:
            await log_event(
                "golden_throne_recovery_skipped_stale_pane",
                instance_id=instance_id,
                details={
                    "reason": stale_pane_reason,
                    "tmux_pane": instance.get("tmux_pane"),
                    "status": instance.get("status"),
                    "quiet_at": quiet_at.isoformat(),
                },
            )
            continue
        if scheduler.get_job(f"golden-throne-{instance_id}"):
            continue
        result = await schedule_golden_throne_followup(instance, reason="startup-recover-quiet")
        if result.get("scheduled"):
            recovered.append({"instance_id": instance_id, **result})

    if recovered:
        await log_event(
            "golden_throne_recovered_stopped_timers",
            details={"count": len(recovered), "instances": recovered},
        )
        logger.info(f"Golden Throne: recovered {len(recovered)} stopped GT timer(s)")
    return recovered


def quiet_hours_status(now: datetime | None = None) -> dict:
    schedule = shared.get_quiet_hours_status(now)
    return {
        **schedule,
        "active": timer_engine.current_mode == TimerMode.QUIET,
        "reason": "quiet_mode"
        if timer_engine.current_mode == TimerMode.QUIET
        else "not_quiet_mode",
        "schedule_active": schedule["active"],
        "timer_mode": timer_engine.current_mode.value,
        "quiet_context": timer_engine.quiet_context,
    }


def is_quiet_hours(now: datetime | None = None) -> bool:
    return bool(quiet_hours_status(now)["active"])


async def log_quiet_hours_suppressed(
    *,
    source: str,
    event_type: str,
    app: str | None = None,
    details: dict | None = None,
) -> dict:
    quiet = quiet_hours_status()
    payload = {
        "source": source,
        "event_type": event_type,
        "app": app,
        "quiet_hours": quiet,
        "timer_state": {
            "mode": timer_engine.current_mode.value,
            "activity": timer_engine.activity.value,
            "productivity_active": timer_engine.productivity_active,
            "break_balance_ms": timer_engine.break_balance_ms,
        },
        **(details or {}),
    }
    await log_event("quiet_hours_suppressed", device_id=source, details=payload)
    return payload


QUIET_RESUME_JOB_ID = "quiet-resume-after-state-buster"


async def enter_quiet_mode_internal(
    *, context: str = "sleeping", source: str = "scheduled_sleep"
) -> dict:
    """Enter first-class timer quiet mode without recording a timer shift."""
    global _current_session_id, _session_start_ms
    now_ms = int(time.monotonic() * 1000)
    old_mode = timer_engine.current_mode.value
    changed, _ = timer_engine.enter_quiet(now_ms, context=context)
    PHONE_STATE["current_app"] = None
    PHONE_STATE["app_opened_at"] = None
    PHONE_STATE["is_distracted"] = False
    PHONE_STATE["twitter_open_since"] = None
    PHONE_STATE["twitter_zapped"] = False
    PHONE_STATE["distraction_ack_app"] = None
    PHONE_STATE["distraction_ack_id"] = None
    PHONE_STATE["last_activity"] = datetime.now().isoformat()
    DESKTOP_STATE["current_mode"] = "silence"
    DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
    stop_enforcement_cascade(reason=f"quiet_enter:{source}")
    if changed:
        today = datetime.now().strftime("%Y-%m-%d")
        if _current_session_id > 0:
            await timer_end_session(_current_session_id, now_ms - _session_start_ms)
        _current_session_id = await timer_start_session("quiet", today)
        _session_start_ms = now_ms
        await timer_log_mode_change(old_mode, "quiet", is_automatic=True)
        await log_event(
            "timer_quiet_entered",
            details={"source": source, "context": context, "old_mode": old_mode},
        )
    try:
        scheduler.remove_job(QUIET_RESUME_JOB_ID)
    except Exception:
        pass
    return {
        "status": timer_engine.current_mode.value,
        "quiet_context": timer_engine.quiet_context,
        "changed": changed,
    }


async def exit_quiet_mode_internal(*, source: str, reason: str) -> dict:
    """Exit quiet mode without recording a normal timer shift."""
    global _current_session_id, _session_start_ms
    if timer_engine.current_mode != TimerMode.QUIET:
        return {"changed": False, "status": timer_engine.current_mode.value}
    now_ms = int(time.monotonic() * 1000)
    old_context = timer_engine.quiet_context
    changed, _ = timer_engine.resume(now_ms)
    timer_engine.set_productivity(False, now_ms)
    new_mode = timer_engine.current_mode.value
    if changed:
        today = datetime.now().strftime("%Y-%m-%d")
        if _current_session_id > 0:
            await timer_end_session(_current_session_id, now_ms - _session_start_ms)
        _current_session_id = await timer_start_session(new_mode, today)
        _session_start_ms = now_ms
        await timer_log_mode_change("quiet", new_mode, is_automatic=True)
        await log_event(
            "timer_quiet_exited",
            details={
                "source": source,
                "reason": reason,
                "old_context": old_context,
                "new_mode": new_mode,
            },
        )
    return {"changed": changed, "status": new_mode, "old_context": old_context}


def _schedule_quiet_resume_after_state_buster() -> None:
    fires_at = datetime.now() + timedelta(hours=1)
    scheduler.add_job(
        _quiet_resume_after_state_buster_sync,
        DateTrigger(run_date=fires_at),
        id=QUIET_RESUME_JOB_ID,
        replace_existing=True,
    )


def _quiet_resume_after_state_buster_sync() -> dict:
    try:
        if APP_LOOP and APP_LOOP.is_running():
            future = asyncio.run_coroutine_threadsafe(
                enter_quiet_mode_internal(context="sleeping", source="state_buster_idle_timeout"),
                APP_LOOP,
            )
            return future.result(timeout=20)
        return asyncio.run(
            enter_quiet_mode_internal(context="sleeping", source="state_buster_idle_timeout")
        )
    except Exception as exc:
        logger.warning(f"Quiet resume after state-buster failed: {exc}")
        return {"success": False, "error": str(exc)}


def _scheduled_quiet_enter_sync() -> dict:
    try:
        if APP_LOOP and APP_LOOP.is_running():
            future = asyncio.run_coroutine_threadsafe(
                enter_quiet_mode_internal(context="sleeping", source="quiet_schedule"),
                APP_LOOP,
            )
            return future.result(timeout=20)
        return asyncio.run(enter_quiet_mode_internal(context="sleeping", source="quiet_schedule"))
    except Exception as exc:
        logger.warning(f"Scheduled quiet entry failed: {exc}")
        return {"success": False, "error": str(exc)}


def _scheduled_quiet_exit_sync() -> dict:
    try:
        if APP_LOOP and APP_LOOP.is_running():
            future = asyncio.run_coroutine_threadsafe(
                exit_quiet_mode_internal(source="quiet_schedule", reason="quiet_end"),
                APP_LOOP,
            )
            return future.result(timeout=20)
        return asyncio.run(exit_quiet_mode_internal(source="quiet_schedule", reason="quiet_end"))
    except Exception as exc:
        logger.warning(f"Scheduled quiet exit failed: {exc}")
        return {"success": False, "error": str(exc)}


async def bust_quiet_state(source: str, event_type: str, details: dict | None = None) -> dict:
    """Leave sleeping quiet because real activity was observed; arm one idle resume."""
    if timer_engine.current_mode != TimerMode.QUIET:
        if scheduler.get_job(QUIET_RESUME_JOB_ID):
            _schedule_quiet_resume_after_state_buster()
            await log_event(
                "quiet_state_buster_activity",
                device_id=source,
                details={
                    "source": source,
                    "event_type": event_type,
                    "resume_after_seconds": 3600,
                    **(details or {}),
                },
            )
            return {"busted": False, "resume_rescheduled": True}
        return {"busted": False}
    before = quiet_hours_status()
    exit_result = await exit_quiet_mode_internal(source=source, reason=event_type)
    _schedule_quiet_resume_after_state_buster()
    await log_event(
        "quiet_state_buster",
        device_id=source,
        details={
            "source": source,
            "event_type": event_type,
            "before": before,
            "exit_result": exit_result,
            "resume_after_seconds": 3600,
            **(details or {}),
        },
    )
    return {"busted": True, "exit_result": exit_result}


def _expected_ack_deadlines(
    now: datetime | None = None,
    *,
    ack_delay: timedelta = EXPECTED_ACK_DEFAULT_ACK_DELAY,
    level2_delay: timedelta = EXPECTED_ACK_DEFAULT_LEVEL2_DELAY,
    pavlok_delay: timedelta = EXPECTED_ACK_DEFAULT_PAVLOK_DELAY,
) -> dict:
    now = now or datetime.now()
    return {
        "created_at": now,
        "ack_due_at": now + ack_delay,
        "level2_due_at": now + level2_delay,
        "pavlok_due_at": now + pavlok_delay,
    }


def _expected_ack_job_id(ack_id: str, level: int) -> str:
    return f"expected-ack-{ack_id}-l{level}"


def _schedule_expected_ack_level(ack_id: str, level: int, due_at: datetime) -> None:
    scheduler.add_job(
        _expected_ack_escalate_sync,
        DateTrigger(run_date=due_at),
        args=[ack_id, level],
        id=_expected_ack_job_id(ack_id, level),
        replace_existing=True,
        misfire_grace_time=ENFORCEMENT_JOB_MISFIRE_GRACE_SECONDS,
    )


def _expected_ack_policy(source: str | None) -> tuple[str, ...]:
    return EXPECTED_ACK_POLICY_BY_SOURCE.get(source or "", EXPECTED_ACK_POLICY_DEFAULT)


def _expected_ack_scheduled_levels(source: str | None) -> tuple[int, ...]:
    return tuple(EXPECTED_ACK_LEVEL_BY_STAGE[stage] for stage in _expected_ack_policy(source))


def _expected_ack_stage_for_level(source: str | None, level: int) -> str | None:
    stage = EXPECTED_ACK_STAGE_BY_LEVEL.get(level)
    if stage not in _expected_ack_policy(source):
        return None
    return stage


def _expected_ack_due_at_for_stage(ack: dict, stage: str) -> str:
    return ack[EXPECTED_ACK_DUE_FIELD_BY_STAGE[stage]]


def _expected_ack_scheduled_stages(source: str | None) -> tuple[dict, ...]:
    return tuple(
        {
            "stage": stage,
            "level": EXPECTED_ACK_LEVEL_BY_STAGE[stage],
            "due_field": EXPECTED_ACK_DUE_FIELD_BY_STAGE[stage],
        }
        for stage in _expected_ack_policy(source)
    )


def _schedule_expected_ack_ladder(
    ack_id: str,
    ack_due_at: str,
    level2_due_at: str,
    pavlok_due_at: str,
    source: str | None = None,
) -> None:
    due_by_field = {
        "ack_due_at": ack_due_at,
        "level2_due_at": level2_due_at,
        "pavlok_due_at": pavlok_due_at,
    }
    for stage in _expected_ack_scheduled_stages(source):
        due_at = due_by_field[stage["due_field"]]
        _schedule_expected_ack_level(ack_id, stage["level"], datetime.fromisoformat(due_at))


def _schedule_expected_ack_remaining(ack: dict) -> None:
    fired_levels = set(ack.get("fired_levels") or [])
    for stage in _expected_ack_scheduled_stages(ack.get("source")):
        level = stage["level"]
        if level not in fired_levels:
            due_at = _expected_ack_due_at_for_stage(ack, stage["stage"])
            _schedule_expected_ack_level(ack["id"], level, datetime.fromisoformat(due_at))


def _cancel_expected_ack_ladder(ack_id: str) -> None:
    for level in (1, 2, 3):
        try:
            scheduler.remove_job(_expected_ack_job_id(ack_id, level))
        except Exception:
            pass


def _snippet(value: bytes | str | None, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = str(value)
    text = text.strip()
    return text[:limit]


async def _tmux_pane_exists(tmux_pane: str | None) -> bool:
    return await shared.tmux_pane_exists(tmux_pane)


PANE_WRITE_PENDING = "pending"
PANE_WRITE_SENT = "sent"
PANE_WRITE_FAILED = "failed"
PANE_WRITE_CANCELLED = "cancelled"
PANE_WRITE_DEFERRED = "deferred"


def _pane_input_line_has_text(line: str) -> bool:
    stripped = line.rstrip()
    if not stripped:
        return False
    if re.search(r"^[\s│░▒▓]*>\s*$", stripped):
        return False
    if re.search(r"[$%#>❯]\s*$", stripped):
        return False
    if not re.search(r"[$%#>❯]", stripped):
        return False
    return True


async def _tmux_pane_has_pending_input(tmux_pane: str) -> bool:
    """Server-owned typing guard for automated pane writes."""
    proc = await _run_subprocess_offloop(
        ("tmux", "capture-pane", "-t", tmux_pane, "-p"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        timeout=5,
    )
    if proc.returncode != 0:
        return False
    lines = [line for line in proc.stdout.decode(errors="replace").splitlines() if line.strip()]
    if not lines:
        return False
    return _pane_input_line_has_text(lines[-1])


async def enqueue_pane_write(
    *,
    instance_id: str,
    tmux_pane: str,
    source: str,
    purpose: str,
    payload: str,
) -> dict:
    if not (tmux_pane or "").strip():
        raise ValueError("pane write requires a concrete tmux pane target")
    queue_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO pane_write_queue (
                id, instance_id, tmux_pane, source, purpose, payload,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (queue_id, instance_id, tmux_pane, source, purpose, payload, now, now),
        )
        await db.commit()
    return {
        "id": queue_id,
        "instance_id": instance_id,
        "tmux_pane": tmux_pane,
        "source": source,
        "purpose": purpose,
        "payload": payload,
        "status": PANE_WRITE_PENDING,
        "created_at": now,
    }


async def cancel_pending_pane_writes(
    instance_id: str,
    *,
    source: str | None = None,
    purpose: str | None = None,
) -> int:
    clauses = ["instance_id = ?", "status = 'pending'"]
    params: list[str] = [instance_id]
    if source:
        clauses.append("source = ?")
        params.append(source)
    if purpose:
        clauses.append("purpose = ?")
        params.append(purpose)
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            f"""
            UPDATE pane_write_queue
            SET status = 'cancelled',
                cancelled_at = ?,
                updated_at = ?,
                last_result_json = ?
            WHERE {" AND ".join(clauses)}
            """,
            (
                now,
                now,
                json.dumps({"cancelled_by": "user_activity"}),
                *params,
            ),
        )
        await db.commit()
        return cursor.rowcount or 0


async def _mark_pane_write(
    queue_id: str,
    *,
    status: str,
    result: dict,
    error: str | None = None,
) -> None:
    now = datetime.now().isoformat()
    sent_at = now if status == PANE_WRITE_SENT else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE pane_write_queue
            SET status = ?,
                updated_at = ?,
                attempted_at = COALESCE(attempted_at, ?),
                sent_at = COALESCE(sent_at, ?),
                last_error = ?,
                last_result_json = ?
            WHERE id = ?
            """,
            (status, now, now, sent_at, error, json.dumps(result), queue_id),
        )
        await db.commit()


async def _tmux_send_payload_then_submit(
    tmux_pane: str,
    payload: str,
    *,
    clear_prompt: bool = False,
) -> dict:
    """Send text and submit through tmuxctl's pane-write primitive."""
    from tmuxctl.tmux_adapter import TmuxAdapter, TmuxError

    adapter = TmuxAdapter()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                adapter.send_text_then_submit,
                tmux_pane,
                payload,
                clear_prompt=clear_prompt,
            ),
            timeout=10,
        )
    except TmuxError as exc:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": str(exc),
            "failed_operation": ["tmuxctl", "send_text_then_submit"],
        }

    import hashlib
    import re
    import uuid

    normalized = re.sub(r"[\r\n]+", " ", payload).rstrip()
    return {
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "operation": "tmuxctl.send_text_then_submit",
        "dispatch_id": str(uuid.uuid4()),
        "payload_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "verification_status": "sent",
        "verified_by": "tmuxctl",
        "pane": tmux_pane,
        "instance_id": None,
    }


async def process_pane_write_queue_once(
    queue_id: str | None = None, *, limit: int = 10
) -> list[dict]:
    """Drain pending automated pane writes that are safe to deliver."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if queue_id:
            cursor = await db.execute(
                "SELECT * FROM pane_write_queue WHERE id = ? AND status = 'pending'",
                (queue_id,),
            )
        else:
            cursor = await db.execute(
                """
                SELECT * FROM pane_write_queue
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            )
        rows = await cursor.fetchall()

    results: list[dict] = []
    for row in rows:
        item = dict(row)
        pane = item["tmux_pane"]
        base = {
            "queue_id": item["id"],
            "instance_id": item["instance_id"],
            "tmux_pane": pane,
            "source": item["source"],
            "purpose": item["purpose"],
        }
        try:
            if await _tmux_pane_has_pending_input(pane):
                result = {**base, "status": PANE_WRITE_PENDING, "reason": "dispatch_deferred"}
                await _mark_pane_write(
                    item["id"],
                    status=PANE_WRITE_PENDING,
                    result=result,
                    error="user_input_pending",
                )
                results.append(result)
                continue
            send_result = await _tmux_send_payload_then_submit(pane, item["payload"])
            result = {
                **base,
                "status": PANE_WRITE_SENT if send_result["returncode"] == 0 else PANE_WRITE_FAILED,
                **send_result,
            }
            await _mark_pane_write(
                item["id"],
                status=result["status"],
                result=result,
                error=result["stderr"] if send_result["returncode"] != 0 else None,
            )
            results.append(result)
        except Exception as exc:
            result = {**base, "status": PANE_WRITE_FAILED, "error": str(exc)}
            await _mark_pane_write(
                item["id"],
                status=PANE_WRITE_FAILED,
                result=result,
                error=str(exc),
            )
            results.append(result)
    return results


def _process_pane_write_queue_sync() -> dict:
    try:
        import sqlite3

        conn = sqlite3.connect(DB_PATH)
        try:
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM pane_write_queue WHERE status = 'pending'"
            ).fetchone()[0]
        finally:
            conn.close()
        if not pending_count:
            return {"success": True, "processed": 0}
        if APP_LOOP and APP_LOOP.is_running():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is APP_LOOP:
                APP_LOOP.create_task(process_pane_write_queue_once())
                return {"success": True, "scheduled": True}
            future = asyncio.run_coroutine_threadsafe(process_pane_write_queue_once(), APP_LOOP)
            results = future.result(timeout=30)
        else:
            results = asyncio.run(process_pane_write_queue_once())
        return {"success": True, "processed": len(results)}
    except Exception as exc:
        logger.warning(f"Pane write queue worker failed: {exc}")
        return {"success": False, "error": str(exc)}


async def _tmux_resolve_pane_id(tmux_pane: str | None) -> str | None:
    return await shared.resolve_tmux_pane_id(tmux_pane)


async def _detect_tmux_agent_panes() -> list[AgentRuntime]:
    pane_rows = await _tmux_pane_rows()
    agents = []
    for pane, command, cwd, window, tty in pane_rows:
        is_agent, engine = await _pane_is_agent(command, cwd, window, tty)
        if not is_agent:
            continue
        agents.append(
            AgentRuntime(
                id=None,
                name=window or engine,
                status="observed",
                engine=engine,
                working_dir=cwd,
                tmux_pane=pane,
                device_id=LOCAL_DEVICE_NAME,
                registered=False,
                live_pane=True,
            )
        )
    return agents


_AGENT_PROCESS_TOKEN_RE = re.compile(r"(?:^|[\s/])(claude|codex)(?:[\s/-]|$)", re.IGNORECASE)


def _agent_engine_for_args(args: str) -> str | None:
    match = _AGENT_PROCESS_TOKEN_RE.search(args)
    return match.group(1).lower() if match else None


async def _tmux_pane_processes(tty: str | None) -> list[str]:
    return await asyncio.to_thread(_tmux_pane_processes_sync, tty)


def _tmux_pane_processes_sync(tty: str | None) -> list[str]:
    if not tty:
        return []
    tty_arg = tty[len("/dev/") :] if tty.startswith("/dev/") else tty
    try:
        result = subprocess.run(
            ["ps", "-t", tty_arg, "-o", "args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


async def _pane_is_agent(
    command: str | None,
    cwd: str | None,
    window: str | None,
    tty: str | None,
) -> tuple[bool, str | None]:
    cmd = (command or "").lower()
    if cmd in ("codex", "claude"):
        return True, cmd
    for line in await _tmux_pane_processes(tty):
        engine = _agent_engine_for_args(line)
        if engine:
            return True, engine
    return False, None


async def _tmux_pane_rows() -> list[tuple[str, str, str, str, str]]:
    return await asyncio.to_thread(_tmux_pane_rows_sync)


def _tmux_pane_rows_sync() -> list[tuple[str, str, str, str, str]]:
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{window_name}\t#{pane_tty}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode != 0:
            return []
    except Exception:
        return []

    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        if len(parts) == 4:
            parts.append("")
        rows.append((parts[0], parts[1], parts[2], parts[3], parts[4]))
    return rows


def _normalize_tty(tty: str | None) -> str | None:
    if not tty:
        return None
    return tty[len("/dev/") :] if tty.startswith("/dev/") else tty


def _agent_engine_by_tty_sync() -> dict[str, str]:
    """Return tty -> agent engine from one ps scan.

    This avoids one `ps -t` fork per tmux pane in the high-frequency work-state
    read model. It intentionally runs in a worker thread via
    `_agent_engine_by_tty()` so subprocess fork/exec cannot park the asyncio
    main thread.
    """
    try:
        result = subprocess.run(
            ["ps", "axo", "tty=,args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0:
            return {}
    except Exception:
        return {}

    engines: dict[str, str] = {}
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        tty, args = parts
        if tty == "??":
            continue
        engine = _agent_engine_for_args(args)
        if engine and tty not in engines:
            engines[tty] = engine
    return engines


async def _agent_engine_by_tty() -> dict[str, str]:
    return await asyncio.to_thread(_agent_engine_by_tty_sync)


def _pane_is_agent_from_snapshot(
    command: str | None,
    tty: str | None,
    agent_engines_by_tty: dict[str, str],
) -> tuple[bool, str | None]:
    cmd = (command or "").lower()
    if cmd in ("codex", "claude"):
        return True, cmd
    tty_key = _normalize_tty(tty)
    engine = agent_engines_by_tty.get(tty_key or "")
    if engine:
        return True, engine
    return False, None


def _resolve_tmux_pane_id_sync(tmux_pane: str | None) -> str | None:
    if not tmux_pane:
        return None
    cli_bin = Path(__file__).resolve().parents[1] / "cli-tools" / "bin" / "tmux-resolve-pane"
    cli_lib = Path(__file__).resolve().parents[1] / "cli-tools" / "lib"
    try:
        result = subprocess.run(
            [str(cli_bin), "--format", "id", tmux_pane],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
            check=False,
            env={
                **os.environ,
                "PYTHONPATH": f"{cli_lib}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            },
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", tmux_pane, "-p", "#{pane_id}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except Exception:
        return None


async def _resolve_tmux_pane_id_for_read_model(tmux_pane: str | None) -> str | None:
    return await asyncio.to_thread(_resolve_tmux_pane_id_sync, tmux_pane)


def _activity_icons() -> list[ActivityIconState]:
    desktop_mode = DESKTOP_STATE.get("current_mode", "silence")
    steam_name = (DESKTOP_STATE.get("steam_app_name") or "").lower()
    steam_exe = (DESKTOP_STATE.get("steam_exe") or "").lower()
    phone_app = (PHONE_STATE.get("current_app") or "").lower()
    phone_mode = PHONE_DISTRACTION_APPS.get(phone_app)

    youtube_active = phone_app in ("youtube", "com.google.android.youtube") or (
        desktop_mode == "video"
    )
    spotify_active = phone_app == "spotify" or desktop_mode == "music"
    mewgenics_active = "mewgenics" in steam_name or "mewgenics" in steam_exe
    gaming_active = desktop_mode == "gaming" or phone_mode == "gaming"

    return [
        ActivityIconState(
            key="youtube", icon="▶", label="YouTube", active=youtube_active, source="phone/desktop"
        ),
        ActivityIconState(
            key="spotify", icon="♪", label="Spotify", active=spotify_active, source="phone/desktop"
        ),
        ActivityIconState(
            key="steam", icon="◉", label="Gaming", active=gaming_active, source="steam/phone"
        ),
        ActivityIconState(
            key="mewgenics",
            icon="M",
            label="Mewgenics",
            active=mewgenics_active,
            source="steam",
        ),
    ]


async def compute_work_state() -> WorkStateResponse:
    now = datetime.now()
    cutoff = now - timedelta(minutes=30)
    active_instances: list[AgentRuntime] = []
    processing_recent_count = 0
    tracked_panes: set[str] = set()
    local_pane_row_list = await _tmux_pane_rows()
    local_pane_rows = {row[0]: row for row in local_pane_row_list}
    agent_engines_by_tty = await _agent_engine_by_tty()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, tab_name, status, engine, working_dir, tmux_pane, device_id, last_activity
            FROM claude_instances
            WHERE status IN ('processing', 'idle')
              AND COALESCE(is_subagent, 0) = 0
            """
        )
        rows = await cursor.fetchall()
        pane_activity_cursor = await db.execute(
            """
            SELECT tmux_pane, MAX(last_activity) AS last_activity
            FROM claude_instances
            WHERE tmux_pane IS NOT NULL
              AND device_id = ?
            GROUP BY tmux_pane
            """,
            (LOCAL_DEVICE_NAME,),
        )
        pane_last_activity: dict[str, str] = {
            r["tmux_pane"]: r["last_activity"]
            for r in await pane_activity_cursor.fetchall()
            if r["last_activity"]
        }

    for row in rows:
        last_activity = row["last_activity"]
        is_recent = False
        try:
            is_recent = datetime.fromisoformat(last_activity) >= cutoff
        except Exception:
            pass
        live_pane = None
        pane_is_agent = None
        canonical_tmux_pane = row["tmux_pane"]
        if row["tmux_pane"] and row["device_id"] == LOCAL_DEVICE_NAME:
            if row["tmux_pane"] in local_pane_rows:
                canonical_tmux_pane = row["tmux_pane"]
            elif str(row["tmux_pane"]).startswith("%"):
                canonical_tmux_pane = None
            else:
                # Rare non-% tmux targets can still be resolved, but do it in a
                # worker thread. The prior path used async subprocess creation in
                # the event loop for every row, which reintroduced the P0
                # fork/exec blocker on cold `/api/timer` polls.
                canonical_tmux_pane = await _resolve_tmux_pane_id_for_read_model(row["tmux_pane"])
            live_pane = canonical_tmux_pane in local_pane_rows
            pane_row = local_pane_rows.get(canonical_tmux_pane)
            if pane_row:
                pane_is_agent, _ = _pane_is_agent_from_snapshot(
                    pane_row[1], pane_row[4], agent_engines_by_tty
                )
            else:
                pane_is_agent = False
        if row["status"] == "processing" and is_recent:
            processing_recent_count += 1
        if row["device_id"] == LOCAL_DEVICE_NAME and not pane_is_agent:
            continue
        if row["device_id"] != LOCAL_DEVICE_NAME and not is_recent:
            continue
        if live_pane is False:
            continue
        if canonical_tmux_pane:
            tracked_panes.add(canonical_tmux_pane)
        active_instances.append(
            AgentRuntime(
                id=row["id"],
                name=row["tab_name"],
                status=row["status"],
                engine=row["engine"],
                working_dir=row["working_dir"],
                tmux_pane=row["tmux_pane"],
                device_id=row["device_id"],
                last_activity=last_activity,
                registered=True,
                live_pane=live_pane,
            )
        )

    observed_agents = []
    for pane, command, cwd, window, tty in local_pane_row_list:
        if pane in tracked_panes:
            continue
        is_agent, engine = _pane_is_agent_from_snapshot(command, tty, agent_engines_by_tty)
        if not is_agent:
            continue
        # Gate observed panes by the same 30-min recency cutoff applied to
        # active_instances. An idle Claude pane that hasn't moved in hours
        # otherwise flickers productivity_active back on every poll cycle.
        # No DB row → treat as not-recent (per observed-agents-recency-cutoff
        # ticket: unregistered panes default to not-recent).
        pane_last_activity_str = pane_last_activity.get(pane)
        if not pane_last_activity_str:
            continue
        try:
            if datetime.fromisoformat(pane_last_activity_str) < cutoff:
                continue
        except Exception:
            continue
        observed_agents.append(
            AgentRuntime(
                id=None,
                name=window or engine,
                status="observed",
                engine=engine,
                working_dir=cwd,
                tmux_pane=pane,
                device_id=LOCAL_DEVICE_NAME,
                last_activity=pane_last_activity_str,
                registered=False,
                live_pane=True,
            )
        )
    productivity_active = bool(active_instances or observed_agents)
    reason = "tracked_or_observed_agent" if productivity_active else "no_live_agent"
    return WorkStateResponse(
        productivity_active=productivity_active,
        reason=reason,
        active_instance_count=len(active_instances),
        processing_recent_count=processing_recent_count,
        observed_agent_count=len(observed_agents),
        active_instances=active_instances,
        observed_agents=observed_agents,
        activity_icons=_activity_icons(),
        timer_mode=timer_engine.current_mode.value,
        activity=timer_engine.activity.value,
        desktop_mode=DESKTOP_STATE.get("current_mode", "silence"),
        phone_app=PHONE_STATE.get("current_app"),
        generated_at=now.isoformat(),
    )


_WORK_STATE_CACHE: dict[str, object] = {"value": None, "monotonic": 0.0}
_WORK_STATE_CACHE_LOCK = asyncio.Lock()
_WORK_STATE_REFRESH_TASK: asyncio.Task | None = None


async def _refresh_work_state_cache() -> None:
    async with _WORK_STATE_CACHE_LOCK:
        value = await compute_work_state()
        _WORK_STATE_CACHE["value"] = value
        _WORK_STATE_CACHE["monotonic"] = time.monotonic()


def _schedule_work_state_refresh() -> None:
    global _WORK_STATE_REFRESH_TASK
    if _WORK_STATE_REFRESH_TASK and not _WORK_STATE_REFRESH_TASK.done():
        return
    _WORK_STATE_REFRESH_TASK = asyncio.create_task(_refresh_work_state_cache())


async def get_cached_work_state(max_age_seconds: float = 1.0) -> WorkStateResponse:
    """Collapse high-frequency dashboard polls onto a short-lived work-state cache.

    `compute_work_state()` shells out to tmux/ps and reads SQLite. Calling it once
    per `/api/timer` poll creates a thundering herd under dashboards. Timer-worker
    internals still call `compute_work_state()` directly when they need a fresh
    sample; HTTP read models can tolerate a sub-second snapshot.
    """
    now = time.monotonic()
    cached = _WORK_STATE_CACHE.get("value")
    cached_at = float(_WORK_STATE_CACHE.get("monotonic") or 0.0)
    if isinstance(cached, WorkStateResponse) and (now - cached_at) <= max_age_seconds:
        return cached
    if isinstance(cached, WorkStateResponse):
        # Stale-while-revalidate for HTTP dashboards. A cold work-state sample can
        # take ~2s when tmux/ps are slow; making the first `/api/timer` caller wait
        # violates the P0 latency gate even though the event loop is not blocked.
        # Return the last snapshot and refresh in the background.
        _schedule_work_state_refresh()
        return cached

    async with _WORK_STATE_CACHE_LOCK:
        now = time.monotonic()
        cached = _WORK_STATE_CACHE.get("value")
        cached_at = float(_WORK_STATE_CACHE.get("monotonic") or 0.0)
        if isinstance(cached, WorkStateResponse) and (now - cached_at) <= max_age_seconds:
            return cached
        if isinstance(cached, WorkStateResponse):
            _schedule_work_state_refresh()
            return cached

        # Startup path: no cached snapshot exists yet, so compute once.
        value = await compute_work_state()
        _WORK_STATE_CACHE["value"] = value
        _WORK_STATE_CACHE["monotonic"] = time.monotonic()
        return value


async def recover_expected_ack_jobs() -> int:
    """Re-arm pending acknowledgement ladders after a Token-API restart."""
    now = datetime.now()
    immediate = now + timedelta(seconds=1)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM expected_acknowledgements WHERE status = 'pending'"
        )
        rows = await cursor.fetchall()

    for row in rows:
        ack = _expected_ack_row_to_dict(row)
        fired_levels = set(ack.get("fired_levels") or [])
        for stage in _expected_ack_scheduled_stages(ack.get("source")):
            level = stage["level"]
            if level in fired_levels:
                continue
            due_at = datetime.fromisoformat(_expected_ack_due_at_for_stage(ack, stage["stage"]))
            _schedule_expected_ack_level(ack["id"], level, immediate if now >= due_at else due_at)

    if rows:
        jobs = [
            {
                "id": job.id,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
            }
            for job in scheduler.get_jobs()
            if job.id.startswith("expected-ack-")
        ]
        logger.info(f"Expected ack recovery: re-armed {len(rows)} pending ack(s): {jobs}")
    return len(rows)


async def create_expected_ack(
    source: str,
    reason: str,
    instance_id: str | None = None,
    details: dict | None = None,
    dedupe_pending: bool = True,
    ack_delay: timedelta = EXPECTED_ACK_DEFAULT_ACK_DELAY,
    level2_delay: timedelta = EXPECTED_ACK_DEFAULT_LEVEL2_DELAY,
    pavlok_delay: timedelta = EXPECTED_ACK_DEFAULT_PAVLOK_DELAY,
) -> dict:
    """Persist an expected acknowledgement and schedule its escalation ladder."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if dedupe_pending and instance_id:
            cursor = await db.execute(
                """
                SELECT * FROM expected_acknowledgements
                WHERE source = ? AND instance_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (source, instance_id),
            )
            existing = await cursor.fetchone()
            if existing:
                ack = _expected_ack_row_to_dict(existing)
                _schedule_expected_ack_remaining(ack)
                return ack

        ack_id = str(uuid.uuid4())
        deadlines = _expected_ack_deadlines(
            ack_delay=ack_delay, level2_delay=level2_delay, pavlok_delay=pavlok_delay
        )
        details_json = json.dumps(details or {})
        await db.execute(
            """
            INSERT INTO expected_acknowledgements (
                id, source, instance_id, reason, status, created_at,
                ack_due_at, level2_due_at, pavlok_due_at, fired_levels_json, details_json
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, '[]', ?)
            """,
            (
                ack_id,
                source,
                instance_id,
                reason,
                deadlines["created_at"].isoformat(),
                deadlines["ack_due_at"].isoformat(),
                deadlines["level2_due_at"].isoformat(),
                deadlines["pavlok_due_at"].isoformat(),
                details_json,
            ),
        )
        await db.commit()

    _schedule_expected_ack_ladder(
        ack_id,
        deadlines["ack_due_at"].isoformat(),
        deadlines["level2_due_at"].isoformat(),
        deadlines["pavlok_due_at"].isoformat(),
        source=source,
    )
    ack = {
        "id": ack_id,
        "source": source,
        "instance_id": instance_id,
        "reason": reason,
        "status": "pending",
        "created_at": deadlines["created_at"].isoformat(),
        "ack_due_at": deadlines["ack_due_at"].isoformat(),
        "level2_due_at": deadlines["level2_due_at"].isoformat(),
        "pavlok_due_at": deadlines["pavlok_due_at"].isoformat(),
        "acknowledged_at": None,
        "bailout_reason": None,
        "fired_levels": [],
        "details": details or {},
    }
    await log_event("expected_ack_created", instance_id=instance_id, details=ack)
    return ack


def _expected_ack_row_to_dict(row) -> dict:
    details_raw = row["details_json"] if "details_json" in row.keys() else None
    fired_raw = row["fired_levels_json"] if "fired_levels_json" in row.keys() else None
    try:
        details = json.loads(details_raw) if details_raw else {}
    except Exception:
        details = {}
    try:
        fired_levels = json.loads(fired_raw) if fired_raw else []
    except Exception:
        fired_levels = []
    return {
        "id": row["id"],
        "source": row["source"],
        "instance_id": row["instance_id"],
        "reason": row["reason"],
        "status": row["status"],
        "created_at": row["created_at"],
        "ack_due_at": row["ack_due_at"],
        "level2_due_at": row["level2_due_at"],
        "pavlok_due_at": row["pavlok_due_at"],
        "acknowledged_at": row["acknowledged_at"],
        "bailout_reason": row["bailout_reason"],
        "fired_levels": fired_levels,
        "details": details,
    }


async def _update_expected_ack_details(ack_id: str, details: dict) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE expected_acknowledgements
            SET details_json = ?
            WHERE id = ?
            """,
            (json.dumps(details), ack_id),
        )
        await db.commit()


async def _find_expected_ack(
    ack_id: str | None, source: str | None, instance_id: str | None
) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if ack_id:
            cursor = await db.execute(
                "SELECT * FROM expected_acknowledgements WHERE id = ?", (ack_id,)
            )
        elif source and instance_id:
            cursor = await db.execute(
                """
                SELECT * FROM expected_acknowledgements
                WHERE source = ? AND instance_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (source, instance_id),
            )
        else:
            return None
        row = await cursor.fetchone()
    return _expected_ack_row_to_dict(row) if row else None


async def _resolve_expected_ack(
    *,
    ack_id: str | None,
    source: str | None,
    instance_id: str | None,
    status: str,
    bailout_reason: str | None = None,
) -> dict:
    ack = await _find_expected_ack(ack_id, source, instance_id)
    if not ack:
        raise HTTPException(status_code=404, detail="pending acknowledgement not found")
    if ack["status"] != "pending":
        return {"updated": False, "ack": ack}

    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if status == "acknowledged":
            await db.execute(
                """
                UPDATE expected_acknowledgements
                SET status = ?, acknowledged_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, now, ack["id"]),
            )
        else:
            await db.execute(
                """
                UPDATE expected_acknowledgements
                SET status = ?, acknowledged_at = ?, bailout_reason = ?
                WHERE id = ? AND status = 'pending'
                """,
                (status, now, bailout_reason, ack["id"]),
            )
        await db.commit()

    _cancel_expected_ack_ladder(ack["id"])
    ack = await _find_expected_ack(ack["id"], None, None)
    event_type = (
        "expected_ack_acknowledged" if status == "acknowledged" else "expected_ack_bailed_out"
    )
    await log_event(event_type, instance_id=ack["instance_id"], details=ack)
    return {"updated": True, "ack": ack}


async def acknowledge_pending_acks_for_instance(instance_id: str, source: str | None = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if source:
            cursor = await db.execute(
                """
                SELECT id FROM expected_acknowledgements
                WHERE instance_id = ? AND source = ? AND status = 'pending'
                """,
                (instance_id, source),
            )
        else:
            cursor = await db.execute(
                """
                SELECT id FROM expected_acknowledgements
                WHERE instance_id = ? AND status = 'pending'
                """,
                (instance_id,),
            )
        rows = await cursor.fetchall()

    count = 0
    for row in rows:
        result = await _resolve_expected_ack(
            ack_id=row["id"], source=None, instance_id=None, status="acknowledged"
        )
        if result["updated"]:
            count += 1
    return count


async def acknowledge_pending_work_action_acks() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id FROM expected_acknowledgements
            WHERE status = 'pending'
              AND source IN ('phone_distraction', 'backlog_violation')
            """
        )
        rows = await cursor.fetchall()

    count = 0
    for row in rows:
        result = await _resolve_expected_ack(
            ack_id=row["id"], source=None, instance_id=None, status="acknowledged"
        )
        if result.get("updated"):
            count += 1
    return count


async def acknowledge_backlog_surface_acks(surface: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id FROM expected_acknowledgements
            WHERE status = 'pending'
              AND source = 'backlog_violation'
              AND json_extract(details_json, '$.surface') = ?
            """,
            (surface,),
        )
        rows = await cursor.fetchall()

    count = 0
    for row in rows:
        result = await _resolve_expected_ack(
            ack_id=row["id"], source=None, instance_id=None, status="acknowledged"
        )
        if result.get("updated"):
            count += 1
    return count


async def _terminal_backlog_ack_for_active_span(
    instance_id: str, active_since: str | None
) -> dict | None:
    """Return the latest terminal backlog ack for the same still-open distraction span."""
    if not active_since:
        return None
    try:
        active_since_dt = datetime.fromisoformat(active_since)
    except Exception:
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM expected_acknowledgements
            WHERE source = 'backlog_violation'
              AND instance_id = ?
              AND status IN ('acknowledged', 'bailed_out', 'expired', 'blocked_by_guardrail')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (instance_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return None

    ack = _expected_ack_row_to_dict(row)
    details = ack.get("details") or {}
    if details.get("active_since") == active_since:
        return ack
    try:
        if datetime.fromisoformat(ack["created_at"]) >= active_since_dt:
            return ack
    except Exception:
        pass
    return None


async def _mark_expected_ack_level_fired(ack: dict, level: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT fired_levels_json
            FROM expected_acknowledgements
            WHERE id = ? AND status = 'pending'
            """,
            (ack["id"],),
        )
        row = await cursor.fetchone()
        if not row:
            await db.rollback()
            return False
        try:
            fired_levels = set(json.loads(row["fired_levels_json"] or "[]"))
        except Exception:
            fired_levels = set()
        if level in fired_levels:
            await db.rollback()
            return False
        fired_levels.add(level)
        await db.execute(
            """
            UPDATE expected_acknowledgements
            SET fired_levels_json = ?
            WHERE id = ? AND status = 'pending'
            """,
            (json.dumps(sorted(fired_levels)), ack["id"]),
        )
        await db.commit()
    ack["fired_levels"] = sorted(fired_levels)
    return True


async def _expected_ack_escalate(ack_id: str, level: int) -> dict:
    ack = await _find_expected_ack(ack_id, None, None)
    if not ack or ack["status"] != "pending":
        return {"skipped": True, "reason": "not_pending", "ack_id": ack_id, "level": level}
    stage = _expected_ack_stage_for_level(ack["source"], level)
    if stage is None:
        result = {
            "skipped": True,
            "reason": "stage_not_in_policy",
            "ack_id": ack_id,
            "level": level,
            "source": ack["source"],
        }
        await log_event(
            "expected_ack_stage_skipped",
            instance_id=ack["instance_id"],
            details={"ack": ack, "level": level, "result": result},
        )
        return result
    if not await _mark_expected_ack_level_fired(ack, level):
        return {"skipped": True, "reason": "level_already_fired", "ack_id": ack_id, "level": level}

    ack["escalation_level"] = level
    ack["escalation_stage"] = stage

    if is_quiet_hours():
        suppression = await log_quiet_hours_suppressed(
            source=ack["source"],
            event_type="expected_ack_escalation",
            details={"ack": ack, "level": level},
        )
        await log_event(
            "expected_ack_escalated",
            instance_id=ack["instance_id"],
            details={
                "ack": ack,
                "level": level,
                "suppressed": True,
                "reason": "quiet_hours",
                "quiet_hours": suppression["quiet_hours"],
            },
        )
        return {
            "skipped": True,
            "reason": "quiet_hours",
            "ack_id": ack_id,
            "level": level,
            "quiet_hours": suppression["quiet_hours"],
        }

    message = f"Expected acknowledgement missed: {ack['reason']}"
    ack_details = ack.get("details") or {}
    derived_ack_surface = None
    if ack_details.get("tab_name") or ack_details.get("tmux_pane") or ack_details.get("pane_label"):
        derived_ack_surface = _format_human_pane_surface(
            ack_details.get("tab_name"),
            ack_details.get("tmux_pane"),
            ack_details.get("pane_label"),
        )
    ack_surface = (
        _sanitize_human_surface(ack_details.get("human_pane_surface"))
        or _sanitize_human_surface(derived_ack_surface)
        or _sanitize_human_surface(ack_details.get("pane_surface"))
        or _sanitize_human_surface(ack_details.get("pane_label"))
    )
    ack_due_text = f"Ack due: {ack_surface}" if ack_surface else "Ack due"
    ack_overdue_text = f"Ack overdue: {ack_surface}" if ack_surface else "Ack overdue"
    if ack["source"] == "backlog_violation" and level == 1:
        desktop_result = None
        if DESKTOP_STATE.get("current_mode") in ("video", "scrolling", "gaming"):
            desktop_result = close_distraction_windows()
        result = await enforce(
            EnforceRequest(
                message=f"{message}. Backlog violation.",
                intensity=25,
                source=ack["source"],
            )
        )
        result = {"enforce": result, "desktop": desktop_result}
    elif ack["source"] == "backlog_violation" and level == 2:
        result = await enforce(
            EnforceRequest(
                message=f"{message} (backlog parry expired)",
                intensity=25,
                source=ack["source"],
            )
        )
    elif level == 1:
        result = await dispatch_notification(
            NotifyRequest(message=ack_due_text or message, type="tts")
        )
    elif level == 2:
        if ack["source"] == "desktop_gaming":
            await asyncio.to_thread(enforce_desktop_app, "mewgenics", "minimize")
        result = await enforce(
            EnforceRequest(
                message=ack_overdue_text or message,
                intensity=25,
                source=ack["source"],
            )
        )
    else:
        if ack["source"] == "backlog_violation" and not _backlog_distraction_still_active(ack):
            resolved = await _resolve_expected_ack(
                ack_id=ack_id,
                source=None,
                instance_id=None,
                status="acknowledged",
            )
            return {
                "skipped": True,
                "reason": "backlog_distraction_resolved",
                "ack_id": ack_id,
                "level": level,
                "result": resolved,
            }
        pavlok_result = await asyncio.to_thread(
            send_pavlok_stimulus,
            "zap",
            PAVLOK_CONFIG.get("friday_zap_value", 30),
            f"expected_ack_{ack['source']}",
            True,
        )
        status = "blocked_by_guardrail" if pavlok_result.get("blocked_by_guardrail") else "expired"
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE expected_acknowledgements SET status = ? WHERE id = ? AND status = 'pending'",
                (status, ack_id),
            )
            await db.commit()
        _cancel_expected_ack_ladder(ack_id)
        result = {"pavlok": pavlok_result, "final_status": status}

    await log_event(
        "expected_ack_escalated",
        instance_id=ack["instance_id"],
        details={"ack": ack, "level": level, "result": result},
    )
    await handle_custodes_state_event(
        "expected_ack_escalated",
        ack["source"],
        instance_id=ack["instance_id"],
        severity=min(2 + level, 5),
        payload={
            "ack_id": ack_id,
            "level": level,
            "reason": ack.get("reason"),
            "app": (ack.get("details") or {}).get("steam_app_name")
            or (ack.get("details") or {}).get("game"),
        },
    )
    return {"ack_id": ack_id, "level": level, "result": result}


def _expected_ack_escalate_sync(ack_id: str, level: int) -> dict:
    try:
        if APP_LOOP and APP_LOOP.is_running():
            future = asyncio.run_coroutine_threadsafe(
                _expected_ack_escalate(ack_id, level), APP_LOOP
            )
            return future.result(timeout=30)
        return asyncio.run(_expected_ack_escalate(ack_id, level))
    except Exception as e:
        logger.warning(f"Expected ack escalation failed for {ack_id} L{level}: {e}")
        return {"success": False, "error": str(e)}


def _ack_current_level(ack: dict, now: datetime | None = None) -> int:
    now = now or datetime.now()
    if ack["status"] != "pending":
        return 0
    current_level = 0
    for stage in _expected_ack_scheduled_stages(ack.get("source")):
        if now >= datetime.fromisoformat(_expected_ack_due_at_for_stage(ack, stage["stage"])):
            current_level = max(current_level, stage["level"])
    if current_level:
        return current_level
    return 0


def _load_golden_throne_sop() -> str:
    """Default SOP for Golden Throne follow-ups.

    Per-instance custom SOPs use the follow_up_sop column instead.
    """
    return (
        "Read your session doc. Assess what remains. "
        "Act if clear, escalate if blocked. Update session doc. "
        "Do not just say 'victory' in-thread; victory is an API/session-doc "
        "state transition. If done, call POST $TOKEN_API_URL/api/session-docs/"
        "<doc_id>/victory-ack or POST $TOKEN_API_URL/api/instances/"
        "<instance_id>/victory. If Golden Throne pings are wrong for this "
        "thread, disable them by setting the instance to one_off: PATCH "
        "$TOKEN_API_URL/api/instances/<instance_id>/type with "
        '{"instance_type":"one_off"}. Do not allow yourself to be '
        "Sisyphus-looped; either make measurable progress, escalate, disable "
        "Golden Throne for this thread, or perform the victory state transition "
        "so usage limits are not burned."
    )


async def _tmux_pane_label(tmux_pane: str | None) -> str | None:
    return await asyncio.to_thread(_tmux_pane_label_sync, tmux_pane)


def _tmux_pane_label_sync(tmux_pane: str | None) -> str | None:
    if not tmux_pane:
        return None
    try:
        result = subprocess.run(
            ["tmux", "show-options", "-pv", "-t", tmux_pane, "@PANE_ID"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode == 0:
            label = result.stdout.strip()
            return label or None
    except Exception as exc:
        logger.debug(f"Golden Throne: pane label lookup failed for {tmux_pane}: {exc}")
    return None


_PANE_LABEL_REPAIR_TASK: asyncio.Task | None = None
_PANE_LABEL_REPAIR_LAST_MONOTONIC = 0.0
_PANE_LABEL_REPAIR_MIN_INTERVAL_SECONDS = 30.0


def _schedule_pane_label_repair(candidates: list[dict]) -> None:
    """Best-effort pane-label repair outside high-traffic read requests.

    `/api/instances` used to run tmux subprocesses and DB writes while its
    aiosqlite connection was open. Under polling load that amplified lock waits
    and subprocess thread growth. Keep the read endpoint pure; repair labels in a
    debounced background task.
    """
    global _PANE_LABEL_REPAIR_TASK, _PANE_LABEL_REPAIR_LAST_MONOTONIC

    candidates = [
        {"id": c.get("id"), "tmux_pane": c.get("tmux_pane")}
        for c in candidates
        if c.get("id") and c.get("tmux_pane")
    ][:20]
    if not candidates:
        return
    if _PANE_LABEL_REPAIR_TASK and not _PANE_LABEL_REPAIR_TASK.done():
        return
    now = time.monotonic()
    if now - _PANE_LABEL_REPAIR_LAST_MONOTONIC < _PANE_LABEL_REPAIR_MIN_INTERVAL_SECONDS:
        return
    _PANE_LABEL_REPAIR_LAST_MONOTONIC = now
    _PANE_LABEL_REPAIR_TASK = asyncio.create_task(_repair_missing_pane_labels(candidates))


async def _repair_missing_pane_labels(candidates: list[dict]) -> None:
    for candidate in candidates:
        instance_id = candidate["id"]
        tmux_pane = candidate["tmux_pane"]
        try:
            pane_label = await _tmux_pane_label(tmux_pane)
            if not pane_label:
                continue
            async with aiosqlite.connect(DB_PATH) as db:
                await sanctioned_update_instance(
                    db,
                    instance_id=instance_id,
                    updates={"pane_label": pane_label},
                    mutation_type="instance_updated",
                    write_source="api",
                    actor="background-pane-label-repair",
                )
                await db.commit()
        except Exception as exc:
            logger.debug(f"Pane label background repair failed for {tmux_pane}: {exc}")


def _golden_throne_surface(tab_name: str, tmux_pane: str | None, pane_label: str | None) -> str:
    if pane_label:
        return pane_label
    if tmux_pane and not str(tmux_pane).startswith("%"):
        return tmux_pane
    return tab_name


def _golden_throne_human_surface(
    tab_name: str, tmux_pane: str | None, pane_label: str | None
) -> str:
    """Human-spoken pane name: '<position> <name>' when both are available."""
    return _format_human_pane_surface(tab_name, tmux_pane, pane_label)


def _is_meaningful_tab_name(tab_name: str | None) -> bool:
    """True if tab_name has been set to something more useful than the default."""
    return _is_meaningful_surface_name(tab_name)


def _golden_throne_tts_text(tab_name: str | None, human_pane_surface: str) -> str:
    """Spoken GT resume notification body; surface already includes name when available."""
    return f"Golden Throne resuming {human_pane_surface}"


def _golden_throne_banner_text(tab_name: str | None, human_pane_surface: str) -> str:
    """On-screen GT resume banner; surface already includes name when available."""
    return f"GT resume: {human_pane_surface}"


def _humanize_condition_key(key: str) -> str:
    """Turn a frontmatter condition key into a short spoken phrase."""
    if key.startswith("legacy:"):
        return "session"
    return key.replace("_", " ")


def _golden_throne_tts_text_for_rubric(human_pane_surface: str, status: RubricStatus) -> str:
    """Spoken GT body for an incomplete-rubric fire. ~50 char SAPI cap (see project_tts_wsl_sapi_truncation)."""
    if not status.missing:
        return f"Golden Throne {human_pane_surface}"
    first = _humanize_condition_key(status.missing[0])
    return f"GT {human_pane_surface} needs {first}"


def _golden_throne_banner_text_for_rubric(human_pane_surface: str, status: RubricStatus) -> str:
    """On-screen banner for an incomplete-rubric fire."""
    if not status.missing:
        return f"GT {human_pane_surface}"
    head = ", ".join(_humanize_condition_key(m) for m in status.missing[:3])
    return f"GT {human_pane_surface}: missing {head}"


def _golden_throne_ready_for_ack_tts(human_pane_surface: str) -> str:
    """Notify-only TTS when rubric just went complete."""
    return f"{human_pane_surface} ready for ack"


def _golden_throne_ready_for_ack_banner(human_pane_surface: str) -> str:
    return f"GT {human_pane_surface}: ready for victory-ack"


def _golden_throne_victorious_bug_tts(human_pane_surface: str) -> str:
    """TTS when GT hit a victorious instance — bug-event, ack or clear."""
    return f"GT bug on {human_pane_surface}, ack or clear"


def _golden_throne_victorious_bug_banner(human_pane_surface: str) -> str:
    return f"GT bug: {human_pane_surface} victorious but unacked"


def _golden_throne_accountability_prompt(status: RubricStatus, doc_path: Path | None) -> str:
    """Persona instruction body injected into the agent's pane.

    Names specific unmet conditions and the action ladder. Mirrors the
    aspirant-persona dispatch-boundary framing — the rubric is the contract,
    silently rolling over is not an option.
    """
    missing_list = ", ".join(f"`{m}`" for m in status.missing) or "(rubric not yet complete)"
    doc_line = f"  {doc_path}\n\n" if doc_path else "\n"
    return (
        f"Golden Throne accountability check for this session doc.\n"
        f"{doc_line}"
        f"Unmet conditions: {missing_list}.\n"
        f"This session is not done. Either:\n"
        f"  1. Address the unmet condition and flip its frontmatter flag, or\n"
        f"  2. Escalate to Emperor via /api/notify if you are blocked, or\n"
        f"  3. Mark inapplicable conditions in `{status.rubric_key}_skip` "
        f"(with justification in the doc body).\n\n"
        f"Declaring victory is not an in-thread action: do not merely write "
        f"'victory' or a completion claim. Victory must be recorded through "
        f"the API/session-doc state transition: POST "
        f"$TOKEN_API_URL/api/session-docs/<doc_id>/victory-ack, or the legacy "
        f"POST $TOKEN_API_URL/api/instances/<instance_id>/victory if no doc id "
        f"is available.\n"
        f"To disable Golden Throne pings for this thread, set the instance to "
        f"one_off: PATCH $TOKEN_API_URL/api/instances/<instance_id>/type with "
        f'{{"instance_type":"one_off"}}.\n'
        f"Do not allow yourself to be Sisyphus-looped. Either make measurable "
        f"progress, escalate, disable Golden Throne for this thread, or perform "
        f"the victory state transition so usage limits are not burned.\n\n"
        f"Silently rolling over is not an option. The session doc is the contract."
    )


async def _get_or_create_legion_pane() -> str:
    """Allocate a managed legion worker pane for an autonomous resume.

    Pane allocation is delegated to the typed tmuxctl stack primitive so
    Custodes remains the left orchestrator and autonomous resume panes file
    into the right-side legion worker stack.
    """
    tmuxctl = SCRIPTS_DIR / "cli-tools" / "bin" / "tmuxctl"
    proc = await asyncio.create_subprocess_exec(
        str(tmuxctl),
        "stack",
        "add",
        "legion",
        "--session",
        "main",
        "--cwd",
        str(Path.home()),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(
            f"failed to allocate managed legion worker pane: "
            f"{stderr.decode(errors='replace').strip()}"
        )
    pane_id = stdout.decode().strip().splitlines()[0] if stdout.decode().strip() else ""
    if not pane_id:
        raise RuntimeError("managed legion worker allocation returned empty pane id")
    logger.info(f"Golden Throne: allocated managed legion worker pane {pane_id}")
    return pane_id


async def _log_golden_throne_dispatch_failed(session_id: str, details: dict) -> None:
    await log_event("golden_throne_dispatch_failed", instance_id=session_id, details=details)
    logger.error(
        "Golden Throne: dispatch failed for "
        f"{session_id[:12]} transport={details.get('transport')} rc={details.get('returncode')}"
    )


async def _golden_throne_handle_ready_for_ack(
    instance: dict,
    status: RubricStatus,
    doc_meta: dict,
    human_pane_surface: str,
) -> dict:
    """Notify-only Emperor ping when a session doc just went rubric-complete.

    No send-keys to the instance — a victorious agent should not be hounded.
    Stamp <rubric_key>_notified_at so the next GT fire knows this is the
    second touch and escalates to a bug-event.
    """
    session_id = instance["id"]
    doc_path = doc_meta.get("file_path")
    doc_id = doc_meta.get("doc_id")
    tts_body = _golden_throne_ready_for_ack_tts(human_pane_surface)
    banner_body = _golden_throne_ready_for_ack_banner(human_pane_surface)
    phone_result = None
    try:
        phone_result = await asyncio.to_thread(
            _send_to_phone,
            "/notify",
            {
                "vibe": 30,
                "tts_text": tts_body,
                "banner_text": banner_body,
            },
        )
    except Exception as e:
        phone_result = {"success": False, "error": str(e)}
        logger.warning(f"GT: ready-for-ack notify failed for {session_id[:12]}: {e}")
    if doc_path and doc_path.exists():
        try:
            await asyncio.to_thread(mark_rubric_notified, doc_path)
        except Exception as exc:
            logger.warning(f"GT: failed to stamp notified_at on {doc_path}: {exc}")
    await log_event(
        "golden_throne_ready_for_ack",
        instance_id=session_id,
        details={
            "doc_id": doc_id,
            "doc_path": str(doc_path) if doc_path else None,
            "rubric_key": status.rubric_key,
            "phone_result": phone_result,
            "human_pane_surface": human_pane_surface,
        },
    )
    logger.info(
        f"GT: ready-for-ack notify sent for {session_id[:12]} doc={doc_id} "
        f"(no instance send-keys; awaiting victory-ack)"
    )
    return {"state": "ready_for_ack", "doc_id": doc_id, "notify_result": phone_result}


async def _golden_throne_handle_victorious_bug(
    instance: dict,
    status: RubricStatus,
    doc_meta: dict,
    human_pane_surface: str,
) -> dict:
    """Bug-event enforcement when GT re-fires on a complete-but-unacked rubric.

    Per feedback_no_warnings_only_shocks: this is an atomic Pavlok shock + TTS,
    not a warning. A victorious instance should not be hounded; if GT touches
    one repeatedly, that's a bug for the Emperor to fix (ack or clear), and
    the shock is the prompt to fix it.
    """
    session_id = instance["id"]
    doc_path = doc_meta.get("file_path")
    doc_id = doc_meta.get("doc_id")
    tts_body = _golden_throne_victorious_bug_tts(human_pane_surface)
    banner_body = _golden_throne_victorious_bug_banner(human_pane_surface)
    payload = _enforcement_state_payload(
        source="golden_throne",
        ack_source="golden_throne",
        trigger="victorious_unacked",
        instance_id=session_id,
        tab_name=instance.get("tab_name"),
        tmux_pane=instance.get("tmux_pane"),
        pane_label=instance.get("pane_label"),
        pane_surface=instance.get("pane_surface"),
        human_pane_surface=human_pane_surface,
        doc_id=doc_id,
    )
    await handle_custodes_state_event(
        "enforcement_cascade_started",
        "golden_throne",
        instance_id=session_id,
        severity=4,
        payload=payload,
    )
    enforcement = await enforce(
        EnforceRequest(
            message=tts_body or f"GT bug on victorious {human_pane_surface}",
            intensity=int(PAVLOK_CONFIG.get("friday_zap_value", 30)),
            source="golden_throne",
        )
    )
    await log_event(
        "golden_throne_victorious_bug",
        instance_id=session_id,
        details={
            "doc_id": doc_id,
            "doc_path": str(doc_path) if doc_path else None,
            "rubric_key": status.rubric_key,
            "enforcement": enforcement,
            "human_pane_surface": human_pane_surface,
        },
    )
    logger.warning(
        f"GT: victorious-bug event fired for {session_id[:12]} doc={doc_id} "
        f"— Emperor must ack-or-clear"
    )
    return {"state": "victorious_bug", "doc_id": doc_id, "enforcement": enforcement}


_golden_throne_fire_times: deque[float] = deque()


def _golden_throne_rate_limit_delay(now: float | None = None) -> tuple[float | None, dict]:
    """Return defer delay if Golden Throne is over its rolling fire cap.

    Mirrors the Pavlok cooldown pattern: keep recent fire timestamps in memory,
    drop expired entries at function entry, and only append when the fire is
    allowed to proceed.
    """
    try:
        max_fires = int(os.getenv("GT_MAX_FIRES_PER_WINDOW", "3"))
    except (TypeError, ValueError):
        max_fires = 3
    try:
        window_seconds = int(os.getenv("GT_RATE_WINDOW_SECONDS", "60"))
    except (TypeError, ValueError):
        window_seconds = 60
    max_fires = max(1, max_fires)
    window_seconds = max(1, window_seconds)

    now = now if now is not None else time.time()
    while _golden_throne_fire_times and _golden_throne_fire_times[0] <= now - window_seconds:
        _golden_throne_fire_times.popleft()

    details = {
        "max_fires": max_fires,
        "window_seconds": window_seconds,
        "recent_fires": len(_golden_throne_fire_times),
    }

    if len(_golden_throne_fire_times) >= max_fires:
        oldest = _golden_throne_fire_times[0]
        delay_seconds = max(0.001, window_seconds - (now - oldest))
        details["deferred_seconds"] = delay_seconds
        return delay_seconds, details

    _golden_throne_fire_times.append(now)
    details["recent_fires"] = len(_golden_throne_fire_times)
    return None, details


async def golden_throne_followup(session_id: str):
    """APScheduler callback: wake up an idle Claude instance with SOP prompt."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (session_id,))
        instance = await cursor.fetchone()

    if not instance:
        logger.warning(f"Golden Throne: instance {session_id[:12]} not found, skipping")
        return

    instance = dict(instance)

    # Skip if already processing (user beat us to it)
    # Sync instances are permanently processing — never skip them
    if instance["status"] == "processing" and instance.get("instance_type") != "sync":
        logger.info(f"Golden Throne: {session_id[:12]} already processing, skipping")
        return

    # Skip if victory was declared
    if instance.get("victory_at"):
        logger.info(f"Golden Throne: {session_id[:12]} already declared victory, skipping")
        return

    quiet_hours = shared.get_quiet_hours_status()
    if quiet_hours.get("active"):
        rescheduled = await schedule_golden_throne_followup(
            instance,
            reason="quiet-hours-deferred-dispatch",
        )
        await log_event(
            "golden_throne_dispatch_suppressed_quiet_hours",
            instance_id=session_id,
            details={"quiet_hours": quiet_hours, "rescheduled": rescheduled},
        )
        logger.info(
            f"Golden Throne: suppressed dispatch for {session_id[:12]} during quiet hours; "
            f"rescheduled={rescheduled.get('scheduled')}"
        )
        return

    delay_seconds, rate_details = _golden_throne_rate_limit_delay()
    if delay_seconds is not None:
        fire_at = datetime.now() + timedelta(seconds=delay_seconds)
        scheduler.add_job(
            golden_throne_followup,
            DateTrigger(run_date=fire_at),
            args=[session_id],
            id=f"golden-throne-{session_id}",
            replace_existing=True,
            name=f"Golden Throne follow-up {session_id[:12]}",
            misfire_grace_time=300,
            jobstore="golden_throne",
        )
        logger.info(
            f"Golden Throne: rate limited {session_id[:12]}, deferred "
            f"{delay_seconds:.1f}s until {fire_at.isoformat()}"
        )
        await log_event(
            "gt_fire_deferred",
            instance_id=session_id,
            details={
                "reason": "rate_limit",
                "fire_at": fire_at.isoformat(),
                **rate_details,
            },
        )
        return

    # SOP selection: custom per-instance override, then default.
    # (Sync retrigger path removed — sync instances now self-evaluate via StopValidate.)
    instance_type = instance.get("instance_type", "one_off")
    custom_sop_path = instance.get("follow_up_sop")
    if custom_sop_path:
        expanded = Path(custom_sop_path).expanduser()
        if expanded.exists():
            sop_prompt = expanded.read_text()
            logger.info(f"Golden Throne: using custom SOP {custom_sop_path} for {session_id[:12]}")
        else:
            logger.warning(f"Golden Throne: custom SOP {custom_sop_path} not found, using default")
            sop_prompt = _load_golden_throne_sop()
    else:
        sop_prompt = _load_golden_throne_sop()
    tmux_pane = instance.get("tmux_pane")
    working_dir = instance.get("working_dir") or "~"
    tab_name = instance.get("tab_name", "session")
    engine = _agent_engine(instance)
    pane_label = await _tmux_pane_label(tmux_pane)
    pane_surface = _golden_throne_surface(tab_name, tmux_pane, pane_label)
    human_pane_surface = _golden_throne_human_surface(tab_name, tmux_pane, pane_label)
    instance["pane_label"] = pane_label
    instance["pane_surface"] = pane_surface
    instance["human_pane_surface"] = human_pane_surface

    # Dispatch: local for instances on this machine, satellite for remote
    device_id = instance.get("device_id", LOCAL_DEVICE_NAME)
    followup_id = str(uuid.uuid4())
    dispatch_details = {
        "followup_id": followup_id,
        "pane": tmux_pane,
        "engine": engine,
        "transport": "unknown",
        "target_pane": tmux_pane,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "tmux_pane_exists": False,
        "device_id": device_id,
    }
    if device_id == LOCAL_DEVICE_NAME and tmux_pane:
        # Local delivery — transport detection per Golden Throne spec:
        # Check pane_current_command to decide send-keys vs claude --resume
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "display-message",
                "-t",
                tmux_pane,
                "-p",
                "#{pane_current_command}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            current_cmd = stdout.decode().strip() if proc.returncode == 0 else ""
            dispatch_details["pane_current_command_returncode"] = proc.returncode
            dispatch_details["pane_current_command_stdout"] = _snippet(stdout)
            dispatch_details["pane_current_command_stderr"] = _snippet(stderr)
        except Exception as exc:
            current_cmd = ""
            dispatch_details["pane_current_command_error"] = str(exc)

        agent_alive = _agent_is_alive_command(
            engine, current_cmd
        ) or await _tmux_pane_has_agent_process(tmux_pane, engine)
        dispatch_details["agent_alive"] = agent_alive

        if agent_alive:
            # Agent alive in pane — queue a guarded pane write.
            # send-keys can only reliably deliver short single-line prompts.
            # For long/multi-line SOPs, write to file and send a short read command.
            MAX_SENDKEYS_LEN = 200
            if len(sop_prompt) <= MAX_SENDKEYS_LEN and "\n" not in sop_prompt:
                inject_prompt = sop_prompt
            else:
                sop_file = f"/tmp/golden-throne-sop-{session_id[:8]}.md"
                Path(sop_file).write_text(sop_prompt)
                inject_prompt = (
                    f"Golden Throne follow-up. Run: cat {sop_file} — then execute that SOP."
                )
            try:
                queued = await enqueue_pane_write(
                    instance_id=session_id,
                    tmux_pane=tmux_pane,
                    source="golden_throne",
                    purpose="followup",
                    payload=inject_prompt,
                )
                queue_results = await process_pane_write_queue_once(queued["id"])
                queue_result = queue_results[0] if queue_results else queued
                dispatch_details.update(
                    {
                        "transport": "send-keys",
                        "target_pane": tmux_pane,
                        "returncode": queue_result.get("returncode"),
                        "stdout": queue_result.get("stdout", ""),
                        "stderr": queue_result.get("stderr", ""),
                        "queue_id": queued["id"],
                        "queue_status": queue_result.get("status"),
                        "defer_reason": queue_result.get("reason"),
                        "tmux_pane_exists": await _tmux_pane_exists(tmux_pane),
                    }
                )
                if queue_result.get("status") == PANE_WRITE_SENT:
                    logger.info(
                        f"Golden Throne: follow-up delivered to {session_id[:12]} via send-keys "
                        f"engine={engine} pane={tmux_pane}"
                    )
                elif queue_result.get("reason") == "dispatch_deferred":
                    logger.info(
                        f"Golden Throne: follow-up deferred for {session_id[:12]} "
                        f"because pane has pending user input"
                    )
                else:
                    logger.error(
                        f"Golden Throne: queued send failed for {session_id[:12]}: "
                        f"{queue_result.get('stderr') or queue_result.get('error')}"
                    )
            except Exception as e:
                dispatch_details.update(
                    {
                        "transport": "send-keys",
                        "target_pane": tmux_pane,
                        "error": str(e),
                        "tmux_pane_exists": await _tmux_pane_exists(tmux_pane),
                    }
                )
                logger.error(f"Golden Throne: send-keys failed for {session_id[:12]}: {e}")
        else:
            # Agent not running — resume in a managed legion worker pane with SOP prompt
            try:
                resume_pane = await _get_or_create_legion_pane()
                # Write SOP to temp file (avoids shell escaping issues)
                sop_file = f"/tmp/golden-throne-sop-{session_id[:8]}.md"
                Path(sop_file).write_text(sop_prompt)
                resume_cmd = _agent_resume_command(engine, session_id, working_dir, sop_file)
                queued = await enqueue_pane_write(
                    instance_id=session_id,
                    tmux_pane=resume_pane,
                    source="golden_throne",
                    purpose="followup",
                    payload=resume_cmd,
                )
                queue_results = await process_pane_write_queue_once(queued["id"])
                queue_result = queue_results[0] if queue_results else queued
                dispatch_details.update(
                    {
                        "transport": "resume",
                        "target_pane": resume_pane,
                        "legion_worker_pane": resume_pane,
                        "resume_command": resume_cmd,
                        "returncode": queue_result.get("returncode"),
                        "stdout": queue_result.get("stdout", ""),
                        "stderr": queue_result.get("stderr", ""),
                        "queue_id": queued["id"],
                        "queue_status": queue_result.get("status"),
                        "defer_reason": queue_result.get("reason"),
                        "tmux_pane_exists": await _tmux_pane_exists(resume_pane),
                    }
                )
                if queue_result.get("status") == PANE_WRITE_SENT:
                    logger.info(
                        f"Golden Throne: resumed {session_id[:12]} in managed legion worker "
                        f"pane={resume_pane} via {engine} resume"
                    )
                elif queue_result.get("reason") == "dispatch_deferred":
                    logger.info(
                        f"Golden Throne: resume deferred for {session_id[:12]} "
                        f"because pane has pending user input"
                    )
            except Exception as e:
                dispatch_details.update(
                    {
                        "transport": "resume",
                        "error": str(e),
                        "tmux_pane_exists": False,
                    }
                )
                logger.error(f"Golden Throne: resume failed for {session_id[:12]}: {e}")
    else:
        # Remote delivery via satellite
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"http://{DESKTOP_CONFIG['host']}:{DESKTOP_CONFIG['port']}/golden-throne/followup",
                    json={
                        "session_id": session_id,
                        "tmux_pane": tmux_pane,
                        "working_dir": working_dir,
                        "prompt": sop_prompt,
                        "engine": engine,
                    },
                )
                try:
                    result = resp.json()
                except Exception:
                    result = {"raw_body": resp.text[:500]}
                remote_success = resp.status_code < 400 and result.get("success") is True
                dispatch_details.update(
                    {
                        "transport": result.get("transport", "satellite"),
                        "target_pane": result.get("pane") or result.get("target_pane") or tmux_pane,
                        "returncode": 0 if remote_success else 1,
                        "stdout": _snippet(result),
                        "stderr": "" if resp.status_code < 400 else _snippet(resp.text),
                        "tmux_pane_exists": remote_success,
                        "defer_reason": result.get("reason")
                        if result.get("status") == "deferred"
                        else None,
                        "satellite_status_code": resp.status_code,
                        "satellite_result": result,
                    }
                )
                logger.info(
                    f"Golden Throne: follow-up dispatched for {session_id[:12]} "
                    f"via {result.get('transport', '?')}"
                )
        except Exception as e:
            dispatch_details.update(
                {
                    "transport": "satellite",
                    "error": str(e),
                    "tmux_pane_exists": False,
                }
            )
            logger.error(f"Golden Throne: satellite dispatch failed for {session_id[:12]}: {e}")

    dispatch_ok = (
        dispatch_details.get("returncode") == 0
        and bool(dispatch_details.get("target_pane"))
        and bool(dispatch_details.get("tmux_pane_exists"))
    )
    if dispatch_details.get("defer_reason") == "dispatch_deferred":
        if dispatch_details.get("queue_id"):
            try:
                await _mark_pane_write(
                    dispatch_details["queue_id"],
                    status=PANE_WRITE_CANCELLED,
                    result={
                        "status": PANE_WRITE_CANCELLED,
                        "reason": "dispatch_deferred_rescheduled",
                        "reschedule_source": "golden_throne",
                    },
                    error="dispatch_deferred_rescheduled",
                )
            except Exception as exc:
                dispatch_details["queue_cancel_error"] = str(exc)
        try:
            rescheduled = await schedule_golden_throne_followup(
                instance,
                reason="dispatch-deferred",
            )
            dispatch_details["rescheduled"] = rescheduled
        except Exception as exc:
            dispatch_details["reschedule_error"] = str(exc)
            logger.warning(
                f"Golden Throne: failed to reschedule deferred dispatch "
                f"for {session_id[:12]}: {exc}"
            )
        await log_event(
            "golden_throne_dispatch_deferred",
            instance_id=session_id,
            details=dispatch_details,
        )
        return
    if not dispatch_ok:
        await _log_golden_throne_dispatch_failed(session_id, dispatch_details)
        return

    resume_state = await record_golden_throne_resume(instance)
    dispatch_details["resume_state"] = resume_state
    await log_event(
        "golden_throne_dispatch_validated",
        instance_id=session_id,
        details=dispatch_details,
    )

    phone_result = None
    if instance_type != "sync":
        zealotry = instance.get("zealotry") or 4
        vibe_intensity = min(20 + (zealotry - 4) * 10, 80)  # 20 at z4, 80 at z10
        try:
            phone_result = await asyncio.to_thread(
                _send_to_phone,
                "/notify",
                {
                    "vibe": vibe_intensity,
                    "tts_text": _golden_throne_tts_text(tab_name, human_pane_surface),
                    "banner_text": _golden_throne_banner_text(tab_name, human_pane_surface),
                },
            )
        except Exception as e:
            phone_result = {"success": False, "error": str(e)}
            logger.warning(f"Golden Throne: phone notify failed for {session_id[:12]}: {e}")
        await log_event(
            "golden_throne_resume_notify",
            instance_id=session_id,
            details={
                "followup_id": followup_id,
                "phone_result": phone_result,
                "pane_surface": pane_surface,
                "human_pane_surface": human_pane_surface,
                "resume_count": resume_state["resume_count"],
            },
        )

    await log_event(
        "golden_throne_followup",
        instance_id=session_id,
        details={
            "zealotry": instance.get("zealotry", 4),
            "engine": engine,
            "resume_count": resume_state["resume_count"],
            "enforced": resume_state["enforced"],
            "followup_id": followup_id,
            "dispatch_ack": dispatch_details,
            "phone_result": phone_result,
        },
    )


async def _nudge_instance(instance_id: str, reason: str = "") -> dict:
    """Internal: immediate followup via satellite dispatch (MiniMax escalation path).

    Reuses golden throne satellite infra but fires immediately instead of on a timer.
    Cancels any pending golden throne timer for this instance.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
        instance = await cursor.fetchone()

    if not instance:
        logger.warning(f"Nudge: instance {instance_id[:12]} not found")
        return {"nudged": False, "reason": "instance_not_found"}

    instance = dict(instance)

    if instance["status"] == "processing":
        logger.info(f"Nudge: {instance_id[:12]} already processing, skipping")
        return {"nudged": False, "reason": "already_processing"}

    if instance.get("victory_at"):
        logger.info(f"Nudge: {instance_id[:12]} declared victory, skipping")
        return {"nudged": False, "reason": "victory_declared"}

    # Cancel pending Golden Throne timer (nudge supersedes it)
    try:
        scheduler.remove_job(f"golden-throne-{instance_id}")
        logger.info(f"Nudge: cancelled golden throne timer for {instance_id[:12]}")
    except Exception:
        pass

    tmux_pane = instance.get("tmux_pane")
    working_dir = instance.get("working_dir") or "~"
    tab_name = instance.get("tab_name", instance_id[:8])

    prompt = (
        f"[Evaluator nudge for {tab_name}]\n"
        f"{reason}\n\n"
        f"Address the above finding(s) before continuing. "
        f"If the finding is about instructing the user to do manual actions, "
        f"perform those actions autonomously instead."
    )

    # Deliver prompt: local for instances on this machine, satellite for remote
    device_id = instance.get("device_id", LOCAL_DEVICE_NAME)
    if device_id == LOCAL_DEVICE_NAME and tmux_pane:
        # Local delivery via claude-cmd
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude-cmd",
                "--pane",
                tmux_pane,
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode == 0:
                logger.info(
                    f"Nudge: delivered to {instance_id[:12]} via claude-cmd pane={tmux_pane}"
                )
            else:
                logger.error(
                    f"Nudge: claude-cmd failed for {instance_id[:12]}: {stderr.decode()[:200]}"
                )
                return {"nudged": False, "reason": f"claude-cmd failed: rc={proc.returncode}"}
        except Exception as e:
            logger.error(f"Nudge: local delivery failed for {instance_id[:12]}: {e}")
            return {"nudged": False, "reason": f"local_failed: {e}"}
    else:
        # Remote delivery via WSL satellite
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"http://{DESKTOP_CONFIG['host']}:{DESKTOP_CONFIG['port']}/golden-throne/followup",
                    json={
                        "session_id": instance_id,
                        "tmux_pane": tmux_pane,
                        "working_dir": working_dir,
                        "prompt": prompt,
                    },
                )
                result = resp.json()
                logger.info(
                    f"Nudge: dispatched for {instance_id[:12]} via satellite {result.get('transport', '?')}"
                )
        except Exception as e:
            logger.error(f"Nudge: satellite dispatch failed for {instance_id[:12]}: {e}")
            return {"nudged": False, "reason": f"dispatch_failed: {e}"}

    # Record nudge timestamp for evaluator cooldown
    _recently_nudged[instance_id] = time.time()

    await log_event("nudge_dispatched", instance_id=instance_id, details={"reason": reason[:200]})
    return {"nudged": True, "reason": reason[:200]}


_CUSTODES_STATE_DEBOUNCE_SECONDS = 20 * 60
_custodes_state_debounce: dict[str, dict] = {}
_CUSTODES_CANCEL_REASON = "intervention_canceled_by_negative_edge"


async def _custodes_state_snapshot() -> dict:
    """Small /api/state-style snapshot for Custodes policy prompts."""
    cascade_count_today = 0
    open_panes = 0
    active_threads_count = 0
    active_threads_names: list[str] = []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE created_at > date('now', 'start of day') "
                "AND ("
                "  (event_type='custodes_state_event' "
                "   AND json_extract(details, '$.event_type')='enforcement_cascade_started') "
                "  OR (event_type='enforcement_cascade_start' "
                "      AND NOT EXISTS ("
                "        SELECT 1 FROM events paired "
                "        WHERE paired.event_type='custodes_state_event' "
                "          AND json_extract(paired.details, '$.event_type')='enforcement_cascade_started' "
                "          AND paired.created_at > date('now', 'start of day') "
                "          AND paired.created_at BETWEEN datetime(events.created_at, '-5 seconds') "
                "                                  AND datetime(events.created_at, '+5 seconds') "
                "          AND COALESCE(json_extract(paired.details, '$.payload.app'), '') = "
                "              COALESCE(json_extract(events.details, '$.app'), '')"
                "      ))"
                ")"
            )
            row = await cursor.fetchone()
            if row:
                cascade_count_today = int(row[0] or 0)

            cursor = await db.execute(
                "SELECT tab_name, status FROM claude_instances "
                "WHERE status IN ('processing', 'idle') "
                "AND COALESCE(is_subagent, 0) = 0 "
                "AND device_id = ?",
                (LOCAL_DEVICE_NAME,),
            )
            inst_rows = await cursor.fetchall()
            open_panes = len(inst_rows)
            for tab_name, status in inst_rows:
                if status == "processing":
                    active_threads_count += 1
                    if tab_name:
                        active_threads_names.append(str(tab_name))
    except Exception as e:
        logger.warning(f"_custodes_state_snapshot enrichment failed: {e}")

    return {
        "timer": {
            "current_mode": timer_engine.current_mode.value,
            "break_balance_ms": timer_engine.break_balance_ms,
            "total_work_time_ms": timer_engine.total_work_time_ms,
        },
        "phone": {
            "current_app": PHONE_STATE.get("current_app"),
            "is_distracted": PHONE_STATE.get("is_distracted", False),
            "last_activity": PHONE_STATE.get("last_activity"),
        },
        "desktop": {
            "current_mode": DESKTOP_STATE.get("current_mode", "silence"),
            "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
            "location_zone": DESKTOP_STATE.get("location_zone"),
            "steam_app_id": DESKTOP_STATE.get("steam_app_id"),
            "steam_app_name": DESKTOP_STATE.get("steam_app_name"),
            "steam_exe": DESKTOP_STATE.get("steam_exe"),
        },
        "cascade_count_today": cascade_count_today,
        "open_panes": open_panes,
        "active_threads": {
            "count": active_threads_count,
            "names": active_threads_names,
        },
    }


async def _custodes_state_dedupe_decision(dedupe_key: str, severity: int) -> tuple[bool, str]:
    """Return (suppressed, reason) using memory plus recent event-log history."""
    now = time.time()
    cached = _custodes_state_debounce.get(dedupe_key)
    if cached and now - cached["at"] < _CUSTODES_STATE_DEBOUNCE_SECONDS:
        if severity <= cached.get("severity", 1):
            return True, "memory_debounce"

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """SELECT details
               FROM events
               WHERE event_type = 'custodes_intervention'
                 AND created_at > datetime('now', '-20 minutes')
               ORDER BY created_at DESC
               LIMIT 100"""
        )
        rows = await cursor.fetchall()

    for (details_json,) in rows:
        if not details_json:
            continue
        try:
            details = json.loads(details_json)
        except Exception:
            continue
        if details.get("dedupe_key") != dedupe_key:
            continue
        if not (details.get("delivery") or {}).get("dispatched"):
            continue
        previous_severity = normalize_severity(details.get("severity"))
        if severity <= previous_severity:
            _custodes_state_debounce[dedupe_key] = {"at": now, "severity": previous_severity}
            return True, "event_log_debounce"

    return False, "not_duplicate"


async def _custodes_intervention_negative_edge_cancel_result(
    event: StateEvent,
    intervention,
    *,
    stage: str,
) -> dict | None:
    """Cancel stale intervention delivery after a compliance/negative-edge signal.

    State events can be generated before an app close / ack resolution, then sit
    behind tmux/CLI dispatch work. Re-read the authoritative state immediately
    before delivery so a queued Custodes intervention cannot fire after the user
    has complied.
    """
    payload = event.payload or {}
    cancel_details: dict | None = None

    if event.event_type == "expected_ack_escalated" and payload.get("ack_id"):
        ack = await _find_expected_ack(str(payload["ack_id"]), None, None)
        if not ack:
            cancel_details = {
                "reason": "ack_missing",
                "ack_id": payload.get("ack_id"),
            }
        elif ack.get("status") != EXPECTED_ACK_PENDING:
            cancel_details = {
                "reason": "ack_not_pending",
                "ack_id": ack.get("id"),
                "ack_status": ack.get("status"),
                "ack_source": ack.get("source"),
                "ack_instance_id": ack.get("instance_id"),
            }

    if cancel_details is None:
        return None

    details = {
        **cancel_details,
        "stage": stage,
        "event_type": event.event_type,
        "source": event.source,
        "severity": intervention.severity,
        "dedupe_key": intervention.dedupe_key,
        "payload": payload,
    }
    await log_event(
        _CUSTODES_CANCEL_REASON,
        instance_id=event.instance_id,
        device_id=event.source,
        details=details,
    )
    return {
        "dispatched": False,
        "reason": _CUSTODES_CANCEL_REASON,
        "canceled": True,
        "cancel_details": details,
    }


async def _inject_custodes_prompt_to_pane(
    prompt: str,
    tmux_pane: str,
    *,
    instance_id: str | None = None,
    cancel_check=None,
) -> dict:
    """Inject a Custodes prompt into a known tmux pane."""
    if cancel_check:
        canceled = await cancel_check("pre_pane_inject")
        if canceled:
            return canceled
    try:
        claude_cmd = SCRIPTS_DIR / "cli-tools" / "bin" / "claude-cmd"
        proc = await asyncio.create_subprocess_exec(
            str(claude_cmd),
            "--pane",
            tmux_pane,
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PATH": ":".join(
                    [
                        str(SCRIPTS_DIR / "cli-tools" / "bin"),
                        str(Path.home() / ".local" / "bin"),
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                        os.environ.get("PATH", ""),
                    ]
                ),
            },
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            reason = f"claude-cmd failed: rc={proc.returncode}"
            logger.warning(f"Custodes state hook: {reason}: {stderr.decode()[:200]}")
            return {
                "dispatched": False,
                "reason": reason,
                "instance_id": instance_id,
                "tmux_pane": tmux_pane,
            }
    except Exception as exc:
        logger.warning(f"Custodes state hook: delivery failed: {exc}")
        return {
            "dispatched": False,
            "reason": f"delivery_failed: {exc}",
            "instance_id": instance_id,
            "tmux_pane": tmux_pane,
        }

    logger.info(
        f"Custodes state hook: delivered pane={tmux_pane} instance={instance_id or 'unknown'}"
    )
    return {
        "dispatched": True,
        "reason": "dispatched",
        "instance_id": instance_id,
        "tmux_pane": tmux_pane,
    }


async def _find_custodes_tmux_pane() -> str | None:
    """Recover a live Custodes pane from tmux when DB singleton tracking is stale.

    Uses the `@PANE_ID = legion:custodes` tmux pane option as the identity signal
    — set by `_create_custodes_legion_pane` at pane creation. Pane background
    color is an OUTPUT of legion designation (driven by `pane_recolor_queue`),
    never an input — keying recovery on color creates a circular SoT dependency
    where a missed recolor makes Custodes "disappear" to the dispatcher.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}\t#{@PANE_ID}\t#{pane_current_command}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception as exc:
        logger.warning(f"Custodes state hook: tmux pane recovery failed: {exc}")
        return None

    if proc.returncode != 0:
        return None

    candidates: list[str] = []
    for line in stdout.decode().splitlines():
        try:
            pane_id, pane_marker, current_cmd = line.split("\t", 2)
        except ValueError:
            continue
        if pane_marker != "legion:custodes":
            continue
        cmd_is_claude = "claude" in current_cmd.lower() or (
            current_cmd[0:1].isdigit() and "." in current_cmd
        )
        if cmd_is_claude:
            candidates.append(pane_id)

    return candidates[0] if candidates else None


async def _create_custodes_legion_pane() -> str | None:
    """Create or return the fixed Custodes pane in the local legion window."""
    try:
        exists = await asyncio.create_subprocess_exec(
            "tmux",
            "list-windows",
            "-t",
            "main",
            "-F",
            "#{window_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(exists.communicate(), timeout=5)
        windows = stdout.decode().splitlines() if exists.returncode == 0 else []

        if "legion" in windows:
            list_proc = await asyncio.create_subprocess_exec(
                "tmux",
                "list-panes",
                "-t",
                "main:legion",
                "-F",
                "#{pane_id}\t#{@PANE_ID}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            pane_stdout, pane_stderr = await asyncio.wait_for(list_proc.communicate(), timeout=5)
            if list_proc.returncode != 0:
                logger.warning(
                    f"Custodes state hook: could not inspect legion panes: {pane_stderr.decode()[:200]}"
                )
                return None
            first_pane = ""
            for line in pane_stdout.decode().splitlines():
                try:
                    pane_id, pane_role = line.split("\t", 1)
                except ValueError:
                    continue
                first_pane = first_pane or pane_id
                if pane_role == "legion:custodes":
                    return pane_id
            if first_pane:
                tag_proc = await asyncio.create_subprocess_exec(
                    "tmux",
                    "set-option",
                    "-p",
                    "-t",
                    first_pane,
                    "@PANE_ID",
                    "legion:custodes",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(tag_proc.communicate(), timeout=5)
                type_proc = await asyncio.create_subprocess_exec(
                    "tmux",
                    "set-option",
                    "-p",
                    "-t",
                    first_pane,
                    "@PANE_TYPE",
                    "legion",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(type_proc.communicate(), timeout=5)
                return first_pane
            return None
        else:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "new-window",
                "-t",
                "main",
                "-n",
                "legion",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        pane_stdout, pane_stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            logger.warning(
                f"Custodes state hook: could not create legion pane: {pane_stderr.decode()[:200]}"
            )
            return None
        pane_id = pane_stdout.decode().strip().splitlines()[0]

        tag_proc = await asyncio.create_subprocess_exec(
            "tmux",
            "set-option",
            "-p",
            "-t",
            pane_id,
            "@PANE_ID",
            "legion:custodes",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(tag_proc.communicate(), timeout=5)
        type_proc = await asyncio.create_subprocess_exec(
            "tmux",
            "set-option",
            "-p",
            "-t",
            pane_id,
            "@PANE_TYPE",
            "legion",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(type_proc.communicate(), timeout=5)
        return pane_id
    except Exception as exc:
        logger.warning(f"Custodes state hook: legion pane creation failed: {exc}")
        return None


async def _assert_and_send_custodes(prompt: str, *, source: str) -> dict:
    tmuxctl_bin = SCRIPTS_DIR / "cli-tools" / "bin" / "tmuxctl"

    async def _run_assert() -> tuple[dict, str]:
        proc = await asyncio.create_subprocess_exec(
            str(tmuxctl_bin),
            "assert-instance",
            "--pane",
            "legion:custodes",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
        raw = stdout.decode().strip()
        try:
            result = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            result = {
                "ok": False,
                "reason": f"bad_assert_output rc={proc.returncode}",
                "raw": raw[:200],
            }
        return result, stderr.decode()

    result, stderr = await _run_assert()
    if result.get("action") in {"launched", "persona_correction_sent"}:
        await asyncio.sleep(3)
        result, stderr = await _run_assert()
    if not result.get("ok"):
        logger.warning(
            f"{source}: assert-instance legion:custodes failed: {result.get('reason')} stderr={stderr[:200]}"
        )
        return {
            "dispatched": False,
            "reason": result.get("reason") or "assert_failed",
            "assertion": result,
        }

    proc = await asyncio.create_subprocess_exec(
        str(tmuxctl_bin),
        "send-text",
        "--pane",
        "legion:custodes",
        "--stdin",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr_b = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=45)
    if proc.returncode != 0:
        reason = stderr_b.decode().strip()[:240] or stdout.decode().strip()[:240]
        return {"dispatched": False, "reason": f"send_text_failed: {reason}", "assertion": result}
    return {"dispatched": True, "reason": "sent", "pane": result.get("pane"), "assertion": result}


async def _launch_custodes_for_intervention(prompt: str, *, cancel_check=None) -> dict:
    """Assert `legion:custodes`, then send the intervention only after assertion is true."""
    if cancel_check:
        canceled = await cancel_check("pre_launch_prompt")
        if canceled:
            return canceled

    launch_prompt = (
        "Custodes state hook fired while no live synced Custodes singleton was registered. "
        "Register yourself as legion=custodes, instance_type=sync, synced=true, then handle this intervention.\n\n"
        f"{prompt}"
    )

    try:
        result = await _assert_and_send_custodes(launch_prompt, source="Custodes state hook")
    except Exception as exc:
        logger.warning(f"Custodes state hook: assert/send legion:custodes failed: {exc}")
        return {"dispatched": False, "reason": f"custodes_launch_failed: {exc}"}
    if not result.get("dispatched"):
        logger.warning(f"Custodes state hook: delivery failed: {result.get('reason')}")
    else:
        logger.warning(f"Custodes state hook: delivered pane={result.get('pane')}")
    return result


async def _inject_custodes_prompt_to_pane_maybe_cancel(
    prompt: str,
    tmux_pane: str,
    *,
    instance_id: str | None = None,
    cancel_check=None,
) -> dict:
    inject = _inject_custodes_prompt_to_pane
    try:
        supports_cancel_check = "cancel_check" in inspect.signature(inject).parameters
    except (TypeError, ValueError):
        supports_cancel_check = False
    if supports_cancel_check:
        return await inject(
            prompt,
            tmux_pane,
            instance_id=instance_id,
            cancel_check=cancel_check,
        )

    if cancel_check:
        canceled = await cancel_check("pre_pane_inject")
        if canceled:
            return canceled
    return await inject(prompt, tmux_pane, instance_id=instance_id)


async def _launch_custodes_for_intervention_maybe_cancel(
    prompt: str,
    *,
    cancel_check=None,
) -> dict:
    launch = _launch_custodes_for_intervention
    try:
        supports_cancel_check = "cancel_check" in inspect.signature(launch).parameters
    except (TypeError, ValueError):
        supports_cancel_check = False
    if supports_cancel_check:
        return await launch(prompt, cancel_check=cancel_check)

    if cancel_check:
        canceled = await cancel_check("pre_launch_prompt")
        if canceled:
            return canceled
    return await launch(prompt)


async def _dispatch_custodes_intervention(prompt: str, *, cancel_check=None) -> dict:
    """Inject into Custodes, recovering or launching the singleton if needed."""
    if cancel_check:
        canceled = await cancel_check("pre_dispatch_lookup")
        if canceled:
            return canceled
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, tmux_pane, device_id
               FROM claude_instances
               WHERE legion = 'custodes'
                 AND synced = 1
                 AND status IN ('idle', 'processing')
               ORDER BY last_activity DESC
               LIMIT 1"""
        )
        row = await cursor.fetchone()

    if not row:
        recovered_pane = await _find_custodes_tmux_pane()
        if recovered_pane:
            logger.warning(
                f"Custodes state hook: DB singleton missing; recovered Custodes pane={recovered_pane}"
            )
            delivery = await _inject_custodes_prompt_to_pane_maybe_cancel(
                prompt,
                recovered_pane,
                cancel_check=cancel_check,
            )
            if delivery.get("dispatched"):
                delivery["reason"] = "recovered_tmux_pane"
            return delivery
        logger.warning("Custodes state hook: no live singleton found; launching new Custodes")
        return await _launch_custodes_for_intervention_maybe_cancel(
            prompt,
            cancel_check=cancel_check,
        )

    instance = dict(row)
    tmux_pane = instance.get("tmux_pane")
    if not tmux_pane:
        recovered_pane = await _find_custodes_tmux_pane()
        if recovered_pane:
            logger.warning(
                f"Custodes state hook: DB singleton has no pane; recovered pane={recovered_pane}"
            )
            delivery = await _inject_custodes_prompt_to_pane_maybe_cancel(
                prompt,
                recovered_pane,
                instance_id=instance["id"],
                cancel_check=cancel_check,
            )
            if delivery.get("dispatched"):
                delivery["reason"] = "recovered_tmux_pane"
            return delivery
        logger.warning(
            f"Custodes state hook: {instance['id'][:12]} has no pane; launching replacement"
        )
        return await _launch_custodes_for_intervention_maybe_cancel(
            prompt,
            cancel_check=cancel_check,
        )

    device_id = instance.get("device_id", LOCAL_DEVICE_NAME)
    if device_id != LOCAL_DEVICE_NAME:
        recovered_pane = await _find_custodes_tmux_pane()
        if recovered_pane:
            logger.warning(
                f"Custodes state hook: DB singleton remote ({device_id}); recovered local pane={recovered_pane}"
            )
            delivery = await _inject_custodes_prompt_to_pane_maybe_cancel(
                prompt,
                recovered_pane,
                instance_id=instance["id"],
                cancel_check=cancel_check,
            )
            if delivery.get("dispatched"):
                delivery["reason"] = "recovered_tmux_pane"
            return delivery
        logger.warning(
            f"Custodes state hook: synced singleton remote ({device_id}); launching local replacement"
        )
        return await _launch_custodes_for_intervention_maybe_cancel(
            prompt,
            cancel_check=cancel_check,
        )

    return await _inject_custodes_prompt_to_pane_maybe_cancel(
        prompt,
        tmux_pane,
        instance_id=instance["id"],
        cancel_check=cancel_check,
    )


async def _dispatch_custodes_intervention_maybe_cancel(prompt: str, cancel_check) -> dict:
    """Call the intervention dispatcher with cancellation when supported.

    Tests often monkeypatch `_dispatch_custodes_intervention` with a one-arg
    fake; keep that supported while the production dispatcher receives the
    dispatch-time negative-edge guard.
    """
    dispatch = _dispatch_custodes_intervention
    try:
        supports_cancel_check = "cancel_check" in inspect.signature(dispatch).parameters
    except (TypeError, ValueError):
        supports_cancel_check = False
    if supports_cancel_check:
        return await dispatch(prompt, cancel_check=cancel_check)

    canceled = await cancel_check("pre_dispatch")
    if canceled:
        return canceled
    return await dispatch(prompt)


async def handle_custodes_state_event(
    event_type: str,
    source: str,
    *,
    instance_id: str | None = None,
    severity: int | None = None,
    payload: dict | None = None,
) -> dict:
    """Internal router for state events that may wake Custodes."""
    event = StateEvent(
        event_type=event_type,
        source=source,
        instance_id=instance_id,
        severity=severity,
        payload=payload or {},
    )
    dedupe_key = build_dedupe_key(event)
    normalized_severity = normalize_severity(severity)

    await log_event(
        "custodes_state_event",
        instance_id=instance_id,
        device_id=source,
        details={
            "event_type": event_type,
            "source": source,
            "severity": normalized_severity,
            "dedupe_key": dedupe_key,
            "payload": payload or {},
        },
    )

    intervention = evaluate_state_event(event, await _custodes_state_snapshot())
    if intervention is None:
        return {
            "received": True,
            "intervention_dispatched": False,
            "dedupe_key": dedupe_key,
            "reason": "no_policy_match",
        }

    if is_quiet_hours():
        suppression = await log_quiet_hours_suppressed(
            source=source,
            event_type=f"custodes_state_event:{event_type}",
            details={
                "dedupe_key": intervention.dedupe_key,
                "severity": intervention.severity,
                "payload": payload or {},
            },
        )
        await log_event(
            "custodes_intervention",
            instance_id=instance_id,
            device_id=source,
            details={
                "event_type": intervention.event_type,
                "dedupe_key": intervention.dedupe_key,
                "severity": intervention.severity,
                "audience_instance_id": "custodes",
                "prompt": intervention.prompt,
                "delivery": {
                    "dispatched": False,
                    "reason": "quiet_hours",
                    "quiet_hours": suppression["quiet_hours"],
                },
            },
        )
        return {
            "received": True,
            "intervention_dispatched": False,
            "dedupe_key": intervention.dedupe_key,
            "reason": "quiet_hours",
            "quiet_hours": suppression["quiet_hours"],
        }

    suppressed, dedupe_reason = await _custodes_state_dedupe_decision(
        intervention.dedupe_key,
        intervention.severity,
    )
    if suppressed:
        return {
            "received": True,
            "intervention_dispatched": False,
            "dedupe_key": intervention.dedupe_key,
            "reason": dedupe_reason,
        }

    # Claim the dedupe key BEFORE dispatch to close the TOCTOU window.
    # Without this, two events with the same dedupe_key arriving in quick succession
    # both pass the check above and both dispatch, because the cache is only written
    # after dispatch completes. Writing the claim here means the second event sees
    # the cache hit and is suppressed.
    _custodes_state_debounce[intervention.dedupe_key] = {
        "at": time.time(),
        "severity": intervention.severity,
    }

    async def cancel_check(stage: str) -> dict | None:
        return await _custodes_intervention_negative_edge_cancel_result(
            event,
            intervention,
            stage=stage,
        )

    delivery = await _dispatch_custodes_intervention_maybe_cancel(
        intervention.prompt,
        cancel_check,
    )
    if delivery.get("canceled"):
        return {
            "received": True,
            "intervention_dispatched": False,
            "dedupe_key": intervention.dedupe_key,
            "reason": delivery.get("reason", _CUSTODES_CANCEL_REASON),
            "cancel_details": delivery.get("cancel_details"),
        }
    if delivery.get("dispatched"):
        _custodes_state_debounce[intervention.dedupe_key] = {
            "at": time.time(),
            "severity": intervention.severity,
        }

    await log_event(
        "custodes_intervention",
        instance_id=delivery.get("instance_id") or instance_id,
        device_id=source,
        details={
            "event_type": intervention.event_type,
            "dedupe_key": intervention.dedupe_key,
            "severity": intervention.severity,
            "audience_instance_id": delivery.get("instance_id") or "custodes",
            "prompt": intervention.prompt,
            "delivery": delivery,
        },
    )

    return {
        "received": True,
        "intervention_dispatched": bool(delivery.get("dispatched")),
        "dedupe_key": intervention.dedupe_key,
        "reason": delivery.get("reason", intervention.reason),
    }


@app.post("/api/custodes/state-event")
async def custodes_state_event_endpoint(request: CustodesStateEventRequest):
    """Internal state-hook ingestion point for immediate Custodes interventions."""
    return await handle_custodes_state_event(
        request.event_type,
        request.source,
        instance_id=request.instance_id,
        severity=request.severity,
        payload=request.payload,
    )


@app.post("/api/instances/{instance_id}/nudge")
async def nudge_instance_endpoint(instance_id: str, request: Request):
    """Immediate followup — MiniMax escalation path.

    Fires a satellite dispatch to resume the Claude instance with a contextual
    prompt. Cancels any pending Golden Throne timer. Used by the plan auditor
    swarm when it detects stale session doc state.
    """
    body = await request.json()
    reason = body.get("reason", "Manual nudge")
    return await _nudge_instance(instance_id, reason=reason)


@app.patch("/api/instances/{instance_id}/zealotry")
async def set_zealotry(instance_id: str, request: Request):
    """Set zealotry (follow-up frequency) for an instance. Range 1-10."""
    body = await request.json()
    zealotry = body.get("zealotry")
    if not isinstance(zealotry, int) or zealotry < 1 or zealotry > 10:
        raise HTTPException(status_code=400, detail="zealotry must be integer 1-10")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM claude_instances WHERE id = ?", (instance_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"zealotry": zealotry},
            mutation_type="instance_updated",
            write_source="api",
            actor="zealotry",
        )
        await db.commit()

    # Cancel pending follow-up if zealotry drops below threshold
    timer_cancelled = False
    if zealotry < 4:
        try:
            scheduler.remove_job(f"golden-throne-{instance_id}")
            timer_cancelled = True
        except Exception:
            pass

    logger.info(f"Golden Throne: zealotry={zealotry} for {instance_id[:12]}")
    return {"instance_id": instance_id, "zealotry": zealotry, "timer_cancelled": timer_cancelled}


# ── Legion / Synced Session Endpoints ─────────────────────────

ALLOWED_LEGIONS = {"astartes", "mechanicus", "custodes", "civic", "fabricator"}
SINGLETON_LEGIONS = {"custodes", "fabricator"}


@app.patch("/api/instances/{instance_id}/legion")
async def set_instance_legion(instance_id: str, request: Request):
    """Set the legion for an instance."""
    body = await request.json()
    legion = body.get("legion")
    if legion not in ALLOWED_LEGIONS:
        raise HTTPException(
            status_code=400, detail=f"legion must be one of: {', '.join(sorted(ALLOWED_LEGIONS))}"
        )

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM claude_instances WHERE id = ?", (instance_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"legion": legion},
            mutation_type="instance_updated",
            write_source="api",
            actor="set-legion",
        )
        await db.commit()

    logger.info(f"Legion: {instance_id[:12]} → {legion}")
    return {"instance_id": instance_id, "legion": legion}


@app.patch("/api/instances/{instance_id}/synced")
async def set_instance_synced(instance_id: str, request: Request):
    """Set synced flag for an instance. Enforces one synced session per legion."""
    body = await request.json()
    synced = body.get("synced")
    if synced not in (True, False, 0, 1):
        raise HTTPException(status_code=400, detail="synced must be true or false")
    synced_int = 1 if synced else 0

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, legion FROM claude_instances WHERE id = ?", (instance_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")
        legion = row[1] or "astartes"

        if synced_int:
            # Check for existing synced session in this legion
            cursor = await db.execute(
                "SELECT id FROM claude_instances WHERE legion = ? AND synced = 1 AND status IN ('idle', 'processing') AND id != ?",
                (legion, instance_id),
            )
            conflict = await cursor.fetchone()
            if conflict:
                raise HTTPException(
                    status_code=409,
                    detail=f"Legion '{legion}' already has a synced session: {conflict[0][:12]}",
                )

        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"synced": synced_int},
            mutation_type="instance_updated",
            write_source="api",
            actor="set-synced",
        )
        await db.commit()

    logger.info(f"Synced: {instance_id[:12]} → synced={synced_int} (legion={legion})")
    return {"instance_id": instance_id, "synced": bool(synced_int), "legion": legion}


@app.patch("/api/instances/{instance_id}/discord")
async def set_instance_discord(instance_id: str, request: Request):
    """Set discord_hosted flag and discord_channel for an instance."""
    body = await request.json()
    discord_hosted = body.get("discord_hosted")
    discord_channel = body.get("discord_channel")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM claude_instances WHERE id = ?", (instance_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")

        updates = {}
        if discord_hosted is not None:
            if discord_hosted not in (True, False, 0, 1):
                raise HTTPException(status_code=400, detail="discord_hosted must be true or false")
            updates["discord_hosted"] = 1 if discord_hosted else 0
        if discord_channel is not None:
            updates["discord_channel"] = discord_channel if discord_channel else None

        if not updates:
            raise HTTPException(
                status_code=400, detail="Provide discord_hosted and/or discord_channel"
            )

        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates=updates,
            mutation_type="instance_updated",
            write_source="api",
            actor="discord-linkage",
        )
        await db.commit()

    logger.info(f"Discord: {instance_id[:12]} → hosted={discord_hosted}, channel={discord_channel}")
    return {
        "instance_id": instance_id,
        "discord_hosted": discord_hosted,
        "discord_channel": discord_channel,
    }


@app.get("/api/legion/{legion}/synced-session")
async def get_synced_session(legion: str):
    """Lookup the active synced session for a legion."""
    if legion not in ALLOWED_LEGIONS:
        raise HTTPException(
            status_code=400, detail=f"legion must be one of: {', '.join(sorted(ALLOWED_LEGIONS))}"
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, tab_name, tmux_pane, device_id, legion, status
               FROM claude_instances
               WHERE legion = ? AND synced = 1 AND status IN ('idle', 'processing')
               LIMIT 1""",
            (legion,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"legion": legion, "synced_session": None}
        return {
            "legion": legion,
            "synced_session": dict(row),
        }


@app.get("/api/instances/{instance_id}/zealotry")
async def get_zealotry(instance_id: str):
    """Get zealotry level and timer status for an instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT zealotry, victory_at, victory_reason FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

    zealotry = row["zealotry"] or 4
    job = scheduler.get_job(f"golden-throne-{instance_id}")
    return {
        "instance_id": instance_id,
        "zealotry": zealotry,
        "timer_pending": job is not None,
        "next_fire": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "victory_at": row["victory_at"],
        "victory_reason": row["victory_reason"],
    }


async def _victory_ack_core(
    doc_id: int,
    reason: str,
    deliverables: list[str],
    *,
    force: bool = False,
    source: str = "victory-ack",
) -> dict:
    """Shared core for the victory-ack flow.

    Precondition: rubric must be complete (unless force=True for legacy callers).
    Action: stamp acknowledged_at + reason; archive the doc; cancel GT timers
    on all linked instances; downgrade those instances to one_off. Returns a
    summary dict; raises HTTPException(409) when precondition fails.
    """
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, file_path, title, status FROM session_documents WHERE id = ?",
            (doc_id,),
        )
        doc_row = await cursor.fetchone()
        if not doc_row:
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")
        doc_path = Path(doc_row["file_path"]) if doc_row["file_path"] else None
        doc_title = doc_row["title"] or f"doc-{doc_id}"
        already_archived = doc_row["status"] == "archived"

        rubric_status: RubricStatus | None = None
        if doc_path and doc_path.exists():
            try:
                rubric_status = await asyncio.to_thread(read_rubric, doc_path)
            except Exception as exc:
                logger.warning(f"victory-ack: rubric read failed for {doc_path}: {exc}")

        # Precondition: rubric must be complete (else 409). Legacy docs without
        # a typed rubric get a pass — the Emperor can ack them freely.
        if (
            not force
            and rubric_status is not None
            and rubric_status.present
            and not rubric_status.legacy_string
        ):
            if not rubric_status.complete:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "rubric_incomplete",
                        "doc_id": doc_id,
                        "missing": rubric_status.missing,
                        "skipped": rubric_status.skipped,
                        "message": (
                            "Cannot ack victory — these conditions are unmet: "
                            + ", ".join(rubric_status.missing)
                            + ". Address them, mark inapplicable ones in "
                            f"{rubric_status.rubric_key}_skip, or pass force=true."
                        ),
                    },
                )

        # Stamp rubric ack on the session-doc frontmatter (modern path).
        if doc_path and doc_path.exists():
            try:
                await asyncio.to_thread(mark_rubric_acknowledged, doc_path, reason)
                if deliverables:
                    await asyncio.to_thread(
                        update_frontmatter, doc_path, {"deliverables": deliverables}
                    )
            except Exception as exc:
                logger.warning(f"victory-ack: frontmatter ack failed for {doc_path}: {exc}")

        # Archive the doc in the DB (status transition).
        if not already_archived:
            await db.execute(
                "UPDATE session_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                (now, doc_id),
            )

        # Resolve all instances linked to this doc; downgrade and cancel timers.
        cursor = await db.execute(
            "SELECT id, tab_name FROM claude_instances WHERE session_doc_id = ?",
            (doc_id,),
        )
        linked_rows = await cursor.fetchall()
        linked_instance_ids: list[str] = []
        instance_surfaces: list[str] = []
        for linked in linked_rows:
            iid = linked["id"]
            linked_instance_ids.append(iid)
            tab = linked["tab_name"] or iid[:12]
            instance_surfaces.append(tab if _is_meaningful_tab_name(tab) else iid[:12])
            await sanctioned_update_instance(
                db,
                instance_id=iid,
                updates={
                    "victory_at": now,
                    "victory_reason": reason,
                    "instance_type": "one_off",
                    "gt_resume_count": 0,
                    "gt_resume_window_started_at": None,
                    "gt_last_resume_at": None,
                },
                mutation_type="instance_updated",
                write_source="api",
                actor=source,
            )
        await db.commit()

    timers_cancelled: list[str] = []
    for iid in linked_instance_ids:
        try:
            scheduler.remove_job(f"golden-throne-{iid}")
            timers_cancelled.append(iid)
        except Exception:
            pass

    victory_surface = ", ".join(instance_surfaces) or doc_title
    try:
        await asyncio.to_thread(
            subprocess.run,
            [
                "discord",
                "send",
                "fleet",
                f"⚔️ **IMPERIUM VICTORIOUS** — {victory_surface}\n> {reason}",
            ],
            timeout=10,
            capture_output=True,
        )
    except Exception as e:
        logger.warning(f"victory-ack: Discord notify failed: {e}")

    await log_event(
        "session_doc_victory_ack",
        details={
            "doc_id": doc_id,
            "doc_path": str(doc_path) if doc_path else None,
            "reason": reason,
            "deliverables": deliverables,
            "linked_instance_ids": linked_instance_ids,
            "timers_cancelled": timers_cancelled,
            "force": force,
            "source": source,
            "rubric_complete": (rubric_status.complete if rubric_status else None),
        },
    )
    logger.info(
        f"victory-ack: doc {doc_id} archived (force={force}) — "
        f"{len(linked_instance_ids)} instance(s) downgraded, "
        f"{len(timers_cancelled)} timer(s) cancelled"
    )
    return {
        "doc_id": doc_id,
        "victory": True,
        "archived": True,
        "linked_instance_ids": linked_instance_ids,
        "timers_cancelled": timers_cancelled,
        "force": force,
    }


@app.post("/api/session-docs/{doc_id}/rubric-flip")
async def session_doc_rubric_flip(doc_id: int, request: Request):
    """Flip a single rubric field on a session doc's frontmatter.

    Used by automated hook surfaces (post-push, post-pr-create, CodeRabbit
    webhook) to record SOP completion without forcing the agent to remember.
    Optional `extra` dict sets sibling frontmatter fields atomically — e.g.
    pr_url alongside pr_opened.
    """
    body = await request.json()
    key = body.get("key")
    if not isinstance(key, str) or not key:
        raise HTTPException(status_code=400, detail="key (str) is required")
    value = body.get("value", True)
    rubric_key = body.get("rubric_key")
    extra = body.get("extra") or {}
    if not isinstance(extra, dict):
        raise HTTPException(status_code=400, detail="extra must be an object")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT file_path FROM session_documents WHERE id = ?", (doc_id,))
        row = await cursor.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")
    fp = Path(row[0])
    if not fp.exists():
        raise HTTPException(status_code=404, detail=f"Session doc file missing: {fp}")

    try:
        await asyncio.to_thread(update_rubric_field, fp, key, value, rubric_key)
        if extra:
            await asyncio.to_thread(update_frontmatter, fp, extra)
    except Exception as exc:
        logger.warning(f"rubric-flip: write failed for doc {doc_id} key={key}: {exc}")
        raise HTTPException(status_code=500, detail=f"frontmatter write failed: {exc}")

    await log_event(
        "session_doc_rubric_flip",
        details={
            "doc_id": doc_id,
            "rubric_key": rubric_key or DEFAULT_RUBRIC_KEY,
            "key": key,
            "value": value,
            "extra": list(extra.keys()),
        },
    )
    logger.info(f"rubric-flip: doc {doc_id} {rubric_key or 'victory'}.{key} = {value}")
    return {"doc_id": doc_id, "key": key, "value": value, "extra": list(extra.keys())}


@app.post("/api/session-docs/{doc_id}/victory-ack")
async def victory_ack_session_doc(doc_id: int, request: Request):
    """Emperor's final ack on a session doc.

    Preconditions: the doc's victory rubric must be complete (all conditions
    true or in victory_skip). If not, returns 409 with the missing list. Pass
    `force: true` in the body to override (legacy/escape-hatch use only).

    On success: stamps victory_acknowledged_at + victory_reason, archives the
    doc, cancels GT timers on all linked instances, and downgrades them to
    one_off so they never re-fire.
    """
    body = await request.json()
    reason = body.get("reason") or "victory"
    deliverables = body.get("deliverables", []) or []
    force = bool(body.get("force"))
    return await _victory_ack_core(doc_id, reason, deliverables, force=force, source="victory-ack")


@app.post("/api/instances/{instance_id}/victory")
async def declare_victory(instance_id: str, request: Request):
    """[DEPRECATED — use POST /api/session-docs/{doc_id}/victory-ack]

    Legacy entry point. Resolves the instance's linked session doc and routes
    through the new victory-ack flow with force=True (preserving the old
    permissive semantics so this endpoint never 409s). Instances with no
    linked doc fall back to the old DB-only path.
    """
    body = await request.json()
    reason = body.get("reason")
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    deliverables = body.get("deliverables", []) or []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, tab_name, session_doc_id FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")
        session_doc_id = row["session_doc_id"]
        tab_name = row["tab_name"] or instance_id[:12]

    if session_doc_id:
        result = await _victory_ack_core(
            session_doc_id,
            reason,
            deliverables,
            force=True,
            source="declare_victory_legacy",
        )
        result["instance_id"] = instance_id
        result["deprecated"] = "use /api/session-docs/{doc_id}/victory-ack"
        return result

    # No linked doc — legacy bare-instance path.
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={
                "victory_at": now,
                "victory_reason": reason,
                "instance_type": "one_off",
                "gt_resume_count": 0,
                "gt_resume_window_started_at": None,
                "gt_last_resume_at": None,
            },
            mutation_type="instance_updated",
            write_source="api",
            actor="declare_victory_legacy",
        )
        await db.commit()

    timer_cancelled = False
    try:
        scheduler.remove_job(f"golden-throne-{instance_id}")
        timer_cancelled = True
    except Exception:
        pass

    victory_surface = tab_name if _is_meaningful_tab_name(tab_name) else instance_id[:12]
    try:
        await asyncio.to_thread(
            subprocess.run,
            [
                "discord",
                "send",
                "fleet",
                f"⚔️ **IMPERIUM VICTORIOUS** — {victory_surface}\n> {reason}",
            ],
            timeout=10,
            capture_output=True,
        )
    except Exception as e:
        logger.warning(f"declare_victory_legacy: Discord notify failed: {e}")

    await log_event(
        "golden_throne_victory",
        instance_id=instance_id,
        details={
            "reason": reason,
            "timer_cancelled": timer_cancelled,
            "session_doc_updated": False,
            "deliverables": deliverables,
            "legacy_path": "no_doc_linked",
        },
    )
    return {
        "instance_id": instance_id,
        "victory": True,
        "timer_cancelled": timer_cancelled,
        "session_doc_updated": False,
        "deprecated": "use /api/session-docs/{doc_id}/victory-ack",
    }


@app.post("/api/instances/{instance_id}/golden-throne/trigger")
async def trigger_golden_throne_followup(instance_id: str):
    """Manually trigger the Golden Throne follow-up callback for an instance."""
    await golden_throne_followup(instance_id)
    return {"triggered": True, "instance_id": instance_id}


# ============ Instance Lifecycle Type ============

VALID_INSTANCE_TYPES = {"sync", "golden_throne", "one_off", "hook_driven", "archived"}


@app.patch("/api/instances/{instance_id}/type")
async def set_instance_type(instance_id: str, request: Request):
    """Set instance lifecycle type with transition validation."""
    body = await request.json()
    new_type = body.get("instance_type")
    if new_type not in VALID_INSTANCE_TYPES:
        raise HTTPException(
            status_code=400, detail=f"instance_type must be one of {VALID_INSTANCE_TYPES}"
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
        instance = await cursor.fetchone()
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")

        old_type = instance["instance_type"] or "one_off"

        # Archived instances can only unarchive to one_off
        if old_type == "archived" and new_type != "one_off":
            raise HTTPException(
                status_code=400, detail="Archived instances can only be unarchived to one_off"
            )

        updates = {"instance_type": new_type}

        # Optional follow_up_sop — persist custom SOP path for GT follow-ups
        follow_up_sop = body.get("follow_up_sop")
        if follow_up_sop is not None:
            updates["follow_up_sop"] = follow_up_sop if follow_up_sop else None

        # Optional zealotry override in same call
        zealotry_override = body.get("zealotry")
        if isinstance(zealotry_override, int) and 1 <= zealotry_override <= 10:
            updates["zealotry"] = zealotry_override

        # Auto-set zealotry minimum when promoting to golden_throne
        if (
            new_type == "golden_throne"
            and (instance["zealotry"] or 4) < 4
            and zealotry_override is None
        ):
            updates["zealotry"] = 4

        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates=updates,
            mutation_type="instance_updated",
            write_source="api",
            actor="instance-type",
        )
        await db.commit()

    # Cancel timers when leaving golden_throne or sync
    if old_type in ("golden_throne", "sync") and new_type not in ("golden_throne", "sync"):
        try:
            scheduler.remove_job(f"golden-throne-{instance_id}")
        except Exception:
            pass
        try:
            scheduler.remove_job(f"sync-retrigger-{instance_id}")
        except Exception:
            pass
    elif new_type == "golden_throne":
        refreshed = dict(instance)
        refreshed.update(updates)
        if refreshed.get("status") in ("idle", "stopped") and not refreshed.get("victory_at"):
            try:
                await schedule_golden_throne_followup(refreshed, reason="instance-type")
            except Exception as exc:
                logger.warning(
                    f"Golden Throne: failed to schedule after type change "
                    f"for {instance_id[:12]}: {exc}"
                )

    await log_event(
        "instance_type_changed",
        instance_id=instance_id,
        details={"old_type": old_type, "new_type": new_type},
    )
    return {"instance_id": instance_id, "instance_type": new_type, "old_type": old_type}


@app.patch("/api/instances/{instance_id}/archive")
async def archive_instance(instance_id: str):
    """Archive an instance — manual user action only."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM claude_instances WHERE id = ?", (instance_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"instance_type": "archived", "status": "stopped"},
            mutation_type="instance_archived",
            write_source="api",
            actor="archive-instance",
        )
        await db.commit()

    # Cancel any pending timers
    for prefix in ("golden-throne-", "sync-retrigger-"):
        try:
            scheduler.remove_job(f"{prefix}{instance_id}")
        except Exception:
            pass

    return {"instance_id": instance_id, "instance_type": "archived"}


@app.patch("/api/instances/{instance_id}/unarchive")
async def unarchive_instance(instance_id: str):
    """Unarchive an instance — returns to one_off."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM claude_instances WHERE id = ?", (instance_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"instance_type": "one_off"},
            mutation_type="instance_updated",
            write_source="api",
            actor="unarchive-instance",
        )
        await db.commit()

    return {"instance_id": instance_id, "instance_type": "one_off"}


def _send_pedal_enter():
    """Send Enter keystroke via satellite /ahk/execute."""
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    try:
        resp = requests.post(
            f"http://{host}:{port}/ahk/execute",
            json={"script": "pedal-enter.ahk"},
            timeout=DESKTOP_CONFIG["timeout"],
        )
        logger.info(f"Pedal: Enter sent (satellite status={resp.status_code})")
    except (requests.ConnectionError, requests.Timeout) as e:
        logger.warning(f"Pedal: Satellite unreachable: {e}")


def _schedule_pedal_enter(delay_s: float):
    """Schedule a delayed Enter send, cancelling any existing scheduled send."""
    # Cancel existing scheduled send
    if PEDAL_STATE["queued_task"] and not PEDAL_STATE["queued_task"].done():
        PEDAL_STATE["queued_task"].cancel()

    async def _delayed_send():
        await asyncio.sleep(delay_s)
        PEDAL_STATE["enter_queued"] = False
        PEDAL_STATE["bypass_active"] = True
        PEDAL_STATE["bypass_start"] = time.monotonic()
        _send_pedal_enter()
        logger.info(f"Pedal: Queued Enter sent after {delay_s}s buffer")

    PEDAL_STATE["queued_task"] = asyncio.create_task(_delayed_send())


@app.post("/api/pedal/left")
async def pedal_left():
    """Handle left pedal press. Mirrors ring-remap left button logic.

    - During dictation: queue Enter for after buffer
    - After dictation (bypass window): single tap sends Enter
    - Normal: double-tap required to send Enter
    """
    now = time.monotonic()

    # During active dictation — queue enter
    if DICTATION_STATE["active"]:
        PEDAL_STATE["enter_queued"] = True
        logger.info("Pedal: Enter queued (dictation active)")
        return {"action": "queued", "reason": "dictation_active"}

    # Check if we're in the buffer window right after dictation ended
    dictation_updated = DICTATION_STATE.get("updated_at")
    if dictation_updated and not DICTATION_STATE["active"]:
        ended_at = datetime.fromisoformat(dictation_updated)
        elapsed = (datetime.now() - ended_at).total_seconds()
        if elapsed < PEDAL_BUFFER_MS:
            remaining = PEDAL_BUFFER_MS - elapsed
            PEDAL_STATE["enter_queued"] = True
            _schedule_pedal_enter(remaining)
            logger.info(f"Pedal: Enter in {remaining:.1f}s (buffer window)")
            return {"action": "buffered", "delay_s": round(remaining, 1)}

    # Bypass window — single tap sends Enter
    if PEDAL_STATE["bypass_active"]:
        if (now - PEDAL_STATE["bypass_start"]) < PEDAL_BYPASS_MS:
            PEDAL_STATE["bypass_active"] = False
            PEDAL_STATE["last_tap_time"] = 0
            _send_pedal_enter()
            return {"action": "sent", "reason": "bypass"}
        else:
            PEDAL_STATE["bypass_active"] = False

    # Double-tap logic
    if (now - PEDAL_STATE["last_tap_time"]) < (PEDAL_DOUBLE_TAP_MS / 1000.0):
        PEDAL_STATE["last_tap_time"] = 0
        _send_pedal_enter()
        return {"action": "sent", "reason": "double_tap"}
    else:
        PEDAL_STATE["last_tap_time"] = now
        logger.info("Pedal: Tap 1/2")
        return {"action": "waiting", "reason": "first_tap"}


_INSTANCES_READ_CACHE: dict[tuple[str, str, int], tuple[float, list[dict]]] = {}
_INSTANCES_READ_CACHE_LOCK = asyncio.Lock()
_INSTANCES_READ_CACHE_TTL_SECONDS = 0.5


@app.get("/api/instances", response_model=list[dict])
async def list_instances(
    status: str | None = None,
    sort: str | None = None,
    limit: int = 300,
):
    """List instances, optionally filtered by status and sorted."""
    order_clauses = {
        "status": "status ASC, last_activity DESC",
        "recent_activity": "last_activity DESC",
        "recent_stopped": "stopped_at DESC NULLS LAST, last_activity DESC",
        "created": "registered_at DESC",
    }
    order_by = order_clauses.get(sort, "registered_at DESC")
    limit = max(1, min(int(limit or 300), 1000))
    cache_key = (status or "", sort or "", limit)
    cache_hit = _INSTANCES_READ_CACHE.get(cache_key)
    now_mono = time.monotonic()
    if cache_hit and now_mono - cache_hit[0] <= _INSTANCES_READ_CACHE_TTL_SECONDS:
        return cache_hit[1]

    async with _INSTANCES_READ_CACHE_LOCK:
        cache_hit = _INSTANCES_READ_CACHE.get(cache_key)
        now_mono = time.monotonic()
        if cache_hit and now_mono - cache_hit[0] <= _INSTANCES_READ_CACHE_TTL_SECONDS:
            return cache_hit[1]

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            if status:
                cursor = await db.execute(
                    f"SELECT * FROM claude_instances WHERE status = ? ORDER BY {order_by} LIMIT ?",
                    (status, limit),
                )
            else:
                cursor = await db.execute(
                    f"SELECT * FROM claude_instances ORDER BY {order_by} LIMIT ?",
                    (limit,),
                )

            rows = await cursor.fetchall()

        instances = []
        pane_label_repair_candidates = []
        for row in rows:
            inst = dict(row)
            # voice_chat derived from tts_mode column (DB-authoritative)
            is_vc = (inst.get("tts_mode") == "voice-chat") or (inst["id"] in VOICE_CHAT_SESSIONS)
            if is_vc:
                inst["voice_chat"] = True
                inst["listening"] = DICTATION_STATE["active"]
                # Ensure in-memory session exists if DB says voice-chat
                if inst["id"] not in VOICE_CHAT_SESSIONS:
                    VOICE_CHAT_SESSIONS[inst["id"]] = {
                        "active": True,
                        "started_at": datetime.now().isoformat(),
                    }
            # Resolve cc_color from profile name
            pn = inst.get("profile_name")
            if pn:
                for p in PROFILES + FALLBACK_VOICES + [ULTIMATE_FALLBACK]:
                    if p["name"] == pn:
                        inst["color"] = p.get("color", "#0099ff")
                        inst["cc_color"] = p.get("cc_color", "default")
                        break
            # Golden Throne: enrich with pending timer state
            gt_job = scheduler.get_job(f"golden-throne-{inst['id']}")
            inst["gt_next_fire"] = (
                gt_job.next_run_time.isoformat() if gt_job and gt_job.next_run_time else None
            )
            if (
                not inst.get("pane_label")
                and inst.get("status") != "stopped"
                and inst.get("device_id") == LOCAL_DEVICE_NAME
                and inst.get("tmux_pane")
            ):
                pane_label_repair_candidates.append(
                    {"id": inst["id"], "tmux_pane": inst.get("tmux_pane")}
                )

            instances.append(inst)

        _schedule_pane_label_repair(pane_label_repair_candidates)
        _INSTANCES_READ_CACHE[cache_key] = (time.monotonic(), instances)
        return instances


@app.get("/api/instances/resolve")
async def resolve_instance(pid: int | None = None, cwd: str | None = None):
    """Resolve the calling agent's instance using PID and/or CWD fallback.

    Returns instance + session doc info in a single call.
    Resolution order: PID match → CWD match (prefer processing over idle).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        instance = None

        # Method 1: PID match
        if pid:
            cursor = await db.execute(
                "SELECT * FROM claude_instances WHERE pid = ? AND status IN ('processing', 'idle') LIMIT 1",
                (pid,),
            )
            instance = await cursor.fetchone()

        # Method 2: CWD match (prefer processing)
        if not instance and cwd:
            cursor = await db.execute(
                "SELECT * FROM claude_instances WHERE working_dir = ? AND status = 'processing' ORDER BY last_activity DESC LIMIT 1",
                (cwd,),
            )
            instance = await cursor.fetchone()
            if not instance:
                cursor = await db.execute(
                    "SELECT * FROM claude_instances WHERE working_dir = ? AND status = 'idle' ORDER BY last_activity DESC LIMIT 1",
                    (cwd,),
                )
                instance = await cursor.fetchone()

        if not instance:
            raise HTTPException(404, "No matching instance found")

        result = dict(instance)

        # Attach session doc if linked
        if result.get("session_doc_id"):
            cursor = await db.execute(
                "SELECT * FROM session_documents WHERE id = ?", (result["session_doc_id"],)
            )
            doc = await cursor.fetchone()
            result["session_doc"] = dict(doc) if doc else None
        else:
            result["session_doc"] = None

        return result


@app.get("/api/instances/{instance_id}", response_model=dict)
async def get_instance(instance_id: str):
    """Get details of a specific instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
        row = await cursor.fetchone()

        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        instance = dict(row)
        # Resolve color from profile name
        profile_name = instance.get("profile_name")
        if profile_name:
            for p in PROFILES + FALLBACK_VOICES + [ULTIMATE_FALLBACK]:
                if p["name"] == profile_name:
                    instance["color"] = p.get("color", "#0099ff")
                    instance["cc_color"] = p.get("cc_color", "default")
                    break
        return instance


@app.get("/api/instances/{instance_id}/workflow-events", response_model=list[dict])
async def get_instance_workflow_events(instance_id: str, limit: int = 20):
    """Return recent workflow events for a specific instance."""
    limit = max(1, min(limit, 100))

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT 1 FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")

        cursor = await db.execute(
            """SELECT id, instance_id, workflow_state, event_type, event_owner, details_json, created_at
               FROM workflow_events
               WHERE instance_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (instance_id, limit),
        )
        rows = await cursor.fetchall()

    events = []
    for row in rows:
        event = dict(row)
        details_json = event.pop("details_json", None)
        event["details"] = json.loads(details_json) if details_json else None
        events.append(event)
    return events


@app.get("/api/instances/{instance_id}/provenance", response_model=dict)
async def get_instance_provenance(instance_id: str, limit: int = 20):
    """Return recent sanctioned mutation history for a specific instance."""
    limit = max(1, min(limit, 100))
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT 1 FROM claude_instances WHERE id = ?", (instance_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Instance not found")
        mutations = await get_instance_mutations(db, instance_id, limit=limit)
    return {
        "instance_id": instance_id,
        "latest_sanctioned_mutation": mutations[0] if mutations else None,
        "recent_mutations": mutations,
        "last_write_txn_id": mutations[0]["write_txn_id"] if mutations else None,
    }


@app.get("/api/instances/{instance_id}/reconciliation", response_model=dict)
async def get_instance_reconciliation(instance_id: str):
    """Return reconciliation status for one instance against sanctioned writes and pane projection."""
    async with aiosqlite.connect(DB_PATH) as db:
        result = await reconcile_instance(db, instance_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Instance not found")
    if result["status"] in RECONCILIATION_SUSPICIOUS:
        affected_fields = sorted(
            {field for finding in result["findings"] for field in finding.get("fields", [])}
        )
        await log_event(
            "instance_reconciliation_drift",
            instance_id=instance_id,
            details={
                "reconciliation_status": result["status"],
                "affected_fields": affected_fields,
                "write_txn_id": result.get("last_write_txn_id"),
                "pending_projection": result["status"] == "pending_projection",
            },
        )
    return result


@app.get("/api/reconciliation/instances", response_model=dict)
async def list_instance_reconciliation(limit: int = 50, suspicious_only: bool = True):
    """Return recent reconciliation findings across instances."""
    limit = max(1, min(limit, 200))
    results = []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id
               FROM claude_instances
               ORDER BY datetime(last_activity) DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        for row in rows:
            result = await reconcile_instance(db, row["id"])
            if result is None:
                continue
            if suspicious_only and result["status"] == "clean":
                continue
            results.append(result)
            if result["status"] in RECONCILIATION_SUSPICIOUS:
                affected_fields = sorted(
                    {field for finding in result["findings"] for field in finding.get("fields", [])}
                )
                await log_event(
                    "instance_reconciliation_drift",
                    instance_id=row["id"],
                    details={
                        "reconciliation_status": result["status"],
                        "affected_fields": affected_fields,
                        "write_txn_id": result.get("last_write_txn_id"),
                        "pending_projection": result["status"] == "pending_projection",
                    },
                )
    return {"instances": results}


_KNOWN_VAULTS = ("Imperium-ENV", "Pax-ENV", "Civic-ENV")


def _derive_vault_and_relative(file_path: str) -> tuple[str, str]:
    """Split an absolute session-doc path into (vault_name, vault_relative_path).

    file_path may be stored as absolute (/Volumes/Imperium/Imperium-ENV/Terra/…)
    or as vault-relative (Terra/Sessions/…). Writers vary by machine; readers
    need a normalized form for the obsidian:// URI and the obsidian CLI.
    """
    for vault in _KNOWN_VAULTS:
        marker = f"/{vault}/"
        idx = file_path.find(marker)
        if idx >= 0:
            return vault, file_path[idx + len(marker) :]
    return "Imperium-ENV", file_path


@app.get("/api/panes/{tmux_pane}/session-doc")
async def pane_session_doc(tmux_pane: str):
    """Resolve the session doc linked to a tmux pane.

    Returns {vault, file_path (vault-relative), absolute_path, title, doc_id,
    instance_id}. 404 if no instance for the pane, or no linked session doc.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT ci.id AS instance_id, sd.id AS doc_id, sd.file_path,
                      sd.title, sd.project
               FROM claude_instances ci
               LEFT JOIN session_documents sd ON ci.session_doc_id = sd.id
               WHERE ci.tmux_pane = ?
               ORDER BY ci.last_activity DESC
               LIMIT 1""",
            (tmux_pane,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, f"No instance for pane {tmux_pane}")
        if not row["doc_id"]:
            raise HTTPException(404, f"Instance {row['instance_id']} has no session doc")

        vault, rel = _derive_vault_and_relative(row["file_path"])
        return {
            "instance_id": row["instance_id"],
            "doc_id": row["doc_id"],
            "vault": vault,
            "file_path": rel,
            "absolute_path": row["file_path"],
            "title": row["title"],
            "project": row["project"],
        }


@app.get("/api/panes/{tmux_pane}/instance")
async def pane_instance(tmux_pane: str):
    """Resolve the most recent active instance bound to a tmux pane."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT *
               FROM claude_instances
               WHERE tmux_pane = ? AND status != 'stopped'
               ORDER BY last_activity DESC
               LIMIT 1""",
            (tmux_pane,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, f"No active instance for pane {tmux_pane}")

    inst = dict(row)
    gt_job = scheduler.get_job(f"golden-throne-{inst['id']}")
    inst["gt_next_fire"] = (
        gt_job.next_run_time.isoformat() if gt_job and gt_job.next_run_time else None
    )
    return inst


@app.get("/api/orchestrator/pane_truth")
async def orchestrator_pane_truth():
    """Merged tmux↔DB pane truth for the orchestrator.

    Joins live ``tmux list-panes -a`` against ``claude_instances`` (active or
    pane-resident) and ``session_documents``. Drift flags are computed live —
    independent of whether the reconciler has run yet.

    Anti-archaeology: one query, one answer. No caller writes its own join.
    """
    panes = await _read_tmux_panes()
    if panes is None:
        panes = {}
    pane_ids_in_tmux = set(panes.keys())

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT ci.id AS instance_id, ci.tmux_pane, ci.pane_label, ci.tab_name,
                      ci.status, ci.last_activity, ci.engine, ci.legion,
                      ci.session_doc_id, ci.workflow_state, ci.workflow_blocked_reason,
                      sd.file_path AS session_doc_path,
                      sd.title AS session_doc_title
               FROM claude_instances ci
               LEFT JOIN session_documents sd ON ci.session_doc_id = sd.id
               WHERE ci.status IN ('processing', 'idle', 'active')
                  OR (ci.tmux_pane IS NOT NULL AND ci.tmux_pane != '')
               ORDER BY ci.last_activity DESC"""
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    # Filter rows: keep active rows OR rows whose tmux_pane is currently live.
    # (A stopped row whose pane never existed in tmux is noise.)
    filtered: list[dict] = []
    for row in rows:
        if row.get("status") in ("processing", "idle", "active"):
            filtered.append(row)
            continue
        if row.get("tmux_pane") and row["tmux_pane"] in pane_ids_in_tmux:
            filtered.append(row)

    # Identify duplicate-pane owners for the superseded_duplicate flag.
    by_pane: dict[str, list[dict]] = {}
    for row in filtered:
        tp = row.get("tmux_pane")
        if tp:
            by_pane.setdefault(tp, []).append(row)
    duplicate_pane_ids: set[str] = set()
    superseded_row_ids: set[str] = set()
    for tp, group in by_pane.items():
        if len(group) <= 1:
            continue
        duplicate_pane_ids.add(tp)
        ordered = sorted(
            group,
            key=lambda r: (r.get("last_activity") or "", r.get("instance_id") or ""),
            reverse=True,
        )
        for losing in ordered[1:]:
            superseded_row_ids.add(losing["instance_id"])

    out: list[dict] = []
    for row in filtered:
        tmux_pane = row.get("tmux_pane")
        pane_meta = panes.get(tmux_pane) if tmux_pane else None
        # Compute drift flags. superseded_duplicate is per-row, not per-pane.
        flags: list[str] = []
        if row["instance_id"] in superseded_row_ids:
            flags.append("superseded_duplicate")
        if tmux_pane and tmux_pane not in pane_ids_in_tmux:
            flags.append("pane_missing")
        if pane_meta and row.get("pane_label") != pane_meta["pane_label"]:
            flags.append("pane_label_drift")
        if _is_placeholder_tab_name(row.get("tab_name")) and row.get("session_doc_id"):
            flags.append("tab_name_placeholder")
        if _tab_name_session_doc_mismatch(row.get("tab_name"), row.get("session_doc_path")):
            flags.append("tab_name_session_doc_mismatch")

        descriptive_name = _clean_tab_name(row.get("tab_name")) or None
        is_placeholder = _is_placeholder_tab_name(row.get("tab_name"))

        out.append(
            {
                "instance_id": row["instance_id"],
                "tmux_pane": tmux_pane,
                "tmux_session_window": pane_meta["session_window"] if pane_meta else None,
                "tmux_current_command": pane_meta["current_command"] if pane_meta else None,
                "pane_label": row.get("pane_label"),
                "descriptive_name": None if is_placeholder else descriptive_name,
                "is_placeholder_name": is_placeholder,
                "session_doc_id": row.get("session_doc_id"),
                "session_doc_path": row.get("session_doc_path"),
                "session_doc_title": row.get("session_doc_title"),
                "status": row.get("status"),
                "last_activity": row.get("last_activity"),
                "engine": row.get("engine"),
                "legion": row.get("legion"),
                "workflow_state": row.get("workflow_state"),
                "workflow_blocked_reason": row.get("workflow_blocked_reason"),
                "drift_flags": flags,
            }
        )

    return out


# --- Trinity Chunk 1: talk / brief inter-persona comm primitives ------------


async def _talk_send_payload(target_pane: str, payload: str) -> dict:
    """Inject `payload` into ``target_pane`` via the existing pane-write queue.

    Drained synchronously so a CLI long-poll sees an actual delivery state.
    """
    queued = await enqueue_pane_write(
        instance_id=target_pane,
        tmux_pane=target_pane,
        source="talk",
        purpose="talk_send",
        payload=payload,
    )
    drained = await process_pane_write_queue_once(queued["id"])
    return drained[0] if drained else queued


@app.post("/api/talk/send")
async def talk_send(request: TalkSendRequest):
    """Open a two-way talk pair and inject the payload into the target's input.

    Returns ``talk_id``; the caller then long-polls ``/api/talk/await/{id}``.
    If the target_pane is already the turn-holder of a pair where the caller
    is the listener (i.e. caller=A, target=B and now A → B again while B has
    not yet returned), the existing pair is reused — no new id.
    """
    caller_raw = request.caller_pane.strip()
    target_raw = request.target_pane.strip()
    caller_pane = await talk_service.resolve_pane(caller_raw)
    target_pane = await talk_service.resolve_pane(target_raw)
    if not caller_pane:
        raise HTTPException(status_code=400, detail=f"caller_pane unresolved: {caller_raw}")
    if not target_pane:
        raise HTTPException(status_code=400, detail=f"target_pane unresolved: {target_raw}")
    if caller_pane == target_pane:
        raise HTTPException(status_code=400, detail="caller_pane and target_pane are the same")

    # Explicit-return shortcut: if the SWAPPED pair (caller=target, target=caller)
    # is already open, this call IS the return.
    returned = await talk_service.return_talk(
        caller_pane=caller_pane,
        target_pane=target_pane,
        payload=request.payload,
    )
    if returned is not None:
        # Still deliver the message into the original caller's pane so the
        # listener actually sees the response in its input stream.
        try:
            send_result = await _talk_send_payload(target_pane, request.payload)
        except Exception as exc:  # noqa: BLE001
            send_result = {"status": "failed", "error": str(exc)}
        return {
            "status": "returned",
            "talk_id": returned["talk_id"],
            "result_kind": "explicit",
            "delivery": send_result,
            "talk": returned,
        }

    target_instance = await talk_service.lookup_instance_for_pane(target_pane)
    target_engine = (target_instance or {}).get("engine") or "claude"
    record = await talk_service.register_talk(
        caller_pane=caller_pane,
        target_pane=target_pane,
        payload=request.payload,
        target_instance=target_instance,
        engine=target_engine,
    )
    try:
        send_result = await _talk_send_payload(target_pane, request.payload)
    except Exception as exc:  # noqa: BLE001
        await talk_service.cancel_talk(record["talk_id"], reason="delivery_failed")
        raise HTTPException(status_code=502, detail=f"talk delivery failed: {exc}") from exc

    return {
        "status": "open",
        "talk_id": record["talk_id"],
        "caller_pane": caller_pane,
        "target_pane": target_pane,
        "target_instance_id": record["target_instance_id"],
        "delivery": send_result,
    }


@app.get("/api/talk/await/{talk_id}")
async def talk_await(talk_id: str, timeout: float = 30.0):
    """Long-poll for a talk pair result. ``timeout`` capped server-side."""
    timeout = max(1.0, min(float(timeout or 30.0), 120.0))
    record = await talk_service.await_talk(talk_id, timeout=timeout)
    if record is None:
        raise HTTPException(status_code=404, detail=f"talk_id not found: {talk_id}")
    return record


@app.post("/api/talk/cancel/{talk_id}")
async def talk_cancel(talk_id: str):
    record = await talk_service.cancel_talk(talk_id, reason="caller_cancel")
    if record is None:
        raise HTTPException(status_code=404, detail=f"talk_id not open: {talk_id}")
    return {"status": "cancelled", "talk_id": talk_id}


@app.post("/api/brief/send")
async def brief_send(request: BriefSendRequest):
    """Fire-and-forget delivery to one or more panes/pages with dedup."""
    if not request.panes and not request.pages:
        raise HTTPException(status_code=400, detail="at least one --pane or --page required")

    resolved, unresolved = await talk_service.resolve_brief_targets(
        panes=request.panes,
        pages=request.pages,
    )
    if not resolved:
        return {
            "status": "no_targets",
            "ephemeral": request.ephemeral,
            "resolved": [],
            "unresolved": unresolved,
            "delivered": 0,
        }

    delivered: list[dict] = []
    for target in resolved:
        pane_id = target["pane_id"]
        try:
            if request.ephemeral:
                # Reuse the temp_message side-channel infra (/btw or /side).
                instance = await talk_service.lookup_instance_for_pane(pane_id)
                engine = (instance or {}).get("engine")
                receipt = await temp_message_service.send_temp_message(
                    pane_id,
                    request.payload,
                    engine,
                    instance_id=(instance or {}).get("id") or pane_id,
                    queue_sender=enqueue_pane_write,
                    queue_drainer=process_pane_write_queue_once,
                )
                receipt = {**receipt, **target}
            else:
                queued = await enqueue_pane_write(
                    instance_id=pane_id,
                    tmux_pane=pane_id,
                    source="brief",
                    purpose="brief_send",
                    payload=request.payload,
                )
                drained = await process_pane_write_queue_once(queued["id"])
                receipt = drained[0] if drained else queued
                receipt = {**receipt, **target}
            delivered.append(receipt)
        except Exception as exc:  # noqa: BLE001
            delivered.append({**target, "status": "failed", "error": str(exc)})
    return {
        "status": "ok",
        "ephemeral": request.ephemeral,
        "delivered": len([r for r in delivered if r.get("status") in {"sent", "pending"}]),
        "resolved": delivered,
        "unresolved": unresolved,
    }


@app.post("/api/orchestrator/temp_message")
async def orchestrator_temp_message(request: TempMessageRequest):
    """Dispatch an ephemeral roll-call prompt to panes selected by engine/page/name."""
    poll_id = request.idempotency_key or str(uuid.uuid4())
    try:
        receipts = await temp_message_service.broadcast_temp_message(
            request.selector,
            request.payload,
            idempotency_key=poll_id,
            db_path=DB_PATH,
            queue_sender=enqueue_pane_write,
            queue_drainer=process_pane_write_queue_once,
        )
    except temp_message_service.SelectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "ok",
        "poll_id": poll_id,
        "selector": request.selector,
        "target_count": len(receipts),
        "receipts": receipts,
    }


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
        cursor = await db.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 20")
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
            tts_queue=get_tts_queue_status(),
        )


class LogEventRequest(BaseModel):
    event_type: str
    instance_id: str | None = None
    details: dict | None = None


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

# [MOVED to shared.py / routes/tts.py] — was: # Windows satellite server config (token-satellite

# [MOVED to shared.py] — VOICE_CHAT_SESSIONS, DICTATION_STATE, PEDAL_STATE, pedal constants

# Valid desktop detection modes (replaces OBSIDIAN_CONFIG["mode_commands"].keys())
VALID_DETECTION_MODES = [
    "silence",
    "music",
    "video",
    "scrolling",
    "gaming",
    "gym",
    "work_gym",
    "meeting",
]

# ============ Timer Engine ============
timer_engine = TimerEngine(now_mono_ms=int(time.monotonic() * 1000))
shared.timer_engine = timer_engine


def reset_idle_timer():
    """Signal productivity to the timer engine. Replaces old _last_work_event_ms tracking."""
    now_ms = int(time.monotonic() * 1000)
    timer_engine.set_productivity(True, now_ms)


# Paths for Obsidian vault — NAS mount preferred, home fallback
_imperium_root = Path(os.environ.get("IMPERIUM", "/Volumes/Imperium"))
if not _imperium_root.exists():
    _imperium_root = Path.home()
OBSIDIAN_VAULT_PATH = _imperium_root / "Imperium-ENV"
OBSIDIAN_DAILY_PATH = OBSIDIAN_VAULT_PATH / "Terra" / "Journal" / "Daily"
OBSIDIAN_INBOX_PATH = OBSIDIAN_VAULT_PATH / "Aspirants"


def _write_productivity_score(date_str: str, score: int):
    """Write productivity_score to a daily note's front matter."""
    try:
        note_path = OBSIDIAN_DAILY_PATH / f"{date_str}.md"
        if not note_path.exists():
            print(f"TIMER: No daily note for {date_str}, skipping score write")
            return

        update_frontmatter(
            note_path,
            {
                "productivity_score": score,
                "timer_completed": True,
            },
        )
        print(f"TIMER: Wrote productivity score {score} to {date_str}")
    except Exception as e:
        print(f"TIMER: Failed to write productivity score: {e}")


# ============ Productivity Check-In System ============
DISCORD_CHECKIN_CHANNEL = "1472043387535495323"

# Discord response routing
# [MOVED to routes/hooks.py or shared.py] — was: DISCORD_DAEMON_URL = "http://127.0.0.1:7779"

MECHANICUS_USER_ID = "1472042705788866611"
MECHANICUS_ROLE_ID = "1477162726093492308"
CUSTODES_USER_ID = "1477159418498912357"
INQUISITION_USER_ID = "1477164289742864479"
OPERATOR_USER_ID = "229461055628115968"
CUSTODES_CHANNELS = {"briefing", "chat"}  # Channels where replies route to Custodes

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
    energy: int | None = None
    focus: int | None = None
    mood: str | None = None
    plan: str | None = None
    notes: str | None = None
    on_track: bool | None = None


def send_discord_checkin(message: str):
    """Send check-in prompt to Discord via openclaw CLI."""
    try:
        # Use full path to avoid PATH issues when running as service
        cmd = [
            "/opt/homebrew/bin/openclaw",
            "message",
            "send",
            "--channel",
            "discord",
            "--target",
            DISCORD_CHECKIN_CHANNEL,
            "--message",
            message,
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
        await db.execute(
            """
            INSERT OR IGNORE INTO checkins (checkin_type, date, prompted_at)
            VALUES (?, ?, ?)
        """,
            (checkin_type, today, prompted_at),
        )
        await db.commit()

    # Send Discord message
    discord_sent = send_discord_checkin(config["discord_message"])

    # TTS nudge
    speak_checkin_tts(config["tts_prompt"])

    logger.info(f"Check-in triggered: {checkin_type} (discord={discord_sent})")
    await log_event(
        "checkin_prompted",
        details={
            "checkin_type": checkin_type,
            "discord_sent": discord_sent,
        },
    )

    return {
        "checkin_type": checkin_type,
        "name": config["name"],
        "discord_sent": discord_sent,
        "prompted_at": prompted_at,
    }


DAILY_NOTE_DIR = Path("/Volumes/Imperium/Imperium-ENV/Terra/Journal/Daily")


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

    # Build new fields from check-in data
    config = CHECKIN_SCHEDULE.get(checkin_type, {})
    time_suffix = config.get("time_suffix", "")

    updates = {}
    if data.get("energy") is not None and time_suffix:
        updates[f"energy_{time_suffix}"] = data["energy"]
    if data.get("focus") is not None and time_suffix:
        updates[f"focus_{time_suffix}"] = data["focus"]
    if data.get("mood") is not None and time_suffix:
        updates[f"mood_{time_suffix}"] = data["mood"]
    if data.get("plan") is not None and time_suffix:
        updates[f"checkin_plan_{time_suffix}"] = data["plan"]
    if data.get("notes") is not None and time_suffix:
        updates[f"checkin_notes_{time_suffix}"] = data["notes"]

    if not updates:
        return False

    # Also update top-level energy/focus/mood to latest value (for meta-bind widgets)
    if data.get("energy") is not None:
        updates["energy"] = data["energy"]
    if data.get("focus") is not None:
        updates["focus"] = data["focus"]
    if data.get("mood") is not None:
        updates["mood"] = data["mood"]

    try:
        update_frontmatter(note_path, updates)
        logger.info(f"Updated daily note frontmatter: {list(updates.keys())}")
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


class DailyNoteCalloutRequest(BaseModel):
    callout_id: str
    content: str
    title: str | None = None
    callout_type: str = "info"
    date: str | None = None


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


@app.put("/api/daily-note/callout")
async def put_daily_note_callout(request: DailyNoteCalloutRequest):
    """Atomically replace or append a managed callout block in a daily note."""
    if not CALLOUT_ID_RE.fullmatch(request.callout_id or ""):
        raise HTTPException(status_code=400, detail="callout_id must match [a-z0-9_-]+")
    if request.callout_type not in ALLOWED_CALLOUT_TYPES:
        allowed = ", ".join(sorted(ALLOWED_CALLOUT_TYPES))
        raise HTTPException(status_code=400, detail=f"callout_type must be one of: {allowed}")
    if len(request.content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise HTTPException(status_code=400, detail=f"content exceeds {MAX_CONTENT_BYTES} bytes")

    date_str = request.date or datetime.now().strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")

    note_path = DAILY_NOTE_DIR / f"{date_str}.md"
    if not note_path.exists():
        raise HTTPException(status_code=404, detail=f"Daily note not found: {note_path}")

    try:
        result = await asyncio.to_thread(
            apply_callout,
            note_path,
            request.callout_id,
            request.content,
            request.title,
            request.callout_type,
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Daily note not found: {note_path}") from None
    except CalloutConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except CalloutError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "date": date_str,
        "callout_id": request.callout_id,
        "action": result.action,
        "path": str(result.path),
        "bytes_written": result.bytes_written,
    }


# [MOVED to phone_service.py] — _persist_twitter_zap_cooldown, _restore_twitter_zap_cooldown

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
            return {
                "success": False,
                "reason": "cooldown",
                "wait_seconds": round(SHIZUKU_CONFIG["restart_cooldown_seconds"] - elapsed),
            }

    if SHIZUKU_STATE["consecutive_failures"] >= SHIZUKU_CONFIG["max_consecutive_failures"]:
        return {
            "success": False,
            "reason": "max_failures_reached",
            "failures": SHIZUKU_STATE["consecutive_failures"],
        }

    SHIZUKU_STATE["last_restart_attempt"] = now.isoformat()
    logger.info(
        f"Shizuku: attempting restart via shizuku-connect (attempt #{SHIZUKU_STATE['restart_count'] + 1})"
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "shizuku-connect",
            "start",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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

    except TimeoutError:
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
    "slay the spire": "gaming",
    "slay": "gaming",
    "com.humble.SlayTheSpire": "gaming",
    "com.humble.slaythespire": "gaming",
}

PHONE_DISTRACTION_ACK_AFTER_SECONDS = int(
    os.environ.get("PHONE_DISTRACTION_ACK_AFTER_SECONDS", "60")
)
PHONE_DISTRACTION_RECOVERY_WINDOW_SECONDS = int(
    os.environ.get("PHONE_DISTRACTION_RECOVERY_WINDOW_SECONDS", str(15 * 60))
)

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
    "slay the spire": "Slay the Spire",
    "slay": "Slay the Spire",
    "com.humble.SlayTheSpire": "Slay the Spire",
    "com.humble.slaythespire": "Slay the Spire",
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


def _phone_ack_instance_id(source: str, app_name: str) -> str:
    return f"{source}:phone:{app_name}"


def _backlog_ack_instance_id(surface: str, app_name: str | None = None) -> str:
    return f"backlog:{surface}:{(app_name or 'distraction').lower()}"


def _backlog_distraction_still_active(ack: dict) -> bool:
    details = ack.get("details") or {}
    surface = details.get("surface")
    app = (details.get("app") or "").lower()
    if surface == "phone":
        return bool(PHONE_STATE.get("is_distracted")) and (
            not app or (PHONE_STATE.get("current_app") or "").lower() == app
        )
    if surface == "desktop":
        return DESKTOP_STATE.get("current_mode") in ("video", "scrolling", "gaming")
    return bool(PHONE_STATE.get("is_distracted")) or DESKTOP_STATE.get("current_mode") in (
        "video",
        "scrolling",
        "gaming",
    )


def _sync_activity_from_remaining_distraction_signals(now_mono_ms: int) -> bool:
    """Recompute the activity layer after one distraction source is cleared."""
    desktop_mode = DESKTOP_STATE.get("current_mode")
    phone_app = (PHONE_STATE.get("current_app") or "").lower()
    phone_mode = PHONE_DISTRACTION_APPS.get(phone_app) if PHONE_STATE.get("is_distracted") else None

    if phone_mode:
        result = timer_engine.set_activity(
            Activity.DISTRACTION,
            is_scrolling_gaming=phone_mode in ("scrolling", "gaming"),
            now_mono_ms=now_mono_ms,
        )
        return TimerEvent.MODE_CHANGED in result.events

    if desktop_mode in ("video", "scrolling", "gaming"):
        result = timer_engine.set_activity(
            Activity.DISTRACTION,
            is_scrolling_gaming=desktop_mode in ("scrolling", "gaming"),
            now_mono_ms=now_mono_ms,
        )
        return TimerEvent.MODE_CHANGED in result.events

    result = timer_engine.set_activity(
        Activity.WORKING,
        is_scrolling_gaming=False,
        now_mono_ms=now_mono_ms,
    )
    return TimerEvent.MODE_CHANGED in result.events


async def maybe_create_backlog_violation_ack(
    *,
    surface: str,
    app_name: str | None,
    display_name: str | None,
    package: str | None = None,
    distraction_mode: str | None = None,
    trigger: str,
) -> dict | None:
    """Create the compressed backlog-enforcement ack when distraction happens in debt."""
    if DESKTOP_STATE.get("work_mode", "clocked_in") != "clocked_in":
        return None
    if timer_engine.break_balance_ms >= 0:
        return None
    if is_quiet_hours():
        await log_quiet_hours_suppressed(
            source=f"{surface}_detection",
            event_type="backlog_violation_ack_creation",
            app=app_name,
            details={
                "surface": surface,
                "display_name": display_name,
                "package": package,
                "distraction_mode": distraction_mode,
                "trigger": trigger,
                "break_balance_ms": timer_engine.break_balance_ms,
            },
        )
        return None
    instance_id = _backlog_ack_instance_id(surface, app_name)
    active_since = (
        PHONE_STATE.get("app_opened_at")
        if surface == "phone"
        else DESKTOP_STATE.get("last_detection")
    )
    terminal_ack = await _terminal_backlog_ack_for_active_span(instance_id, active_since)
    if terminal_ack:
        await log_event(
            "backlog_violation_ack_suppressed",
            instance_id=instance_id,
            details={
                "surface": surface,
                "app": app_name,
                "display_name": display_name,
                "active_since": active_since,
                "terminal_ack_id": terminal_ack["id"],
                "terminal_status": terminal_ack["status"],
                "trigger": trigger,
                "break_balance_ms": timer_engine.break_balance_ms,
            },
        )
        return None

    ack = await create_expected_ack(
        source="backlog_violation",
        instance_id=instance_id,
        reason=f"Backlog distraction: {display_name or app_name or surface}",
        details={
            "surface": surface,
            "app": app_name,
            "display_name": display_name,
            "package": package,
            "distraction_mode": distraction_mode,
            "active_since": active_since,
            "timer_mode": timer_engine.current_mode.value,
            "break_balance_ms": timer_engine.break_balance_ms,
            "trigger": trigger,
        },
        ack_delay=timedelta(seconds=0),
        level2_delay=timedelta(seconds=15),
        pavlok_delay=timedelta(seconds=15),
    )
    await log_event(
        "backlog_violation_ack_required",
        details={
            "surface": surface,
            "app": app_name,
            "display_name": display_name,
            "distraction_mode": distraction_mode,
            "ack_id": ack["id"],
            "trigger": trigger,
            "break_balance_ms": timer_engine.break_balance_ms,
        },
    )
    return ack


async def _recent_productivity_active() -> bool:
    return (await compute_work_state()).productivity_active


async def maybe_create_phone_distraction_ack(
    *,
    app_name: str,
    display_name: str,
    package: str | None,
    distraction_mode: str,
    trigger: str,
    timer_updated: bool = False,
    min_open_seconds: int | None = None,
    productivity_active: bool | None = None,
) -> dict | None:
    """Create a guarded ack for sustained phone distraction while clocked in."""
    if DESKTOP_STATE.get("work_mode", "clocked_in") != "clocked_in":
        return None
    if not PHONE_STATE.get("is_distracted"):
        return None
    if (PHONE_STATE.get("current_app") or "").lower() != app_name:
        return None
    if is_quiet_hours():
        await log_quiet_hours_suppressed(
            source="phone_detection",
            event_type="phone_distraction_ack_creation",
            app=app_name,
            details={
                "display_name": display_name,
                "package": package,
                "distraction_mode": distraction_mode,
                "trigger": trigger,
            },
        )
        return None
    if timer_engine.break_balance_ms < 0:
        return await maybe_create_backlog_violation_ack(
            surface="phone",
            app_name=app_name,
            display_name=display_name,
            package=package,
            distraction_mode=distraction_mode,
            trigger=trigger,
        )
    if productivity_active is None:
        productivity_active = await _recent_productivity_active()
    if productivity_active:
        return None

    opened_at_raw = PHONE_STATE.get("app_opened_at")
    try:
        opened_at = datetime.fromisoformat(opened_at_raw) if opened_at_raw else datetime.now()
    except Exception:
        opened_at = datetime.now()
    open_seconds = (datetime.now() - opened_at).total_seconds()
    threshold = (
        PHONE_DISTRACTION_ACK_AFTER_SECONDS if min_open_seconds is None else min_open_seconds
    )
    if open_seconds < threshold:
        return None

    if PHONE_STATE.get("distraction_ack_app") == app_name and PHONE_STATE.get("distraction_ack_id"):
        return None

    ack = await create_expected_ack(
        source="phone_distraction",
        instance_id=_phone_ack_instance_id("phone_distraction", app_name),
        reason=f"Phone distraction during work: {display_name}",
        details={
            "app": app_name,
            "display_name": display_name,
            "package": package,
            "distraction_mode": distraction_mode,
            "timer_mode": timer_engine.current_mode.value,
            "timer_updated": timer_updated,
            "trigger": trigger,
            "open_seconds": round(open_seconds),
            "break_balance_ms": timer_engine.break_balance_ms,
        },
    )
    PHONE_STATE["distraction_ack_app"] = app_name
    PHONE_STATE["distraction_ack_id"] = ack["id"]
    await log_event(
        "phone_distraction_ack_required",
        details={
            "app": app_name,
            "display_name": display_name,
            "package": package,
            "distraction_mode": distraction_mode,
            "timer_mode": timer_engine.current_mode.value,
            "ack_id": ack["id"],
            "trigger": trigger,
            "open_seconds": round(open_seconds),
        },
    )
    return ack


async def acknowledge_phone_acks(app_name: str) -> int:
    count = 0
    for source in ("phone_distraction", "phone_gaming", "backlog_violation"):
        instance_id = (
            _backlog_ack_instance_id("phone", app_name)
            if source == "backlog_violation"
            else _phone_ack_instance_id(source, app_name)
        )
        try:
            result = await _resolve_expected_ack(
                ack_id=None,
                source=source,
                instance_id=instance_id,
                status="acknowledged",
            )
            if result.get("updated"):
                count += 1
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
    if PHONE_STATE.get("distraction_ack_app") == app_name:
        PHONE_STATE["distraction_ack_app"] = None
        PHONE_STATE["distraction_ack_id"] = None
    return count


async def recover_recent_phone_distraction_state() -> bool:
    """Restore an open phone distraction after restart when no close event followed it."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT details, created_at
            FROM events
            WHERE event_type IN ('phone_distraction_allowed', 'phone_distraction_ack_required')
              AND created_at >= datetime('now', ?)
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (f"-{PHONE_DISTRACTION_RECOVERY_WINDOW_SECONDS} seconds",),
        )
        rows = await cursor.fetchall()
    if not rows:
        return False

    row = None
    details = {}
    app_name = ""
    distraction_mode = ""
    for candidate in rows:
        try:
            candidate_details = json.loads(candidate["details"] or "{}")
        except Exception:
            continue
        candidate_app = (candidate_details.get("app") or "").lower()
        candidate_mode = PHONE_DISTRACTION_APPS.get(candidate_app) or candidate_details.get(
            "distraction_mode"
        )
        if not candidate_app or not candidate_mode:
            continue
        async with aiosqlite.connect(DB_PATH) as db:
            close_cursor = await db.execute(
                """
                SELECT 1
                FROM events
                WHERE event_type = 'phone_app_closed'
                  AND created_at > ?
                  AND json_extract(details, '$.app') = ?
                LIMIT 1
                """,
                (candidate["created_at"], candidate_app),
            )
            close_row = await close_cursor.fetchone()
        if close_row:
            continue
        row = candidate
        details = candidate_details
        app_name = candidate_app
        distraction_mode = candidate_mode
        break
    if not row:
        return False

    try:
        event_utc = datetime.fromisoformat(row["created_at"])
        age_seconds = max(0, (datetime.utcnow() - event_utc).total_seconds())
    except Exception:
        age_seconds = 0
    opened_at = datetime.now() - timedelta(seconds=age_seconds)
    PHONE_STATE["current_app"] = app_name
    PHONE_STATE["app_opened_at"] = opened_at.isoformat()
    PHONE_STATE["last_activity"] = datetime.now().isoformat()
    PHONE_STATE["is_distracted"] = True
    DESKTOP_STATE["current_mode"] = distraction_mode
    DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
    timer_engine.set_activity(
        Activity.DISTRACTION,
        is_scrolling_gaming=distraction_mode in ("scrolling", "gaming"),
        now_mono_ms=int(time.monotonic() * 1000),
    )
    await log_event(
        "phone_distraction_state_recovered",
        details={
            "app": app_name,
            "display_name": get_phone_app_display_name(app_name),
            "distraction_mode": distraction_mode,
            "event_age_seconds": round(age_seconds),
        },
    )
    return True


# MacroDroid trigger name → internal app key
# MacroDroid's "trigger that fired" gives: "Application Launched (X)", "Application Closed (X)"
# The name in parens is the app's display name as configured in the trigger.
# This map resolves that display name to the key used in PHONE_DISTRACTION_APPS.
MACRODROID_TRIGGER_APP_MAP = {
    "x": "twitter",
    "youtube": "youtube",
    "thronefall": "game",
    "slice & dice": "game",
    "20 minutes till dawn": "game",
    "onebit adventure": "game",
    "minecraft": "minecraft",
    "slay the spire": "slay the spire",
    "spotify": "spotify",
}


_APP_TRIGGER_RE = re.compile(r"Application (?:Launched|Closed) \((.+)\)", re.IGNORECASE)
_GEO_TRIGGER_RE = re.compile(r"Geofence (Entry|Exit) \((.+)\)", re.IGNORECASE)


def parse_macrodroid_trigger(raw: str) -> dict:
    """Parse any MacroDroid trigger name into a structured dict.

    Returns dict with keys:
      type: "app" | "geofence" | "unknown"
      + type-specific fields

    App triggers:
      "Application Launched (X)" → {type: "app", app: "twitter", action: "open"}
      "Application Closed (YouTube)" → {type: "app", app: "youtube", action: "close"}

    Geofence triggers:
      "Geofence Entry (Home)" → {type: "geofence", location: "home", action: "enter"}
      "Geofence Exit (Gym)" → {type: "geofence", location: "gym", action: "exit"}

    Passthrough:
      "twitter" → {type: "unknown", raw: "twitter"}
    """
    if not raw:
        return {"type": "unknown", "raw": ""}

    stripped = raw.strip()

    # App trigger
    m = _APP_TRIGGER_RE.match(stripped)
    if m:
        display_name = m.group(1).strip().lower()
        app_key = MACRODROID_TRIGGER_APP_MAP.get(display_name, display_name)
        action = "open" if "launched" in stripped.lower() else "close"
        return {"type": "app", "app": app_key, "action": action}

    # Geofence trigger
    m = _GEO_TRIGGER_RE.match(stripped)
    if m:
        direction = m.group(1).lower()  # "entry" or "exit"
        location = m.group(2).strip().lower()
        action = "enter" if direction == "entry" else "exit"
        return {"type": "geofence", "location": location, "action": action}

    # Passthrough
    return {"type": "unknown", "raw": stripped.lower()}


# Backwards compat alias
def parse_macrodroid_trigger_app(raw: str) -> str:
    parsed = parse_macrodroid_trigger(raw)
    return parsed.get("app", parsed.get("raw", ""))


# ============ Timer I/O Functions ============


# [MOVED to shared.py] — _sync_log_shift, timer_log_shift
from shared import timer_log_shift


def _sync_generate_daily_analytics(date_str: str):
    """Generate daily timer analytics from timer_shifts.

    Writes:
    1. Summary fields to the daily note's YAML front matter
    2. Full JSON to Imperium-ENV/Journal/Daily/analytics/ for programmatic access
    Then wipes timer_shifts table.
    """
    import json
    import sqlite3
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
        "avg_active_instances": round(sum(instance_counts) / len(instance_counts), 1)
        if instance_counts
        else 0,
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
        update_frontmatter(
            note_path,
            {
                "timer_total_shifts": summary["total_shifts"],
                "timer_enforcements": enforcement_count,
                "timer_twitter_shifts": twitter_shifts,
                "timer_peak_break": format_timer_time(peak_balance),
                "timer_min_break": format_timer_time(
                    min_balance if min_balance != float("inf") else 0
                ),
                "timer_avg_instances": summary["avg_active_instances"],
                "timer_max_instances": summary["max_active_instances"],
            },
        )

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
            await log_event(
                "timer_daily_analytics_generated", details={"file": result, "date": date_str}
            )
        else:
            print(f"TIMER: No shift data for {date_str}, skipping analytics")
    except Exception as e:
        print(f"TIMER: Failed to generate daily analytics: {e}")


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
        session_count = (
            conn.execute("SELECT COUNT(*) FROM timer_sessions WHERE date = ?", (today,)).fetchone()[
                0
            ]
            or 0
        )
        mode_change_count = (
            conn.execute(
                "SELECT COUNT(*) FROM timer_mode_changes WHERE timestamp LIKE ?", (f"{today}%",)
            ).fetchone()[0]
            or 0
        )
        conn.close()
    except Exception:
        pass  # Silently skip if DB query fails

    update_frontmatter(
        note_path,
        {
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
        },
    )


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
        (state_json,),
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
        (datetime.now().isoformat(), old_mode, new_mode, 1 if is_automatic else 0),
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
        (date, datetime.now().isoformat(), mode),
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


def _sync_end_session(
    session_id: int, duration_ms: int, break_earned_ms: int = 0, break_used_ms: int = 0
):
    """End a timer session."""
    import sqlite3
    from datetime import datetime

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """UPDATE timer_sessions SET end_time = ?, duration_ms = ?, break_earned_ms = ?, break_used_ms = ?
           WHERE id = ?""",
        (datetime.now().isoformat(), duration_ms, break_earned_ms, break_used_ms, session_id),
    )
    conn.commit()
    conn.close()


async def timer_end_session(
    session_id: int, duration_ms: int, break_earned_ms: int = 0, break_used_ms: int = 0
):
    """End a timer session asynchronously."""
    try:
        await asyncio.to_thread(
            _sync_end_session, session_id, duration_ms, break_earned_ms, break_used_ms
        )
    except Exception as e:
        print(f"TIMER: Failed to end session: {e}")


def _sync_save_daily_score(
    date: str,
    productivity_score: int,
    total_work_ms: int,
    total_break_used_ms: int,
    session_count: int,
    mode_change_count: int,
):
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
        (
            date,
            productivity_score,
            total_work_ms,
            total_break_used_ms,
            session_count,
            mode_change_count,
        ),
    )
    conn.commit()
    conn.close()


async def timer_save_daily_score(
    date: str,
    productivity_score: int,
    total_work_ms: int,
    total_break_used_ms: int,
    session_count: int,
    mode_change_count: int,
):
    """Save daily productivity score asynchronously."""
    try:
        await asyncio.to_thread(
            _sync_save_daily_score,
            date,
            productivity_score,
            total_work_ms,
            total_break_used_ms,
            session_count,
            mode_change_count,
        )
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
            print(
                f"TIMER: Restored state from DB (mode={timer_engine.current_mode.value}, break={timer_engine.break_balance_ms / 1000:.0f}s)"
            )
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
    await log_event(
        "timer_daily_reset",
        details={
            "source": "9am_scheduler",
            "productivity_score": result.productivity_score,
            "date": today,
        },
    )


# ============ Audio Proxy State ============
# Tracks phone audio proxy status for routing phone audio through PC

AUDIO_PROXY_STATE = {
    "phone_connected": False,
    "receiver_running": False,
    "receiver_pid": None,
    "last_connect_time": None,
    "last_disconnect_time": None,
}

PHONE_YOUTUBE_APP_KEYS = {
    "youtube",
    "com.google.android.youtube",
    "yt",
    "yt_bg",
    "youtube background",
}


def phone_youtube_active() -> bool:
    """Return true when phone telemetry says YouTube is the active media source."""
    app = (PHONE_STATE.get("current_app") or "").strip().lower()
    return bool(PHONE_STATE.get("is_distracted")) and app in PHONE_YOUTUBE_APP_KEYS


def send_tts_transport_control(command: str = "toggle") -> dict:
    """Send transport control to the WSL TTS satellite."""
    host = DESKTOP_CONFIG["host"]
    port = DESKTOP_CONFIG["port"]
    try:
        response = requests.post(
            f"http://{host}:{port}/tts/control",
            json={"command": command},
            timeout=3,
        )
        return {
            "success": response.status_code == 200,
            "status_code": response.status_code,
            "body": response.text[:200],
        }
    except requests.exceptions.Timeout:
        return {"success": False, "error": "timeout"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": "connection_error"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============ Headless Mode (disabled on macOS) ============


def get_headless_state() -> dict:
    """Headless mode is not applicable on macOS."""
    return {
        "enabled": False,
        "last_changed": None,
        "hostname": None,
        "error": "not applicable on macOS",
    }


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


# [MOVED to enforcement_service.py] — close_distraction_windows, enforce_desktop_app, check_desktop_reachable
from enforcement_service import (
    check_desktop_reachable,
    close_distraction_windows,
    enforce_desktop_app,
)


def trigger_obsidian_command_async(command_id: str, no_focus: bool = False):
    """Fire-and-forget Obsidian trigger (log-only on macOS)."""
    trigger_obsidian_command(command_id, no_focus)


def trigger_obsidian_command(command_id: str, no_focus: bool = False) -> bool:
    """Log Obsidian command (Obsidian is just a log sink now, not a runtime dependency)."""
    logger.info(f"OBSIDIAN: command '{command_id}' (log-only, no_focus={no_focus})")
    return True


def enforce_phone_app(app_name: str, action: str = "disable", _auto_retry: bool = True) -> dict:
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
    pavlok_result = None

    if action != "enable":
        # /enforce must not depend solely on the mobile MacroDroid route.
        pavlok_result = send_pavlok_stimulus(
            "zap",
            30,
            reason=f"manual_phone_enforce_{action}_{app_name}",
            respect_cooldown=False,
        )
        if PHONE_STATE.get("reachable") is False:
            return {
                "success": bool(pavlok_result.get("success")),
                "phone_skipped": True,
                "reason": "phone_known_offline",
                "pavlok": pavlok_result,
            }

    try:
        response = requests.get(url, params=params, timeout=timeout)
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = datetime.now().isoformat()

        print(f"PHONE: Enforce {action} {app_name} -> {response.status_code}")
        # Detect Shizuku death from enforce response
        try:
            resp_json = response.json()
            if resp_json.get("status") == "shizuku_dead" and _auto_retry:
                logger.warning(f"PHONE: Shizuku dead during enforce {action} {app_name}")
                SHIZUKU_STATE["dead"] = True
                asyncio.get_event_loop().create_task(_enforce_shizuku_retry(app_name, action))
                return {"success": False, "error": "shizuku_dead", "restart_initiated": True}
        except (ValueError, AttributeError):
            pass
        return {
            "success": response.status_code == 200
            or bool(pavlok_result and pavlok_result.get("success")),
            "status_code": response.status_code,
            "response": response.text[:200] if response.text else None,
            "pavlok": pavlok_result,
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


async def _enforce_shizuku_retry(app_name: str, action: str):
    """Restart Shizuku and retry enforce once. Fire-and-forget via create_task."""
    try:
        logger.info(f"PHONE: Auto-restarting Shizuku for {action} {app_name}")
        restart_result = await attempt_shizuku_restart()
        await log_event(
            "shizuku_auto_restart",
            device_id="Token-S24",
            details={"trigger": f"enforce_{action}_{app_name}", "result": restart_result},
        )
        if not restart_result.get("success"):
            logger.warning(f"PHONE: Shizuku restart failed, skipping retry for {action} {app_name}")
            return
        await asyncio.sleep(3)  # Wait for Shizuku init
        retry_result = await asyncio.to_thread(enforce_phone_app, app_name, action, False)
        logger.info(f"PHONE: Enforce retry {action} {app_name} -> {retry_result}")
        await log_event(
            "enforce_shizuku_retry",
            device_id="Token-S24",
            details={"app": app_name, "action": action, "result": retry_result},
        )
    except Exception as e:
        logger.error(f"PHONE: Shizuku retry failed for {action} {app_name}: {e}")


# [MOVED to phone_service.py] — _send_to_phone

# Wire TTS route dependencies
from routes.tts import init_deps as tts_init_deps

tts_init_deps(send_to_phone=_send_to_phone)

# Wire enforce + notify dependencies (atomic emitter + device-aware dispatcher)
from enforce import EnforceRequest, enforce
from enforce import init_deps as enforce_init_deps
from notify import NotifyRequest, dispatch_notification
from notify import init_deps as notify_init_deps

notify_init_deps(send_to_phone=_send_to_phone)
enforce_init_deps(is_quiet_hours=is_quiet_hours)

# Wire voice route dependencies
from routes.voice import init_deps as voice_init_deps

voice_init_deps(schedule_pedal_enter=_schedule_pedal_enter)


def _enforcement_state_payload(
    *,
    source: str,
    app: str | None = None,
    phone_app: str | None = None,
    ack_source: str | None = None,
    **extra,
) -> dict:
    """Build Custodes enforcement-state payloads without app/ack slot bleed.

    `phone_app`/`app` are foreground application telemetry fields for the
    phone path. Internal acknowledgement identifiers (AskUserQuestion,
    Golden Throne, expected-ack namespaces) are diagnostic ack sources and must
    never populate the phone-app slots.
    """
    payload = dict(extra)
    if source == "phone":
        resolved_phone_app = phone_app or app
        if resolved_phone_app is not None:
            payload["app"] = resolved_phone_app
        payload["phone_app"] = resolved_phone_app
    else:
        if ack_source is None and app is not None:
            ack_source = app
        if ack_source is not None:
            payload["ack_source"] = ack_source
        payload["phone_app"] = None
    return payload


def start_enforcement_cascade(app_name: str) -> None:
    """Fire a single atomic enforce for a distraction app.

    Replaces the legacy 5-level cascade. Golden Throne owns any repetition.
    Distraction-source is set to "phone" so the notification is never routed
    back to the device the user is being asked to put down.
    """
    if is_quiet_hours():
        print(f"ENFORCE: quiet hours suppressed for {app_name}")
        try:
            asyncio.ensure_future(
                log_quiet_hours_suppressed(
                    source="phone",
                    event_type="phone_distraction_enforce",
                    app=app_name,
                )
            )
        except RuntimeError:
            pass
        return

    print(f"ENFORCE: phone distraction app={app_name}")
    try:
        asyncio.ensure_future(
            handle_custodes_state_event(
                "phone_distraction_enforce",
                "phone",
                severity=4,
                payload=_enforcement_state_payload(source="phone", app=app_name),
            )
        )
    except RuntimeError:
        pass
    try:
        asyncio.ensure_future(
            enforce(
                EnforceRequest(
                    message=f"Close {app_name}",
                    intensity=50,
                    distraction_source="phone",
                    source=f"phone_distraction_{app_name}",
                )
            )
        )
    except RuntimeError:
        pass


def stop_enforcement_cascade(reason: str = "app_close") -> None:
    """No-op shim retained for in-tree callers after cascade removal.

    Golden Throne now owns any ongoing escalation state, so there is nothing
    to stop here. Kept so prompt-submit, quiet-enter, negative-edge close,
    and work-action paths keep their call signatures.
    """
    asyncio.ensure_future(log_event("enforcement_stop_shim", details={"reason": reason}))


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
    productivity_active = timer_engine.productivity_active
    active_count = 0
    should_close = not productivity_active
    work_state = await get_cached_work_state()

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
            "window_title": request.window_title if request else None,
            "work_state": work_state.model_dump(),
        },
    )

    return WindowEnforceResponse(
        productivity_active=productivity_active,
        active_instance_count=active_count,
        should_close_distractions=should_close,
        distraction_apps=DISTRACTION_APPS,
        reason=reason,
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

    await log_event("manual_enforcement", details={"result": result})

    return {"action": "close_distractions", "result": result}


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
    steam_details = {
        "steam_app_id": request.steam_app_id,
        "steam_app_name": request.steam_app_name,
        "steam_exe": request.steam_exe,
    }

    # Validate detected mode
    if detected_mode not in VALID_DETECTION_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid detected_mode '{detected_mode}'. Valid: {VALID_DETECTION_MODES}",
        )

    work_mode = DESKTOP_STATE.get("work_mode", "clocked_in")
    print(
        f">>> Desktop detection from {source}: mode={detected_mode} window='{window_title}' work_mode={work_mode}"
    )
    if detected_mode in ("video", "scrolling", "gaming"):
        await bust_quiet_state(
            "desktop_detection",
            "desktop_distraction_detected",
            {
                "detected_mode": detected_mode,
                "window_title": window_title,
                **steam_details,
            },
        )

    # Get current mode
    current_mode = DESKTOP_STATE["current_mode"]
    was_timer_break_mode = timer_engine.current_mode == TimerMode.BREAK

    # Check if mode change is needed
    if detected_mode == current_mode:
        if detected_mode == "gaming":
            DESKTOP_STATE["steam_app_id"] = request.steam_app_id
            DESKTOP_STATE["steam_app_name"] = request.steam_app_name
            DESKTOP_STATE["steam_exe"] = request.steam_exe
            DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
        print(f"    Mode unchanged ({detected_mode}), skipping")
        return DesktopDetectionResponse(
            action="none",
            detected_mode=detected_mode,
            reason="mode_unchanged",
            productivity_active=True,
            active_instance_count=0,
            timer_updated=False,
        )

    # Startup grace period: ignore transitions TO silence for N seconds after
    # server start. AHK restarts detect silence before catching real audio state.
    grace_secs = DESKTOP_STATE.get("startup_grace_secs", 0)
    if grace_secs > 0 and detected_mode == "silence" and current_mode != "silence":
        elapsed = time.time() - DESKTOP_STATE.get("startup_time", 0)
        if elapsed < grace_secs:
            remaining = round(grace_secs - elapsed, 1)
            print(
                f"    GRACE PERIOD: Ignoring silence detection ({remaining}s remaining, current={current_mode})"
            )
            return DesktopDetectionResponse(
                action="none",
                detected_mode=detected_mode,
                reason=f"startup_grace_period ({remaining}s remaining)",
                productivity_active=True,
                active_instance_count=0,
                timer_updated=False,
            )

    work_state = await compute_work_state()
    productivity_active = work_state.productivity_active
    active_count = work_state.active_instance_count + work_state.observed_agent_count

    # Determine if mode change is allowed
    allowed = True
    reason = "allowed"

    # CLOCKED OUT: All modes allowed, no enforcement
    if work_mode == "clocked_out":
        allowed = True
        reason = "clocked_out"
        print("    Clocked out - all modes allowed")
    # GYM MODE: All modes allowed (gym has its own timer logic)
    elif work_mode == "gym":
        allowed = True
        reason = "gym_mode"
        print("    Gym mode - all modes allowed")
    # CLOCKED IN: Video/gaming mode requires either break time OR productivity
    elif detected_mode == "video" or detected_mode == "gaming":
        has_break_time = timer_engine.break_balance_ms > 0
        break_secs = round(timer_engine.break_balance_ms / 1000)

        if break_secs < 0:
            allowed = True
            reason = "backlog_violation"
            print(f"    {detected_mode.title()} in backlog: creating compressed enforcement ack")
        elif has_break_time:
            allowed = True
            reason = "break_time_available"
            print(f"    {detected_mode.title()} allowed: {break_secs}s break available")
        elif detected_mode == "gaming":
            allowed = True
            reason = "gaming_ack_required"
            print("    Gaming allowed with expected acknowledgement ladder")
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
        if old_mode in ("video", "scrolling", "gaming") and detected_mode not in (
            "video",
            "scrolling",
            "gaming",
        ):
            acknowledged_acks = await acknowledge_backlog_surface_acks("desktop")
            await log_event(
                "enforcement_negative_edge",
                details={
                    "surface": "desktop",
                    "old_mode": old_mode,
                    "new_mode": detected_mode,
                    "window_title": window_title,
                    "acknowledged_expected_acks": acknowledged_acks,
                    "reason": "desktop_distraction_closed",
                },
            )
        DESKTOP_STATE["current_mode"] = detected_mode
        DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
        DESKTOP_STATE["steam_app_id"] = request.steam_app_id if detected_mode == "gaming" else None
        DESKTOP_STATE["steam_app_name"] = (
            request.steam_app_name if detected_mode == "gaming" else None
        )
        DESKTOP_STATE["steam_exe"] = request.steam_exe if detected_mode == "gaming" else None

        # Track meeting state (suppresses TTS)
        was_meeting = DESKTOP_STATE["in_meeting"]
        DESKTOP_STATE["in_meeting"] = detected_mode == "meeting"
        if DESKTOP_STATE["in_meeting"] and not was_meeting:
            print("    MEETING STARTED: TTS suppressed")
        elif was_meeting and not DESKTOP_STATE["in_meeting"]:
            print("    MEETING ENDED: TTS resumed")

        # Update timer activity layer
        now_ms = int(time.monotonic() * 1000)
        old_timer_mode = timer_engine.current_mode.value

        was_focused = timer_engine.focus_active
        if detected_mode in ("video", "scrolling", "gaming"):
            is_sg = detected_mode in ("scrolling", "gaming")
            result = timer_engine.set_activity(
                Activity.DISTRACTION, is_scrolling_gaming=is_sg, now_mono_ms=now_ms
            )
        else:
            result = timer_engine.set_activity(
                Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=now_ms
            )

        # Log focus auto-exit on distraction
        if was_focused and not timer_engine.focus_active:
            focus_min = round(timer_engine.total_focus_time_ms / 60000)
            await log_event(
                "focus_toggle",
                details={
                    "action": "off",
                    "trigger": "distraction",
                    "detected_mode": detected_mode,
                    "total_focus_time_ms": timer_engine.total_focus_time_ms,
                    "focus_cutoff_time": timer_engine.focus_cutoff_time,
                },
            )
            loop = asyncio.get_event_loop()
            loop.run_in_executor(
                None, speak_tts, f"Focus broken by {detected_mode}. {focus_min} minutes earned."
            )

        timer_updated = TimerEvent.MODE_CHANGED in result.events
        if timer_updated:
            await timer_log_shift(
                old_timer_mode,
                timer_engine.current_mode.value,
                trigger="desktop_detection",
                source="ahk",
            )
            if timer_engine.current_mode == TimerMode.WORKING:
                _mark_mewgenics_work_action("timer_mode_working", "desktop_detection")

        ack = None
        if reason == "backlog_violation":
            ack = await maybe_create_backlog_violation_ack(
                surface="desktop",
                app_name=request.steam_app_id or request.steam_exe or detected_mode,
                display_name=request.steam_app_name or window_title or detected_mode,
                distraction_mode=detected_mode,
                trigger="desktop_detection",
            )
            close_result = (
                {"skipped": True, "reason": "quiet_hours"}
                if is_quiet_hours()
                else close_distraction_windows()
            )
            if is_quiet_hours():
                await log_quiet_hours_suppressed(
                    source="desktop_detection",
                    event_type="desktop_backlog_enforcement",
                    app=request.steam_app_id or request.steam_exe or detected_mode,
                    details={"detected_mode": detected_mode, "window_title": window_title},
                )
            await log_event(
                "desktop_backlog_violation",
                details={
                    "detected_mode": detected_mode,
                    "window_title": window_title,
                    **steam_details,
                    "ack_id": ack["id"] if ack else None,
                    "enforcement": close_result,
                    "break_balance_ms": timer_engine.break_balance_ms,
                },
            )
        await log_event(
            "desktop_mode_change",
            details={
                "old_mode": old_mode,
                "new_mode": detected_mode,
                "window_title": window_title,
                "source": source,
                **steam_details,
                "timer_updated": timer_updated,
                "productivity_active": productivity_active,
                "active_instances": active_count,
                "work_state": work_state.model_dump(),
                "created_ack": bool(ack),
                "ack_id": ack["id"] if ack else None,
            },
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
            active_instance_count=active_count,
        )
    else:
        if is_quiet_hours():
            suppression = await log_quiet_hours_suppressed(
                source="desktop_detection",
                event_type="desktop_mode_blocked",
                app=detected_mode,
                details={
                    "detected_mode": detected_mode,
                    "reason": reason,
                    "window_title": window_title,
                    **steam_details,
                    "productivity_active": productivity_active,
                    "active_instances": active_count,
                },
            )
            return DesktopDetectionResponse(
                action="none",
                detected_mode=detected_mode,
                reason="quiet_hours",
                productivity_active=productivity_active,
                active_instance_count=active_count,
                timer_updated=False,
            )
        # Mode change blocked - immediately enforce by closing distraction windows
        print(f"<<< Mode change BLOCKED: {detected_mode} | reason={reason}")

        enforce_result = close_distraction_windows()
        send_pavlok_stimulus(reason="desktop_distraction_blocked")
        asyncio.create_task(
            asyncio.to_thread(
                _send_to_phone,
                "/notify",
                {
                    "vibe": 30,
                    "banner_text": f"Desktop blocked: {detected_mode}",
                },
            )
        )

        await log_event(
            "desktop_mode_blocked",
            details={
                "detected_mode": detected_mode,
                "reason": reason,
                "window_title": window_title,
                "source": source,
                **steam_details,
                "productivity_active": productivity_active,
                "active_instances": active_count,
                "enforcement": enforce_result,
            },
        )
        asyncio.create_task(
            handle_custodes_state_event(
                "desktop_mode_blocked",
                "desktop",
                severity=2,
                payload={
                    "desktop_mode": detected_mode,
                    "reason": reason,
                    "window_title": window_title,
                    **steam_details,
                    "active_instances": active_count,
                },
            )
        )

        raise HTTPException(
            status_code=403,
            detail=DesktopDetectionResponse(
                action="blocked",
                detected_mode=detected_mode,
                reason=reason,
                timer_updated=False,
                productivity_active=productivity_active,
                active_instance_count=active_count,
            ).model_dump(),
        )


MEWGENICS_SPACE_STATE = {
    "seen_space": False,
    "work_action_since_last_space": True,
    "last_space_at": None,
    "last_work_action_at": None,
    "last_work_action_source": None,
}


def _mark_mewgenics_work_action(source: str, note: str | None = None) -> None:
    """Record work evidence for the next Mewgenics space-pair policy check."""
    MEWGENICS_SPACE_STATE["work_action_since_last_space"] = True
    MEWGENICS_SPACE_STATE["last_work_action_at"] = datetime.now().isoformat()
    MEWGENICS_SPACE_STATE["last_work_action_source"] = source
    if note:
        MEWGENICS_SPACE_STATE["last_work_action_note"] = note


def _reset_mewgenics_space_pair(reason: str) -> None:
    MEWGENICS_SPACE_STATE["seen_space"] = False
    MEWGENICS_SPACE_STATE["work_action_since_last_space"] = True
    MEWGENICS_SPACE_STATE["reset_reason"] = reason


@app.post("/api/telemetry/mewgenics-space", response_model=MewgenicsSpaceTelemetryResponse)
async def handle_mewgenics_space_telemetry(request: MewgenicsSpaceTelemetryRequest):
    """Consume policy-free Mewgenics Space key telemetry and apply server-side zap policy."""
    if request.event != "mewgenics_space":
        raise HTTPException(status_code=400, detail="event must be mewgenics_space")

    now = datetime.now().isoformat()
    timer_mode = timer_engine.current_mode.value
    details = {
        "event": request.event,
        "source": request.source,
        "client_ts": request.ts,
        "timer_mode": timer_mode,
        "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "work_action_since_last_space": MEWGENICS_SPACE_STATE.get(
            "work_action_since_last_space", True
        ),
        "seen_space": MEWGENICS_SPACE_STATE.get("seen_space", False),
        "last_space_at": MEWGENICS_SPACE_STATE.get("last_space_at"),
        "last_work_action_at": MEWGENICS_SPACE_STATE.get("last_work_action_at"),
        "last_work_action_source": MEWGENICS_SPACE_STATE.get("last_work_action_source"),
    }

    zap_fired = False
    reason = "telemetry_only"
    pavlok_result = None

    if timer_engine.current_mode == TimerMode.BREAK:
        reason = "break_mode"
        _reset_mewgenics_space_pair(reason)
    elif timer_engine.current_mode in (TimerMode.WORKING, TimerMode.MULTITASKING):
        if MEWGENICS_SPACE_STATE.get("seen_space", False) and not MEWGENICS_SPACE_STATE.get(
            "work_action_since_last_space", True
        ):
            if is_quiet_hours():
                reason = "suppressed_by_quiet_hours"
                await log_quiet_hours_suppressed(
                    source="mewgenics_space",
                    event_type="mewgenics_space_zap",
                    app="mewgenics",
                    details=details,
                )
            else:
                pavlok_result = await asyncio.to_thread(
                    send_pavlok_stimulus,
                    "zap",
                    PAVLOK_CONFIG.get("friday_zap_value", 30),
                    "mewgenics_space",
                    True,
                )
                zap_fired = bool(pavlok_result.get("success")) and not pavlok_result.get(
                    "blocked_by_guardrail"
                )
                reason = "direct_zap" if zap_fired else "blocked_by_guardrail"
        else:
            reason = "armed"
        MEWGENICS_SPACE_STATE["seen_space"] = True
        MEWGENICS_SPACE_STATE["work_action_since_last_space"] = False
    else:
        reason = "timer_mode_ignored"
        _reset_mewgenics_space_pair(reason)

    MEWGENICS_SPACE_STATE["last_space_at"] = now
    details.update(
        {
            "reason": reason,
            "zap_fired": zap_fired,
            "pavlok_result": pavlok_result,
            "state_after": dict(MEWGENICS_SPACE_STATE),
        }
    )
    await log_event("mewgenics_space", device_id="desktop", details=details)
    return MewgenicsSpaceTelemetryResponse(
        recorded=True,
        reason=reason,
        zap_fired=zap_fired,
    )


@app.post("/games/turn", response_model=GameTurnResponse)
async def handle_game_turn(request: GameTurnRequest):
    """Record legacy game-specific turn-end events without creating acknowledgement ladders."""
    game = request.game.strip().lower()
    if not game:
        raise HTTPException(status_code=400, detail="game is required")

    details = {
        "game": game,
        "steam_app_id": request.steam_app_id,
        "steam_app_name": request.steam_app_name,
        "steam_exe": request.steam_exe,
        "source": request.source,
    }
    details["created_ack"] = False
    details["legacy_policy"] = "ack_creation_disabled"
    await log_event("game_turn_end", device_id="desktop", details=details)
    logger.info(f"Game turn-end recorded: game={game} appid={request.steam_app_id}")

    return GameTurnResponse(
        recorded=True,
        block=False,
        reason="observational_only",
        ack_id=None,
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
        "desktop_manual_enforcement", details={"app": app, "action": action, "result": result}
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


# ============ Media Transport Passthrough ============


@app.post("/api/media/pause")
async def media_pause_passthrough(request: MediaPauseRequest | None = None):
    """
    Pause key dispatcher for the WSL/desktop media key path.

    If phone telemetry says YouTube is active, route pause to the phone's
    MacroDroid `/pause` endpoint so Bluetooth-sink phone audio pauses at the
    source. Otherwise preserve the old desktop behavior by toggling WSL TTS
    transport.
    """
    request = request or MediaPauseRequest()
    current_app = PHONE_STATE.get("current_app")
    youtube_active = phone_youtube_active()
    audio_proxy_connected = bool(AUDIO_PROXY_STATE.get("phone_connected"))

    if youtube_active:
        result = await asyncio.to_thread(
            _send_to_phone,
            "/pause",
            {"source": request.source},
        )
        payload = {
            "handled": bool(result.get("success")),
            "target": "phone_youtube",
            "phone_app": current_app,
            "audio_proxy_connected": audio_proxy_connected,
            "phone_result": result,
            "source": request.source,
        }
        await log_event("media_pause_passthrough", device_id="phone", details=payload)
        return payload

    result = await asyncio.to_thread(send_tts_transport_control, "toggle")
    payload = {
        "handled": bool(result.get("success")),
        "target": "tts",
        "phone_app": current_app,
        "audio_proxy_connected": audio_proxy_connected,
        "tts_result": result,
        "source": request.source,
    }
    await log_event("media_pause_passthrough", details=payload)
    return payload


# ============ Phone Heartbeat Endpoints ============


@app.post("/api/phone/heartbeat")
async def phone_heartbeat(request: Request):
    """Record phone heartbeat — resets silence timer and clears alert state."""
    body = await request.json()
    PHONE_HEARTBEAT["last_seen"] = datetime.utcnow()
    PHONE_HEARTBEAT["device_id"] = body.get("device_id", "phone")
    PHONE_HEARTBEAT["alert_state"] = None  # Reset alert on heartbeat
    return {"ok": True, "timestamp": PHONE_HEARTBEAT["last_seen"].isoformat()}


@app.get("/api/phone/heartbeat/status")
async def phone_heartbeat_status():
    """Return last heartbeat time, silence duration, and current alert state."""
    last = PHONE_HEARTBEAT["last_seen"]
    if last is None:
        return {
            "last_heartbeat": None,
            "silence_minutes": None,
            "alert_state": None,
            "device_id": None,
        }
    silence_minutes = (datetime.utcnow() - last).total_seconds() / 60
    return {
        "last_heartbeat": last.isoformat(),
        "silence_minutes": round(silence_minutes, 1),
        "alert_state": PHONE_HEARTBEAT["alert_state"],
        "device_id": PHONE_HEARTBEAT["device_id"],
    }


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
        old_app_norm = (old_app or "").lower()
        close_matches_current = bool(old_app_norm) and (
            old_app_norm == app_name or (package and old_app_norm == package.lower())
        )
        opened_at = PHONE_STATE.get("app_opened_at")
        duration_seconds = None
        if opened_at and close_matches_current:
            try:
                duration_seconds = round(
                    (datetime.now() - datetime.fromisoformat(opened_at)).total_seconds()
                )
            except Exception:
                duration_seconds = None
        if close_matches_current:
            PHONE_STATE["current_app"] = None
            PHONE_STATE["app_opened_at"] = None
            PHONE_STATE["is_distracted"] = False
        PHONE_STATE["last_activity"] = datetime.now().isoformat()

        # Clear Twitter tracking on close
        if app_name in ("twitter", "x", "com.twitter.android"):
            PHONE_STATE["twitter_open_since"] = None
            PHONE_STATE["twitter_zapped"] = False  # reset zap latch on confirmed close
            # Clear manual mode so close event restores work mode
            timer_engine._clear_manual_mode()
            print("    Twitter closed, manual mode cleared")

        # Phone close is observational; server-side work/activity derivation owns timer state.
        timer_updated = False
        acknowledged_acks = await acknowledge_phone_acks(app_name)
        stop_enforcement_cascade(reason=f"negative_edge_close:{app_name}")
        if close_matches_current:
            timer_updated = False
            print(f"    Phone close -> working | timer={timer_updated}")
        elif old_app:
            print(f"    Ignoring close for {app_name}; current_app remains {old_app}")

        await log_event(
            "phone_app_closed",
            details={
                "app": app_name,
                "old_app": old_app,
                "matched_current": close_matches_current,
                "display_name": display_name,
                "package": package,
                "duration_seconds": duration_seconds,
                "timer_mode": timer_engine.current_mode.value,
                "timer_updated": timer_updated,
                "acknowledged_expected_acks": acknowledged_acks,
            },
        )
        await log_event(
            "enforcement_negative_edge",
            details={
                "surface": "phone",
                "app": app_name,
                "display_name": display_name,
                "acknowledged_expected_acks": acknowledged_acks,
                "cascade_stopped": True,
                "reason": "app_closed",
            },
        )

        return PhoneActivityResponse(allowed=True, reason="closed", message="App closed")

    # Determine distraction category
    distraction_mode = PHONE_DISTRACTION_APPS.get(app_name)
    if not distraction_mode and package:
        distraction_mode = PHONE_DISTRACTION_APPS.get(package)

    # If not a known distraction app, allow it
    if not distraction_mode:
        print(f"    Unknown app, allowing: {app_name}")
        return PhoneActivityResponse(
            allowed=True, reason="not_tracked", message="App not in distraction list"
        )

    await bust_quiet_state(
        "phone_detection",
        "phone_distraction_open",
        {
            "app": app_name,
            "display_name": display_name,
            "package": package,
            "distraction_mode": distraction_mode,
        },
    )

    is_twitter = app_name in ("twitter", "x", "com.twitter.android")

    # Phantom open guard: if Twitter was already zapped and we haven't received
    # a confirmed close event, ignore all subsequent "open" events entirely.
    # MacroDroid's app_launched trigger re-fires on notification banners, app
    # switcher, etc. — these phantom opens were resetting current_app and
    # restarting the 7-minute timer, causing repeat zaps.
    if is_twitter and PHONE_STATE.get("twitter_zapped"):
        print("    Phantom Twitter open ignored (already zapped, awaiting confirmed close)")
        return PhoneActivityResponse(
            allowed=False,
            reason="phantom_blocked",
            message="Twitter already enforced, waiting for confirmed close",
        )

    # Duplicate open debounce: if we're already tracking this app, don't
    # re-process. MacroDroid sends repeated app_launched events for the same
    # app (notification banners, app switcher swipe-throughs, etc.).
    current = (PHONE_STATE.get("current_app") or "").lower()
    if current == app_name or (is_twitter and current in ("twitter", "x", "com.twitter.android")):
        print(f"    Duplicate {app_name} open ignored (already current_app)")
        ack = None
        if is_quiet_hours():
            await log_quiet_hours_suppressed(
                source="phone_detection",
                event_type="phone_duplicate_open",
                app=app_name,
                details={"display_name": display_name, "package": package},
            )
        else:
            ack = await maybe_create_phone_distraction_ack(
                app_name=app_name,
                display_name=display_name,
                package=package,
                distraction_mode=distraction_mode,
                trigger="duplicate_open",
            )
        return PhoneActivityResponse(
            allowed=True,
            reason="ack_required" if ack else "already_tracked",
            message="Distraction acknowledgement required" if ack else "Already tracking this app",
        )

    async def _observe_phone_distraction(productivity_active: bool | None = None) -> bool:
        """Feed phone foreground into the composite timer state without direct mode assertions."""
        # If twitter was already zapped, don't let phantom opens change timer mode
        # (this prevents phantom opens from burning break time → break_exhausted zaps)
        if app_name in ("twitter", "x", "com.twitter.android") and PHONE_STATE.get(
            "twitter_zapped"
        ):
            print("    Skipping phone observation — twitter already zapped")
            return False
        PHONE_STATE["distraction_observed_count"] = (
            int(PHONE_STATE.get("distraction_observed_count") or 0) + 1
        )
        now_ms = int(time.monotonic() * 1000)
        old_timer_mode = timer_engine.current_mode.value
        old_activity = timer_engine.activity.value
        if productivity_active is not None:
            timer_engine.set_productivity(productivity_active, now_ms)
        was_focused = timer_engine.focus_active
        is_sg = distraction_mode in ("scrolling", "gaming")
        result = timer_engine.set_activity(
            Activity.DISTRACTION, is_scrolling_gaming=is_sg, now_mono_ms=now_ms
        )
        timer_updated = TimerEvent.MODE_CHANGED in result.events
        shift_to_log = (old_timer_mode, timer_engine.current_mode.value) if timer_updated else None
        if was_focused and not timer_engine.focus_active:
            await log_event(
                "focus_toggle",
                details={
                    "action": "off",
                    "trigger": "phone_distraction",
                    "app": app_name,
                    "total_focus_time_ms": timer_engine.total_focus_time_ms,
                    "focus_cutoff_time": timer_engine.focus_cutoff_time,
                },
            )
        await log_event(
            "phone_distraction_observed",
            device_id="phone",
            details={
                "app": app_name,
                "display_name": display_name,
                "package": package,
                "distraction_mode": distraction_mode,
                "old_timer_mode": old_timer_mode,
                "timer_mode": timer_engine.current_mode.value,
                "old_activity": old_activity,
                "activity": timer_engine.activity.value,
                "productivity_active": timer_engine.productivity_active,
                "timer_updated": timer_updated,
                "count": PHONE_STATE["distraction_observed_count"],
            },
        )
        print(f"    Phone open observed -> {distraction_mode} | timer={timer_updated}")
        # Track Twitter open time for 7-minute enforcement
        if app_name in ("twitter", "x", "com.twitter.android"):
            if PHONE_STATE["twitter_open_since"] is None and not PHONE_STATE.get("twitter_zapped"):
                PHONE_STATE["twitter_open_since"] = time.monotonic()
                print("    Twitter timer started")
            elif PHONE_STATE.get("twitter_zapped"):
                print("    Twitter open (ignoring — already zapped, waiting for confirmed close)")
        else:
            # Different app opened — if twitter timer is running, close event was dropped
            if PHONE_STATE["twitter_open_since"] is not None or PHONE_STATE.get("twitter_zapped"):
                print(f"    Clearing stale Twitter timer (new app: {app_name})")
                PHONE_STATE["twitter_open_since"] = None
                PHONE_STATE["twitter_zapped"] = False
        if shift_to_log:
            asyncio.create_task(
                timer_log_shift(
                    shift_to_log[0],
                    shift_to_log[1],
                    trigger="phone_distraction",
                    source="macrodroid",
                    phone_app=app_name,
                )
            )
        return timer_updated

    # Check work mode
    work_mode = DESKTOP_STATE.get("work_mode", "clocked_in")
    was_break_mode = timer_engine.current_mode == TimerMode.BREAK

    async def _create_phone_gaming_ack(timer_updated: bool) -> dict | None:
        if distraction_mode != "gaming" or work_mode != "clocked_in":
            return None
        if was_break_mode:
            return None
        if is_quiet_hours():
            await log_quiet_hours_suppressed(
                source="phone_detection",
                event_type="phone_gaming_ack_creation",
                app=app_name,
                details={
                    "display_name": display_name,
                    "package": package,
                    "timer_mode": timer_engine.current_mode.value,
                    "timer_updated": timer_updated,
                },
            )
            return None
        ack = await create_expected_ack(
            source="phone_gaming",
            instance_id=_phone_ack_instance_id("phone_gaming", app_name),
            reason=f"Phone gaming during work: {display_name}",
            details={
                "app": app_name,
                "display_name": display_name,
                "package": package,
                "timer_mode": timer_engine.current_mode.value,
                "timer_updated": timer_updated,
            },
        )
        return ack

    # Clocked out or gym mode = all allowed
    if work_mode in ("clocked_out", "gym"):
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["app_opened_at"] = datetime.now().isoformat()
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        _updated = await _observe_phone_distraction()

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": work_mode,
            },
        )

        return PhoneActivityResponse(
            allowed=True, reason=work_mode, message=f"Allowed ({work_mode})"
        )

    if is_quiet_hours():
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["app_opened_at"] = datetime.now().isoformat()
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        _updated = await _observe_phone_distraction()
        await log_quiet_hours_suppressed(
            source="phone_detection",
            event_type="phone_distraction_open",
            app=app_name,
            details={
                "display_name": display_name,
                "package": package,
                "distraction_mode": distraction_mode,
                "timer_updated": _updated,
            },
        )
        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": "quiet_hours",
                "timer_mode": timer_engine.current_mode.value,
                "created_ack": False,
                "cascade_started": False,
            },
        )
        return PhoneActivityResponse(
            allowed=True,
            reason="quiet_hours",
            break_seconds=round(timer_engine.break_balance_ms / 1000),
            message="Quiet hours",
        )

    # Clocked in - check break time and productivity
    break_secs = round(timer_engine.break_balance_ms / 1000)

    work_state = await compute_work_state()
    productivity_active = work_state.productivity_active
    active_count = work_state.active_instance_count + work_state.observed_agent_count

    # === TEST SHIM - bypasses break/productivity checks ===
    test_force_block = PHONE_CONFIG.get("test_force_block", False)
    if test_force_block:
        print(
            f"    TEST MODE: Forcing block (ignoring break={break_secs}s, productivity={productivity_active})"
        )
        break_secs = 0
        productivity_active = False
    # ======================================================

    # Decision logic (same as desktop)
    if break_secs < 0:
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["app_opened_at"] = datetime.now().isoformat()
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        _updated = await _observe_phone_distraction(productivity_active=productivity_active)
        ack = await maybe_create_backlog_violation_ack(
            surface="phone",
            app_name=app_name,
            display_name=display_name,
            package=package,
            distraction_mode=distraction_mode,
            trigger="phone_open_backlog",
        )
        if is_quiet_hours():
            await log_quiet_hours_suppressed(
                source="phone_detection",
                event_type="phone_enforcement_cascade_start",
                app=app_name,
                details={"reason": "backlog_violation"},
            )
        else:
            start_enforcement_cascade(app_name)
        await log_event(
            "phone_backlog_violation",
            details={
                "app": app_name,
                "display_name": display_name,
                "package": package,
                "break_seconds": break_secs,
                "timer_mode": timer_engine.current_mode.value,
                "ack_id": ack["id"] if ack else None,
            },
        )
        return PhoneActivityResponse(
            allowed=False,
            reason="backlog_violation",
            break_seconds=break_secs,
            message="Backlog violation: close the app or work now",
        )

    if break_secs > 0:
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["app_opened_at"] = datetime.now().isoformat()
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        _updated = await _observe_phone_distraction(productivity_active=productivity_active)

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": "break_time",
                "break_seconds": break_secs,
                "timer_mode": timer_engine.current_mode.value,
                "created_ack": False,
            },
        )

        print(f"    Allowed: {break_secs}s break available")
        return PhoneActivityResponse(
            allowed=True,
            reason="break_time_available",
            break_seconds=break_secs,
            message=f"Break time: {break_secs // 60}m {break_secs % 60}s",
        )

    elif productivity_active:
        PHONE_STATE["current_app"] = app_name
        PHONE_STATE["app_opened_at"] = datetime.now().isoformat()
        PHONE_STATE["is_distracted"] = True
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        _updated = await _observe_phone_distraction(productivity_active=productivity_active)

        ack = await _create_phone_gaming_ack(_updated)

        await log_event(
            "phone_distraction_allowed",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": "productivity_active",
                "active_instances": active_count,
                "timer_mode": timer_engine.current_mode.value,
                "created_ack": bool(ack),
                "ack_id": ack["id"] if ack else None,
            },
        )

        print(f"    Allowed: productivity active ({active_count} instances)")
        return PhoneActivityResponse(
            allowed=True,
            reason="productivity_active",
            break_seconds=0,
            message="Productivity active (penalty mode)",
        )

    else:
        if distraction_mode == "gaming" and work_mode == "clocked_in":
            PHONE_STATE["current_app"] = app_name
            PHONE_STATE["app_opened_at"] = datetime.now().isoformat()
            PHONE_STATE["is_distracted"] = True
            PHONE_STATE["last_activity"] = datetime.now().isoformat()
            _updated = await _observe_phone_distraction(productivity_active=productivity_active)
            ack = await _create_phone_gaming_ack(_updated)
            if not ack:
                ack = await maybe_create_phone_distraction_ack(
                    app_name=app_name,
                    display_name=display_name,
                    package=package,
                    distraction_mode=distraction_mode,
                    trigger="no_break_no_productivity",
                    timer_updated=_updated,
                    min_open_seconds=0,
                    productivity_active=False,
                )
            await log_event(
                "phone_gaming_ack_required",
                details={
                    "app": app_name,
                    "display_name": display_name,
                    "package": package,
                    "timer_mode": timer_engine.current_mode.value,
                    "ack_id": ack["id"] if ack else None,
                },
            )
            return PhoneActivityResponse(
                allowed=True,
                reason="ack_required",
                break_seconds=0,
                message="Gaming during work requires acknowledgement",
            )

        print("    BLOCKED: no break time, no productivity")
        # v2: Start enforcement cascade instead of Shizuku disable
        if is_quiet_hours():
            await log_quiet_hours_suppressed(
                source="phone_detection",
                event_type="phone_enforcement_cascade_start",
                app=app_name,
                details={"reason": "no_break_no_productivity"},
            )
        else:
            start_enforcement_cascade(app_name)

        await log_event(
            "phone_distraction_blocked",
            details={
                "app": app_name,
                "display_name": display_name,
                "reason": "no_break_no_productivity",
                "enforcement": "cascade_started",
            },
        )
        asyncio.create_task(
            handle_custodes_state_event(
                "phone_distraction_blocked",
                "phone",
                severity=2,
                payload={
                    "app": app_name,
                    "phone_app": app_name,
                    "display_name": display_name,
                    "reason": "no_break_no_productivity",
                },
            )
        )

        return PhoneActivityResponse(
            allowed=False,
            reason="blocked",
            break_seconds=0,
            message="No break time or productivity",
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
    - app_open: v2 telemetry — app opened (routes through distraction detection)
    - app_close: v2 telemetry — app closed (stops enforcement cascade)
    - discord_fallback_received: phone confirmed it received Discord relay
    - shizuku_died: Shizuku service stopped (legacy, kept for transition)
    - shizuku_restored: Shizuku came back (legacy)
    - device_boot: Phone rebooted
    - heartbeat: Periodic health check
    """
    now = datetime.now().isoformat()

    # Parse trigger name to determine event type and routing
    # Minimal format: {"app": "Application Launched (X)"} or {"app": "Geofence Entry (Home)"}
    raw_trigger = request.app or ""
    parsed = (
        parse_macrodroid_trigger(raw_trigger) if raw_trigger else {"type": "unknown", "raw": ""}
    )
    event = request.event

    # Infer event from trigger name if not provided
    if not event:
        if parsed["type"] == "app":
            event = "app_open" if parsed["action"] == "open" else "app_close"
        elif parsed["type"] == "geofence":
            event = "geofence"
        elif raw_trigger:
            event = "app_telemetry"

    if not event:
        return {"received": True, "error": "no event or app field"}

    # Log non-app events here; app open/close are logged downstream by handle_phone_activity
    # with richer details (phone_app_closed, phone_distraction_allowed, phone_distraction_blocked)
    if event not in ("app_open", "app_close", "app_telemetry"):
        await log_event(
            f"phone_{event}",
            device_id="phone",
            details={
                "time": request.time,
                "app": request.app,
                "server": request.server,
                "shizuku_dead": request.shizuku_dead,
            },
        )

    # ---- App telemetry (open/close) ----
    if event in ("app_open", "app_close", "app_telemetry") and parsed["type"] == "app":
        app_key = parsed["app"]
        action = parsed["action"] or ("close" if event == "app_close" else "open")

        print(f">>> /phone/event {event}: raw={raw_trigger!r} -> app={app_key!r} action={action!r}")

        if action == "close":
            stop_enforcement_cascade(reason="app_close")

        phone_req = PhoneActivityRequest(app=app_key, action=action, package=app_key)
        result = await handle_phone_activity(phone_req)
        return {
            "received": True,
            "event": event,
            "app": app_key,
            "action": action,
            "raw": raw_trigger,
            "decision": result.dict(),
        }

    # ---- Geofence (entry/exit) ----
    elif event == "geofence" or parsed["type"] == "geofence":
        location = parsed["location"]
        action = parsed["action"]

        print(
            f">>> /phone/event geofence: raw={raw_trigger!r} -> location={location!r} action={action!r}"
        )

        loc_req = LocationEventRequest(location=location, action=action, source="macrodroid_v2")
        result = await handle_location_event(loc_req)
        return {
            "received": True,
            "event": "geofence",
            "location": location,
            "action": action,
            "raw": raw_trigger,
            "result": result,
        }

    # ---- v2 Discord fallback acknowledgement ----
    elif event == "discord_fallback_received":
        logger.info("Discord fallback received by phone")
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = now
        return {"received": True, "event": event}

    # ---- Legacy: Shizuku events (kept for transition) ----
    elif event == "shizuku_died":
        SHIZUKU_STATE["dead"] = True
        SHIZUKU_STATE["last_death"] = now
        logger.warning(f"Shizuku died at {request.time}")
        restart_result = await attempt_shizuku_restart()
        return {"received": True, "event": event, "restart_attempt": restart_result}

    elif event == "shizuku_restored":
        SHIZUKU_STATE["dead"] = False
        SHIZUKU_STATE["consecutive_failures"] = 0
        logger.info(f"Shizuku restored at {request.time}")
        return {"received": True, "event": event}

    elif event == "device_boot":
        SHIZUKU_STATE["dead"] = True
        logger.info(f"Phone booted at {request.time}")
        return {"received": True, "event": event}

    elif event == "heartbeat":
        PHONE_STATE["reachable"] = True
        PHONE_STATE["last_reachable_check"] = now
        PHONE_HEARTBEAT["last_seen"] = datetime.utcnow()
        PHONE_HEARTBEAT["device_id"] = "Token-S24"
        PHONE_HEARTBEAT["alert_state"] = None
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
    await handle_custodes_state_event(
        "break_exhausted",
        "api",
        severity=2,
        payload={"break_balance_ms": timer_engine.break_balance_ms, "result": result},
    )

    if not result.get("enforced"):
        return {"enforced": False, "reason": "no_active_distractions"}

    return result


@app.post("/api/timer/set-break")
async def set_break_time(seconds: int):
    """Debug: directly set accumulated break time (in seconds). Negative values set backlog."""
    timer_engine._break_balance_ms = seconds * 1000
    await log_event(
        "timer_debug_set_break",
        details={
            "seconds": seconds,
            "test_override": True,
            "break_balance_ms": timer_engine._break_balance_ms,
            "backlog_ms": abs(min(0, timer_engine._break_balance_ms)),
        },
    )
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
    work_state = await get_cached_work_state()
    return {
        "current_mode": timer_engine.current_mode.value,
        "activity": timer_engine.activity.value,
        "productivity_active": work_state.productivity_active,
        "work_state": work_state.model_dump(),
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
        "steam_app_id": DESKTOP_STATE.get("steam_app_id"),
        "steam_app_name": DESKTOP_STATE.get("steam_app_name"),
        "steam_exe": DESKTOP_STATE.get("steam_exe"),
        "phone_app": PHONE_STATE.get("current_app"),
        "ahk_reachable": DESKTOP_STATE.get("ahk_reachable"),
    }


@app.get("/api/work-state", response_model=WorkStateResponse)
async def get_work_state():
    """Typed read model for what Token-API thinks the operator is doing."""
    return await get_cached_work_state()


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
        return {
            "total_shifts": 0,
            "balance_series": [],
            "mode_distribution": {},
            "shifts_by_trigger": {},
            "enforcement_count": 0,
            "twitter_time_mins": 0,
        }

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
        balance_timeline.append(
            {
                "t": r["timestamp"],
                "bal": bal_min,
                "mode": r["new_mode"],
            }
        )
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
        await timer_end_session(
            _current_session_id,
            now_ms - _session_start_ms,
            break_used_ms=timer_engine.total_break_time_ms,
        )
        _current_session_id = await timer_start_session("break", today)
        _session_start_ms = now_ms
        await log_event("timer_mode_change", details={"new_mode": "break", "source": "api"})
    return {
        "status": "break",
        "changed": changed,
        "break_available_seconds": round(max(0, timer_engine.break_balance_ms) / 1000),
    }


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
    """Enter sleeping quiet mode - neutral, no enforcement or timer shifts."""
    return await enter_quiet_mode_internal(context="sleeping", source="api")


@app.post("/api/timer/go-to-sleep")
async def go_to_sleep_mode():
    """Debrief hook alias for entering sleeping quiet mode."""
    return await enter_quiet_mode_internal(context="sleeping", source="debrief")


@app.post("/api/timer/resume")
async def resume_work_mode():
    """Exit break/sleeping and resume. Also sets productivity active."""
    global _current_session_id, _session_start_ms
    now_ms = int(time.monotonic() * 1000)
    old_mode = timer_engine.current_mode.value
    changed, tick_result = timer_engine.resume(now_ms)
    try:
        scheduler.remove_job(QUIET_RESUME_JOB_ID)
    except Exception:
        pass
    # Also ensure productivity is active
    timer_engine.set_productivity(True, now_ms)
    if changed:
        DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
        today = datetime.now().strftime("%Y-%m-%d")
        new_mode = timer_engine.current_mode.value
        await timer_log_mode_change(old_mode, new_mode, is_automatic=False)
        await timer_log_shift(old_mode, new_mode, trigger="manual", source="api")
        if timer_engine.current_mode == TimerMode.WORKING:
            _mark_mewgenics_work_action("timer_mode_working", "manual_resume")
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
        await log_event(
            "focus_toggle",
            details={
                "action": action,
                "total_focus_time_ms": timer_engine.total_focus_time_ms,
                "focus_cutoff_time": timer_engine.focus_cutoff_time,
            },
        )
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
    return {
        "status": "reset",
        "total_work_time": "0h 0m",
        "accumulated_break": "5m",
        "current_mode": timer_engine.current_mode.value,
    }


@app.post("/api/work-action")
async def work_action(request: WorkActionRequest | None = None):
    """Manual work action signal — sets productivity active and resolves distraction acks."""
    global _current_session_id, _session_start_ms
    request = request or WorkActionRequest()
    await bust_quiet_state(
        "api",
        "work_action",
        {"source": request.source, "note": request.note},
    )
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

    acknowledged_acks = await acknowledge_pending_work_action_acks()
    _mark_mewgenics_work_action(request.source, request.note)
    stop_enforcement_cascade(reason=f"work_action:{request.source}")
    if PHONE_STATE.get("is_distracted"):
        PHONE_STATE["current_app"] = None
        PHONE_STATE["app_opened_at"] = None
        PHONE_STATE["is_distracted"] = False
        PHONE_STATE["distraction_ack_app"] = None
        PHONE_STATE["distraction_ack_id"] = None
        PHONE_STATE["last_activity"] = datetime.now().isoformat()
        DESKTOP_STATE["last_detection"] = datetime.now().isoformat()
        _sync_activity_from_remaining_distraction_signals(now_ms)

    await log_event(
        "work_action",
        details={
            "source": request.source,
            "note": request.note,
            "old_mode": old_mode,
            "current_mode": timer_engine.current_mode.value,
            "acknowledged_expected_acks": acknowledged_acks,
        },
    )

    return {
        "idle_timer_reset": True,
        "exited_idle": exited_idle,
        "current_mode": timer_engine.current_mode.value,
        "acknowledged_expected_acks": acknowledged_acks,
    }


async def hook_work_action_callback(source: str, note: str | None = None):
    return await work_action(WorkActionRequest(source=source, note=note))


# ============ Work Mode / Geofence Endpoints ============
# MacroDroid uses geofence to send work mode changes


class WorkModeRequest(BaseModel):
    mode: str = Field(..., description="Work mode: clocked_in, clocked_out, gym")
    source: str = Field(
        default="api", description="Source of the request (macrodroid, manual, etc)"
    )
    token: str | None = Field(default=None, description="Optional auth token for MacroDroid")


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
            status_code=400, detail=f"Invalid work mode '{request.mode}'. Valid: {valid_modes}"
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
        },
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
            await log_event(
                "location_event",
                details={
                    "location": location,
                    "action": action,
                    "status": "duplicate",
                    "current_zone": current_zone,
                    "source": request.source,
                },
            )
            return {"status": "duplicate", "reason": f"Already in {location}", "zone": current_zone}

        if current_zone is not None and current_zone != location:
            # Geofence exit didn't fire — log the implied exit
            notes.append(f"implied_exit:{current_zone}")
            print(f">>> Implied exit from {current_zone} (no exit event received)")
            await log_event(
                "location_event",
                details={
                    "location": current_zone,
                    "action": "exit",
                    "implied": True,
                    "reason": f"entered {location} without exiting {current_zone}",
                    "source": "state_machine",
                },
            )

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
        work_mode_req = WorkModeRequest(mode=new_mode, source=f"macrodroid:{location}:{action}")
        result = await set_work_mode(work_mode_req)
    else:
        print(
            f">>> Location tracked: {location}:{action} (work_mode unchanged - manual control only)"
        )

    # Gym bounty: +30 min break on gym exit
    if location == "gym" and action == "exit":
        now_ms = int(time.monotonic() * 1000)
        timer_engine.apply_gym_bounty(now_ms)
        bounty_min = round(timer_engine.break_balance_ms / 60000, 1)
        print(f">>> Gym bounty applied: +30min break (total: {bounty_min}min)")
        await log_event("gym_bounty", details={"break_minutes": bounty_min})

    await log_event(
        "location_event",
        details={
            "location": location,
            "action": action,
            "mapped_mode": new_mode,
            "prev_zone": current_zone,
            "notes": notes or None,
            "source": request.source,
        },
    )

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
            (request.type, today),
        )
        existing = await cursor.fetchone()

        if existing:
            await db.execute(
                """
                UPDATE checkins SET
                    energy = ?, focus = ?, mood = ?, plan = ?, notes = ?,
                    on_track = ?, responded_at = ?
                WHERE checkin_type = ? AND date = ?
            """,
                (
                    request.energy,
                    request.focus,
                    request.mood,
                    request.plan,
                    request.notes,
                    1 if request.on_track else (0 if request.on_track is not None else None),
                    now,
                    request.type,
                    today,
                ),
            )
        else:
            # Submit without a prior prompt (manual submission)
            await db.execute(
                """
                INSERT INTO checkins (checkin_type, date, energy, focus, mood, plan, notes, on_track, prompted_at, responded_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'api')
            """,
                (
                    request.type,
                    today,
                    request.energy,
                    request.focus,
                    request.mood,
                    request.plan,
                    request.notes,
                    1 if request.on_track else (0 if request.on_track is not None else None),
                    now,
                    now,
                ),
            )

        await db.commit()

    # Write to daily note frontmatter
    data = {k: v for k, v in request.model_dump().items() if k != "type" and v is not None}
    obsidian_updated = update_daily_note_frontmatter(request.type, data)

    await log_event(
        "checkin_submitted",
        details={
            "checkin_type": request.type,
            "energy": request.energy,
            "focus": request.focus,
            "obsidian_updated": obsidian_updated,
        },
    )

    return {"status": "ok", "checkin_type": request.type, "obsidian_updated": obsidian_updated}


@app.get("/api/checkin/today")
async def get_today_checkins():
    """Return all check-ins for today with completion status."""
    today = datetime.now().strftime("%Y-%m-%d")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM checkins WHERE date = ? ORDER BY prompted_at", (today,)
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
            "SELECT checkin_type, responded_at FROM checkins WHERE date = ?", (today,)
        )
        rows = await cursor.fetchall()

    completed = [r["checkin_type"] for r in rows if r["responded_at"]]
    prompted = [r["checkin_type"] for r in rows]

    # Determine next check-in based on current time
    schedule_order = [
        "morning_start",
        "mid_morning",
        "decision_point",
        "afternoon",
        "afternoon_check",
    ]
    time_map = {
        "morning_start": "09:00",
        "mid_morning": "10:30",
        "decision_point": "11:00",
        "afternoon": "13:00",
        "afternoon_check": "14:30",
    }

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
    await log_event(request.event_type, instance_id=request.instance_id, details=request.details)
    return {"status": "logged", "event_type": request.event_type}


@app.get("/api/events/recent")
async def get_recent_events(limit: int = 10):
    """Get recent events with instance name data (LEFT JOIN)."""
    limit = min(limit, 100)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT e.*, ci.tab_name as instance_tab_name, ci.working_dir as instance_working_dir
            FROM events e
            LEFT JOIN claude_instances ci ON e.instance_id = ci.id
            ORDER BY e.created_at DESC
            LIMIT ?
        """,
            (limit,),
        )
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
            message="Phone audio proxy already active",
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
            },
        )

        action = "connected" if result.get("status") == "started" else "reconnected"
        return AudioProxyConnectResponse(
            success=True,
            action=action,
            receiver_started=True,
            receiver_pid=result.get("pid"),
            message="Audio proxy activated.",
        )
    else:
        # Failed to start receiver
        await log_event(
            "audio_proxy_connect_failed",
            device_id=request.phone_device_id,
            details={"error": result.get("error"), "source": request.source},
        )

        return AudioProxyConnectResponse(
            success=False,
            action="error",
            receiver_started=False,
            message=f"Failed to start audio receiver: {result.get('error')}",
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
        details={"stopped_count": result.get("stopped_count", 0), "source": request.source},
    )

    return {
        "success": True,
        "action": "disconnected",
        "stopped_count": result.get("stopped_count", 0),
        "message": "Audio proxy deactivated. Phone can reconnect to headphones.",
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
        last_disconnect_time=AUDIO_PROXY_STATE["last_disconnect_time"],
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
        message="Headless mode not available on macOS",
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
        result = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=10
        )

        if result.returncode == 0:
            logger.info(f"SYSTEM: Initiated {action} with delay={delay_minutes}min")
            return ShutdownResponse(
                success=True,
                action=action,
                delay_seconds=request.delay_seconds,
                message=f"System {action} initiated"
                + (f" in {delay_minutes} minutes" if delay_minutes > 0 else ""),
            )
        else:
            error_msg = result.stderr.strip() or result.stdout.strip()
            logger.error(f"SYSTEM: Failed to {action}: {error_msg}")
            return ShutdownResponse(
                success=False,
                action=action,
                delay_seconds=request.delay_seconds,
                message=f"Failed: {error_msg}",
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
        result = await asyncio.to_thread(
            subprocess.run,
            ["sudo", "killall", "shutdown"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            logger.info("SYSTEM: Cancelled pending shutdown")
            return {"success": True, "message": "Shutdown cancelled"}
        else:
            return {
                "success": False,
                "message": f"No pending shutdown or cancel failed: {result.stderr.strip()}",
            }
    except Exception as e:
        return {"success": False, "message": str(e)}


# ============ KVM (Deskflow) ============


def _mac_kvm_set_state(**updates):
    MAC_KVM_STATE.update(updates)
    MAC_KVM_STATE["last_changed"] = datetime.now().isoformat()


def _deskflow_client_remote_host() -> str:
    try:
        config_text = DESKFLOW_CLIENT_CONFIG_PATH.read_text()
    except OSError as e:
        logger.warning(
            f"KVM: Could not read Deskflow client config at {DESKFLOW_CLIENT_CONFIG_PATH}: {e}"
        )
        return DESKTOP_CONFIG["host"]

    in_client_section = False
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_client_section = line.lower() == "[client]"
            continue
        if in_client_section and line.startswith("remoteHost="):
            host = line.split("=", 1)[1].strip()
            if host:
                return host

    logger.warning(
        f"KVM: Deskflow client config has no [client] remoteHost; falling back to {DESKTOP_CONFIG['host']}"
    )
    return DESKTOP_CONFIG["host"]


def _wsl_deskflow_server_reachable() -> tuple[bool, str]:
    host = _deskflow_client_remote_host()
    try:
        with socket.create_connection((host, DESKFLOW_SERVER_PORT), timeout=2):
            return True, host
    except OSError:
        return False, host


def _mac_deskflow_pids() -> list[str]:
    pids: list[str] = []
    for name in ("Deskflow", "deskflow-core"):
        result = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            pids.extend(line for line in result.stdout.strip().splitlines() if line)
    return pids


def _mac_deskflow_running() -> bool:
    return bool(_mac_deskflow_pids())


def _ensure_mac_deskflow_keymap(reason: str):
    """Pin the macOS input-source state Deskflow needs for US-ANSI symbols.

    After macOS restart, Deskflow can reconnect with a JIS-like symbol map
    (`'` arriving as `:`). This guard is surgical: it does not wipe Deskflow
    settings; it reselects the known-good US-compatible input source and
    disables client language-sync before client start/reload so the WSL server cannot overwrite the Mac input source.
    """
    if not DESKFLOW_KEYMAP_GUARD.exists():
        logger.warning(f"KVM: Deskflow keymap guard missing: {DESKFLOW_KEYMAP_GUARD}")
        return
    result = subprocess.run(
        [str(DESKFLOW_KEYMAP_GUARD)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode == 0:
        logger.info(f"KVM: Deskflow keymap guard ok ({reason}): {result.stdout.strip()}")
    else:
        logger.warning(
            f"KVM: Deskflow keymap guard failed ({reason}, exit={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _start_mac_deskflow_client(reason: str):
    _ensure_mac_deskflow_keymap(f"{reason}_pre_start")
    subprocess.Popen(["open", "/Applications/Deskflow.app"])
    subprocess.Popen(["caffeinate", "-u", "-t", "5"])
    _ensure_mac_deskflow_keymap(f"{reason}_post_start")
    logger.info(f"KVM: Started Deskflow client ({reason})")


def _reload_mac_deskflow_client(reason: str):
    _ensure_mac_deskflow_keymap(f"{reason}_pre_reload")
    subprocess.run(["open", "-a", "Deskflow"], capture_output=True, text=True, timeout=5)
    subprocess.Popen(["caffeinate", "-u", "-t", "5"])
    _ensure_mac_deskflow_keymap(f"{reason}_post_reload")
    logger.info(f"KVM: Reloaded Deskflow client ({reason})")


def _stop_mac_deskflow_client(reason: str):
    result = subprocess.run(
        ["killall", "-9", "Deskflow", "deskflow-core"],
        capture_output=True,
        text=True,
    )
    logger.info(f"KVM: Stopped Deskflow client ({reason}, exit={result.returncode})")


async def mac_kvm_supervisor():
    """Mac-side guard that prevents Deskflow's native client from retry-spamming.

    If the WSL Deskflow server port is absent, the Mac client is stopped so
    deskflow-core cannot loop internally. Token-API then probes with exponential
    backoff and starts the client only after the server port is reachable.
    """
    await asyncio.sleep(5)
    while True:
        try:
            server_reachable, server_host = await asyncio.to_thread(_wsl_deskflow_server_reachable)
            client_running = await asyncio.to_thread(_mac_deskflow_running)

            if server_reachable:
                if not client_running:
                    await asyncio.to_thread(_start_mac_deskflow_client, "server_reachable")
                    client_running = True
                    action = "client_started"
                else:
                    action = "server_reachable"
                _mac_kvm_set_state(
                    state="running",
                    server_host=server_host,
                    server_reachable=True,
                    client_running=client_running,
                    retry_attempts=0,
                    next_probe_at=0.0,
                    last_action=action,
                )
                await asyncio.sleep(30)
                continue

            stopped_client = False
            if client_running:
                await asyncio.to_thread(_stop_mac_deskflow_client, "server_unreachable")
                client_running = False
                stopped_client = True

            attempts = int(MAC_KVM_STATE.get("retry_attempts") or 0) + 1
            delay = MAC_KVM_BACKOFF_SECONDS[min(attempts - 1, len(MAC_KVM_BACKOFF_SECONDS) - 1)]
            next_probe_at = time.time() + delay
            _mac_kvm_set_state(
                state="backoff",
                server_host=server_host,
                server_reachable=False,
                client_running=False,
                retry_attempts=attempts,
                next_probe_at=next_probe_at,
                last_action="client_stopped_backoff" if stopped_client else "server_absent_backoff",
            )
            logger.info(f"KVM: WSL Deskflow server absent; next Mac probe in {delay}s")
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"KVM supervisor error: {e}")
            _mac_kvm_set_state(state="error", last_action=str(e)[:200])
            await asyncio.sleep(60)


@app.post("/api/kvm/start")
async def kvm_start():
    """Start Deskflow client (software KVM) on this Mac."""
    try:
        if _mac_deskflow_running():
            return {"success": True, "message": "Deskflow already running", "already_running": True}

        _start_mac_deskflow_client("api_start")
        return {"success": True, "message": "Deskflow started", "already_running": False}
    except Exception as e:
        logger.error(f"KVM: Failed to start Deskflow: {e}")
        return {"success": False, "message": str(e)}


@app.post("/api/kvm/reload")
async def kvm_reload():
    """Lightly nudge Deskflow client on this Mac without force-killing the app."""
    try:
        _reload_mac_deskflow_client("api_reload")
        return {"success": True, "message": "Deskflow reload nudged"}
    except Exception as e:
        logger.error(f"KVM: Failed to reload Deskflow: {e}")
        return {"success": False, "message": str(e)}


@app.post("/api/kvm/stop")
async def kvm_stop():
    """Stop Deskflow client on this Mac."""
    try:
        was_running = _mac_deskflow_running()
        _stop_mac_deskflow_client("api_stop")
        if was_running:
            return {"success": True, "message": "Deskflow stopped"}
        else:
            return {"success": True, "message": "Deskflow was not running"}
    except Exception as e:
        logger.error(f"KVM: Failed to stop Deskflow: {e}")
        return {"success": False, "message": str(e)}


@app.get("/api/kvm/status")
async def kvm_status():
    """Check if Deskflow is running on this Mac."""
    pids = _mac_deskflow_pids()
    running = bool(pids)
    return {
        "running": running,
        "pids": pids,
        "supervisor": {
            **MAC_KVM_STATE,
            "next_probe_at": (
                datetime.fromtimestamp(MAC_KVM_STATE["next_probe_at"]).isoformat()
                if MAC_KVM_STATE.get("next_probe_at")
                else None
            ),
        },
    }


# ============ Task Endpoints ============


@app.get("/api/tasks", response_model=list[TaskResponse])
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
                (task_id,),
            )
            last_exec = await cursor.fetchone()

            last_run = None
            if last_exec:
                last_exec_dict = dict(last_exec)
                last_run = {
                    "status": last_exec_dict["status"],
                    "started_at": last_exec_dict["started_at"],
                    "duration_ms": last_exec_dict["duration_ms"],
                }

            # Get next run time from scheduler
            next_run = None
            job = scheduler.get_job(task_id)
            if job and job.next_run_time:
                next_run = job.next_run_time.isoformat()

            result.append(
                TaskResponse(
                    id=task_dict["id"],
                    name=task_dict["name"],
                    description=task_dict["description"],
                    task_type=task_dict["task_type"],
                    schedule=task_dict["schedule"],
                    enabled=bool(task_dict["enabled"]),
                    max_retries=task_dict["max_retries"],
                    last_run=last_run,
                    next_run=next_run,
                )
            )

        return result


@app.get("/api/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get details of a specific task."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        task = await cursor.fetchone()

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        task_dict = dict(task)

        # Get last execution
        cursor = await db.execute(
            """SELECT * FROM task_executions
               WHERE task_id = ?
               ORDER BY started_at DESC LIMIT 1""",
            (task_id,),
        )
        last_exec = await cursor.fetchone()

        last_run = None
        if last_exec:
            last_exec_dict = dict(last_exec)
            last_run = {
                "status": last_exec_dict["status"],
                "started_at": last_exec_dict["started_at"],
                "duration_ms": last_exec_dict["duration_ms"],
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
            next_run=next_run,
        )


@app.patch("/api/tasks/{task_id}", response_model=TaskResponse)
async def update_task(task_id: str, request: TaskUpdateRequest):
    """Update a task's schedule or enabled status."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check task exists
        cursor = await db.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
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
                f"UPDATE scheduled_tasks SET {', '.join(updates)} WHERE id = ?", params
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
                                day_of_week=parts[4],
                            )

                        scheduler.add_job(
                            execute_task,
                            trigger=trigger,
                            args=[task_id],
                            id=task_id,
                            replace_existing=True,
                        )
                    except Exception as e:
                        raise HTTPException(status_code=400, detail=f"Invalid schedule: {e}")

    # Return updated task
    return await get_task(task_id)


@app.post("/api/tasks/{task_id}/trigger")
async def trigger_task(task_id: str):
    """Manually trigger a task to run immediately."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id FROM scheduled_tasks WHERE id = ?", (task_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Task not found")

    if task_id not in TASK_REGISTRY:
        raise HTTPException(status_code=400, detail="Task has no implementation")

    # Run the task asynchronously
    asyncio.create_task(execute_task(task_id))

    return {"status": "triggered", "task_id": task_id}


@app.get("/api/tasks/{task_id}/history", response_model=list[TaskExecutionResponse])
async def get_task_history(task_id: str, limit: int = 20):
    """Get execution history for a task."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check task exists
        cursor = await db.execute("SELECT id FROM scheduled_tasks WHERE id = ?", (task_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail="Task not found")

        cursor = await db.execute(
            """SELECT * FROM task_executions
               WHERE task_id = ?
               ORDER BY started_at DESC
               LIMIT ?""",
            (task_id, limit),
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

            result.append(
                TaskExecutionResponse(
                    id=row_dict["id"],
                    task_id=row_dict["task_id"],
                    status=row_dict["status"],
                    started_at=row_dict["started_at"],
                    completed_at=row_dict["completed_at"],
                    duration_ms=row_dict["duration_ms"],
                    result=result_data,
                    retry_count=row_dict["retry_count"],
                )
            )

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
    has_command = "command" in data
    has_structured = "model" in data and "prompt_path" in data
    if "name" not in data or "schedule" not in data or not (has_command or has_structured):
        raise HTTPException(
            status_code=400,
            detail="name, schedule, and either command or (model + prompt_path) required",
        )
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


@app.post("/api/cron/jobs/{job_id}/victory")
async def declare_cron_victory(job_id: str, request: Request):
    """Declare victory for a cron job run — record reason and fire Discord notification.
    Body: {"reason": "...", "run_id": optional int}
    Agents can call this explicitly instead of emitting the ##IMPERIUM_VICTORIOUS:...## pattern."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    run_id = body.get("run_id")

    job = await cron_engine.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Persist victory_reason to the specified run (or most recent run)
    async with aiosqlite.connect(DB_PATH) as db:
        if run_id:
            await db.execute(
                "UPDATE cron_runs SET victory_reason = ? WHERE id = ?", (reason, run_id)
            )
        else:
            await db.execute(
                "UPDATE cron_runs SET victory_reason = ? WHERE id = (SELECT id FROM cron_runs WHERE job_id = ? ORDER BY id DESC LIMIT 1)",
                (reason, job_id),
            )
        await db.commit()

    await cron_engine.handle_victory(job, run_id or 0, reason)
    await log_event(
        "cron_victory", details={"job_id": job_id, "job_name": job["name"], "reason": reason}
    )
    return {"job_id": job_id, "job_name": job["name"], "victory": True, "reason": reason}


@app.get("/api/cron/jobs/{job_id}/runs")
async def get_cron_job_runs(job_id: str, limit: int = 20):
    """Get recent run history for a cron job."""
    runs = await cron_engine.get_runs(job_id, limit=limit)
    return {"runs": runs}


@app.get("/api/cron/status")
async def get_cron_status():
    """Overall cron engine status."""
    return await cron_engine.get_status()


@app.post("/api/fleet/pause")
async def pause_fleet(request: Request):
    """Pause the fleet by disabling enabled cron jobs.
    Stores which jobs were enabled so /unpause restores exactly the previous state.
    Optional body: {"commanders": ["mechanicus"]} to pause specific factions."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    commanders = data.get("commanders")
    return await cron_engine.pause_fleet(commanders)


@app.post("/api/fleet/unpause")
async def unpause_fleet():
    """Unpause the fleet by re-enabling jobs that were paused."""
    return await cron_engine.unpause_fleet()


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
            body = line[bracket_end + 1 :].strip()

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
            body = line[bracket_end + 1 :].strip()
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
        result = await asyncio.to_thread(
            subprocess.run,
            ["openclaw", "system", "heartbeat", "last"],
            capture_output=True,
            text=True,
            timeout=5,
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

    return {"logs": recent_logs, "count": len(recent_logs)}


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint with API info."""
    return {
        "name": "Token-API",
        "version": "0.1.0",
        "description": "Local FastAPI server for Claude instance management",
        "docs": "/docs",
        "ui": "/ui/ops",
    }


def _ops_parse_datetime(timestamp: str | datetime | None) -> datetime | None:
    if not timestamp:
        return None
    if isinstance(timestamp, datetime):
        return timestamp
    try:
        normalized = timestamp.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def _ops_seconds_since(
    timestamp: str | datetime | None, *, now: datetime | None = None
) -> int | None:
    parsed = _ops_parse_datetime(timestamp)
    if not parsed:
        return None
    reference = now or (datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now())
    try:
        return max(0, int((reference - parsed).total_seconds()))
    except Exception:
        return None


def _ops_parse_event_details(raw: str | None) -> dict | list | str | None:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _ops_instance_staleness(status: str | None, age_seconds: int | None) -> dict:
    if age_seconds is None:
        return {"is_stale": False, "threshold_seconds": None, "reason": None}
    threshold = 10 * 60 if status == "processing" else 2 * 60 * 60
    is_stale = age_seconds >= threshold
    return {
        "is_stale": is_stale,
        "threshold_seconds": threshold,
        "reason": f"{status or 'unknown'}_activity_age" if is_stale else None,
    }


def _ops_display_name(inst: dict) -> str:
    return (
        inst.get("tab_name")
        or inst.get("pane_label")
        or inst.get("session_doc_title")
        or inst.get("working_dir")
        or str(inst.get("id") or "")[:12]
    )


def _ops_parse_duration_seconds(value: str | int | float | None, default: int) -> int:
    """Parse compact duration query params such as `21600`, `6h`, `60s`, `15m`."""
    if value is None:
        return default
    if isinstance(value, int | float):
        return max(1, int(value))
    raw = str(value).strip().lower()
    if not raw:
        return default
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", raw)
    if not match:
        return default
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return max(1, int(amount * multiplier))


def _ops_timer_mode_rate(mode: str | None) -> int:
    """Timer v2 signed balance rate in ms/ms for history reconstruction."""
    normalized = (mode or "").lower()
    if normalized == TimerMode.WORKING.value:
        return 1
    if normalized in (TimerMode.DISTRACTED.value, TimerMode.BREAK.value):
        return -1
    return 0


def _ops_advance_timer_balance(balance_ms: int, mode: str | None, seconds: float) -> int:
    return int(balance_ms + (_ops_timer_mode_rate(mode) * seconds * 1000))


def _ops_shift_to_annotation(row: dict) -> dict:
    mode = row.get("new_mode") or "unknown"
    trigger = row.get("trigger") or "mode_change"
    lane = "timer"
    if row.get("source") == "phone" or row.get("phone_app"):
        lane = "phone"
    elif trigger in {"enforcement", "break_exhausted"}:
        lane = "enforcement"
    severity = "info"
    if mode in {TimerMode.DISTRACTED.value, TimerMode.BREAK.value}:
        severity = "warn"
    if trigger in {"enforcement", "break_exhausted"}:
        severity = "bad"
    if mode == TimerMode.WORKING.value:
        severity = "good"
    return {
        "id": f"timer-shift-{row.get('id')}",
        "t": row.get("timestamp"),
        "lane": lane,
        "type": trigger,
        "label": f"{row.get('old_mode') or 'start'} → {mode}",
        "severity": severity,
        "details": {
            "source": row.get("source"),
            "phone_app": row.get("phone_app"),
            "break_balance_ms": row.get("break_balance_ms"),
            "details": _ops_parse_event_details(row.get("details")),
        },
    }


async def _ops_read_timer_history(window: str | int = "6h", bucket: str | int = "60s") -> dict:
    """Return live timer history for the ops graph.

    The existing timer persistence stores exact shift rows plus current engine
    state, not 1Hz samples. This read model reconstructs a bucketed line from
    the last known shift, the TimerEngine's signed mode rates, and the current
    live snapshot. It is real telemetry, not frontend mock data; gaps are
    explicitly marked when no shift row anchors the requested window.
    """
    now = datetime.now()
    window_seconds = max(15 * 60, min(_ops_parse_duration_seconds(window, 6 * 3600), 48 * 3600))
    bucket_seconds = max(10, min(_ops_parse_duration_seconds(bucket, 60), 15 * 60))
    start = now - timedelta(seconds=window_seconds)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT *
            FROM timer_shifts
            WHERE datetime(timestamp) < datetime(?)
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
            """,
            (start.isoformat(),),
        )
        previous = await cursor.fetchone()
        cursor = await db.execute(
            """
            SELECT *
            FROM timer_shifts
            WHERE datetime(timestamp) >= datetime(?) AND datetime(timestamp) <= datetime(?)
            ORDER BY timestamp ASC, id ASC
            """,
            (start.isoformat(), now.isoformat()),
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    work_state = await get_cached_work_state()
    current_mode = timer_engine.current_mode.value
    current_balance = int(timer_engine.break_balance_ms)
    current_active = int(work_state.active_instance_count)
    current_processing = int(work_state.processing_recent_count)
    current_phone_app = PHONE_STATE.get("current_app")
    current_desktop_mode = DESKTOP_STATE.get("current_mode", "silence")

    anchors: list[dict] = []
    gaps: list[dict] = []
    data_start = start

    if previous:
        prev = dict(previous)
        prev_t = _ops_parse_datetime(prev.get("timestamp")) or start
        prev_mode = prev.get("new_mode") or current_mode
        prev_balance = int(prev.get("break_balance_ms") or 0)
        start_balance = _ops_advance_timer_balance(
            prev_balance, prev_mode, max(0, (start - prev_t).total_seconds())
        )
        anchors.append(
            {
                "t": start,
                "mode": prev_mode,
                "activity": "distraction"
                if prev_mode in {TimerMode.DISTRACTED.value, TimerMode.BREAK.value}
                else "working",
                "break_balance_ms": start_balance,
                "active_instance_count": int(prev.get("active_instances") or 0),
                "processing_recent_count": 0,
                "phone_app": prev.get("phone_app"),
                "desktop_mode": None,
                "productivity_active": bool(prev.get("active_instances") or 0),
            }
        )
    elif rows:
        first_t = _ops_parse_datetime(rows[0].get("timestamp")) or start
        data_start = first_t
        anchors.append(
            {
                "t": first_t,
                "mode": rows[0].get("new_mode") or rows[0].get("old_mode") or current_mode,
                "activity": "distraction"
                if (rows[0].get("new_mode") or rows[0].get("old_mode"))
                in {TimerMode.DISTRACTED.value, TimerMode.BREAK.value}
                else "working",
                "break_balance_ms": int(rows[0].get("break_balance_ms") or 0),
                "active_instance_count": int(rows[0].get("active_instances") or 0),
                "processing_recent_count": 0,
                "phone_app": rows[0].get("phone_app"),
                "desktop_mode": None,
                "productivity_active": bool(rows[0].get("active_instances") or 0),
            }
        )
        if first_t > start + timedelta(seconds=bucket_seconds):
            gaps.append(
                {"start": start.isoformat(), "end": first_t.isoformat(), "reason": "no_anchor"}
            )
    else:
        # No persisted shifts in the window. Use a flat live line rather than
        # fabricating a plausible arc.
        anchors.append(
            {
                "t": start,
                "mode": current_mode,
                "activity": timer_engine.activity.value,
                "break_balance_ms": current_balance,
                "active_instance_count": current_active,
                "processing_recent_count": current_processing,
                "phone_app": current_phone_app,
                "desktop_mode": current_desktop_mode,
                "productivity_active": work_state.productivity_active,
            }
        )
        gaps.append(
            {"start": start.isoformat(), "end": now.isoformat(), "reason": "no_timer_shifts"}
        )

    for row in rows:
        mode = row.get("new_mode") or current_mode
        anchors.append(
            {
                "t": _ops_parse_datetime(row.get("timestamp")) or now,
                "mode": mode,
                "activity": "distraction"
                if mode in {TimerMode.DISTRACTED.value, TimerMode.BREAK.value}
                else "working",
                "break_balance_ms": int(row.get("break_balance_ms") or 0),
                "active_instance_count": int(row.get("active_instances") or 0),
                "processing_recent_count": 0,
                "phone_app": row.get("phone_app"),
                "desktop_mode": None,
                "productivity_active": bool(row.get("active_instances") or 0),
            }
        )

    anchors.append(
        {
            "t": now,
            "mode": current_mode,
            "activity": timer_engine.activity.value,
            "break_balance_ms": current_balance,
            "active_instance_count": current_active,
            "processing_recent_count": current_processing,
            "phone_app": current_phone_app,
            "desktop_mode": current_desktop_mode,
            "productivity_active": work_state.productivity_active,
        }
    )
    anchors.sort(key=lambda item: item["t"])

    segments = []
    for i, anchor in enumerate(anchors[:-1]):
        next_anchor = anchors[i + 1]
        if next_anchor["t"] <= anchor["t"]:
            continue
        segments.append(
            {
                "start": anchor["t"].isoformat(),
                "end": next_anchor["t"].isoformat(),
                "mode": anchor["mode"],
                "activity": anchor["activity"],
                "productivity_active": anchor["productivity_active"],
                "source": None,
            }
        )

    points = []
    bucket_delta = timedelta(seconds=bucket_seconds)
    cursor = data_start
    anchor_index = 0
    while cursor < now:
        while anchor_index + 1 < len(anchors) and anchors[anchor_index + 1]["t"] <= cursor:
            anchor_index += 1
        anchor = anchors[anchor_index]
        elapsed = max(0, (cursor - anchor["t"]).total_seconds())
        points.append(
            {
                "t": cursor.isoformat(),
                "break_balance_ms": _ops_advance_timer_balance(
                    int(anchor["break_balance_ms"]), anchor["mode"], elapsed
                ),
                "mode": anchor["mode"],
                "activity": anchor["activity"],
                "productivity_active": anchor["productivity_active"],
                "active_instance_count": anchor["active_instance_count"],
                "processing_recent_count": anchor["processing_recent_count"],
                "desktop_mode": anchor.get("desktop_mode"),
                "phone_app": anchor.get("phone_app"),
            }
        )
        cursor += bucket_delta

    # Always include an exact live endpoint as the final point.
    points.append(
        {
            "t": now.isoformat(),
            "break_balance_ms": current_balance,
            "mode": current_mode,
            "activity": timer_engine.activity.value,
            "productivity_active": work_state.productivity_active,
            "active_instance_count": current_active,
            "processing_recent_count": current_processing,
            "desktop_mode": current_desktop_mode,
            "phone_app": current_phone_app,
        }
    )

    return {
        "generated_at": now.isoformat(),
        "window_seconds": window_seconds,
        "bucket_seconds": bucket_seconds,
        "points": points,
        "segments": segments,
        "annotations": [_ops_shift_to_annotation(row) for row in rows],
        "gaps": gaps,
        "source": "timer_shifts+live_timer_engine",
    }


async def _ops_read_cron_summary() -> dict:
    if cron_engine is not None:
        try:
            status = await cron_engine.get_status()
            jobs = status.get("jobs") or []
            return {
                "available": True,
                "total_jobs": status.get("total_jobs", 0),
                "enabled": status.get("enabled", 0),
                "running": status.get("running", 0),
                "runs_last_24h": status.get("runs_last_24h", 0),
                "jobs": jobs[:12],
            }
        except Exception as exc:
            logger.warning(f"Ops cron summary via cron_engine failed: {exc}")

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            counts = await db.execute(
                """
                SELECT
                    COUNT(*) AS total_jobs,
                    SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled
                FROM cron_jobs
                """
            )
            count_row = await counts.fetchone()
            runs = await db.execute(
                "SELECT COUNT(*) AS runs_last_24h FROM cron_runs WHERE started_at > ?",
                ((datetime.now() - timedelta(hours=24)).isoformat(),),
            )
            runs_row = await runs.fetchone()
            jobs_cursor = await db.execute(
                """
                SELECT id, name, enabled, schedule_type, schedule_value, updated_at
                FROM cron_jobs
                ORDER BY enabled DESC, name ASC
                LIMIT 12
                """
            )
            jobs = [dict(row) for row in await jobs_cursor.fetchall()]
        return {
            "available": True,
            "total_jobs": int(count_row["total_jobs"] or 0) if count_row else 0,
            "enabled": int(count_row["enabled"] or 0) if count_row else 0,
            "running": 0,
            "runs_last_24h": int(runs_row["runs_last_24h"] or 0) if runs_row else 0,
            "jobs": jobs,
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "total_jobs": 0,
            "enabled": 0,
            "running": 0,
            "runs_last_24h": 0,
            "jobs": [],
        }


async def _ops_read_enforcement_summary() -> dict:
    now = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT *
                FROM expected_acknowledgements
                WHERE status = 'pending'
                ORDER BY ack_due_at ASC
                LIMIT 12
                """
            )
            rows = await cursor.fetchall()
        pending = []
        for row in rows:
            ack = _expected_ack_row_to_dict(row)
            ack["current_level"] = _ack_current_level(ack, now)
            pending.append(ack)
        return {
            "available": True,
            "pending_count": len(pending),
            "pending": pending,
            "pavlok": {
                "enabled": PAVLOK_CONFIG.get("enabled"),
                "zap_count": PAVLOK_STATE.get("zap_count", 0),
                "daily_zap_cap": PAVLOK_CONFIG.get("daily_zap_cap", 6),
                "last_zap_at": PAVLOK_STATE.get("last_zap_at"),
                "last_soft_at": PAVLOK_STATE.get("last_soft_at"),
            },
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "pending_count": 0,
            "pending": [],
            "pavlok": {
                "enabled": PAVLOK_CONFIG.get("enabled"),
                "zap_count": PAVLOK_STATE.get("zap_count", 0),
                "daily_zap_cap": PAVLOK_CONFIG.get("daily_zap_cap", 6),
                "last_zap_at": PAVLOK_STATE.get("last_zap_at"),
                "last_soft_at": PAVLOK_STATE.get("last_soft_at"),
            },
        }


async def _ops_read_instances(now: datetime) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT ci.*,
                   sd.title AS session_doc_title,
                   sd.file_path AS session_doc_path,
                   sd.status AS session_doc_status,
                   sd.project AS session_doc_project,
                   sd.cron_job_id AS session_doc_cron_job_id
            FROM claude_instances ci
            LEFT JOIN session_documents sd ON sd.id = ci.session_doc_id
            WHERE ci.status IN ('processing', 'idle')
            ORDER BY
                CASE ci.status WHEN 'processing' THEN 0 WHEN 'idle' THEN 1 ELSE 2 END,
                ci.last_activity DESC
            LIMIT 160
            """
        )
        rows = await cursor.fetchall()

    active = []
    status_counts: dict[str, int] = {}
    engine_counts: dict[str, int] = {}
    legion_counts: dict[str, int] = {}
    stale_count = 0
    for row in rows:
        inst = dict(row)
        status = inst.get("status") or "unknown"
        engine = inst.get("engine") or "claude"
        legion = inst.get("legion") or inst.get("instance_type") or "unassigned"
        status_counts[status] = status_counts.get(status, 0) + 1
        engine_counts[engine] = engine_counts.get(engine, 0) + 1
        legion_counts[legion] = legion_counts.get(legion, 0) + 1
        activity_anchor = inst.get("last_activity") or inst.get("registered_at")
        age_seconds = _ops_seconds_since(activity_anchor, now=now)
        staleness = _ops_instance_staleness(status, age_seconds)
        if staleness["is_stale"]:
            stale_count += 1
        gt_job = scheduler.get_job(f"golden-throne-{inst.get('id')}")
        active.append(
            {
                "id": inst.get("id"),
                "session_id": inst.get("session_id"),
                "display_name": _ops_display_name(inst),
                "tab_name": inst.get("tab_name"),
                "status": status,
                "engine": engine,
                "device_id": inst.get("device_id"),
                "working_dir": inst.get("working_dir"),
                "tmux_pane": inst.get("tmux_pane"),
                "pane_label": inst.get("pane_label"),
                "last_activity": inst.get("last_activity"),
                "registered_at": inst.get("registered_at"),
                "age_seconds": age_seconds,
                "age_minutes": None if age_seconds is None else age_seconds // 60,
                "is_subagent": bool(inst.get("is_subagent") or 0),
                "legion": inst.get("legion"),
                "instance_type": inst.get("instance_type"),
                "workflow_state": inst.get("workflow_state"),
                "next_required_action": inst.get("next_required_action"),
                "stop_allowed": bool(inst.get("stop_allowed"))
                if inst.get("stop_allowed") is not None
                else None,
                "session_doc": {
                    "id": inst.get("session_doc_id"),
                    "title": inst.get("session_doc_title"),
                    "path": inst.get("session_doc_path") or inst.get("dispatch_session_doc_path"),
                    "status": inst.get("session_doc_status"),
                    "project": inst.get("session_doc_project"),
                    "policy": inst.get("session_doc_policy"),
                    "binding_source": inst.get("continuity_binding_source"),
                    "cron_job_id": inst.get("session_doc_cron_job_id"),
                },
                "stale": staleness,
                "zealotry": inst.get("zealotry") or 4,
                "gt": {
                    "next_fire": gt_job.next_run_time.isoformat()
                    if gt_job and gt_job.next_run_time
                    else None,
                    "resume_count": inst.get("gt_resume_count") or 0,
                    "resume_window_started_at": inst.get("gt_resume_window_started_at"),
                    "last_resume_at": inst.get("gt_last_resume_at"),
                    "victory_at": inst.get("victory_at"),
                    "victory_reason": inst.get("victory_reason"),
                },
            }
        )

    return {
        "active": active,
        "counts": {
            "active": len(active),
            "stale": stale_count,
            "by_status": status_counts,
            "by_engine": engine_counts,
            "by_legion": legion_counts,
        },
    }


async def _ops_read_events() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT event_type, instance_id, device_id, details, created_at
            FROM events
            ORDER BY created_at DESC
            LIMIT 24
            """
        )
        rows = await cursor.fetchall()
    return [
        {
            "event_type": row["event_type"],
            "instance_id": row["instance_id"],
            "device_id": row["device_id"],
            "details": _ops_parse_event_details(row["details"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _ops_assertion(
    assertion_id: str,
    label: str,
    value: str,
    status: str,
    *,
    confidence: str = "high",
    evidence: list[str] | None = None,
    freshness_seconds: int | None = None,
    correction_hint: str | None = None,
    details: dict | None = None,
) -> dict:
    return {
        "id": assertion_id,
        "label": label,
        "value": value,
        "status": status,
        "confidence": confidence,
        "evidence": evidence or [],
        "freshness_seconds": freshness_seconds,
        "correction_hint": correction_hint,
        "details": details or {},
    }


def _ops_build_state_assertions(
    *,
    generated_at: datetime,
    work_state: WorkStateResponse,
    instances: dict,
    events: list[dict],
    enforcement_summary: dict,
    tts_summary: dict,
) -> list[dict]:
    """Plain-language assertions: what Token-API currently believes is true.

    These are intentionally redundant with the raw state. The cockpit should
    show beliefs, evidence, freshness, and correction affordances without
    making the operator infer semantics from dozens of fields.
    """
    desktop_mode = DESKTOP_STATE.get("current_mode", "silence")
    desktop_last = DESKTOP_STATE.get("last_detection")
    desktop_age = _ops_seconds_since(desktop_last, now=generated_at)
    phone_app = PHONE_STATE.get("current_app")
    phone_distracted = bool(PHONE_STATE.get("is_distracted", False))
    phone_last = PHONE_STATE.get("last_activity")
    phone_age = _ops_seconds_since(phone_last, now=generated_at)
    phone_heartbeat_age = _ops_seconds_since(PHONE_HEARTBEAT.get("last_seen"))
    mode = timer_engine.current_mode.value
    break_balance = int(timer_engine.break_balance_ms)
    stale_count = int((instances.get("counts") or {}).get("stale") or 0)
    active_count = int((instances.get("counts") or {}).get("active") or 0)
    pending_enforcement = int(enforcement_summary.get("pending_count") or 0)
    tts_queue_len = int(tts_summary.get("queue_length") or 0)
    latest_phone_distraction = next(
        (event for event in events if event.get("event_type") == "phone_distraction_observed"),
        None,
    )
    latest_desktop_detection = next(
        (event for event in events if "desktop" in str(event.get("event_type") or "")),
        None,
    )
    latest_phone_details = (
        _ops_parse_event_details(latest_phone_distraction.get("details"))
        if latest_phone_distraction
        else None
    )
    latest_phone_label = "none"
    if isinstance(latest_phone_details, dict):
        latest_phone_label = str(
            latest_phone_details.get("display_name") or latest_phone_details.get("app") or "unknown"
        )

    assertions = [
        _ops_assertion(
            "timer_mode",
            "Timer mode",
            mode.upper(),
            "bad"
            if mode in {TimerMode.DISTRACTED.value, TimerMode.BREAK.value}
            else "warn"
            if mode == TimerMode.MULTITASKING.value
            else "good"
            if mode == TimerMode.WORKING.value
            else "neutral",
            evidence=[
                f"activity={timer_engine.activity.value}",
                f"productivity_active={work_state.productivity_active}",
                f"manual_mode={timer_engine.manual_mode.value if timer_engine.manual_mode else 'none'}",
            ],
            correction_hint="If wrong, use timer-mode resume/pause/break or correct the attention source.",
            details={
                "activity": timer_engine.activity.value,
                "productivity_active": work_state.productivity_active,
                "manual_mode": timer_engine.manual_mode.value if timer_engine.manual_mode else None,
            },
        ),
        _ops_assertion(
            "break_balance",
            "Break balance",
            f"{round(break_balance / 60000, 1)} min",
            "bad" if break_balance < 0 else "good",
            evidence=[
                f"available_ms={max(0, break_balance)}",
                f"backlog_ms={abs(min(0, break_balance))}",
            ],
            correction_hint="If this jumped unexpectedly, inspect timer history gaps and reset/daily-reset events.",
            details={"break_balance_ms": break_balance},
        ),
        _ops_assertion(
            "productivity",
            "Productivity",
            "ACTIVE" if work_state.productivity_active else "INACTIVE",
            "good" if work_state.productivity_active else "neutral",
            evidence=[
                work_state.reason,
                f"active_instances={work_state.active_instance_count}",
                f"observed_agents={work_state.observed_agent_count}",
                f"processing_recent={work_state.processing_recent_count}",
            ],
            correction_hint="If wrong, check live agent panes, instance registration, and stale pane filtering.",
            details=work_state.model_dump(),
        ),
        _ops_assertion(
            "desktop_attention",
            "Desktop attention",
            str(desktop_mode or "unknown"),
            "bad"
            if desktop_mode in {"scrolling", "gaming"}
            else "warn"
            if desktop_mode in {"video", "music"}
            else "neutral",
            confidence="low" if desktop_age is not None and desktop_age > 300 else "high",
            freshness_seconds=desktop_age,
            evidence=[
                f"work_mode={DESKTOP_STATE.get('work_mode', 'clocked_in')}",
                f"ahk_reachable={DESKTOP_STATE.get('ahk_reachable')}",
                f"steam={DESKTOP_STATE.get('steam_app_name') or DESKTOP_STATE.get('steam_exe') or 'none'}",
            ],
            correction_hint="If wrong, correct the desktop detector/AHK source or close the detected app.",
            details={
                "last_detection": desktop_last,
                "steam_app_name": DESKTOP_STATE.get("steam_app_name"),
                "steam_exe": DESKTOP_STATE.get("steam_exe"),
            },
        ),
        _ops_assertion(
            "phone_attention",
            "Phone attention",
            str(phone_app or "clear"),
            "bad"
            if phone_distracted
            else "warn"
            if timer_engine.activity == Activity.DISTRACTION and latest_phone_distraction
            else "neutral",
            confidence="low"
            if phone_heartbeat_age is not None and phone_heartbeat_age > 600
            else "low"
            if timer_engine.activity == Activity.DISTRACTION
            and not phone_distracted
            and latest_phone_distraction
            else "medium"
            if phone_age is not None and phone_age > 180
            else "high",
            freshness_seconds=phone_age,
            evidence=[
                f"is_distracted={phone_distracted}",
                f"heartbeat_age_s={phone_heartbeat_age if phone_heartbeat_age is not None else 'unknown'}",
                f"app_opened_at={PHONE_STATE.get('app_opened_at') or 'none'}",
                f"latest_phone_distraction={latest_phone_label}",
            ],
            correction_hint="If wrong, send/refresh phone close telemetry or clear stale phone app state.",
            details={
                "last_activity": phone_last,
                "heartbeat_age_seconds": phone_heartbeat_age,
                "latest_phone_distraction": latest_phone_distraction,
            },
        ),
        _ops_assertion(
            "fleet",
            "Fleet",
            f"{active_count} active",
            "warn" if stale_count else "good" if active_count else "neutral",
            evidence=[
                f"stale={stale_count}",
                f"processing={instances.get('counts', {}).get('by_status', {}).get('processing', 0)}",
                f"idle={instances.get('counts', {}).get('by_status', {}).get('idle', 0)}",
            ],
            correction_hint="If wrong, refresh pane registrations or inspect stale instance rows.",
            details=instances.get("counts", {}),
        ),
        _ops_assertion(
            "enforcement",
            "Enforcement",
            "CLEAR" if pending_enforcement == 0 else f"{pending_enforcement} pending",
            "good" if pending_enforcement == 0 else "bad",
            evidence=[
                f"pavlok_enabled={((enforcement_summary.get('pavlok') or {}).get('enabled'))}",
                f"pending_count={pending_enforcement}",
            ],
            correction_hint="If wrong, acknowledge/resolve pending expected acknowledgements.",
            details={"pending": enforcement_summary.get("pending", [])[:3]},
        ),
        _ops_assertion(
            "tts",
            "TTS queue",
            f"{tts_queue_len} queued",
            "warn" if tts_queue_len else "neutral",
            evidence=[
                f"backend={tts_summary.get('backend')}",
                f"satellite_available={tts_summary.get('satellite_available')}",
                f"global_mode={tts_summary.get('global_mode')}",
            ],
            correction_hint="If wrong, inspect TTS queue status or satellite health.",
            details={
                "hot_queue_length": tts_summary.get("hot_queue_length", 0),
                "pause_queue_length": tts_summary.get("pause_queue_length", 0),
            },
        ),
    ]

    if (
        timer_engine.activity == Activity.DISTRACTION
        and desktop_mode
        not in {
            "video",
            "scrolling",
            "gaming",
        }
        and not phone_distracted
    ):
        latest = latest_phone_distraction or latest_desktop_detection
        latest_details = _ops_parse_event_details(latest.get("details")) if latest else None
        latest_label = "unknown"
        if isinstance(latest_details, dict):
            latest_label = str(
                latest_details.get("display_name")
                or latest_details.get("app")
                or latest_details.get("mode")
                or latest.get("event_type")
            )
        elif latest:
            latest_label = str(latest.get("event_type"))
        assertions.append(
            _ops_assertion(
                "attention_consistency",
                "Attention consistency",
                "TIMER DISTRACTION WITHOUT ACTIVE SOURCE",
                "bad",
                confidence="low",
                freshness_seconds=_ops_seconds_since(latest.get("created_at"), now=generated_at)
                if latest
                else None,
                evidence=[
                    f"timer_activity={timer_engine.activity.value}",
                    f"desktop_mode={desktop_mode}",
                    f"phone_current_app={phone_app or 'none'}",
                    f"latest_source={latest_label}",
                ],
                correction_hint="Timer says distraction but live attention sources look clear. Refresh phone telemetry or clear stale timer activity/source.",
                details={"latest_attention_event": latest},
            )
        )

    # Attention-critical assertions first, quiet/neutral assertions later.
    severity_order = {"bad": 0, "warn": 1, "good": 2, "neutral": 3}
    confidence_order = {"low": 0, "medium": 1, "high": 2}
    return sorted(
        assertions,
        key=lambda item: (
            severity_order.get(item["status"], 4),
            confidence_order.get(item["confidence"], 3),
            item["label"],
        ),
    )


@app.get("/ui/ops")
async def ops_ui():
    """Serve the Terminus ops cockpit shell."""
    index_path = UI_DIR / "ops" / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Ops UI build not found")
    return FileResponse(index_path, media_type="text/html")


@app.get("/ui/ops/{asset_path:path}")
async def ops_ui_asset(asset_path: str):
    """Serve built Terminus ops assets without exposing paths outside ui/ops."""
    if not asset_path or asset_path.startswith(("/", "\\")):
        raise HTTPException(status_code=404, detail="Ops UI asset not found")
    root = (UI_DIR / "ops").resolve()
    target = (root / asset_path).resolve()
    if not target.is_relative_to(root) or not target.is_file() or target.name == "index.html":
        raise HTTPException(status_code=404, detail="Ops UI asset not found")
    media_type, _ = mimetypes.guess_type(str(target))
    return FileResponse(target, media_type=media_type or "application/octet-stream")


@app.get("/api/ui/ops/timer/history")
async def get_ops_timer_history(window: str = "6h", bucket: str = "60s"):
    """Live timer history for the ops cockpit graph."""
    return await _ops_read_timer_history(window=window, bucket=bucket)


@app.get("/api/ui/ops/state")
async def get_ops_display_state():
    """Aggregate read model for the Terminus ops cockpit."""
    now = datetime.now()
    work_state = await get_cached_work_state()
    instances = await _ops_read_instances(now)
    events = await _ops_read_events()
    cron_summary = await _ops_read_cron_summary()
    enforcement_summary = await _ops_read_enforcement_summary()
    tts_summary = get_tts_queue_status()
    assertions = _ops_build_state_assertions(
        generated_at=now,
        work_state=work_state,
        instances=instances,
        events=events,
        enforcement_summary=enforcement_summary,
        tts_summary=tts_summary,
    )

    break_balance_ms = timer_engine.break_balance_ms
    return {
        "surface": "ops",
        "generated_at": now.isoformat(),
        "timer": {
            "mode": timer_engine.current_mode.value,
            "activity": timer_engine.activity.value,
            "productivity_active": work_state.productivity_active,
            "manual_mode": timer_engine.manual_mode.value if timer_engine.manual_mode else None,
            "manual_mode_lock": timer_engine.manual_mode_lock,
            "manual_trigger": timer_engine.manual_trigger,
            "focus_active": timer_engine.focus_active,
            "break_balance_ms": break_balance_ms,
            "break_available_ms": max(0, break_balance_ms),
            "break_backlog_ms": abs(min(0, break_balance_ms)),
            "is_in_backlog": break_balance_ms < 0,
            "total_work_time_ms": timer_engine.total_work_time_ms,
            "total_break_time_ms": timer_engine.total_break_time_ms,
            "daily_start_date": timer_engine.daily_start_date,
        },
        "assertions": assertions,
        "attention": {
            "desktop": {
                "mode": DESKTOP_STATE.get("current_mode", "silence"),
                "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
                "last_detection": DESKTOP_STATE.get("last_detection"),
                "location_zone": DESKTOP_STATE.get("location_zone"),
                "ahk_reachable": DESKTOP_STATE.get("ahk_reachable"),
                "steam_app_id": DESKTOP_STATE.get("steam_app_id"),
                "steam_app_name": DESKTOP_STATE.get("steam_app_name"),
                "steam_exe": DESKTOP_STATE.get("steam_exe"),
                "in_meeting": DESKTOP_STATE.get("in_meeting", False),
            },
            "phone": {
                "app": PHONE_STATE.get("current_app"),
                "is_distracted": PHONE_STATE.get("is_distracted", False),
                "last_activity": PHONE_STATE.get("last_activity"),
                "app_opened_at": PHONE_STATE.get("app_opened_at"),
                "heartbeat_age_seconds": _ops_seconds_since(PHONE_HEARTBEAT.get("last_seen")),
            },
        },
        "work_state": work_state.model_dump(),
        "instances": instances,
        "events": events,
        "cron": cron_summary,
        "tts": {
            "current": tts_summary.get("current"),
            "hot_queue": tts_summary.get("hot_queue", []),
            "pause_queue": tts_summary.get("pause_queue", []),
            "hot_queue_length": tts_summary.get("hot_queue_length", 0),
            "pause_queue_length": tts_summary.get("pause_queue_length", 0),
            "queue_length": tts_summary.get("queue_length", 0),
            "backend": tts_summary.get("backend"),
            "satellite_available": tts_summary.get("satellite_available"),
            "global_mode": tts_summary.get("global_mode"),
        },
        "voice_drafts": [
            _discord_voice_draft_summary(key, state) for key, state in _discord_voice_drafts.items()
        ],
        "enforcement": enforcement_summary,
    }


# [MOVED to shared.py / routes/tts.py] — was: # ============ TTS/Notification System ===========

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
                    await timer_log_shift(
                        "idle", "break", trigger="idle_timeout", source="timer_worker"
                    )
                    asyncio.create_task(
                        handle_custodes_state_event(
                            "idle_timeout",
                            "timer_worker",
                            payload={"timer_mode": "break"},
                        )
                    )
                    _current_session_id = await timer_start_session("break", today)
                    _session_start_ms = now_ms
                    _mode_change_count += 1
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(None, speak_tts, "break mode")
                    continue
                elif event == TimerEvent.DISTRACTION_TIMEOUT:
                    print("TIMER: Distraction timeout — scrolling/gaming ≥10min → DISTRACTED")
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        await timer_end_session(_current_session_id, duration_ms)
                    await timer_log_shift(
                        "multitasking",
                        "distracted",
                        trigger="distraction_timeout",
                        source="timer_worker",
                    )
                    asyncio.create_task(
                        handle_custodes_state_event(
                            "distraction_timeout",
                            "timer_worker",
                            severity=2,
                            payload={"timer_mode": "distracted"},
                        )
                    )
                    _current_session_id = await timer_start_session("distracted", today)
                    _session_start_ms = now_ms
                    _mode_change_count += 1
                    # Enforce: close distraction windows + Pavlok + phone notify
                    close_distraction_windows()
                    send_pavlok_stimulus(reason="distraction_timeout")
                    asyncio.create_task(
                        asyncio.to_thread(
                            _send_to_phone,
                            "/notify",
                            {
                                "vibe": 30,
                                "banner_text": "distraction logged",
                            },
                        )
                    )
                    loop = asyncio.get_event_loop()
                    loop.run_in_executor(None, speak_tts, "distraction logged")
                    continue
                elif event == TimerEvent.MODE_CHANGED and (
                    has_idle_timeout or has_distraction_timeout
                ):
                    continue  # Already handled above
                elif event == TimerEvent.BREAK_EXHAUSTED:
                    await timer_log_shift(
                        timer_engine.current_mode.value,
                        "break_exhausted",
                        trigger="enforcement",
                        source="timer_worker",
                    )
                    asyncio.create_task(
                        handle_custodes_state_event(
                            "break_exhausted",
                            "timer_worker",
                            severity=2,
                            payload={"break_balance_ms": timer_engine.break_balance_ms},
                        )
                    )
                    asyncio.create_task(_async_enforce_break_exhausted())
                elif event == TimerEvent.DAILY_RESET:
                    print(
                        f"TIMER: Daily reset (was {result.reset_date}, now {today}). Productivity score: {result.productivity_score}"
                    )
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        await timer_end_session(
                            _current_session_id,
                            duration_ms,
                            break_earned_ms=timer_engine.total_break_time_ms,
                        )
                    await timer_save_daily_score(
                        result.reset_date,
                        result.productivity_score or 0,
                        timer_engine.total_work_time_ms,
                        timer_engine.total_break_time_ms,
                        _mode_change_count,
                        _mode_change_count,
                    )
                    await generate_daily_timer_analytics(result.reset_date)
                    await timer_log_shift(
                        result.old_mode.value if result.old_mode else None,
                        timer_engine.current_mode.value,
                        trigger="daily_reset",
                        source="timer_worker",
                    )
                    _mode_change_count = 0
                    _current_session_id = await timer_start_session(
                        timer_engine.current_mode.value, today
                    )
                    _session_start_ms = now_ms
                elif event == TimerEvent.MODE_CHANGED and result.old_mode:
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        if result.old_mode in (
                            TimerMode.WORKING,
                            TimerMode.MULTITASKING,
                            TimerMode.DISTRACTED,
                        ):
                            await timer_end_session(
                                _current_session_id,
                                duration_ms,
                                break_earned_ms=timer_engine.total_break_time_ms,
                            )
                        else:
                            await timer_end_session(
                                _current_session_id,
                                duration_ms,
                                break_used_ms=timer_engine.total_break_time_ms,
                            )
                    await timer_log_mode_change(
                        result.old_mode.value if result.old_mode else None,
                        timer_engine.current_mode.value,
                        is_automatic=False,
                    )
                    _mode_change_count += 1
                    _current_session_id = await timer_start_session(
                        timer_engine.current_mode.value, today
                    )
                    _session_start_ms = now_ms
                    async with aiosqlite.connect(DB_PATH) as _wdb:
                        _cur = await _wdb.execute(
                            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
                        )
                        _row = await _cur.fetchone()
                        _active = _row[0] if _row else 0
                    asyncio.create_task(
                        push_phone_widget_async(timer_engine.current_mode.value, _active)
                    )

            # Phone current_app staleness check: if last_activity is >3 min old,
            # it's a phantom open (real usage would get refreshed by the debounce
            # or by a close event). Clear it to prevent false enforcement.
            _phone_last = PHONE_STATE.get("last_activity")
            _phone_app = PHONE_STATE.get("current_app")
            if _phone_app and _phone_last:
                try:
                    _phone_age = (
                        datetime.now() - datetime.fromisoformat(_phone_last)
                    ).total_seconds()
                    if _phone_age > 180:  # 3 minutes stale
                        print(
                            f"TIMER: Clearing stale phone_app={_phone_app!r} (last_activity {_phone_age:.0f}s ago)"
                        )
                        PHONE_STATE["current_app"] = None
                        PHONE_STATE["is_distracted"] = False
                        if _phone_app in ("twitter", "x", "com.twitter.android"):
                            PHONE_STATE["twitter_open_since"] = None
                        _sync_activity_from_remaining_distraction_signals(
                            int(time.monotonic() * 1000)
                        )
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
                    print(
                        f"TIMER: Twitter timer stale ({stale_elapsed:.0f}s) — current_app={current_app!r}, clearing (dropped close event)"
                    )
                    PHONE_STATE["twitter_open_since"] = None
                    PHONE_STATE["twitter_zapped"] = False
                else:
                    twitter_elapsed = time.monotonic() - twitter_since
                    if twitter_elapsed > 420:  # 7 minutes
                        now_mono = time.monotonic()
                        since_last_zap = now_mono - PHONE_STATE.get("twitter_last_zap_at", 0)
                        if since_last_zap < 1800:  # 30-minute cooldown
                            print(
                                f"TIMER: Twitter 7-min hit but cooldown active ({since_last_zap:.0f}s < 1800s). Skipping zap."
                            )
                            PHONE_STATE["twitter_open_since"] = None
                        else:
                            print(
                                f"TIMER: Twitter open for {twitter_elapsed:.0f}s (>7min). Forcing break."
                            )
                            PHONE_STATE["twitter_open_since"] = None  # one-shot per session
                            PHONE_STATE["twitter_zapped"] = (
                                True  # block re-zap until confirmed close
                            )
                            PHONE_STATE["twitter_last_zap_at"] = now_mono
                            PHONE_STATE["twitter_last_zap_wall"] = time.time()
                            _persist_twitter_zap_cooldown()
                            asyncio.create_task(_async_enforce_twitter_timeout())

            now = time.time()

            # Update idle_timeout_exempt based on location only
            # NOTE: work_mode is manual-only now (user clocks in/out explicitly).
            # Location-based exemptions still apply (e.g., campus = studying).
            location_zone = DESKTOP_STATE.get("location_zone")
            timer_engine.idle_timeout_exempt = location_zone == "campus"

            # Productivity layer update (every 10s) — poll DB for active instances
            if now - last_db_save >= 10:  # piggyback on DB save interval
                work_state = await compute_work_state()
                productivity_active = work_state.productivity_active

                old_mode = timer_engine.current_mode.value
                prod_result = timer_engine.set_productivity(productivity_active, now_ms)
                if TimerEvent.MODE_CHANGED in prod_result.events:
                    new_mode = timer_engine.current_mode.value
                    trigger = (
                        "productivity_active" if productivity_active else "productivity_inactive"
                    )
                    print(f"TIMER: Productivity {trigger} — {old_mode} → {new_mode}")
                    await timer_log_shift(
                        old_mode,
                        new_mode,
                        trigger=trigger,
                        source="timer_worker",
                        details=json.dumps(
                            work_state.model_dump(),
                            sort_keys=True,
                            default=str,
                        ),
                    )
                    if _current_session_id > 0:
                        duration_ms = now_ms - _session_start_ms
                        await timer_end_session(_current_session_id, duration_ms)
                    _current_session_id = await timer_start_session(new_mode, today)
                    _session_start_ms = now_ms
                    _mode_change_count += 1

                current_phone_app = (PHONE_STATE.get("current_app") or "").lower()
                current_phone_mode = (
                    PHONE_DISTRACTION_APPS.get(current_phone_app) if current_phone_app else None
                )
                if current_phone_app and current_phone_mode:
                    await maybe_create_phone_distraction_ack(
                        app_name=current_phone_app,
                        display_name=get_phone_app_display_name(current_phone_app),
                        package=None,
                        distraction_mode=current_phone_mode,
                        trigger="timer_worker",
                        productivity_active=productivity_active,
                    )

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

    # Desktop: notification sound + TTS
    play_sound()
    try:
        await asyncio.to_thread(subprocess.Popen, ["say", "-v", "Daniel", "twitter timeout"])
    except Exception:
        pass

    # Pavlok is queued separately; /enforce is notification/TTS/Spotify only.
    pavlok_result = await asyncio.to_thread(
        send_pavlok_stimulus,
        "zap",
        30,
        "twitter_timeout",
        True,
    )
    result = await asyncio.to_thread(
        _send_to_phone,
        "/enforce",
        {
            "tts_text": "twitter timeout",
            "banner_text": "Twitter timeout",
        },
    )

    # Force timer into BREAK mode (clear any existing manual mode first)
    old_mode = timer_engine.current_mode.value
    timer_engine._clear_manual_mode()
    changed, _ = timer_engine.enter_break(now_ms)
    if changed:
        today = datetime.now().strftime("%Y-%m-%d")
        await timer_log_mode_change(old_mode, "break", is_automatic=False)
        await timer_log_shift(
            old_mode, "break", trigger="enforcement", source="timer_worker", phone_app="twitter"
        )
        await timer_end_session(_current_session_id, now_ms - _session_start_ms)
        _current_session_id = await timer_start_session("break", today)
        _session_start_ms = now_ms

    await log_event(
        "twitter_timeout_enforcement",
        details={
            "old_mode": old_mode,
            "forced_break": changed,
            "pavlok": pavlok_result,
            "phone": result,
        },
    )


async def enforce_break_exhausted_impl() -> dict:
    """Shared enforcement logic for break exhaustion (used by timer worker and API endpoint)."""
    enforced_any = False
    phone_result = None
    desktop_result = None

    # Desktop enforcement: close distraction windows
    if DESKTOP_STATE.get("current_mode") in ("video", "scrolling", "gaming"):
        await maybe_create_backlog_violation_ack(
            surface="desktop",
            app_name=DESKTOP_STATE.get("steam_app_id")
            or DESKTOP_STATE.get("steam_exe")
            or DESKTOP_STATE.get("current_mode"),
            display_name=DESKTOP_STATE.get("steam_app_name") or DESKTOP_STATE.get("current_mode"),
            distraction_mode=DESKTOP_STATE.get("current_mode"),
            trigger="break_exhausted",
        )
    desktop_result = close_distraction_windows()
    if desktop_result.get("closed_count"):
        enforced_any = True
        print(
            f"BREAK-EXHAUSTED: Closed {desktop_result['closed_count']} desktop distraction windows"
        )

    # Phone enforcement: disable active distraction app
    current_app = PHONE_STATE.get("current_app")
    if current_app:
        await maybe_create_backlog_violation_ack(
            surface="phone",
            app_name=current_app,
            display_name=get_phone_app_display_name(current_app),
            distraction_mode=PHONE_DISTRACTION_APPS.get(current_app),
            trigger="break_exhausted",
        )
        enforce_app = current_app
        if current_app in ("x", "twitter", "com.twitter.android"):
            enforce_app = "twitter"
        elif current_app in (
            "youtube",
            "com.google.android.youtube",
            "app.revanced.android.youtube",
        ):
            enforce_app = "youtube"
        elif current_app in PHONE_DISTRACTION_APPS:
            mode = PHONE_DISTRACTION_APPS.get(current_app)
            if mode == "gaming":
                enforce_app = "game"

        print(
            f"BREAK-EXHAUSTED: Starting enforcement cascade on {current_app} (mapped to {enforce_app})"
        )
        start_enforcement_cascade(enforce_app)
        phone_result = {"cascade_started": True, "app": enforce_app}
        enforced_any = True

    if enforced_any:
        # Phone prompt only; Pavlok is handled by the backlog ack parry deadline.
        phone_notify = await asyncio.to_thread(
            _send_to_phone,
            "/notify",
            {
                "vibe": 60,
                "beep": 40,
                "tts_text": "break exhausted",
                "banner_text": "break exhausted",
            },
        )

    return {
        "enforced": enforced_any,
        "app": current_app,
        "desktop_enforcement": desktop_result,
        "phone_enforcement": phone_result,
    }


async def legion_pane_recolor_worker():
    """Background worker that processes the pane_recolor_queue table.

    The SQLite trigger `trg_legion_recolor` fires on any UPDATE to the legion
    column, inserting a row here. This worker polls every second, reads pending
    recolors, and applies `tmux select-pane -P 'bg=...'` to each pane.
    Catches ALL legion change entry points without caller cooperation.
    """
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, instance_id, legion, tmux_pane FROM pane_recolor_queue ORDER BY id"
                )
                rows = await cursor.fetchall()
                if not rows:
                    await asyncio.sleep(1)
                    continue

                processed_ids = []
                for row in rows:
                    queue_id = row["id"]
                    instance_id = row["instance_id"]
                    legion = row["legion"] or "astartes"
                    tmux_pane = row["tmux_pane"]

                    # If trigger didn't capture tmux_pane, look it up
                    if not tmux_pane:
                        cur2 = await db.execute(
                            "SELECT tmux_pane FROM claude_instances WHERE id = ?", (instance_id,)
                        )
                        pane_row = await cur2.fetchone()
                        tmux_pane = pane_row["tmux_pane"] if pane_row else None

                    if tmux_pane:
                        bg = LEGION_PANE_COLORS.get(legion, "default")
                        try:
                            if bg == "default":
                                cmd = ["tmux", "select-pane", "-t", tmux_pane, "-P", "bg=default"]
                            else:
                                cmd = ["tmux", "select-pane", "-t", tmux_pane, "-P", f"bg={bg}"]
                            await asyncio.to_thread(
                                _run_tmux_focus_preserved,
                                tuple(cmd),
                                source="token-api pane-recolor",
                                attempted_target=tmux_pane,
                            )
                            logger.info(
                                f"Legion recolor: {instance_id[:12]} → {legion} (bg={bg}, pane={tmux_pane})"
                            )
                        except Exception as e:
                            logger.warning(f"Legion recolor failed for {tmux_pane}: {e}")

                    processed_ids.append(queue_id)

                # Clear processed entries
                if processed_ids:
                    placeholders = ",".join("?" * len(processed_ids))
                    await db.execute(
                        f"DELETE FROM pane_recolor_queue WHERE id IN ({placeholders})",
                        processed_ids,
                    )
                    await db.commit()

        except Exception as e:
            logger.error(f"Legion recolor worker error: {e}")

        await asyncio.sleep(1)


async def pane_state_worker():
    """Process pane_state_queue — push tmux pane variables (@CC_STATE etc).

    The SQLite trigger `trg_status_pane_state` fires on any UPDATE to the status
    column, inserting a row here. This worker polls every second, reads pending
    state changes, and applies `tmux set-option -p` to each pane.
    """
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, instance_id, variable, value, tmux_pane FROM pane_state_queue ORDER BY id"
                )
                rows = await cursor.fetchall()
                if not rows:
                    await asyncio.sleep(1)
                    continue

                processed = []
                stopped_assertions: list[tuple[str, str]] = []
                for row in rows:
                    pane = row["tmux_pane"]
                    pane_label = None
                    if not pane:
                        cur2 = await db.execute(
                            "SELECT tmux_pane, pane_label FROM claude_instances WHERE id = ?",
                            (row["instance_id"],),
                        )
                        r = await cur2.fetchone()
                        pane = r["tmux_pane"] if r else None
                        pane_label = r["pane_label"] if r else None
                    else:
                        cur2 = await db.execute(
                            "SELECT pane_label FROM claude_instances WHERE id = ?",
                            (row["instance_id"],),
                        )
                        r = await cur2.fetchone()
                        pane_label = r["pane_label"] if r else None
                    if pane:
                        try:
                            await _run_subprocess_offloop(
                                (
                                    "tmux",
                                    "set-option",
                                    "-p",
                                    "-t",
                                    pane,
                                    row["variable"],
                                    row["value"],
                                ),
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.DEVNULL,
                                timeout=5,
                            )
                            logger.info(
                                f"Pane state: {row['instance_id'][:12]} {row['variable']}={row['value']} (pane={pane})"
                            )
                        except Exception as e:
                            logger.warning(f"Pane state set failed for {pane}: {e}")
                    if (
                        row["variable"] == "@CC_STATE"
                        and row["value"] == "stopped"
                        and not _is_assert_persona_label(pane_label)
                    ):
                        stopped_assertions.append((pane_label or pane, row["instance_id"]))
                    processed.append(row["id"])

                if processed:
                    ph = ",".join("?" * len(processed))
                    await db.execute(f"DELETE FROM pane_state_queue WHERE id IN ({ph})", processed)
                    await db.commit()
                for pane_target, instance_id in stopped_assertions:
                    spawn_tmux_assert_instance(pane_target, instance_id, "pane-state-stopped")
        except Exception as e:
            logger.error(f"Pane state worker error: {e}")
        await asyncio.sleep(1)


# ============ tmux↔DB Reconciler ============

RECONCILE_CYCLE_SECONDS = 30
RECONCILE_IDLE_THRESHOLD_SECONDS = 60
RECONCILE_PROCESSING_THRESHOLD_SECONDS = 10


async def _read_tmux_panes() -> dict[str, dict] | None:
    """Return ``{pane_id: {pane_label, session_window, current_command, title}}`` from tmux.

    Returns ``None`` when tmux itself is unreachable (don't reconcile a blind cycle).
    Returns ``{}`` when tmux is alive but has zero panes.
    """
    try:
        proc = await _run_subprocess_offloop(
            (
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}|#{@PANE_ID}|#{session_name}:#{window_name}|#{pane_current_command}|#{pane_title}",
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    panes: dict[str, dict] = {}
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("|", 4)
        if len(parts) < 5:
            continue
        pane_id, pane_label, session_window, current_command, title = parts
        if pane_id:
            panes[pane_id] = {
                "pane_label": pane_label or session_window,
                "session_window": session_window,
                "current_command": current_command,
                "title": title,
            }
    return panes


def _parse_last_activity(value) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        # Fallback to space-separated SQLite TIMESTAMP form
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def _reconcile_eligible(row: dict, now: datetime) -> bool:
    """Reconciler only mutates rows that aren't fresh writes from a live dispatch.

    - idle/active rows: ≥60s of inactivity (don't fight a fresh dispatch).
    - processing rows: ≥10s of inactivity (the bot is mid-tool-use; let it be).
    """
    last = _parse_last_activity(row.get("last_activity"))
    if last is None:
        return True
    age = (now - last).total_seconds()
    if row.get("status") == "processing":
        return age >= RECONCILE_PROCESSING_THRESHOLD_SECONDS
    return age >= RECONCILE_IDLE_THRESHOLD_SECONDS


def _is_placeholder_tab_name(tab_name: str | None) -> bool:
    if not tab_name:
        return False
    cleaned = tab_name.lstrip("✳⠐⠸ ").strip()
    if not cleaned:
        return False
    if cleaned in {"needs-name", "needs-session-name"}:
        return True
    return bool(DEFAULT_TAB_NAME_RX.match(cleaned))


def _clean_tab_name(tab_name: str | None) -> str:
    if not tab_name:
        return ""
    return tab_name.lstrip("✳⠐⠸ ").strip()


_SESSION_DOC_DATE_PREFIX_RX = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)$")


def _derive_session_doc_slug(file_path: str | None) -> str | None:
    """Extract the slug portion of a session doc filename (post date prefix)."""
    if not file_path:
        return None
    name = file_path.rsplit("/", 1)[-1]
    if name.endswith(".md"):
        name = name[:-3]
    match = _SESSION_DOC_DATE_PREFIX_RX.match(name)
    return match.group(1) if match else (name or None)


def _tab_name_session_doc_mismatch(tab_name: str | None, file_path: str | None) -> bool:
    cleaned = _clean_tab_name(tab_name)
    if not cleaned or _is_placeholder_tab_name(tab_name):
        return False
    slug = _derive_session_doc_slug(file_path)
    if not slug:
        return False
    a = cleaned.lower()
    b = slug.lower()
    if a == b:
        return False
    # Tolerant: either side wholly contained in the other counts as a match.
    return a not in b and b not in a


def _compute_drift_flags(
    row: dict,
    panes: dict[str, dict],
    duplicate_pane_ids: set[str],
) -> list[str]:
    """Return live drift flags for a single instance row.

    Vocabulary matches the reconciler event log:
    pane_label_drift, tab_name_placeholder, tab_name_session_doc_mismatch,
    superseded_duplicate, pane_missing.
    """
    flags: list[str] = []
    tmux_pane = row.get("tmux_pane")
    if tmux_pane and tmux_pane in duplicate_pane_ids:
        flags.append("superseded_duplicate")
    if tmux_pane and tmux_pane not in panes:
        flags.append("pane_missing")
    if tmux_pane and tmux_pane in panes:
        tmux_label = panes[tmux_pane]["pane_label"]
        if row.get("pane_label") != tmux_label:
            flags.append("pane_label_drift")
    if _is_placeholder_tab_name(row.get("tab_name")) and row.get("session_doc_id"):
        flags.append("tab_name_placeholder")
    if _tab_name_session_doc_mismatch(row.get("tab_name"), row.get("session_doc_path")):
        flags.append("tab_name_session_doc_mismatch")
    return flags


async def _run_tmux_db_reconcile_cycle() -> dict:
    """Single reconciliation pass. Returns counts dict for telemetry."""
    counts = {
        "pane_vanished": 0,
        "pane_label_drift": 0,
        "superseded_duplicate": 0,
        "placeholder_tab_name_drift": 0,
        "tab_name_session_doc_mismatch": 0,
    }
    panes = await _read_tmux_panes()
    if panes is None:
        # tmux unreachable — skip the cycle rather than stop a fleet of rows.
        return counts

    now = datetime.now()
    deferred_events: list[tuple[str, str, dict]] = []  # (event_type, instance_id, details)

    async with aiosqlite.connect(DB_PATH) as db:
        # Wait up to 5s for other writers before raising "database is locked".
        await db.execute("PRAGMA busy_timeout = 5000")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT ci.id, ci.tmux_pane, ci.pane_label, ci.tab_name, ci.session_doc_id,
                      ci.status, ci.last_activity, ci.workflow_blocked_reason,
                      sd.file_path AS session_doc_path
               FROM claude_instances ci
               LEFT JOIN session_documents sd ON ci.session_doc_id = sd.id
               WHERE ci.status != 'stopped'"""
        )
        rows = [dict(r) for r in await cursor.fetchall()]

        # Group rows by tmux_pane for duplicate detection.
        by_pane: dict[str, list[dict]] = {}
        for row in rows:
            tp = row.get("tmux_pane")
            if tp:
                by_pane.setdefault(tp, []).append(row)

        # Resolve which row "owns" each pane; the rest are superseded duplicates.
        owners: set[str] = set()
        superseded: list[dict] = []
        for tp, group in by_pane.items():
            if len(group) <= 1:
                owners.add(group[0]["id"])
                continue
            # Newest by last_activity wins. Fall back to id for stable order.
            ordered = sorted(
                group,
                key=lambda r: (r.get("last_activity") or "", r.get("id") or ""),
                reverse=True,
            )
            owners.add(ordered[0]["id"])
            superseded.extend(ordered[1:])

        # Stop superseded duplicates first.
        for row in superseded:
            if not _reconcile_eligible(row, now):
                continue
            try:
                await sanctioned_update_instance(
                    db,
                    instance_id=row["id"],
                    updates={
                        "status": "stopped",
                        "stopped_at": now.isoformat(),
                    },
                    mutation_type="reconcile_superseded",
                    write_source="tmux_db_reconciler",
                    actor="reconciler",
                )
                counts["superseded_duplicate"] += 1
            except Exception as e:
                logger.warning(f"Reconciler superseded mutation failed for {row.get('id')}: {e}")

        # Process owners + tmux_pane-less rows.
        for row in rows:
            tmux_pane = row.get("tmux_pane")
            if tmux_pane and row["id"] not in owners:
                continue  # already handled above
            if not _reconcile_eligible(row, now):
                continue

            # Pane vanished.
            if tmux_pane and tmux_pane not in panes:
                try:
                    await sanctioned_update_instance(
                        db,
                        instance_id=row["id"],
                        updates={
                            "status": "stopped",
                            "stopped_at": now.isoformat(),
                        },
                        mutation_type="reconcile_pane_missing",
                        write_source="tmux_db_reconciler",
                        actor="reconciler",
                    )
                    counts["pane_vanished"] += 1
                    if not _is_assert_persona_label(row.get("pane_label")):
                        spawn_tmux_assert_instance(
                            row.get("pane_label") or tmux_pane,
                            row.get("id", ""),
                            "reconciler-pane-missing",
                        )
                except Exception as e:
                    logger.warning(
                        f"Reconciler pane_missing mutation failed for {row.get('id')}: {e}"
                    )
                continue

            # Pane label drift.
            if tmux_pane and tmux_pane in panes:
                tmux_label = panes[tmux_pane]["pane_label"]
                if row.get("pane_label") != tmux_label:
                    try:
                        workflow_events = [
                            {
                                "workflow_state": row.get("status"),
                                "event_type": "pane_label_reconciled",
                                "event_owner": "tmux_db_reconciler",
                                "details": {
                                    "previous_pane_label": row.get("pane_label"),
                                    "tmux_pane_label": tmux_label,
                                    "tmux_session_window": panes[tmux_pane].get("session_window"),
                                    "tmux_pane": tmux_pane,
                                },
                            }
                        ]
                        await sanctioned_update_instance(
                            db,
                            instance_id=row["id"],
                            updates={"pane_label": tmux_label},
                            mutation_type="reconcile_pane_label",
                            write_source="tmux_db_reconciler",
                            actor="reconciler",
                            workflow_events=workflow_events,
                        )
                        logger.info(
                            "Reconciler pane_label mutation instance=%s tmux_pane=%s old=%s new=%s session_window=%s",
                            row.get("id"),
                            tmux_pane,
                            row.get("pane_label"),
                            tmux_label,
                            panes[tmux_pane].get("session_window"),
                        )
                        counts["pane_label_drift"] += 1
                    except Exception as e:
                        logger.warning(
                            f"Reconciler pane_label mutation failed for {row.get('id')}: {e}"
                        )

            # Placeholder tab_name with attached session doc — flag, don't rename.
            if _is_placeholder_tab_name(row.get("tab_name")) and row.get("session_doc_id"):
                counts["placeholder_tab_name_drift"] += 1
                if row.get("workflow_blocked_reason") != "tab_name_placeholder":
                    try:
                        await sanctioned_update_instance(
                            db,
                            instance_id=row["id"],
                            updates={"workflow_blocked_reason": "tab_name_placeholder"},
                            mutation_type="reconcile_flag_placeholder",
                            write_source="tmux_db_reconciler",
                            actor="reconciler",
                        )
                    except Exception as e:
                        logger.warning(
                            f"Reconciler placeholder flag failed for {row.get('id')}: {e}"
                        )
                deferred_events.append(
                    (
                        "placeholder_tab_name_drift",
                        row["id"],
                        {
                            "tab_name": row.get("tab_name"),
                            "session_doc_id": row.get("session_doc_id"),
                        },
                    )
                )

            # tab_name ↔ session_doc mismatch — log only, no mutation.
            if _tab_name_session_doc_mismatch(row.get("tab_name"), row.get("session_doc_path")):
                counts["tab_name_session_doc_mismatch"] += 1
                deferred_events.append(
                    (
                        "tab_name_session_doc_mismatch",
                        row["id"],
                        {
                            "tab_name": row.get("tab_name"),
                            "derived_slug": _derive_session_doc_slug(row.get("session_doc_path")),
                            "session_doc_path": row.get("session_doc_path"),
                        },
                    )
                )

        await db.commit()

    # Emit deferred events after the reconciler's connection is closed so we
    # don't fight ourselves on the SQLite write lock.
    for event_type, instance_id, details in deferred_events:
        try:
            await log_event(event_type, instance_id=instance_id, details=details)
        except Exception as e:
            logger.warning(f"Reconciler deferred log_event {event_type} failed: {e}")

    return counts


async def tmux_db_reconciler_worker():
    """Background worker that converges ``claude_instances`` to tmux truth.

    Every ``RECONCILE_CYCLE_SECONDS`` it walks ``tmux list-panes -a``, marks
    orphan rows stopped, fixes ``pane_label`` drift, dedupes duplicate rows for
    the same ``tmux_pane`` (keeping the newest), and logs (does NOT auto-rename)
    ``Claude HH:MM`` placeholder tab_names plus tab_name↔session_doc mismatches.

    All mutations go through ``sanctioned_update_instance`` so the audit log
    records the reconciler as the agent. Telemetry: one
    ``tmux_db_reconcile_cycle`` event per cycle that found drift.
    """
    while True:
        try:
            await asyncio.sleep(RECONCILE_CYCLE_SECONDS)
            counts = await _run_tmux_db_reconcile_cycle()
            if any(counts.values()):
                await log_event("tmux_db_reconcile_cycle", details=counts)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"tmux_db_reconciler_worker error: {e}")


async def session_doc_sync_worker():
    """Process session_doc_sync_queue — update frontmatter when DB state changes.

    SQLite triggers fire on status change, tab rename, doc link, and doc unlink,
    inserting rows here. This worker deduplicates by doc_id per batch and calls
    _update_doc_agents_list() to sync the agents: list in the session doc.
    """
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, doc_id, reason FROM session_doc_sync_queue ORDER BY id"
                )
                rows = await cursor.fetchall()
                if not rows:
                    await asyncio.sleep(2)
                    continue

                # Deduplicate: only process each doc_id once per batch
                seen_docs = set()
                processed = []
                for row in rows:
                    doc_id = row["doc_id"]
                    processed.append(row["id"])
                    if doc_id not in seen_docs:
                        seen_docs.add(doc_id)
                        try:
                            await _update_doc_agents_list(db, doc_id)
                            logger.info(f"Doc sync: doc {doc_id} updated (reason: {row['reason']})")
                        except Exception as e:
                            logger.warning(f"Doc sync failed for doc {doc_id}: {e}")

                if processed:
                    ph = ",".join("?" * len(processed))
                    await db.execute(
                        f"DELETE FROM session_doc_sync_queue WHERE id IN ({ph})", processed
                    )
                    await db.commit()
        except Exception as e:
            logger.error(f"Session doc sync worker error: {e}")
        await asyncio.sleep(2)


async def clear_stale_processing_flags():
    """Background worker that clears stale processing and stops dead local pane rows."""
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """SELECT *
                       FROM claude_instances
                       WHERE status IN ('processing', 'idle')
                         AND tmux_pane IS NOT NULL
                         AND device_id = ?""",
                    (LOCAL_DEVICE_NAME,),
                )
                pane_rows = await cursor.fetchall()
                stopped_dead_panes = []
                for row in pane_rows:
                    if await _tmux_pane_exists(row["tmux_pane"]):
                        continue
                    try:
                        await sanctioned_update_instance(
                            db,
                            instance_id=row["id"],
                            updates={
                                "status": "stopped",
                                "synced": 0,
                                "stopped_at": datetime.now().isoformat(),
                            },
                            mutation_type="instance_stopped",
                            write_source="system",
                            actor="clear-dead-tmux-pane",
                        )
                        stopped_dead_panes.append(dict(row))
                    except LookupError:
                        continue

                cursor = await db.execute(
                    """SELECT *
                       FROM claude_instances
                       WHERE status = 'processing'
                         AND datetime(last_activity) < datetime('now', 'localtime', '-5 minutes')"""
                )
                rows = await cursor.fetchall()
                stale_idle_instances = []
                for row in rows:
                    try:
                        await sanctioned_update_instance(
                            db,
                            instance_id=row["id"],
                            updates={"status": "idle"},
                            mutation_type="status_changed",
                            write_source="system",
                            actor="clear-stale-processing",
                        )
                        stale_idle_instances.append(dict(row))
                    except LookupError:
                        continue
                await db.commit()

                if rows:
                    logger.warning(f"Auto-cleared {len(rows)} stale processing flags")
                if stopped_dead_panes:
                    logger.warning(
                        f"Auto-stopped {len(stopped_dead_panes)} dead tmux-pane instance rows"
                    )
                    for stopped in stopped_dead_panes:
                        if _is_assert_persona_label(stopped.get("pane_label")):
                            continue
                        spawn_tmux_assert_instance(
                            stopped.get("pane_label") or stopped.get("tmux_pane"),
                            stopped.get("id", ""),
                            "clear-dead-tmux-pane",
                        )
                    for stopped in stopped_dead_panes:
                        if stopped.get("instance_type") != "golden_throne":
                            continue
                        try:
                            await schedule_golden_throne_followup(
                                stopped, reason="clear-dead-tmux-pane"
                            )
                        except Exception as exc:
                            logger.warning(
                                "Golden Throne: failed to schedule dead-pane follow-up "
                                f"for {stopped.get('id', '')[:12]}: {exc}"
                            )
                for stale_idle in stale_idle_instances:
                    if stale_idle.get("instance_type") != "golden_throne":
                        continue
                    stale_idle["status"] = "idle"
                    try:
                        await schedule_golden_throne_followup(
                            stale_idle, reason="clear-stale-processing"
                        )
                    except Exception as exc:
                        logger.warning(
                            "Golden Throne: failed to schedule stale-processing follow-up "
                            f"for {stale_idle.get('id', '')[:12]}: {exc}"
                        )

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
                discovered_pid = (
                    await find_claude_pid_by_workdir(working_dir) if working_dir else None
                )

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


async def phone_heartbeat_worker():
    """Monitor phone heartbeat silence. Alert via Discord + Pavlok if phone goes silent."""
    await asyncio.sleep(120)  # Wait 2 min after startup before first check
    while True:
        await asyncio.sleep(300)  # Check every 5 minutes
        try:
            last = PHONE_HEARTBEAT["last_seen"]
            if last is None:
                continue  # No heartbeat ever received — skip until first ping arrives
            silence_min = (datetime.utcnow() - last).total_seconds() / 60
            current_alert = PHONE_HEARTBEAT["alert_state"]

            # Don't alert during quiet hours (11 PM - 9 AM)
            if _is_quiet_hours():
                continue

            if silence_min > 60 and current_alert != "zap":
                PHONE_HEARTBEAT["alert_state"] = "zap"
                msg = f"⚡ Phone heartbeat silent {silence_min:.0f}min — Pavlok ZAP fired. Check Tailscale."
                logger.warning(f"PHONE HB: {msg}")
                try:
                    await _run_subprocess_offloop(
                        ("discord", "send", "alerts", msg),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        timeout=15,
                    )
                except Exception as e:
                    logger.warning(f"PHONE HB: Discord alert failed: {e}")
                await asyncio.to_thread(
                    send_pavlok_stimulus, "zap", None, "phone_heartbeat_silence_60min", True
                )
                await log_event(
                    "phone_heartbeat_silence",
                    device_id="Token-S24",
                    details={"silence_min": round(silence_min), "alert": "zap"},
                )

            elif silence_min > 30 and current_alert is None:
                PHONE_HEARTBEAT["alert_state"] = "beep"
                msg = f"📵 Phone heartbeat silent {silence_min:.0f}min — Tailscale may be down."
                logger.warning(f"PHONE HB: {msg}")
                try:
                    await _run_subprocess_offloop(
                        ("discord", "send", "fallback", msg),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        timeout=15,
                    )
                except Exception as e:
                    logger.warning(f"PHONE HB: Discord alert failed: {e}")
                await asyncio.to_thread(
                    send_pavlok_stimulus, "beep", None, "phone_heartbeat_silence_30min", True
                )
                await log_event(
                    "phone_heartbeat_silence",
                    device_id="Token-S24",
                    details={"silence_min": round(silence_min), "alert": "beep"},
                )

        except Exception as e:
            logger.error(f"phone_heartbeat_worker error: {e}")


# [MOVED to shared.py / routes/tts.py] — was: def _is_quiet_hours() -> bool:

# ── Morning Enforce ─────────────────────────────────────────
# In-memory state for the current day's morning session enforce loop.
# Resets each time a new morning session fires.
MORNING_ENFORCE_STATE: dict = {
    "status": "idle",  # idle | pending | acknowledged | overridden | blocked
    "session_type": None,
    "fired_at": None,
    "acknowledged_at": None,
    "override_reason": None,
    "escalation_level": 0,  # 0 = none, 1 = TTS repeat, 2 = Discord DM, 3 = blocked
}


def _register_morning_expected_response(session_type: str = "morning_session") -> None:
    """Register an expected response and schedule escalation checks at +5/+10/+15 min."""
    MORNING_ENFORCE_STATE.update(
        {
            "status": "pending",
            "session_type": session_type,
            "fired_at": datetime.utcnow().isoformat(),
            "acknowledged_at": None,
            "override_reason": None,
            "escalation_level": 0,
        }
    )

    base = datetime.now()
    for level, offset_min in [(1, 5), (2, 10), (3, 15)]:
        fire_at = base + timedelta(minutes=offset_min)
        job_id = f"morning-enforce-l{level}"
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
        scheduler.add_job(
            _morning_escalate,
            DateTrigger(run_date=fire_at),
            args=[level],
            id=job_id,
            replace_existing=True,
            name=f"Morning Enforce L{level}",
            misfire_grace_time=120,
        )

    # Schedule instance health check at +90s — detects 401, crash, dead pane
    health_fire_at = base + timedelta(seconds=90)
    health_job_id = "morning-health-check"
    try:
        scheduler.remove_job(health_job_id)
    except Exception:
        pass
    scheduler.add_job(
        _morning_health_check,
        DateTrigger(run_date=health_fire_at),
        id=health_job_id,
        replace_existing=True,
        name="Morning Health Check",
        misfire_grace_time=120,
    )
    logger.info(
        f"Morning enforce registered: {session_type}, escalations at +5/+10/+15 min, health check at +90s"
    )


@app.post("/api/morning/enforce-register")
async def register_morning_enforce():
    """Register the morning escalation chain independently of /api/morning/start.

    Used by morning_launcher.py which handles session spawning itself but still
    needs the enforce escalation pathway (Discord DMs at +5/+10/+15 min).
    """
    _register_morning_expected_response("morning_session")
    await log_event("morning_enforce_registered", device_id="cron")
    return {
        "status": "registered",
        "escalations": [
            "+90s health-check",
            "+5min phone+TTS",
            "+10min phone+beep+Discord",
            "+15min zap+blocked+Discord",
        ],
    }


def _cancel_morning_escalations() -> None:
    """Cancel all pending escalation jobs and health check (called on acknowledge/override)."""
    for job_id in [
        "morning-enforce-l1",
        "morning-enforce-l2",
        "morning-enforce-l3",
        "morning-health-check",
    ]:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass


def _morning_health_check() -> None:
    """APScheduler callback at +90s: verify the morning Claude instance is alive.

    Checks if the instance acknowledged (meaning it booted, authenticated, and
    ran its first tool). If not, captures the tmux pane to detect 401/crash,
    sends TTS alert, and dispatches a self-heal investigation agent.
    """
    state = MORNING_ENFORCE_STATE
    if state["status"] not in ("pending",):
        logger.info(f"Morning health check: status={state['status']}, no action needed")
        return

    # Check if acknowledged_at was set by Emperor-origin Discord/API acknowledgement.
    if state.get("acknowledged_at"):
        logger.info("Morning health check: already acknowledged, healthy")
        return

    # Not acknowledged after 90s — check the pane for signs of life
    logger.warning("Morning health check: no acknowledgement after 90s, inspecting pane")

    pane_id = None
    error_signature = None
    pane_output = ""

    # Read pane_id from state file
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        state_file = Path("/tmp/custodes_morning_sessions") / f"morning_{today}.json"
        if state_file.exists():
            state_data = json.loads(state_file.read_text())
            pane_id = state_data.get("pane_id")
    except Exception as e:
        logger.warning(f"Morning health check: could not read state file: {e}")

    # Capture pane output to detect failure signatures
    if pane_id:
        try:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", pane_id, "-p"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pane_output = result.stdout
        except Exception as e:
            logger.warning(f"Morning health check: pane capture failed: {e}")
            pane_output = f"[pane capture error: {e}]"

    # Detect known failure signatures
    failure_signatures = [
        "401",
        "/login",
        "authentication_error",
        "Invalid authentication",
        "ECONNREFUSED",
    ]
    for sig in failure_signatures:
        if sig.lower() in pane_output.lower():
            error_signature = sig
            break

    if not error_signature and pane_output.strip():
        # Pane has output but no error signature — might just be slow, give benefit of doubt
        # Check if there's any sign of Claude running (prompt indicators)
        if any(
            indicator in pane_output for indicator in ["❯", "⎿", "Claude", "bypass permissions"]
        ):
            logger.info(
                "Morning health check: Claude appears running but hasn't acknowledged yet — deferring to enforce chain"
            )
            return

    # ── Failure confirmed — alert and self-heal ──
    failure_reason = error_signature or "no response and no Claude activity detected"
    logger.error(f"Morning health check FAILED: {failure_reason}")

    # 1. TTS alert
    speak_checkin_tts("Morning session launch failed. Dispatching investigation.")

    # 2. Log event
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(
                log_event(
                    "morning_health_check_failed",
                    details={
                        "reason": failure_reason,
                        "pane_id": pane_id,
                        "pane_snippet": pane_output[-500:] if pane_output else None,
                    },
                ),
                loop,
            ).result(timeout=5)
    except Exception as e:
        logger.warning(f"Morning health check: event log failed: {e}")

    # 3. Dispatch self-heal investigation agent
    try:
        diag_prompt = (
            f"Morning session health check failed at +90s. Failure reason: {failure_reason}. "
            f"Pane {pane_id or 'unknown'} output snippet: {pane_output[-300:] if pane_output else 'empty'}. "
            "Investigate the root cause (auth expiry, CLI crash, network issue, etc.), "
            "attempt to fix it if possible (e.g. kill dead pane, re-auth), "
            "and report findings to Discord #briefing channel. "
            "If the fix requires user interaction (like /login), send TTS instructions."
        )
        subprocess.Popen(
            [
                "claude",
                "--print",
                "--output-format",
                "text",
                "--model",
                "sonnet",
                "--allowedTools",
                "Bash,Read,Grep,Glob,Write",
                "-p",
                diag_prompt,
            ],
            cwd="/Volumes/Imperium/Imperium-ENV",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info("Morning health check: self-heal agent dispatched")
    except Exception as e:
        logger.error(f"Morning health check: self-heal dispatch failed: {e}")
        # Last resort: just TTS what went wrong
        speak_checkin_tts(
            f"Morning session failed: {failure_reason}. Could not dispatch self-heal agent."
        )


def _morning_escalate(level: int) -> None:
    """APScheduler callback: fire morning enforce escalation at the given level.

    Runs in APScheduler thread — bridges to async enforce via event loop.
    L1 is a pure notification; L2/L3 fire atomic shock+TTS at rising intensity.
    """
    state = MORNING_ENFORCE_STATE
    if state["status"] != "pending":
        logger.info(f"Morning escalation L{level} skipped: status={state['status']}")
        return

    state["escalation_level"] = level
    logger.warning(f"Morning enforce escalation L{level} fired")

    if level == 1:
        _notify_sync(NotifyRequest(message="Morning session pending.", type="tts"))
    elif level == 2:
        _enforce_sync(
            EnforceRequest(
                message="Morning Session unacknowledged",
                intensity=25,
                source="morning",
            )
        )
    elif level == 3:
        state["status"] = "blocked"
        logger.warning("Morning enforce L3: morning_blocked — Custodes will not proceed")
        _enforce_sync(
            EnforceRequest(
                message=(
                    "Morning Session BLOCKED (15 min elapsed). "
                    "Custodes will not proceed until you respond."
                ),
                intensity=50,
                source="morning",
            )
        )


# ── Atomic Enforce (stateless emitter) ────────────────────
# Every /api/enforce call fires Pavlok (>=25 intensity) AND a notification.
# Escalation, ack tracking, and ladder logic live in Golden Throne, not here.
# Implementation: enforce.py + notify.py.


def _enforce_sync(request: EnforceRequest) -> dict:
    """Sync wrapper for enforce() — for APScheduler callbacks."""
    try:
        if APP_LOOP and APP_LOOP.is_running():
            future = asyncio.run_coroutine_threadsafe(enforce(request), APP_LOOP)
            return future.result(timeout=20)
        return asyncio.run(enforce(request))
    except Exception as e:
        logger.warning(f"Enforce sync wrapper failed: {e}")
        return {"fired": False, "error": str(e)}


def _notify_sync(request: NotifyRequest) -> dict:
    """Sync wrapper for dispatch_notification() — for APScheduler callbacks."""
    try:
        if APP_LOOP and APP_LOOP.is_running():
            future = asyncio.run_coroutine_threadsafe(dispatch_notification(request), APP_LOOP)
            return future.result(timeout=20)
        return asyncio.run(dispatch_notification(request))
    except Exception as e:
        logger.warning(f"Notify sync wrapper failed: {e}")
        return {"delivered": False, "error": str(e)}


@app.post("/api/enforce")
async def enforce_endpoint(request: EnforceRequest):
    """Atomic stateless enforce: Pavlok shock + notification.

    Every call fires Pavlok (>=25 intensity) AND a notification. Guardrails:
    quiet-hours + in-meeting only. No warnings, no soft tiers, no cooldowns.
    Escalation/ack tracking live in Golden Throne, not here.
    """
    return await enforce(request)


@app.post("/api/enforcement/ack")
async def enforcement_ack(request: EnforcementAckRequest):
    """Acknowledge a pending expected acknowledgement."""
    if not request.ack_id and not (request.source and request.instance_id):
        raise HTTPException(status_code=400, detail="ack_id or source+instance_id is required")
    return await _resolve_expected_ack(
        ack_id=request.ack_id,
        source=request.source,
        instance_id=request.instance_id,
        status="acknowledged",
    )


@app.post("/api/enforcement/expect")
async def enforcement_expect(request: EnforcementExpectRequest):
    """Create a manual expected acknowledgement using the default enforcement ladder."""
    reason = (request.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    ack = await create_expected_ack(
        source=(request.source or "manual").strip() or "manual",
        instance_id=request.instance_id,
        reason=reason,
        details=request.details or {},
    )
    return {"created": True, "ack": ack}


@app.post("/api/enforcement/bailout")
async def enforcement_bailout(request: EnforcementBailoutRequest):
    """Manual bailout for one ack. Requires reason and disables Pavlok for that ack."""
    reason = (request.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    if not request.ack_id and not (request.source and request.instance_id):
        raise HTTPException(status_code=400, detail="ack_id or source+instance_id is required")
    return await _resolve_expected_ack(
        ack_id=request.ack_id,
        source=request.source,
        instance_id=request.instance_id,
        status="bailed_out",
        bailout_reason=reason,
    )


@app.get("/api/enforcement/status")
async def enforcement_status():
    """Return pending acknowledgements plus current escalation and Pavlok guardrail state."""
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT * FROM expected_acknowledgements
            WHERE status = 'pending'
            ORDER BY ack_due_at ASC
            """
        )
        rows = await cursor.fetchall()

    pending = []
    for row in rows:
        ack = _expected_ack_row_to_dict(row)
        ack["current_level"] = _ack_current_level(ack, now)
        ack["scheduled_jobs"] = []
        ack["policy_stages"] = list(_expected_ack_scheduled_stages(ack.get("source")))
        for level in _expected_ack_scheduled_levels(ack.get("source")):
            job = scheduler.get_job(_expected_ack_job_id(ack["id"], level))
            if job:
                ack["scheduled_jobs"].append(
                    {
                        "id": job.id,
                        "level": level,
                        "next_run_time": job.next_run_time.isoformat()
                        if job.next_run_time
                        else None,
                    }
                )
        pending.append(ack)

    def _cooldown_remaining(last_at: str | None, cooldown_seconds: int | None) -> int:
        if not last_at or not cooldown_seconds:
            return 0
        elapsed = (datetime.now() - datetime.fromisoformat(last_at)).total_seconds()
        return round(max(0, cooldown_seconds - elapsed))

    return {
        "pending": pending,
        "pending_count": len(pending),
        "pavlok": {
            "enabled": PAVLOK_CONFIG["enabled"],
            "daily_zap_cap": PAVLOK_CONFIG.get("daily_zap_cap", 6),
            "zap_count_date": PAVLOK_STATE.get("zap_count_date"),
            "zap_count": PAVLOK_STATE.get("zap_count", 0),
            "zap_cooldown_seconds": PAVLOK_CONFIG.get("zap_cooldown_seconds"),
            "soft_cooldown_seconds": PAVLOK_CONFIG.get("soft_cooldown_seconds"),
            "last_zap_at": PAVLOK_STATE.get("last_zap_at"),
            "last_soft_at": PAVLOK_STATE.get("last_soft_at"),
            "zap_cooldown_remaining_seconds": _cooldown_remaining(
                PAVLOK_STATE.get("last_zap_at"), PAVLOK_CONFIG.get("zap_cooldown_seconds")
            ),
            "soft_cooldown_remaining_seconds": _cooldown_remaining(
                PAVLOK_STATE.get("last_soft_at"), PAVLOK_CONFIG.get("soft_cooldown_seconds")
            ),
        },
    }


# ── Morning Session ────────────────────────────────────────


class MorningBriefRequest(BaseModel):
    date: str | None = None


@app.post("/api/custodes/morning-brief")
async def custodes_morning_brief(request: MorningBriefRequest | None = None):
    """Morning-session-as-compaction proxy.

    If a Custodes singleton is alive, inject a 3-step prompt (handoff blurb →
    /compact → morning brief) into its pane to preserve continuity. Otherwise
    fall back to the existing spawn path via run_morning_session().
    """
    from morning_session import (
        build_prompt,
        gather_context,
        get_daily_thread_id,
        run_morning_session,
    )

    today = request.date if request and request.date else datetime.now().strftime("%Y-%m-%d")

    # Resolve alive Custodes singleton
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT id, tmux_pane, device_id
               FROM claude_instances
               WHERE legion = 'custodes'
                 AND status IN ('idle', 'processing')
                 AND stopped_at IS NULL
               ORDER BY last_activity DESC
               LIMIT 1"""
        )
        row = await cursor.fetchone()

    target_pane: str | None = None
    target_instance: str | None = None
    if row and (row["tmux_pane"] or "").strip():
        if await _tmux_pane_exists(row["tmux_pane"]):
            target_pane = row["tmux_pane"]
            target_instance = row["id"]
    if not target_pane:
        # DB stale — try recovering from tmux directly
        recovered = await _find_custodes_tmux_pane()
        if recovered:
            target_pane = recovered

    if target_pane:
        # Build the morning brief body from the same source the standalone launcher uses
        ctx = gather_context()
        ctx["trigger"] = "alarm"
        ctx["daily_thread_id"] = get_daily_thread_id(today) or ""
        morning_brief_body = build_prompt(ctx)

        injection = (
            f"Morning session — running into existing Custodes pane.\n\n"
            f"Step 1 — write today's handoff blurb to Terra/Journal/Daily/{today}.md.\n"
            f"The blurb should cover: yesterday's open carryover (cascades, pending decisions, anything mid-flight),\n"
            f"harness regressions surfaced overnight, and any persistent state that compaction would lose.\n"
            f"Write it as a tight section (not a stream-of-consciousness dump). Keep <500 words.\n\n"
            f"Step 2 — /compact\n\n"
            f"Step 3 — morning session brief follows.\n\n"
            f"{morning_brief_body}"
        )

        delivery = await _inject_custodes_prompt_to_pane(
            injection, target_pane, instance_id=target_instance
        )
        await log_event(
            "custodes_morning_brief_dispatched",
            details={
                "mode": "inject",
                "target_pane": target_pane,
                "instance_id": target_instance,
                "date": today,
                "dispatched": delivery.get("dispatched", False),
                "reason": delivery.get("reason"),
            },
        )
        return {
            "mode": "inject",
            "target_pane": target_pane,
            "instance_id": target_instance,
            "date": today,
            "delivery": delivery,
        }

    # Fallback: no live Custodes — spawn fresh via existing synchronous path
    # without blocking the event loop.
    asyncio.create_task(asyncio.to_thread(run_morning_session))
    _register_morning_expected_response("morning_session")
    await log_event(
        "custodes_morning_brief_dispatched",
        details={"mode": "spawn", "target_pane": None, "instance_id": None, "date": today},
    )
    return {"mode": "spawn", "date": today}


@app.post("/api/morning/start")
async def start_morning_session():
    """Trigger the Custodes morning session.

    Called by the phone Morning Setup macro after alarm dismiss.
    Gathers context, spawns a Custodes Claude session with daily persistence,
    sends briefing via TTS, and enters a follow-up loop.
    """
    from morning_session import run_morning_session

    today = datetime.now().strftime("%Y-%m-%d")
    state_file = Path(f"/tmp/custodes_morning_sessions/morning_{today}.json")

    # Check if already running
    if state_file.exists():
        import json as _json

        data = _json.loads(state_file.read_text())
        if data.get("status") == "active":
            return {"status": "already_active", "session_id": data.get("session_id")}

    # Fire and forget — the synchronous launcher runs in a worker thread.
    asyncio.create_task(asyncio.to_thread(run_morning_session))

    # Register enforce expected response — escalation chain fires if unanswered
    _register_morning_expected_response("morning_session")

    await log_event("morning_session_start", device_id="phone", details={"date": today})
    return {"status": "started", "date": today}


@app.get("/api/morning/status")
async def get_morning_session_status():
    """Check current morning session state, including enforce escalation status."""
    today = datetime.now().strftime("%Y-%m-%d")
    state_file = Path(f"/tmp/custodes_morning_sessions/morning_{today}.json")
    session_state: dict = {}
    if state_file.exists():
        import json as _json

        session_state = _json.loads(state_file.read_text())
    else:
        session_state = {"status": "not_started", "date": today}

    # Merge enforce state
    session_state["enforce"] = {
        "status": MORNING_ENFORCE_STATE["status"],
        "escalation_level": MORNING_ENFORCE_STATE["escalation_level"],
        "fired_at": MORNING_ENFORCE_STATE["fired_at"],
        "acknowledged_at": MORNING_ENFORCE_STATE["acknowledged_at"],
        "override_reason": MORNING_ENFORCE_STATE["override_reason"],
        "morning_blocked": MORNING_ENFORCE_STATE["status"] == "blocked",
    }
    return session_state


@app.post("/api/morning/acknowledge")
async def acknowledge_morning_session():
    """Acknowledge the morning session — clears pending enforce state and cancels escalations."""
    state = MORNING_ENFORCE_STATE
    if state["status"] not in ("pending", "blocked"):
        return {
            "status": "noop",
            "reason": f"enforce state is '{state['status']}', nothing to acknowledge",
        }

    _cancel_morning_escalations()
    state["acknowledged_at"] = datetime.utcnow().isoformat()
    state["status"] = "acknowledged"
    logger.info("Morning session acknowledged — enforce escalations cancelled")

    await log_event(
        "morning_acknowledged",
        details={
            "escalation_level": state["escalation_level"],
            "fired_at": state["fired_at"],
        },
    )
    return {
        "status": "acknowledged",
        "escalation_level": state["escalation_level"],
        "fired_at": state["fired_at"],
        "acknowledged_at": state["acknowledged_at"],
    }


class MorningOverrideRequest(BaseModel):
    reason: str


@app.post("/api/morning/override")
async def override_morning_session(request: MorningOverrideRequest):
    """Override the morning session enforce — requires a reason. Unblocks Custodes."""
    if not request.reason or not request.reason.strip():
        raise HTTPException(status_code=400, detail="reason is required for override")

    _cancel_morning_escalations()
    MORNING_ENFORCE_STATE["status"] = "overridden"
    MORNING_ENFORCE_STATE["override_reason"] = request.reason.strip()
    MORNING_ENFORCE_STATE["acknowledged_at"] = datetime.utcnow().isoformat()
    logger.info(f"Morning session overridden: {request.reason.strip()}")

    await log_event(
        "morning_overridden",
        details={
            "reason": request.reason.strip(),
            "escalation_level": MORNING_ENFORCE_STATE["escalation_level"],
            "fired_at": MORNING_ENFORCE_STATE["fired_at"],
        },
    )
    return {
        "status": "overridden",
        "reason": request.reason.strip(),
        "escalation_level": MORNING_ENFORCE_STATE["escalation_level"],
    }


_ALARM_DISMISS_JOB_ID = "morning_alarm_dismiss"


@app.post("/api/morning/alarm-dismiss")
async def alarm_dismiss(delay_minutes: int = 0):
    """Schedule morning session after alarm dismiss.

    Called by phone when the alarm is dismissed. Schedules /api/morning/start
    after an optional delay, replacing any previously pending alarm-dismiss job.
    Eliminates the MacroDroid timer dependency.

    Args:
        delay_minutes: Seconds to wait before firing (default 0, max 120).
    """
    delay_minutes = max(0, min(delay_minutes, 120))
    now = datetime.now()
    fires_at = now + timedelta(minutes=delay_minutes)

    async def _fire_morning_start():
        import httpx

        try:
            async with httpx.AsyncClient() as client:
                await client.post("http://localhost:7777/api/morning/start", timeout=10)
        except Exception as exc:
            logger.error(f"alarm-dismiss fire failed: {exc}")

    # Cancel any existing pending job (idempotent)
    try:
        scheduler.remove_job(_ALARM_DISMISS_JOB_ID)
        logger.info("alarm-dismiss: cancelled previous pending job")
    except Exception:
        pass  # No existing job — fine

    scheduler.add_job(
        _fire_morning_start,
        DateTrigger(run_date=fires_at),
        id=_ALARM_DISMISS_JOB_ID,
        replace_existing=True,
        name="Morning Alarm Dismiss",
        misfire_grace_time=300,
    )

    logger.info(
        f"alarm-dismiss: morning session scheduled for {fires_at.isoformat()} (delay={delay_minutes}min)"
    )
    await log_event(
        "morning_alarm_dismiss",
        device_id="phone",
        details={
            "delay_minutes": delay_minutes,
            "fires_at": fires_at.isoformat(),
        },
    )

    return {
        "scheduled_at": now.isoformat(),
        "fires_at": fires_at.isoformat(),
    }


# ── Notifications ──────────────────────────────────────────

# [MOVED to shared.py / routes/tts.py] — was: @app.post("/api/notify")


# [MOVED to routes/hooks.py or shared.py] — was: # ============ Claude Code Hook Handlers =========

# ============ Stash: Cross-Machine Clipboard & File Sharing ============


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
        items.append(
            {
                "name": f.name,
                "size": stat.st_size,
                "age_seconds": int(age_secs),
                "age_human": f"{int(age_secs // 3600)}h{int((age_secs % 3600) // 60)}m"
                if age_secs >= 3600
                else f"{int(age_secs // 60)}m",
            }
        )
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


def _format_discord_injection(channel_name: str, content: str) -> str:
    """Format a Discord message for injection into a synced session."""
    # Strip Discord mention tags for cleaner injection
    clean = re.sub(r"<@&?\d+>\s*", "", content).strip()
    return f"[Emperor via Discord #{channel_name}]: {clean}"


async def _resolve_selected_tmux_pane() -> str | None:
    """Resolve the selected pane from the most recently active tmux client.

    Token-API normally runs under launchd with no TMUX environment, so bare
    `tmux display-message` can resolve the wrong client or fail with no current
    client.  Imperial Guard voice is an operator input surface: target the pane
    selected in the human's most recently active attached client.
    """
    try:
        clients = await asyncio.create_subprocess_exec(
            "tmux",
            "list-clients",
            "-F",
            "#{client_activity}\t#{client_name}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        clients_stdout, clients_stderr = await asyncio.wait_for(clients.communicate(), timeout=5)
        if clients.returncode != 0:
            logger.warning(
                "Discord active-pane injection: tmux client resolve failed: "
                f"{clients_stderr.decode().strip()[:200]}"
            )
            return None

        selected_client = None
        selected_activity = -1
        for line in clients_stdout.decode().splitlines():
            try:
                raw_activity, client_name = line.split("\t", 1)
                activity = int(raw_activity or "0")
            except ValueError:
                continue
            if client_name and activity > selected_activity:
                selected_client = client_name
                selected_activity = activity

        if not selected_client:
            logger.warning("Discord active-pane injection: no attached tmux client found")
            return None

        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "display-message",
            "-c",
            selected_client,
            "-p",
            "#{pane_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            logger.warning(
                f"Discord active-pane injection: tmux selected-pane resolve failed: {stderr.decode().strip()[:200]}"
            )
            return None
        pane = stdout.decode().strip()
        if not pane.startswith("%"):
            logger.warning(f"Discord active-pane injection: invalid selected pane {pane!r}")
            return None

        # Verify the pane still exists before writing to it.
        check = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_id}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        check_stdout, _ = await asyncio.wait_for(check.communicate(), timeout=5)
        if pane not in set(check_stdout.decode().splitlines()):
            logger.warning(f"Discord active-pane injection: selected pane {pane} is not alive")
            return None
        logger.info(f"Discord active-pane injection: selected client {selected_client} pane {pane}")
        return pane
    except TimeoutError:
        logger.warning("Discord active-pane injection: tmux selected-pane resolve timed out")
        return None
    except Exception as e:
        logger.warning(f"Discord active-pane injection: tmux selected-pane resolve error: {e}")
        return None


async def _discord_voice_error_message(bot: str, error_msg: str):
    """Send immediate Discord VC TTS feedback for a voice routing failure."""
    try:
        import functools

        import requests as _req

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            functools.partial(
                _req.post,
                f"{DISCORD_DAEMON_URL}/voice/tts",
                json={"message": error_msg, "bot": bot, "voice": "Samantha", "rate": 200},
                timeout=30,
            ),
        )
    except Exception as e:
        logger.error(f"Voice error feedback TTS failed: {e}")


async def _try_discord_active_pane_injection(message) -> bool:
    """Inject a Discord voice transcript into the pane locked for this utterance.

    The Discord daemon captures target_tmux_pane at speech start (or, failing
    that, at the silence/commit edge) so click-away during transcription does
    not retarget the final transcript.  If old daemons omit it, fall back to the
    currently selected pane for compatibility.
    """
    requested_pane = getattr(message, "target_tmux_pane", None)
    if not requested_pane:
        logger.warning(
            "Discord active-pane injection: no locked pane supplied; refusing fallback retarget"
        )
        return False

    if not requested_pane.startswith("%") or not await _tmux_pane_exists(requested_pane):
        logger.warning(
            f"Discord active-pane injection: locked pane {requested_pane!r} is invalid or dead"
        )
        return False
    pane = requested_pane
    logger.info(f"Discord active-pane injection: using locked pane {pane}")

    formatted = _format_discord_injection("imperial_guard", message.content or "")

    try:
        tmux_dictate = SCRIPTS_DIR / "cli-tools" / "bin" / "tmux-dictate"
        dictate_args = [str(tmux_dictate), "-t", pane]
        if not getattr(message, "voice_no_submit", False) and not getattr(
            message, "voice_append_submit", False
        ):
            dictate_args.append("--submit")
        if getattr(message, "voice_append_submit", False):
            # A pooled short utterance is already sitting in the prompt bar.
            # Append only the new speech, then submit the combined prompt.
            formatted = f" {message.content or ''}"
        dictate_args.append(formatted)
        proc = await asyncio.create_subprocess_exec(
            *dictate_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PATH": ":".join(
                    [
                        str(SCRIPTS_DIR / "cli-tools" / "bin"),
                        str(Path.home() / ".local" / "bin"),
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                        os.environ.get("PATH", ""),
                    ]
                ),
            },
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            if getattr(message, "voice_append_submit", False):
                enter = await asyncio.create_subprocess_exec(
                    "tmux",
                    "send-keys",
                    "-t",
                    pane,
                    "Enter",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(enter.communicate(), timeout=5)
            mode = "no-submit" if getattr(message, "voice_no_submit", False) else "submit"
            logger.info(f"Discord active-pane injection: imperial_guard → {pane} ({mode})")
            return True
        logger.warning(
            f"Discord active-pane injection failed for {pane} (rc={proc.returncode}): {stderr.decode()[:200]}"
        )
        return False
    except TimeoutError:
        logger.warning(f"Discord active-pane injection timed out for {pane}")
        return False
    except Exception as e:
        logger.warning(f"Discord active-pane injection error for {pane}: {e}")
        return False


async def _discord_voice_error(legion: str, transcript: str):
    """Send a TTS error message back through Discord voice so the operator knows they weren't heard.

    No silent failures — if voice input can't route, the operator gets immediate audio feedback.
    """
    # Determine why injection failed
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, status, tmux_pane FROM claude_instances WHERE legion = ? ORDER BY last_activity DESC LIMIT 1",
            (legion,),
        )
        row = await cursor.fetchone()

    if not row:
        reason = f"No {legion} instance is running."
    elif row[1] not in ("idle", "processing"):
        reason = f"{legion.capitalize()} instance is {row[1]}, not active."
    elif not row[2]:
        reason = f"{legion.capitalize()} instance has no terminal attached."
    else:
        reason = f"{legion.capitalize()} injection failed."

    error_msg = f"Voice not received. {reason}"
    logger.warning(f"Voice error feedback [{legion}]: {error_msg} (transcript: {transcript[:60]})")
    await _discord_voice_error_message(legion, error_msg)


async def _try_discord_injection(legion: str, message, *, require_synced: bool = False) -> bool:
    """Try to inject a Discord message into a live instance for a legion.

    Args:
        require_synced: If True, only target instances with synced=1 (Mechanicus pattern).
                        If False, target any live instance (Custodes singleton pattern).
    Returns True if injection succeeded, False if no matching instance or injection failed.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        if require_synced:
            cursor = await db.execute(
                """SELECT id, tmux_pane, device_id FROM claude_instances
                   WHERE legion = ? AND synced = 1 AND status IN ('idle', 'processing')
                   LIMIT 1""",
                (legion,),
            )
        else:
            cursor = await db.execute(
                """SELECT id, tmux_pane, device_id FROM claude_instances
                   WHERE legion = ? AND status IN ('idle', 'processing')
                   LIMIT 1""",
                (legion,),
            )
        row = await cursor.fetchone()

    instance_id = row[0] if row else None
    tmux_pane = row[1] if row else None

    # Custodes is a singleton pane.  The DB row is often stale after compaction,
    # restarts, or manual pane recovery, while tmux still has the authoritative
    # legion:custodes marker.  Recover from tmux instead of returning a false
    # target failure.
    if (not tmux_pane) and legion == "custodes" and not require_synced:
        recovered = await _find_custodes_tmux_pane()
        if recovered:
            tmux_pane = recovered
            logger.info(f"Discord injection: recovered Custodes pane {tmux_pane}")

    formatted = _format_discord_injection(message.channel_name or "dm", message.content or "")

    if not tmux_pane and legion == "custodes" and not require_synced:
        # Voice Custodes should not degrade to a target-failure readback just
        # because the singleton row is stale or currently stopped.  Delegate
        # upsert-vs-launch to the same tmuxctl owner used by state hooks.
        try:
            result = await _assert_and_send_custodes(formatted, source="Discord injection")
            if result.get("dispatched"):
                logger.info(
                    f"Discord injection: custodes → {result.get('pane')} via assert-instance legion:custodes"
                )
                return True
            logger.warning(
                f"Discord injection: assert-instance legion:custodes failed: {result.get('reason')}"
            )
            return False
        except Exception as e:
            logger.warning(f"Discord injection: assert-instance legion:custodes error: {e}")
            return False

    if not tmux_pane:
        if instance_id:
            logger.warning(f"Discord injection: {instance_id[:12]} has no tmux_pane")
        else:
            logger.warning(f"Discord injection: no live {legion} instance or recoverable pane")
        return False

    try:
        agent_cmd = SCRIPTS_DIR / "cli-tools" / "bin" / "agent-cmd"
        cmd = [str(agent_cmd)]
        if instance_id:
            cmd.extend(["--instance", instance_id])
        else:
            cmd.extend(["--pane", tmux_pane])
        cmd.append(formatted)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "PATH": ":".join(
                    [
                        str(SCRIPTS_DIR / "cli-tools" / "bin"),
                        str(Path.home() / ".local" / "bin"),
                        "/opt/homebrew/bin",
                        "/usr/local/bin",
                        os.environ.get("PATH", ""),
                    ]
                ),
            },
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode == 0:
            target = instance_id[:12] if instance_id else tmux_pane
            logger.info(f"Discord injection: {legion} → {target} (#{message.channel_name})")
            return True
        logger.warning(f"Discord injection failed (rc={proc.returncode}): {stderr.decode()[:200]}")
        return False
    except TimeoutError:
        target = instance_id[:12] if instance_id else tmux_pane
        logger.warning(f"Discord injection timed out for {target}")
        return False
    except Exception as e:
        logger.warning(f"Discord injection error: {e}")
        return False


# Dedup cache for Discord messages (daemon sometimes delivers twice)
_discord_seen_ids: set = set()
_DISCORD_DEDUP_MAX = 200

# Aspirant thread gating — prevents bot message spam without Emperor engagement.
# Key = thread_id, Value = number of allowed posts before gate closes.
# 0 = gated (no bot posts allowed). >0 = that many posts allowed.
# New threads start at 2 (implantation + trials initial posts; gene-seed goes direct).
# Emperor reply sets to 1 (one response per Emperor message).
_aspirant_thread_gates: dict[str, int] = {}
# Queued messages for gated threads — posted when gate opens.
_aspirant_gated_queue: dict[str, list[tuple[str, str]]] = {}  # thread_id -> [(message, bot)]


_VOICE_DRAFT_TITLE_PREFIX = {
    "imperial_guard": "IG🔒",
    "mechanicus": "MECH🔒",
    "custodes": "CUST🔒",
}
_discord_voice_drafts: dict[tuple[str, str], dict] = {}


def _discord_voice_draft_summary(key: tuple[str, str], state: dict) -> dict:
    bot, author_id = key
    return {
        "bot_name": bot,
        "author_id": author_id,
        "pane": state.get("pane"),
        "created_at": state.get("created_at"),
        "utterances": int(state.get("utterances") or 0),
        "pane_alive": None,
    }


async def _clear_discord_voice_draft(key: tuple[str, str]) -> dict | None:
    state = _discord_voice_drafts.pop(key, None)
    if not state:
        return None
    await _discord_voice_restore_title(state)
    return _discord_voice_draft_summary(key, state)


def _discord_voice_author_key(request: DiscordMessageRequest) -> tuple[str, str]:
    bot = (request.bot_name or "unknown").strip().lower().replace("-", "_")
    author_id = str((request.author or {}).get("id") or "unknown")
    return (bot, author_id)


def _normalize_discord_voice_command(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9\s]+", "", (text or "").lower())
    return re.sub(r"\s+", " ", normalized).strip()


def _discord_voice_parse_command(text: str) -> tuple[str | None, str]:
    normalized = _normalize_discord_voice_command(text)
    if normalized.startswith("command "):
        normalized = normalized.removeprefix("command ").strip()

    commands = {
        "ship it": "ship",
        "ship": "ship",
        "scratch that": "scratch",
        "scratch": "scratch",
        "mute": "mute",
        "unmute": "unmute",
        "retarget": "clear",
        "reset target": "clear",
        "clear target": "clear",
        "clear lock": "clear",
        "unlock": "clear",
    }
    for phrase, command in sorted(commands.items(), key=lambda item: len(item[0]), reverse=True):
        if normalized == phrase:
            return command, ""
        suffix = f" {phrase}"
        if normalized.endswith(suffix):
            # Strip the command phrase from the original text by word count. This
            # preserves punctuation/casing in the draft payload while allowing
            # suffix commands such as "do x and y ship".
            words = (text or "").strip().split()
            return command, " ".join(words[: -len(phrase.split())]).strip()
    return None, (text or "").strip()


async def _discord_voice_mute_member(bot: str, author_id: str) -> bool:
    """Ask the Discord daemon to server-mute the speaking operator if possible."""
    try:
        import functools

        import requests as _req

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            functools.partial(
                _req.post,
                f"{DISCORD_DAEMON_URL}/voice/mute",
                json={"bot": bot, "user_id": author_id, "duration_ms": 15000},
                timeout=10,
            ),
        )
        if resp.status_code == 200:
            return bool(resp.json().get("muted"))
        logger.warning(f"Voice mute failed via daemon: HTTP {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"Voice mute daemon request failed: {e}")
        return False


async def _tmux_display_value(pane: str, fmt: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "display-message",
        "-p",
        "-t",
        pane,
        fmt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace").rstrip("\n")


async def _tmux_set_pane_title(pane: str, title: str) -> None:
    await asyncio.to_thread(
        _run_tmux_focus_preserved,
        ("tmux", "select-pane", "-t", pane, "-T", title),
        source="token-api pane-title",
        attempted_target=pane,
    )


async def _discord_voice_restore_title(state: dict) -> None:
    pane = state.get("pane")
    if pane and await _tmux_pane_exists(pane):
        try:
            await _tmux_set_pane_title(pane, state.get("title") or "")
        except Exception as e:
            logger.warning(f"Voice draft: failed restoring pane title for {pane}: {e}")


async def _discord_voice_mark_title(bot: str, pane: str) -> str:
    old_title = await _tmux_display_value(pane, "#{pane_title}") or ""
    prefix = _VOICE_DRAFT_TITLE_PREFIX.get(bot, f"{bot.upper()[:4]}🔒")
    if not old_title.startswith(prefix):
        await _tmux_set_pane_title(pane, f"{prefix} {old_title}".strip())
    return old_title


async def _discord_voice_type(pane: str, text: str) -> bool:
    tmux_dictate = SCRIPTS_DIR / "cli-tools" / "bin" / "tmux-dictate"
    proc = await asyncio.create_subprocess_exec(
        str(tmux_dictate),
        "-t",
        pane,
        text,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "PATH": ":".join(
                [
                    str(SCRIPTS_DIR / "cli-tools" / "bin"),
                    str(Path.home() / ".local" / "bin"),
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    os.environ.get("PATH", ""),
                ]
            ),
        },
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        logger.warning(f"Voice draft: tmux-dictate failed for {pane}: {stderr.decode()[:200]}")
        return False
    return True


async def _discord_voice_send_key(pane: str, key: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "send-keys",
        "-t",
        pane,
        key,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
    if proc.returncode != 0:
        logger.warning(f"Voice draft: send-key {key} failed for {pane}: {stderr.decode()[:200]}")
        return False
    return True


async def _resolve_discord_voice_target(bot: str, message: DiscordMessageRequest) -> str | None:
    if bot == "imperial_guard":
        pane = message.target_tmux_pane
        if pane and pane.startswith("%") and await _tmux_pane_exists(pane):
            return pane
        logger.warning(f"Voice draft [imperial_guard]: supplied target pane invalid/dead: {pane!r}")
        return None

    require_synced = bot == "mechanicus"
    async with aiosqlite.connect(DB_PATH) as db:
        if require_synced:
            cursor = await db.execute(
                """SELECT tmux_pane FROM claude_instances
                   WHERE legion = ? AND synced = 1 AND status IN ('idle', 'processing')
                   LIMIT 1""",
                (bot,),
            )
        else:
            cursor = await db.execute(
                """SELECT tmux_pane FROM claude_instances
                   WHERE legion = ? AND status IN ('idle', 'processing')
                   LIMIT 1""",
                (bot,),
            )
        row = await cursor.fetchone()
    pane = row[0] if row else None
    if (not pane) and bot == "custodes":
        pane = await _find_custodes_tmux_pane()
    if pane and await _tmux_pane_exists(pane):
        return pane
    logger.warning(f"Voice draft [{bot}]: no live target pane")
    return None


async def _discord_voice_unmute_member(bot: str, author_id: str) -> bool:
    """Ask the Discord daemon to server-unmute the speaking operator if possible."""
    try:
        import functools

        import requests as _req

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(
            None,
            functools.partial(
                _req.post,
                f"{DISCORD_DAEMON_URL}/voice/unmute",
                json={"bot": bot, "user_id": author_id},
                timeout=10,
            ),
        )
        if resp.status_code == 200:
            return bool(resp.json().get("unmuted"))
        logger.warning(f"Voice unmute failed via daemon: HTTP {resp.status_code} {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"Voice unmute daemon request failed: {e}")
        return False


async def _handle_discord_voice_draft(request: DiscordMessageRequest) -> dict:
    key = _discord_voice_author_key(request)
    bot, author_id = key
    text = (request.content or "").strip()
    command, draft_text = _discord_voice_parse_command(text)
    state = _discord_voice_drafts.get(key)

    if state and not await _tmux_pane_exists(state.get("pane")):
        _discord_voice_drafts.pop(key, None)
        logger.warning(f"Voice draft [{bot}/{author_id}]: locked pane died; cleared draft")
        await _discord_voice_error_message(
            bot, "Voice draft cleared. The locked terminal pane is gone."
        )
        return {
            "received": True,
            "message_id": request.message_id,
            "voice": True,
            "injected": False,
            "cleared": True,
            "reason": "dead_pane",
        }

    if command == "ship":
        if not state:
            logger.info(f"Voice draft [{bot}/{author_id}]: ship with no active draft")
            await _discord_voice_error_message(bot, "No active voice draft to ship.")
            return {
                "received": True,
                "message_id": request.message_id,
                "voice": True,
                "injected": False,
                "command": "ship",
                "reason": "no_draft",
            }
        if draft_text:
            segment = draft_text if not state.get("utterances") else f" {draft_text}"
            if await _discord_voice_type(state["pane"], segment):
                state["utterances"] = int(state.get("utterances") or 0) + 1
        ok = await _discord_voice_send_key(state["pane"], "Enter")
        await _discord_voice_restore_title(state)
        _discord_voice_drafts.pop(key, None)
        return {
            "received": True,
            "message_id": request.message_id,
            "voice": True,
            "injected": ok,
            "command": "ship",
            "pane": state["pane"],
        }

    if command == "scratch":
        if not state:
            logger.info(f"Voice draft [{bot}/{author_id}]: scratch with no active draft")
            await _discord_voice_error_message(bot, "No active voice draft to scratch.")
            return {
                "received": True,
                "message_id": request.message_id,
                "voice": True,
                "injected": False,
                "command": "scratch",
                "reason": "no_draft",
            }
        ok = await _discord_voice_send_key(state["pane"], "C-c")
        await _discord_voice_restore_title(state)
        _discord_voice_drafts.pop(key, None)
        return {
            "received": True,
            "message_id": request.message_id,
            "voice": True,
            "injected": ok,
            "command": "scratch",
            "pane": state["pane"],
        }

    if command == "clear":
        if state:
            await _discord_voice_restore_title(state)
            _discord_voice_drafts.pop(key, None)
            logger.info(f"Voice draft [{bot}/{author_id}]: lock cleared by command")
            return {
                "received": True,
                "message_id": request.message_id,
                "voice": True,
                "injected": True,
                "command": "clear",
                "cleared": True,
            }
        return {
            "received": True,
            "message_id": request.message_id,
            "voice": True,
            "injected": True,
            "command": "clear",
            "cleared": False,
            "reason": "no_draft",
        }

    if command == "mute":
        if draft_text and state:
            segment = draft_text if not state.get("utterances") else f" {draft_text}"
            if await _discord_voice_type(state["pane"], segment):
                state["utterances"] = int(state.get("utterances") or 0) + 1
        muted = await _discord_voice_mute_member(bot, author_id)
        return {
            "received": True,
            "message_id": request.message_id,
            "voice": True,
            "injected": muted,
            "command": "mute",
            "muted": muted,
            "temporary": True,
            "duration_ms": 15000,
        }

    if command == "unmute":
        unmuted = await _discord_voice_unmute_member(bot, author_id)
        return {
            "received": True,
            "message_id": request.message_id,
            "voice": True,
            "injected": unmuted,
            "command": "unmute",
            "unmuted": unmuted,
        }

    text = draft_text
    if not text:
        return {
            "received": True,
            "message_id": request.message_id,
            "voice": True,
            "injected": False,
            "reason": "empty",
        }

    if not state:
        pane = await _resolve_discord_voice_target(bot, request)
        if not pane:
            if bot == "imperial_guard":
                await _discord_voice_error_message(
                    bot,
                    "Voice not received. No active tmux pane could be targeted.",
                )
            else:
                await _discord_voice_error(bot, text)
            return {
                "received": True,
                "message_id": request.message_id,
                "voice": True,
                "injected": False,
                "reason": "no_target",
            }
        if await _tmux_pane_has_pending_input(pane):
            logger.warning(
                f"Voice draft [{bot}/{author_id}]: birth blocked by typing guard on {pane}"
            )
            await _discord_voice_error_message(
                bot, "Voice draft not started. The target terminal already has pending input."
            )
            return {
                "received": True,
                "message_id": request.message_id,
                "voice": True,
                "injected": False,
                "reason": "typing_guard",
                "pane": pane,
            }
        title = await _discord_voice_mark_title(bot, pane)
        state = {"pane": pane, "title": title, "created_at": datetime.now().isoformat()}
        _discord_voice_drafts[key] = state
        logger.info(f"Voice draft [{bot}/{author_id}]: locked {pane}")

    segment = text if not state.get("utterances") else f" {text}"
    ok = await _discord_voice_type(state["pane"], segment)
    if ok:
        state["utterances"] = int(state.get("utterances") or 0) + 1
    else:
        failure_msg = (
            "Voice [imperial_guard] active-pane injection failed. Voice not received."
            if bot == "imperial_guard"
            else f"Voice [{bot}] active-pane injection failed. Voice not received."
        )
        logger.warning(f"{failure_msg} pane={state['pane']}")
        if not state.get("utterances"):
            await _discord_voice_restore_title(state)
            _discord_voice_drafts.pop(key, None)
        await _discord_voice_error_message(bot, failure_msg)
        return {
            "received": True,
            "message_id": request.message_id,
            "voice": True,
            "injected": False,
            "drafting": bool(state.get("utterances")),
            "reason": "active_pane_injection_failed",
            "pane": state["pane"],
        }
    return {
        "received": True,
        "message_id": request.message_id,
        "voice": True,
        "injected": ok,
        "drafting": True,
        "pane": state["pane"],
    }


@app.get("/api/discord/voice-drafts")
async def list_discord_voice_drafts():
    """Inspect in-memory Discord voice draft locks."""
    drafts = []
    for key, state in _discord_voice_drafts.items():
        item = _discord_voice_draft_summary(key, state)
        try:
            item["pane_alive"] = await _tmux_pane_exists(state.get("pane"))
        except Exception:
            item["pane_alive"] = False
        drafts.append(item)
    return {"count": len(drafts), "drafts": drafts}


@app.post("/api/discord/voice-drafts/clear")
async def clear_discord_voice_drafts(request: Request):
    """Clear Discord voice draft locks without restarting Token API.

    Optional JSON body:
      {"bot_name": "imperial_guard", "author_id": "..."}

    Omit filters to clear all locks.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    bot_name = (body.get("bot_name") or "").strip().lower()
    author_id = str(body.get("author_id") or "").strip()

    cleared = []
    for key in list(_discord_voice_drafts.keys()):
        bot, author = key
        if bot_name and bot != bot_name:
            continue
        if author_id and author != author_id:
            continue
        item = await _clear_discord_voice_draft(key)
        if item:
            cleared.append(item)
    logger.info(f"Voice draft: cleared {len(cleared)} lock(s) via API")
    return {"cleared": len(cleared), "drafts": cleared}


@app.post("/api/discord/message")
async def receive_discord_message(request: DiscordMessageRequest):
    """Receive a forwarded Discord message from the discord-cli daemon."""
    # Dedup: skip if we've already processed this message_id
    if request.message_id:
        if request.message_id in _discord_seen_ids:
            return {"received": True, "message_id": request.message_id, "dedup": True}
        _discord_seen_ids.add(request.message_id)
        # Evict oldest entries when cache is full
        if len(_discord_seen_ids) > _DISCORD_DEDUP_MAX:
            _discord_seen_ids.clear()

    author_name = request.author.get("username", "unknown") if request.author else "unknown"
    author_id = request.author.get("id") if request.author else None

    # --- Phone fallback: token-ping couldn't reach server, relayed via Discord ---
    # Format from token-ping: "POST phone/event {'app':'Application Launched (X)'}"
    # Skip discord_message log for fallback — downstream app event captures it
    if request.channel_name == "fallback":
        content = request.content or ""
        # Extract and replay as /phone/event
        import re

        m = re.search(r"phone/event\s+['\"{]", content)
        if m:
            body_start = content.find("{", m.start())
            if body_start >= 0:
                raw_body = content[body_start:].strip().rstrip("}") + "}"
                raw_body = raw_body.replace("'", '"')
                try:
                    import json as _json

                    body = _json.loads(raw_body)
                    app_raw = body.get("app", "")
                    if app_raw:
                        req = PhoneSystemEventRequest(app=app_raw)
                        result = await handle_phone_system_event(req)
                        logger.info(f"Fallback replayed: {app_raw} -> {result}")
                except Exception as e:
                    logger.warning(f"Fallback parse failed: {e} | raw={raw_body[:200]}")
        return {"received": True, "message_id": request.message_id, "fallback_processed": True}

    # Log non-fallback Discord messages
    await log_event(
        "discord_message",
        device_id="discord",
        details={
            "channel_id": request.channel_id,
            "channel_name": request.channel_name,
            "author_id": author_id,
            "author_name": author_name,
            "content": request.content[:500],
            "message_id": request.message_id,
            "timestamp": request.timestamp,
            "is_dm": request.is_dm,
            "is_reply": request.is_reply,
        },
    )

    logger.info(
        f"Discord [{request.channel_name or request.channel_id}] {author_name}: {request.content[:80]}"
    )

    # --- Discord response routing ---
    # Never respond to bots (loop prevention)
    if (request.author or {}).get("bot"):
        return {"received": True, "message_id": request.message_id}

    content = request.content or ""

    # --- Aspirant thread gating: Emperor replied → open gate + flush queued messages ---
    # Thread messages arrive with channel_id = thread snowflake ID
    if request.channel_id in _aspirant_thread_gates:
        _aspirant_thread_gates[request.channel_id] = 1  # Allow one bot response
        logger.info(f"Aspirant gate opened for thread {request.channel_id} (Emperor replied)")
        # Flush one queued message (gate will close again after posting)
        queued = _aspirant_gated_queue.get(request.channel_id, [])
        if queued:
            msg, bot = queued.pop(0)
            if not queued:
                _aspirant_gated_queue.pop(request.channel_id, None)
            asyncio.create_task(_post_to_aspirant_thread(request.channel_id, msg, bot=bot))
            logger.info(
                f"Flushed 1 gated message for thread {request.channel_id} ({len(queued)} remaining)"
            )

    # Trigger V: Voice transcription → route to matching legion's instance
    if request.is_voice and request.bot_name:
        voice_result = await _handle_discord_voice_draft(request)
        try:
            await log_event(
                "discord_voice_draft",
                device_id="discord",
                details={
                    "bot_name": request.bot_name,
                    "author_id": author_id,
                    "message_id": request.message_id,
                    "content": (request.content or "")[:500],
                    "target_tmux_pane": request.target_tmux_pane,
                    "result": voice_result,
                },
            )
        except Exception as e:
            logger.warning(f"Voice draft audit log failed: {e}")
        return voice_result

    # Trigger 0: bare URL in #forge → auto-clip to vault
    stripped = content.strip()
    if request.channel_name == "forge" and stripped.startswith("http") and " " not in stripped:
        asyncio.create_task(_discord_clip(stripped, request))
        return {"received": True, "message_id": request.message_id}

    # Morning ack shortcut — fastest possible escalation cancel via Discord
    if MORNING_ENFORCE_STATE.get("status") == "pending":
        lower = content.lower().strip()
        if any(kw in lower for kw in ("ack", "acknowledged", "acknowledge", "here", "awake")):
            await acknowledge_morning_session()
            logger.info(f"Morning ack via Discord from {author_name}")
            # Still fall through to inject into synced session if applicable

    # Trigger 1: @Mechanicus mention → try synced injection, fallback to one-off responder
    if f"<@{MECHANICUS_USER_ID}>" in content or f"<@&{MECHANICUS_ROLE_ID}>" in content:
        injected = await _try_discord_injection("mechanicus", request, require_synced=True)
        if not injected:
            asyncio.create_task(_discord_respond(request, bot="mechanicus"))
        return {"received": True, "message_id": request.message_id}

    # Trigger 1.5: @Custodes mention → route to singleton (no one-off responder)
    if f"<@{CUSTODES_USER_ID}>" in content:
        injected = await _try_discord_injection("custodes", request)
        if not injected:
            logger.warning("Custodes @mention but no live Custodes instance to route to")
        return {"received": True, "message_id": request.message_id}

    # Trigger 1.6: @Inquisition mention — no synced support, always one-off responder
    if f"<@{INQUISITION_USER_ID}>" in content:
        asyncio.create_task(_discord_respond(request, bot="inquisition"))
        return {"received": True, "message_id": request.message_id}

    # Trigger 2: Reply in a Custodes-owned channel → route to singleton (no one-off responder)
    if request.is_reply and request.channel_name in CUSTODES_CHANNELS:
        injected = await _try_discord_injection("custodes", request)
        if not injected:
            logger.warning(f"Reply in #{request.channel_name} but no live Custodes instance")

    return {"received": True, "message_id": request.message_id}


async def _discord_clip(url: str, message: DiscordMessageRequest):
    """Run clip CLI on a bare URL dropped in #forge, reply with the vault path."""
    channel = message.channel_name or "forge"
    reply_to = message.message_id or ""
    logger.info(f"Discord clip: {url}")

    clip_bin = SCRIPTS_DIR / "cli-tools" / "bin" / "clip"
    env = {
        **os.environ,
        "PATH": ":".join(
            [
                str(SCRIPTS_DIR / "cli-tools" / "bin"),
                str(Path.home() / ".local" / "bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                os.environ.get("PATH", ""),
            ]
        ),
    }

    try:
        proc = await asyncio.create_subprocess_exec(
            str(clip_bin),
            url,
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
            # Parse "Saved to Imperium-ENV: Aspirants/slug.md" and "Title: ..." from stderr
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

    except TimeoutError:
        logger.warning(f"Discord clip timed out: {url}")
        reply_content = f"Clip timed out for <{url}>"
    except Exception as e:
        logger.warning(f"Discord clip error: {e}")
        return

    # Send reply via daemon
    try:
        import urllib.request as _urllib_req

        payload = json.dumps(
            {
                "channel": channel,
                "bot": "mechanicus",
                "content": reply_content,
                "reply_to": reply_to,
            }
        ).encode()
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

    if bot == "mechanicus":
        persona = "Fabricator General (Adeptus Mechanicus)"
    elif bot == "inquisition":
        persona = "Inquisitor (Inquisition)"
    else:
        persona = "Adeptus Custodes"

    # Model selection: Custodes gets Sonnet, others get Haiku
    model = "claude-sonnet-4-6" if bot == "custodes" else "claude-haiku-4-5-20251001"

    author_display = (message.author or {}).get(
        "displayName", (message.author or {}).get("username", "user")
    )

    if bot == "custodes":
        # Custodes: fetch full day's conversation, daily note, and habits
        context_str = ""
        try:
            today_midnight = (
                datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            )

            def _fetch_custodes_context():
                import urllib.request as _ur

                req = _ur.Request(
                    f"{DISCORD_DAEMON_URL}/read?channel={channel}&limit=100&since={today_midnight}",
                    method="GET",
                )
                with _ur.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read())

            data = await asyncio.get_event_loop().run_in_executor(None, _fetch_custodes_context)
            msgs = data.get("messages", [])
            context_str = "\n".join(
                f"[{m['author'].get('displayName', m['author'].get('username', '?'))}]: {m['content']}"
                for m in msgs
            )
        except Exception as e:
            logger.warning(f"Discord responder (custodes): could not fetch context: {e}")

        # Read today's daily note
        daily_note_content = "(Daily note not created yet)"
        today_str = datetime.now().strftime("%Y-%m-%d")
        daily_note_path = DAILY_NOTE_DIR / f"{today_str}.md"
        try:
            if daily_note_path.exists():
                daily_note_content = daily_note_path.read_text()
        except Exception as e:
            logger.warning(f"Discord responder (custodes): could not read daily note: {e}")

        # Fetch habits from internal API
        habits_str = "(Could not read habit state)"
        try:

            def _fetch_habits():
                import urllib.request as _ur

                req = _ur.Request("http://127.0.0.1:7777/api/habits/today", method="GET")
                with _ur.urlopen(req, timeout=5) as resp:
                    return resp.read().decode()

            habits_str = await asyncio.get_event_loop().run_in_executor(None, _fetch_habits)
        except Exception as e:
            logger.warning(f"Discord responder (custodes): could not fetch habits: {e}")

        # Fetch timer state
        timer_str = "(Could not read timer state)"
        try:

            def _fetch_timer():
                import urllib.request as _ur

                req = _ur.Request("http://127.0.0.1:7777/api/timer", method="GET")
                with _ur.urlopen(req, timeout=5) as resp:
                    return resp.read().decode()

            timer_str = await asyncio.get_event_loop().run_in_executor(None, _fetch_timer)
        except Exception as e:
            logger.warning(f"Discord responder (custodes): could not fetch timer: {e}")

        # Load Custodes responder prompt template
        custodes_prompt_path = Path.home() / ".claude" / "prompts" / "custodes-responder.md"
        try:
            custodes_template = custodes_prompt_path.read_text()
        except Exception as e:
            logger.warning(f"Discord responder (custodes): could not read prompt template: {e}")
            custodes_template = "You are the Adeptus Custodes. Respond helpfully."

        system_prompt = f"""{custodes_template}

---

## Injected Context

### Today's Date
{today_str}

### Daily Note (`Terra/Journal/Daily/{today_str}.md`)
{daily_note_content}

### Habit State (from /api/habits/today)
{habits_str}

### Timer State (from /api/timer)
{timer_str}

### Conversation in #{channel} (today, oldest to newest)
{context_str or "(no prior messages today)"}

### Current Message (replying to)
[{author_display}]: {message.content}"""

    else:
        # Non-Custodes bots: existing 10-message fetch
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

        system_prompt = f"""You are {persona}, responding to a Discord message in #{channel}.

Recent conversation (oldest to newest):
{context_str or "(no prior context)"}

You are replying directly to:
[{author_display}]: {message.content}

Rules:
- Be concise. Discord markdown is supported.
- Stay in character.
- Do not start with a greeting or preamble.
- One reply only."""

    # Write system prompt to temp file (avoids shell escaping issues)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(system_prompt)
        prompt_file = f.name

    responder = Path(__file__).parent / "discord_responder.py"
    env = {
        **os.environ,
        "CLAUDECODE": "",
        "TOKEN_API_SUBAGENT": f"discord_responder:{bot}",
        "PATH": ":".join(
            [
                str(SCRIPTS_DIR / "cli-tools" / "bin"),
                str(Path.home() / ".local" / "bin"),
                "/opt/homebrew/bin",
                "/usr/local/bin",
                os.environ.get("PATH", ""),
            ]
        ),
    }

    proc = await asyncio.create_subprocess_exec(
        "python3",
        str(responder),
        channel,
        message.message_id or "",
        bot,
        prompt_file,
        model,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    logger.info(
        f"Discord responder spawned: bot={bot} model={model} channel=#{channel} pid={proc.pid}"
    )

    # Fire-and-forget but log errors
    async def _wait_and_log():
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"Discord responder exited {proc.returncode}: {stderr.decode()[:300]}")

    asyncio.create_task(_wait_and_log())


# ============ Aspirant Pipeline (Inbox) ============


def _safe_filename_slug(value: str, fallback: str = "untitled") -> str:
    # Backcompat name for callers in this file.  Session docs now use
    # readable, Obsidian Sync-safe stems rather than lowercase date slugs.
    return human_filename_stem(value, fallback=fallback, max_len=80)


def _resolve_aspirant_note_path(note_path: str) -> tuple[Path, str]:
    """Return (absolute_file_path, vault_relative_path) for an aspirant note."""
    raw = Path(note_path)
    full_path = raw if raw.is_absolute() else OBSIDIAN_VAULT_PATH / note_path
    try:
        relative = str(full_path.relative_to(OBSIDIAN_VAULT_PATH))
    except ValueError:
        relative = note_path
    return full_path, relative


def _extract_gene_seed_from_content(content: str) -> str:
    """Extract the > [!dna] Gene-Seed callout, falling back to body text."""
    lines = content.splitlines()
    gene_seed_lines: list[str] = []
    in_gene_seed = False
    for line in lines:
        if "> [!dna]" in line and "Gene-Seed" in line:
            in_gene_seed = True
            continue
        if not in_gene_seed:
            continue
        if line.startswith("> "):
            gene_seed_lines.append(line[2:])
        elif line.strip() == ">":
            gene_seed_lines.append("")
        else:
            break

    gene_seed = "\n".join(gene_seed_lines).strip()
    if gene_seed:
        return gene_seed

    # Fallback: strip frontmatter and headings from simple manually-created notes.
    if content.startswith("---"):
        end_fm = content.find("\n---", 3)
        if end_fm != -1:
            content = content[end_fm + 4 :].lstrip()
    return content.strip()


def _build_aspirant_system_prompt(note_path: str) -> str:
    return f"""You are a full aspirant implantation/trials session, not a one-shot summarizer.

Operational contract:
- On startup, load vault context from your linked session document. If the vault-mind skill is available, invoke/use it; otherwise read the linked session doc directly.
- Read the aspirant note at `{note_path}` before acting.
- Treat the Gene-Seed section as authoritative intent. Preserve it; do not rewrite it away or let later context override it.
- Use Obsidian context actively: `obsidian vault=Imperium-ENV read`, `search`, `search:context`, and `backlinks` as needed.
- Perform concrete implantation/trials work: find related vault notes, identify useful research/context, challenge assumptions, and write the useful output back to the aspirant note.
- Append your work to the aspirant note under clear `## Implantation` / `## Trials` sections or a concise continuation if those sections already exist.
- Ask for Emperor direction only when the Gene-Seed is genuinely ambiguous or blocked by a decision only the user can make.
- Keep changes surgical and auditable. Do not deploy/promote the note unless explicitly instructed."""


def _build_aspirant_initial_prompt(
    *,
    title: str,
    note_path: str,
    note_type: str,
    source: str,
    thread_id: str | None,
    session_doc_path: Path,
    gene_seed: str,
) -> str:
    thread_line = (
        f"- Source/thread id: {source} / {thread_id}" if thread_id else f"- Source: {source}"
    )
    return f"""# Aspirant Session Launch

You are being launched as a managed legion aspirant session.

## Metadata
- Title: {title}
- Aspirant note: `{note_path}`
- Note type: {note_type}
{thread_line}
- Linked session doc: `{session_doc_path}`

## Gene-Seed (authoritative)
```markdown
{gene_seed or "(empty)"}
```

## First actions
1. Read the linked session doc.
2. Read the aspirant note at `{note_path}`.
3. Load relevant vault context with Obsidian search/read/backlinks.
4. Begin implantation/trials work and append concrete results back to the aspirant note.

Do not use the old automatic MiniMax/Sonnet pipeline. This is a full managed session."""


async def _create_aspirant_session_doc(
    *, title: str, note_path: str, note_type: str, source: str, launch_id: str, gene_seed: str
) -> tuple[int, Path]:
    sessions_dir = OBSIDIAN_VAULT_PATH / "Terra" / "Sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    session_doc_path = unique_human_path(sessions_dir, f"Aspirant - {title}", fallback="Aspirant")

    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
               VALUES (?, ?, ?, 'active', ?, ?)""",
            (f"Aspirant: {title}", str(session_doc_path), "aspirants", now, now),
        )
        doc_id = cursor.lastrowid
        await db.commit()

    content = f"""---
session_doc_id: {doc_id}
created: {today}
agents: []
instance_ids: []
status: active
type: session
project: aspirants
aspirant_note: "{note_path}"
aspirant_launch_id: "{launch_id}"
aspirant_type: "{note_type}"
aspirant_source: "{source}"
victory: pending
victory_conditions:
  - Read the aspirant note and preserve the gene-seed as authoritative intent.
  - Append concrete implantation/trials work back to the aspirant note.
---

# Session: Aspirant — {title}

## Aspirant

- Note: [[{note_path.replace(".md", "")}]]
- Type: `{note_type}`
- Source: `{source}`
- Launch ID: `{launch_id}`

## Gene-Seed

```markdown
{gene_seed or "(empty)"}
```

## Plan

1. Read this session doc.
2. Read the aspirant note.
3. Gather vault context with Obsidian search/read/backlinks.
4. Append implantation/trials output to the aspirant note.

## Activity Log

"""
    session_doc_path.write_text(content, encoding="utf-8")
    return doc_id, session_doc_path


async def _write_temp_text_file(prefix: str, content: str) -> str:
    def _write() -> str:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=prefix, suffix=".md", delete=False
        ) as f:
            f.write(content)
            return f.name

    return await asyncio.to_thread(_write)


async def _set_aspirant_properties(note_file: Path, updates: dict[str, str]) -> None:
    await asyncio.to_thread(update_frontmatter, note_file, updates)


async def launch_aspirant_session(
    *, note_path: str, title: str, note_type: str, source: str
) -> dict:
    """Launch a managed Claude legion session for an aspirant note, idempotently."""
    note_file, vault_note_path = _resolve_aspirant_note_path(note_path)
    if not note_file.exists():
        raise HTTPException(status_code=404, detail=f"Aspirant note not found: {note_path}")

    fm, _body = await asyncio.to_thread(read_frontmatter, note_file)
    existing_launch_id = str(fm.get("aspirant_launch_id") or "").strip()
    existing_status = str(fm.get("aspirant_session_status") or "").strip()
    if existing_launch_id and existing_status in {"launching", "launched"}:
        return {
            "launched": False,
            "duplicate": True,
            "path": vault_note_path,
            "aspirant_launch_id": existing_launch_id,
            "aspirant_session_status": existing_status,
            "session_doc": fm.get("aspirant_session_doc"),
        }

    raw_content = await asyncio.to_thread(note_file.read_text, encoding="utf-8")
    gene_seed = _extract_gene_seed_from_content(raw_content)
    thread_id = str(fm.get("thread_id") or "").strip() or None
    launch_id = str(uuid.uuid4())

    await _set_aspirant_properties(
        note_file,
        {
            "aspirant_launch_id": launch_id,
            "aspirant_session_status": "launching",
            "aspirant_launcher": "dispatch",
            "aspirant_dispatch_target": "legion:new",
            "aspirant_session_started_at": datetime.now().isoformat(),
        },
    )

    try:
        session_doc_id, session_doc_path = await _create_aspirant_session_doc(
            title=title,
            note_path=vault_note_path,
            note_type=note_type,
            source=source,
            launch_id=launch_id,
            gene_seed=gene_seed,
        )
        await _set_aspirant_properties(
            note_file,
            {
                "aspirant_session_doc_id": str(session_doc_id),
                "aspirant_session_doc": str(session_doc_path),
            },
        )

        system_prompt = _build_aspirant_system_prompt(vault_note_path)
        initial_prompt = _build_aspirant_initial_prompt(
            title=title,
            note_path=vault_note_path,
            note_type=note_type,
            source=source,
            thread_id=thread_id,
            session_doc_path=session_doc_path,
            gene_seed=gene_seed,
        )
        system_prompt_file = await _write_temp_text_file("aspirant-system-", system_prompt)
        prompt_file = await _write_temp_text_file("aspirant-prompt-", initial_prompt)

        dispatch_bin = SCRIPTS_DIR / "cli-tools" / "bin" / "dispatch"
        env = {
            **os.environ,
            "TOKEN_API_WRAPPER_LAUNCH_ID": launch_id,
            "PATH": ":".join(
                [
                    str(SCRIPTS_DIR / "cli-tools" / "bin"),
                    str(Path.home() / ".local" / "bin"),
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    os.environ.get("PATH", ""),
                ]
            ),
        }
        proc = await asyncio.create_subprocess_exec(
            str(dispatch_bin),
            "--target",
            "legion:new",
            "--dir",
            str(OBSIDIAN_VAULT_PATH),
            "--session-doc",
            str(session_doc_path),
            "--system-prompt-file",
            system_prompt_file,
            "--prompt-file",
            prompt_file,
            "--gt",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            err_text = (
                stderr.decode("utf-8", errors="replace").strip()
                or stdout.decode("utf-8", errors="replace").strip()
            )
            raise RuntimeError(f"dispatch failed ({proc.returncode}): {err_text}")

        await _set_aspirant_properties(
            note_file,
            {
                "aspirant_session_status": "launched",
                "aspirant_session_launched_at": datetime.now().isoformat(),
            },
        )
        await log_event(
            "aspirant_session_launched",
            device_id="obsidian",
            details={
                "path": vault_note_path,
                "title": title,
                "type": note_type,
                "source": source,
                "launch_id": launch_id,
                "session_doc_id": session_doc_id,
                "session_doc_path": str(session_doc_path),
                "dispatch_target": "legion:new",
            },
        )
        return {
            "launched": True,
            "duplicate": False,
            "path": vault_note_path,
            "aspirant_launch_id": launch_id,
            "aspirant_session_status": "launched",
            "session_doc_id": session_doc_id,
            "session_doc": str(session_doc_path),
        }
    except Exception as e:
        error_text = str(e)
        logger.error(f"Aspirant launch failed for '{title}' ({vault_note_path}): {error_text}")
        await _set_aspirant_properties(
            note_file,
            {
                "aspirant_session_status": "failed",
                "aspirant_session_failed_at": datetime.now().isoformat(),
                "aspirant_launch_error": error_text[:500],
            },
        )
        await log_event(
            "aspirant_session_launch_failed",
            device_id="obsidian",
            details={
                "path": vault_note_path,
                "title": title,
                "type": note_type,
                "source": source,
                "launch_id": launch_id,
                "error": error_text[:500],
            },
        )
        raise HTTPException(status_code=500, detail=error_text)


@app.post("/api/inbox/notify")
async def inbox_notify(request: InboxNotifyRequest):
    """Gene-seed: receive birth notification for a new inbox note."""
    await log_event(
        "inbox_notify",
        device_id="obsidian",
        details={
            "path": request.path,
            "title": request.title,
            "type": request.type,
            "source": request.source,
        },
    )
    logger.info(f"Inbox: new {request.type} note '{request.title}' from {request.source}")
    launch_result = await launch_aspirant_session(
        note_path=request.path,
        title=request.title,
        note_type=request.type,
        source=request.source,
    )
    return {
        "received": True,
        "path": request.path,
        "type": request.type,
        "aspirant_session": launch_result,
    }


@app.post("/api/inbox/create")
async def inbox_create(request: InboxCreateRequest):
    """Create a new aspirant note in Aspirants/ from an external source."""
    # Sanitize title for filename
    safe_title = re.sub(r"[^\w\s-]", "", request.title).strip()
    safe_title = re.sub(r"\s+", " ", safe_title)
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

    raw_body = request.content or ""
    source_line = (
        f"*Captured from {request.author} via {request.source}*\n\n" if request.author else ""
    )

    # Wrap gene-seed content in a callout so pipeline stages can distinguish
    # the Emperor's original intent from pipeline-appended content
    gene_seed_lines = raw_body.strip().splitlines()
    callout_body = "\n> ".join(gene_seed_lines) if gene_seed_lines else "(empty)"
    body = f"{source_line}> [!dna] Gene-Seed\n> {callout_body}\n"

    content = "\n".join(frontmatter_lines) + "\n\n" + body + "\n"
    filepath.write_text(content, encoding="utf-8")

    note_path = f"Aspirants/{filename}"

    # Create a thread in #aspirants for this note's pipeline lifecycle
    thread_id = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                "http://127.0.0.1:7779/thread/create",
                json={
                    "channel": "aspirants",
                    "name": safe_title[:100],
                    "bot": "custodes",
                },
            )
            if resp.status_code == 200:
                thread_data = resp.json()
                thread_id = thread_data.get("thread_id")
                # Register thread in gating registry — initial pipeline gets a free pass
                # Allow 2 posts: implantation summary + trials initiation
                # (gene-seed post goes direct, not through _post_to_aspirant_thread)
                _aspirant_thread_gates[thread_id] = 2
                logger.info(f"Inbox: created aspirant thread '{safe_title}' -> {thread_id}")

                # Store thread_id in note frontmatter
                await asyncio.create_subprocess_exec(
                    OBSIDIAN_CLI,
                    "vault=Imperium-ENV",
                    "property:set",
                    f"path={note_path}",
                    "property=thread_id",
                    f"value={thread_id}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                # Post gene-seed to the thread
                gene_seed_msg = (
                    f"**🧬 New Aspirant: {safe_title}**\n"
                    f"Type: `{request.type}` | Source: `{request.source}`\n\n"
                    f"> {raw_body.strip()[:1500]}"
                )
                await client.post(
                    "http://127.0.0.1:7779/send",
                    json={
                        "channel": "aspirants",
                        "thread_id": thread_id,
                        "content": gene_seed_msg,
                        "bot": "custodes",
                    },
                )
            else:
                logger.warning(
                    f"Inbox: thread creation failed for '{safe_title}': {resp.status_code}"
                )
    except Exception as e:
        logger.warning(f"Inbox: thread creation failed for '{safe_title}': {e}")

    launch_result = await launch_aspirant_session(
        note_path=note_path,
        title=safe_title,
        note_type=request.type,
        source=request.source,
    )

    obsidian_uri = f"obsidian://open?vault=Imperium-ENV&file={note_path.replace(' ', '%20')}"

    logger.info(f"Inbox: created '{filename}' from {request.source}")
    return {
        "created": True,
        "path": note_path,
        "title": safe_title,
        "obsidian_uri": obsidian_uri,
        "thread_id": thread_id,
        "aspirant_session": launch_result,
    }


# ---- Stage 2: Implantation ----

OBSIDIAN_CLI = str(SCRIPTS_DIR / "cli-tools" / "bin" / "obsidian")
WEB_SEARCH_CLI = str(SCRIPTS_DIR / "cli-tools" / "bin" / "web-search")
DISCORD_CLI = str(SCRIPTS_DIR / "cli-tools" / "bin" / "discord")


async def _read_note_property(note_path: str, prop: str) -> str | None:
    """Read a single frontmatter property from a note via obsidian CLI."""
    try:
        proc = await asyncio.create_subprocess_exec(
            OBSIDIAN_CLI,
            "vault=Imperium-ENV",
            "property:read",
            f"path={note_path}",
            f"property={prop}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        val = stdout.decode("utf-8", errors="replace").strip()
        return val if val and val != "null" and val != "undefined" else None
    except Exception:
        return None


async def _post_to_aspirant_thread(
    thread_id: str, message: str, bot: str = "custodes", bypass_gate: bool = False
) -> None:
    """Post a message to an aspirant's thread in #aspirants.

    Respects thread gating: if gate is closed, queues the message instead.
    Gate closes after each successful post (prevents consecutive bot messages).
    Use bypass_gate=True for initial pipeline messages (gene-seed notification).
    """
    allowed = _aspirant_thread_gates.get(thread_id, 0)
    if not bypass_gate and allowed <= 0:
        # Gate closed — queue the message
        _aspirant_gated_queue.setdefault(thread_id, []).append((message, bot))
        logger.info(
            f"Aspirant thread {thread_id} gated — queued message ({len(_aspirant_gated_queue[thread_id])} pending)"
        )
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{DISCORD_DAEMON_URL}/send",
                json={
                    "channel": "aspirants",
                    "thread_id": thread_id,
                    "content": message[:2000],
                    "bot": bot,
                },
            )
        # Decrement allowance (0 = gated until Emperor replies)
        if not bypass_gate:
            _aspirant_thread_gates[thread_id] = max(0, allowed - 1)
    except Exception as e:
        logger.warning(f"Failed to post to aspirant thread {thread_id}: {e}")


CODEX_CONTEXT = """The Imperium of Claude is an agent orchestration system using Warhammer 40K hierarchy:
- The Emperor: Token (human) — final authority
- Adeptus Custodes: Claude Opus — strategic architecture, rare invocation
- Inquisitor: Claude Sonnet — orchestration, medium/large tasks
- Adeptus Mechanicus: Claude Sonnet (dedicated) — dev/tooling autonomy
- Imperial Guard: MiniMax M2.5 — bulk work, volume tasks, disposable

MiniMax is for mass, not precision. 300 prompts per 5 hours budget. Notes enter through Aspirants/ and get enriched before promotion to Terra/Ultramar/ (authoritative notes).

The system uses an Obsidian vault (Imperium-ENV) as its knowledge base. Terra/ is personal domain, Mars/ is mechanicus/agent operations."""


async def run_implantation(
    note_path: str,
    title: str,
    note_type: str,
    source: str,
    skip_trials: bool = False,
    emperor_feedback: str = "",
) -> None:
    """Stage 2: Implantation — async enrichment of an inbox note with vault matches and research swarm."""
    try:
        # Sisyphus guard — track implantation count, cap at 2 re-implantations
        MAX_IMPLANT_CYCLES = 3  # initial + 2 retries
        implant_count_str = await _read_note_property(note_path, "implant_count") or "0"
        try:
            implant_count = int(implant_count_str)
        except ValueError:
            implant_count = 0

        if implant_count >= MAX_IMPLANT_CYCLES:
            logger.warning(
                f"Implantation halted for '{title}': max cycles reached ({implant_count}/{MAX_IMPLANT_CYCLES}). Holding for manual intervention."
            )
            # Set status to held so it doesn't re-enter the loop
            try:
                proc = await asyncio.create_subprocess_exec(
                    OBSIDIAN_CLI,
                    "vault=Imperium-ENV",
                    "property:set",
                    f"path={note_path}",
                    "property=status",
                    "value=held",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=15)
            except Exception:
                pass
            # Notify in thread
            thread_id = await _read_note_property(note_path, "thread_id")
            if thread_id:
                await _post_to_aspirant_thread(
                    thread_id,
                    f"**⏸️ Implantation Halted**\nMax cycles reached ({implant_count}). Status set to `held` — needs manual intervention or Emperor direction.",
                )
            return

        # Increment implant count
        implant_count += 1
        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "property:set",
                f"path={note_path}",
                "property=implant_count",
                f"value={implant_count}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
        except Exception:
            pass

        # Budget check — swarm costs ~14-18 queries per note
        if minimax_limiter.remaining < 25:
            logger.warning(
                f"Implantation skipped for '{title}': MiniMax budget low ({minimax_limiter.remaining} remaining)"
            )
            return

        sections = []

        # --- Phase 0: Read gene-seed content from the aspirant note ---
        gene_seed_content = ""
        try:
            note_file = OBSIDIAN_VAULT_PATH / note_path
            if note_file.exists():
                raw = note_file.read_text(encoding="utf-8")
                # Extract gene-seed from the callout block
                in_geneseed = False
                gs_lines = []
                for line in raw.splitlines():
                    if "> [!dna]" in line:
                        in_geneseed = True
                        continue
                    if in_geneseed:
                        if line.startswith("> "):
                            gs_lines.append(line[2:])
                        elif line.strip() == ">":
                            gs_lines.append("")
                        else:
                            break
                gene_seed_content = "\n".join(gs_lines).strip()
        except Exception as e:
            logger.warning(f"Implantation: could not read gene-seed from '{title}': {e}")

        # --- Phase 1a: Similar note search (vault) — enriched with full note reads ---
        vault_synthesis = None
        raw_vault_lines = []
        vault_context_str = ""
        vault_context_full = ""  # enriched: full content of top matching notes
        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "search:context",
                f"query={title}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            vault_output = stdout.decode("utf-8", errors="replace").strip()

            if vault_output:
                # Filter to active vault domains (exclude STC/ legacy content)
                active_prefixes = ("Terra/", "Mars/", "Warp/", "Aspirants/")
                for line in vault_output.splitlines():
                    if any(line.startswith(p) for p in active_prefixes):
                        raw_vault_lines.append(line)

                if raw_vault_lines:
                    vault_context_str = "\n".join(raw_vault_lines[:50])  # cap context size

                    # Enrichment: read full content of top matching notes
                    matched_paths = []
                    seen_paths = set()
                    for line in raw_vault_lines:
                        fpath = line.split(":")[0] if ":" in line else line.strip()
                        if fpath and fpath not in seen_paths and fpath.endswith(".md"):
                            seen_paths.add(fpath)
                            matched_paths.append(fpath)
                    matched_paths = matched_paths[:5]  # top 5 matches

                    full_note_contents = []
                    total_chars = 0
                    MAX_VAULT_CONTEXT = 6000  # char budget for full note reads
                    for mpath in matched_paths:
                        if total_chars >= MAX_VAULT_CONTEXT:
                            break
                        try:
                            mfile = OBSIDIAN_VAULT_PATH / mpath
                            if mfile.exists():
                                mcontent = mfile.read_text(encoding="utf-8")
                                # Strip frontmatter for context
                                if mcontent.startswith("---"):
                                    end_fm = mcontent.find("---", 3)
                                    if end_fm != -1:
                                        mcontent = mcontent[end_fm + 3 :].strip()
                                # Cap per-note at 1500 chars
                                mcontent = mcontent[:1500]
                                full_note_contents.append(f"### [[{mpath}]]\n{mcontent}")
                                total_chars += len(mcontent)
                        except Exception:
                            pass

                    if full_note_contents:
                        vault_context_full = "\n\n---\n\n".join(full_note_contents)

                    vault_synthesis = await minimax_chat(
                        system_prompt=IMPLANTATION_ROLES["vault_relevance"]["system"],
                        user_content=f"New note title: {title}\nNote type: {note_type}\n\nVault search results:\n{vault_context_str}",
                        max_tokens=IMPLANTATION_ROLES["vault_relevance"]["max_tokens"],
                    )
        except TimeoutError:
            logger.warning(f"Implantation: vault search timed out for '{title}'")
        except Exception as e:
            logger.warning(f"Implantation: vault search failed for '{title}': {e}")

        # Build Similar Notes section
        if vault_synthesis and vault_synthesis.strip() and vault_synthesis.strip() != "NO_MATCHES":
            sections.append(f"### Similar Notes\n\n{vault_synthesis.strip()}")
        elif raw_vault_lines:
            # Fallback: raw wikilinks from matched files
            fallback_links = []
            seen = set()
            for line in raw_vault_lines:
                fname = line.split(":")[0] if ":" in line else line
                if fname not in seen:
                    seen.add(fname)
                    # Strip domain prefix for wikilink (Terra/Ultramar/Foo.md → Foo)
                    note_name = fname.replace(".md", "")
                    for prefix in (
                        "Terra/Ultramar/",
                        "Terra/Meta/",
                        "Terra/Sessions/",
                        "Mars/Tasks/",
                        "Mars/Sessions/",
                        "Mars/Fleet/",
                        "Aspirants/",
                    ):
                        if note_name.startswith(prefix):
                            note_name = note_name[len(prefix) :]
                            break
                    fallback_links.append(f"- [[{note_name}]]")
            if fallback_links:
                sections.append(f"### Similar Notes\n\n{chr(10).join(fallback_links[:5])}")

        # --- Phase 1b: Research swarm ---
        # Step 1: Generate diverse search queries via MiniMax
        search_queries = []
        try:
            # Build query generator input with gene-seed + vault context + Emperor feedback
            query_input = f"Note title: {title}\nNote type: {note_type}"
            if gene_seed_content:
                query_input += f"\n\nGene-seed (the Emperor's original note content):\n{gene_seed_content[:1500]}"
            if vault_context_full:
                query_input += f"\n\nExisting vault knowledge (full content of related notes):\n{vault_context_full[:3000]}"
            elif vault_context_str:
                query_input += f"\n\nExisting vault context (related notes found in the Obsidian vault):\n{vault_context_str[:1000]}"
            if emperor_feedback:
                query_input += f"\n\nIMPORTANT — Emperor's feedback from a previous failed cycle (research the RIGHT domain this time):\n{emperor_feedback[:1000]}"
            query_response = await minimax_chat(
                system_prompt=IMPLANTATION_ROLES["query_generator"]["system"],
                user_content=query_input,
                max_tokens=IMPLANTATION_ROLES["query_generator"]["max_tokens"],
            )
            if query_response and query_response.strip():
                search_queries = [
                    q.strip() for q in query_response.strip().splitlines() if q.strip()
                ]
                search_queries = search_queries[:15]  # cap at 15
        except Exception as e:
            logger.warning(f"Implantation: query generation failed for '{title}': {e}")

        if not search_queries:
            # Fallback: use title as sole query
            search_queries = [title]

        logger.info(
            f"Implantation: swarm dispatched ({len(search_queries)} researchers) for '{title}'"
        )

        # Step 2: Run all web searches in parallel
        async def _web_search(query: str) -> tuple[str, str]:
            """Run a single web search, return (query, results)."""
            try:
                proc = await asyncio.create_subprocess_exec(
                    WEB_SEARCH_CLI,
                    "--count",
                    "5",
                    query,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                return (query, stdout.decode("utf-8", errors="replace").strip())
            except TimeoutError:
                return (query, "")
            except Exception:
                return (query, "")

        web_results = await asyncio.gather(
            *[_web_search(q) for q in search_queries],
            return_exceptions=True,
        )

        # Step 3: Fire MiniMax researcher calls in parallel
        vault_brief = (
            vault_context_full[:3000]
            if vault_context_full
            else (
                vault_context_str[:2000]
                if vault_context_str
                else "No existing vault notes found on this topic."
            )
        )

        async def _research(query: str, web_output: str) -> str:
            """One researcher synthesizes their search results."""
            if not web_output:
                return ""
            return await minimax_chat(
                system_prompt=IMPLANTATION_ROLES["web_researcher"]["system"],
                user_content=(
                    f"Context: {CODEX_CONTEXT}\n\n"
                    f"Existing vault knowledge:\n{vault_brief}\n\n"
                    f"Your research angle: {query}\n\n"
                    f"Web search results:\n{web_output}"
                ),
                max_tokens=512,
            )

        researcher_tasks = []
        for result in web_results:
            if isinstance(result, Exception):
                continue
            query, output = result
            if output:
                researcher_tasks.append(_research(query, output))

        researcher_outputs = []
        if researcher_tasks:
            raw_outputs = await asyncio.gather(*researcher_tasks, return_exceptions=True)
            for out in raw_outputs:
                if isinstance(out, str) and out.strip():
                    researcher_outputs.append(out.strip())

        logger.info(
            f"Implantation: {len(researcher_outputs)}/{len(researcher_tasks)} researchers returned content for '{title}'"
        )

        # --- Phase 1c: Consolidation ---
        consolidated = None
        if researcher_outputs:
            combined = "\n\n---\n\n".join(researcher_outputs)
            try:
                consolidated = await minimax_chat(
                    system_prompt=IMPLANTATION_ROLES["consolidator"]["system"],
                    user_content=f"Topic: {title}\nNote type: {note_type}\n\nResearch reports:\n\n{combined}",
                    max_tokens=IMPLANTATION_ROLES["consolidator"]["max_tokens"],
                )
            except Exception as e:
                logger.warning(f"Implantation: consolidation failed for '{title}': {e}")

        logger.info(f"Implantation: consolidation complete for '{title}'")

        # --- Phase 1d: Inquisition Oversight ---
        # Sonnet reviews the MiniMax consolidated output for hallucinations,
        # off-topic content, and ensures system-context grounding.
        inquisition_reviewed = None
        review_input = consolidated if consolidated else "\n\n".join(researcher_outputs[:3])
        if review_input and review_input.strip():
            inquisition_prompt = (
                f"{CODEX_CONTEXT}\n\n"
                f"You are an Inquisitor reviewing research produced by Imperial Guard (MiniMax) servitors. "
                f"The Guard is cheap and fast but UNTRUSTED — they hallucinate URLs, produce off-topic content, "
                f"and fail to ground research in our system's context.\n\n"
                f'The note being researched is titled: "{title}"\n'
                f"Note type: {note_type}\n"
                f"Gene-seed (Emperor's original note content):\n{gene_seed_content[:1000] if gene_seed_content else '(title only — no body content)'}\n\n"
                f"Existing vault knowledge:\n{vault_context_full[:4000] if vault_context_full else (vault_context_str[:1500] if vault_context_str else 'No existing vault notes on this topic.')}\n\n"
                f"Here is the Guard's consolidated research output:\n\n---\n{review_input}\n---\n\n"
                f"Your task:\n"
                f"1. REMOVE any fabricated/hallucinated URLs (fake protocols like imperium://, made-up domains). Keep only real, verifiable URLs.\n"
                f"2. REMOVE content that is clearly off-topic or not relevant to the note's actual subject.\n"
                f"3. GRADE the relevance: is this research actually useful for understanding or acting on this note? If the Guard researched the wrong thing entirely, say so.\n"
                f"4. REWRITE the research brief with only verified, relevant content. Add a one-line Inquisition verdict at the end.\n\n"
                f"Output the cleaned research in markdown. Be ruthless — better to have 2 solid paragraphs than 6 paragraphs of noise."
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    "claude",
                    "--model",
                    "claude-sonnet-4-6",
                    "-p",
                    "--no-session-persistence",
                    "--dangerously-skip-permissions",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    stdin=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=inquisition_prompt.encode("utf-8")),
                    timeout=90,
                )
                inquisition_reviewed = stdout.decode("utf-8", errors="replace").strip()
                logger.info(f"Implantation: Inquisition review complete for '{title}'")
            except TimeoutError:
                logger.warning(
                    f"Implantation: Inquisition review timed out for '{title}', using raw consolidation"
                )
            except Exception as e:
                logger.warning(f"Implantation: Inquisition review failed for '{title}': {e}")

        # Build Research section (prefer Inquisition-reviewed, fallback to raw consolidation)
        if inquisition_reviewed and inquisition_reviewed.strip():
            sections.append(f"### Research\n\n{inquisition_reviewed.strip()}")
        elif consolidated and consolidated.strip():
            sections.append(
                f"### Research\n\n*\u26a0\ufe0f Inquisition review unavailable — unreviewed Guard output:*\n\n{consolidated.strip()}"
            )
        elif researcher_outputs:
            fallback_research = "\n\n".join(researcher_outputs[:3])
            sections.append(
                f"### Research\n\n*\u26a0\ufe0f Unreviewed Guard output (no consolidation):*\n\n{fallback_research[:3000]}"
            )

        # --- Append to note ---
        if not sections:
            logger.info(f"Implantation: no enrichment content for '{title}', skipping append")
            return

        # Strip old pipeline artifacts on re-implantation (prevents snowball accumulation)
        if implant_count > 0:
            try:
                note_file = OBSIDIAN_VAULT_PATH / note_path
                if note_file.exists():
                    raw = note_file.read_text(encoding="utf-8")
                    # Find first ## Implantation and truncate everything after it
                    marker_idx = raw.find("\n## Implantation")
                    if marker_idx != -1:
                        cleaned = raw[:marker_idx].rstrip() + "\n\n"
                        note_file.write_text(cleaned, encoding="utf-8")
                        logger.info(f"Implantation: stripped old pipeline artifacts from '{title}'")
            except Exception as e:
                logger.warning(f"Implantation: failed to strip old artifacts from '{title}': {e}")

        implant_content = "## Implantation\n\n" + "\n\n".join(sections)

        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "append",
                f"path={note_path}",
                f"content={implant_content}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                logger.error(f"Implantation: append failed for '{title}': {stderr.decode()}")
                return
        except Exception as e:
            logger.error(f"Implantation: append command failed for '{title}': {e}")
            return

        # Update status to implanted
        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "property:set",
                f"path={note_path}",
                "property=status",
                "value=implanted",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
        except Exception as e:
            logger.warning(f"Implantation: property:set failed for '{title}': {e}")

        try:
            await log_event(
                "implantation_complete",
                device_id="minimax",
                details={
                    "path": note_path,
                    "title": title,
                    "type": note_type,
                    "source": source,
                    "has_vault_matches": bool(vault_synthesis or raw_vault_lines),
                    "has_web_research": bool(consolidated or researcher_outputs),
                    "researcher_count": len(researcher_outputs),
                },
            )
        except Exception as e:
            logger.warning(f"Implantation: log_event failed for '{title}': {e}")
        logger.info(f"Implantation complete for '{title}' ({note_path})")

        # Post implantation summary to aspirant thread
        thread_id = await _read_note_property(note_path, "thread_id")
        if thread_id:
            implant_summary = (
                f"**📚 Implantation Complete**\n"
                f"Researchers: {len(researcher_outputs)} | "
                f"Vault matches: {'Yes' if (vault_synthesis or raw_vault_lines) else 'None'}\n\n"
                f"{(inquisition_reviewed or consolidated or '')[:1500]}"
            )
            await _post_to_aspirant_thread(thread_id, implant_summary)

        # --- Fire Trials phase ---
        if not skip_trials:
            asyncio.create_task(
                run_trials(note_path, title, note_type, vault_context=vault_context_full)
            )
            logger.info(f"Trials dispatched for '{title}'")

    except Exception as e:
        logger.error(f"Implantation failed for '{title}': {e}", exc_info=True)


async def run_trials(note_path: str, title: str, note_type: str, vault_context: str = "") -> None:
    """Stage 3: Trials — Custodes Sonnet challenge of an implanted note."""
    try:
        logger.info(f"Trials: starting for '{title}' ({note_path})")

        # Read the full note content (now with implantation)
        note_content = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "read",
                f"path={note_path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            note_content = stdout.decode("utf-8", errors="replace").strip()
        except Exception as e:
            logger.error(f"Trials: could not read note '{title}': {e}")
            return

        if not note_content:
            logger.warning(f"Trials: empty note content for '{title}', skipping")
            return

        # Build the trials prompt for Sonnet
        trials_prompt = (
            f"{CODEX_CONTEXT}\n\n"
            f"You are an Adeptus Custodes performing trials on a new aspirant note. "
            f"Your role is to challenge, question, and stress-test this note before it enters the vault.\n\n"
            f"## Pipeline Context — READ THIS FIRST\n\n"
            f"This note has already passed through earlier pipeline stages. The note contains:\n"
            f"1. **Gene-Seed** (in a `> [!dna]` callout): The Emperor's original idea/content. This is the CORE of the note — "
            f"everything else exists to serve it. Your trials should evaluate whether the gene-seed idea is sound, not whether "
            f"the pipeline processing was perfect.\n"
            f"2. **## Implantation / ### Research**: Research produced by an Imperial Guard (MiniMax) swarm, then reviewed "
            f"by the Inquisition (Sonnet). The Inquisition Review section is the oversight layer's assessment of the Guard's "
            f"research quality. If the Inquisition says 'MISSION FAILURE' or 'FAIL', that means the Guard researched the wrong "
            f"thing — it does NOT mean the gene-seed idea is wrong. The gene-seed and the research quality are independent.\n"
            f"3. **Inquisition Verdict**: The Inquisitor's final assessment of research quality. This is pipeline metadata, "
            f"not part of the note's content. Do not treat it as contradicting the gene-seed.\n\n"
            f"Your job is to evaluate the GENE-SEED IDEA on its own merits, informed by whatever useful research survived "
            f"the Inquisition review. If the research failed, note that the idea needs better research — don't conflate "
            f"research failure with idea failure.\n\n"
            f"The note's declared type is: {note_type}\n\n"
            f"{'## Existing Vault Knowledge' + chr(10) + chr(10) + 'Use this to ground your challenges in how the system actually works:' + chr(10) + chr(10) + vault_context[:4000] + chr(10) + chr(10) if vault_context else ''}"
            f"Here is the full note content:\n\n---\n{note_content}\n---\n\n"
            f"Produce exactly THREE sections in this format (use Obsidian callout syntax):\n\n"
            f"## Trials\n\n"
            f"> [!question] Open Questions\n"
            f"> - (What's unclear about the GENE-SEED idea? What assumptions is the Emperor making? What needs clarification?)\n\n"
            f"> [!warning] Challenges\n"
            f"> - (What could go wrong with this idea? Counterarguments? What's being overlooked?)\n\n"
            f"> [!tip] Implementation Speculation\n"
            f"> - (If prescriptive: how might it be implemented, what steps? If descriptive: what are the implications?)\n\n"
            f"IMPORTANT: Check the note's `type` field. If the content is prescriptive (a goal, directive, or task) "
            f"but marked as `descriptive`, or vice versa, flag this as a classification error in your Open Questions section.\n\n"
            f"Be concise but thorough. 3-5 bullets per section. Output ONLY the formatted sections, nothing else."
        )

        # Run Claude Sonnet via CLI (pipe prompt via stdin for long content)
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "--model",
                "claude-sonnet-4-6",
                "-p",
                "--no-session-persistence",
                "--dangerously-skip-permissions",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=trials_prompt.encode("utf-8")),
                timeout=120,
            )
            trials_output = stdout.decode("utf-8", errors="replace").strip()
        except TimeoutError:
            logger.error(f"Trials: Sonnet timed out for '{title}'")
            return
        except Exception as e:
            logger.error(f"Trials: Sonnet subprocess failed for '{title}': {e}")
            return

        if not trials_output:
            logger.warning(f"Trials: empty Sonnet response for '{title}'")
            return

        # Ensure the output starts with ## Trials header (add if Sonnet omitted it)
        if not trials_output.startswith("## Trials"):
            trials_output = f"## Trials\n\n{trials_output}"

        # Append trials to the note
        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "append",
                f"path={note_path}",
                f"content={trials_output}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                logger.error(f"Trials: append failed for '{title}': {stderr.decode()}")
                return
        except Exception as e:
            logger.error(f"Trials: append command failed for '{title}': {e}")
            return

        logger.info(
            f"Trials generated for '{title}' ({note_path}) — frontmatter questions gate handles live validation"
        )

        try:
            await log_event(
                "trials_initiated",
                device_id="sonnet",
                details={
                    "path": note_path,
                    "title": title,
                    "type": note_type,
                },
            )
        except Exception as e:
            logger.warning(f"Trials: log_event failed for '{title}': {e}")

    except Exception as e:
        logger.error(f"Trials failed for '{title}': {e}", exc_info=True)


async def run_deployment(note_path: str, title: str, note_type: str) -> None:
    """Stage 4: Deployment — Guilliman codifies and promotes a questions-cleared note to its final vault location."""
    try:
        logger.info(f"Deployment: starting for '{title}' (type={note_type})")

        # Read the full note content
        note_content = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "read",
                f"path={note_path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            note_content = stdout.decode("utf-8", errors="replace").strip()
        except Exception as e:
            logger.error(f"Deployment: could not read note '{title}': {e}")
            return

        # Determine destination based on note type
        is_prescriptive = note_type in ("prescriptive", "task")
        destination_dir = "Mars/Tasks" if is_prescriptive else "Terra/Ultramar"
        destination_path = f"{destination_dir}/{title}.md"

        # Build Guilliman deployment prompt
        deployment_prompt = (
            f"{CODEX_CONTEXT}\n\n"
            f"You are Guilliman, the Codifier. You are deploying aspirant note '{title}' to the Imperium vault.\n\n"
            f"## Source Note\n\nPath: {note_path}\nType: {note_type}\nDestination: {destination_path}\n\n"
            f"## Note Content\n\n{note_content[:4000]}\n\n"
            f"## Instructions\n\n"
            f"Restructure this note for its final vault location. Your output will REPLACE the note content entirely.\n\n"
            f"Requirements:\n"
            f"1. Write valid Obsidian frontmatter. Keep existing fields, update:\n"
            f"   - status: deployed\n"
            f"   - deployed_from: {note_path}\n"
            f"   - deployed_date: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"{'   - For prescriptive: ensure progress, completed, temperature, importance, timescale fields exist' + chr(10) if is_prescriptive else ''}"
            f"2. Remove pipeline artifacts (## Implantation, ## Trials, ## Verdict sections) — these were staging, not canon\n"
            f"3. Keep the Gene-Seed callout as the core content seed\n"
            f"4. Add a clean ## Summary section synthesizing the research insights into authoritative prose\n"
            f"5. Add relevant [[wikilinks]] to existing vault notes where connections exist\n"
            f"6. Add appropriate tags (use {'mars/task' if is_prescriptive else 'terra/ultramar'} domain prefix)\n"
            f"7. Output ONLY the final note content (frontmatter + body), nothing else\n"
        )

        deployed_content = ""
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "--model",
                "claude-sonnet-4-6",
                "-p",
                "--no-session-persistence",
                "--dangerously-skip-permissions",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=deployment_prompt.encode("utf-8")),
                timeout=120,
            )
            deployed_content = stdout.decode("utf-8", errors="replace").strip()
        except TimeoutError:
            logger.error(f"Deployment: Sonnet timed out for '{title}'")
            return
        except Exception as e:
            logger.error(f"Deployment: Sonnet failed for '{title}': {e}")
            return

        if not deployed_content:
            logger.warning(f"Deployment: empty output for '{title}'")
            return

        # Strip markdown code fences that Sonnet sometimes wraps output in
        if deployed_content.startswith("```"):
            lines = deployed_content.splitlines()
            if lines[-1].strip() == "```":
                deployed_content = "\n".join(lines[1:-1]).strip()

        # Create the note at its final destination via obsidian CLI
        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "create",
                f"path={destination_path}",
                f"content={deployed_content}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            if proc.returncode != 0:
                stderr_text = stderr.decode("utf-8", errors="replace")
                if "already exists" in stderr_text.lower():
                    logger.warning(f"Deployment: {destination_path} already exists, overwriting")
                    dest_full = OBSIDIAN_VAULT_PATH / destination_path
                    dest_full.write_text(deployed_content, encoding="utf-8")
                else:
                    logger.error(f"Deployment: create failed for '{title}': {stderr_text}")
                    return
        except Exception as e:
            logger.error(f"Deployment: create failed for '{title}': {e}")
            return

        # Update original note status to deployed
        try:
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "property:set",
                f"path={note_path}",
                "property=status",
                "value=deployed",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
            proc = await asyncio.create_subprocess_exec(
                OBSIDIAN_CLI,
                "vault=Imperium-ENV",
                "property:set",
                f"path={note_path}",
                "property=deployed_to",
                f"value={destination_path}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=15)
        except Exception as e:
            logger.warning(f"Deployment: status update failed for '{title}': {e}")

        # Post deployment summary to aspirant thread
        thread_id = await _read_note_property(note_path, "thread_id")
        if thread_id:
            deploy_msg = (
                f"**Guilliman Deployment Complete**\n"
                f"Promoted to: `{destination_path}`\n"
                f"Status: `deployed`\n\n"
                f"The aspirant has been codified and admitted to the vault canon."
            )
            await _post_to_aspirant_thread(thread_id, deploy_msg)

        try:
            await log_event(
                "deployment",
                device_id="guilliman",
                details={
                    "source_path": note_path,
                    "destination_path": destination_path,
                    "title": title,
                    "type": note_type,
                },
            )
        except Exception as e:
            logger.warning(f"Deployment: log_event failed for '{title}': {e}")
        logger.info(f"Deployment complete: '{title}' -> {destination_path}")

    except Exception as e:
        logger.error(f"Deployment failed for '{title}': {e}", exc_info=True)


class InboxDeployRequest(BaseModel):
    """Manual trigger for deployment on a questions-cleared aspirant note."""

    path: str


@app.post("/api/inbox/deploy")
async def inbox_deploy(request: InboxDeployRequest):
    """Manually trigger Guilliman deployment on a questions-cleared aspirant note."""
    filename = request.path.rsplit("/", 1)[-1] if "/" in request.path else request.path
    title = filename.replace(".md", "")

    full_path = OBSIDIAN_VAULT_PATH / request.path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail=f"Note not found: {request.path}")

    note_type = await _read_note_property(request.path, "type") or "descriptive"
    status = await _read_note_property(request.path, "status")
    if status != "aspirant_trials":
        raise HTTPException(
            status_code=400, detail=f"Note status is '{status}', expected 'aspirant_trials'"
        )
    try:
        clear, blockers = await asyncio.to_thread(trials_clear, full_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not clear:
        raise HTTPException(
            status_code=400,
            detail={"error": "questions_not_clear", "blockers": blockers},
        )

    asyncio.create_task(
        run_deployment(
            note_path=request.path,
            title=title,
            note_type=note_type,
        )
    )

    destination_dir = "Mars/Tasks" if note_type in ("prescriptive", "task") else "Terra/Ultramar"
    return {
        "dispatched": True,
        "path": request.path,
        "title": title,
        "destination": f"{destination_dir}/{title}.md",
    }


class InboxImplantRequest(BaseModel):
    """Manual trigger for implantation on an existing inbox note."""

    path: str
    skip_trials: bool = False


@app.post("/api/inbox/implant")
async def inbox_implant(request: InboxImplantRequest):
    """Manually trigger implantation on an existing inbox note."""
    # Extract title from filename
    filename = request.path.rsplit("/", 1)[-1] if "/" in request.path else request.path
    title = filename.replace(".md", "")

    # Verify note exists
    full_path = OBSIDIAN_VAULT_PATH / request.path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail=f"Note not found: {request.path}")

    asyncio.create_task(
        run_implantation(
            note_path=request.path,
            title=title,
            note_type="capture",
            source="manual",
            skip_trials=request.skip_trials,
        )
    )

    return {
        "dispatched": True,
        "path": request.path,
        "title": title,
        "skip_trials": request.skip_trials,
    }


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

# ---- Implantation Roles ----
IMPLANTATION_ROLES = {
    "vault_relevance": {
        "system": "You are a vault librarian. Given search results from an Obsidian vault and a new note's title and content, identify the 3-5 most relevant existing notes. For each, write one sentence explaining the connection. If none are relevant, respond with exactly: NO_MATCHES. Format as a markdown list with wikilinks: - [[Note Name]] — connection explanation",
        "max_tokens": 512,
    },
    "web_researcher": {
        "system": "You are a research assistant. Given web search results about a topic, write a concise focused paragraph. Include key facts, recent developments, and actionable insights. Cite sources with markdown links. Be factual and dense — no filler. You are one of many researchers working in parallel on different angles of the same topic, so stay focused on YOUR specific research angle.",
        "max_tokens": 512,
    },
    "query_generator": {
        "system": (
            "You are a research strategist for the Imperium of Claude — an agent orchestration system "
            "built with Claude Code, Obsidian vaults, Discord bots, tmux, and Python/FastAPI. "
            "Given a note title, its gene-seed content, and existing vault context, generate 12 diverse "
            "web search queries that would help research this topic thoroughly. "
            "CRITICAL: Ground your queries in the ACTUAL domain of the note. If the title contains words "
            "that have multiple meanings (e.g., 'aspirant', 'pipeline', 'vault', 'fleet', 'guard'), use "
            "the gene-seed content and vault context to determine the correct domain. DO NOT generate "
            "queries about unrelated domains (industrial pipelines, military aspirants, bank vaults, etc.). "
            "Include: direct queries, related technical concepts, implementation patterns, recent developments, "
            "and deep-dives. Return ONLY the queries, one per line, no numbering or bullets."
        ),
        "max_tokens": 512,
    },
    "consolidator": {
        "system": "You are a research consolidator. Given multiple research reports on the same topic, merge them into a single cohesive research brief. Remove duplicates, resolve contradictions, keep the most important facts and insights. Cite sources with markdown links. Output clean markdown, 3-6 paragraphs. Be dense and factual.",
        "max_tokens": 2048,
    },
}

# ---- Minimax API Client ----
_MINIMAX_BASE_URL = "https://api.minimax.io/anthropic"
_MINIMAX_MODEL = "MiniMax-M2.5"


def _get_minimax_key() -> str:
    """Read MiniMax API key from MINIMAX_API_KEY env var."""
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        raise RuntimeError("MINIMAX_API_KEY environment variable not set")
    return key


async def minimax_chat(system_prompt: str, user_content: str, max_tokens: int = 1024) -> str:
    """Send a chat message to Minimax and return the text response.

    MiniMax M2.5 uses extended thinking by default. We set a thinking budget
    so the model spends most tokens on the actual text response. If only
    thinking blocks come back (no text), we extract from thinking as fallback.
    """
    if not await minimax_limiter.acquire():
        logger.warning(f"MiniMax rate limited ({minimax_limiter.remaining} remaining)")
        return ""
    key = _get_minimax_key()

    # Disable extended thinking — MiniMax M2.5 routes everything through
    # thinking blocks via the Anthropic-compatible API, leaving zero text output.
    body: dict = {
        "model": _MINIMAX_MODEL,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
        "thinking": {"type": "disabled"},
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            f"{_MINIMAX_BASE_URL}/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content") or []
        stop_reason = data.get("stop_reason", "?")

        # Primary: extract text blocks
        text = "".join(block["text"] for block in content if block.get("type") == "text")

        # Fallback: if no text blocks, extract from thinking blocks
        if not text:
            thinking_text = "".join(
                block.get("thinking", "") for block in content if block.get("type") == "thinking"
            )
            if thinking_text:
                logger.info(
                    f"MiniMax: no text blocks, extracting from thinking ({len(thinking_text)} chars)"
                )
                text = thinking_text

        if not text:
            logger.warning(
                f"MiniMax empty response: stop_reason={stop_reason}, content={data.get('content')!r}"
            )
        return text


# ---- Stop Evaluators ----
# Async MiniMax-powered evaluators that run after every stop.
# Each evaluator gets shared context (compacted history, recent tail, session doc)
# and can trigger a nudge if it detects a problem.
STOP_EVALUATORS = {
    "action_validator": {
        "system": (
            "You are an Action Validator for an autonomous AI coding agent. "
            "Your job is to determine whether the agent's final message instructs the human user "
            "to perform manual actions (running commands, editing files, opening tools, copy-pasting) "
            "instead of doing those actions autonomously.\n\n"
            "Context: The agent has access to Bash, file editing, web search, and subagent tools. "
            "It should almost never tell the user to do something manually. Exceptions where user "
            "action IS acceptable:\n"
            "- The agent asked the user a question (AskUserQuestion)\n"
            "- The agent is reporting results of completed work\n"
            "- The agent is explaining what it DID (past tense), not what the user SHOULD DO\n"
            "- The agent needs physical-world action (restart an app, plug in a device)\n"
            "- The agent is in plan mode discussing approach\n\n"
            "Analyze the agent's final message. You may reason through your analysis freely, "
            "but you MUST end your response with exactly one of these lines:\n\n"
            "VERDICT: BLOCK <one-sentence description of what the agent should do instead>\n"
            "VERDICT: CONTINUE\n\n"
            "The VERDICT line must be the final line of your response."
        ),
        "max_tokens": 4096,
        "requires_session_doc": False,
    },
    "plan_auditor": {
        "system": (
            "You are a Plan Auditor. Given a session document and recent activity, "
            "identify if any part of the Plan section needs updating based on what just happened.\n\n"
            "Do not block solely because the Plan section is empty or says 'No plan defined yet'. "
            "That placeholder is not actionable by itself. Only block when an existing concrete "
            "plan is now stale or contradicted by the latest activity.\n\n"
            "You may reason through your analysis freely, "
            "but you MUST end your response with exactly one of these lines:\n\n"
            "VERDICT: BLOCK The Plan section currently shows <current state> but <what changed>. "
            "The Plan should be updated to <specific change>.\n"
            "VERDICT: CONTINUE\n\n"
            "The VERDICT line must be the final line of your response."
        ),
        "max_tokens": 4096,
        "requires_session_doc": True,
    },
}


_EVALUATOR_NO_CONTENT_MARKERS = (
    "no transcript",
    "without the actual agent message",
    "actual agent message",
    "actual final message",
    "message content was provided",
    "content was provided",
    "context appears incomplete",
    "cannot analyze",
    "unable to validate",
    "lacks the actual",
)


_PLAN_AUDITOR_PLACEHOLDER_MARKERS = (
    "no plan defined",
    "no plan currently defined",
    "plan section is empty",
    "plan section currently empty",
)


def _parse_verdict_from_tail(text: str) -> tuple[str | None, str]:
    """Scan from the tail of a MiniMax response for a structured verdict.

    Expected format at end of response:
        VERDICT: BLOCK <finding>
        VERDICT: CONTINUE

    Returns (verdict, finding) where verdict is "block"/"continue"/None.
    """
    import re as _re

    lines = text.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        match = _re.match(r"(?i)^VERDICT:\s*(BLOCK|CONTINUE)\s*(.*)", line)
        if match:
            verdict = match.group(1).lower()
            finding = match.group(2).strip().rstrip(".").strip()
            return verdict, finding[:300]
    return None, ""


async def _jury_interpret(evaluator_name: str, original_response: str) -> tuple[bool, str]:
    """Fallback: spawn a second MiniMax call to interpret an unparseable response.

    Cheap, fast, no reasoning — just yes/no on whether the first call decided to block.
    """
    label = evaluator_name.replace("_", " ")
    system = (
        f"A {label} evaluated an AI agent and produced the response below. "
        "Did the evaluator decide the agent should be stopped/blocked/corrected? "
        "Answer ONLY 'yes' or 'no'. No other reasoning, response, or preamble."
    )
    user_content = f"Evaluator response:\n\n{original_response[:2000]}"

    try:
        answer = await minimax_chat(system, user_content, max_tokens=256)
        answer_lower = answer.strip().lower()
        logger.info(f"StopEval: jury raw answer for {evaluator_name}: {answer_lower[:100]!r}")
        # Check for "yes" anywhere — MiniMax may wrap it in thinking/reasoning
        decided_block = "yes" in answer_lower and "no" not in answer_lower
        if decided_block:
            # Extract a usable finding from the original — first substantive line
            for line in original_response.strip().splitlines():
                line = line.strip()
                if len(line) > 15 and not line.upper().startswith("VERDICT"):
                    return True, line[:300]
            return True, f"{label} flagged this session (details in transcript)"
        return False, ""
    except Exception as e:
        logger.warning(f"StopEval jury failed for {evaluator_name}: {e}")
        return False, ""


def _parse_evaluator_result(evaluator_name: str, text: str) -> tuple[bool, str, bool]:
    """Parse MiniMax evaluator response into (should_nudge, finding, needs_jury).

    Scans from the tail for VERDICT: BLOCK/CONTINUE.
    If no verdict found, returns needs_jury=True for fallback interpretation.
    """
    text = text.strip()
    if not text:
        return False, "", False
    text_lower = text.lower()
    if any(marker in text_lower for marker in _EVALUATOR_NO_CONTENT_MARKERS):
        return False, "", False
    if evaluator_name == "plan_auditor" and any(
        marker in text_lower for marker in _PLAN_AUDITOR_PLACEHOLDER_MARKERS
    ):
        return False, "", False

    verdict, finding = _parse_verdict_from_tail(text)

    if verdict == "block":
        finding_lower = finding.lower()
        if any(marker in finding_lower for marker in _EVALUATOR_NO_CONTENT_MARKERS):
            return False, "", False
        if evaluator_name == "plan_auditor" and any(
            marker in finding_lower for marker in _PLAN_AUDITOR_PLACEHOLDER_MARKERS
        ):
            return False, "", False
        return (
            True,
            finding or f"{evaluator_name.replace('_', ' ').title()} flagged this session",
            False,
        )
    if verdict == "continue":
        return False, "", False

    # No parseable verdict — needs jury fallback
    return False, text, True


def _build_evaluator_prompt(evaluator_name: str, ctx: dict) -> str:
    """Build the user-content prompt for a specific evaluator from shared context."""
    if evaluator_name == "action_validator":
        parts = []
        if ctx["compacted_history"]:
            parts.append(f"## Session History (compacted)\n{ctx['compacted_history'][:3000]}")
        parts.append(f"## Recent Agent Activity (raw transcript tail)\n{ctx['recent_tail'][:3000]}")
        parts.append(f"Agent name: {ctx['tab_name']}")
        parts.append(
            "Analyze the agent's FINAL message. Is it telling the user to do something manually?"
        )
        return "\n\n".join(parts)

    elif evaluator_name == "plan_auditor":
        parts = []
        if ctx["session_doc"]:
            parts.append(f"## Session Document\n{ctx['session_doc'][:3000]}")
        if ctx["compacted_history"]:
            parts.append(f"## Session History (compacted)\n{ctx['compacted_history'][:2000]}")
        parts.append(f"## Recent Activity\n{ctx['recent_tail'][:2000]}")
        parts.append(f"Agent name: {ctx['tab_name']}")
        parts.append("Does the Plan section need any updates based on this activity?")
        return "\n\n".join(parts)

    return ctx["recent_tail"][:4000]


async def _gather_evaluator_context(
    instance_id: str,
    session_doc_id: int | None,
    transcript_tail: str,
    tab_name: str,
) -> dict:
    """Build shared context dict for all stop evaluators.

    Returns dict with: compacted_history, recent_tail, session_doc, tab_name.
    """
    ctx = {
        "compacted_history": "",
        "recent_tail": transcript_tail[:4000] if transcript_tail else "",
        "session_doc": None,
        "tab_name": tab_name,
    }

    # Fetch compacted transcripts (prior stops for this instance)
    prefix = instance_id[:8]
    transcript_rel = await _find_latest_transcript(prefix)
    if transcript_rel:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["obsidian", "vault=Imperium-ENV", "read", f"path={transcript_rel}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                ctx["compacted_history"] = result.stdout[:6000]
        except Exception:
            pass

    # Fetch session doc if linked
    if session_doc_id:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT file_path FROM session_documents WHERE id = ?", (session_doc_id,)
                )
                row = await cursor.fetchone()
                if row:
                    fp = Path(row[0])
                    if fp.exists():
                        ctx["session_doc"] = fp.read_text()[:3000]
        except Exception:
            pass

    return ctx


async def _auto_name_instance(
    instance: dict, transcript_tail: str, transcript_path: str = ""
) -> None:
    """Deprecated: do not programmatically invent names.

    Naming is now agent-owned. The system may detect unnamed docs and interview
    the live instance, but it must not synthesize a fallback name from cwd,
    timestamp, transcript, model, pane, or UUID.
    """
    logger.info(f"AutoName: disabled for {str(instance.get('id', '?'))[:12]}")
    return


async def _run_stop_evaluators(
    instance_id: str,
    session_doc_id: int | None,
    transcript_tail: str,
    tab_name: str,
) -> None:
    """Dispatch stop evaluators concurrently — first failure wins, cancel the rest.

    All evaluators produce a binary outcome: nudge or pass. If any evaluator
    triggers a nudge the others are irrelevant (the instance is being nudged
    regardless), so we race them and cancel survivors on first failure.
    """
    # Loop prevention: skip if recently nudged
    last_nudge = _recently_nudged.get(instance_id, 0)
    if time.time() - last_nudge < NUDGE_COOLDOWN_SECONDS:
        logger.info(
            f"StopEval: skipping {instance_id[:12]} — nudged {int(time.time() - last_nudge)}s ago"
        )
        return

    # Rate limit check
    if minimax_limiter.remaining < 5:
        logger.warning(f"StopEval: skipping — MiniMax budget low ({minimax_limiter.remaining})")
        return

    # Gather shared context once
    ctx = await _gather_evaluator_context(instance_id, session_doc_id, transcript_tail, tab_name)

    # Fire all applicable evaluators concurrently
    tasks: dict[str, asyncio.Task] = {}
    for name, config in STOP_EVALUATORS.items():
        if config.get("requires_session_doc") and not ctx["session_doc"]:
            continue
        if (
            name == "action_validator"
            and not ctx["recent_tail"].strip()
            and not ctx["compacted_history"].strip()
        ):
            logger.info(
                f"StopEval: skipping action_validator for {instance_id[:12]} — no transcript content"
            )
            continue
        prompt = _build_evaluator_prompt(name, ctx)
        tasks[name] = asyncio.create_task(
            minimax_chat(config["system"], prompt, config["max_tokens"])
        )

    if not tasks:
        return

    # Race: first nudge wins, cancel the rest
    task_to_name = {t: n for n, t in tasks.items()}
    pending = set(tasks.values())
    nudge_evaluator = None
    nudge_finding = None
    jury_queue: list[tuple[str, str]] = []  # (evaluator_name, raw_response)

    while pending:
        done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            name = task_to_name[task]
            try:
                text = task.result()
            except Exception as e:
                logger.error(f"StopEval: {name} failed for {instance_id[:12]}: {e}")
                continue

            should_nudge, finding, needs_jury = _parse_evaluator_result(name, text)
            if needs_jury:
                # Log tail of unparseable response for debugging
                tail_preview = text.strip().splitlines()[-3:] if text.strip() else ["(empty)"]
                logger.info(f"StopEval: {name} response tail: {' | '.join(tail_preview)[:200]}")
            if should_nudge and finding:
                nudge_evaluator = name
                nudge_finding = finding
                logger.info(f"StopEval: {name} triggered for {instance_id[:12]}: {finding[:100]}")
                for t in pending:
                    t.cancel()
                pending = set()
                break
            elif needs_jury:
                logger.info(f"StopEval: {name} unparseable for {instance_id[:12]}, queuing jury")
                jury_queue.append((name, text))
            else:
                logger.info(f"StopEval: {name} passed for {instance_id[:12]}")

    # Jury fallback: interpret any unparseable responses
    if not nudge_finding and jury_queue:
        for eval_name, raw_response in jury_queue:
            logger.info(f"StopEval: jury interpreting {eval_name} for {instance_id[:12]}")
            should_nudge, finding = await _jury_interpret(eval_name, raw_response)
            if should_nudge and finding:
                nudge_evaluator = eval_name
                nudge_finding = finding
                logger.info(
                    f"StopEval: jury confirmed {eval_name} for {instance_id[:12]}: {finding[:100]}"
                )
                break
            else:
                logger.info(f"StopEval: jury cleared {eval_name} for {instance_id[:12]}")

    if not nudge_finding:
        # All evaluators passed — transition to idle (stop hook left it as processing)
        _recently_nudged.pop(instance_id, None)
        (Path.home() / ".claude" / "tui-signals" / f"evaluating-{instance_id}").unlink(
            missing_ok=True
        )
        async with aiosqlite.connect(DB_PATH) as db:
            try:
                await sanctioned_update_instance(
                    db,
                    instance_id=instance_id,
                    updates={"status": "idle"},
                    mutation_type="status_changed",
                    write_source="system",
                    actor="stop-evaluator",
                    where_clause="id = ? AND status = 'processing'",
                    where_params=(instance_id,),
                )
            except LookupError:
                pass
            await db.commit()
        logger.info(f"StopEval: all passed for {instance_id[:12]} — status → idle")
        return

    # Append finding to transcript file
    label = nudge_evaluator.replace("_", " ").title()
    nudge_message = f"[{label}] {nudge_finding}"

    transcript_rel = await _find_latest_transcript(instance_id[:8])
    if transcript_rel:
        audit_section = f"\n\n## Evaluator Finding\n\n**{nudge_evaluator}**: {nudge_finding}\n"
        try:
            await asyncio.to_thread(
                subprocess.run,
                [
                    "obsidian",
                    "vault=Imperium-ENV",
                    "append",
                    f"path={transcript_rel}",
                    f"content={audit_section}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception:
            pass

    # Record nudge timestamp BEFORE nudging
    _recently_nudged[instance_id] = time.time()
    (Path.home() / ".claude" / "tui-signals" / f"evaluating-{instance_id}").unlink(missing_ok=True)

    # Nudge the instance
    try:
        await _nudge_instance(instance_id, reason=nudge_message)
    except Exception as e:
        logger.warning(f"StopEval: nudge failed for {instance_id[:12]}: {e}")


async def _find_latest_transcript(instance_id_short: str) -> str | None:
    """Find the most recent transcript file for an instance (by glob on prefix)."""
    import glob as _glob

    pattern = f"Mars/Logs/Transcripts/{instance_id_short}-*.md"
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["obsidian", "vault=Imperium-ENV", "search", f"query=path:{pattern}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Fallback: glob on disk
        disk_pattern = str(Path.home() / "Imperium-ENV" / pattern)
        matches = sorted(_glob.glob(disk_pattern), reverse=True)
        if not matches:
            # Also check NAS path
            nas_pattern = f"/Volumes/Imperium/Imperium-ENV/{pattern}"
            matches = sorted(_glob.glob(nas_pattern), reverse=True)
        if matches:
            # Return vault-relative path
            for m in matches:
                parts = Path(m).parts
                for i, p in enumerate(parts):
                    if p.lower().endswith("-env"):
                        return str(Path(*parts[i + 1 :]))
        return None
    except Exception:
        return None


# Valid session doc status transitions (from → set of valid targets)
# Any status can transition to 'archived' as an escape hatch
VALID_STATUS_TRANSITIONS = {
    "active": {"completed", "archived"},
    "completed": {"deployment", "active", "archived"},
    "deployment": {"processed", "archived"},
    "processed": {"archived"},
    "archived": set(),  # terminal
}


async def get_primarch_from_db(db, name: str) -> dict | None:
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


# [MOVED to session_doc_helpers.py] — create_session_doc_file, _update_doc_agents_list


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
            "SELECT COUNT(*) FROM claude_instances WHERE session_doc_id = ?", (doc_id,)
        )
        count = (await cursor.fetchone())[0]
        if count > 0:
            return

        cursor = await db.execute(
            "SELECT file_path, title, status FROM session_documents WHERE id = ?", (doc_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return

        fp = Path(row[0])
        status = row[2]
        now = datetime.now().isoformat()

        # completed / deployment docs are in the pipeline — don't touch them
        if status in ("completed", "deployment"):
            logger.info(
                f"Orphan cleanup: doc {doc_id} ({row[1]}) is {status}, leaving for Administratum"
            )
            return

        # processed docs can be archived
        if status == "processed":
            await db.execute(
                "UPDATE session_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                (now, doc_id),
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
                (now, doc_id),
            )
            await db.commit()
            logger.info(
                f"Orphan cleanup: completed edited session doc {doc_id} ({row[1]}) — ready for deployment"
            )


# ============ Session Document Endpoints ============


@app.post("/api/session-docs")
async def create_session_doc(request: SessionDocCreateRequest):
    """Create a new session document."""
    if request.file_path:
        fp = Path(request.file_path)
    else:
        fp = unique_human_path(DEFAULT_SESSIONS_DIR, request.title)

    if fp.exists():
        raise HTTPException(status_code=409, detail=f"File already exists: {fp}")

    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO session_documents (title, file_path, project, primarch_name, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (request.title, str(fp), request.project, request.primarch_name, now, now),
        )
        doc_id = cursor.lastrowid

        # Auto-link primarch if specified
        if request.primarch_name:
            # Unlink any existing active doc for this primarch
            await db.execute(
                "UPDATE primarch_session_docs SET unlinked_at = ? WHERE primarch_name = ? AND unlinked_at IS NULL",
                (now, request.primarch_name),
            )
            await db.execute(
                "INSERT INTO primarch_session_docs (primarch_name, session_doc_id, linked_at) VALUES (?, ?, ?)",
                (request.primarch_name, doc_id, now),
            )

        await db.commit()

    create_session_doc_file(fp, request.title, doc_id, request.project, request.primarch_name)

    await log_event(
        "session_doc_created",
        details={
            "doc_id": doc_id,
            "title": request.title,
            "file_path": str(fp),
            "primarch_name": request.primarch_name,
        },
    )
    logger.info(f"Created session doc {doc_id}: {request.title} -> {fp}")

    return {
        "id": doc_id,
        "title": request.title,
        "file_path": str(fp),
        "status": "active",
        "primarch_name": request.primarch_name,
    }


@app.get("/api/session-docs")
async def list_session_docs(status: str | None = None, project: str | None = None):
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
                "SELECT COUNT(*) FROM claude_instances WHERE session_doc_id = ?", (row["id"],)
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
            "SELECT id, tab_name, status, working_dir FROM claude_instances WHERE session_doc_id = ?",
            (doc_id,),
        )
        instances = [dict(r) for r in await cursor.fetchall()]
        doc["instances"] = instances

    return doc


@app.get("/api/session-docs/{doc_id}/content")
async def get_session_doc_content(doc_id: int):
    """Read the actual markdown file content of a session document."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT file_path, title FROM session_documents WHERE id = ?", (doc_id,)
        )
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
        cursor = await db.execute(
            "SELECT id, status, file_path FROM session_documents WHERE id = ?", (doc_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")

        updates = []
        params = []
        if request.title is not None:
            updates.append("title = ?")
            params.append(request.title)
            old_raw = Path(row[2]) if row[2] else None
            old_path = (
                old_raw
                if old_raw and old_raw.is_absolute()
                else (OBSIDIAN_VAULT_PATH / old_raw if old_raw else None)
            )
            if old_path and old_path.exists():
                desired_name = f"{human_filename_stem(request.title, fallback='Session')}.md"
                new_path = (
                    old_path
                    if old_path.name == desired_name
                    else unique_human_path(old_path.parent, request.title, fallback="Session")
                )
                if new_path != old_path:
                    old_path.rename(new_path)
                try:
                    new_file_path = str(new_path.relative_to(OBSIDIAN_VAULT_PATH))
                except ValueError:
                    new_file_path = str(new_path)
                updates.append("file_path = ?")
                params.append(new_file_path)
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
                    detail=f"Invalid status transition: {current_status} → {request.status}. Valid: {valid_targets | {'archived'}}",
                )
            updates.append("status = ?")
            params.append(request.status)

        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        updates.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(doc_id)

        await db.execute(f"UPDATE session_documents SET {', '.join(updates)} WHERE id = ?", params)
        if request.title is not None:
            base = human_filename_stem(request.title, fallback="session-doc")
            cursor = await db.execute(
                """SELECT id, tab_name
                   FROM claude_instances
                   WHERE session_doc_id = ?
                   ORDER BY registered_at ASC, id ASC""",
                (doc_id,),
            )
            linked = await cursor.fetchall()
            ordinal = 1
            for inst_id, tab_name in linked:
                current = str(tab_name or "")
                if re.match(rf"^{re.escape(base)}-\d+$", current):
                    continue
                if current and not _is_placeholder_tab_name(current):
                    continue
                await sanctioned_update_instance(
                    db,
                    instance_id=inst_id,
                    updates={"tab_name": f"{base}-{ordinal}"},
                    mutation_type="instance_updated",
                    write_source="api",
                    actor="session-doc-rename",
                )
                ordinal += 1
        await db.commit()

    logger.info(f"Updated session doc {doc_id}: {updates}")
    return {"id": doc_id, "updated": True}


@app.delete("/api/session-docs/{doc_id}")
async def delete_session_doc(doc_id: int, hard: bool = False):
    """Delete a session document. Default is soft delete (archive). Use ?hard=true for hard delete."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT file_path, title FROM session_documents WHERE id = ?", (doc_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")

        if hard:
            cursor = await db.execute(
                "SELECT id FROM claude_instances WHERE session_doc_id = ?",
                (doc_id,),
            )
            linked_rows = await cursor.fetchall()
            for linked_row in linked_rows:
                await sanctioned_update_instance(
                    db,
                    instance_id=linked_row[0],
                    updates={
                        "session_doc_id": None,
                        "session_doc_policy": None,
                        "continuity_binding_source": None,
                    },
                    mutation_type="continuity_binding_changed",
                    write_source="api",
                    actor="delete-session-doc",
                )
            # Delete from DB
            await db.execute("DELETE FROM session_documents WHERE id = ?", (doc_id,))
            await db.commit()

            # Remove file
            fp = Path(row[0])
            if fp.exists():
                fp.unlink()

            await log_event(
                "session_doc_deleted", details={"doc_id": doc_id, "title": row[1], "hard": True}
            )
            logger.info(f"Hard deleted session doc {doc_id}: {row[1]}")
            return {"id": doc_id, "deleted": True, "hard": True}
        else:
            await db.execute(
                "UPDATE session_documents SET status = 'archived', updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), doc_id),
            )
            await db.commit()

            await log_event("session_doc_archived", details={"doc_id": doc_id, "title": row[1]})
            logger.info(f"Archived session doc {doc_id}: {row[1]}")
            return {"id": doc_id, "archived": True}


@app.post("/api/session-docs/{doc_id}/merge")
async def merge_into_session_doc(doc_id: int, request: SessionDocMergeRequest):
    """Intelligently merge content into a session document using LLM."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT file_path, title FROM session_documents WHERE id = ?", (doc_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Session document not found")

    fp = Path(row[0])
    if not fp.exists():
        raise HTTPException(404, f"File not found: {fp}")

    current_content = fp.read_text()
    context_hint = f"\nContext: {request.context}" if request.context else ""

    system_prompt = """You are a document editor for a session planning document. You will receive the current document and new content to merge in.

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

        # Agent-initiated merges are a "doc touched" signal — flip the inverse
        # flag back to True so the next GT cycle sees a current doc.
        if request.source == "agent":
            try:
                await asyncio.to_thread(bump_session_doc_up_to_date, fp, True)
            except Exception as exc:
                logger.debug(f"merge: bump session_doc_up_to_date failed: {exc}")

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE session_documents SET updated_at = ? WHERE id = ?",
                (datetime.now().isoformat(), doc_id),
            )
            await db.commit()

        await log_event(
            "session_doc_merged",
            details={
                "doc_id": doc_id,
                "source": request.source,
                "content_length": len(request.content),
            },
        )
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
        cursor = await db.execute(
            "SELECT id, session_doc_id, workflow_state FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        inst_row = await cursor.fetchone()
        if not inst_row:
            raise HTTPException(status_code=404, detail=f"Instance {instance_id} not found")

        old_doc_id = inst_row[1]
        workflow_state = inst_row[2]

        # Verify doc exists
        cursor = await db.execute("SELECT id FROM session_documents WHERE id = ?", (doc_id,))
        if not await cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"Session doc {doc_id} not found")

        # Assign
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={
                "session_doc_id": doc_id,
                "session_doc_policy": "manual_assigned",
                "continuity_binding_source": "manual",
            },
            mutation_type="continuity_binding_changed",
            write_source="api",
            actor="assign-doc",
            workflow_events=[
                {
                    "workflow_state": workflow_state,
                    "event_type": "continuity_binding_changed",
                    "event_owner": "api",
                    "details": {
                        "old_session_doc_id": old_doc_id,
                        "new_session_doc_id": doc_id,
                        "continuity_binding_source": "manual",
                    },
                },
                {
                    "workflow_state": workflow_state,
                    "event_type": "session_doc_bound",
                    "event_owner": "api",
                    "details": {
                        "session_doc_id": doc_id,
                        "session_doc_policy": "manual_assigned",
                        "continuity_binding_source": "manual",
                    },
                },
            ],
        )
        await db.commit()

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
        cursor = await db.execute(
            "SELECT id, session_doc_id, workflow_state FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        inst_row = await cursor.fetchone()
        if not inst_row:
            raise HTTPException(status_code=404, detail=f"Instance {instance_id} not found")

        old_doc_id = inst_row[1]
        workflow_state = inst_row[2]

    # Create the doc by reusing the create endpoint logic

    if request.file_path:
        fp = Path(request.file_path)
    else:
        fp = unique_human_path(DEFAULT_SESSIONS_DIR, request.title)

    if fp.exists():
        raise HTTPException(status_code=409, detail=f"File already exists: {fp}")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
               VALUES (?, ?, ?, 'active', ?, ?)""",
            (
                request.title,
                str(fp),
                request.project,
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )
        doc_id = cursor.lastrowid

        # Assign to instance
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={
                "session_doc_id": doc_id,
                "session_doc_policy": "manual_created",
                "continuity_binding_source": "manual",
            },
            mutation_type="continuity_binding_changed",
            write_source="api",
            actor="create-doc",
            workflow_events=[
                {
                    "workflow_state": workflow_state,
                    "event_type": "continuity_binding_changed",
                    "event_owner": "api",
                    "details": {
                        "old_session_doc_id": old_doc_id,
                        "new_session_doc_id": doc_id,
                        "continuity_binding_source": "manual",
                    },
                },
                {
                    "workflow_state": workflow_state,
                    "event_type": "session_doc_bound",
                    "event_owner": "api",
                    "details": {
                        "session_doc_id": doc_id,
                        "session_doc_policy": "manual_created",
                        "continuity_binding_source": "manual",
                    },
                },
            ],
        )
        await db.commit()

    create_session_doc_file(fp, request.title, doc_id, request.project)

    # Handle orphan cleanup for old doc
    if old_doc_id:
        await _handle_orphan_doc(old_doc_id)

    await log_event(
        "session_doc_created",
        instance_id=instance_id,
        details={
            "doc_id": doc_id,
            "title": request.title,
            "file_path": str(fp),
            "auto_assigned": True,
        },
    )
    logger.info(f"Created session doc {doc_id}: {request.title} and assigned to {instance_id}")

    return {
        "id": doc_id,
        "title": request.title,
        "file_path": str(fp),
        "instance_id": instance_id,
        "status": "active",
    }


@app.delete("/api/instances/{instance_id}/unassign-doc")
async def unassign_doc_from_instance(instance_id: str):
    """Unlink a session document from an instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, session_doc_id, workflow_state FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        inst_row = await cursor.fetchone()
        if not inst_row:
            raise HTTPException(status_code=404, detail=f"Instance {instance_id} not found")

        old_doc_id = inst_row[1]
        workflow_state = inst_row[2]
        if not old_doc_id:
            return {
                "instance_id": instance_id,
                "unassigned": False,
                "reason": "No doc was assigned",
            }

        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={
                "session_doc_id": None,
                "session_doc_policy": None,
                "continuity_binding_source": None,
            },
            mutation_type="continuity_binding_changed",
            write_source="api",
            actor="unassign-doc",
            workflow_events=[
                {
                    "workflow_state": workflow_state,
                    "event_type": "continuity_binding_changed",
                    "event_owner": "api",
                    "details": {
                        "old_session_doc_id": old_doc_id,
                        "new_session_doc_id": None,
                        "continuity_binding_source": None,
                    },
                },
            ],
        )
        await db.commit()

    # Handle orphan cleanup
    await _handle_orphan_doc(old_doc_id)

    await log_event(
        "session_doc_unassigned", instance_id=instance_id, details={"doc_id": old_doc_id}
    )
    logger.info(f"Unassigned instance {instance_id} from session doc {old_doc_id}")

    return {"instance_id": instance_id, "doc_id": old_doc_id, "unassigned": True}


@app.get("/api/instances/{instance_id}/session-doc")
async def get_instance_session_doc(instance_id: str):
    """Get the session document linked to this instance."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT session_doc_id FROM claude_instances WHERE id = ?", (instance_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Instance not found")
        if not row[0]:
            return {"session_doc_id": None}

        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM session_documents WHERE id = ?", (row[0],))
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
                (p["name"],),
            )
            link_row = await cursor.fetchone()
            active_doc = None
            if link_row:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, title, file_path, status FROM session_documents WHERE id = ?",
                    (link_row[0],),
                )
                doc_row = await cursor.fetchone()
                if doc_row:
                    active_doc = dict(doc_row)
                db.row_factory = None

            result.append(
                {
                    "name": p["name"],
                    "title": p["title"],
                    "aliases": p["aliases"],
                    "vault": p["vault"],
                    "role": p["role"],
                    "instance_name_prefix": p["instance_name_prefix"],
                    "vault_note_path": p.get("vault_note_path"),
                    "active_doc": active_doc,
                }
            )
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
            (p["name"],),
        )
        link_row = await cursor.fetchone()
        active_doc = None
        if link_row:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, title, file_path, status FROM session_documents WHERE id = ?",
                (link_row[0],),
            )
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
            (name,),
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
    title: str | None = None


@app.post("/api/primarchs/{name}/link-doc")
async def link_primarch_doc(
    name: str, doc_id: int | None = None, request: PrimarchLinkDocRequest = None
):
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
                (name, now, doc_id),
            )
        elif request and request.title:
            # Create new doc + link
            fp = unique_human_path(DEFAULT_SESSIONS_DIR, request.title)
            if fp.exists():
                raise HTTPException(409, f"File already exists: {fp}")
            cursor = await db.execute(
                """INSERT INTO session_documents (title, file_path, primarch_name, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'active', ?, ?)""",
                (request.title, str(fp), name, now, now),
            )
            target_doc_id = cursor.lastrowid
            create_session_doc_file(fp, request.title, target_doc_id, primarch_name=name)
        else:
            raise HTTPException(400, "Provide doc_id query param or {title} in body")

        # Unlink previous active doc
        await db.execute(
            "UPDATE primarch_session_docs SET unlinked_at = ? WHERE primarch_name = ? AND unlinked_at IS NULL",
            (now, name),
        )
        # Create new link
        await db.execute(
            "INSERT INTO primarch_session_docs (primarch_name, session_doc_id, linked_at) VALUES (?, ?, ?)",
            (name, target_doc_id, now),
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
            (name,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"primarch": name, "unlinked": False, "reason": "No active doc linked"}

        doc_id = row[0]
        await db.execute(
            "UPDATE primarch_session_docs SET unlinked_at = ? WHERE primarch_name = ? AND unlinked_at IS NULL",
            (now, name),
        )
        await db.execute(
            "UPDATE session_documents SET primarch_name = NULL, updated_at = ? WHERE id = ?",
            (now, doc_id),
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
        cursor = await db.execute(
            "SELECT id, status, title FROM session_documents WHERE id = ?", (doc_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, f"Session doc {doc_id} not found")
        if row[1] != "completed":
            raise HTTPException(400, f"Can only deploy completed docs, current status: {row[1]}")

        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE session_documents SET status = 'deployment', updated_at = ? WHERE id = ?",
            (now, doc_id),
        )
        await db.commit()

    await log_event("session_doc_deployed", details={"doc_id": doc_id, "title": row[2]})
    logger.info(f"Session doc {doc_id} ({row[2]}) moved to deployment")
    return {"id": doc_id, "status": "deployment"}


@app.post("/api/session-docs/{doc_id}/mark-processed")
async def mark_session_doc_processed(doc_id: int):
    """Mark a deployment doc as processed by Administratum. Unlinks primarch if linked."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, status, title, primarch_name FROM session_documents WHERE id = ?", (doc_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, f"Session doc {doc_id} not found")
        if row[1] != "deployment":
            raise HTTPException(
                400, f"Can only mark deployment docs as processed, current status: {row[1]}"
            )

        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE session_documents SET status = 'processed', primarch_name = NULL, updated_at = ? WHERE id = ?",
            (now, doc_id),
        )

        # Unlink primarch if this was linked
        if row[3]:
            await db.execute(
                "UPDATE primarch_session_docs SET unlinked_at = ? WHERE primarch_name = ? AND session_doc_id = ? AND unlinked_at IS NULL",
                (now, row[3], doc_id),
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


async def _get_fleet_state_row(db: aiosqlite.Connection) -> dict | None:
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
        cursor = await db.execute(
            """
            SELECT h.id, h.name, h.category, h.window_start_hour, h.window_end_hour, h.notes,
                   hc.completed_at, hc.notes AS completion_notes
            FROM habits h
            LEFT JOIN habit_completions hc ON hc.habit_id = h.id AND hc.date = ?
            WHERE h.active = 1
            ORDER BY h.window_start_hour, h.category, h.id
        """,
            (today,),
        )
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
        "summary": {
            "total": len(habits),
            "completed": completed_count,
            "pending": len(habits) - completed_count,
        },
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
        cursor = await db.execute(
            "SELECT id, name FROM habits WHERE id = ? AND active = 1", (habit_id,)
        )
        habit = await cursor.fetchone()
        if not habit:
            raise HTTPException(status_code=404, detail=f"Habit '{habit_id}' not found")

        if completed:
            await db.execute(
                """
                INSERT INTO habit_completions (habit_id, date, notes)
                VALUES (?, ?, ?)
                ON CONFLICT(habit_id, date) DO UPDATE SET completed_at = CURRENT_TIMESTAMP, notes = excluded.notes
            """,
                (habit_id, today, notes),
            )
        else:
            await db.execute(
                "DELETE FROM habit_completions WHERE habit_id = ? AND date = ?", (habit_id, today)
            )
        await db.commit()

    action = "completed" if completed else "uncompleted"
    return {"habit_id": habit_id, "date": today, "action": action, "notes": notes}


def _normalize_state_assertion(value) -> str | bool | int | float | None:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "yes", "y", "1", "on"):
            return True
        if normalized in ("false", "no", "n", "0", "off"):
            return False
        if normalized in ("none", "null", ""):
            return None
        return normalized
    return value


def _state_app_matches(app_name: str | None) -> bool:
    expected = (app_name or "").strip().lower()
    current = (PHONE_STATE.get("current_app") or "").strip().lower()
    if not expected:
        return False
    return PHONE_STATE.get("is_distracted", False) and current == expected


async def _state_validator_observed(request: StateValidateRequest):
    key = request.state or request.var or request.name
    if request.app and not key:
        return {
            "key": f"app.{request.app}",
            "observed": _state_app_matches(request.app),
            "details": {
                "current_app": PHONE_STATE.get("current_app"),
                "is_distracted": PHONE_STATE.get("is_distracted", False),
            },
        }

    if not key:
        raise HTTPException(status_code=400, detail="state, var, name, or app is required")

    key = key.strip().lower()
    work_state = None
    if key.startswith("work.") or key in ("productivity_active", "work_state.productivity_active"):
        work_state = await get_cached_work_state()

    observed_map = {
        "phone.current_app": PHONE_STATE.get("current_app"),
        "phone.app": PHONE_STATE.get("current_app"),
        "phone.is_distracted": PHONE_STATE.get("is_distracted", False),
        "phone.reachable": PHONE_STATE.get("reachable"),
        "desktop.current_mode": DESKTOP_STATE.get("current_mode", "silence"),
        "desktop.mode": DESKTOP_STATE.get("current_mode", "silence"),
        "desktop.work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "work_mode": DESKTOP_STATE.get("work_mode", "clocked_in"),
        "timer.mode": timer_engine.current_mode.value,
        "timer.activity": timer_engine.activity.value,
        "timer.break_in_backlog": timer_engine.break_balance_ms < 0,
        "timer.break_balance_ms": timer_engine.break_balance_ms,
    }
    if work_state:
        observed_map.update(
            {
                "work.productivity_active": work_state.productivity_active,
                "work.reason": work_state.reason,
                "work.active_instance_count": work_state.active_instance_count,
                "productivity_active": work_state.productivity_active,
                "work_state.productivity_active": work_state.productivity_active,
            }
        )
    if key.startswith("app."):
        app_name = key.split(".", 1)[1]
        observed_map[key] = _state_app_matches(app_name)
    if key.startswith("activity."):
        icon_key = key.split(".", 1)[1]
        observed_map[key] = next(
            (icon.active for icon in _activity_icons() if icon.key == icon_key),
            False,
        )

    if key not in observed_map:
        raise HTTPException(status_code=400, detail=f"unknown state key: {key}")
    return {"key": key, "observed": observed_map[key], "details": {}}


def _state_validate_request_from_query(request: Request) -> StateValidateRequest:
    params = request.query_params
    return StateValidateRequest.model_validate(
        {
            "state": params.get("state"),
            "var": params.get("var"),
            "name": params.get("name"),
            "app": params.get("app"),
            "assert": params.get("assert"),
        }
    )


async def _validate_state_response(request: Request, assertion: StateValidateRequest):
    observed = await _state_validator_observed(assertion)
    expected = _normalize_state_assertion(assertion.assert_)
    actual = _normalize_state_assertion(observed["observed"])
    matched = actual == expected
    payload = {
        "match": matched,
        "key": observed["key"],
        "expected": expected,
        "observed": actual,
        "details": observed["details"],
    }
    await log_event(
        "state_validate",
        details={
            **payload,
            "method": request.method,
            "client": request.client.host if request.client else None,
        },
    )
    return JSONResponse(status_code=200 if matched else 409, content=payload)


@app.api_route("/api/state/validate", methods=["GET", "POST"])
async def validate_state(
    request: Request,
    assertion: StateValidateRequest | None = Body(default=None),
):
    """
    Generic state assertion endpoint for MacroDroid and other pingers.

    Accepts JSON body or query parameters. Returns 200 when the assertion
    matches and 409 when it does not, so automations can branch on HTTP code.
    """
    assertion = assertion or _state_validate_request_from_query(request)
    return await _validate_state_response(request, assertion)


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
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        row = await cursor.fetchone()
        active_count = row[0] if row else 0

        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status = 'processing'"
        )
        row = await cursor.fetchone()
        processing_count = row[0] if row else 0

        # Habits
        cursor = await db.execute(
            """
            SELECT h.window_start_hour, h.window_end_hour,
                   hc.completed_at
            FROM habits h
            LEFT JOIN habit_completions hc ON hc.habit_id = h.id AND hc.date = ?
            WHERE h.active = 1
        """,
            (today,),
        )
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


def _askq_label(state: dict | None) -> str:
    if not state:
        return "an instance"
    label = state.get("instance_label") or state.get("tab_name")
    return label or (state.get("instance_id") or "")[:12] or "an instance"


async def _askq_level1_callback(instance_id: str, question_text: str, state: dict) -> None:
    """Level 1 of the AskUserQuestion ladder: Discord nudge.

    The TTS re-read fires inline in routes/hooks.py. This callback owns the
    Discord nudge so the Emperor sees the open question even if TTS was missed.
    """
    label = _askq_label(state)
    snippet = question_text.strip().splitlines()[0] if question_text else ""
    if len(snippet) > 160:
        snippet = snippet[:157] + "..."
    msg = f"Open question on **{label}**: {snippet}" if snippet else f"Open question on **{label}**"
    try:
        await dispatch_notification(
            NotifyRequest(
                message=f"You have an open question on {label}.",
                type="tts",
                instance_id=instance_id,
            )
        )
    except Exception as e:
        logger.warning(f"AskQ Level 1: notify failed: {e}")


hooks_init_deps(
    scheduler=scheduler,
    timer_engine=shared.timer_engine,
    timer_log_shift=shared.timer_log_shift,
    run_stop_evaluators=_run_stop_evaluators,
    auto_name_instance=_auto_name_instance,
    work_action_callback=hook_work_action_callback,
    schedule_golden_throne_callback=schedule_golden_throne_followup,
    golden_throne_activity_callback=golden_throne_user_activity,
    askq_level1_callback=_askq_level1_callback,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
