"""Phone callback receiver for the AskUserQuestion phone-bubble bridge.

The phone's ``ask_notes`` MacroDroid macro POSTs the operator's answer here once
they finish the bubble. This resolves the matching pending question so the
background orchestrator in ``ask_service`` can inject the answer back into the
agent's terminal. See ``ask_service`` for the full lifecycle.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

import ask_service
from shared import log_event

logger = logging.getLogger("token-api.ask")

router = APIRouter()


class AskAnswer(BaseModel):
    id: str
    # `choice` = the tapped option/recc/none; `answer` = the final (possibly
    # edited) text field. Older macro revisions sent only `answer`; both optional
    # so a partial payload still resolves the waiter (terminal-state guarantee).
    choice: str | None = None
    answer: str | None = None


@router.post("/api/ask/answer")
async def ask_answer(payload: AskAnswer) -> dict:
    """Resolve a pending phone question. Idempotent and never blocks: always 200,
    even for an unknown/late/duplicate id (the phone callback may fire on empty or
    repeated terminal states)."""
    choice = payload.choice or ""
    answer = payload.answer or ""
    matched = ask_service.resolve(payload.id, choice, answer)
    logger.info(
        "ask/answer id=%s matched=%s choice=%r answer=%r",
        payload.id,
        matched,
        choice[:80],
        answer[:80],
    )
    await log_event(
        "ask_answered",
        details={
            "id": payload.id,
            "choice": choice[:200],
            "answer": answer[:200],
            "matched": matched,
        },
    )
    return {"ok": True, "matched": matched}
