"""Universal tmux send gate — the single, inescapable pane-write sentinel.

Every byte written to a tmux pane goes out through one of two language
entrypoints:

  * Python: ``TmuxAdapter.run()`` (token-api interventions, tmuxctl CLI,
    enforcement, pane recovery) — it resolves the *real* tmux binary and
    execs ``send-keys`` / ``paste-buffer`` directly.
  * Shell: bare ``tmux send-keys`` in ~15 scripts, which resolves through the
    ``cli-tools/bin/tmux`` shim before reaching real tmux.

This module is the ONE predicate both entrypoints consult. The invariant it
enforces: **automated pane writes do not race the Emperor's direct input in
the same pane**. Quiet hours still cancel automated writes by default; the
typing guard delays automation by default for the guarded target pane only;
sanctioned direct-input sends may pierce. It is filtered
to the mutating send verbs only (reads such as ``capture-pane`` /
``display-message`` are never gated).

Design properties:

  * **One source of truth.** Quiet-hours and typing-guard predicates live here
    and nowhere else; ``tmux-guard.sh`` and the ``bin/tmux`` shim are thin
    readers that call ``python -m tmuxctl.send_gate check``. The typing guard is
    pane-local for pane writes; aggregate/global checks are derived queries via
    ``typing_guard_active()`` without a target.
  * **Fail-open on infrastructure error, fail-closed on a positive signal.**
    The gate refuses only when it can positively determine quiet-hours or
    typing-guard is active. If it cannot read the DB or tmux, it allows the
    send (so a transient fault never bricks all pane writes) and logs.
  * **Explicit disposition.** ``TMUX_SEND_GATE_POLICY`` is a delay/cancel/pierce
    enum. If unset, sanctioned human/direct-input sends pierce, typing-guarded
    automation delays, and quiet-hours automation cancels.
  * **Sanctioned, audited override.** Human-initiated sends (dictation, the
    pedal-enter, an operator-driven transplant) set ``TMUX_SEND_GATE_ALLOW`` to
    a reason string; the gate then allows but logs ``send_gate_override``.
    Automated senders never set it, so they are delayed/cancelled by policy.
    The gate is never silently escapable.
  * **Never raises into callers.** Cancellation returns a structured result;
    delay waits until the gate clears; pierce writes and audits.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("tmuxctl.send_gate")

# The only tmux verbs that write bytes into a pane's PTY. Everything else
# (capture-pane, display-message, list-*, set-option, select-*, …) is a read or
# a non-PTY mutation and is never gated.
MUTATING_SEND_VERBS = frozenset({"send-keys", "send-key", "send", "paste-buffer"})

# Environment override: a non-empty value is a sanctioned reason for a
# human/direct-input send. Allowed, but always logged.
_SEND_GATE_ALLOW_ENV = "TMUX_SEND_GATE_ALLOW"

# Explicit disposition enum for a positive gate signal. Valid values:
#   delay  — wait until the gate clears, then send (default for typing guard)
#   cancel — suppress/no-op (default for quiet hours)
#   pierce — send now and audit (default when TMUX_SEND_GATE_ALLOW is set)
_SEND_GATE_POLICY_ENV = "TMUX_SEND_GATE_POLICY"
_SEND_GATE_POLICIES = frozenset({"delay", "cancel", "pierce"})
_SEND_GATE_DELAY_TIMEOUT_ENV = "TMUX_SEND_GATE_DELAY_TIMEOUT"  # unset/0 = no timeout

# Per-pane keystroke-anchored typing lock. The tmux root-table any-key binding
# (cli-tools/tmux/tmux-base.conf) stamps this pane option with an ABSOLUTE expiry
# epoch the moment the Emperor first types into a pane (first keystroke + the
# 5-min window, in the same unix-epoch timebase as ``time.time()``), and an Enter
# keystroke into that pane clears it. The gate reads this option as the SOLE
# typing signal: a pane the Emperor typed into is locked until the timer expires
# or an Enter clears it — held even after focus leaves the pane, and never armed
# by focus/click or by the fleet's own ``send-keys`` (those bypass the key
# table). This replaces the old focus-coupled ``#{client_activity}`` shadow.
_TYPING_LOCK_OPTION = "@TYPING_LOCK_UNTIL"
_TYPING_PENDING_OPTION = "@TYPING_PENDING_UNTIL"

# Poll cap (seconds) while a send is delayed behind a typing lock. The lock has a
# concrete absolute expiry, so the delay sleeps toward it; the cap bounds the
# interval so an Enter-clear (or a vanished draft) releases a held send within
# ~1s instead of after the whole 5-min window.
_TYPING_LOCK_RECHECK_SECONDS = 1.0

# Automated-activation marker TTL (seconds). Every send through
# TmuxAdapter.run() is automated by construction (see module docstring), so the
# gate stamps the target pane with a marker that compute_work_state uses to
# discount the woken agent's reflex activity from productivity. 90s covers the
# PromptSubmit + PostToolUse reflex burst (debounced ~2s) yet stays under the
# 3-min work_activity_cutoff, so a pane still producing activity past the window
# re-anchors WORKING — the marker never *permanently* suppresses. The
# permanent-vs-reflex distinction (carving legitimate automated work back in) is
# deliberately deferred to a later pass.
_DEFAULT_AUTOMATED_ACTIVITY_TTL = 90
_AUTOMATED_ACTIVITY_TTL_ENV = "TMUXCTL_AUTOMATED_ACTIVITY_TTL"

# Quiet-hours configuration. Same env contract as token-api/shared.py so there
# is a single configuration source (the environment), not a duplicated literal.
_QUIET_START_ENV = "TOKEN_API_QUIET_START_HOUR"
_QUIET_END_ENV = "TOKEN_API_QUIET_END_HOUR"
_QUIET_TZ_ENV = "TOKEN_API_QUIET_TIMEZONE"
_DEFAULT_QUIET_START = 23
_DEFAULT_QUIET_END = 7
_DEFAULT_QUIET_TZ = "America/Phoenix"

# Only an explicit/official morning action may release the morning quiet latch
# early. day_state is written by exactly two paths in this codebase: the
# automated schedule_fallback wake-anchor (source="schedule_fallback") — which
# fired while the Emperor slept and must NOT release quiet — and the
# /api/day-start/fire endpoint (the documented "single morning latch") whose
# human/official sources are alarm_silenced|manual|custodes. The automated
# "schedule"/"schedule_fallback" sources are deliberately excluded. If early
# release never fires, the 07:00 clock boundary (the _DEFAULT_QUIET_END default
# below) still ends quiet hours.
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


def _real_tmux_binary() -> str:
    """Resolve the real tmux binary, never the bin/tmux shim.

    Lazy import to avoid a module cycle (tmux_adapter imports send_gate). The
    shim consults this module via ``python -m tmuxctl.send_gate``; shelling
    back into the shim from here would recurse through the gate.
    """
    try:
        from .tmux_adapter import _tmux_binary

        return _tmux_binary()
    except Exception:
        return "tmux"


def _pane_lock_until(target: str) -> int | None:
    """Absolute expiry epoch of ``target``'s keystroke lock, or None.

    Reads the per-pane ``@TYPING_LOCK_UNTIL`` option the tmux any-key binding
    stamps. ``show-options -pqv`` prints the value for a set option and an empty
    line (exit 0) for an unset one. Fail-open: any tmux error / unset / unparsable
    value yields None (no lock), so a transient fault never wedges pane writes.
    """
    if not target:
        return None
    try:
        proc = subprocess.run(
            [_real_tmux_binary(), "show-options", "-pqv", "-t", target, _TYPING_LOCK_OPTION],
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
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pane_pending_until(target: str) -> int | None:
    """Return the pane's post-submit pending hold epoch, if any."""
    if not target:
        return None
    try:
        proc = subprocess.run(
            [_real_tmux_binary(), "show-options", "-pqv", "-t", target, _TYPING_PENDING_OPTION],
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
    raw = proc.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _pane_hold_until(target: str) -> int | None:
    """Return the latest active typing/pending hold epoch for ``target``."""
    deadlines = [
        v for v in (_pane_lock_until(target), _pane_pending_until(target)) if v is not None
    ]
    if not deadlines:
        return None
    return max(deadlines)


def _pane_keystroke_locked(target: str) -> bool:
    """True iff ``target`` carries a live typing or pending hold.

    The ON lock is keystroke-anchored and focus-decoupled. Enter moves the pane
    into a short PENDING hold (``@TYPING_PENDING_UNTIL``), which remains
    send-blocking so automation cannot race the human's submitted prompt.
    """
    now = time.time()
    lock_until = _pane_lock_until(target)
    if lock_until is not None and now < lock_until:
        return True
    pending_until = _pane_pending_until(target)
    return pending_until is not None and now < pending_until


def _live_pane_ids() -> list[str] | None:
    try:
        proc = subprocess.run(
            [_real_tmux_binary(), "list-panes", "-a", "-F", "#{pane_id}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def any_typing_guard_active() -> bool:
    """Aggregate query: true if any pane is under a typing guard.

    Aggregate behavior for global policies that intentionally hang on ANY typing
    guard: true iff any live pane carries a keystroke lock.
    """
    panes = _live_pane_ids()
    if panes is None:
        return False
    return any(_pane_keystroke_locked(pane) for pane in panes)


def typing_guard_active(*, target: str | None = None) -> bool:
    """Canonical typing-guard predicate — the keystroke-anchored per-pane lock.

    With ``target`` set, the guard holds iff that pane carries a live keystroke
    lock (``@TYPING_LOCK_UNTIL``) or a post-submit pending hold
    (``@TYPING_PENDING_UNTIL``). This is the single, honest, focus-DECOUPLED
    signal both surfaces consume — the event-updated ⌨ pane-border diagnostic and
    the universal send-hold.
    Focus/click never set, move, or clear it; the fleet's own ``send-keys`` never
    arms it (those bypass the key table). A genuine unsent draft is covered by
    the lock the keystroke that typed it armed — no prompt-line screen-scraping,
    so an idle worker pane with leftover prompt text is never falsely held.

    With no target, keep aggregate behavior for policies that hang on ANY guard.

    Fail-open: if tmux is unreachable or reports nothing, returns False.
    """
    if target:
        return _pane_keystroke_locked(target)
    return any_typing_guard_active()


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


def send_gate_policy(*, override: str | None = None, reason: str | None = None) -> str:
    """Return the delay/cancel/pierce disposition for a positive gate signal.

    Default policy is intentionally asymmetric: quiet-hours automation cancels
    (waiting overnight would wedge callers), typing-guard automation delays
    (never drop a prompt merely because the Emperor was typing), and sanctioned
    direct-input sends pierce.
    """
    explicit = os.environ.get(_SEND_GATE_POLICY_ENV, "").strip().lower()
    if explicit in _SEND_GATE_POLICIES:
        return explicit
    if override:
        return "pierce"
    if reason == "typing_guard":
        return "delay"
    return "cancel"


def _delay_timeout_seconds() -> float | None:
    raw = os.environ.get(_SEND_GATE_DELAY_TIMEOUT_ENV, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# Margin added past the computed lock expiry so the post-sleep re-check lands
# strictly outside the lock window (epoch math is whole-second).
_DELAY_WAKE_MARGIN_SECONDS = 0.1
# An explicitly configured quiet-hours delay (TMUX_SEND_GATE_POLICY=delay) has
# no keystroke-derived deadline; re-evaluate at a coarse cadence instead.
_QUIET_DELAY_RECHECK_SECONDS = 60.0


def _typing_delay_sleep(target: str | None) -> float:
    """Seconds to sleep before re-checking a typing-guard delay.

    The keystroke/pending hold carries an absolute expiry, so sleep toward it —
    a quiet 5-min lock costs ~one wake per second rather than a busy-spin — but
    cap the interval at ``_TYPING_LOCK_RECHECK_SECONDS`` so a clear releases the
    held send within ~1s. With no live hold (or tmux unreadable) fall back to the
    cap.
    """
    until = _pane_hold_until(target) if target else None
    if until is None:
        return _TYPING_LOCK_RECHECK_SECONDS
    remaining = (until + _DELAY_WAKE_MARGIN_SECONDS) - time.time()
    if remaining <= 0:
        return 0.05
    return min(_TYPING_LOCK_RECHECK_SECONDS, remaining)


def wait_for_gate_clear(
    args: tuple[str, ...] | list[str],
    *,
    db_path: Path | None = None,
    now: datetime | None = None,
    timeout_seconds: float | None = None,
) -> bool:
    """Wait while policy remains ``delay``; return True once sending is allowed.

    Returns False if the policy changes to cancel or an explicit timeout
    expires. A typing-guard wait sleeps toward the target pane's keystroke-lock
    expiry (capped so an Enter-clear or vanished draft releases promptly); a
    quiet-hours delay re-checks coarsely.
    """
    if timeout_seconds is None:
        timeout_seconds = _delay_timeout_seconds()
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    args_tuple = tuple(args)
    target = _extract_target(args_tuple)
    result = evaluate(args_tuple, db_path=db_path, now=now)
    while True:
        if result is None or not result.get("suppressed"):
            return True
        if result.get("policy") != "delay":
            return False
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if result.get("reason") == "typing_guard":
            sleep_for = _typing_delay_sleep(target)
        else:
            # Quiet-hours only delays under an explicit TMUX_SEND_GATE_POLICY=delay;
            # there is no keystroke deadline to sleep to, so re-check coarsely.
            sleep_for = _QUIET_DELAY_RECHECK_SECONDS
        if deadline is not None:
            sleep_for = min(sleep_for, max(0.05, deadline - time.monotonic()))
        time.sleep(sleep_for)
        result = evaluate(args_tuple, db_path=db_path, now=now)


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

    target = _extract_target(args)
    quiet, quiet_ctx = quiet_hours_active(db_path=db_path, now=now)
    typing = typing_guard_active(target=target)
    if not (quiet or typing):
        return None

    reason = "quiet_hours" if quiet else "typing_guard"
    policy = send_gate_policy(override=override, reason=reason)
    result = {
        "suppressed": policy != "pierce",
        "policy": policy,
        "reason": reason,
        "verb": args[0],
        "target": target,
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
    policy = result.get("policy") or ("pierce" if override else "cancel")
    if override is not None or policy == "pierce":
        event_type = "send_gate_override"
        logger.warning(
            "send_gate OVERRIDE reason=%s policy=%s gate=%s target=%s",
            override,
            policy,
            reason,
            target,
        )
    elif policy == "delay":
        event_type = "quiet_hours_delayed" if reason == "quiet_hours" else "typing_guard_delayed"
        logger.warning("send_gate DELAY reason=%s target=%s", reason, target)
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


def automated_activity_ttl() -> int:
    """TTL (seconds) for an automated-activation marker. Env-overridable; floored at 1."""
    try:
        ttl = int(os.environ.get(_AUTOMATED_ACTIVITY_TTL_ENV, str(_DEFAULT_AUTOMATED_ACTIVITY_TTL)))
    except (TypeError, ValueError):
        ttl = _DEFAULT_AUTOMATED_ACTIVITY_TTL
    return max(1, ttl)


def register_automated_send(
    args: tuple[str, ...] | list[str],
    *,
    db_path: Path | None = None,
    source: str | None = None,
) -> None:
    """Stamp the send's target pane with an automated-activation marker.

    Every send through ``TmuxAdapter.run()`` is automated by construction (see the
    module docstring): humans type directly into tmux, never through ``run()``. The
    marker lets ``compute_work_state`` discount the woken agent's reflex activity
    (instance ``last_activity`` bump + ``work_action``) from productivity accounting,
    so an automated state-hook / dispatch / enforcement wake does not anchor WORKING
    and the idle clock can mature.

    Fires only for mutating send verbs with a resolved ``-t`` target (by the time
    ``run()`` calls this the target is the canonical ``%pane_id``). Best-effort and
    fail-open — never raises into the send path. Timestamps are naive-local to match
    ``instances.last_activity`` (the convention compute_work_state compares
    against); the upsert slides the window forward across a multi-send reflex burst.
    """
    args = tuple(args)
    if not is_send_verb(args):
        return
    target = _extract_target(args)
    if not target:
        return
    now = datetime.now()
    injected_at = now.isoformat()
    expires_at = (now + timedelta(seconds=automated_activity_ttl())).isoformat()
    path = db_path or _db_path()
    try:
        with sqlite3.connect(path, timeout=2.0) as conn:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute(
                """
                INSERT INTO automated_pane_activity
                    (tmux_pane, injected_at, expires_at, source, verb)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tmux_pane) DO UPDATE SET
                    injected_at = excluded.injected_at,
                    expires_at  = excluded.expires_at,
                    source      = excluded.source,
                    verb        = excluded.verb
                """,
                (target, injected_at, expires_at, source or "tmuxctl", args[0]),
            )
            conn.commit()
    except Exception as exc:  # fail-open: a marker write must never break the send.
        logger.debug("send_gate automated-activity marker dropped for %s: %s", target, exc)


def _cli(argv: list[str]) -> int:
    """CLI for the shell readers (bin/tmux shim, tmux-guard.sh, status segment).

    Subcommands:
      * ``check <verb> [args...]`` — gate a tmux command. Exit 0 allow after any delay / 100 cancel.
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
        if result is not None and result.get("policy") == "delay":
            record_suppression(result)
            if wait_for_gate_clear(tuple(verb_args)):
                return 0
            result = {**result, "policy": "cancel", "suppressed": True, "delay_failed": True}
        if result is None or not result.get("suppressed"):
            if result is not None and result.get("override") is not None:
                record_suppression(result)
            return 0
        record_suppression(result)
        return 100
    if cmd == "typing":
        target = argv[1] if len(argv) > 1 else None
        return 0 if typing_guard_active(target=target) else 1
    if cmd == "quiet":
        active, _ = quiet_hours_active()
        return 0 if active else 1
    sys.stderr.write("usage: send_gate check|typing|quiet [args...]\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
