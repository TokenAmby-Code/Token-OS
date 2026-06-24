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
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger("token_api")
_LOG_EVENT_WRITE_LOCK = threading.Lock()

# ============ Configuration ============

DB_PATH = Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))


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
            "pane_tint": "#302800",
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
            env={**os.environ, "IMPERIUM_TMUX_RAW": "1"},
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
            (sys.executable, "-m", "tmuxctl.cli", "resolve-pane", tmux_pane),
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


# ── Persona pane tint (event-driven) ────────────────────────────────────────
# Pane background colour is resolved from canonical instances.persona_id →
# personas.pane_tint and applied only with `tmux select-pane -P bg=...`.
# Claude slash-color is not used.


def apply_pane_tint(
    tmux_pane: str | None, pane_tint: str | None, *, source: str = "pane-tint"
) -> None:
    """Paint a pane's background with an already-resolved persona tint.

    Event-driven — call this when an instance registers or changes persona
    (apply the colour) or vacates a pane (pass ``default`` to clear).
    Synchronous (runs a tmux subprocess); async callers should wrap it in
    asyncio.to_thread.
    """
    if not tmux_pane:
        return
    bg = pane_tint or "default"
    cli_lib = Path(__file__).resolve().parents[1] / "cli-tools" / "lib"
    try:
        import sys

        if str(cli_lib) not in sys.path:
            sys.path.insert(0, str(cli_lib))
        from tmuxctl.tmux_adapter import TmuxAdapter
    except Exception as exc:  # tmuxctl unavailable (e.g. non-tmux host)
        logger.warning("pane tint: tmuxctl adapter unavailable (%s)", exc)
        return
    try:
        adapter = TmuxAdapter()
        voice_locked = adapter.run(
            "show-options",
            "-pqv",
            "-t",
            tmux_pane,
            "@DISCORD_VOICE_LOCK",
            allow_failure=True,
        ).strip()
        if voice_locked == "1":
            return
        # select-pane -P is camera-neutral (style only) — no focus snapshot or
        # restore needed around it.
        adapter.run("select-pane", "-t", tmux_pane, "-P", f"bg={bg}", allow_failure=True)
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
# The pane-border nametag reads pushed @-vars only (zero #() shell-outs, zero
# per-pane polling — the 2026-06-05 freeze lesson). Every field sources from the
# engine-agnostic ``instances`` table via the existing pane_state_queue → 1s
# pane_state_worker → ``tmux set-option -p`` path, so Claude and Codex panes light
# up identically (only ``instances.engine`` distinguishes them, and nothing here
# branches on it). Triggers can't JOIN, so display values are resolved in
# application code at the points where the underlying field is set, then enqueued.


def cwd_basename(working_dir: str | None) -> str:
    """Basename of an instance working_dir for the @CWD nametag field.

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
    to "" when None so the NOT NULL ``value`` column never aborts the insert; the
    border treats "" as unset (the ``#{?@VAR,…,}`` conditional renders the empty
    branch). The caller's transaction owns the commit. No pane id is stored — the
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
    """Resolve + enqueue the engine-agnostic nametag vars from the canonical row.

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
# Optional fast path: when TMUXCTLD_URL is set, pane/instance resolution prefers
# the local tmuxctld HTTP daemon (loopback, stdlib-only) over a fresh subprocess.
# The subprocess path remains the fail-closed fallback when the daemon is absent
# or errors. Requests go through an opener built with an EMPTY ProxyHandler so a
# loopback call to 127.0.0.1 never gets routed through a system/env HTTP proxy
# (http_proxy / HTTPS_PROXY / macOS system proxy) — that would break or hang it.

_TMUXCTLD_OPENER: urllib.request.OpenerDirector = urllib.request.build_opener(
    urllib.request.ProxyHandler({})
)

# The daemon is loopback-only and unauthenticated; the client refuses to speak to
# anything but a loopback host, so a stray/hostile TMUXCTLD_URL cannot turn this
# into an SSRF or exfiltration vector.
_TMUXCTLD_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _tmuxctld_url() -> str | None:
    """Return the configured tmuxctld base URL, or None.

    Trailing slash trimmed; only ``http`` loopback URLs are honoured — a
    non-loopback (or non-http) host is rejected (returns None) so the client
    never reaches off-box.
    """
    url = str(os.environ.get("TMUXCTLD_URL") or "").strip().rstrip("/")
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "http" or (parsed.hostname or "") not in _TMUXCTLD_LOOPBACK_HOSTS:
        return None
    return url


def _tmuxctld_get_json(path: str, params: dict[str, str], *, timeout: float = 0.5) -> dict | None:
    """GET ``path`` from the tmuxctld daemon and return the unwrapped ``result`` dict.

    Returns None when the daemon is not configured, is unreachable, replies
    non-200, or returns a non-``ok`` envelope — so every caller falls through to
    its subprocess fallback. Proxy bypass is enforced via the module opener.
    """
    base = _tmuxctld_url()
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
    # Fast path: prefer the tmuxctld loopback daemon when configured. Its
    # /tmux/resolve-instance result is canonical-only and fail-closed, so a
    # `found:false` is authoritative — no subprocess fallback in that case.
    result = await asyncio.to_thread(
        _tmuxctld_get_json, "/tmux/resolve-instance", {"instance_id": instance_id}, timeout=0.5
    )
    if result is not None:
        if not result.get("found"):
            return (None, None)
        pane_id = (result.get("pane_id") or "").strip() or None
        role = (result.get("pane_role") or "").strip() or None
        return (pane_id, role)
    cli_lib = Path(__file__).resolve().parents[1] / "cli-tools" / "lib"
    try:
        # sys.executable, never bare "python3": on the live host "python3" resolves
        # to the uv shim, which re-syncs token-api's venv on every spawn and fails
        # closed (no stdout) on a corrupt venv — null panes for every row. tmuxctl's
        # CLI is stdlib-only, so the running interpreter runs it directly via PYTHONPATH.
        proc = await _run_subprocess_offloop(
            (
                sys.executable,
                "-m",
                "tmuxctl.cli",
                "resolve-instance",
                instance_id,
                "--format",
                "json",
            ),
            env={
                **os.environ,
                "PYTHONPATH": f"{cli_lib}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            },
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=3,
        )
    except Exception:
        return (None, None)
    # Exit 1 is the not-found sentinel; --format json still prints the payload on
    # both exit codes, so parse stdout regardless and trust the `found` flag.
    try:
        payload = json.loads(proc.stdout.decode(errors="ignore").strip() or "{}")
    except (ValueError, json.JSONDecodeError):
        return (None, None)
    if not payload.get("found"):
        return (None, None)
    pane_id = (payload.get("pane_id") or "").strip() or None
    role = (payload.get("pane_role") or "").strip() or None
    return (pane_id, role)


async def instance_id_for_pane(pane: str | None) -> str | None:
    """Reverse of :func:`resolve_instance_pane`: read a pane's live ``@INSTANCE_ID``
    stamp (``pane -> instance_id``).

    tmuxctl and the agent wrapper own the stamp — set at register, cleared on agent
    death — so the pane itself is the authoritative reverse bridge. token-api keeps
    no tmux-pane perspective; this is the only reverse lookup, replacing every
    legacy stored-pane query. Fails closed: any
    miss, error, or unstamped/dead pane returns ``None`` so callers never act on a
    stale or reused pane.
    """
    if not (pane or "").strip():
        return None
    # Fast path: prefer the tmuxctld loopback daemon when configured. A reachable
    # daemon's /tmux/instance-id-for-pane result is authoritative (empty stamp ->
    # None); only fall through to the tmux subprocess when the daemon is absent.
    result = await asyncio.to_thread(
        _tmuxctld_get_json, "/tmux/instance-id-for-pane", {"pane": pane or ""}, timeout=0.5
    )
    if result is not None:
        return (result.get("instance_id") or "").strip() or None
    try:
        proc = await _run_subprocess_offloop(
            ("tmux", "show-options", "-pv", "-t", pane, "@INSTANCE_ID"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=3,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.decode(errors="ignore").strip() if proc.stdout else ""
    return value or None


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
    "`POST /api/morning/end` (or PATCH /api/instances/{{id}}/type to one_off as a "
    "rip cord) to exit the loop."
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


def _sync_write_timer_sample(source: str, work_state=None, timestamp: str | None = None) -> None:
    """Persist a point-in-time timer read-model sample."""
    conn = sqlite3.connect(DB_PATH)
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
        cursor = conn.execute(
            "SELECT COUNT(*) FROM instances WHERE status NOT IN ('stopped', 'archived') AND COALESCE(is_subagent, 0) = 0"
        )
        active_instances = int(cursor.fetchone()[0] or 0)

    processing_recent = _coerce_work_state_value(work_state, "processing_recent_count")
    if processing_recent is None:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM instances WHERE status = 'working' AND COALESCE(is_subagent, 0) = 0"
        )
        processing_recent = int(cursor.fetchone()[0] or 0)

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

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")

    cursor = conn.execute(
        "SELECT COUNT(*) FROM instances WHERE status NOT IN ('stopped', 'archived') AND COALESCE(is_subagent, 0) = 0"
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
