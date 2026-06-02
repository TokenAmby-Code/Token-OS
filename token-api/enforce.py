"""Stateless atomic enforce emitter.

Every call fires a Pavlok shock (>=25 intensity) AND a notification.
No warnings, no soft tiers, no cooldowns. Guardrails: quiet-hours +
in-meeting only. Escalation, ack tracking, and ladder logic live in
Golden Throne — not here.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from notify import NotifyRequest, dispatch_notification
from phone_service import send_pavlok_stimulus
from shared import DESKTOP_STATE, log_event

logger = logging.getLogger("token_api")

ENFORCE_MIN_INTENSITY = 25


class EnforceRequest(BaseModel):
    message: str
    intensity: int = 50
    source: str = "api"
    context: dict | None = None


_is_quiet_hours = None
_typing_guard_active = None


def init_deps(*, is_quiet_hours=None, typing_guard_active=None) -> None:
    """Late-bind dependencies from main.py to avoid circular imports."""
    global _is_quiet_hours, _typing_guard_active
    if is_quiet_hours is not None:
        _is_quiet_hours = is_quiet_hours
    if typing_guard_active is not None:
        _typing_guard_active = typing_guard_active


def _in_meeting() -> bool:
    return bool(DESKTOP_STATE.get("in_meeting"))


async def enforce(request: EnforceRequest) -> dict:
    """Atomic single-shot enforce: Pavlok shock + notification.

    Returns {"fired": True, ...} on success or
    {"fired": False, "blocked_by": <reason>} if blocked by a guardrail.
    """
    if _is_quiet_hours and _is_quiet_hours():
        await log_event(
            "enforce_blocked",
            details={
                "reason": "quiet_hours",
                "source": request.source,
                "message": request.message[:200],
            },
        )
        return {"fired": False, "blocked_by": "quiet_hours"}

    # The physical Pavlok stays typing-guard-blocked: typing IS the appeal — if the
    # Emperor is actively at the keyboard the shock is held, not fired (D2, Emperor
    # 2026-05-31). Mirrors the universal send gate's typing-guard predicate.
    if _typing_guard_active and _typing_guard_active():
        await log_event(
            "enforce_blocked",
            details={
                "reason": "typing_guard",
                "source": request.source,
                "message": request.message[:200],
            },
        )
        return {"fired": False, "blocked_by": "typing_guard"}

    if _in_meeting():
        await log_event(
            "enforce_blocked",
            details={
                "reason": "meeting",
                "source": request.source,
                "message": request.message[:200],
            },
        )
        return {"fired": False, "blocked_by": "meeting"}

    intensity = max(int(request.intensity), ENFORCE_MIN_INTENSITY)
    pavlok_result = send_pavlok_stimulus(
        stimulus_type="zap",
        value=intensity,
        reason=f"enforce_{request.source}",
    )
    await log_event("pavlok_stimulus", details=pavlok_result)

    notify_result = await dispatch_notification(
        NotifyRequest(
            message=request.message,
            type="tts",
        )
    )

    await log_event(
        "enforce",
        details={
            "source": request.source,
            "intensity": intensity,
            "message": request.message[:200],
            "pavlok": pavlok_result,
            "notify": notify_result,
            "context": request.context,
        },
    )

    return {
        "fired": True,
        "intensity": intensity,
        "pavlok": pavlok_result,
        "notify": notify_result,
    }
