from __future__ import annotations

from enum import Enum


class WindowArchetype(str, Enum):
    UNKNOWN = "unknown"
    PALACE = "palace"
    SOMNIUM = "somnium"
    COUNCIL = "council"
    MECHANICUS_STACK = "mechanicus_stack"


class GridState(str, Enum):
    UNKNOWN = "unknown"
    SMALL = "small"
    SIDE = "side"
    MINI = "mini"
    WIDE = "wide"


class PaneKind(str, Enum):
    UNKNOWN = "unknown"
    AUDIENCE = "audience"
    COUNCIL = "council"
    MECHANICUS = "mechanicus"
    TOMBSTONE = "tombstone"


class SeatVacancyPolicy(str, Enum):
    """How a vacated (runtime-dead) standing seat is treated by reconcile.

    ``MUST_FILL`` — a perpetual seat: when its runtime dies the daemon respawns it
    unconditionally (the six persona singleton seats and the standing reservist
    seats). ``FILL_IF_ROW`` — an ephemeral seat: refill only when a registry row
    still binds the pane, otherwise let it die (stack workers).
    """

    MUST_FILL = "must_fill"
    FILL_IF_ROW = "fill_if_row"


# Vacancy policy per seat CLASS — the normalized seat kind, NOT the raw live
# ``@PANE_TYPE`` (the persona singleton seats carry their page region as the type,
# ``council`` / ``mechanicus``, and are recognized by their stable pane LABEL).
# ``assertions.seat_class`` maps a pane (label, type) onto one of these keys.
#
#   persona      — the six singleton persona seats (council:custodes, …)
#   reservists   — the two standing reservist seats
#   stack-worker — ephemeral mechanicus/stack workers
VACANCY_POLICY: dict[str, SeatVacancyPolicy] = {
    "persona": SeatVacancyPolicy.MUST_FILL,
    "reservists": SeatVacancyPolicy.MUST_FILL,
    "stack-worker": SeatVacancyPolicy.FILL_IF_ROW,
}


class InstanceStatus(str, Enum):
    UNKNOWN = "unknown"
    IDLE = "idle"
    PROCESSING = "processing"
    STOPPED = "stopped"


class ResumeDisposition(str, Enum):
    SKIP = "skip"
    RESUME = "resume"
    RESUME_AND_CONTINUE = "resume_and_continue"


class CoherenceSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class AttachmentClass(str, Enum):
    LOCAL_LEADER = "local_leader"
    LOCAL_GROUPED = "local_grouped"
    REMOTE_LEADER = "remote_leader"
    REMOTE_GROUPED = "remote_grouped"


class RestartPhase(str, Enum):
    CAPTURE = "capture"
    COHERENCE_CHECK = "coherence_check"
    TEARDOWN = "teardown"
    REBUILD = "rebuild"
    RESTORE = "restore"
    VERIFY = "verify"
    COMPLETE = "complete"
