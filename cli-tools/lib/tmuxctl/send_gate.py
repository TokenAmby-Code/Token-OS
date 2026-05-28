"""Universal tmux send gate — the single, inescapable pane-write sentinel.

Every byte written to a tmux pane goes out through one of two language
entrypoints:

  * Python: ``TmuxAdapter.run()`` (token-api interventions, tmuxctl CLI,
    enforcement, pane recovery) — it resolves the *real* tmux binary and
    execs ``send-keys`` / ``paste-buffer`` directly.
  * Shell: bare ``tmux send-keys`` in ~15 scripts, which resolves through the
    ``cli-tools/bin/tmux`` shim before reaching real tmux.

This module is the ONE predicate both entrypoints consult. The invariant it
enforces: **nothing reaches any pane while quiet hours OR the typing guard is
active.** It is filtered to the mutating send verbs only (reads such as
``capture-pane`` / ``display-message`` are never gated).

Design properties:

  * **One source of truth.** Quiet-hours and typing-guard predicates live here
    and nowhere else; ``tmux-guard.sh`` and the ``bin/tmux`` shim are thin
    readers that call ``python -m tmuxctl.send_gate check``.
  * **Fail-open on infrastructure error, fail-closed on a positive signal.**
    The gate refuses only when it can positively determine quiet-hours or
    typing-guard is active. If it cannot read the DB or tmux, it allows the
    send (so a transient fault never bricks all pane writes) and logs.
  * **Sanctioned, audited override.** Human-initiated sends (dictation, the
    pedal-enter, an operator-driven transplant) set ``TMUX_SEND_GATE_ALLOW`` to
    a reason string; the gate then allows but logs ``send_gate_override``.
    Automated senders never set it, so they are always gated. The gate is never
    silently escapable.
  * **Never raises into callers.** Suppression returns a structured result; the
    caller treats a suppressed send as a silent no-op.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("tmuxctl.send_gate")

# The only tmux verbs that write bytes into a pane's PTY. Everything else
# (capture-pane, display-message, list-*, set-option, select-*, …) is a read or
# a non-PTY mutation and is never gated.
MUTATING_SEND_VERBS = frozenset({"send-keys", "send-key", "send", "paste-buffer"})

# Environment override: a non-empty value is a sanctioned reason for a
# human-initiated send. Allowed, but always logged.
_SEND_GATE_ALLOW_ENV = "TMUX_SEND_GATE_ALLOW"

# Recent-keystroke window (seconds) for the typing guard. Mirrors
# bin/tmux-typing-guard-status' TMUX_TYPING_GUARD_WINDOW.
_DEFAULT_TYPING_GUARD_WINDOW = 10

# Quiet-hours configuration. Same env contract as token-api/shared.py so there
# is a single configuration source (the environment), not a duplicated literal.
_QUIET_START_ENV = "TOKEN_API_QUIET_START_HOUR"
_QUIET_END_ENV = "TOKEN_API_QUIET_END_HOUR"
_QUIET_TZ_ENV = "TOKEN_API_QUIET_TIMEZONE"
_DEFAULT_QUIET_START = 23
_DEFAULT_QUIET_END = 9
_DEFAULT_QUIET_TZ = "America/Phoenix"

# Only an explicit/official morning action may release the morning quiet latch
# early. day_state is written by exactly two paths in this codebase: the
# automated schedule_fallback wake-anchor (source="schedule_fallback") — which
# fired while the Emperor slept and must NOT release quiet — and the
# /api/day-start/fire endpoint (the documented "single morning latch") whose
# human/official sources are alarm_silenced|manual|custodes. The automated
# "schedule"/"schedule_fallback" sources are deliberately excluded. If early
# release never fires, the 09:00 clock boundary still ends quiet hours.
# Overridable via env (TOKEN_API_MORNING_SOURCES) and kept in sync with shared.py.
_OFFICIAL_MORNING_SOURCES = frozenset(
    s.strip()
    for s in os.environ.get(
        "TOKEN_API_MORNING_SOURCES", "alarm_silenced,manual,custodes,morning"
    ).split(",")
    if s.strip()
)


def _db_path() -> Path:
    return Path(os.environ.get("TOKEN_API_DB", Path.home() / ".claude" / "agents.db"))


def _quiet_config() -> tuple[int, int, str]:
    def _int(env: str, default: int) -> int:
        try:
            return int(os.environ.get(env, str(default)))
        except (TypeError, ValueError):
            return default

    return (
        _int(_QUIET_START_ENV, _DEFAULT_QUIET_START),
        _int(_QUIET_END_ENV, _DEFAULT_QUIET_END),
        os.environ.get(_QUIET_TZ_ENV, _DEFAULT_QUIET_TZ),
    )


def _local_now(now: datetime | None = None) -> datetime:
    _, _, tz_name = _quiet_config()
    # Fail-open: a misconfigured timezone must never raise out of the gate.
    tz: tzinfo
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        logger.debug(
            "send_gate invalid timezone %r; falling back to %s", tz_name, _DEFAULT_QUIET_TZ
        )
        try:
            tz = ZoneInfo(_DEFAULT_QUIET_TZ)
        except Exception:
            tz = UTC
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _clock_window(local_now: datetime) -> tuple[bool, str]:
    """Whether the configured clock window is active, and which segment.

    Mirrors ``shared._quiet_hour_window_active`` exactly; kept in sync via the
    shared env config above.
    """
    start, end, _ = _quiet_config()
    hour = local_now.hour + local_now.minute / 60 + local_now.second / 3600
    if start == end:
        return True, "all_day"
    if start < end:
        active = start <= hour < end
        return active, ("same_day" if active else "outside")
    if hour >= start:
        return True, "night_start"
    if hour < end:
        return True, "morning_latch"
    return False, "outside"


def _read_day_state(db_path: Path, local_date: str) -> tuple[str | None, str | None]:
    """Return (day_started_at, source) for ``local_date`` from a cold DB read.

    Cold by design: the overnight bypass was a poisoned in-process cache, so the
    gate never trusts an in-memory value — it reads the row fresh every time.
    """
    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT day_started_at, source FROM day_state WHERE date = ?",
                (local_date,),
            ).fetchone()
    except Exception as exc:  # missing table, locked DB, etc. — fail open.
        logger.debug("send_gate day_state read failed: %s", exc)
        return None, None
    if not row:
        return None, None
    return row["day_started_at"], row["source"]


def _session_quiet_latch(db_path: Path) -> bool:
    """True if the persisted timer state is in QUIET mode (session latch).

    The nightly debrief latches the timer into QUIET (``manual_mode == 'quiet'``
    in the serialized state); the morning system releases it. This extends quiet
    suppression beyond the clock window (e.g. an early debrief or a daytime nap)
    and is the session-driven half of the predicate.
    """
    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            row = conn.execute("SELECT state_json FROM timer_state WHERE id = 1").fetchone()
    except Exception as exc:
        logger.debug("send_gate timer_state read failed: %s", exc)
        return False
    if not row or not row[0]:
        return False
    try:
        state = json.loads(row[0])
    except (ValueError, TypeError):
        return False
    return state.get("manual_mode") == "quiet"


def quiet_hours_active(
    *, db_path: Path | None = None, now: datetime | None = None
) -> tuple[bool, dict]:
    """Canonical standalone quiet-hours decision for the send gate.

    Active when EITHER:
      * the clock window is active (with the morning latch released only by an
        official morning source — never by schedule_fallback/wake_anchor/manual
        or a stale cache), OR
      * the persisted timer session latch is QUIET.

    Returns ``(active, context)``. Fail-open: any read error yields whatever the
    clock window says (the clock window needs no DB), so an unreadable DB still
    enforces the overnight window.
    """
    path = db_path or _db_path()
    local_now = _local_now(now)
    local_date = local_now.date().isoformat()
    window_active, segment = _clock_window(local_now)

    day_started_at, day_source = _read_day_state(path, local_date)
    clock_active = window_active
    morning_released = False
    if window_active and segment == "morning_latch" and day_started_at:
        if day_source in _OFFICIAL_MORNING_SOURCES:
            clock_active = False
            morning_released = True
        # else: schedule_fallback / wake_anchor / manual — latch HOLDS.

    session_latch = _session_quiet_latch(path)
    active = clock_active or session_latch

    context = {
        "clock_window_active": window_active,
        "clock_segment": segment,
        "clock_active": clock_active,
        "session_quiet_latch": session_latch,
        "day_started_at": day_started_at,
        "day_source": day_source,
        "morning_latch_released": morning_released,
        "local_time": local_now.isoformat(),
    }
    return active, context


def _client_activity_epoch() -> int | None:
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "#{client_activity}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.3,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def typing_guard_active(*, window_seconds: int | None = None) -> bool:
    """Canonical typing-guard predicate: recent human keystroke on a client.

    Reads tmux ``#{client_activity}`` (updated on client input) and reports
    active for ``window_seconds`` after the last keystroke. This is the single
    implementation; ``tmux-guard.sh`` and the shim are thin readers of it.

    Fail-open: if tmux is unreachable or reports nothing, returns False.
    """
    if window_seconds is None:
        try:
            window_seconds = int(
                os.environ.get("TMUX_TYPING_GUARD_WINDOW", str(_DEFAULT_TYPING_GUARD_WINDOW))
            )
        except (TypeError, ValueError):
            window_seconds = _DEFAULT_TYPING_GUARD_WINDOW
    window_seconds = max(1, window_seconds)

    activity = _client_activity_epoch()
    if activity is None:
        return False
    age = int(time.time()) - activity
    return 0 <= age <= window_seconds


def is_send_verb(args: tuple[str, ...] | list[str]) -> bool:
    return bool(args) and args[0] in MUTATING_SEND_VERBS


def _extract_target(args: tuple[str, ...] | list[str]) -> str | None:
    for idx, arg in enumerate(args):
        if arg in ("-t", "-s") and idx + 1 < len(args):
            return args[idx + 1]
        if arg.startswith("-t") and arg != "-t":
            return arg[2:]
    return None


def sanctioned_override() -> str | None:
    """Return the sanctioned-send reason if one is set, else None."""
    reason = os.environ.get(_SEND_GATE_ALLOW_ENV, "").strip()
    return reason or None


def evaluate(
    args: tuple[str, ...] | list[str],
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Evaluate the gate for a tmux command.

    Returns ``None`` to allow the send. Returns a structured suppression result
    (``{"suppressed": True, "reason": ..., ...}``) when the verb is a mutating
    send and (quiet hours OR typing guard) is active and no sanctioned override
    is present. Never raises.
    """
    args = tuple(args)
    if not is_send_verb(args):
        return None

    override = sanctioned_override()

    quiet, quiet_ctx = quiet_hours_active(db_path=db_path, now=now)
    typing = typing_guard_active()
    if not (quiet or typing):
        return None

    reason = "quiet_hours" if quiet else "typing_guard"
    result = {
        "suppressed": override is None,
        "reason": reason,
        "verb": args[0],
        "target": _extract_target(args),
        "quiet_hours": quiet,
        "typing_guard": typing,
        "quiet_context": quiet_ctx,
        "override": override,
    }
    return result


def record_suppression(result: dict, *, db_path: Path | None = None) -> None:
    """Log a gate decision: a suppression, or a sanctioned override.

    Best-effort write to the events table plus a logger line. Never raises.
    """
    override = result.get("override")
    reason = result.get("reason")
    target = result.get("target")
    if override is not None:
        event_type = "send_gate_override"
        logger.warning("send_gate OVERRIDE reason=%s gate=%s target=%s", override, reason, target)
    else:
        event_type = (
            "quiet_hours_suppressed" if reason == "quiet_hours" else "typing_guard_suppressed"
        )
        logger.warning("send_gate SUPPRESSED reason=%s target=%s", reason, target)

    path = db_path or _db_path()
    try:
        with sqlite3.connect(path, timeout=2.0) as conn:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute(
                "INSERT INTO events (event_type, device_id, details) VALUES (?, ?, ?)",
                (event_type, "tmuxctl_send_gate", json.dumps(result, default=str)),
            )
            conn.commit()
    except Exception as exc:
        logger.debug("send_gate event log dropped (%s): %s", event_type, exc)


def _cli(argv: list[str]) -> int:
    """CLI for the shell readers (bin/tmux shim, tmux-guard.sh, status segment).

    Subcommands:
      * ``check <verb> [args...]`` — gate a tmux command. Exit 0 allow / 100 suppress.
      * ``typing`` — typing-guard predicate. Exit 0 active / 1 inactive.
      * ``quiet``  — quiet-hours predicate.  Exit 0 active / 1 inactive.
    """
    if not argv:
        sys.stderr.write("usage: send_gate check|typing|quiet [args...]\n")
        return 2
    cmd = argv[0]
    if cmd == "check":
        verb_args = argv[1:]
        result = evaluate(tuple(verb_args))
        if result is None or not result.get("suppressed"):
            if result is not None and result.get("override") is not None:
                record_suppression(result)
            return 0
        record_suppression(result)
        return 100
    if cmd == "typing":
        return 0 if typing_guard_active() else 1
    if cmd == "quiet":
        active, _ = quiet_hours_active()
        return 0 if active else 1
    sys.stderr.write("usage: send_gate check|typing|quiet [args...]\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
