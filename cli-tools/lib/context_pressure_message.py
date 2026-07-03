"""Context-pressure hook message text — shared by ``tmux-context`` and the
forthcoming ctx-governor worker.

When an instance's context window crosses the flush threshold, a hook nudges the
agent to refocus.  The nudge must be PLAN-AWARE: an instance already in plan mode
must NOT be told to "switch to plan mode OR run /compact" — that derails a
planning turn (the agent is *already* planning).  It should instead pose the plan
immediately, from the context it already holds, without gathering more.

The ``planning_state`` value comes from the Token-API ``instances`` table
(``none``/``preplanning``/``planning``/``approving``); ``tmux-context`` already
has it in the instance dict it fetches from ``GET /api/instances``.
"""

from __future__ import annotations

# planning_state values that mean "already planning" and must not be told to
# enter plan mode again. Mirrors token-api PLANNING_STATES minus the idle "none".
PLAN_ACTIVE_STATES = frozenset({"planning", "preplanning", "approving"})

# Standard nudge: update the session doc, then recommend plan mode or /compact.
_STANDARD_MSG = (
    "Context threshold reached (250k). Pause your current task and update your "
    "session document comprehensively — include completed work, assumptions "
    "made, unvalidated code, design decisions, and remaining tasks. Then end "
    "your inference with an explicit recommendation: switch to plan mode OR run "
    "/compact to refocus."
)

# Plan-aware nudge: already planning — pose the plan now, do not gather context.
_PLAN_MODE_MSG = (
    "Your context is full and you are in plan mode. Do NOT gather any more "
    "context — pose the plan now from what you already have: state the approach, "
    "the specific files and changes, and any open decisions, then present it for "
    "approval. If a session document is linked, capture the plan there as you go."
)


def is_plan_active(planning_state: str | None) -> bool:
    """True when the instance is in a (pre)planning or approval state."""
    if not planning_state:
        return False
    return str(planning_state).strip().lower() in PLAN_ACTIVE_STATES


def context_full_message(planning_state: str | None = None) -> str:
    """Return the context-pressure nudge, plan-aware.

    In plan mode: pose the plan without gathering context.  Otherwise: the
    standard update-the-doc-then-plan-or-compact nudge.
    """
    return _PLAN_MODE_MSG if is_plan_active(planning_state) else _STANDARD_MSG
