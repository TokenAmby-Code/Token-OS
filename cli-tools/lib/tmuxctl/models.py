from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .enums import (
    AttachmentClass,
    CoherenceSeverity,
    GridState,
    InstanceStatus,
    PaneKind,
    RestartPhase,
    ResumeDisposition,
    WindowArchetype,
)


@dataclass(frozen=True)
class PaneSnapshot:
    pane_id: str
    session_name: str
    window_index: int
    window_name: str
    pane_index: int
    width: int
    height: int
    current_command: str
    tty: str
    pane_role: str
    grid_state: GridState
    pane_kind: PaneKind
    reserved: bool
    active: bool
    tombstone_target: str = ""
    tombstone_source: str = ""


@dataclass(frozen=True)
class WindowSnapshot:
    session_name: str
    window_index: int
    window_name: str
    archetype: WindowArchetype
    focused: bool
    grid_expanded: str
    grid_stash: str
    side_expanded: str
    panes: tuple[PaneSnapshot, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def target(self) -> str:
        return f"{self.session_name}:{self.window_index}"


@dataclass(frozen=True)
class WorkspaceSnapshot:
    session_name: str
    windows: tuple[WindowSnapshot, ...]

    def iter_panes(self) -> Iterable[PaneSnapshot]:
        for window in self.windows:
            yield from window.panes


@dataclass(frozen=True)
class InstanceRegistryEntry:
    instance_id: str
    device_id: str
    pane_label: str
    tmux_pane: str
    working_dir: str
    status: InstanceStatus
    pre_stop_status: InstanceStatus
    is_subagent: bool = False
    last_activity: str = ""
    stopped_at: str = ""

    @property
    def was_processing(self) -> bool:
        return self.status is InstanceStatus.PROCESSING or (
            self.status is InstanceStatus.STOPPED
            and self.pre_stop_status is InstanceStatus.PROCESSING
        )


@dataclass(frozen=True)
class InstanceRegistrySnapshot:
    device_id: str
    instances: tuple[InstanceRegistryEntry, ...]


@dataclass(frozen=True)
class ClientAttachment:
    client_tty: str
    session_name: str
    is_remote: bool
    client_name: str = ""
    leader_session_name: str = ""
    selected_window_index: int = -1
    selected_window_name: str = ""
    attachment_class: AttachmentClass = AttachmentClass.LOCAL_LEADER


@dataclass(frozen=True)
class GroupedSessionSnapshot:
    session_name: str
    leader_session_name: str
    selected_window_index: int
    selected_window_name: str


@dataclass(frozen=True)
class CoherenceIssue:
    severity: CoherenceSeverity
    code: str
    message: str
    instance_id: str = ""
    pane_label: str = ""
    pane_id: str = ""


@dataclass(frozen=True)
class PlannedResume:
    instance_id: str
    pane_label: str
    target_pane_id: str
    working_dir: str
    disposition: ResumeDisposition
    reason: str
    target_hidden_until_rebuild: bool = False


@dataclass(frozen=True)
class RestartPlan:
    session_name: str
    phase: RestartPhase
    resumes: tuple[PlannedResume, ...] = field(default_factory=tuple)
    skipped: tuple[PlannedResume, ...] = field(default_factory=tuple)
    client_attachments: tuple[ClientAttachment, ...] = field(default_factory=tuple)
    grouped_sessions: tuple[GroupedSessionSnapshot, ...] = field(default_factory=tuple)
    coherence_issues: tuple[CoherenceIssue, ...] = field(default_factory=tuple)

    @property
    def has_errors(self) -> bool:
        return any(issue.severity is CoherenceSeverity.ERROR for issue in self.coherence_issues)


@dataclass(frozen=True)
class RestartAction:
    phase: RestartPhase
    description: str


@dataclass(frozen=True)
class ResumeResult:
    instance_id: str
    pane_label: str
    target_pane_id: str
    disposition: ResumeDisposition
    success: bool
    message: str


@dataclass(frozen=True)
class RestartExecutionResult:
    session_name: str
    phase: RestartPhase
    plan: RestartPlan
    actions: tuple[RestartAction, ...] = field(default_factory=tuple)
    resume_results: tuple[ResumeResult, ...] = field(default_factory=tuple)
    coherence_issues: tuple[CoherenceIssue, ...] = field(default_factory=tuple)
    postcondition_violations: tuple[str, ...] = field(default_factory=tuple)
    clients_parked: int = 0
    clients_detached: int = 0
    clients_restored: int = 0
    grouped_sessions_recreated: int = 0

    @property
    def resumes_attempted(self) -> int:
        return len(self.resume_results)

    @property
    def resumes_succeeded(self) -> int:
        return sum(1 for result in self.resume_results if result.success)

    @property
    def resumes_failed(self) -> int:
        return sum(1 for result in self.resume_results if not result.success)

    @property
    def is_success(self) -> bool:
        has_error = any(
            issue.severity is CoherenceSeverity.ERROR for issue in self.coherence_issues
        )
        return (
            self.phase in {RestartPhase.VERIFY, RestartPhase.COMPLETE}
            and not has_error
            and not self.postcondition_violations
            and self.resumes_failed == 0
        )
