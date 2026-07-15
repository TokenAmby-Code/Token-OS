"""
Shared state, configuration, and utilities for Token-API.

This module exists to break circular imports between main.py and route modules.
State dicts live here; both main.py and routes/* import from this module.

Phase 2 will convert these raw dicts into TypedDicts/dataclasses.
"""

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import aiosqlite
from db_connections import connect_agents_db, connect_agents_db_sync, resolve_telemetry_db_path

logger = logging.getLogger("token_api")
_LOG_EVENT_WRITE_LOCK = threading.Lock()

# ============ Configuration ============

RUNTIME_DATABASE_DIR = Path(
    os.environ.get("TOKEN_API_DATABASE_DIR", Path.home() / "runtimes" / "database")
).expanduser()
DEFAULT_AGENTS_DB_PATH = RUNTIME_DATABASE_DIR / "agents.db"
DEFAULT_TIMER_DB_PATH = RUNTIME_DATABASE_DIR / "timer.db"
DEFAULT_TELEMETRY_DB_PATH = RUNTIME_DATABASE_DIR / "telemetry.db"


def _configured_agents_db_path() -> Path:
    """Resolve the Token-API agents database path.

    TOKEN_API_AGENTS_DB is the canonical override. TOKEN_API_DB remains a
    compatibility override for existing worktree/test isolation.
    """
    value = os.environ.get("TOKEN_API_AGENTS_DB") or os.environ.get("TOKEN_API_DB")
    return Path(value).expanduser() if value else DEFAULT_AGENTS_DB_PATH


def _configured_timer_db_path() -> Path:
    """Resolve the Token-API timer database path.

    TOKEN_API_TIMER_DB is the canonical override. If legacy TOKEN_API_DB is set
    and no timer-specific override exists, keep timer writes on that same
    isolated DB for existing dev/test harnesses. Production defaults split timer
    writes into ~/runtimes/database/timer.db.
    """
    value = os.environ.get("TOKEN_API_TIMER_DB") or os.environ.get("TOKEN_API_DB")
    return Path(value).expanduser() if value else DEFAULT_TIMER_DB_PATH


def _configured_telemetry_db_path() -> Path:
    """Resolve the high-frequency telemetry database path.

    Production stores telemetry beside agents.db/timer.db.  Test/dev worktrees
    that still set TOKEN_API_DB get a sibling telemetry.db so the telemetry
    split remains true without touching live state.
    """
    return resolve_telemetry_db_path()


DB_PATH = _configured_agents_db_path()
AGENTS_DB_PATH = DB_PATH
TIMER_DB_PATH = _configured_timer_db_path()
TELEMETRY_DB_PATH = _configured_telemetry_db_path()


@asynccontextmanager
async def hook_db():
    """Autocommit DB connection for hook hot paths.

    Hook handlers perform slow side effects (tmux, vault/frontmatter, routing).
    A deferred SQLite transaction can hold the write lock across those awaits.
    Autocommit commits each statement immediately so write locks are not held
    across non-DB awaits; explicit commits remain harmless no-ops.
    """

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await connect_agents_db(DB_PATH, timeout=5.0, isolation_level=None)
    try:
        yield db
    finally:
        await db.close()


def _vault_root() -> Path:
    """Resolve the Obsidian vault root at CALL time (never frozen at import).

    Import-time freezing here let test runs pollute the live vault: with
    IMPERIUM_ENV unset and /Volumes/Imperium mounted, the session dirs bound the
    live vault before any test fixture could redirect them.  Reading env per call
    lets the test isolation fixture point writes at a temp dir.
    """
    env = os.environ.get("IMPERIUM_ENV")
    if env:
        return Path(env)
    imperium = Path(os.environ.get("IMPERIUM", "/Volumes/Imperium"))
    if not imperium.exists():
        imperium = Path.home()
    return imperium / "Imperium-ENV"


def default_sessions_dir() -> Path:
    """Terra/Sessions under the live vault, resolved lazily."""
    return _vault_root() / "Terra" / "Sessions"


def mars_sessions_dir() -> Path:
    """Mars/Sessions under the live vault, resolved lazily."""
    return _vault_root() / "Mars" / "Sessions"


SERVER_PORT = 7777
CRASH_LOG_PATH = Path.home() / ".claude" / "token-api-crash.log"
STASH_DIR = Path.home() / ".claude" / "stash"
STASH_MAX_AGE_HOURS = 24
QUIET_HOURS_START = int(os.environ.get("TOKEN_API_QUIET_START_HOUR", "23"))
QUIET_HOURS_END = int(os.environ.get("TOKEN_API_QUIET_END_HOUR", "7"))
QUIET_HOURS_TIMEZONE = os.environ.get("TOKEN_API_QUIET_TIMEZONE", "America/Phoenix")
# Only an explicit/official morning action releases the morning quiet latch
# early. day_state is written by exactly two paths: the automated
# schedule_fallback wake-anchor (source="schedule_fallback") — which fired while
# the Emperor slept and must NOT release quiet — and /api/day-start/fire (the
# documented "single morning latch") whose human/official sources are
# alarm_silenced|manual|custodes. The automated "schedule"/"schedule_fallback"
# are deliberately excluded; if early release never fires the 07:00 clock
# boundary (TOKEN_API_QUIET_END_HOUR default) still ends quiet hours. Kept in
# sync with tmuxctl.send_gate.
OFFICIAL_MORNING_SOURCES = frozenset(
    s.strip()
    for s in os.environ.get(
        "TOKEN_API_MORNING_SOURCES", "alarm_silenced,manual,custodes,morning"
    ).split(",")
    if s.strip()
)
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
    with contextlib.closing(sqlite3.connect(path)) as conn, conn:
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
        with contextlib.closing(sqlite3.connect(db_path or DB_PATH)) as conn, conn:
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
    with contextlib.closing(sqlite3.connect(db_path or DB_PATH)) as conn, conn:
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
        _DAY_STATE_CACHE.update({"date": date_str, "value": result, "monotonic": time.monotonic()})
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
    day_source = day_state.get("source") if day_state else None

    if (
        active
        and segment == "morning_latch"
        and day_started_at
        and day_source in OFFICIAL_MORNING_SOURCES
    ):
        # Released only by the official morning system. A schedule_fallback
        # wake-anchor (or any non-morning source) leaves the latch ON.
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
        "day_source": day_source,
        "day_state_date": local_now.date().isoformat(),
        "quiet_segment": segment,
    }


# ============ Voice Profiles / Persona Registry ============

# Runtime persona data lives in the SQLite ``personas`` table. These lists are
# compatibility projections for older endpoints that still speak in terms of
# profile_name/wsl_voice while the main instances table refactor lands.
from personas import (  # noqa: E402
    BACKUP_PROFILES,
    PERSONA_COMPAT_PROFILES,
    PRIMARY_PROFILES,
    PROFILE_BY_SLUG,
    ULTIMATE_FALLBACK_PROFILE,
)

PROFILES = PRIMARY_PROFILES
FALLBACK_VOICES = BACKUP_PROFILES
ULTIMATE_FALLBACK = ULTIMATE_FALLBACK_PROFILE
CUSTODES_PROFILE = PROFILE_BY_SLUG["custodes"]
PERSONA_PROFILES = [p for p in PERSONA_COMPAT_PROFILES if p["default_rank"] != "astartes"]


def resolve_persona_profile(primarch: str | None, legion: str | None = None) -> dict:
    """Compatibility resolver for persona panes.

    The canonical DB resolver is ``personas.resolve_persona``. This seed-backed
    projection is used during hook registration before the broader instances
    refactor wires ``persona_id`` directly into every call site.
    """
    slug = (primarch or legion or "persona").strip().lower()
    return PROFILE_BY_SLUG.get(
        slug,
        {
            "id": None,
            "name": slug,
            "slug": slug,
            "chapter": slug.replace("-", " ").title(),
            "display_name": slug.replace("-", " ").title(),
            "default_rank": "overseer",
            "assignment_pool": None,
            "assignment_order": None,
            "wsl_voice": None,
            "wsl_rate": None,
            "mac_voice": None,
            "notification_sound": None,
            "color": "#302800",
            "chip_color": "#302800",
            "pane_tint": None,
            "tts_voice": None,
            "tts_rate": None,
            "silent": True,
        },
    )


def profile_by_name(profile_name: str | None) -> dict | None:
    """Resolve a stored ``profile_name``/persona slug to its compatibility dict."""
    if not profile_name:
        return None
    return PROFILE_BY_SLUG.get(profile_name)


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

# In-process provenance for hook-driven autonomous wakes.  The durable
# ``instances.hook_driven`` bit answers "was this row driven by automation?";
# this sidecar answers "which automation did it?" for the narrow places that
# must avoid self-feeding loops without adding broad notification suppression.
HOOK_DRIVEN_ACTORS: dict[str, str] = {}


def note_hook_driven_actor(instance_id: str | None, actor: str) -> None:
    """Remember the actor for a hook-driven wake until that instance stops."""
    if not instance_id:
        return
    HOOK_DRIVEN_ACTORS[str(instance_id)] = str(actor or "")


def pop_hook_driven_actor(instance_id: str | None) -> str | None:
    """Consume the actor sidecar for an instance stop."""
    if not instance_id:
        return None
    return HOOK_DRIVEN_ACTORS.pop(str(instance_id), None)


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
    input=None,
    text: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run short tmux resolver subprocesses without forking on the event loop."""
    return await asyncio.to_thread(
        subprocess.run,
        list(args),
        stdout=stdout,
        stderr=stderr,
        input=input,
        text=text,
        env=env,
        timeout=timeout,
        check=False,
    )


async def _resolve_tmux_pane_direct(tmux_pane: str) -> str | None:
    stdout = await tmuxctld_stdout(
        ("display-message", "-t", tmux_pane, "-p", "#{pane_id}"),
        timeout=1,
    )
    return (stdout or "").strip() or None


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
    result = await asyncio.to_thread(
        _tmuxctld_get_value,
        "/resolve-pane",
        {"target": tmux_pane, "format": "physical"},
        timeout=3,
        default_loopback=True,
    )
    if result is not None:
        pane_id = str(result or "").strip()
        if pane_id:
            _TMUX_PANE_RESOLVE_CACHE[tmux_pane] = (now, pane_id)
            return pane_id
    pane_id = await _resolve_tmux_pane_direct(tmux_pane)
    _TMUX_PANE_RESOLVE_CACHE[tmux_pane] = (now, pane_id)
    return pane_id


async def tmux_pane_exists(tmux_pane: str | None) -> bool:
    return await resolve_tmux_pane_id(tmux_pane) is not None


# ── Persona pane tint (event-driven) ────────────────────────────────────────
# Pane background colour is resolved from canonical instances.persona_id →
# personas.pane_tint and applied by setting per-pane style options directly.
# Claude slash-color is not used.


def apply_pane_tint(
    tmux_pane: str | None, pane_tint: str | None, *, source: str = "pane-tint"
) -> None:
    """Paint a pane's background with an already-resolved persona tint.

    Event-driven — call this when an instance registers or changes persona
    (apply the colour) or vacates a pane (pass ``default`` to clear).
    Synchronous tmuxctld transport; async callers should wrap it in
    asyncio.to_thread.
    """
    if not tmux_pane:
        return
    bg = pane_tint or "default"
    try:
        style_args: list[tuple[str, ...]]
        if not bg or bg == "default":
            style_args = [
                ("set-option", "-pu", "-t", tmux_pane, "window-style"),
                ("set-option", "-pu", "-t", tmux_pane, "window-active-style"),
            ]
        else:
            style = f"bg={bg}"
            style_args = [
                ("set-option", "-p", "-t", tmux_pane, "window-style", style),
                ("set-option", "-p", "-t", tmux_pane, "window-active-style", style),
            ]
        for args in style_args:
            _tmuxctld_run_tmux(args, timeout=2)
    except Exception as exc:
        logger.warning("pane tint failed for %s (bg=%s): %s", tmux_pane, bg, exc)


def clear_pane_tint(tmux_pane: str | None, *, source: str = "pane-tint-clear") -> None:
    """Clear a pane's persona tint back to tmux default."""
    apply_pane_tint(tmux_pane, "default", source=source)


async def apply_instance_pane_tint(
    db,
    instance_id: str | None,
    tmux_pane: str | None,
    *,
    source: str = "pane-tint",
) -> str:
    """Resolve and apply pane tint from canonical ``instances.persona_id``."""
    from personas import persona_tint_for_instance

    bg = await persona_tint_for_instance(db, instance_id)
    if tmux_pane:
        await asyncio.to_thread(apply_pane_tint, tmux_pane, bg, source=source)
    return bg


# ── Engine-agnostic pushed statusline @-vars ──
# Pushed tmux @-vars use zero #() shell-outs and zero per-pane polling — the
# 2026-06-05 freeze lesson. Values source from the
# engine-agnostic ``instances`` table via the existing pane_state_queue → 1s
# pane_state_worker → ``tmux set-option -p`` path, so Claude and Codex panes light
# up identically (only ``instances.engine`` distinguishes them, and nothing here
# branches on it). Triggers can't JOIN, so display values are resolved in
# application code at the points where the underlying field is set, then enqueued.


def cwd_basename(working_dir: str | None) -> str:
    """Basename of an instance working_dir for the @CWD status field.

    Trailing slashes are stripped first so ``/a/b/`` → ``b`` (not ``""``). Root
    ``/`` and empty/None collapse to ``""`` — the border treats "" as unset and
    renders the empty branch of ``#{?@CWD,…,}``.
    """
    if not working_dir:
        return ""
    return Path(working_dir.rstrip("/")).name or ""


async def _persona_display_name(db, persona_id) -> str:
    """Resolve ``personas.display_name`` from ``instances.persona_id`` (or "")."""
    if not persona_id:
        return ""
    cursor = await db.execute("SELECT display_name FROM personas WHERE id = ?", (persona_id,))
    row = await cursor.fetchone()
    if not row:
        return ""
    # Row may be a tuple or aiosqlite.Row depending on the connection's row_factory.
    return (row[0] if row[0] else "") or ""


async def _session_doc_title(db, session_doc_id) -> str:
    """Resolve ``session_documents.title`` from ``instances.session_doc_id`` (or "")."""
    if not session_doc_id:
        return ""
    cursor = await db.execute("SELECT title FROM session_documents WHERE id = ?", (session_doc_id,))
    row = await cursor.fetchone()
    if not row:
        return ""
    return (row[0] if row[0] else "") or ""


async def queue_pane_var(
    db: "aiosqlite.Connection",
    instance_id: str,
    variable: str,
    value: str | None,
) -> None:
    """Enqueue one pushed pane @-var via ``pane_state_queue``.

    Mirrors the trigger INSERTs (``trg_tab_name_pane_state`` etc.) so the single 1s
    ``pane_state_worker`` stays the only writer of pane options. ``value`` is coerced
    to "" when None so the NOT NULL ``value`` column never aborts the insert;
    consumers treat "" as unset (``#{?@VAR,…,}`` conditionals render empty
    branches). The caller's transaction owns the commit. No pane id is stored — the
    worker re-resolves the live pane from ``instance_id`` per drain.
    """
    await db.execute(
        """INSERT INTO pane_state_queue (instance_id, variable, value)
           VALUES (?, ?, ?)""",
        (instance_id, variable, value or ""),
    )


async def push_agnostic_pane_vars(
    db: "aiosqlite.Connection", instance_id: str | None
) -> dict[str, str]:
    """Resolve + enqueue engine-agnostic statusline identity vars from the canonical row.

    Reads ``persona_id``/``session_doc_id``/``working_dir`` from the freshly-written
    ``instances`` row (uncommitted writes on the same connection are visible) and
    enqueues ``@PERSONA``/``@SESSION_DOC``/``@CWD``. Identical for ``engine='codex'``
    and ``engine='claude'`` — agnosticism by construction. No pane id is read or
    stored; the worker re-resolves the live pane from ``instance_id`` per drain.
    Best-effort: never raises into a caller in a registration/legion critical
    section. Returns the resolved values (for tests).
    """
    if not instance_id:
        return {}
    try:
        cursor = await db.execute(
            "SELECT persona_id, session_doc_id, working_dir FROM instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {}
        persona_id, session_doc_id, working_dir = (row[0], row[1], row[2])
        values = {
            "@PERSONA": await _persona_display_name(db, persona_id),
            "@SESSION_DOC": await _session_doc_title(db, session_doc_id),
            "@CWD": cwd_basename(working_dir),
        }
        for variable, value in values.items():
            await queue_pane_var(db, instance_id, variable, value)
        return values
    except Exception as exc:  # pragma: no cover - defensive, never fail the caller
        logger.warning("push_agnostic_pane_vars failed for %s: %s", instance_id, exc)
        return {}


# ============ tmuxctld loopback client ============
#
# Fast path: pane/instance resolution prefers the local tmuxctld HTTP daemon
# (loopback, stdlib-only) over a fresh subprocess; callers can use the default
# loopback daemon even when TMUXCTLD_URL is not exported. The subprocess path
# remains the fail-closed fallback when the daemon is disabled, absent, or errors.
# Requests go through an opener built with an EMPTY ProxyHandler so a loopback
# call to 127.0.0.1 never gets routed through a system/env HTTP proxy (http_proxy
# / HTTPS_PROXY / macOS system proxy) — that would break or hang it.

_TMUXCTLD_OPENER: urllib.request.OpenerDirector = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)

# The daemon is loopback-only and unauthenticated; the client refuses to speak to
# anything but a loopback host, so a stray/hostile TMUXCTLD_URL cannot turn this
# into an SSRF or exfiltration vector.
_TMUXCTLD_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _tmuxctld_url(*, default_loopback: bool = False) -> str | None:
    """Return the configured tmuxctld base URL, or None.

    Trailing slash trimmed; only ``http`` loopback URLs are honoured — a
    non-loopback (or non-http) host is rejected (returns None) so the client
    never reaches off-box.
    """
    configured = str(os.environ.get("TMUXCTLD_URL") or "").strip()
    if configured.lower() in {"0", "false", "off", "disabled"}:
        return None
    url = (configured or ("http://127.0.0.1:7778" if default_loopback else "")).rstrip("/")
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or (parsed.hostname or "") not in _TMUXCTLD_LOOPBACK_HOSTS:
        return None
    return url


def _tmuxctld_get_json(
    path: str,
    params: dict[str, str],
    *,
    timeout: float = 0.5,
    default_loopback: bool = False,
) -> dict | None:
    """GET ``path`` from the tmuxctld daemon and return the unwrapped ``result`` dict.

    Returns None when the daemon is not configured, is unreachable, replies
    non-200, or returns a non-``ok`` envelope — so every caller falls through to
    its subprocess fallback. Proxy bypass is enforced via the module opener.
    """
    base = _tmuxctld_url(default_loopback=default_loopback)
    if not base:
        return None
    query = urllib.parse.urlencode(params)
    url = f"{base}{path}?{query}" if query else f"{base}{path}"
    try:
        with _TMUXCTLD_OPENER.open(url, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            payload = json.loads(resp.read().decode(errors="ignore") or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    result = payload.get("result")
    return result if isinstance(result, dict) else None


def _tmuxctld_get_value(
    path: str,
    params: dict[str, str],
    *,
    timeout: float = 0.5,
    default_loopback: bool = False,
) -> object | None:
    """GET ``path`` from tmuxctld and return any ok result value."""

    base = _tmuxctld_url(default_loopback=default_loopback)
    if not base:
        return None
    query = urllib.parse.urlencode(params)
    url = f"{base}{path}?{query}" if query else f"{base}{path}"
    try:
        with _TMUXCTLD_OPENER.open(url, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            payload = json.loads(resp.read().decode(errors="ignore") or "{}")
    except Exception:
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return None
    return payload.get("result")


def _tmuxctld_post_json(
    path: str, body: dict, *, timeout: float = 10.0, default_loopback: bool = False
) -> dict | None:
    """POST JSON to tmuxctld and return the full daemon envelope.

    Unlike the GET resolver helper, callers need both ok:true results and
    ok:false structured errors such as code=gated. Returns None only for absent
    config, transport errors, non-200, or malformed responses.
    """
    base = _tmuxctld_url(default_loopback=default_loopback)
    if not base:
        return None
    url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _TMUXCTLD_OPENER.open(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            payload = json.loads(resp.read().decode(errors="ignore") or "{}")
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _tmuxctld_run_tmux(
    args: list[str] | tuple[str, ...],
    *,
    timeout: float = 5.0,
    default_loopback: bool = True,
) -> dict | None:
    """Run an allowlisted tmux argv through tmuxctld's loopback transport.

    Token-API must not shell out to tmux directly.  This helper uses the existing
    tmuxctld JSON client; the daemon owns the real TmuxAdapter call and rejects
    non-allowlisted operations.
    """

    envelope = _tmuxctld_post_json(
        "/tmux/run",
        {"args": [str(arg) for arg in args]},
        timeout=timeout,
        default_loopback=default_loopback,
    )
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return None
    result = envelope.get("result")
    return result if isinstance(result, dict) else None


async def tmuxctld_run_tmux(
    args: list[str] | tuple[str, ...],
    *,
    timeout: float = 5.0,
    default_loopback: bool = True,
) -> dict | None:
    return await asyncio.to_thread(
        _tmuxctld_run_tmux,
        args,
        timeout=timeout,
        default_loopback=default_loopback,
    )


async def tmuxctld_stdout(
    args: list[str] | tuple[str, ...],
    *,
    timeout: float = 5.0,
    default_loopback: bool = True,
) -> str | None:
    result = await tmuxctld_run_tmux(
        args,
        timeout=timeout,
        default_loopback=default_loopback,
    )
    if result is None:
        return None
    return str(result.get("stdout") or "")


async def resolve_instance_pane(instance_id: str | None) -> tuple[str | None, str | None]:
    """Resolve an instance UUID to its live ``(pane_id, role)`` via tmuxctl.

    tmuxctl is the sole owner of ``instance_id -> pane`` resolution, computed
    live from the pane's ``@INSTANCE_ID`` stamp. token-api stores no tmux pane
    perspective; this is the only bridge. Fails closed: any miss, error, or
    unstamped/dead pane returns ``(None, None)`` so callers never send to — or
    speak the position of — a pane that no longer exists.
    """
    if not instance_id:
        return (None, None)
    # Fast path: prefer the launchd-supervised tmuxctld loopback daemon. Token-API
    # runs under launchd, where a fresh tmux subprocess can fail closed even
    # while the interactive tmux server is healthy; tmuxctld is the local
    # canonical liveness oracle for that service context. Its
    # /tmux/resolve-instance result is canonical-only and fail-closed, so a
    # `found:false` is authoritative — no subprocess fallback in that case.
    result = await asyncio.to_thread(
        _tmuxctld_get_json,
        "/tmux/resolve-instance",
        {"instance_id": instance_id},
        # First loopback hit after daemon/code churn can spend just over 0.5s in
        # the daemon handler; keep this tight but not so tight Token-API falls
        # back to launchd's weaker subprocess context and reports false-dead panes.
        timeout=1.0,
        default_loopback=True,
    )
    if result is not None:
        if not result.get("found"):
            return (None, None)
        pane_id = (result.get("pane_id") or "").strip() or None
        role = (result.get("pane_role") or "").strip() or None
        return (pane_id, role)
    return (None, None)


async def instance_id_for_pane(pane: str | None) -> str | None:
    """Reverse of :func:`resolve_instance_pane`: read a pane's live ``@INSTANCE_ID``
    stamp (``pane -> instance_id``).

    tmuxctld and the agent wrapper own the stamp lifecycle, so the pane itself is
    the authoritative reverse bridge. token-api keeps
    no tmux-pane perspective; this is the only reverse lookup, replacing every
    legacy stored-pane query. Fails closed: any
    miss, error, or unstamped/dead pane returns ``None`` so callers never act on a
    stale or reused pane.
    """
    if not (pane or "").strip():
        return None
    # tmuxctld is the only tmux boundary. A miss, absent daemon, or empty stamp
    # fails closed to None.
    result = await asyncio.to_thread(
        _tmuxctld_get_json,
        "/tmux/instance-id-for-pane",
        {"pane": pane or ""},
        timeout=1.0,
        default_loopback=True,
    )
    if result is not None:
        return (result.get("instance_id") or "").strip() or None
    return None


async def tmuxctld_rename_pane(
    *, instance_id: str | None = None, pane: str | None = None, name: str
) -> dict | None:
    """Semantic rename of a pane's identity via tmuxctld ``POST /instance/rename``.

    tmuxctld is the sole writer of pane identity: it owns BOTH the ``@PANE_LABEL``
    border nametag and the native pane title. token-api no longer authors a raw
    ``set-option @PANE_LABEL`` through ``/tmux/run``. Pass either an already
    live-resolved ``pane`` or an ``instance_id`` for the daemon to resolve; the
    daemon fails closed (``result.found = False``) on an unresolved target.

    Returns the full daemon envelope (``{ok, result}``), or None for a transport
    error / absent daemon config.
    """
    body: dict[str, str] = {"name": name}
    if pane:
        body["pane"] = pane
    if instance_id:
        body["instance_id"] = instance_id
    return await asyncio.to_thread(
        _tmuxctld_post_json, "/instance/rename", body, default_loopback=True
    )


async def tmuxctld_stamp_instance(
    *,
    instance_id: str,
    pane: str | None = None,
    wrapper_id: str | None = None,
    persona: str | None = None,
    engine: str | None = None,
    working_dir: str | None = None,
    vacate_pane: str | None = None,
) -> dict | None:
    """Bind the canonical instance id to a pane via tmuxctld ``POST /instance/stamp``.

    tmuxctld is the SOLE writer of the durable ``@INSTANCE_ID`` pane stamp — the
    single-writer counterpart of :func:`tmuxctld_rename_pane` for ``@PANE_LABEL``.
    token-api resolves the canonical ``instances`` row id at SessionStart and hands
    it here; it NEVER authors a raw ``set-option @INSTANCE_ID`` (through ``/tmux/run``
    or otherwise). ``@INSTANCE_ID`` is the bootstrap identity pane resolution depends
    on, so it is stamped onto an EXPLICIT ``pane`` (the SessionStart-resolved live
    pane); ``wrapper_id`` is a ledger-resolved fallback. The daemon fails closed
    (``stamped = False``) on an unresolved target — never a stamp against a wrong or
    dead pane. ``vacate_pane`` guarded-clears a prior pane on a genuine pane move.

    Returns the daemon envelope (``{ok, result}``), or None for a transport error /
    absent daemon config. Best-effort: registration must not depend on tmuxctld being
    up (wrapperstart/reconcile rebuild the ledger; the stamp lands on the next fire).
    """
    if not (instance_id or "").strip():
        return None
    body: dict[str, str] = {"instance_id": instance_id}
    if pane:
        body["pane"] = pane
    if wrapper_id:
        body["wrapper_id"] = wrapper_id
    if persona:
        body["persona"] = persona
    if engine:
        body["engine"] = engine
    if working_dir:
        body["working_dir"] = working_dir
    if vacate_pane:
        body["vacate_pane"] = vacate_pane
    return await asyncio.to_thread(
        _tmuxctld_post_json, "/instance/stamp", body, default_loopback=True
    )


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
        # Use /server-heartbeat — hitting /notify fires the Notify macro (vibrate + TTS)
        # which produced spurious vibrations on every health probe.
        resp = requests.get(f"http://{host}:{port}/server-heartbeat", timeout=2)
        available = resp.status_code == 200
    except Exception:
        available = False

    PHONE_TTS_CONFIG["reachable"] = available
    PHONE_TTS_CONFIG["last_health_check"] = now
    if available:
        logger.info("TTS: Phone MacroDroid HTTP reachable (diagnostic; not audio-proxy health)")
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
    # min_gap_seconds is single-lane actuator serialization (delays, never drops)
    # — NOT a cooldown/cap. Daily caps, zap/soft cooldowns, and dedup windows were
    # removed per the Enforcement Dedup Removal decree; they masked false-fires.
    "min_gap_seconds": 2.0,
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
    # Deskflow KVM client presence — heartbeated by the Mac deskflow-client
    # supervisor (Shell/deskflow-client-supervisor.py). While the client is
    # connected the Emperor is at his desk; compute_work_state treats a FRESH
    # heartbeat as auto active-process work evidence so genuine desk work counts
    # as WORK instead of decaying to idle_break. Ages out via TTL when the client
    # disconnects/quiets. Complements (does not replace) the explicit
    # work-action / typing-guard signals. Live heartbeat — not persisted across
    # restart (see _RESTART_STATE_DENYLIST); the supervisor republishes on boot.
    "deskflow_active": False,
    "deskflow_last_seen": None,  # ISO-8601 of the last active heartbeat
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

# Re-injected on the clean Stop of a sync (morning) instance — but ONLY while an
# active, in-bound morning session exists (see morning_session.morning_session_active).
# The morning session is temporally bound, not turn-based: it must not be possible
# to walk away and have it sit idle. Each Stop yields a fresh timestamped keepalive
# that pushes the instance to keep moving via tts / AskUserQuestion. The loop exits
# when the morning session ends (POST /api/morning/end) or trips the 2-hour bound;
# after either, `sync` no longer re-injects.
MORNING_KEEPALIVE_PROMPT = (
    "It is currently {ts} MST. The morning session is still active. Use `tts` or "
    "AskUserQuestion to keep things moving — advance the regiment/plan, prompt the "
    "Emperor, pace yourself by blocking on AskUserQuestion between phases. This "
    "session stays alive until the Emperor officially ends it; when he does, call "
    "`POST /api/morning/end` (or PATCH /api/instances/{{id}}/golden-throne "
    'with {"mode":"off"} as a rip cord) to exit the loop.'
)

# Sent ONCE to the morning pane when the {hours}-hour bound trips. It is a notice,
# not a keepalive — the session is already auto-ended, so this does not re-prompt.
MORNING_EXPIRY_NOTICE = (
    "The morning session has reached its {hours}-hour limit and has been "
    "automatically ended. No further keepalive prompts will be sent. Wrap up "
    "cleanly — a brief closing `tts` if appropriate, then stop. You remain the "
    "Custodes singleton for state-hook interventions; only the morning loop is over."
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
                with connect_agents_db_sync(DB_PATH, timeout=5.0, site="shared.log_event") as conn:
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


def _timer_value(name: str, default=None):
    value = getattr(timer_engine, name, default)
    if hasattr(value, "value"):
        return value.value
    return value


def _coerce_work_state_value(work_state, name: str, default=None):
    if work_state is None:
        return default
    if isinstance(work_state, dict):
        return work_state.get(name, default)
    return getattr(work_state, name, default)


def _ensure_timer_samples_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS timer_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            mode TEXT NOT NULL,
            activity TEXT,
            productivity_active INTEGER,
            break_balance_ms INTEGER,
            break_backlog_ms INTEGER,
            work_time_ms INTEGER,
            active_instance_count INTEGER,
            processing_recent_count INTEGER,
            observed_agent_count INTEGER,
            desktop_mode TEXT,
            phone_app TEXT,
            source TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_timer_samples_timestamp ON timer_samples(timestamp)"
    )


def _sync_count_agent_rows(sql: str, timer_conn=None) -> int:
    """Run a read-only count against the agents DB.

    Timer writes live in TIMER_DB_PATH, but timer samples still include current
    instance counts from the agents DB. When both paths intentionally point at
    one isolated test DB, reuse the caller's connection.
    """
    try:
        if timer_conn is not None and TIMER_DB_PATH.resolve() == DB_PATH.resolve():
            cursor = timer_conn.execute(sql)
            return int(cursor.fetchone()[0] or 0)
        with contextlib.closing(sqlite3.connect(DB_PATH, timeout=5.0)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            cursor = conn.execute(sql)
            return int(cursor.fetchone()[0] or 0)
    except Exception as exc:
        logger.warning("timer agent-count read failed: %s", exc)
        return 0


def _sync_write_timer_sample(source: str, work_state=None, timestamp: str | None = None) -> None:
    """Persist a point-in-time timer read-model sample."""
    TIMER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TIMER_DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        _sync_insert_timer_sample(conn, source=source, work_state=work_state, timestamp=timestamp)
        conn.commit()
    finally:
        conn.close()


def _sync_insert_timer_sample(
    conn,
    source: str,
    work_state=None,
    timestamp: str | None = None,
    active_instances: int | None = None,
) -> None:
    """Insert a timer sample using the current in-memory timer/attention state."""
    _ensure_timer_samples_table(conn)

    if active_instances is None:
        active_instances = _coerce_work_state_value(work_state, "active_instance_count")
    if active_instances is None:
        active_instances = _sync_count_agent_rows(
            "SELECT COUNT(*) FROM instances WHERE status NOT IN ('stopped', 'archived') AND COALESCE(is_subagent, 0) = 0",
            conn,
        )

    processing_recent = _coerce_work_state_value(work_state, "processing_recent_count")
    if processing_recent is None:
        processing_recent = _sync_count_agent_rows(
            "SELECT COUNT(*) FROM instances WHERE status = 'working' AND COALESCE(is_subagent, 0) = 0",
            conn,
        )

    observed_agents = _coerce_work_state_value(work_state, "observed_agent_count")
    if observed_agents is None:
        observed_agents = active_instances

    productivity_active = _coerce_work_state_value(
        work_state, "productivity_active", _timer_value("productivity_active", None)
    )

    conn.execute(
        """
        INSERT INTO timer_samples (
            timestamp, mode, activity, productivity_active,
            break_balance_ms, break_backlog_ms, work_time_ms,
            active_instance_count, processing_recent_count, observed_agent_count,
            desktop_mode, phone_app, source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp or datetime.now().isoformat(),
            _timer_value("current_mode", "unknown"),
            _timer_value("activity", None),
            1 if productivity_active else 0,
            int(_timer_value("break_balance_ms", 0) or 0),
            abs(min(0, int(_timer_value("break_balance_ms", 0) or 0))),
            int(_timer_value("total_work_time_ms", 0) or 0),
            int(active_instances or 0),
            int(processing_recent or 0),
            int(observed_agents or 0),
            DESKTOP_STATE.get("current_mode", "silence"),
            PHONE_STATE.get("current_app"),
            source,
        ),
    )


def _sync_log_shift(
    old_mode, new_mode: str, trigger: str, source: str, phone_app=None, details=None
):
    """Log a timer mode shift to the analytics table (sync, for thread offload)."""
    from datetime import datetime as _dt

    TIMER_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(TIMER_DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")

    active_instances = _sync_count_agent_rows(
        "SELECT COUNT(*) FROM instances WHERE status NOT IN ('stopped', 'archived') AND COALESCE(is_subagent, 0) = 0",
        conn,
    )

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
    _sync_insert_timer_sample(conn, source=source, active_instances=active_instances)
    conn.commit()
    conn.close()


async def timer_write_sample(source: str = "timer_worker", work_state=None):
    """Persist a timer sample asynchronously."""
    import asyncio

    try:
        await asyncio.to_thread(_sync_write_timer_sample, source, work_state)
    except Exception as e:
        print(f"TIMER: Failed to write sample: {e}")


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
