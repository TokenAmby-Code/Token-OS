"""Phone-bubble AskUserQuestion bridge.

When an agent calls Claude Code's ``AskUserQuestion`` tool, its PreToolUse hook
fires a rich notification bubble at the phone (MacroDroid ``/ask`` endpoint) and
returns ``allow`` immediately — the terminal selector renders and blocks the turn
as usual. A background task owns the rest of the lifecycle:

  fire bubble -> await the phone's async ``/api/ask/answer`` callback -> (serialize
  over multiple questions) -> ESC-cancel the terminal selector -> type+submit a
  synthesized natural-language prompt as a fresh turn.

Why async-inject (not a blocking hook): ``generic-hook.sh`` caps the synchronous
PreToolUse curl at ``--max-time 3``, so the hook CANNOT block for a human phone
answer. And because the terminal selector is only cancelled once a real phone
answer lands, an unreachable phone / timeout simply leaves the selector up as a
clean in-terminal fallback. ``PostToolUse(AskUserQuestion)`` (operator answered in
the terminal) cancels any pending phone-ask.

Everything runs in the single token-api FastAPI event loop: the ``asyncio.Future``
created here is resolved by the ``/api/ask/answer`` route in the same loop.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

import shared
from phone_service import _send_to_phone_raw

logger = logging.getLogger("token-api.ask")

# Correlation-id -> Future resolved with {"choice","answer"} by /api/ask/answer.
_PENDING: dict[str, asyncio.Future] = {}
# session_id (instance id) -> live background orchestration task (for cancel).
_SESSION_TASKS: dict[str, asyncio.Task] = {}

# How long to wait for the operator to answer one bubble before giving up and
# leaving the terminal selector as the fallback. Human-paced; the phone callback
# is async so this is a normal asyncio wait, unrelated to the phone's ~10s
# held-response ceiling.
PHONE_ANSWER_TIMEOUT_S = 300.0
# Settle gap between the ESC that cancels the selector and the text injection.
_ESC_SETTLE_S = 0.35


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def resolve(corr_id: str, choice: str, answer: str) -> bool:
    """Idempotently resolve a pending question. Returns True iff it matched a
    still-open waiter. Late/duplicate/unknown ids are a harmless no-op (the phone
    callback is a terminal-state guarantee — it may fire more than once)."""
    fut = _PENDING.get(corr_id)
    if fut is None or fut.done():
        return False
    fut.set_result({"choice": choice or "", "answer": answer or ""})
    return True


# ──────────────────────────── /ask param mapping ────────────────────────────


def map_question(q: dict) -> dict:
    """Map one AskUserQuestion ``questions[]`` entry onto ``/ask`` params.

    Schema: ``{question, header, multiSelect, options:[{label,description}]}``.
    No explicit recommended flag — this system's convention is options[0]. The
    phone shows 3 option buttons + a dynamic menu; >3 options are appended into
    the HTML body so they remain visible (operator can free-text any of them).
    """
    options = [
        (o.get("label") or "").strip()
        for o in (q.get("options") or [])
        if isinstance(o, dict) and (o.get("label") or "").strip()
    ]
    body = q.get("question") or ""
    if q.get("multiSelect"):
        body += "<br><br><i>(multi-select — pick one and add the rest as a note)</i>"
    extra = options[3:]
    if extra:
        body += "<br><br>More options: " + ", ".join(extra)
    return {
        "header": (q.get("header") or "question").strip() or "question",
        "title": (q.get("header") or "Question").strip() or "Question",
        "question": body,
        "recc": options[0] if options else "",
        "options": options[:3],
    }


def _ask_params(corr_id: str, mapped: dict) -> dict:
    params = {
        "id": corr_id,
        "title": mapped["title"],
        "question": mapped["question"],
        "recc": mapped["recc"],
    }
    opts = mapped["options"]
    for i in range(3):
        params[f"opt{i + 1}"] = opts[i] if i < len(opts) else ""
    return params


async def _fire_ask(params: dict) -> bool:
    """Fire the ``/ask`` GET at the phone; its 200 ack means the bubble posted."""
    res = await asyncio.to_thread(_send_to_phone_raw, "/ask", params)
    return bool(res and res.get("success"))


async def ask_once(mapped: dict, *, timeout: float = PHONE_ANSWER_TIMEOUT_S) -> dict | None:
    """Fire one bubble and await its answer. Returns ``{"choice","answer"}`` or
    ``None`` (phone unreachable, timeout, or cancelled)."""
    corr_id = _new_id()
    fut: asyncio.Future = asyncio.get_event_loop().create_future()
    _PENDING[corr_id] = fut
    try:
        if not await _fire_ask(_ask_params(corr_id, mapped)):
            logger.info("phone-ask: bubble fire failed (phone unreachable)")
            return None
        return await asyncio.wait_for(fut, timeout=timeout)
    except TimeoutError:
        logger.info("phone-ask: no answer within %ss for %r", timeout, mapped["header"])
        return None
    except asyncio.CancelledError:
        raise
    finally:
        _PENDING.pop(corr_id, None)


# ──────────────────────────── prompt synthesis ────────────────────────────


def _segment(ordinal: str, header: str, choice: str, notes: str) -> str:
    seg = f'to answer your {ordinal} question "{header}" I pick {choice}'
    if notes:
        seg += f" with commentary: {notes}"
    return seg


def build_prompt(answered: list[dict]) -> str:
    """answered: list of ``{"header","choice","notes"}`` in question order."""
    if len(answered) == 1:
        a = answered[0]
        # Uppercase only the leading "to" — .capitalize() would lowercase the
        # rest, mangling the operator's header/choice text (e.g. "OAuth").
        seg = _segment("first", a["header"], a["choice"], a["notes"])
        return seg[0].upper() + seg[1:] + "."
    ordinals = ["first", "second", "third", "fourth"]
    parts = [
        _segment(
            ordinals[i] if i < len(ordinals) else f"#{i + 1}", a["header"], a["choice"], a["notes"]
        )
        for i, a in enumerate(answered)
    ]
    return ("To " + "; ".join(parts)[3:]).rstrip(".") + "."


def _split_choice_notes(result: dict) -> tuple[str, str]:
    """Derive (choice, notes) from the phone payload. ``choice`` is the tapped
    option/recc/none; ``answer`` is the final (possibly edited) text field.
    Commentary exists only when the operator edited the prefill (answer != choice).
    Pure free-text (no choice) becomes the choice itself."""
    choice = (result.get("choice") or "").strip()
    answer = (result.get("answer") or "").strip()
    if not choice and answer:
        return answer, ""
    notes = answer if (answer and answer != choice) else ""
    return (choice or "none"), notes


# ──────────────────────────── injection ────────────────────────────


async def _inject(session_id: str, prompt: str) -> bool:
    """ESC-cancel the terminal AskUserQuestion selector, then type+submit the
    synthesized prompt as a fresh turn. Fails closed if the pane is unresolved or
    the human-typing lock is engaged (the gated send refuses)."""
    pane, _role = await shared.resolve_instance_pane(session_id)
    if not pane:
        logger.warning("phone-ask: pane unresolved for %s; cannot inject", session_id[:12])
        return False
    # Dismiss the multiple-choice selector so the prompt accepts free text.
    await asyncio.to_thread(
        shared._tmuxctld_post_json,
        "/tmux/send-keys",
        {"pane": pane, "command": "Escape"},
        default_loopback=True,
    )
    await asyncio.sleep(_ESC_SETTLE_S)
    env = await asyncio.to_thread(
        shared._tmuxctld_post_json,
        "/instance/send-text",
        {"instance_id": session_id, "text": prompt, "submit": True},
        default_loopback=True,
    )
    ok = bool(env and env.get("ok"))
    if not ok:
        logger.warning("phone-ask: send-text envelope not ok for %s: %s", session_id[:12], env)
    return ok


# ──────────────────────────── orchestration ────────────────────────────


async def _run(session_id: str, questions: list[dict]) -> None:
    answered: list[dict] = []
    try:
        for q in questions:
            mapped = map_question(q)
            result = await ask_once(mapped)
            if result is None:
                # Unreachable / timed out: abandon the phone path and leave the
                # terminal selector up for the operator. No injection.
                logger.info(
                    "phone-ask: abandoning %s (no phone answer); terminal selector stands",
                    session_id[:12],
                )
                return
            choice, notes = _split_choice_notes(result)
            answered.append({"header": mapped["header"], "choice": choice, "notes": notes})
        prompt = build_prompt(answered)
        # Drop our own task handle BEFORE injecting: the ESC may emit a
        # PostToolUse(AskUserQuestion) whose cancel() must not abort our inject.
        _SESSION_TASKS.pop(session_id, None)
        ok = await _inject(session_id, prompt)
        logger.info("phone-ask: %s inject ok=%s: %s", session_id[:12], ok, prompt[:140])
    except asyncio.CancelledError:
        logger.info("phone-ask: %s cancelled (answered in terminal?)", session_id[:12])
        raise
    finally:
        _SESSION_TASKS.pop(session_id, None)


def start_phone_ask(session_id: str, questions: list[dict]) -> bool:
    """Kick off the background bubble→answer→inject lifecycle and return at once.
    Replaces any prior pending phone-ask for this instance."""
    if not session_id or not questions:
        return False
    cancel(session_id)
    _SESSION_TASKS[session_id] = asyncio.create_task(_run(session_id, questions))
    return True


def cancel(session_id: str) -> None:
    """Cancel a pending phone-ask (e.g. the operator answered in the terminal)."""
    task = _SESSION_TASKS.pop(session_id, None)
    if task and not task.done():
        task.cancel()
