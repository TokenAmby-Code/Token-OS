"""Unified day-start hook.

The endpoint is the single morning latch for quiet-hours release and future
morning fan-out work. Keep it side-effect-light until each consumer is wired.
"""

import asyncio
import logging
import re
from typing import Any

import aiosqlite
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


async def _consumer_custodes_morning_session() -> dict:
    import httpx

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post("http://localhost:7777/api/morning/start", timeout=10)
            data = resp.json()
            return {
                "status": "ok",
                "result": data.get("status"),
                "pane_id": data.get("pane_id"),
            }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


_DAILY_NOTE_BASENAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\.md$")


async def _consumer_custodes_doc_rebind() -> dict:
    """Rebind a live custodes bound to a prior-day daily note onto today's note.

    A custodes alive across midnight stays bound to yesterday's daily note. Only
    date-named daily-note bindings are rebound; bespoke dockets are left untouched.
    """
    from instance_mutation import sanctioned_update_instance
    from session_doc_helpers import resolve_or_create_today_daily_note_session_doc

    rebound: list[dict] = []
    skipped: list[dict] = []
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        today_id = await resolve_or_create_today_daily_note_session_doc(db)

        # Canonical "today" date string from the resolved note's own path so the
        # comparison matches whatever date the helper minted.
        cursor = await db.execute(
            "SELECT file_path FROM session_documents WHERE id = ?", (today_id,)
        )
        today_row = await cursor.fetchone()
        today_match = (
            _DAILY_NOTE_BASENAME_RE.search(today_row["file_path"])
            if today_row and today_row["file_path"]
            else None
        )
        today_date = today_match.group(1) if today_match else None

        cursor = await db.execute(
            """
            SELECT ci.id AS id, ci.session_doc_id AS session_doc_id, sd.file_path AS file_path
            FROM claude_instances ci
            LEFT JOIN session_documents sd ON sd.id = ci.session_doc_id
            WHERE ci.legion = 'custodes'
              AND ci.status IN ('idle', 'processing')
              AND ci.stopped_at IS NULL
            """
        )
        live = await cursor.fetchall()

        for row in live:
            file_path = row["file_path"]
            match = _DAILY_NOTE_BASENAME_RE.search(file_path) if file_path else None
            if not match:
                # No doc, or a bespoke docket — leave the binding untouched.
                skipped.append({"instance_id": row["id"], "reason": "not_daily_note"})
                continue
            if today_date and match.group(1) == today_date:
                skipped.append({"instance_id": row["id"], "reason": "already_today"})
                continue

            await sanctioned_update_instance(
                db,
                instance_id=row["id"],
                updates={"session_doc_id": today_id},
                mutation_type="instance_updated",
                write_source="day_start",
                actor="day_start:custodes_doc_rebind",
            )
            rebound.append({"instance_id": row["id"], "from_date": match.group(1)})

        if rebound:
            await db.commit()
            await log_event(
                "custodes_doc_rebound",
                details={"today_doc_id": today_id, "rebound": rebound},
            )

    return {"status": "ok", "rebound": rebound, "skipped": skipped}


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
        ("custodes_doc_rebind", _consumer_custodes_doc_rebind()),
        ("custodes_morning_session", _consumer_custodes_morning_session()),
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
