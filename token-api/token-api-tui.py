#!/usr/bin/env python3
"""
Token-API TUI: Terminal dashboard for Claude instance management.

Connects to existing Token-API server running on port 7777.

Controls:
  arrow/jk  - Select instance/cron job (up/down)
  g/G       - Jump to first/last
  [/]       - Switch table (Instances/Cron)
  h/l       - Switch info panel (Events/Logs/Deploy/Monitor)
  Enter     - Open selected instance in new terminal tab
  r         - Rename selected instance
  y         - Copy resume command to clipboard (yank)
  v         - Change voice for instance
  f         - Cycle filter (all/active/stopped)
  s         - Stop selected instance
  d         - Delete selected instance
  U         - Unstick frozen instance (SIGWINCH, gentle nudge)
  I         - Interrupt frozen instance (SIGINT, cancel op)
  K         - Kill deadlocked instance (SIGKILL, preserves terminal for /resume)
  R         - Restart Token-API server
  Ctrl+R    - Full refresh (restart server + reload TUI code)
  c         - Clear all instances
  o         - Change sort order
  q         - Quit
"""

import sys
import os
import re
import argparse
import json
import sqlite3
import subprocess
import time
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add the script directory to path for imports
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
from rich.prompt import Prompt
from rich.highlighter import JSONHighlighter
from dotenv import load_dotenv

# Load .env from same directory as this script (TOKEN_API_URL, etc.)
load_dotenv(Path(__file__).parent / ".env")

# API configuration — reads from env, defaults to localhost (Mac)
API_URL = os.environ.get("TOKEN_API_URL", "http://localhost:7777")
SERVER_PORT = 7777

# Database path (for direct queries like session doc lookup)
DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))

# Configuration
REFRESH_INTERVAL = 2  # seconds


# Resume copy feedback state
resume_feedback: Optional[tuple[float, str]] = None  # (timestamp, message)

# Unstick feedback state
unstick_feedback: Optional[tuple[float, str]] = None  # (timestamp, message)

# Restart feedback state
restart_feedback: Optional[tuple[float, str]] = None  # (timestamp, message)

# Timer display cache (for when API is unreachable)
_timer_cache = {
    "break_secs": 0,
    "backlog_secs": 0,
    "mode": "working",
    "work_mode": "clocked_in",
}

# Layout detection thresholds
MOBILE_TAILSCALE_IP = "100.102.92.24"
MOBILE_WIDTH_THRESHOLD = 60  # Below this = mobile mode
COMPACT_WIDTH_THRESHOLD = 100  # Below this (but above mobile) = compact mode
# Vertical threshold: character cells are ~2x taller than wide, so a "square" terminal
# in pixels has aspect ratio ~2.0 in characters. We favor vertical mode - only clearly
# wide terminals (aspect > 2.5) get full mode. Square-ish terminals stay vertical.
VERTICAL_ASPECT_RATIO_THRESHOLD = 2.5

# Global state
selected_index = 0
instances_cache = []
todos_cache = {}  # instance_id -> last known todos data (persists when not polling)
api_healthy = True
api_error_message = None
layout_mode = "full"  # "mobile", "vertical", "compact", or "full"
layout_mode_forced = False  # True if user used --mobile, --vertical, --compact, or --no-mobile
sort_mode = "recent_activity"  # "status", "recent_activity", "recent_stopped", "created"
filter_mode = "all"  # "all", "active", "stopped"
show_subagents = False  # Hide subagents by default, toggle with 'a'
global_tts_mode = "verbose"  # Cached from API
table_mode = "instances"  # "instances" or "cron"
cron_selected_index = 0
panel_page = 0  # 0 = events view, 1 = server logs view, 2 = deploy logs view
PANEL_PAGE_MAX = 4  # 0=Events, 1=Logs, 2=Deploy, 3=Monitor, 4=Timer Stats
deploy_active = False
deploy_log_path = None
deploy_metadata = {}
deploy_previous_page = 0
deploy_auto_switched = False
DEPLOY_SCAN_DIR = Path.home() / "ProcAgentDir"
TUI_SIGNAL_DIR = Path.home() / ".claude"
TUI_SLOTS = ("desktop", "mobile")  # Two monitor slots
console = Console()


def detect_layout_mode() -> str:
    """Detect layout mode: 'mobile', 'vertical', 'compact', or 'full'.

    Priority:
    1. Phone SSH always gets mobile
    2. Very narrow (<60) always gets mobile
    3. Vertical/square orientation (aspect < 2.5) gets vertical (stacked panels)
    4. Medium width (60-100) gets compact (no sidebar)
    5. Wide + normal aspect gets full

    Note: Character cells are ~2x taller than wide, so a "square" terminal in pixels
    has aspect ratio ~2.0 in character terms. We favor vertical mode (threshold 2.5)
    so only clearly wide terminals get full mode.
    """
    ssh_client = os.environ.get("SSH_CLIENT", "")

    # Phone always mobile
    if ssh_client.startswith(MOBILE_TAILSCALE_IP + " "):
        return "mobile"

    width = console.size.width
    height = console.size.height

    # Very narrow always mobile
    if width < MOBILE_WIDTH_THRESHOLD:
        return "mobile"

    # Check vertical orientation (tall terminal in character terms)
    is_vertical = height > 0 and (width / height) < VERTICAL_ASPECT_RATIO_THRESHOLD

    # Vertical monitor gets dedicated vertical mode (stacked panels)
    if is_vertical:
        return "vertical"

    # Medium width gets compact (no sidebar but horizontal header)
    if width < COMPACT_WIDTH_THRESHOLD:
        return "compact"

    return "full"


ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return ANSI_ESCAPE_RE.sub('', text)


def check_deploy_status() -> tuple[bool, Path | None, dict]:
    """Check for active deployment by scanning for .claude-deploy-signal files."""
    try:
        if not DEPLOY_SCAN_DIR.exists():
            return False, None, {}
        for entry in DEPLOY_SCAN_DIR.iterdir():
            if entry.is_dir():
                signal = entry / ".claude-deploy-signal"
                if signal.exists():
                    log = entry / ".claude-deploy.log"
                    try:
                        metadata = json.loads(signal.read_text())
                    except Exception:
                        metadata = {}
                    return True, log, metadata
    except Exception:
        pass
    return False, None, {}


def check_tui_restart_signal(slot: str) -> dict | None:
    """Check for a TUI restart signal file for the given slot."""
    signal_file = TUI_SIGNAL_DIR / f"tui-restart-{slot}.signal"
    try:
        if signal_file.exists():
            try:
                metadata = json.loads(signal_file.read_text())
            except Exception:
                metadata = {"reason": "unknown"}
            signal_file.unlink(missing_ok=True)
            return metadata
    except Exception:
        pass
    return None


def check_api_health() -> tuple[bool, str | None]:
    """Check if the API server is reachable."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/instances", method="GET")
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                return True, None
            return False, f"API returned status {response.status}"
    except urllib.error.URLError as e:
        if "Connection refused" in str(e):
            return False, f"API server not running (port 7777)"
        return False, f"Cannot reach API: {e.reason}"
    except Exception as e:
        return False, f"Health check failed: {str(e)}"


def format_duration(start_time_str: str, end_time_str: str = None) -> str:
    """Format duration from start time to now or end time."""
    try:
        start = datetime.fromisoformat(start_time_str.replace("Z", "+00:00").replace("T", " ").split(".")[0])
        if end_time_str:
            end = datetime.fromisoformat(end_time_str.replace("Z", "+00:00").replace("T", " ").split(".")[0])
        else:
            end = datetime.now()

        delta = end - start
        total_seconds = int(delta.total_seconds())

        if total_seconds < 0:
            return "0m"

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
    except Exception:
        return "?"


def format_duration_colored(start_time_str: str, end_time_str: str = None) -> str:
    """Format duration with color based on age: green <30m, yellow 30m-2h, dim >2h."""
    duration = format_duration(start_time_str, end_time_str)
    try:
        start = datetime.fromisoformat(start_time_str.replace("Z", "+00:00").replace("T", " ").split(".")[0])
        end = datetime.fromisoformat(end_time_str.replace("Z", "+00:00").replace("T", " ").split(".")[0]) if end_time_str else datetime.now()
        total_minutes = int((end - start).total_seconds()) // 60
    except Exception:
        return duration
    if total_minutes < 30:
        return f"[green]{duration}[/green]"
    elif total_minutes < 120:
        return f"[yellow]{duration}[/yellow]"
    else:
        return f"[dim]{duration}[/dim]"


def _is_stale_instance(instance: dict) -> bool:
    """Check if a stopped instance should be hidden (pre-today or unnamed 0m run)."""
    if instance.get("status") not in ("stopped",):
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    # Check if registered before today
    reg = instance.get("registered_at", "")
    if reg and not reg.startswith(today):
        return True
    # Check for unnamed 0m runs (auto-named "Claude HH:MM" with 0m duration)
    tab_name = instance.get("tab_name", "")
    if not is_custom_tab_name(tab_name):
        stopped_at = instance.get("stopped_at") or instance.get("last_activity", "")
        dur = format_duration(reg, stopped_at if stopped_at else None)
        if dur == "0m":
            return True
    return False


def filter_instances(instances: list) -> list:
    """Filter instances based on current filter_mode and subagent visibility."""
    # First filter by subagent visibility
    if not show_subagents:
        instances = [i for i in instances if not i.get("is_subagent")]

    # Hide stale instances (pre-today stopped, unnamed 0m runs)
    instances = [i for i in instances if not _is_stale_instance(i)]

    if filter_mode == "all":
        return instances
    elif filter_mode == "active":
        return [i for i in instances if i.get("status") in ("processing", "idle")]
    elif filter_mode == "stopped":
        return [i for i in instances if i.get("status") == "stopped"]
    return instances


def is_custom_tab_name(tab_name: str) -> bool:
    """Check if tab_name is a custom name (not auto-generated like 'Claude HH:MM')."""
    import re
    if not tab_name:
        return False
    # Auto-generated names match "Claude HH:MM" pattern
    if re.match(r'^Claude \d{2}:\d{2}$', tab_name):
        return False
    return True


def format_instance_name(instance: dict, max_len: int = 20) -> str:
    """Format instance name, prioritizing custom tab_name over working_dir."""
    tab_name = instance.get("tab_name", "")

    # If user has set a custom name, always use it
    if is_custom_tab_name(tab_name):
        if len(tab_name) > max_len:
            return tab_name[:max_len - 3] + "..."
        return tab_name

    # Otherwise derive from working_dir
    working_dir = instance.get("working_dir")
    if working_dir:
        # Extract the last 2-3 path components for a readable name
        parts = working_dir.rstrip("/").split("/")
        # Filter out empty parts and common prefixes like 'home', 'mnt', 'c', etc.
        parts = [p for p in parts if p and p not in ("home", "mnt", "c", "Users")]
        if len(parts) >= 2:
            name = "/".join(parts[-2:])  # Last two components
        elif parts:
            name = parts[-1]
        else:
            name = working_dir
        if len(name) > max_len:
            name = "..." + name[-(max_len - 3):]
        return name
    # Fallback to tab_name or id
    return tab_name or instance.get("id", "?")[:max_len]


def get_instances():
    """Fetch all instances from the API with current sort order."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/instances?sort={sort_mode}")
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.loads(response.read().decode())
    except Exception:
        return []


def get_instance_todos(instance_id: str, use_cache: bool = False) -> dict:
    """Fetch todos for an instance from the API.

    If use_cache=True and data is cached, returns cached data without polling.
    If use_cache=True but no cached data exists, fetches fresh data to seed the cache.
    If use_cache=False, always fetches fresh data and updates the cache.
    """
    global todos_cache
    default = {"progress": 0, "current_task": None, "total": 0, "todos": []}

    if use_cache and instance_id in todos_cache:
        return todos_cache[instance_id]

    try:
        req = urllib.request.Request(f"{API_URL}/api/instances/{instance_id}/todos")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode())
            todos_cache[instance_id] = data  # Update cache with fresh data
            return data
    except Exception:
        return todos_cache.get(instance_id, default)  # On error, return cached or default


def rename_instance(instance_id: str, new_name: str) -> bool:
    """Rename an instance via the API."""
    try:
        data = json.dumps({"tab_name": new_name}).encode()
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/rename",
            data=data,
            headers={"Content-Type": "application/json"},
            method="PATCH"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            return result.get("status") == "renamed"
    except Exception:
        return False


def delete_instance(instance_id: str) -> bool:
    """Delete/stop an instance via the API."""
    try:
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}",
            method="DELETE"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            return result.get("status") == "stopped"
    except Exception:
        return False


def kill_instance(instance_id: str) -> dict:
    """Kill a frozen instance via the API. Returns result dict or None on failure."""
    try:
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/kill",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=b"{}"
        )
        resp = urllib.request.urlopen(req, timeout=20)  # longer timeout for SIGINT×2 sequence
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return {"status": "error", "detail": body.get("detail", str(e))}
        except Exception:
            return {"status": "error", "detail": str(e)}
    except Exception:
        return None


def unstick_instance(instance_id: str, level: int = 1) -> dict:
    """Nudge a stuck instance. Level 1=SIGWINCH (gentle), Level 2=SIGINT (cancel op). Returns result dict or None on failure."""
    try:
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/unstick?level={level}",
            method="POST",
            headers={"Content-Type": "application/json"},
            data=b"{}"
        )
        resp = urllib.request.urlopen(req, timeout=10)  # 4s server wait + margin
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
            return {"status": "error", "detail": body.get("detail", str(e))}
        except Exception:
            return {"status": "error", "detail": str(e)}
    except Exception:
        return None


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Copy text to clipboard. Returns (success, message)."""
    # Try clip.exe first (WSL)
    try:
        subprocess.run(["clip.exe"], input=text, text=True, check=True, timeout=2)
        return (True, "Copied to clipboard")
    except FileNotFoundError:
        pass
    except Exception as e:
        pass

    # Try xclip
    try:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True, check=True, timeout=2)
        return (True, "Copied to clipboard")
    except FileNotFoundError:
        pass
    except Exception as e:
        pass

    # Try xsel
    try:
        subprocess.run(["xsel", "--clipboard", "--input"], input=text, text=True, check=True, timeout=2)
        return (True, "Copied to clipboard")
    except FileNotFoundError:
        pass
    except Exception as e:
        return (False, f"Copy failed: {str(e)[:25]}")

    return (False, "No clipboard tool (need clip.exe/xclip/xsel)")


def get_available_voices() -> list:
    """Get list of available voices from the API."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/voices")
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            return result.get("voices", [])
    except Exception:
        return []


def change_instance_voice(instance_id: str, voice: str) -> dict:
    """Change an instance's TTS voice via the API.

    Returns dict with 'success', 'changes' (list of bumps), or None on error.
    """
    try:
        data = json.dumps({"voice": voice}).encode()
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/voice",
            data=data,
            headers={"Content-Type": "application/json"},
            method="PATCH"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            if result.get("status") in ("voice_changed", "no_change"):
                return {
                    "success": True,
                    "changes": result.get("changes", []),
                    "status": result.get("status")
                }
            return {"success": False}
    except Exception:
        return {"success": False}


def cycle_instance_tts_mode(instance_id: str, current_mode: str) -> dict | None:
    """Cycle TTS mode: verbose -> muted -> silent -> verbose."""
    mode_cycle = {"verbose": "muted", "muted": "silent", "silent": "verbose"}
    new_mode = mode_cycle.get(current_mode, "muted")
    try:
        data = json.dumps({"mode": new_mode}).encode()
        req = urllib.request.Request(
            f"{API_URL}/api/instances/{instance_id}/tts-mode",
            data=data,
            headers={"Content-Type": "application/json"},
            method="PATCH"
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            result = json.loads(response.read().decode())
            return result
    except Exception:
        return None


def refresh_global_tts_mode():
    """Fetch global TTS mode from server."""
    global global_tts_mode
    try:
        req = urllib.request.Request(f"{API_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=1) as response:
            data = json.loads(response.read().decode())
            global_tts_mode = data.get("tts_global_mode", "verbose")
    except Exception:
        pass


def cycle_global_tts_mode() -> dict | None:
    """Cycle global TTS mode: verbose -> muted -> silent -> verbose."""
    global global_tts_mode
    mode_cycle = {"verbose": "muted", "muted": "silent", "silent": "verbose"}
    new_mode = mode_cycle.get(global_tts_mode, "muted")
    try:
        data = json.dumps({"mode": new_mode}).encode()
        req = urllib.request.Request(
            f"{API_URL}/api/tts/global-mode",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            result = json.loads(response.read().decode())
            global_tts_mode = result.get("mode", global_tts_mode)
            return result
    except Exception:
        return None


def delete_all_instances() -> tuple[bool, int]:
    """Delete all instances via the API. Returns (success, count)."""
    try:
        req = urllib.request.Request(
            f"{API_URL}/api/instances/all",
            method="DELETE"
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
            if result.get("status") in ("deleted_all", "no_instances"):
                return True, result.get("deleted_count", 0)
            return False, 0
    except Exception:
        return False, 0


def get_recent_events(limit: int = 5):
    """Fetch recent events from the API with instance names."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/events/recent?limit={limit}")
        with urllib.request.urlopen(req, timeout=3) as response:
            return json.loads(response.read().decode())
    except Exception:
        return []


def format_event_instance_name(event: dict, max_len: int = 15) -> str:
    """Format instance name for event display using joined instance data or fallbacks."""
    instance_id = event.get("instance_id", "")
    details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}

    # First check joined instance data (from LEFT JOIN)
    tab_name = event.get("instance_tab_name")
    working_dir = event.get("instance_working_dir")

    # If instance still exists and has a custom name, use it
    if is_custom_tab_name(tab_name):
        if len(tab_name) > max_len:
            return tab_name[:max_len - 2] + ".."
        return tab_name

    # Check details for name (some events store it there)
    details_name = details.get("tab_name") or details.get("new_name")
    if is_custom_tab_name(details_name):
        if len(details_name) > max_len:
            return details_name[:max_len - 2] + ".."
        return details_name

    # Derive from working_dir if available
    if working_dir:
        parts = working_dir.rstrip("/").split("/")
        parts = [p for p in parts if p and p not in ("home", "mnt", "c", "Users")]
        if parts:
            name = parts[-1]
            if len(name) > max_len:
                name = name[:max_len - 2] + ".."
            return name

    # Fallback to truncated ID
    if instance_id:
        return instance_id[:8] + ".." if len(instance_id) > 10 else instance_id
    return "system"


def get_tts_queue_status():
    """Fetch TTS queue status from the API."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/notify/queue/status")
        with urllib.request.urlopen(req, timeout=2) as response:
            return json.loads(response.read().decode())
    except Exception:
        return {"current": None, "queue": [], "queue_length": 0}


def _read_timer() -> dict:
    """Read live timer state from the in-memory timer via API.
    Falls back to cached values if API is unreachable."""
    global _timer_cache
    try:
        req = urllib.request.Request(f"{API_URL}/api/timer")
        with urllib.request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read().decode())
        bal_ms = data.get("break_balance_ms", data.get("accumulated_break_ms", 0) - data.get("break_backlog_ms", 0))
        _timer_cache = {
            "break_secs": round(max(0, bal_ms) / 1000),
            "backlog_secs": round(abs(min(0, bal_ms)) / 1000),
            "mode": data.get("current_mode", "working"),
            "work_mode": data.get("work_mode", "clocked_in"),
            "desktop_mode": data.get("desktop_mode", "silence"),
            "phone_app": data.get("phone_app"),
            "location_zone": data.get("location_zone"),
            "activity": data.get("activity", "working"),
            "productivity_active": data.get("productivity_active", False),
            "ahk_reachable": data.get("ahk_reachable"),
        }
    except Exception:
        pass
    return _timer_cache


def utc_to_local_timestr(utc_str: str) -> str:
    """Convert UTC timestamp string (from SQLite CURRENT_TIMESTAMP) to local HH:MM."""
    try:
        # SQLite format: "2026-02-16 19:47:00"
        dt_utc = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        dt_local = dt_utc.astimezone()
        return dt_local.strftime("%H:%M")
    except Exception:
        # Fallback: return raw time portion
        if " " in utc_str:
            return utc_str.split(" ")[1][:5]
        return utc_str[:5] if utc_str else "??:??"


def format_break_time(seconds: int) -> str:
    """Format break time as HH:MM:SS or MM:SS."""
    abs_secs = abs(seconds) if seconds else 0
    if abs_secs == 0:
        return "00:00"
    hours = abs_secs // 3600
    minutes = (abs_secs % 3600) // 60
    secs = abs_secs % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def break_balance_style(break_secs: int, backlog_secs: int) -> str:
    """Return Rich style string for break balance color coding."""
    if backlog_secs > 0:
        return "bold magenta"
    if break_secs > 3600:
        return "bold green"
    elif break_secs > 1800:
        return "green"
    elif break_secs > 900:
        return "yellow"
    else:
        return "bold red"


def get_timer_header_text() -> Text:
    """Generate timer/mode display for header. Reads directly from in-memory timer via API."""
    state = _read_timer()
    break_secs = state["break_secs"]
    backlog_secs = state["backlog_secs"]
    obsidian_mode = state["mode"]
    work_mode = state["work_mode"]

    # Mode icons
    mode_icons = {
        "working": "💻",
        "multitasking": "📺",
        "idle": "💤",
        "break": "☕",
        "distracted": "⚠️",
        "sleeping": "🌙",
    }

    # Parse mode for display
    icon = mode_icons.get(obsidian_mode, "❓")
    mode_name = obsidian_mode.replace("_", " ").title()

    # Break time color: bold green >60min, green >30min, yellow >15min, bold red ≥0, bold magenta backlog
    is_backlog = backlog_secs > 0
    break_style = break_balance_style(break_secs, backlog_secs)
    break_str = format_break_time(backlog_secs if is_backlog else break_secs)

    # Work mode indicator
    if work_mode == "clocked_out":
        work_indicator = "[dim]OFF[/dim]"
    elif work_mode == "gym":
        work_indicator = "[magenta]GYM[/magenta]"
    else:
        work_indicator = ""

    # Build display text
    text = Text()
    text.append(f"{icon} ", style="bold")
    text.append(f"{mode_name}", style="bold white")
    text.append("  ", style="dim")
    text.append("⏱ ", style="dim")
    if is_backlog:
        text.append("BACKLOG ", style=break_style)
    text.append(break_str, style=break_style)
    if work_indicator:
        text.append(f"  {work_indicator}")

    # Mode distribution bar (compact, inline)
    shifts_data = _fetch_timer_shifts()
    mode_dist = shifts_data.get("mode_distribution", {}) if shifts_data else {}
    if mode_dist:
        text.append("  ")
        text.append_text(_mode_bar(mode_dist, width=20))
        # Inline legend (top 3 modes)
        total = sum(mode_dist.values())
        MODE_SHORTS = {
            "working": ("wrk", "bright_white"),
            "multitasking": ("multi", "yellow"),
            "idle": ("idle", "dim"),
            "break": ("brk", "blue"),
            "distracted": ("dist", "red"),
            "sleeping": ("slp", "dim"),
        }
        legend_parts = []
        for mode, secs in sorted(mode_dist.items(), key=lambda x: -x[1]):
            pct = round(secs / total * 100)
            if pct < 5:
                continue
            short, color = MODE_SHORTS.get(mode, (mode[-4:], "white"))
            legend_parts.append((f" {short}{pct}%", color))
        text.append(" ")
        for label, color in legend_parts[:3]:
            text.append(label, style=color)

    return text


def make_progress_bar(progress: int, width: int = 10) -> str:
    """Create a text-based progress bar."""
    if progress == 0:
        return "[dim]" + "─" * width + "[/dim]"

    filled = int(width * progress / 100)
    empty = width - filled

    if progress == 100:
        return f"[green]{'█' * filled}[/green]"
    else:
        return f"[cyan]{'█' * filled}[/cyan][dim]{'─' * empty}[/dim]"


def create_instances_table(instances: list, selected_idx: int) -> Table:
    """Create the instances table with selection and todo progress."""
    max_name_len = 15
    for inst in instances:
        name = format_instance_name(inst, max_len=30)
        max_name_len = max(max_name_len, len(name) + 2)

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=False
    )

    table.add_column("", width=2, justify="center")
    table.add_column("●", style="dim", width=1, justify="center")
    table.add_column("Name", style="white", width=max_name_len)
    table.add_column("Device", style="yellow", width=10)
    table.add_column("Progress", width=14)
    table.add_column("Task", style="dim", min_width=20, max_width=30)
    table.add_column("Time", width=6, justify="right")

    for i, instance in enumerate(instances):
        is_sub = instance.get("is_subagent")
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=30)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"
        elif is_sub:
            name = f"[dim]@ {name}[/dim]"

        device = instance.get("device_id", "?")
        instance_id = instance.get("id", "")
        status = instance.get("status", "idle")
        # Poll for fresh todos when processing, otherwise use cached data
        if status == "processing":
            todos = get_instance_todos(instance_id, use_cache=False)
        else:
            todos = get_instance_todos(instance_id, use_cache=True)

        has_active_subtask = todos.get("current_task") is not None

        if status == "stopped":
            status_icon = "[dim]o[/dim]"
        elif status == "processing" or has_active_subtask:
            status_icon = "[green]>[/green]"
        else:
            status_icon = "[cyan]*[/cyan]"

        if todos.get("total", 0) > 0:
            progress = todos.get("progress", 0)
            progress_bar = make_progress_bar(progress, 8)
            progress_text = f"{progress_bar} {progress}%"
        else:
            progress_text = "[dim]-[/dim]"

        current_task = todos.get("current_task", "")
        if current_task:
            if len(current_task) > 28:
                current_task = current_task[:25] + "..."
            current_task = f"[italic]{current_task}[/italic]"
        else:
            current_task = "[dim]-[/dim]"

        end_time = instance.get("stopped_at") if instance["status"] == "stopped" else None
        duration = format_duration_colored(instance.get("registered_at", ""), end_time)

        # Dim all columns for subagent rows
        if is_sub and i != selected_idx:
            device = f"[dim]{device}[/dim]"
            progress_text = "[dim]-[/dim]"
            current_task = "[dim]-[/dim]"
            duration = f"[dim]{duration}[/dim]"

        table.add_row(selector, status_icon, name, device, progress_text, current_task, duration)

    if not instances:
        table.add_row(" ", "[dim]-[/dim]", "[dim]No instances[/dim]", "-", "-", "-", "-")

    return table


def create_mobile_instances_table(instances: list, selected_idx: int) -> Table:
    """Create a compact instances table for mobile."""
    table = Table(
        title="Instances [dim](jk r s d c o q)[/dim]",
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=True,
        padding=(0, 0)
    )

    table.add_column("", width=1, justify="center")
    table.add_column("*", width=1, justify="center")
    table.add_column("Name", style="white", no_wrap=True, max_width=20)
    table.add_column("Prog", width=6)
    table.add_column("T", width=4, justify="right")

    for i, instance in enumerate(instances):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=18)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        instance_id = instance.get("id", "")
        status = instance.get("status", "idle")
        # Poll for fresh todos when processing, otherwise use cached data
        if status == "processing":
            todos = get_instance_todos(instance_id, use_cache=False)
        else:
            todos = get_instance_todos(instance_id, use_cache=True)

        has_active_subtask = todos.get("current_task") is not None

        if status == "stopped":
            status_icon = "[dim]o[/dim]"
        elif status == "processing" or has_active_subtask:
            status_icon = "[green]>[/green]"
        else:
            status_icon = "[cyan]*[/cyan]"

        if todos.get("total", 0) > 0:
            progress = todos.get("progress", 0)
            progress_bar = make_progress_bar(progress, 5)
        else:
            progress_bar = "[dim]-----[/dim]"

        end_time = instance.get("stopped_at") if status == "stopped" else None
        duration = format_duration_colored(instance.get("registered_at", ""), end_time)

        table.add_row(selector, status_icon, name, progress_bar, duration)

    if not instances:
        table.add_row(" ", "o", "[dim]None[/dim]", "-----", "-")

    return table


def _format_cron_schedule(job: dict) -> str:
    """Format cron job schedule as a compact string."""
    stype = job.get("schedule_type", "")
    sval = job.get("schedule_value", "")
    if stype == "interval" and sval:
        return sval  # Already compact like "15m", "2h"
    elif stype == "cron" and sval:
        return sval
    # Fallback for old format
    schedule = job.get("schedule", {})
    every_ms = schedule.get("everyMs", 0)
    if every_ms >= 3600000:
        return f"{every_ms // 3600000}h"
    elif every_ms >= 60000:
        return f"{every_ms // 60000}m"
    return schedule.get("cron", "?")


def _format_cron_next(job: dict) -> str:
    """Format next run countdown for a cron job."""
    # New local engine format: ISO string
    next_run_at = job.get("next_run_at")
    if next_run_at:
        try:
            next_dt = datetime.fromisoformat(next_run_at)
            secs_left = max(0, int((next_dt - datetime.now(next_dt.tzinfo)).total_seconds()))
        except (ValueError, TypeError):
            return "[dim]--:--[/dim]"
    else:
        # Fallback for old format
        state = job.get("state", {})
        next_run_ms = state.get("nextRunAtMs")
        if not next_run_ms:
            return "[dim]--:--[/dim]"
        secs_left = max(0, int((next_run_ms / 1000) - time.time()))

    mins, secs = divmod(secs_left, 60)
    if mins >= 60:
        hours = mins // 60
        mins = mins % 60
        cd_str = f"{hours}h{mins:02d}m"
    else:
        cd_str = f"{mins}:{secs:02d}"
    if secs_left <= 0:
        return "[green bold]NOW[/green bold]"
    elif secs_left <= 60:
        return f"[red bold]{cd_str}[/red bold]"
    elif secs_left <= 300:
        return f"[yellow]{cd_str}[/yellow]"
    return f"[cyan]{cd_str}[/cyan]"


def _format_cron_last(job: dict) -> str:
    """Format last run time for a cron job."""
    # Try new format: fetch from cached run history
    job_id = job.get("id", "")
    if job_id:
        runs = get_cached_cron_run_history(job_id, max_runs=1)
        if runs:
            started = runs[0].get("started_at", "")
            if started:
                try:
                    last_dt = datetime.fromisoformat(started)
                    last_ago = int((datetime.now() - last_dt).total_seconds())
                    if last_ago < 60:
                        return f"{last_ago}s ago"
                    elif last_ago < 3600:
                        return f"{last_ago // 60}m ago"
                    return f"{last_ago // 3600}h ago"
                except (ValueError, TypeError):
                    pass
    # Fallback for old format
    state = job.get("state", {})
    last_run_ms = state.get("lastRunAtMs")
    if not last_run_ms:
        return "[dim]--[/dim]"
    last_ago = int(time.time() - (last_run_ms / 1000))
    if last_ago < 60:
        return f"{last_ago}s ago"
    elif last_ago < 3600:
        return f"{last_ago // 60}m ago"
    return f"{last_ago // 3600}h ago"


def _format_cron_status(job: dict) -> str:
    """Format cron job status."""
    enabled = job.get("enabled")
    # Handle both bool and int (new engine uses 0/1)
    if enabled is not None and not enabled:
        return "[dim]disabled[/dim]"
    # New format: is_running bool
    if job.get("is_running"):
        return "[green bold]running[/green bold]"
    # Old format: state.status
    state = job.get("state", {})
    job_status = state.get("status", "idle")
    if job_status == "running":
        return "[green bold]running[/green bold]"
    return "[cyan]idle[/cyan]"


def create_cron_table(jobs: list, selected_idx: int) -> Table:
    """Create the cron jobs table (full layout)."""
    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=False
    )

    table.add_column("", width=2, justify="center")
    table.add_column("Name", style="white", min_width=15)
    table.add_column("Schedule", style="dim", width=8, justify="center")
    table.add_column("Next", width=10, justify="right")
    table.add_column("Last", style="dim", width=10, justify="right")
    table.add_column("Status", width=10)

    for i, job in enumerate(jobs):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = job.get("name", job.get("id", "?")[:12])
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        table.add_row(
            selector,
            name,
            _format_cron_schedule(job),
            _format_cron_next(job),
            _format_cron_last(job),
            _format_cron_status(job),
        )

    if not jobs:
        table.add_row(" ", "[dim]No cron jobs[/dim]", "-", "-", "-", "-")

    return table


def create_compact_cron_table(jobs: list, selected_idx: int) -> Table:
    """Create a compact cron jobs table (compact/vertical layout)."""
    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=True
    )

    table.add_column("", width=2, justify="center")
    table.add_column("Name", style="white")
    table.add_column("Next", width=10, justify="right")
    table.add_column("Last", style="dim", width=10, justify="right")

    for i, job in enumerate(jobs):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = job.get("name", job.get("id", "?")[:12])
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        table.add_row(
            selector,
            name,
            _format_cron_next(job),
            _format_cron_last(job),
        )

    if not jobs:
        table.add_row(" ", "[dim]No cron jobs[/dim]", "-", "-")

    return table


def create_mobile_cron_table(jobs: list, selected_idx: int) -> Table:
    """Create a mobile cron jobs table."""
    table = Table(
        title="Cron [dim](jk \\[\\] q)[/dim]",
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=True,
        padding=(0, 0)
    )

    table.add_column("", width=1, justify="center")
    table.add_column("Name", style="white", no_wrap=True, max_width=20)
    table.add_column("Next", width=8, justify="right")

    for i, job in enumerate(jobs):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = job.get("name", job.get("id", "?")[:12])
        if len(name) > 18:
            name = name[:15] + "..."
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        table.add_row(selector, name, _format_cron_next(job))

    if not jobs:
        table.add_row(" ", "[dim]No jobs[/dim]", "-")

    return table


def get_cron_run_history(job_id: str, max_runs: int = 5) -> list[dict]:
    """Fetch recent run records for a cron job from the API."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/cron/jobs/{job_id}/runs?limit={max_runs}")
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            return data.get("runs", [])
    except Exception:
        return []


# Cache run history (refresh every 15s alongside cron jobs)
_cron_runs_cache: dict[str, list] = {}
_cron_runs_cache_time: float = 0


def get_cached_cron_run_history(job_id: str, max_runs: int = 5) -> list[dict]:
    """Cached wrapper around get_cron_run_history."""
    global _cron_runs_cache, _cron_runs_cache_time
    now = time.time()
    if now - _cron_runs_cache_time > 15:
        _cron_runs_cache = {}
        _cron_runs_cache_time = now
    if job_id not in _cron_runs_cache:
        _cron_runs_cache[job_id] = get_cron_run_history(job_id, max_runs)
    return _cron_runs_cache[job_id]


def _wrap_summary_lines(text: str, width: int = 70) -> list[str]:
    """Split a summary into lines that fit within width, respecting markdown bullets."""
    raw_lines = text.split("\n")
    out = []
    for line in raw_lines:
        line = line.rstrip()
        # Strip markdown bold markers for cleaner display
        clean = line.replace("**", "")
        if not clean:
            continue
        # Wrap long lines
        while len(clean) > width:
            # Find a break point
            brk = clean.rfind(" ", 0, width)
            if brk <= 0:
                brk = width
            out.append(clean[:brk])
            clean = "  " + clean[brk:].lstrip()
        out.append(clean)
    return out


def create_cron_details_panel(job: dict, max_lines: int = 8) -> Panel:
    """Create a panel showing details for the selected cron job."""
    if not job:
        return Panel("[dim]No cron job selected[/dim]", title="Cron Details", border_style="magenta")

    lines = []
    name = job.get("name", job.get("id", "?"))
    job_id = job.get("id", "")
    enabled = job.get("enabled")
    schedule_str = _format_cron_schedule(job)
    is_running = job.get("is_running", False)

    # Header: name + status + quota info
    status_tag = "[green]enabled[/green]" if enabled else "[red]disabled[/red]"
    if is_running:
        status_tag = "[green bold]RUNNING[/green bold]"
    quota_tag = ""
    max_runs = job.get("max_runs_per_window")
    if max_runs:
        window = job.get("run_window_hours", 5)
        quota_tag = f"  [dim]quota: {max_runs}/{window}h[/dim]"
    quiet_start = job.get("quiet_hours_start")
    quiet_end = job.get("quiet_hours_end")
    quiet_tag = ""
    if quiet_start is not None and quiet_end is not None:
        quiet_tag = f"  [dim]quiet: {quiet_start}-{quiet_end}[/dim]"
    lines.append(f"[bold]{name}[/bold]  ({schedule_str})  {status_tag}{quota_tag}{quiet_tag}")

    # Last/next timing from run history
    runs = get_cached_cron_run_history(job_id, max_runs=3)
    if runs:
        latest = runs[0]
        started = latest.get("started_at", "")
        try:
            last_dt = datetime.fromisoformat(started)
            last_str = last_dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            last_str = "?"
        dur = latest.get("duration_seconds")
        dur_str = f" ({dur:.0f}s)" if dur else ""
        last_status = latest.get("status", "")
        result_style = "green" if last_status in ("ok", "success", "") else ("yellow" if last_status == "skipped" else "red")
        result_tag = f"[{result_style}]{last_status}[/{result_style}]" if last_status else ""
        skip_reason = latest.get("skip_reason", "")
        skip_tag = f" [dim]({skip_reason})[/dim]" if skip_reason else ""
        lines.append(f"Last: [cyan]{last_str}[/cyan]{dur_str} {result_tag}{skip_tag}  Next: {_format_cron_next(job)}")
    else:
        lines.append(f"[dim]No previous runs[/dim]  Next: {_format_cron_next(job)}")

    # Run transcript content
    if runs:
        latest = runs[0]
        summary = latest.get("output_summary", "") or latest.get("summary", "")
        error = latest.get("error_summary", "") or latest.get("error", "")

        if error:
            lines.append(f"[red]{error}[/red]")
        if summary:
            lines.append("")
            summary_lines = _wrap_summary_lines(summary, width=70)
            for sl in summary_lines:
                if len(lines) >= max_lines:
                    break
                # Color bullet lines
                stripped = sl.lstrip()
                if stripped.startswith("- "):
                    lines.append(f"[green]>[/green] {stripped[2:]}")
                else:
                    lines.append(sl)
        elif not error:
            lines.append("[dim]No summary from last run[/dim]")
    else:
        lines.append("[dim]No run history[/dim]")

    content = "\n".join(lines[:max_lines])
    return Panel(content, title="Cron Details", border_style="magenta")


def create_compact_cron_details_panel(job: dict) -> Panel:
    """Create a compact single-line cron details panel for vertical layout."""
    if not job:
        return Panel("[dim]No cron job selected[/dim]", title="Cron Details", border_style="magenta")

    name = job.get("name", job.get("id", "?"))
    job_id = job.get("id", "")
    schedule_str = _format_cron_schedule(job)

    parts = [f"[bold]{name}[/bold]"]
    parts.append(f"[dim]({schedule_str})[/dim]")

    if job.get("is_running"):
        parts.append("[green bold]RUNNING[/green bold]")
    elif not job.get("enabled"):
        parts.append("[red]disabled[/red]")

    # First line of last run summary
    runs = get_cached_cron_run_history(job_id, max_runs=1)
    if runs:
        latest = runs[0]
        error = latest.get("error_summary", "") or latest.get("error", "")
        summary = latest.get("output_summary", "") or latest.get("summary", "")
        if error:
            if len(error) > 40:
                error = error[:37] + "..."
            parts.append(f"[red]{error}[/red]")
        elif summary:
            # Grab first meaningful line
            for line in summary.split("\n"):
                line = line.strip().replace("**", "")
                if line and not line.startswith("#"):
                    if len(line) > 45:
                        line = line[:42] + "..."
                    parts.append(f"[dim]{line}[/dim]")
                    break

    content = "  ".join(parts)
    return Panel(content, title="Cron Details", border_style="magenta")


def create_mobile_cron_details_panel(job: dict) -> Panel:
    """Create a compact cron details panel for mobile."""
    if not job:
        return Panel("[dim]No selection[/dim]", title="Details", border_style="magenta", padding=(0, 1))

    lines = []
    name = job.get("name", job.get("id", "?"))
    job_id = job.get("id", "")

    status_icon = "[green]>[/green]" if job.get("is_running") else "[cyan]*[/cyan]"
    if not job.get("enabled"):
        status_icon = "[dim]-[/dim]"
    lines.append(f"{status_icon} [bold]{name}[/bold]  [dim]{_format_cron_schedule(job)}[/dim]")

    # Last run summary from transcript
    runs = get_cached_cron_run_history(job_id, max_runs=1)
    if runs:
        latest = runs[0]
        error = latest.get("error_summary", "") or latest.get("error", "")
        summary = latest.get("output_summary", "") or latest.get("summary", "")
        if error:
            if len(error) > 35:
                error = error[:32] + "..."
            lines.append(f"[red]{error}[/red]")
        elif summary:
            # First meaningful line
            for line in summary.split("\n"):
                line = line.strip().replace("**", "")
                if line and not line.startswith("#"):
                    if len(line) > 35:
                        line = line[:32] + "..."
                    lines.append(f"[dim]{line}[/dim]")
                    break
    else:
        lines.append(f"Next: {_format_cron_next(job)}")

    return Panel("\n".join(lines), title="Details", border_style="magenta", padding=(0, 1))


def create_compact_instances_table(instances: list, selected_idx: int) -> Table:
    """Create a compact instances table without Task column (for compact mode)."""
    max_name_len = 25
    for inst in instances:
        name = format_instance_name(inst, max_len=40)
        max_name_len = max(max_name_len, len(name) + 2)

    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="blue",
        expand=True
    )

    table.add_column("", width=2, justify="center")
    table.add_column("●", style="dim", width=1, justify="center")
    table.add_column("Name", style="white")  # Dynamic width - fills available space
    table.add_column("Device", style="yellow", width=10)
    table.add_column("Progress", width=14)
    table.add_column("Time", width=6, justify="right")

    for i, instance in enumerate(instances):
        selector = "[yellow]>[/yellow]" if i == selected_idx else " "
        name = format_instance_name(instance, max_len=35)
        if i == selected_idx:
            name = f"[bold yellow]{name}[/bold yellow]"

        device = instance.get("device_id", "?")
        instance_id = instance.get("id", "")
        status = instance.get("status", "idle")
        # Poll for fresh todos when processing, otherwise use cached data
        if status == "processing":
            todos = get_instance_todos(instance_id, use_cache=False)
        else:
            todos = get_instance_todos(instance_id, use_cache=True)

        has_active_subtask = todos.get("current_task") is not None

        if status == "stopped":
            status_icon = "[dim]o[/dim]"
        elif status == "processing" or has_active_subtask:
            status_icon = "[green]>[/green]"
        else:
            status_icon = "[cyan]*[/cyan]"

        if todos.get("total", 0) > 0:
            progress = todos.get("progress", 0)
            progress_bar = make_progress_bar(progress, 8)
            progress_text = f"{progress_bar} {progress}%"
        else:
            progress_text = "[dim]-[/dim]"

        end_time = instance.get("stopped_at") if status == "stopped" else None
        duration = format_duration_colored(instance.get("registered_at", ""), end_time)

        table.add_row(selector, status_icon, name, device, progress_text, duration)

    if not instances:
        table.add_row(" ", "[dim]-[/dim]", "[dim]No instances[/dim]", "-", "-", "-")

    return table


def create_events_panel(events: list) -> Panel:
    """Create the events panel."""
    lines = []

    EVENT_STYLES = {
        "instance_registered": ("green", "+", "registered"),
        "instance_stopped": ("red", "-", "stopped"),
        "instance_killed": ("red", "x", "killed"),
        "instance_unstick": ("cyan", "!", "nudged"),
        "instance_renamed": ("yellow", "~", "renamed"),
        "tts_queued": ("yellow", "o", "queued TTS"),
        "tts_playing": ("cyan", ">", "speaking"),
        "tts_completed": ("blue", "v", "TTS done"),
        "notification_sent": ("magenta", "*", "notified"),
        "sound_played": ("yellow", "~", "sound"),
        "phone_app_closed": ("blue", "📱", "closed"),
        "phone_distraction_allowed": ("yellow", "📱", "allowed"),
        "phone_distraction_blocked": ("red", "📱", "blocked"),
    }

    for event in events:
        try:
            created = event.get("created_at", "")
            time_str = utc_to_local_timestr(created) if created else "??:??"

            event_type = event.get("event_type", "unknown")
            details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}

            # Get human-readable name using the helper function
            display_name = format_event_instance_name(event, max_len=18)
            color, icon, action = EVENT_STYLES.get(event_type, ("dim", ".", event_type))

            if event_type == "instance_registered":
                msg = f"[{color}]{icon}[/{color}] [bold]{display_name}[/bold]: [green]registered[/green]"
            elif event_type == "instance_stopped":
                msg = f"[{color}]{icon}[/{color}] [bold]{display_name}[/bold]: [red]stopped[/red]"
            elif event_type == "instance_renamed":
                old_name = details.get("old_name", "?")
                new_name = details.get("new_name", "?")
                msg = f"[{color}]{icon}[/{color}] [bold]{old_name}[/bold] -> [bold]{new_name}[/bold]"
            elif event_type in ("tts_queued", "tts_playing", "tts_completed"):
                voice = details.get("voice", "").replace("Microsoft ", "").replace(" Desktop", "")
                msg = f"[{color}]{icon}[/{color}] [bold]{display_name}[/bold]: [{color}]{action}[/{color}]"
                if voice and event_type == "tts_playing":
                    msg += f" [dim]({voice})[/dim]"
            elif event_type in ("phone_app_closed", "phone_distraction_allowed", "phone_distraction_blocked"):
                app_display = details.get("display_name") or details.get("app", "?")
                reason = details.get("reason", "")
                msg = f"[{color}]{icon}[/{color}] [bold]{app_display}[/bold]: [{color}]{action}[/{color}]"
                if reason and event_type != "phone_app_closed":
                    msg += f" [dim]({reason})[/dim]"
            else:
                msg = f"[{color}]{icon}[/{color}] [bold]{display_name}[/bold]: [{color}]{action}[/{color}]"

            lines.append(f"[dim]{time_str}[/dim]  {msg}")
        except Exception:
            continue

    if not lines:
        lines.append("[dim]No recent events[/dim]")

    content = "\n".join(lines[:6])
    return Panel(content, title="Recent Events", border_style="blue")


def create_mobile_events_panel(events: list) -> Panel:
    """Create a compact events panel for mobile."""
    lines = []

    EVENT_ICONS = {
        "instance_registered": "[green]+[/green]",
        "instance_stopped": "[red]-[/red]",
        "instance_killed": "[red]x[/red]",
        "instance_unstick": "[cyan]![/cyan]",
        "instance_renamed": "[yellow]~[/yellow]",
        "tts_playing": "[cyan]>[/cyan]",
        "notification_sent": "[magenta]*[/magenta]",
        "phone_app_closed": "[blue]📱[/blue]",
        "phone_distraction_allowed": "[yellow]📱[/yellow]",
        "phone_distraction_blocked": "[red]📱[/red]",
    }

    for event in events[:4]:
        try:
            created = event.get("created_at", "")
            time_str = utc_to_local_timestr(created) if created else "??:??"

            event_type = event.get("event_type", "unknown")
            details = event.get("details", {}) if isinstance(event.get("details"), dict) else {}
            icon = EVENT_ICONS.get(event_type, "[dim].[/dim]")

            # Phone events: show app display name instead of instance name
            if event_type in ("phone_app_closed", "phone_distraction_allowed", "phone_distraction_blocked"):
                display_name = details.get("display_name") or details.get("app", "?")
            else:
                # Get human-readable name using the helper function
                display_name = format_event_instance_name(event, max_len=12)

            lines.append(f"[dim]{time_str}[/dim] {icon} {display_name}")
        except Exception:
            continue

    if not lines:
        lines.append("[dim]No events[/dim]")

    return Panel("\n".join(lines), title="Events", border_style="blue", padding=(0, 1))


def create_tts_queue_panel(queue_status: dict) -> Panel:
    """Create a compact one-row TTS queue panel showing instance names in order."""
    current = queue_status.get("current")
    queue = queue_status.get("queue", [])

    # Build compact queue string
    queue_items = []

    if current:
        current_name = current.get('tab_name', '?')
        if len(current_name) > 12:
            current_name = current_name[:10] + ".."
        queue_items.append(f"[yellow]{current_name}[/yellow]")

    for item in queue[:5]:  # Show max 5 queued items
        name = item.get('tab_name', '?')
        if len(name) > 12:
            name = name[:10] + ".."
        queue_items.append(name)

    if len(queue) > 5:
        queue_items.append(f"[dim]+{len(queue) - 5} more[/dim]")

    if queue_items:
        content = "Queue: " + " → ".join(queue_items)
    else:
        content = "[dim]Queue: (empty)[/dim]"

    return Panel(content, title="TTS Queue", border_style="yellow")


def create_server_logs_panel(max_lines: int = 8) -> Panel:
    """Create a panel showing recent server logs fetched from API."""
    json_highlighter = JSONHighlighter()

    try:
        req = urllib.request.Request(f"{API_URL}/api/logs/recent?limit={max_lines}")
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode())
            logs = data.get("logs", [])

            if not logs:
                content = Text("No server logs available", style="dim")
            else:
                # Format logs with timestamp and level colors
                # Build a Text object to support JSON highlighting
                content = Text()
                level_colors = {
                    "INFO": "green",
                    "WARN": "yellow",
                    "ERRO": "red",
                    "DEBU": "dim",
                    "CRIT": "red bold"
                }

                for i, log in enumerate(logs):
                    if i > 0:
                        content.append("\n")

                    timestamp = log.get("timestamp", "??:??:??")
                    level = log.get("level", "INFO")[:4]
                    message = log.get("message", "")
                    level_color = level_colors.get(level, "white")

                    # Add timestamp and level prefix
                    content.append(f"{timestamp} ", style="dim")
                    content.append(f"{level} ", style=level_color)

                    # Apply JSON highlighting to message if it might contain JSON
                    if '{' in message or '[' in message:
                        message_text = json_highlighter(Text(message))
                        content.append_text(message_text)
                    else:
                        content.append(message)

    except Exception:
        content = Text("Server logs unavailable", style="dim")

    return Panel(content, title="Server Logs", border_style="blue")


def create_deploy_logs_panel(max_lines: int = 8) -> Panel:
    """Create a panel showing deploy logs from .claude-deploy.log."""
    is_active, log_path, metadata = check_deploy_status()

    if not is_active or not log_path or not log_path.exists():
        content = Text("No deployment in progress", style="dim")
        return Panel(content, title="Deploy", border_style="blue")

    # Build title from metadata
    env = metadata.get("environment", "?")
    repo = metadata.get("repo", "")
    status_label = "RUNNING" if is_active else "COMPLETED"
    title_parts = ["Deploy"]
    if env:
        title_parts.append(f"[yellow]{env}[/yellow]")
    if repo:
        title_parts.append(f"[dim]{repo}[/dim]")
    title_parts.append(f"[bold green]{status_label}[/bold green]")
    title = " | ".join(title_parts)

    try:
        raw_lines = log_path.read_text().splitlines()
        # Tail: take the last N lines
        tail_lines = raw_lines[-max_lines:] if len(raw_lines) > max_lines else raw_lines

        if not tail_lines:
            content = Text("Deploy log is empty", style="dim")
            return Panel(content, title=title, border_style="yellow")

        content = Text()
        for i, raw_line in enumerate(tail_lines):
            if i > 0:
                content.append("\n")
            line = strip_ansi(raw_line)

            # Color lines based on content
            lower = line.lower()
            if "error" in lower or "fail" in lower or "fatal" in lower:
                content.append(line, style="red")
            elif "success" in lower or "deployed" in lower or "complete" in lower:
                content.append(line, style="green")
            elif "build" in lower or "step" in lower:
                content.append(line, style="cyan")
            elif "warn" in lower:
                content.append(line, style="yellow")
            else:
                content.append(line)

    except Exception:
        content = Text("Could not read deploy log", style="dim red")

    return Panel(content, title=title, border_style="yellow")


def create_instance_details_panel(instance: dict, todos_data: dict, compact: bool = False) -> Panel:
    """Create a panel showing details for the selected instance.

    If compact=True, shows a single-line summary suitable for bottom of vertical layout.
    """
    lines = []

    if not instance:
        return Panel("[dim]No instance selected[/dim]", title="Instance Details", border_style="magenta")

    name = format_instance_name(instance, max_len=25)
    status = instance.get("status", "unknown")
    device = instance.get("device_id", "?")
    if status == "stopped":
        status_icon = "[dim]o[/dim]"
    elif status == "processing":
        status_icon = "[green]>[/green]"
    else:
        status_icon = "[cyan]*[/cyan]"

    # Get TTS voice profile info
    tts_voice = instance.get("tts_voice", "")
    # Clean up voice name: "Microsoft David Desktop" -> "David"
    if tts_voice:
        voice_short = tts_voice.replace("Microsoft ", "").replace(" Desktop", "")
    else:
        voice_short = "?"

    profile_name = instance.get("profile_name", "")
    # Extract profile number: "profile_1" -> "1"
    profile_num = profile_name.replace("profile_", "") if profile_name else "?"

    working_dir = instance.get("working_dir", "")
    if working_dir:
        # Shorten home prefix for display
        working_dir_short = working_dir.replace(str(Path.home()), "~")
    else:
        working_dir_short = "?"

    if compact:
        # Compact single-line format for vertical layout bottom
        todos = todos_data.get("todos", [])
        total = todos_data.get("total", 0)
        progress = todos_data.get("progress", 0)
        current_task = todos_data.get("current_task", "")

        # Build compact line: status icon, name, device, voice, dir, progress, current task
        parts = [f"{status_icon} [bold]{name}[/bold]"]
        parts.append(f"[dim]({device})[/dim]")
        tts_mode = instance.get("tts_mode", "verbose") or "verbose"
        if tts_mode == "verbose":
            parts.append(f"[cyan]Voice:[/cyan] {voice_short}")
        elif tts_mode == "muted":
            parts.append(f"[cyan]Voice:[/cyan] [yellow]muted[/yellow]")
        else:
            parts.append(f"[cyan]Voice:[/cyan] [red]silent[/red]")
        if instance.get("voice_chat"):
            if instance.get("listening", False):
                parts.append("[green]🎙 Listening[/green]")
            else:
                parts.append("[yellow]🎙 Muted[/yellow]")
        parts.append(f"[dim]{working_dir_short}[/dim]")

        if total > 0:
            parts.append(f"[yellow]{progress}%[/yellow]")

        if current_task:
            if len(current_task) > 30:
                current_task = current_task[:27] + "..."
            parts.append(f"[italic]{current_task}[/italic]")

        content = "  ".join(parts)
        return Panel(content, title="Instance Details", border_style="magenta")

    lines.append(f"{status_icon} [bold]{name}[/bold]  [dim]({device})[/dim]")
    tts_mode = instance.get("tts_mode", "verbose") or "verbose"
    if tts_mode == "verbose":
        lines.append(f"[cyan]Voice:[/cyan] {voice_short}  [dim](profile {profile_num})[/dim]")
    elif tts_mode == "muted":
        lines.append(f"[cyan]Voice:[/cyan] [yellow]muted[/yellow]  [dim]({voice_short} reserved)[/dim]")
    else:  # silent
        lines.append(f"[cyan]Voice:[/cyan] [red]silent[/red]")
    if instance.get("voice_chat"):
        if instance.get("listening", False):
            lines.append("[green]🎙 Voice Chat: Listening[/green]")
        else:
            lines.append("[yellow]🎙 Voice Chat: Muted[/yellow]")
    lines.append(f"[cyan]Dir:[/cyan]   [dim]{working_dir_short}[/dim]")

    # Session document display
    session_doc_id = instance.get("session_doc_id")
    if session_doc_id:
        try:
            with sqlite3.connect(DB_PATH) as doc_conn:
                doc_cursor = doc_conn.execute(
                    "SELECT title, file_path FROM session_documents WHERE id = ?",
                    (session_doc_id,)
                )
                doc_row = doc_cursor.fetchone()
            if doc_row:
                doc_title = doc_row[0] or "untitled"
                doc_path = doc_row[1]
                short_path = doc_path.replace(str(Path.home()), "~")
                if len(short_path) > 50:
                    short_path = "..." + short_path[-47:]
                lines.append(f"[cyan]Session:[/cyan] [bold]{doc_title}[/bold]  [dim]{short_path}[/dim]")
        except Exception:
            lines.append(f"[cyan]Session:[/cyan] [dim]doc #{session_doc_id}[/dim]")

    lines.append("")

    todos = todos_data.get("todos", [])
    completed = todos_data.get("completed", 0)
    total = todos_data.get("total", 0)
    progress = todos_data.get("progress", 0)

    if total > 0:
        progress_bar = make_progress_bar(progress, 15)
        lines.append(f"Progress: {progress_bar} {completed}/{total}")
        lines.append("")

        lines.append("[bold cyan]Subtasks:[/bold cyan]")
        for todo in todos:
            status_char = todo.get("status", "pending")
            content = todo.get("content", "")

            if len(content) > 45:
                content = content[:42] + "..."

            if status_char == "completed":
                lines.append(f"  [green]v[/green] [dim]{content}[/dim]")
            elif status_char == "in_progress":
                lines.append(f"  [yellow]>[/yellow] [bold]{content}[/bold]")
            else:
                lines.append(f"  [dim]o[/dim] {content}")
    else:
        lines.append("[dim]No active tasks[/dim]")

    content = "\n".join(lines)
    return Panel(content, title="Instance Details", border_style="magenta")


def create_mobile_instance_details_panel(instance: dict, todos_data: dict) -> Panel:
    """Create a compact panel showing the active subtask for mobile."""
    if not instance:
        return Panel("[dim]No selection[/dim]", title="Details", border_style="magenta", padding=(0, 1))

    lines = []

    name = format_instance_name(instance, max_len=15)
    status = instance.get("status", "unknown")
    if status == "stopped":
        status_icon = "[dim]o[/dim]"
    elif status == "processing":
        status_icon = "[green]>[/green]"
    else:
        status_icon = "[cyan]*[/cyan]"

    # Get TTS voice profile info
    tts_voice = instance.get("tts_voice", "")
    voice_short = tts_voice.replace("Microsoft ", "").replace(" Desktop", "") if tts_voice else "?"

    lines.append(f"{status_icon} [bold]{name}[/bold]  [dim]{voice_short}[/dim]")

    current_task = todos_data.get("current_task")
    progress = todos_data.get("progress", 0)
    total = todos_data.get("total", 0)

    if current_task:
        if len(current_task) > 35:
            current_task = current_task[:32] + "..."
        lines.append(f"[yellow]>[/yellow] {current_task}")
        if total > 0:
            lines.append(f"[dim]{progress}% ({todos_data.get('completed', 0)}/{total})[/dim]")
    elif total > 0:
        lines.append(f"[dim]{progress}% complete[/dim]")
    else:
        lines.append("[dim]No active task[/dim]")

    content = "\n".join(lines)
    return Panel(content, title="Details", border_style="magenta", padding=(0, 1))


def create_server_status_panel() -> Panel:
    """Create a panel showing server status."""
    if api_healthy:
        content = "[green]* Server running[/green] on port 7777"
        border = "green"
    else:
        content = f"[red]! Server error[/red]\n[dim]{api_error_message or 'Unknown error'}[/dim]"
        border = "red"

    return Panel(content, title="API Server", border_style=border)


HEARTBEAT_INTERVAL_SECONDS = 15 * 60  # 15 minutes


def get_heartbeat_status() -> dict:
    """Fetch combined heartbeat status from the API."""
    default = {
        "entries": [], "consecutive_idle": 0, "action_count": 0,
        "total_recent": 0, "last_hb_time": None, "last_hb_epoch": None,
        "watchdog_status": "unknown", "watchdog_last_check": None,
        "last_task": None, "openclaw_status": None,
    }
    try:
        req = urllib.request.Request(f"{API_URL}/api/system/heartbeat")
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception:
        return default


# Cache heartbeat data (refresh every 10 seconds, not every frame)
_heartbeat_cache: dict = {}
_heartbeat_cache_time: float = 0


def get_cached_heartbeat_status() -> dict:
    global _heartbeat_cache, _heartbeat_cache_time
    now = time.time()
    if now - _heartbeat_cache_time > 10:
        _heartbeat_cache = get_heartbeat_status()
        _heartbeat_cache_time = now
    return _heartbeat_cache


def _get_instance_counts() -> tuple[int, int]:
    """Return (manual_count, cron_count) of active instances."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/instances")
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            instances = data if isinstance(data, list) else data.get("instances", [])
            alive = [i for i in instances if i.get("status") in ("active", "processing", "idle") and not i.get("is_subagent")]
            cron = sum(1 for i in alive if i.get("origin_type") == "cron")
            return len(alive) - cron, cron
    except Exception:
        return -1, -1


# Cache instance counts (refresh every 10 seconds)
_instance_counts_cache: tuple[int, int] = (0, 0)
_instance_counts_cache_time: float = 0


def get_cached_instance_counts() -> tuple[int, int]:
    global _instance_counts_cache, _instance_counts_cache_time
    now = time.time()
    if now - _instance_counts_cache_time > 10:
        _instance_counts_cache = _get_instance_counts()
        _instance_counts_cache_time = now
    return _instance_counts_cache


def create_monitor_panel(max_lines: int = 8) -> Panel:
    """Create the unified monitor panel — Emperor/Mechanicus partition + cron job list."""
    status = get_cached_heartbeat_status()
    jobs = get_cached_cron_jobs()
    content = Text()

    # Header line: Emperor instances | Mechanicus workers | watchdog
    manual, cron = get_cached_instance_counts()
    content.append("Emperor:", style="white")
    if manual > 0:
        content.append(f"{manual}", style="green bold")
    elif manual == 0:
        content.append("0", style="dim")
    else:
        content.append("?", style="dim")

    content.append(" | Mechanicus:", style="white")
    if cron > 0:
        content.append(f"{cron}", style="cyan bold")
    elif cron == 0:
        content.append("0", style="dim")
    else:
        content.append("?", style="dim")

    wdog = status["watchdog_status"]
    wdog_styles = {"ok": ("green", "OK"), "nudge": ("yellow", "NUDGE"), "escalation": ("red", "ESCALATED"), "unknown": ("dim", "?")}
    wdog_style, wdog_label = wdog_styles.get(wdog, ("dim", wdog))
    content.append("  Wdog:", style="white")
    content.append(wdog_label, style=wdog_style)

    enabled_count = sum(1 for j in jobs if j.get("enabled", True))
    content.append(f"  {enabled_count}/{len(jobs)} active", style="green" if enabled_count > 0 else "red")
    content.append("\n")

    # Per-job rows
    if not jobs:
        content.append("No cron jobs found", style="dim")
    else:
        for job in jobs[:(max_lines - 1)]:
            name = job.get("name", job.get("id", "?")[:8])
            enabled = job.get("enabled", True)
            state = job.get("state", {})
            schedule = job.get("schedule", {})

            if not enabled:
                content.append("  ", style="dim")
                content.append(name, style="dim strikethrough")
                content.append(" disabled\n", style="dim")
                continue

            # Status icon
            job_status = state.get("status", "idle")
            if job_status == "running":
                content.append(" > ", style="green bold")
            else:
                content.append("   ", style="cyan")

            # Name
            content.append(f"{name}", style="white bold")

            # Schedule
            every_ms = schedule.get("everyMs", 0)
            if every_ms >= 3600000:
                sched_str = f"{every_ms // 3600000}h"
            elif every_ms >= 60000:
                sched_str = f"{every_ms // 60000}m"
            else:
                sched_str = schedule.get("cron", "?")
            content.append(f" ({sched_str})", style="dim")

            # Next run countdown
            next_run_ms = state.get("nextRunAtMs")
            content.append("  next:", style="dim")
            if next_run_ms:
                secs_left = max(0, int((next_run_ms / 1000) - time.time()))
                mins, secs = divmod(secs_left, 60)
                if mins >= 60:
                    hours = mins // 60
                    mins = mins % 60
                    cd_str = f"{hours}h{mins:02d}m"
                else:
                    cd_str = f"{mins}:{secs:02d}"

                if secs_left <= 0:
                    content.append("NOW", style="green bold")
                elif secs_left <= 60:
                    content.append(cd_str, style="red bold")
                elif secs_left <= 300:
                    content.append(cd_str, style="yellow")
                else:
                    content.append(cd_str, style="cyan")
            else:
                content.append("--:--", style="dim")

            # Last run
            last_run_ms = state.get("lastRunAtMs")
            if last_run_ms:
                last_ago = int(time.time() - (last_run_ms / 1000))
                if last_ago < 60:
                    last_str = f"{last_ago}s ago"
                elif last_ago < 3600:
                    last_str = f"{last_ago // 60}m ago"
                else:
                    last_str = f"{last_ago // 3600}h ago"
                content.append(f"  last:{last_str}", style="dim")
            else:
                content.append("  last:--", style="dim")

            content.append("\n")

    # Remove trailing newline
    if content.plain.endswith("\n"):
        content.right_crop(1)

    return Panel(content, title="Monitor", border_style="magenta")


# --- Cron Agents Panel ---

# Cache cron job list (refresh every 15 seconds)
_cron_jobs_cache: list = []
_cron_jobs_cache_time: float = 0


def get_cached_cron_jobs() -> list:
    """Fetch cron jobs from the API, cached."""
    global _cron_jobs_cache, _cron_jobs_cache_time
    now = time.time()
    if now - _cron_jobs_cache_time > 15:
        try:
            req = urllib.request.Request(f"{API_URL}/api/cron/jobs")
            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                if isinstance(data, dict) and isinstance(data.get("jobs"), list):
                    _cron_jobs_cache = data["jobs"]
                elif isinstance(data, list):
                    _cron_jobs_cache = data
                else:
                    _cron_jobs_cache = []
        except Exception:
            _cron_jobs_cache = []
        _cron_jobs_cache_time = now
    return _cron_jobs_cache


_timer_shifts_cache = {}
_timer_shifts_cache_time = 0.0

def _fetch_timer_shifts() -> dict:
    """Fetch timer shift analytics from API (cached 5s)."""
    global _timer_shifts_cache, _timer_shifts_cache_time
    now = time.time()
    if now - _timer_shifts_cache_time < 5 and _timer_shifts_cache:
        return _timer_shifts_cache
    try:
        req = urllib.request.Request(f"{API_URL}/api/timer/shifts")
        with urllib.request.urlopen(req, timeout=2) as resp:
            _timer_shifts_cache = json.loads(resp.read().decode())
            _timer_shifts_cache_time = now
    except Exception:
        pass
    return _timer_shifts_cache


def _line_graph(values: list, width: int = 42, height: int = 3,
                modes: list | None = None) -> list:
    """Render a braille line graph with optional per-column background colors.

    Each braille char is a 2-wide x 4-tall dot grid, giving
    width*2 horizontal and height*4 vertical resolution.
    Y-axis always includes 0 so negatives render below a zero line.
    Returns list[Text] (one per row) or empty list if no values.
    """
    if not values:
        return []

    # Mode → background color mapping
    # Working=dark blue, Multi=dark green, Idle=dark orange, Break=dark red
    MODE_BG = {
        "working":         "#143030",   # teal
        "work_silence":    "#143030",
        "work_music":      "#143030",
        "work_video":      "#141430",   # indigo
        "work_scrolling":  "#301414",   # red
        "work_gaming":     "#301414",
        "break":           "#301414",   # red
        "break_exhausted": "#301414",   # red
        "idle":            "#4F4F4F",   # dark gray
        "multitasking":    "#141430",   # indigo
        "distracted":      "#301414",   # red
        "sleeping":        "#4F4F4F",   # dark gray
    }

    # Braille dot bit positions: (col, row) -> bit
    # col 0: rows 0-3 = bits 0,1,2,6   col 1: rows 0-3 = bits 3,4,5,7
    DOT_BITS = {
        (0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
        (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80,
    }
    BRAILLE_BASE = 0x2800

    h_res = width * 2
    v_res = height * 4

    # Resample values to h_res points
    if len(values) >= h_res:
        step = len(values) / h_res
        sampled = [values[int(i * step)] for i in range(h_res)]
    else:
        # Stretch to fill width
        sampled = []
        for i in range(h_res):
            idx = i * (len(values) - 1) / max(1, h_res - 1)
            lo = int(idx)
            hi = min(lo + 1, len(values) - 1)
            frac = idx - lo
            sampled.append(values[lo] * (1 - frac) + values[hi] * frac)

    # Resample modes to width columns (nearest-neighbor)
    col_modes = [None] * width
    if modes and len(modes) > 0:
        for c in range(width):
            idx = int(c * (len(modes) - 1) / max(1, width - 1))
            col_modes[c] = modes[min(idx, len(modes) - 1)]

    # Y-range always includes 0
    mn = min(min(sampled), 0)
    mx = max(max(sampled), 0)
    rng = mx - mn if mx != mn else 1

    # Scale to 0..v_res-1 (0 = bottom, v_res-1 = top)
    scaled = [int((v - mn) / rng * (v_res - 1)) for v in sampled]

    # Zero line position in dot-space (0 = bottom)
    zero_y = int((0 - mn) / rng * (v_res - 1))

    # Build braille grid: grid[row][col] where row 0 = top
    grid = [[0] * width for _ in range(height)]

    # Draw the data line
    for x, y in enumerate(scaled):
        char_col = x // 2
        dot_col = x % 2
        dot_y = (v_res - 1) - y
        char_row = dot_y // 4
        dot_row = dot_y % 4
        if 0 <= char_row < height and 0 <= char_col < width:
            grid[char_row][char_col] |= DOT_BITS[(dot_col, dot_row)]

    # Draw zero line (dim dots) when range crosses zero
    if mn < 0 < mx:
        zero_dot_y = (v_res - 1) - zero_y
        zero_char_row = zero_dot_y // 4
        zero_dot_row = zero_dot_y % 4
        if 0 <= zero_char_row < height:
            for c in range(width):
                # Add both columns of dots at the zero row
                grid[zero_char_row][c] |= DOT_BITS[(0, zero_dot_row)]
                grid[zero_char_row][c] |= DOT_BITS[(1, zero_dot_row)]

    # Detect slopes — use ╱╲ wherever the vertical gap exceeds one cell height (4 dots).
    # Slash until proven otherwise: if a slash fits, it should be present.
    slope_overrides = {}
    SLOPE_THRESHOLD = 8  # 2 cell heights — gentler slopes stay as braille
    for col in range(width):
        x0 = col * 2
        x1 = col * 2 + 1
        if x1 >= len(scaled):
            break
        # Intra-column slope
        intra = scaled[x1] - scaled[x0]
        # Inter-column slope (previous col's right → this col's left)
        inter = 0
        if col > 0:
            prev_x1 = (col - 1) * 2 + 1
            if prev_x1 < len(scaled):
                inter = scaled[x0] - scaled[prev_x1]
        # Use whichever is steeper
        delta = intra if abs(intra) >= abs(inter) else inter
        if abs(delta) >= SLOPE_THRESHOLD:
            # Which rows does the transition span?
            y_lo = min(scaled[x0], scaled[x1])
            y_hi = max(scaled[x0], scaled[x1])
            dot_y_top = (v_res - 1) - y_hi
            dot_y_bot = (v_res - 1) - y_lo
            row_top = max(0, dot_y_top // 4)
            row_bot = min(height - 1, dot_y_bot // 4)
            slope_char = "╱" if delta > 0 else "╲"
            span = list(range(row_top, row_bot + 1))
            if len(span) <= 2:
                for r in span:
                    slope_overrides[(r, col)] = slope_char
            else:
                slope_overrides[(span[0], col)] = slope_char
                slope_overrides[(span[-1], col)] = slope_char
                for r in span[1:-1]:
                    slope_overrides[(r, col)] = "│"

    # Post-pass: bridge vertical gaps between adjacent slope columns with pipes.
    # When consecutive columns both have slopes (e.g. a steep multi-column drop),
    # fill the row gap between them so the edge looks continuous.
    slope_by_col = {}
    for (r, c), ch in slope_overrides.items():
        slope_by_col.setdefault(c, set()).add(r)
    for col in sorted(slope_by_col):
        next_col = col + 1
        if next_col not in slope_by_col:
            continue
        left_rows = slope_by_col[col]
        right_rows = slope_by_col[next_col]
        left_min, left_max = min(left_rows), max(left_rows)
        right_min, right_max = min(right_rows), max(right_rows)
        # Gap below left slopes, above right slopes (falling right)
        if left_max < right_min - 1:
            for r in range(left_max + 1, right_min):
                if (r, next_col) not in slope_overrides:
                    slope_overrides[(r, next_col)] = "│"
                    slope_by_col[next_col].add(r)
        # Gap below right slopes, above left slopes (rising right)
        elif right_max < left_min - 1:
            for r in range(right_max + 1, left_min):
                if (r, col) not in slope_overrides:
                    slope_overrides[(r, col)] = "│"
                    slope_by_col[col].add(r)

    # Build Text rows with per-column styling
    rows = []
    for row_idx, row in enumerate(grid):
        text = Text()
        for col_idx, cell in enumerate(row):
            if (row_idx, col_idx) in slope_overrides:
                ch = slope_overrides[(row_idx, col_idx)]
            else:
                ch = chr(BRAILLE_BASE + cell)
            mode = col_modes[col_idx]
            bg = MODE_BG.get(mode, "")
            if bg:
                style = f"bright_white on {bg}"
            else:
                style = "bright_white"
            text.append(ch, style=style)
        rows.append(text)
    return rows


def _mode_bar(mode_dist: dict, width: int = 36) -> Text:
    """Render a colored horizontal bar showing time distribution per mode."""
    MODE_COLORS = {
        "working": "bright_white",
        "multitasking": "yellow",
        "idle": "dim",
        "break": "blue",
        "distracted": "red",
        "sleeping": "dim",
    }
    MODE_CHARS = {
        "working": "░",
        "multitasking": "▓",
        "idle": "·",
        "break": "▒",
        "distracted": "█",
        "sleeping": "·",
    }

    total = sum(mode_dist.values())
    if total == 0:
        return Text("No mode data", style="dim")

    text = Text()
    for mode, secs in sorted(mode_dist.items(), key=lambda x: -x[1]):
        chars = max(1, round(secs / total * width))
        color = MODE_COLORS.get(mode, "white")
        char = MODE_CHARS.get(mode, "▒")
        text.append(char * chars, style=color)
    return text


def _format_context_section() -> list:
    """Build 'what the system thinks' context lines from live timer state."""
    timer = _read_timer()
    lines = []

    # Activity inference: what does the system think I'm doing?
    desktop_mode = timer.get("desktop_mode", "silence")
    phone_app = timer.get("phone_app")
    activity = timer.get("activity", "working")
    productivity = timer.get("productivity_active", False)

    ACTIVITY_LABELS = {
        "silence": "Focused work (silence)",
        "music": "Focused work (music)",
        "video": "Watching video",
        "scrolling": "Scrolling (social media)",
        "gaming": "Gaming",
        "meeting": "In meeting (TTS muted)",
    }
    doing = ACTIVITY_LABELS.get(desktop_mode, desktop_mode)
    if phone_app:
        doing += f" + phone: {phone_app}"
    doing_color = "green" if activity == "working" else "yellow"
    lines.append(Text.from_markup(f"  [bold]Activity[/bold]  [{doing_color}]{doing}[/{doing_color}]"))

    # Location inference
    location = timer.get("location_zone")
    loc_label = {"home": "Home", "gym": "Gym", "campus": "Campus"}.get(location, "Unknown")
    lines.append(Text.from_markup(f"  [bold]Location[/bold]  {loc_label}"))

    # Productivity state
    prod_style = "green" if productivity else "red"
    prod_label = "Active" if productivity else "Inactive"
    lines.append(Text.from_markup(f"  [bold]Prod[/bold]     [{prod_style}]{prod_label}[/{prod_style}]"))

    # AHK reachable?
    ahk = timer.get("ahk_reachable")
    if ahk is True:
        lines.append(Text.from_markup("  [bold]Desktop[/bold]  [green]AHK connected[/green]"))
    elif ahk is False:
        lines.append(Text.from_markup("  [bold]Desktop[/bold]  [red]AHK unreachable[/red]"))

    return lines


def create_timer_stats_panel(max_lines: int = 10) -> Panel:
    """Create timer stats panel with context awareness, line graph, mode distribution, and shift stats."""
    data = _fetch_timer_shifts()

    # Context section (always show even without shifts)
    lines = _format_context_section()

    if not data or data.get("total_shifts", 0) == 0:
        content = Text()
        for i, line in enumerate(lines):
            if i > 0:
                content.append("\n")
            if isinstance(line, Text):
                content.append_text(line)
            else:
                content.append_text(Text.from_markup(line))
        if not lines:
            content.append_text(Text.from_markup("[dim]No timer shifts recorded today[/dim]"))
        return Panel(content, title="Timer Stats", border_style="magenta")

    # Trim context for compact viewports (Activity + Prod only)
    if layout_mode in ("mobile", "compact"):
        lines = lines[:2]

    lines.append("")  # spacer

    # Determine available content width for graph
    # Label prefix takes LABEL_PAD chars + 1 trailing space = 11 total
    LABEL_PAD = 10
    LABEL_TOTAL = LABEL_PAD + 1  # includes trailing space after label
    PANEL_CHROME = 4  # 2 border + 2 padding
    try:
        con_width = console.width if console else 80
    except Exception:
        con_width = 80

    if layout_mode == "full":
        # Sidebar: ratio=2 of split_row(3, 2); Layout rounds down with 1-char gap
        sidebar_width = (con_width * 2) // 5 - 1
        graph_width = max(10, sidebar_width - PANEL_CHROME - LABEL_TOTAL)
    elif layout_mode == "mobile":
        # Full-width but narrow terminal — extra -2 safety for rounding
        graph_width = max(6, con_width - PANEL_CHROME - LABEL_TOTAL - 2)
    else:
        # vertical, compact: full-width panel
        graph_width = max(10, con_width - PANEL_CHROME - LABEL_TOTAL)

    # Graph height per mode
    if layout_mode in ("mobile", "compact"):
        graph_height = 2  # minimal useful graph
    else:
        graph_height = max(3, max_lines - 9)  # leave room for context(4) + spacer + labels + stats

    # Break balance line graph (braille with colored backgrounds)
    series = data.get("balance_series", [])
    timeline = data.get("balance_timeline", [])
    graph_modes = [e.get("mode", "") for e in timeline] if timeline else None
    if series and len(series) >= 2:
        graph_rows = _line_graph(series, width=graph_width, height=graph_height, modes=graph_modes)
        mn = min(min(series), 0)
        mx = max(max(series), 0)
        mx_label = f"{mx:.0f}m"
        mn_label = f"{mn:.0f}m"
        # First line: label + max + graph row
        first = Text()
        first.append(f"{'Brk ' + mx_label:>{LABEL_PAD}} ", style="bold")
        if graph_rows:
            first.append_text(graph_rows[0])
        lines.append(first)
        # Middle lines
        for gr in graph_rows[1:-1]:
            mid = Text()
            mid.append(" " * (LABEL_PAD + 1))
            mid.append_text(gr)
            lines.append(mid)
        # Last line: min
        if len(graph_rows) > 1:
            last = Text()
            last.append(f"{mn_label:>{LABEL_PAD}} ", style="dim")
            last.append_text(graph_rows[-1])
            lines.append(last)
    else:
        lines.append("[bold]Break Balance[/bold]  [dim]no data[/dim]")

    # Mode distribution bar
    mode_dist = data.get("mode_distribution", {})
    if mode_dist:
        bar = _mode_bar(mode_dist)
        bar_line = Text()
        bar_line.append("Modes ", style="bold")
        bar_line.append("  ")
        bar_line.append_text(bar)
        lines.append(bar_line)

        # Mode legend (compact)
        legend_parts = []
        total = sum(mode_dist.values())
        MODE_SHORTS = {
            "working": ("░ wrk", "bright_white"),
            "multitasking": ("▓ multi", "yellow"),
            "idle": ("· idle", "dim"),
            "break": ("▒ brk", "blue"),
            "distracted": ("█ dist", "red"),
            "sleeping": ("· slp", "dim"),
        }
        for mode, secs in sorted(mode_dist.items(), key=lambda x: -x[1]):
            pct = round(secs / total * 100)
            if pct < 3:
                continue
            short, color = MODE_SHORTS.get(mode, (mode[-4:], "white"))
            legend_parts.append(f"[{color}]{short}[/{color}] {pct}%")
        if legend_parts:
            lines.append("  " + "  ".join(legend_parts[:4]))

    # Stats row
    shifts = data.get("total_shifts", 0)
    enforcements = data.get("enforcement_count", 0)
    twitter = data.get("twitter_shifts", 0)
    triggers = data.get("shifts_by_trigger", {})

    stats = f"[bold]Shifts[/bold] {shifts}"
    if enforcements:
        stats += f"  [red bold]Enforcements[/red bold] {enforcements}"
    if twitter:
        stats += f"  [magenta]Twitter[/magenta] {twitter}"
    lines.append(stats)

    # Trigger breakdown (compact)
    if triggers:
        trigger_parts = []
        for t, count in sorted(triggers.items(), key=lambda x: -x[1]):
            trigger_parts.append(f"[dim]{t}[/dim]={count}")
        lines.append("  " + "  ".join(trigger_parts[:5]))

    # Join lines — handle mixed str/Text
    content = Text()
    for i, line in enumerate(lines[:max_lines]):
        if i > 0:
            content.append("\n")
        if isinstance(line, Text):
            content.append_text(line)
        else:
            content.append_text(Text.from_markup(line))

    return Panel(content, title="Timer Stats", border_style="magenta")


def create_info_panel(max_lines: int = 8) -> Panel:
    """Create the info panel - events, server logs, deploy logs, monitor, or timer stats based on panel_page."""
    if panel_page == 0:
        events = get_recent_events(max_lines)
        return create_events_panel(events)
    elif panel_page == 1:
        return create_server_logs_panel(max_lines=max_lines)
    elif panel_page == 2:
        return create_deploy_logs_panel(max_lines=max_lines)
    elif panel_page == 3:
        return create_monitor_panel(max_lines=max_lines)
    else:
        return create_timer_stats_panel(max_lines=max_lines)


def create_mobile_info_panel(max_lines: int = 6) -> Panel:
    """Create a compact info panel for mobile - events, server logs, deploy logs, monitor, or timer stats based on panel_page."""
    if panel_page == 0:
        events = get_recent_events(max_lines)
        return create_mobile_events_panel(events)
    elif panel_page == 1:
        return create_server_logs_panel(max_lines=max_lines)
    elif panel_page == 2:
        return create_deploy_logs_panel(max_lines=max_lines)
    elif panel_page == 3:
        return create_monitor_panel(max_lines=max_lines)
    else:
        return create_timer_stats_panel(max_lines=max_lines)


def create_status_bar(instances: list, selected_idx: int) -> Text:
    """Create the status bar."""
    global unstick_feedback, resume_feedback, restart_feedback

    active_count = sum(1 for i in instances if i.get("status") in ("processing", "idle"))
    total_count = len(instances)

    # Mode indicator with color
    mode_colors = {"mobile": "yellow", "vertical": "magenta", "compact": "blue", "full": "cyan"}
    mode_color = mode_colors.get(layout_mode, "white")

    # Page indicator
    page_names = ["Events", "Logs", "Deploy", "Monitor", "Timer"]
    page_name = page_names[panel_page] if panel_page < len(page_names) else "?"

    # Filter indicator
    filter_indicator = ""
    if filter_mode != "all":
        filter_indicator = f"  [magenta]F:{filter_mode}[/magenta]"

    # Subagent count (from unfiltered cache)
    subagent_indicator = ""
    if not show_subagents:
        hidden_sub_count = sum(1 for i in instances_cache if i.get("is_subagent"))
        if hidden_sub_count > 0:
            subagent_indicator = f"  [dim]+{hidden_sub_count} sub[/dim]"

    # Global TTS mode indicator
    tts_mode_indicator = ""
    if global_tts_mode == "muted":
        tts_mode_indicator = "  [yellow]TTS:muted[/yellow]"
    elif global_tts_mode == "silent":
        tts_mode_indicator = "  [red]TTS:silent[/red]"

    # Table mode indicator
    if table_mode == "cron":
        table_indicator = "[yellow bold]\\[Cron][/yellow bold]"
    else:
        table_indicator = "[cyan]\\[Instances][/cyan]"

    text = Text()
    text.append_text(Text.from_markup(f"{table_indicator} [dim](\\[/])[/dim]"))
    text.append(f"  {active_count}/{total_count}  |  ", style="white")
    text.append_text(Text.from_markup(f"[{mode_color}]{layout_mode}[/{mode_color}]"))
    text.append(f"  |  {selected_idx + 1}/{total_count}  |  ", style="white")
    text.append_text(Text.from_markup(f"[cyan]{page_name}[/cyan] [dim](h/l)[/dim]"))
    if filter_indicator:
        text.append_text(Text.from_markup(filter_indicator))
    if subagent_indicator:
        text.append_text(Text.from_markup(subagent_indicator))
    if tts_mode_indicator:
        text.append_text(Text.from_markup(tts_mode_indicator))
    text.append("  |  ", style="white")

    # Check for feedback messages (show for 3 seconds)
    feedback_msg = None
    if restart_feedback:
        fb_time, fb_text = restart_feedback
        if time.time() - fb_time < 3.0:
            feedback_msg = fb_text
        else:
            restart_feedback = None
    if not feedback_msg and unstick_feedback:
        fb_time, fb_text = unstick_feedback
        if time.time() - fb_time < 3.0:
            feedback_msg = fb_text
        else:
            unstick_feedback = None
    if not feedback_msg and resume_feedback:
        fb_time, fb_text = resume_feedback
        if time.time() - fb_time < 3.0:
            feedback_msg = fb_text
        else:
            resume_feedback = None

    if feedback_msg:
        # Use green for success messages, yellow for warnings
        if "Copied" in feedback_msg or "Skipped" in feedback_msg or "Restarted" in feedback_msg:
            text.append_text(Text.from_markup(f"[green bold]✓ {feedback_msg}[/green bold]"))
        else:
            text.append_text(Text.from_markup(f"[yellow bold]{feedback_msg}[/yellow bold]"))
    else:
        text.append_text(Text.from_markup("[dim]jk=nav r=rename s=stop m=mute n=note M=global q=quit[/dim]"))

    return text


def create_mobile_status_bar(instances: list, selected_idx: int) -> Text:
    """Create a compact status bar for mobile."""
    active_count = sum(1 for i in instances if i.get("status") in ("processing", "idle"))
    total_count = len(instances)

    # Page indicator
    page_indicators = {0: "E", 1: "L", 2: "D", 3: "M", 4: "T"}
    page_indicator = page_indicators.get(panel_page, "?")

    table_tag = "C" if table_mode == "cron" else "I"

    text = Text()

    # Timer state (condensed)
    state = _read_timer()
    mode_icons = {
        "working": "💻", "multitasking": "📺", "idle": "💤",
        "break": "☕", "distracted": "⚠️", "sleeping": "🌙",
    }
    icon = mode_icons.get(state["mode"], "❓")
    is_backlog = state["backlog_secs"] > 0
    _break_secs = state["break_secs"]
    _backlog_secs = state["backlog_secs"]
    break_style = break_balance_style(_break_secs, _backlog_secs)
    break_str = format_break_time(_backlog_secs if is_backlog else _break_secs)
    text.append(f"{icon} ", style="bold")
    if is_backlog:
        text.append("BL ", style=break_style)
    text.append(break_str, style=break_style)
    text.append("  ", style="dim")

    text.append(f"{active_count}/{total_count} ", style="white")
    if active_count > 0:
        text.append("*", style="green")
    else:
        text.append("o", style="dim")
    text.append(f"  sel:{selected_idx + 1}", style="dim")
    text.append(f"  [{page_indicator}]", style="cyan")

    return text


def generate_mobile_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate a compact dashboard layout for mobile."""
    global api_healthy, api_error_message

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        instance_id = selected_instance.get("id", "")
        # Poll for fresh todos when processing, otherwise use cached data
        if selected_instance.get("status") == "processing":
            selected_todos = get_instance_todos(instance_id, use_cache=False)
        else:
            selected_todos = get_instance_todos(instance_id, use_cache=True)

    layout = Layout()

    if not api_healthy:
        layout.split_column(
            Layout(name="error", size=2),
            Layout(name="instances"),
            Layout(name="details", size=5),
            Layout(name="info_panel", size=8),
            Layout(name="footer", size=1)
        )
        error_text = Text()
        error_text.append("! API down", style="bold red")
        layout["error"].update(Panel(error_text, border_style="red"))
    else:
        layout.split_column(
            Layout(name="instances"),
            Layout(name="details", size=5),
            Layout(name="info_panel", size=8),
            Layout(name="footer", size=1)
        )

    if table_mode == "cron":
        cron_jobs = get_cached_cron_jobs()
        selected_job = cron_jobs[cron_selected_index] if cron_jobs and 0 <= cron_selected_index < len(cron_jobs) else None
        layout["instances"].update(create_mobile_cron_table(cron_jobs, cron_selected_index))
        layout["details"].update(create_mobile_cron_details_panel(selected_job))
    else:
        layout["instances"].update(create_mobile_instances_table(instances, selected_idx))
        layout["details"].update(create_mobile_instance_details_panel(selected_instance, selected_todos))
    layout["info_panel"].update(create_mobile_info_panel(max_lines=6))
    layout["footer"].update(create_mobile_status_bar(instances, selected_idx))

    return layout


def generate_compact_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate compact dashboard without sidebar (for medium-width terminals)."""
    global api_healthy, api_error_message

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        instance_id = selected_instance.get("id", "")
        # Poll for fresh todos when processing, otherwise use cached data
        if selected_instance.get("status") == "processing":
            selected_todos = get_instance_todos(instance_id, use_cache=False)
        else:
            selected_todos = get_instance_todos(instance_id, use_cache=True)

    layout = Layout()

    # Compact header + main content + footer
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="instances"),
        Layout(name="info_panel", size=4),
        Layout(name="footer", size=1)
    )

    # Single header panel with health dot inline
    health_dot = "[green]●[/green]" if api_healthy else "[red]●[/red]"
    timer_text = get_timer_header_text()
    dot = Text("● ", style="green" if api_healthy else "red")
    dot.append_text(timer_text)
    timer_text = dot
    timer_text.justify = "center"
    layout["header"].update(Panel(
        timer_text,
        border_style="cyan" if api_healthy else "red"
    ))

    if table_mode == "cron":
        cron_jobs = get_cached_cron_jobs()
        selected_job = cron_jobs[cron_selected_index] if cron_jobs and 0 <= cron_selected_index < len(cron_jobs) else None
        layout["instances"].update(create_compact_cron_table(cron_jobs, cron_selected_index))
    else:
        layout["instances"].update(create_compact_instances_table(instances, selected_idx))
    layout["info_panel"].update(create_info_panel(max_lines=3))
    layout["footer"].update(create_status_bar(instances, selected_idx))

    return layout


def generate_vertical_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate vertical dashboard with stacked panels (for vertical monitors).

    Layout (top to bottom):
    - Header (timer + server status)
    - Instance table (sized to fit content, primary element)
    - Recent events (fills remaining space)
    - Instance details (compact, bottom-aligned)
    - Footer (status bar)
    """
    global api_healthy, api_error_message

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        instance_id = selected_instance.get("id", "")
        # Poll for fresh todos when processing, otherwise use cached data
        if selected_instance.get("status") == "processing":
            selected_todos = get_instance_todos(instance_id, use_cache=False)
        else:
            selected_todos = get_instance_todos(instance_id, use_cache=True)

    # Calculate adaptive sizes based on terminal height and content
    height = console.size.height

    # Fixed elements
    header_size = 3
    footer_size = 1
    details_size = 3  # Compact instance details at bottom (single line + borders)

    # Instance table: sized to fit content (primary element)
    num_instances = max(len(instances), 1)
    # Table needs: title + header + separator + N data rows + borders = N + 6
    table_ideal = num_instances + 6
    # Reasonable bounds - table is primary, but don't let it dominate
    table_min = 6   # Minimum to show a couple rows
    table_max = 20  # Allow more room for table as primary element

    # Calculate instance table size
    instance_size = max(table_min, min(table_ideal, table_max))

    # Events panel gets all remaining space
    # Available = total - header - footer - details - table
    events_size = height - header_size - footer_size - details_size - instance_size
    events_min = 6  # Minimum readable events panel
    events_size = max(events_min, events_size)

    layout = Layout()

    # Vertical layout: Table → Events → Details (bottom)
    layout.split_column(
        Layout(name="header", size=header_size),
        Layout(name="instances", size=instance_size),
        Layout(name="info_panel"),  # Events - takes remaining space (no size = flex)
        Layout(name="details", size=details_size),
        Layout(name="footer", size=footer_size)
    )

    # Single header panel with health dot inline
    timer_text = get_timer_header_text()
    dot = Text("● ", style="green" if api_healthy else "red")
    dot.append_text(timer_text)
    timer_text = dot
    timer_text.justify = "center"
    layout["header"].update(Panel(
        timer_text,
        border_style="cyan" if api_healthy else "red"
    ))

    # Calculate how many lines fit in the info panel (panel has 2 border lines)
    info_lines = max(1, events_size - 2)

    if table_mode == "cron":
        cron_jobs = get_cached_cron_jobs()
        selected_job = cron_jobs[cron_selected_index] if cron_jobs and 0 <= cron_selected_index < len(cron_jobs) else None
        layout["instances"].update(create_compact_cron_table(cron_jobs, cron_selected_index))
        layout["details"].update(create_compact_cron_details_panel(selected_job))
    else:
        layout["instances"].update(create_compact_instances_table(instances, selected_idx))
        layout["details"].update(create_instance_details_panel(selected_instance, selected_todos, compact=True))
    layout["info_panel"].update(create_info_panel(max_lines=info_lines))
    layout["footer"].update(create_status_bar(instances, selected_idx))

    return layout


def generate_dashboard(instances: list, selected_idx: int) -> Layout:
    """Generate the full dashboard layout."""
    global api_healthy, api_error_message

    tts_queue = get_tts_queue_status()

    selected_instance = None
    selected_todos = {"progress": 0, "current_task": None, "total": 0, "todos": []}
    if instances and 0 <= selected_idx < len(instances):
        selected_instance = instances[selected_idx]
        instance_id = selected_instance.get("id", "")
        # Poll for fresh todos when processing, otherwise use cached data
        if selected_instance.get("status") == "processing":
            selected_todos = get_instance_todos(instance_id, use_cache=False)
        else:
            selected_todos = get_instance_todos(instance_id, use_cache=True)

    layout = Layout()

    # Include server status in header area
    layout.split_column(
        Layout(name="header", size=5),
        Layout(name="main"),
        Layout(name="footer", size=1)
    )

    # Header with server status
    header_layout = Layout()
    header_layout.split_row(
        Layout(name="title", ratio=2),
        Layout(name="server_status", ratio=1)
    )
    timer_text = get_timer_header_text()
    timer_text.justify = "center"
    header_layout["title"].update(Panel(
        timer_text,
        border_style="cyan"
    ))
    header_layout["server_status"].update(create_server_status_panel())
    layout["header"].update(header_layout)

    # Main content
    layout["main"].split_row(
        Layout(name="left_column", ratio=3),
        Layout(name="sidebar", ratio=2)
    )

    # Left column: instances table + details section (instance_details + tts_queue)
    layout["left_column"].split_column(
        Layout(name="instances", ratio=3),
        Layout(name="details_section", ratio=1)
    )

    # Details section: instance details (3/4) + TTS queue (1/4)
    layout["details_section"].split_column(
        Layout(name="instance_details", ratio=3),
        Layout(name="tts_queue", ratio=1)
    )

    # Sidebar shows events or server logs based on panel_page
    layout["sidebar"].update(create_info_panel(max_lines=20))

    if table_mode == "cron":
        cron_jobs = get_cached_cron_jobs()
        selected_job = cron_jobs[cron_selected_index] if cron_jobs and 0 <= cron_selected_index < len(cron_jobs) else None
        layout["instances"].update(create_cron_table(cron_jobs, cron_selected_index))
        layout["instance_details"].update(create_cron_details_panel(selected_job))
    else:
        layout["instances"].update(create_instances_table(instances, selected_idx))
        layout["instance_details"].update(create_instance_details_panel(selected_instance, selected_todos))
    layout["tts_queue"].update(create_tts_queue_panel(tts_queue))

    layout["footer"].update(create_status_bar(instances, selected_idx))

    return layout


def get_dashboard(instances: list, selected_idx: int) -> Layout:
    """Get appropriate dashboard based on layout_mode (dynamic if not forced)."""
    global layout_mode
    # Dynamically detect layout mode on each render if not forced by CLI
    if not layout_mode_forced:
        layout_mode = detect_layout_mode()

    if layout_mode == "mobile":
        return generate_mobile_dashboard(instances, selected_idx)
    if layout_mode == "vertical":
        return generate_vertical_dashboard(instances, selected_idx)
    if layout_mode == "compact":
        return generate_compact_dashboard(instances, selected_idx)
    return generate_dashboard(instances, selected_idx)


def main():
    """Main entry point."""
    global selected_index, instances_cache, api_healthy, api_error_message, layout_mode, layout_mode_forced, sort_mode, filter_mode, show_subagents, panel_page
    global deploy_active, deploy_log_path, deploy_metadata, deploy_previous_page, deploy_auto_switched
    global table_mode, cron_selected_index, unstick_feedback, global_tts_mode

    parser = argparse.ArgumentParser(description="Token-API TUI Dashboard")
    parser.add_argument("--mobile", "-m", action="store_true",
                        help="Force mobile-friendly layout")
    parser.add_argument("--vertical", "-v", action="store_true",
                        help="Force vertical layout (stacked panels)")
    parser.add_argument("--compact", action="store_true",
                        help="Force compact layout (no sidebar)")
    parser.add_argument("--no-mobile", action="store_true",
                        help="Force full desktop layout even on narrow terminals")
    args = parser.parse_args()

    if args.mobile:
        layout_mode = "mobile"
        layout_mode_forced = True
    elif args.vertical:
        layout_mode = "vertical"
        layout_mode_forced = True
    elif args.compact:
        layout_mode = "compact"
        layout_mode_forced = True
    elif args.no_mobile:
        layout_mode = "full"
        layout_mode_forced = True
    else:
        layout_mode = detect_layout_mode()
        layout_mode_forced = False

    mode_colors = {"mobile": "yellow", "vertical": "magenta", "compact": "blue", "full": "cyan"}
    mode_indicator = f"[{mode_colors.get(layout_mode, 'white')}]{layout_mode}[/{mode_colors.get(layout_mode, 'white')}]"

    console.print(f"[cyan]Starting Token-API TUI[/cyan] ({mode_indicator} mode)")

    # Health check
    api_healthy, api_error_message = check_api_health()
    if not api_healthy:
        console.print(f"[yellow]Warning:[/yellow] {api_error_message}")
        console.print("[dim]TUI will retry API calls — data panels may be empty until server is reachable.[/dim]")

    console.print("[dim]Controls: jk=nav, gG=top/btm, []=table, h/l=page, Enter=open, r=rename, n=note, f=filter, s=stop, d=del, R=restart, q=quit[/dim]\n")

    # Record startup time for smart restart detection; clean stale signals
    tui_slot = "mobile" if layout_mode == "mobile" else "desktop"
    try:
        (TUI_SIGNAL_DIR / f"tui-started-{tui_slot}.timestamp").write_text(str(int(time.time())))
        signal_file = TUI_SIGNAL_DIR / f"tui-restart-{tui_slot}.signal"
        if signal_file.exists():
            age = time.time() - signal_file.stat().st_mtime
            if age > 30:
                signal_file.unlink(missing_ok=True)
    except Exception:
        pass

    quit_flag = threading.Event()
    input_mode = threading.Event()
    update_flag = threading.Event()
    action_queue = []
    action_lock = threading.Lock()

    # Store terminal settings at main scope for cleanup on Ctrl+C
    import tty
    import termios
    original_terminal_settings = termios.tcgetattr(sys.stdin)

    def key_listener():
        """Listen for keypresses."""
        import select as sel

        try:
            tty.setcbreak(sys.stdin.fileno())
            while not quit_flag.is_set():
                if input_mode.is_set():
                    time.sleep(0.05)
                    continue

                if sel.select([sys.stdin], [], [], 0.02)[0]:
                    if input_mode.is_set():
                        continue

                    key = sys.stdin.read(1)

                    if key.lower() == 'q':
                        quit_flag.set()
                        break
                    elif key == '\x1b':
                        if sel.select([sys.stdin], [], [], 0.05)[0]:
                            seq = sys.stdin.read(2)
                            with action_lock:
                                if seq == '[A':
                                    action_queue.append('up')
                                elif seq == '[B':
                                    action_queue.append('down')
                            update_flag.set()
                    elif key == '\x12':  # Ctrl+R: full refresh (restart server + re-exec TUI)
                        with action_lock:
                            action_queue.append('full_refresh')
                        update_flag.set()
                    elif key == 'r':
                        with action_lock:
                            action_queue.append('rename')
                        update_flag.set()
                    elif key.lower() == 'd':
                        with action_lock:
                            action_queue.append('delete')
                        update_flag.set()
                    elif key.lower() == 'c':
                        with action_lock:
                            action_queue.append('delete_all')
                        update_flag.set()
                    elif key.lower() == 's':
                        with action_lock:
                            action_queue.append('stop')
                        update_flag.set()
                    elif key.lower() == 'o':
                        with action_lock:
                            action_queue.append('sort')
                        update_flag.set()
                    elif key == 'j':
                        with action_lock:
                            action_queue.append('down')
                        update_flag.set()
                    elif key == 'k':
                        with action_lock:
                            action_queue.append('up')
                        update_flag.set()
                    elif key == 'h':
                        with action_lock:
                            action_queue.append('page_prev')
                        update_flag.set()
                    elif key == 'l':
                        with action_lock:
                            action_queue.append('page_next')
                        update_flag.set()
                    elif key == 'y':
                        with action_lock:
                            action_queue.append('resume')
                        update_flag.set()
                    elif key == 'v':
                        with action_lock:
                            action_queue.append('voice')
                        update_flag.set()
                    elif key == 'U':
                        with action_lock:
                            action_queue.append('unstick')
                        update_flag.set()
                    elif key == 'I':
                        with action_lock:
                            action_queue.append('unstick2')
                        update_flag.set()
                    elif key == 'K':
                        with action_lock:
                            action_queue.append('kill')
                        update_flag.set()
                    elif key == 'a':
                        with action_lock:
                            action_queue.append('toggle_subagents')
                        update_flag.set()
                    elif key == 'm':
                        with action_lock:
                            action_queue.append('mute_toggle')
                        update_flag.set()
                    elif key == 'n':
                        with action_lock:
                            action_queue.append('session_note')
                        update_flag.set()
                    elif key == 'M':
                        with action_lock:
                            action_queue.append('global_mute_toggle')
                        update_flag.set()
                    elif key == 'f':
                        with action_lock:
                            action_queue.append('filter')
                        update_flag.set()
                    elif key == 'R':
                        with action_lock:
                            action_queue.append('restart')
                        update_flag.set()
                    elif key == '\r' or key == '\n':
                        with action_lock:
                            action_queue.append('open_terminal')
                        update_flag.set()
                    elif key == 'g':
                        with action_lock:
                            action_queue.append('go_top')
                        update_flag.set()
                    elif key == 'G':
                        with action_lock:
                            action_queue.append('go_bottom')
                        update_flag.set()
                    elif key == '[':
                        with action_lock:
                            action_queue.append('table_prev')
                        update_flag.set()
                    elif key == ']':
                        with action_lock:
                            action_queue.append('table_next')
                        update_flag.set()
        except Exception:
            pass
        finally:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)
            except:
                pass

    listener_thread = threading.Thread(target=key_listener, daemon=True)
    listener_thread.start()

    instances_cache = get_instances()
    refresh_global_tts_mode()
    prev_instance_ids = set(i.get("id") for i in instances_cache)

    def _get_displayed():
        """Get filtered instances for display."""
        return filter_instances(instances_cache)

    def _refresh(live_ref):
        """Refresh dashboard with filtered instances."""
        displayed = _get_displayed()
        live_ref.update(get_dashboard(displayed, selected_index))
        live_ref.refresh()

    def _clamp_selection():
        """Clamp selected_index and cron_selected_index to their list bounds."""
        global selected_index, cron_selected_index
        displayed = _get_displayed()
        if displayed:
            selected_index = min(selected_index, len(displayed) - 1)
        else:
            selected_index = 0
        cron_jobs = get_cached_cron_jobs()
        if cron_jobs:
            cron_selected_index = min(cron_selected_index, len(cron_jobs) - 1)
        else:
            cron_selected_index = 0

    try:
        with Live(get_dashboard(_get_displayed(), selected_index), console=console, refresh_per_second=10, screen=True) as live:
            last_refresh = time.time()
            last_timer_refresh = last_refresh

            while not quit_flag.is_set():
                actions_to_process = []
                with action_lock:
                    if action_queue:
                        actions_to_process = action_queue.copy()
                        action_queue.clear()

                displayed = _get_displayed()

                for action in actions_to_process:
                    if action == 'table_prev':
                        table_mode = "instances"
                        _refresh(live)
                        continue

                    elif action == 'table_next':
                        table_mode = "cron"
                        _clamp_selection()
                        _refresh(live)
                        continue

                    if action == 'up':
                        if table_mode == "cron":
                            cron_selected_index = max(0, cron_selected_index - 1)
                        elif displayed:
                            selected_index = max(0, selected_index - 1)
                        _refresh(live)

                    elif action == 'down':
                        if table_mode == "cron":
                            cron_jobs = get_cached_cron_jobs()
                            cron_selected_index = min(len(cron_jobs) - 1, cron_selected_index + 1) if cron_jobs else 0
                        elif displayed:
                            selected_index = min(len(displayed) - 1, selected_index + 1)
                        _refresh(live)

                    elif action == 'go_top':
                        if table_mode == "cron":
                            cron_selected_index = 0
                        elif displayed:
                            selected_index = 0
                        _refresh(live)

                    elif action == 'go_bottom':
                        if table_mode == "cron":
                            cron_jobs = get_cached_cron_jobs()
                            cron_selected_index = len(cron_jobs) - 1 if cron_jobs else 0
                        elif displayed:
                            selected_index = len(displayed) - 1
                        _refresh(live)

                    if action == 'rename' and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            current_name = format_instance_name(instance)

                            input_mode.set()
                            time.sleep(0.1)
                            live.stop()

                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                            console.print(f"\n[yellow]Rename instance:[/yellow] {current_name}")
                            try:
                                new_name = Prompt.ask("New name", default=current_name)
                                if new_name and new_name != current_name:
                                    if rename_instance(instance_id, new_name):
                                        console.print(f"[green]v[/green] Renamed to: {new_name}")
                                    else:
                                        console.print("[red]x[/red] Rename failed")
                                else:
                                    console.print("[dim]Cancelled[/dim]")
                            except (KeyboardInterrupt, EOFError):
                                console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            instances_cache = get_instances()
                            _clamp_selection()
                            live.start()
                            _refresh(live)

                    elif action == 'session_note' and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            session_doc_id = instance.get("session_doc_id")

                            if not session_doc_id:
                                input_mode.set()
                                time.sleep(0.1)
                                live.stop()
                                console.print("[yellow]No session doc linked. Use instance-name --session to create one.[/yellow]")
                                time.sleep(1.5)
                                live.start()
                                input_mode.clear()
                                _refresh(live)
                                continue

                            input_mode.set()
                            time.sleep(0.1)
                            live.stop()

                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                            console.print(f"\n[yellow]Session note for:[/yellow] {format_instance_name(instance)}")
                            try:
                                note = Prompt.ask("Note")
                                if note and note.strip():
                                    try:
                                        merge_body = json.dumps({"content": note.strip(), "source": "tui", "context": "Quick note from TUI"}).encode("utf-8")
                                        req = urllib.request.Request(
                                            f"{API_URL}/api/session-docs/{session_doc_id}/merge",
                                            data=merge_body,
                                            headers={"Content-Type": "application/json"},
                                            method="POST"
                                        )
                                        with urllib.request.urlopen(req, timeout=30) as resp:
                                            result = json.loads(resp.read().decode())
                                        if result.get("status") == "merged":
                                            console.print("[green]v[/green] Note merged into session doc")
                                        else:
                                            console.print(f"[red]x[/red] Unexpected response: {result}")
                                    except Exception as e:
                                        console.print(f"[red]x[/red] Merge request failed: {e}")
                                else:
                                    console.print("[dim]Cancelled[/dim]")
                            except (KeyboardInterrupt, EOFError):
                                console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            instances_cache = get_instances()
                            _clamp_selection()
                            live.start()
                            _refresh(live)

                    elif action == 'delete' and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)

                            input_mode.set()
                            time.sleep(0.1)
                            live.stop()

                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                            console.print(f"\n[red]Delete instance:[/red] {instance_name}")
                            try:
                                confirm = Prompt.ask("Type 'yes' to confirm delete", default="no")
                                if confirm.lower() == 'yes':
                                    if delete_instance(instance_id):
                                        console.print(f"[green]v[/green] Deleted: {instance_name}")
                                    else:
                                        console.print("[red]x[/red] Delete failed")
                                else:
                                    console.print("[dim]Cancelled[/dim]")
                            except (KeyboardInterrupt, EOFError):
                                console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            instances_cache = get_instances()
                            _clamp_selection()
                            live.start()
                            _refresh(live)

                    elif action == 'voice' and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)
                            current_voice = instance.get("tts_voice", "")

                            input_mode.set()
                            time.sleep(0.1)
                            live.stop()

                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                            voices = get_available_voices()
                            if not voices:
                                console.print("[red]Could not fetch voices from API[/red]")
                            else:
                                console.print(f"\n[cyan]Change voice for:[/cyan] {instance_name}")
                                console.print(f"[dim]Current: {current_voice}[/dim]\n")

                                # Display numbered list
                                for i, v in enumerate(voices, 1):
                                    marker = "[green]*[/green]" if v["voice"] == current_voice else " "
                                    console.print(f"  {marker} {i}. {v['short_name']}")

                                console.print()
                                try:
                                    choice = Prompt.ask("Select voice number", default="")
                                    if choice.isdigit():
                                        idx = int(choice) - 1
                                        if 0 <= idx < len(voices):
                                            new_voice = voices[idx]["voice"]
                                            result = change_instance_voice(instance_id, new_voice)
                                            if result.get("success"):
                                                if result.get("status") == "no_change":
                                                    console.print("[dim]Already using that voice[/dim]")
                                                else:
                                                    changes = result.get("changes", [])
                                                    console.print(f"[green]v[/green] Voice changed to: {voices[idx]['short_name']}")
                                                    # Show bump chain if any
                                                    if len(changes) > 1:
                                                        console.print("[yellow]Bump chain:[/yellow]")
                                                        for c in changes:
                                                            old_short = c['old'].replace('Microsoft ', '') if c['old'] else '?'
                                                            new_short = c['new'].replace('Microsoft ', '')
                                                            console.print(f"  {c['name']}: {old_short} -> {new_short}")
                                            else:
                                                console.print("[red]x[/red] Voice change failed")
                                        else:
                                            console.print("[red]Invalid selection[/red]")
                                    else:
                                        console.print("[dim]Cancelled[/dim]")
                                except (KeyboardInterrupt, EOFError):
                                    console.print("[dim]Cancelled[/dim]")

                            time.sleep(0.3)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            instances_cache = get_instances()
                            live.start()
                            _refresh(live)

                    elif action == 'mute_toggle' and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            current_mode = instance.get("tts_mode", "verbose") or "verbose"
                            result = cycle_instance_tts_mode(instance_id, current_mode)
                            if result:
                                new_mode = result.get("mode", "?")
                                mode_display = {"verbose": "Verbose (TTS+Sound)", "muted": "Muted (Sound only)", "silent": "Silent"}
                                unstick_feedback = (time.time(), f"TTS: {mode_display.get(new_mode, new_mode)}")
                                instances_cache = get_instances()
                                refresh_global_tts_mode()

                    elif action == 'global_mute_toggle':
                        result = cycle_global_tts_mode()
                        if result:
                            new_mode = result.get("mode", "?")
                            mode_display = {"verbose": "Verbose", "muted": "Muted", "silent": "Silent"}
                            unstick_feedback = (time.time(), f"Global TTS: {mode_display.get(new_mode, new_mode)}")
                            instances_cache = get_instances()

                    elif action == 'delete_all':
                        total_count = len(instances_cache) if instances_cache else 0

                        if total_count == 0:
                            input_mode.set()
                            live.stop()
                            console.print("\n[dim]No instances to clear.[/dim]")
                            time.sleep(1)
                            tty.setcbreak(sys.stdin.fileno())
                            input_mode.clear()
                            live.start()
                            _refresh(live)
                            continue

                        input_mode.set()
                        time.sleep(0.1)
                        live.stop()

                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                        console.print(f"\n[red bold]Clear all {total_count} instance(s)?[/red bold]")
                        console.print("[dim]This will remove all instances from the database.[/dim]")
                        try:
                            confirm = Prompt.ask("Type 'yes' to confirm", default="no")
                            if confirm.lower() == 'yes':
                                success, count = delete_all_instances()
                                if success:
                                    console.print(f"[green]v[/green] Cleared {count} instance(s)")
                                    selected_index = 0
                                else:
                                    console.print("[red]x[/red] Clear all failed")
                            else:
                                console.print("[dim]Cancelled[/dim]")
                        except (KeyboardInterrupt, EOFError):
                            console.print("[dim]Cancelled[/dim]")

                        time.sleep(0.3)
                        tty.setcbreak(sys.stdin.fileno())
                        input_mode.clear()
                        instances_cache = get_instances()
                        _clamp_selection()
                        live.start()
                        _refresh(live)

                    elif action == 'stop' and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")

                            # Stop without confirmation (it's non-destructive)
                            if delete_instance(instance_id):
                                instances_cache = get_instances()
                                _clamp_selection()
                                _refresh(live)

                    elif action in ('unstick', 'unstick2') and displayed and table_mode == "instances":
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)
                            level = 2 if action == 'unstick2' else 1
                            level_desc = "Interrupting" if level == 2 else "Nudging"

                            # Non-destructive: no confirmation needed, run in background
                            unstick_feedback = (time.time(), f"{level_desc} {instance_name}...")
                            _refresh(live)

                            def _do_unstick(iid, iname, lvl):
                                global unstick_feedback
                                result = unstick_instance(iid, level=lvl)
                                sig = result.get("signal", "?") if result else "?"
                                if result and result.get("status") == "nudged":
                                    unstick_feedback = (time.time(), f"{sig}: {iname} - activity detected")
                                elif result and result.get("status") == "no_change":
                                    unstick_feedback = (time.time(), f"{sig}: {iname} - no change")
                                elif result and result.get("detail"):
                                    unstick_feedback = (time.time(), f"Failed: {result['detail'][:30]}")
                                else:
                                    unstick_feedback = (time.time(), f"Unstick failed for {iname}")
                                update_flag.set()

                            threading.Thread(target=_do_unstick, args=(instance_id, instance_name, level), daemon=True).start()

                    elif action == 'kill' and displayed and table_mode == "instances":
                        # Kill uses unstick level 3 (SIGKILL) - no confirmation needed
                        # since terminal is preserved and instance can be resumed
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id")
                            instance_name = format_instance_name(instance)
                            working_dir = instance.get("working_dir", "")

                            # Show immediate feedback, run in background
                            unstick_feedback = (time.time(), f"Killing {instance_name}...")
                            _refresh(live)

                            def _do_kill(iid, iname, wdir):
                                global unstick_feedback
                                result = unstick_instance(iid, level=3)
                                if result and result.get("status") in ("nudged", "no_change"):
                                    # SIGKILL always "works" - process is dead
                                    # Auto-copy resume command to clipboard
                                    if wdir:
                                        resume_cmd = f"cd {wdir} && claude --resume {iid}"
                                        copied, _ = copy_to_clipboard(resume_cmd)
                                        if copied:
                                            unstick_feedback = (time.time(), f"Killed {iname} - resume cmd copied!")
                                        else:
                                            unstick_feedback = (time.time(), f"Killed {iname} (use y to copy resume)")
                                    else:
                                        unstick_feedback = (time.time(), f"Killed {iname}")
                                elif result and result.get("detail"):
                                    unstick_feedback = (time.time(), f"Kill failed: {result['detail'][:30]}")
                                else:
                                    unstick_feedback = (time.time(), f"Kill failed for {iname}")
                                update_flag.set()

                            threading.Thread(target=_do_kill, args=(instance_id, instance_name, working_dir), daemon=True).start()

                    elif action == 'toggle_subagents':
                        show_subagents = not show_subagents
                        _clamp_selection()
                        _refresh(live)

                    elif action == 'filter':
                        # Cycle filter: all -> active -> stopped -> all
                        filter_cycle = {"all": "active", "active": "stopped", "stopped": "all"}
                        filter_mode = filter_cycle.get(filter_mode, "all")
                        _clamp_selection()
                        _refresh(live)

                    elif action == 'restart':
                        # Restart the Token-API server
                        global restart_feedback
                        restart_feedback = (time.time(), "Restarting server...")
                        _refresh(live)

                        def _do_restart():
                            global restart_feedback, api_healthy, api_error_message
                            try:
                                result = subprocess.run(
                                    ["token-restart"],
                                    capture_output=True, text=True, timeout=15
                                )
                                if result.returncode == 0:
                                    restart_feedback = (time.time(), "Restarted server!")
                                    # Give server a moment to come back up
                                    time.sleep(2)
                                    api_healthy, api_error_message = check_api_health()
                                else:
                                    restart_feedback = (time.time(), f"Restart failed: {result.stderr[:30]}")
                            except FileNotFoundError:
                                restart_feedback = (time.time(), "token-restart not found")
                            except subprocess.TimeoutExpired:
                                restart_feedback = (time.time(), "Restart timed out")
                            except Exception as e:
                                restart_feedback = (time.time(), f"Restart error: {str(e)[:25]}")
                            update_flag.set()

                        threading.Thread(target=_do_restart, daemon=True).start()

                    elif action == 'full_refresh':
                        # Ctrl+R: restart server + re-exec TUI to pick up code changes
                        live.stop()
                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)
                        console.print("\n[cyan bold]Full refresh: restarting server and TUI...[/cyan bold]")
                        try:
                            subprocess.run(["token-restart"], capture_output=True, text=True, timeout=15)
                            console.print("[green]Server restarted.[/green] Re-launching TUI...")
                            time.sleep(1)
                        except Exception as e:
                            console.print(f"[yellow]Server restart issue: {e}[/yellow] Re-launching TUI anyway...")
                            time.sleep(0.5)
                        # Re-exec this process to pick up code changes
                        quit_flag.set()
                        listener_thread.join(timeout=0.5)
                        os.execv(sys.executable, [sys.executable] + sys.argv)

                    elif action == 'open_terminal' and displayed and table_mode == "instances":
                        # Open a new terminal tab with resume command for selected instance
                        global resume_feedback
                        if 0 <= selected_index < len(displayed):
                            instance = displayed[selected_index]
                            instance_id = instance.get("id", "")
                            working_dir = instance.get("working_dir", "")
                            instance_name = format_instance_name(instance)

                            if not instance_id or not working_dir:
                                resume_feedback = (time.time(), "Missing instance data")
                            else:
                                resume_cmd = f"cd {working_dir} && claude --resume {instance_id}"
                                # Try to open in a new Windows Terminal tab
                                try:
                                    subprocess.Popen(
                                        ["cmd.exe", "/c", "start", "wt.exe", "-w", "0", "nt",
                                         "wsl.exe", "-e", "bash", "-ic", resume_cmd],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                                    )
                                    resume_feedback = (time.time(), f"Opened terminal for {instance_name}")
                                except FileNotFoundError:
                                    # Fallback: copy to clipboard
                                    copied, msg = copy_to_clipboard(resume_cmd)
                                    if copied:
                                        resume_feedback = (time.time(), f"Copied resume cmd (no wt.exe)")
                                    else:
                                        resume_feedback = (time.time(), msg)
                                except Exception as e:
                                    resume_feedback = (time.time(), f"Open failed: {str(e)[:25]}")
                        _refresh(live)

                    elif action == 'sort':
                        input_mode.set()
                        time.sleep(0.1)
                        live.stop()

                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)

                        console.print("\n[cyan bold]Sort instances by:[/cyan bold]")
                        console.print("  [yellow]1[/yellow] Status then recent activity (default)")
                        console.print("  [yellow]2[/yellow] Most recent activity")
                        console.print("  [yellow]3[/yellow] Most recently stopped")
                        console.print("  [yellow]4[/yellow] Instance creation time")
                        try:
                            choice = Prompt.ask("Choice", choices=["1", "2", "3", "4"], default="1")
                            sort_options = {
                                "1": "status",
                                "2": "recent_activity",
                                "3": "recent_stopped",
                                "4": "created"
                            }
                            sort_mode = sort_options.get(choice, "status")
                            console.print(f"[green]v[/green] Sorting by: {sort_mode.replace('_', ' ')}")
                        except (KeyboardInterrupt, EOFError):
                            console.print("[dim]Cancelled[/dim]")

                        time.sleep(0.3)
                        tty.setcbreak(sys.stdin.fileno())
                        input_mode.clear()
                        instances_cache = get_instances()
                        live.start()
                        _refresh(live)

                    elif action == 'page_prev':
                        panel_page = max(0, panel_page - 1)
                        # If user manually navigates away from Deploy during active deploy, disable auto-switch-back
                        if deploy_active and deploy_auto_switched and panel_page != 2:
                            deploy_auto_switched = False
                        _refresh(live)

                    elif action == 'page_next':
                        panel_page = min(PANEL_PAGE_MAX, panel_page + 1)
                        # If user manually navigates away from Deploy during active deploy, disable auto-switch-back
                        if deploy_active and deploy_auto_switched and panel_page != 2:
                            deploy_auto_switched = False
                        _refresh(live)

                    elif action == 'resume' and table_mode == "instances":
                        # Copy resume command to clipboard (y key)
                        if not displayed:
                            resume_feedback = (time.time(), "No instances")
                        elif not (0 <= selected_index < len(displayed)):
                            resume_feedback = (time.time(), "No instance selected")
                        else:
                            instance = displayed[selected_index]
                            instance_id = instance.get("id", "")
                            working_dir = instance.get("working_dir", "")
                            instance_name = format_instance_name(instance)

                            if not instance_id or not working_dir:
                                resume_feedback = (time.time(), "Missing instance data")
                            else:
                                resume_cmd = f"cd {working_dir} && claude --resume {instance_id}"
                                copied, msg = copy_to_clipboard(resume_cmd)
                                if copied:
                                    resume_feedback = (time.time(), f"Copied resume cmd for {instance_name}")
                                else:
                                    resume_feedback = (time.time(), msg)
                        _refresh(live)

                update_flag.clear()

                now_t = time.time()

                # Full refresh every REFRESH_INTERVAL: re-fetch instances, health, deploy
                if now_t - last_refresh >= REFRESH_INTERVAL:
                    # Check for remote TUI restart signal
                    tui_signal = check_tui_restart_signal(tui_slot)
                    if tui_signal:
                        live.stop()
                        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)
                        reason = tui_signal.get("reason", "unknown")
                        console.print(f"\n[cyan bold]Remote restart signal received ({reason}). Re-launching TUI...[/cyan bold]")
                        time.sleep(0.5)
                        quit_flag.set()
                        listener_thread.join(timeout=0.5)
                        os.execv(sys.executable, [sys.executable] + sys.argv)

                    old_count = len(instances_cache)
                    instances_cache = get_instances()
                    api_healthy, api_error_message = check_api_health()

                    # Auto-scroll to newest instance when new one appears
                    current_ids = set(i.get("id") for i in instances_cache)
                    new_ids = current_ids - prev_instance_ids
                    if new_ids and len(instances_cache) > old_count:
                        # Find the newest instance in the displayed (filtered) list
                        displayed = _get_displayed()
                        for idx, inst in enumerate(displayed):
                            if inst.get("id") in new_ids:
                                selected_index = idx
                                break
                    prev_instance_ids = current_ids

                    _clamp_selection()

                    # Deploy auto-switch logic
                    now_active, now_log, now_meta = check_deploy_status()
                    if now_active and not deploy_active:
                        # Deploy just started: save current page and switch to Deploy
                        deploy_previous_page = panel_page
                        panel_page = 2
                        deploy_auto_switched = True
                        deploy_log_path = now_log
                        deploy_metadata = now_meta
                    elif not now_active and deploy_active:
                        # Deploy just ended: switch back if we auto-switched
                        if deploy_auto_switched:
                            panel_page = deploy_previous_page
                            deploy_auto_switched = False
                        deploy_log_path = None
                        deploy_metadata = {}
                    deploy_active = now_active

                    _refresh(live)
                    last_refresh = now_t
                    last_timer_refresh = now_t

                # Lightweight timer-only refresh every 1s (re-renders with predicted timer)
                elif now_t - last_timer_refresh >= 1.0:
                    _refresh(live)
                    last_timer_refresh = now_t

                update_flag.wait(timeout=0.02)

    except KeyboardInterrupt:
        pass
    finally:
        quit_flag.set()
        # Wait for listener thread to exit cleanly
        listener_thread.join(timeout=0.5)
        # Restore terminal settings (critical for Ctrl+C cleanup)
        try:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, original_terminal_settings)
        except:
            pass
        console.print("\n[dim]Goodbye![/dim]")


if __name__ == "__main__":
    main()
