"""Unified day-start hook.

The endpoint is the single morning latch for quiet-hours release and future
morning fan-out work. Keep it side-effect-light until each consumer is wired.
"""

import asyncio
import logging
import os
import re
from datetime import time as datetime_time
from pathlib import Path
from typing import Any

import aiosqlite
import yaml
from fastapi import APIRouter
from pydantic import BaseModel, Field

from shared import (
    DB_PATH,
    get_day_state,
    get_quiet_hours_status,
    is_phone_reachable,
    log_event,
    quiet_hours_local_now,
    set_day_started_at,
)

logger = logging.getLogger("token_api")

router = APIRouter()

WAKE_ANCHOR_DEFAULT = "08:30"
WAKE_ANCHOR_TASK_ID = "day_start_schedule_fallback"
WAKE_ANCHOR_RE = re.compile(r"^(?P<hour>[0-2]?\d):(?P<minute>[0-5]\d)$")


def _imperium_env_root() -> Path:
    return Path(os.environ.get("IMPERIUM_ENV", "/Volumes/Imperium/Imperium-ENV"))


def _daily_note_dir() -> Path:
    return _imperium_env_root() / "Terra" / "Journal" / "Daily"


def _normalize_wake_anchor(value: Any) -> str | None:
    """Normalize daily-note wake_anchor values to HH:MM."""
    if value is None:
        return None
    if isinstance(value, datetime_time):
        return f"{value.hour:02d}:{value.minute:02d}"
    text = str(value).strip().strip("'\"")
    match = WAKE_ANCHOR_RE.match(text)
    if not match:
        return None
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23:
        return None
    return f"{hour:02d}:{minute:02d}"


def wake_anchor_to_cron(anchor: str) -> str:
    """Convert HH:MM wake anchor to a daily cron expression."""
    normalized = _normalize_wake_anchor(anchor)
    if normalized is None:
        raise ValueError(f"invalid wake_anchor: {anchor!r}")
    hour, minute = normalized.split(":", 1)
    return f"{int(minute)} {int(hour)} * * *"


def read_wake_anchor_from_daily_note(date_str: str | None = None) -> str:
    """Read wake_anchor from today's daily-note frontmatter, defaulting safely."""
    local_date = date_str or quiet_hours_local_now().date().isoformat()
    note_path = _daily_note_dir() / f"{local_date}.md"
    if not note_path.exists():
        return WAKE_ANCHOR_DEFAULT

    text = note_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return WAKE_ANCHOR_DEFAULT
    end = text.find("\n---", 4)
    if end == -1:
        return WAKE_ANCHOR_DEFAULT

    try:
        frontmatter = yaml.safe_load(text[4:end]) or {}
    except Exception as exc:
        logger.warning("day-start wake_anchor frontmatter parse failed: %s", exc)
        return WAKE_ANCHOR_DEFAULT

    return _normalize_wake_anchor(frontmatter.get("wake_anchor")) or WAKE_ANCHOR_DEFAULT


async def sync_day_start_schedule_from_daily_note(
    *, date_str: str | None = None, db_path: Path | None = None
) -> dict:
    """Update the schedule-fallback task to today's daily-note wake_anchor."""
    anchor = await asyncio.to_thread(read_wake_anchor_from_daily_note, date_str)
    cron = wake_anchor_to_cron(anchor)
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        await db.execute(
            """
            UPDATE scheduled_tasks
            SET schedule = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (cron, WAKE_ANCHOR_TASK_ID),
        )
        await db.commit()
    return {"wake_anchor": anchor, "cron": cron, "task_id": WAKE_ANCHOR_TASK_ID}


class DayStartFireRequest(BaseModel):
    source: str = Field(default="manual", description="alarm_silenced|schedule|manual|custodes")
    force: bool = Field(default=False, description="Re-run fan-out even if today already started")
    details: dict[str, Any] | None = None


async def _consumer_quiet_hours(day_state: dict) -> dict:
    return {
        "status": "ok",
        "effect": "quiet_hours_unlatched",
        "day_started_at": day_state.get("day_started_at"),
    }


async def _consumer_tts_suppression(day_state: dict) -> dict:
    return {
        "status": "ok",
        "effect": "tts_suppression_uses_day_state",
        "day_started_at": day_state.get("day_started_at"),
    }


async def _consumer_phone_reachability(_: dict) -> dict:
    reachable = await asyncio.to_thread(is_phone_reachable)
    return {"status": "ok", "reachable": reachable}


async def _consumer_stub(name: str, follow_up: str) -> dict:
    return {"status": "stubbed", "consumer": name, "follow_up": follow_up}


async def _run_consumer(name: str, coro) -> dict:
    try:
        result = await coro
        await log_event("day_start_consumer", details={"consumer": name, "result": result})
        return {"consumer": name, "success": True, "result": result}
    except Exception as exc:
        logger.warning("day-start consumer %s failed: %s", name, exc)
        result = {"consumer": name, "success": False, "error": str(exc)}
        await log_event("day_start_consumer_failed", details=result)
        return result


async def _day_start_fanout(day_state: dict) -> list[dict]:
    """Dispatch day-start consumers.

    Quiet-hours/TTS are wired now. The other named consumers are explicit
    skeleton slots so future patches can fill them without creating another
    morning hook.
    """
    consumers = [
        ("quiet_hours", _consumer_quiet_hours(day_state)),
        ("tts_suppression", _consumer_tts_suppression(day_state)),
        (
            "custodes_morning_session",
            _consumer_stub(
                "custodes_morning_session",
                "spawn/resume Custodes singleton with day-start continuity context",
            ),
        ),
        (
            "pavlok_daily_warmup",
            _consumer_stub(
                "pavlok_daily_warmup",
                "ping Pavlok, run ack-button readiness check, read battery",
            ),
        ),
        ("phone_reachability_check", _consumer_phone_reachability(day_state)),
        ("music_auto_start", _consumer_stub("music_auto_start", "start default morning audio")),
        (
            "daily_note_creation",
            _consumer_stub(
                "daily_note_creation",
                "verify/create Terra/Journal/Daily/YYYY-MM-DD.md via Obsidian CLI",
            ),
        ),
        (
            "morning_session_start",
            _consumer_stub(
                "morning_session_start",
                "migrate token-api/morning_session.py cron onto this hook",
            ),
        ),
    ]
    return await asyncio.gather(*[_run_consumer(name, coro) for name, coro in consumers])


@router.get("/api/day-start/status")
async def day_start_status():
    local_now = quiet_hours_local_now()
    state = await get_day_state(local_now.date().isoformat())
    return {
        "date": local_now.date().isoformat(),
        "day_state": state,
        "quiet_hours": get_quiet_hours_status(local_now),
    }


async def fire_day_start_internal(
    *,
    source: str = "manual",
    force: bool = False,
    details: dict[str, Any] | None = None,
) -> dict:
    state = await set_day_started_at(
        source=source,
        details=details,
        force=force,
    )
    await log_event(
        "day_start_fired",
        details={
            "source": source,
            "force": force,
            "date": state.get("date"),
            "day_started_at": state.get("day_started_at"),
            "already_started": state.get("already_started", False),
        },
    )

    if state.get("already_started") and not force:
        return {
            "success": True,
            "already_started": True,
            "day_state": state,
            "fanout": [],
            "quiet_hours": get_quiet_hours_status(),
        }

    fanout = await _day_start_fanout(state)
    return {
        "success": True,
        "already_started": False,
        "day_state": state,
        "fanout": fanout,
        "quiet_hours": get_quiet_hours_status(),
    }


@router.post("/api/day-start/fire")
async def fire_day_start(request: DayStartFireRequest):
    return await fire_day_start_internal(
        source=request.source,
        force=request.force,
        details=request.details,
    )
