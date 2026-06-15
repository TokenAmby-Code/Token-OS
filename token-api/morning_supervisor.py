#!/usr/bin/env python3
"""Morning-session supervisor — the redundant "suspenders" observation layer.

This module owns the ONLY concept of an "expected wake time". The reactive
day-start path (POST /api/morning/alarm-silenced -> fan-out -> morning session)
is deliberately stateless and clock-free: it fires when the Hatch alarm is
silenced, whenever that happens. There is NO magic-number wake cron — the
08:30 ``day_start_schedule_fallback`` that fired a phantom morning every day
(weekends included, with no Hatch ack behind it) has been removed.

The supervisor adds a safety net WITHOUT reintroducing a fixed wake time:

  - One fixed 04:00 bookkeeping cron (``morning_supervisor_arm``) — the single
    allowed day-start clock, used only to set up the watch, never to launch a
    session. By 04:00 the Emperor is assuredly asleep.
  - At 04:00 it derives ``expected_wake`` EMPIRICALLY from the most recent real
    ack of the same day-type (weekday vs weekend). Hatch cannot expose the next
    alarm (BLE registration-only; the AWS IoT shadow carries only
    ``current.playing``, no discrete alarm time), so we learn the expectation
    from history and self-correct after one-off desyncs.
  - It then arms a relative 1-minute poller anchored at ``expected_wake`` that
    (a) watches for today's real ack and, once seen, (b) verifies a live
    Custodes is actually running — self-disarming on success. If no ack lands
    within ``SUPERVISOR_GRACE_MIN`` of ``expected_wake``, it emits a
    morning-session-failure alert + a backup message, then disarms.

No history yet -> no supervision (no false alarms). Self-correcting.
"""

import asyncio
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from apscheduler.triggers.interval import IntervalTrigger

import morning_session
import shared
from shared import (
    DB_PATH,
    OFFICIAL_MORNING_SOURCES,
    log_event,
    quiet_hours_local_now,
)

logger = logging.getLogger("token_api")

SUPERVISOR_ARM_TASK_ID = "morning_supervisor_arm"
SUPERVISOR_POLL_JOB_ID = "morning_supervisor_poll"
# Minutes after expected_wake with no ack before we declare a morning failure.
SUPERVISOR_GRACE_MIN = int(os.environ.get("MORNING_SUPERVISOR_GRACE_MIN", "15"))
SUPERVISOR_POLL_INTERVAL_MIN = 1
# How far back to scan day_start_fired events when deriving the expectation.
SUPERVISOR_HISTORY_SCAN = 90
_TZ = ZoneInfo(os.environ.get("TOKEN_API_QUIET_TIMEZONE", "America/Phoenix"))

BASE = "http://localhost:7777"
DISCORD_DAEMON = "http://localhost:7779"


def _is_weekend(d: datetime) -> bool:
    return d.weekday() >= 5


# ── History-derived expectation ───────────────────────────────


async def derive_expected_wake(
    *, now_local: datetime | None = None, db_path=None
) -> datetime | None:
    """Derive today's expected wake time-of-day from history.

    Returns a *naive local* ``datetime.time``-bearing datetime carrying only the
    hour/minute (date is irrelevant), or None when there is no usable history.

    Source of truth: ``day_start_fired`` events with ``source='alarm_silenced'``
    — the canonical Hatch alarm-silence ack. Only events that genuinely latched
    the day (``already_started=false``) are used, because for those the recorded
    ``day_started_at`` IS the real ack time. (Pre-removal, the schedule_fallback
    cron latched 08:30 first every morning, so real acks hit already_started and
    their day_started_at was polluted; those are skipped. After removal,
    alarm_silenced is the first/genuine latcher, so this reads clean.)

    Matches the day-type of ``now_local`` (weekday->last weekday,
    weekend->last weekend) and ignores today's own ack (we predict before it).
    """
    now_local = now_local or quiet_hours_local_now()
    want_weekend = _is_weekend(now_local)

    async with aiosqlite.connect(db_path or DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT details FROM events
            WHERE event_type = 'day_start_fired'
              AND json_extract(details, '$.source') = 'alarm_silenced'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (SUPERVISOR_HISTORY_SCAN,),
        )
        rows = await cursor.fetchall()

    for row in rows:
        try:
            details = json.loads(row["details"] or "{}")
        except (ValueError, TypeError):
            continue
        if details.get("already_started"):
            continue
        dsa = details.get("day_started_at")
        if not dsa:
            continue
        try:
            wake_dt = datetime.fromisoformat(dsa)
        except ValueError:
            continue
        wake_local = wake_dt.astimezone(_TZ) if wake_dt.tzinfo else wake_dt.replace(tzinfo=_TZ)
        if _is_weekend(wake_local) != want_weekend:
            continue
        if wake_local.date() == now_local.date():
            continue
        return wake_local
    return None


async def ack_seen_today(*, now_local: datetime | None = None, db_path=None) -> dict | None:
    """Return today's day_state if a real morning ack has latched it, else None.

    A real ack = day_started_at set with an official morning source
    (alarm_silenced/manual/custodes/morning), NOT a non-morning latch.
    """
    now_local = now_local or quiet_hours_local_now()
    state = await shared.get_day_state(now_local.date().isoformat(), db_path)
    if state and state.get("day_started_at") and state.get("source") in OFFICIAL_MORNING_SOURCES:
        return state
    return None


async def custodes_running() -> dict | None:
    """Return the live Custodes singleton row, or None (process-liveness only)."""
    return await asyncio.to_thread(morning_session.find_live_custodes)


async def morning_is_active(date_str: str | None = None) -> bool:
    """Whether today's morning session is confirmed active (state-file signal).

    This is the deterministic "the morning came up" signal that
    run_morning_session writes after it confirms + reconciles a live custodes.
    The supervisor checks THIS rather than mere custodes-process liveness:
    custodes is a singleton and is usually alive even when no morning launched,
    so "a custodes exists" would mask a real launch failure. "active" here means
    a session was actually confirmed for today.
    """
    active, _ = await asyncio.to_thread(morning_session.morning_session_active, date_str)
    return active


# ── Arming ─────────────────────────────────────────────────────


async def arm_morning_supervisor(
    *, recover: bool = False, now_local: datetime | None = None, db_path=None
) -> dict:
    """Derive expected wake and arm the relative morning watchdog poller.

    Called by the 04:00 bookkeeping cron and, with recover=True, once at server
    startup (the runtime poller lives in the in-memory jobstore and is lost on
    restart; re-arming on boot keeps the day supervised). Idempotent —
    replace_existing on the job id.
    """
    now_local = now_local or quiet_hours_local_now()
    expected = await derive_expected_wake(now_local=now_local, db_path=db_path)
    if expected is None:
        # No usable history -> no supervision. Safe by construction: never raise
        # a false alarm just because we have not learned the wake pattern yet.
        await log_event(
            "morning_supervisor_armed",
            details={"armed": False, "reason": "no_history", "recover": recover},
        )
        logger.info("Morning supervisor: no history for this day-type — no supervision")
        return {"armed": False, "reason": "no_history"}

    anchor = now_local.replace(hour=expected.hour, minute=expected.minute, second=0, microsecond=0)
    deadline = anchor + timedelta(minutes=SUPERVISOR_GRACE_MIN)

    if recover and now_local >= deadline:
        # Restart after the supervision window already closed for today — do not
        # arm (and do not retro-alert; a slept-in/quiet day is not a failure to
        # shout about hours later).
        await log_event(
            "morning_supervisor_armed",
            details={
                "armed": False,
                "reason": "window_passed",
                "anchor": anchor.isoformat(),
                "deadline": deadline.isoformat(),
            },
        )
        return {"armed": False, "reason": "window_passed"}

    start_date = max(now_local, anchor)
    shared.scheduler.add_job(
        _supervisor_poll_job,
        IntervalTrigger(minutes=SUPERVISOR_POLL_INTERVAL_MIN, start_date=start_date),
        kwargs={
            "date_str": now_local.date().isoformat(),
            "anchor_iso": anchor.isoformat(),
            "deadline_iso": deadline.isoformat(),
        },
        id=SUPERVISOR_POLL_JOB_ID,
        replace_existing=True,
        misfire_grace_time=120,
    )
    await log_event(
        "morning_supervisor_armed",
        details={
            "armed": True,
            "recover": recover,
            "expected_wake": anchor.strftime("%H:%M"),
            "day_type": "weekend" if _is_weekend(now_local) else "weekday",
            "anchor": anchor.isoformat(),
            "deadline": deadline.isoformat(),
            "poll_start": start_date.isoformat(),
        },
    )
    logger.info(
        "Morning supervisor armed: expected wake %s (%s), grace until %s",
        anchor.strftime("%H:%M"),
        "weekend" if _is_weekend(now_local) else "weekday",
        deadline.strftime("%H:%M"),
    )
    return {
        "armed": True,
        "expected_wake": anchor.strftime("%H:%M"),
        "anchor": anchor.isoformat(),
        "deadline": deadline.isoformat(),
    }


def _disarm() -> None:
    try:
        shared.scheduler.remove_job(SUPERVISOR_POLL_JOB_ID)
    except Exception:
        pass


# ── Polling ────────────────────────────────────────────────────


async def _supervisor_poll_job(date_str: str, anchor_iso: str, deadline_iso: str) -> None:
    """Relative poller: confirm the morning came up; else alert once and disarm."""
    now_local = quiet_hours_local_now()

    # Rolled past the day this poller was armed for (server clock crossed
    # midnight without disarming) — stand down; the 04:00 cron owns the new day.
    if now_local.date().isoformat() != date_str:
        _disarm()
        return

    ack = await ack_seen_today(now_local=now_local)
    if ack is not None:
        if await morning_is_active(date_str):
            # Healthy: real ack + confirmed-active morning session. Self-disarm.
            cust = await custodes_running()
            await log_event(
                "morning_supervisor_ok",
                details={
                    "date": date_str,
                    "day_started_at": ack.get("day_started_at"),
                    "custodes_instance_id": cust.get("id") if cust else None,
                },
            )
            logger.info("Morning supervisor: ack + active morning session confirmed — disarming")
            _disarm()
            return
        # Ack landed but the morning session is not active — reactive launch failed.
        await _handle_failure(
            failure_type="ack_no_custodes",
            now_local=now_local,
            anchor_iso=anchor_iso,
            ack=ack,
        )
        _disarm()
        return

    # No ack yet.
    deadline = datetime.fromisoformat(deadline_iso)
    if now_local >= deadline:
        await _handle_failure(
            failure_type="no_ack",
            now_local=now_local,
            anchor_iso=anchor_iso,
            ack=None,
        )
        _disarm()
        return
    # Within grace — keep watching.


# ── Failure handling: alert + backup ──────────────────────────


async def _handle_failure(
    *, failure_type: str, now_local: datetime, anchor_iso: str, ack: dict | None
) -> None:
    """Emit the morning-session-failure alert and the backup recovery action."""
    anchor = datetime.fromisoformat(anchor_iso)
    mins_past = int((now_local - anchor).total_seconds() // 60)
    day_type = "weekend" if _is_weekend(now_local) else "weekday"
    expected_hm = anchor.strftime("%H:%M")
    now_hm = now_local.strftime("%H:%M")

    if failure_type == "no_ack":
        # The Hatch ack itself never registered. That is no longer allowed to be
        # a terminal morning failure: the supervisor exists specifically because
        # the Hatch/MacroDroid ingress can silently decay. The durable invariant
        # is "expected wake + grace with no ack => Custodes latches day-start as
        # an explicit recovery source", not "alert the Emperor and leave him in
        # bed with the alarm silenced".
        message = (
            f"Morning supervisor: it's {now_hm}, {mins_past} minutes past your expected "
            f"{day_type} wake of {expected_hm}, and no Hatch alarm-silence ack has landed. "
            f"The Hatch ack pathway is broken; firing the Custodes day-start backstop now."
        )
    else:  # ack_no_custodes
        message = (
            f"Morning supervisor: your {day_type} wake registered (ack at "
            f"{ack.get('day_started_at') if ack else '?'}), but no live Custodes is "
            f"running at {now_hm}. The reactive morning launch failed — retrying."
        )

    await log_event(
        "morning_session_supervisor_alert",
        details={
            "failure_type": failure_type,
            "expected_wake": expected_hm,
            "day_type": day_type,
            "minutes_past": mins_past,
            "now": now_local.isoformat(),
            "ack": ack,
        },
    )
    logger.warning("Morning supervisor failure (%s): %s", failure_type, message)

    # 1) Backup recovery. This must not sit behind best-effort notification
    # timeouts; the durable day-start latch is the thing that prevents the
    # morning from continuing to drift.
    if failure_type == "ack_no_custodes":
        # There WAS a real ack — relaunching the morning session is legitimate.
        retry = await _post_local("/api/morning/start")
        await log_event(
            "morning_supervisor_backup",
            details={"action": "remorning_start", "result": retry},
        )
    elif failure_type == "no_ack":
        # There was no real Hatch ack, but the learned expected-wake window has
        # closed. Latch the official day-start hook through the same endpoint as
        # manual Custodes recovery so quiet-hours/break state and the full
        # day-start fan-out happen atomically before /api/morning/start runs.
        retry = await _post_local(
            "/api/day-start/fire",
            {
                "source": "custodes",
                "details": {
                    "reason": "morning_supervisor_no_ack_backstop",
                    "failure_type": failure_type,
                    "expected_wake": expected_hm,
                    "day_type": day_type,
                    "minutes_past": mins_past,
                    "hatch_ack": "missing",
                    "anchor": anchor.isoformat(),
                    "fired_at": now_local.isoformat(),
                },
            },
        )
        await log_event(
            "morning_supervisor_backup",
            details={"action": "day_start_fire", "source": "custodes", "result": retry},
        )

    # 2) Alert the Emperor (TTS via the canonical comms router + Discord).
    await _notify(message)
    await _discord_alert(message)

    # 3) If a live custodes pane happens to exist (e.g. wrong type), nudge it
    #    directly. For no_ack this is best-effort and usually a no-op.
    await _backup_message_to_custodes(message)


async def _notify(message: str) -> dict:
    return await _post_local("/api/notify", {"message": message, "tts": True})


async def _post_local(path: str, json_body: dict | None = None) -> dict:
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{BASE}{path}", json=json_body or {}, timeout=10)
            return resp.json()
    except Exception as exc:
        logger.warning("supervisor POST %s failed: %s", path, exc)
        return {"error": str(exc)}


async def _discord_alert(content: str) -> None:
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{DISCORD_DAEMON}/send",
                json={"channel": "briefing", "content": content, "bot": "custodes"},
                timeout=10,
            )
    except Exception as exc:
        logger.warning("supervisor discord alert failed: %s", exc)


async def _backup_message_to_custodes(message: str) -> dict:
    """Best-effort: resolve the live custodes pane from tmux and agent-cmd it.

    Never hardcode the pane and do not trust registry runtime columns. The live
    pane identity is the tmux @PANE_ID marker resolved by tmuxctl.
    """
    bin_dir = Path(__file__).resolve().parents[1] / "cli-tools" / "bin"
    tmuxctl = bin_dir / "tmuxctl"
    agent_cmd = bin_dir / "agent-cmd"
    try:
        resolved = await asyncio.to_thread(
            subprocess.run,
            [str(tmuxctl), "resolve-pane", "--format", "physical", "legion:custodes"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        logger.warning("supervisor custodes pane resolve failed: %s", exc)
        return {"sent": False, "reason": f"resolve_failed:{exc}"}
    if resolved.returncode != 0:
        return {
            "sent": False,
            "reason": "resolve_failed",
            "stderr": resolved.stderr.strip()[:200],
        }
    pane = resolved.stdout.strip()
    if not pane:
        return {"sent": False, "reason": "no_live_custodes_pane"}
    try:
        proc = await asyncio.create_subprocess_exec(
            str(agent_cmd),
            "--pane",
            pane,
            message,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except TimeoutError:
            if proc.returncode is None:
                proc.kill()
            _, stderr = await proc.communicate()
            logger.warning("supervisor agent-cmd timed out: %s", stderr.decode()[:200])
            return {"sent": False, "reason": "agent_cmd_timeout", "pane": pane}
        if proc.returncode != 0:
            logger.warning("supervisor agent-cmd failed: %s", stderr.decode()[:200])
            return {"sent": False, "reason": "agent_cmd_failed", "pane": pane}
        return {"sent": True, "pane": pane}
    except Exception as exc:
        logger.warning("supervisor agent-cmd error: %s", exc)
        return {"sent": False, "reason": str(exc)}
