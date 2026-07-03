"""Autonomous/headless context governor policy.

Token-API owns policy/state/audit. tmuxctld owns prompt/stop actuation.
StatusLine and engine hooks are telemetry producers only.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

import shared

_CLI_LIB = Path(__file__).resolve().parents[1] / "cli-tools" / "lib"
if str(_CLI_LIB) not in sys.path:
    sys.path.insert(0, str(_CLI_LIB))
from context_pressure_message import context_governor_message  # noqa: E402

SOFT_MIN_TOKENS = 130_000
HARD_MIN_TOKENS = 160_000
DEFAULT_NO_PROGRESS_TTL_SECONDS = 15 * 60
CONTEXT_STOP_PURPOSE = "context_governor_stop"

router = APIRouter()


class ContextTelemetryRequest(BaseModel):
    instance_id: str | None = None
    session_id: str | None = None
    pane: str | None = None
    engine: str | None = None
    used_tokens: int | None = None
    used_percentage: float | None = None
    context_window_tokens: int | None = None
    source: str = "telemetry"
    model: str | None = None
    no_progress_ttl_seconds: int | None = Field(default=None, ge=0, le=24 * 3600)


class ContextProgressRequest(BaseModel):
    instance_id: str
    event_type: str
    source: str = "hook"
    details: dict[str, Any] | None = None


class ContextSweepRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)


def _now() -> str:
    return datetime.now().isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _json(details: dict[str, Any] | None) -> str:
    return json.dumps(details or {}, sort_keys=True)


async def _audit(
    db: aiosqlite.Connection,
    *,
    instance_id: str | None,
    session_id: str | None,
    stage: str,
    action: str,
    details: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        """INSERT INTO context_governor_audit
           (instance_id, session_id, stage, action, details_json)
           VALUES (?, ?, ?, ?, ?)""",
        (instance_id, session_id, stage, action, _json(details)),
    )


def _normalize_used_tokens(req: ContextTelemetryRequest) -> int | None:
    if req.used_tokens is not None:
        return int(req.used_tokens)
    if req.used_percentage is not None and req.context_window_tokens is not None:
        return int(float(req.used_percentage) * int(req.context_window_tokens) / 100.0)
    return None


async def _resolve_instance(db: aiosqlite.Connection, req: ContextTelemetryRequest) -> dict | None:
    db.row_factory = aiosqlite.Row
    instance_id = (req.instance_id or req.session_id or "").strip()
    if not instance_id and req.pane:
        instance_id = (await shared.instance_id_for_pane(req.pane)) or ""
    if not instance_id:
        return None
    cursor = await db.execute(
        """SELECT i.*, p.slug AS persona_slug
           FROM instances i
           LEFT JOIN personas p ON p.id = i.persona_id
           WHERE i.id = ?
           LIMIT 1""",
        (instance_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


def classify_context_scope(row: dict | None) -> tuple[bool, str]:
    """Return (scoped, reason) using explicit registry facts, not pane-name guessing."""

    if not row:
        return False, "instance_not_found"
    if row.get("status") in {"stopped", "archived"}:
        return False, "inactive"
    if row.get("human_anchored_at"):
        return False, "human_anchored"

    origin = str(row.get("origin_type") or "").lower()
    persona = str(row.get("persona_slug") or "").lower()
    commander_type = str(row.get("commander_type") or "").lower()
    automated = bool(row.get("automated"))
    hook_driven = bool(row.get("hook_driven"))
    is_subagent = bool(row.get("is_subagent"))
    headless_origin = origin in {"cron", "dispatch", "api", "perpetual"}
    commanded = commander_type in {"persona", "chapter"}

    scoped = automated or hook_driven or is_subagent or headless_origin or commanded
    if persona in {"custodes", "emperor"} and not scoped:
        return False, "interactive_persona_exempt"
    if not scoped:
        return False, "interactive_exempt"
    return True, "autonomous_headless"


def _stage_for_used_tokens(used_tokens: int | None) -> str:
    if used_tokens is None:
        return "telemetry"
    if used_tokens > HARD_MIN_TOKENS:
        return "hard_injection"
    if SOFT_MIN_TOKENS <= used_tokens <= HARD_MIN_TOKENS:
        return "soft_stop"
    return "telemetry"


def _planning_state(row: dict | None) -> str | None:
    return (row or {}).get("planning_state") or "none"


def _message(stage: str, planning_state: str | None) -> str:
    return context_governor_message(stage=stage, planning_state=planning_state)


async def _existing_context_stop_subscription(
    db: aiosqlite.Connection, *, target_instance_id: str, target_pane: str | None
) -> int | None:
    if target_pane:
        cursor = await db.execute(
            """SELECT id FROM stop_hook_subscriptions
               WHERE target_instance_id = ?
                 AND target_pane = ?
                 AND purpose = ?
                 AND event = 'stop'
                 AND status = 'active'
               ORDER BY id DESC LIMIT 1""",
            (target_instance_id, target_pane, CONTEXT_STOP_PURPOSE),
        )
    else:
        cursor = await db.execute(
            """SELECT id FROM stop_hook_subscriptions
               WHERE target_instance_id = ?
                 AND purpose = ?
                 AND event = 'stop'
                 AND status = 'active'
               ORDER BY id DESC LIMIT 1""",
            (target_instance_id, CONTEXT_STOP_PURPOSE),
        )
    row = await cursor.fetchone()
    return int(row[0]) if row else None


async def _arm_stop_subscription(
    db: aiosqlite.Connection,
    *,
    instance_id: str,
    pane: str | None,
    payload: str,
) -> int:
    target_pane = pane or ""
    existing = await _existing_context_stop_subscription(
        db, target_instance_id=instance_id, target_pane=target_pane
    )
    if existing:
        await db.execute(
            """UPDATE stop_hook_subscriptions
               SET payload = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (payload, existing),
        )
        return existing
    try:
        cursor = await db.execute(
            """INSERT INTO stop_hook_subscriptions
               (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane,
                event, delivery, status, purpose, payload, oneshot)
               VALUES (?, ?, NULL, ?, 'stop', 'prompt', 'active', ?, ?, 1)""",
            (instance_id, target_pane, pane or instance_id, CONTEXT_STOP_PURPOSE, payload),
        )
    except aiosqlite.IntegrityError:
        existing = await _existing_context_stop_subscription(
            db, target_instance_id=instance_id, target_pane=target_pane
        )
        if existing:
            await db.execute(
                """UPDATE stop_hook_subscriptions
                   SET payload = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (payload, existing),
            )
            return existing
        raise
    return int(cursor.lastrowid)


async def _tmuxctld_context_governor_inject(
    *, instance_id: str, pane: str | None, text: str, stage: str
) -> dict | None:
    return await asyncio.to_thread(
        shared._tmuxctld_post_json,
        "/context-governor/inject",
        {
            "instance_id": instance_id,
            "pane": pane or "",
            "text": text,
            "stage": stage,
            "submit": True,
            "verify": True,
        },
        timeout=10.0,
        default_loopback=True,
    )


async def _tmuxctld_context_governor_stop(
    *, instance_id: str, pane: str | None, reason: str
) -> dict | None:
    return await asyncio.to_thread(
        shared._tmuxctld_post_json,
        "/context-governor/stop",
        {
            "instance_id": instance_id,
            "pane": pane or "",
            "reason": reason,
        },
        timeout=10.0,
        default_loopback=True,
    )


async def _upsert_state(
    db: aiosqlite.Connection,
    *,
    instance_id: str,
    session_id: str | None,
    engine: str | None,
    pane: str | None,
    used_tokens: int | None,
    context_window_tokens: int | None,
    planning_state: str | None,
    scoped: bool,
    scope_reason: str,
    stage: str,
    policy_state: str,
    armed_subscription_id: int | None = None,
    injected_at: str | None = None,
    no_progress_deadline_at: str | None = None,
) -> None:
    await db.execute(
        """INSERT INTO context_governor_state
           (instance_id, session_id, engine, pane, used_tokens, context_window_tokens,
            planning_state, scoped, scope_reason, stage, policy_state,
            armed_subscription_id, injected_at, no_progress_deadline_at, last_telemetry_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(instance_id) DO UPDATE SET
             session_id = excluded.session_id,
             engine = COALESCE(excluded.engine, context_governor_state.engine),
             pane = COALESCE(excluded.pane, context_governor_state.pane),
             used_tokens = excluded.used_tokens,
             context_window_tokens = excluded.context_window_tokens,
             planning_state = excluded.planning_state,
             scoped = excluded.scoped,
             scope_reason = excluded.scope_reason,
             stage = excluded.stage,
             policy_state = excluded.policy_state,
             armed_subscription_id = COALESCE(excluded.armed_subscription_id, context_governor_state.armed_subscription_id),
             injected_at = COALESCE(excluded.injected_at, context_governor_state.injected_at),
             no_progress_deadline_at = COALESCE(excluded.no_progress_deadline_at, context_governor_state.no_progress_deadline_at),
             last_telemetry_at = CURRENT_TIMESTAMP""",
        (
            instance_id,
            session_id,
            engine,
            pane,
            used_tokens,
            context_window_tokens,
            planning_state,
            1 if scoped else 0,
            scope_reason,
            stage,
            policy_state,
            armed_subscription_id,
            injected_at,
            no_progress_deadline_at,
        ),
    )


async def _current_state(db: aiosqlite.Connection, instance_id: str) -> dict | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM context_governor_state WHERE instance_id = ?", (instance_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


@router.post("/api/context-governor/telemetry")
async def ingest_context_telemetry(request: ContextTelemetryRequest) -> dict:
    used_tokens = _normalize_used_tokens(request)
    async with aiosqlite.connect(shared.DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        row = await _resolve_instance(db, request)
        instance_id = (row or {}).get("id") or request.instance_id or request.session_id
        if not instance_id:
            await _audit(
                db,
                instance_id=None,
                session_id=request.session_id,
                stage="telemetry",
                action="unresolved",
                details=request.model_dump(),
            )
            await db.commit()
            return {"success": False, "action": "instance_unresolved", "scoped": False}

        pane = (request.pane or "").strip() or None
        if not pane:
            pane, _ = await shared.resolve_instance_pane(instance_id)
        scoped, scope_reason = classify_context_scope(row)
        stage = _stage_for_used_tokens(used_tokens)
        planning_state = _planning_state(row)
        engine = request.engine or (row or {}).get("engine")
        session_id = request.session_id or instance_id

        if not scoped or stage == "telemetry":
            await _upsert_state(
                db,
                instance_id=instance_id,
                session_id=session_id,
                engine=engine,
                pane=pane,
                used_tokens=used_tokens,
                context_window_tokens=request.context_window_tokens,
                planning_state=planning_state,
                scoped=scoped,
                scope_reason=scope_reason,
                stage=stage,
                policy_state="telemetry_only",
            )
            await _audit(
                db,
                instance_id=instance_id,
                session_id=session_id,
                stage=stage,
                action="telemetry_only",
                details={"used_tokens": used_tokens, "scope_reason": scope_reason},
            )
            await db.commit()
            return {
                "success": True,
                "action": "telemetry_only",
                "scoped": scoped,
                "scope_reason": scope_reason,
                "stage": stage,
                "used_tokens": used_tokens,
            }

        existing_state = await _current_state(db, instance_id)
        if stage == "soft_stop":
            payload = _message(stage, planning_state)
            sub_id = await _arm_stop_subscription(
                db, instance_id=instance_id, pane=pane, payload=payload
            )
            await _upsert_state(
                db,
                instance_id=instance_id,
                session_id=session_id,
                engine=engine,
                pane=pane,
                used_tokens=used_tokens,
                context_window_tokens=request.context_window_tokens,
                planning_state=planning_state,
                scoped=True,
                scope_reason=scope_reason,
                stage=stage,
                policy_state="armed_stop_subscription",
                armed_subscription_id=sub_id,
            )
            await _audit(
                db,
                instance_id=instance_id,
                session_id=session_id,
                stage=stage,
                action="armed_stop_subscription",
                details={"subscription_id": sub_id, "used_tokens": used_tokens},
            )
            await db.commit()
            return {
                "success": True,
                "action": "armed_stop_subscription",
                "scoped": True,
                "stage": stage,
                "subscription_id": sub_id,
                "used_tokens": used_tokens,
            }

        # Hard threshold: immediate forced injection, debounced by instance/session.
        if (
            existing_state
            and existing_state.get("session_id") == session_id
            and existing_state.get("injected_at")
            and existing_state.get("policy_state")
            in {"forced_injection", "context_exhausted", "stop_requested"}
        ):
            await _upsert_state(
                db,
                instance_id=instance_id,
                session_id=session_id,
                engine=engine,
                pane=pane,
                used_tokens=used_tokens,
                context_window_tokens=request.context_window_tokens,
                planning_state=planning_state,
                scoped=True,
                scope_reason=scope_reason,
                stage=stage,
                policy_state="forced_injection",
            )
            await _audit(
                db,
                instance_id=instance_id,
                session_id=session_id,
                stage=stage,
                action="debounced",
                details={"used_tokens": used_tokens},
            )
            await db.commit()
            return {"success": True, "action": "debounced", "scoped": True, "stage": stage}

        text = _message(stage, planning_state)
        injected_at = _now()
        ttl = (
            request.no_progress_ttl_seconds
            if request.no_progress_ttl_seconds is not None
            else DEFAULT_NO_PROGRESS_TTL_SECONDS
        )
        deadline = (datetime.fromisoformat(injected_at) + timedelta(seconds=ttl)).isoformat()
        actuation = await _tmuxctld_context_governor_inject(
            instance_id=instance_id, pane=pane, text=text, stage=stage
        )
        await _upsert_state(
            db,
            instance_id=instance_id,
            session_id=session_id,
            engine=engine,
            pane=pane,
            used_tokens=used_tokens,
            context_window_tokens=request.context_window_tokens,
            planning_state=planning_state,
            scoped=True,
            scope_reason=scope_reason,
            stage=stage,
            policy_state="forced_injection",
            injected_at=injected_at,
            no_progress_deadline_at=deadline,
        )
        await _audit(
            db,
            instance_id=instance_id,
            session_id=session_id,
            stage=stage,
            action="forced_injection",
            details={"used_tokens": used_tokens, "pane": pane, "actuation": actuation},
        )
        await db.commit()
        return {
            "success": True,
            "action": "forced_injection",
            "scoped": True,
            "stage": stage,
            "used_tokens": used_tokens,
            "actuation": actuation,
        }


@router.post("/api/context-governor/progress")
async def record_context_progress(request: ContextProgressRequest) -> dict:
    now = _now()
    async with aiosqlite.connect(shared.DB_PATH, timeout=5.0) as db:
        await db.execute(
            """UPDATE context_governor_state
               SET last_progress_at = ?, policy_state = 'progress_observed'
               WHERE instance_id = ?""",
            (now, request.instance_id),
        )
        await _audit(
            db,
            instance_id=request.instance_id,
            session_id=None,
            stage="progress",
            action=request.event_type,
            details={"source": request.source, **(request.details or {})},
        )
        await db.commit()
    await shared.log_event(
        "context_governor_progress",
        instance_id=request.instance_id,
        details={"event_type": request.event_type, "source": request.source},
    )
    return {"success": True, "action": "progress_recorded", "instance_id": request.instance_id}


async def record_context_governor_progress(
    instance_id: str, event_type: str, *, source: str = "hook", details: dict | None = None
) -> dict:
    return await record_context_progress(
        ContextProgressRequest(
            instance_id=instance_id, event_type=event_type, source=source, details=details or {}
        )
    )


@router.post("/api/context-governor/sweep")
async def sweep_context_governor(request: ContextSweepRequest | None = None) -> dict:
    limit = request.limit if request else 50
    now = datetime.now()
    exhausted: list[dict[str, Any]] = []
    async with aiosqlite.connect(shared.DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM context_governor_state
               WHERE policy_state = 'forced_injection'
                 AND no_progress_deadline_at IS NOT NULL
               ORDER BY no_progress_deadline_at ASC
               LIMIT ?""",
            (limit,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        for row in rows:
            deadline = _parse_dt(row.get("no_progress_deadline_at"))
            progress = _parse_dt(row.get("last_progress_at"))
            if not deadline or deadline > now:
                continue
            injected = _parse_dt(row.get("injected_at"))
            # Only progress observed after the forced injection clears this
            # sweep cycle. Historical compaction before the injection must not
            # mask the no-progress stage.
            if progress and injected and progress >= injected:
                continue
            instance_id = row["instance_id"]
            pane = row.get("pane")
            stop_result = await _tmuxctld_context_governor_stop(
                instance_id=instance_id, pane=pane, reason="no_progress_after_context_injection"
            )
            await db.execute(
                """UPDATE context_governor_state
                   SET policy_state = 'context_exhausted', stage = 'no_progress_stop', exhausted_at = CURRENT_TIMESTAMP
                   WHERE instance_id = ?""",
                (instance_id,),
            )
            await _audit(
                db,
                instance_id=instance_id,
                session_id=row.get("session_id"),
                stage="no_progress_stop",
                action="context_exhausted",
                details={
                    "stop_result": stop_result,
                    "deadline": row.get("no_progress_deadline_at"),
                },
            )
            exhausted.append({"instance_id": instance_id, "pane": pane, "stop_result": stop_result})
        await db.commit()
    for item in exhausted:
        await shared.log_event(
            "context_governor_exhausted",
            instance_id=item["instance_id"],
            details={"pane": item.get("pane")},
        )
    return {
        "success": True,
        "action": "swept",
        "exhausted_count": len(exhausted),
        "exhausted": exhausted,
    }


@router.get("/api/context-governor/state")
async def get_context_governor_state(
    instance_id: str | None = None, limit: int = Query(default=50, ge=1, le=500)
) -> dict:
    clauses: list[str] = []
    params: list[Any] = []
    if instance_id:
        clauses.append("instance_id = ?")
        params.append(instance_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with aiosqlite.connect(shared.DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT * FROM context_governor_state
                {where}
                ORDER BY last_telemetry_at DESC
                LIMIT ?""",
            (*params, limit),
        )
        states = [dict(row) for row in await cursor.fetchall()]
        audit_cursor = await db.execute(
            f"""SELECT * FROM context_governor_audit
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?""",
            (*params, limit),
        )
        audit = [dict(row) for row in await audit_cursor.fetchall()]
    return {"success": True, "states": states, "audit": audit, "count": len(states)}
