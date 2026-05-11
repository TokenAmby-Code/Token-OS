from __future__ import annotations

from enum import Enum


class WindowArchetype(str, Enum):
    UNKNOWN = "unknown"
    PALACE = "palace"
    SOMNIUM = "somnium"
    LEGION_STACK = "legion_stack"
    MECHANICUS_STACK = "mechanicus_stack"
    TUI_SINGLE = "tui_single"


class GridState(str, Enum):
    UNKNOWN = "unknown"
    SMALL = "small"
    SIDE = "side"
    MINI = "mini"
    WIDE = "wide"


class PaneKind(str, Enum):
    UNKNOWN = "unknown"
    AUDIENCE = "audience"
    TUI = "tui"
    LEGION = "legion"
    MECHANICUS = "mechanicus"
    TOMBSTONE = "tombstone"


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
