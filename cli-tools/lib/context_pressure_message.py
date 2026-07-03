"""Context-pressure messages shared by statusLine telemetry and ctx-governor."""

from __future__ import annotations

PLAN_ACTIVE_STATES = frozenset({"planning", "preplanning", "approving"})

_STANDARD_MSG = (
    "Context threshold reached (250k). Pause your current task and update your "
    "session document comprehensively — include completed work, assumptions "
    "made, unvalidated code, design decisions, and remaining tasks. Then end "
    "your inference with an explicit recommendation: switch to plan mode OR run "
    "/compact to refocus."
)

_PLAN_MODE_MSG = "Context full. Pose the plan without gathering context."

_GOVERNOR_SOFT_MSG = (
    "Context is high. Before continuing, checkpoint your session doc with completed work, "
    "decisions, blockers, files changed, and next steps. Then compact or hand off as "
    "instructed. Do not gather more context first."
)

_GOVERNOR_HARD_MSG = (
    "Context is over the autonomous limit. Checkpoint your session doc now with completed "
    "work, decisions, blockers, files changed, and next steps. Then run the engine-appropriate "
    "compaction path or hand off. Do not gather more context first."
)


def is_plan_active(planning_state: str | None) -> bool:
    if not planning_state:
        return False
    return str(planning_state).strip().lower() in PLAN_ACTIVE_STATES


def context_full_message(planning_state: str | None = None) -> str:
    return _PLAN_MODE_MSG if is_plan_active(planning_state) else _STANDARD_MSG


def context_governor_message(*, stage: str, planning_state: str | None = None) -> str:
    if is_plan_active(planning_state):
        return _PLAN_MODE_MSG
    if stage == "soft_stop":
        return _GOVERNOR_SOFT_MSG
    if stage in {"hard_injection", "no_progress_stop"}:
        return _GOVERNOR_HARD_MSG
    return context_full_message(planning_state)
