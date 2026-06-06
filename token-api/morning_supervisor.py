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
from datetime import datetime, timedelta
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
    """Return the live morning-Custodes row, or None — same signal as in-pathway."""
    return await asyncio.to_thread(morning_session.find_live_custodes)


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
        cust = await custodes_running()
        if cust is not None:
            # Healthy: real ack + live Custodes. Self-disarm.
            await log_event(
                "morning_supervisor_ok",
                details={
                    "date": date_str,
                    "day_started_at": ack.get("day_started_at"),
                    "custodes_instance_id": cust.get("id"),
                },
            )
            logger.info("Morning supervisor: ack + live custodes confirmed — disarming")
            _disarm()
            return
        # Ack landed but no live Custodes — the reactive launch failed.
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
        # The wake itself never registered. Do NOT auto-launch a session — a
        # launch with no Hatch ack behind it is exactly the phantom we removed.
        # The alert IS the backup; surface it loudly to the Emperor.
        message = (
            f"Morning supervisor: it's {now_hm}, {mins_past} minutes past your expected "
            f"{day_type} wake of {expected_hm}, and no Hatch alarm-silence ack has landed. "
            f"The morning session never started — either you're still down, or the Hatch "
            f"ack pathway is broken."
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

    # 1) Alert the Emperor (TTS via the canonical comms router + Discord).
    await _notify(message)
    await _discord_alert(message)

    # 2) Backup recovery.
    if failure_type == "ack_no_custodes":
        # There WAS a real ack — relaunching the morning session is legitimate.
        retry = await _post_local("/api/morning/start")
        await log_event(
            "morning_supervisor_backup",
            details={"action": "remorning_start", "result": retry},
        )
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
    """Best-effort: resolve the live custodes pane from the DB and agent-cmd it.

    Never hardcode the pane — it drifts; resolve tmux_pane from the DB where
    pane_label='legion:custodes' (see the fg-custodes-comms-agent-cmd memory).
    A no-op when no live custodes pane exists (the common failure case).
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT tmux_pane FROM claude_instances
            WHERE pane_label = 'legion:custodes' AND stopped_at IS NULL
            ORDER BY last_activity DESC LIMIT 1
            """
        )
        row = await cursor.fetchone()
    pane = row["tmux_pane"] if row else None
    if not pane:
        return {"sent": False, "reason": "no_live_custodes_pane"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "agent-cmd",
            "--pane",
            pane,
            message,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            logger.warning("supervisor agent-cmd failed: %s", stderr.decode()[:200])
            return {"sent": False, "reason": "agent_cmd_failed", "pane": pane}
        return {"sent": True, "pane": pane}
    except Exception as exc:
        logger.warning("supervisor agent-cmd error: %s", exc)
        return {"sent": False, "reason": str(exc)}
