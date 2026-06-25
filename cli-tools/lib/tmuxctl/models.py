from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Self

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
from .labels import canonical_pane_role

_ROLE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class PaneRole(str):
    """Typed, canonical pane identity from @PANE_ID.

    Empty/unset roles are represented as ``None`` by ``parse``. Non-empty roles
    must be public logical identities such as ``palace:N``, ``mechanicus:1``,
    ``council:custodes``, or ``audience:palace:N``; raw tmux ``%pane`` ids and
    whitespace-bearing values are rejected at construction.
    """

    def __new__(cls, value: str) -> Self:
        if not isinstance(value, str):
            raise ValueError(f"pane role must be a string, got {type(value).__name__}")
        raw = value.strip()
        if not raw:
            raise ValueError("pane role must not be empty")
        if raw.startswith("%") or any(ch.isspace() for ch in raw):
            raise ValueError(f"invalid pane role: {value!r}")
        canonical = canonical_pane_role(raw)
        parts = canonical.split(":")
        if len(parts) < 2:
            raise ValueError(f"pane role must be page-qualified: {value!r}")
        if parts[0] == "audience" and len(parts) < 3:
            raise ValueError(f"audience pane role must include source page and slot: {value!r}")
        if any(not _ROLE_SEGMENT_RE.fullmatch(part) for part in parts):
            raise ValueError(f"invalid pane role: {value!r}")
        return str.__new__(cls, canonical)

    @classmethod
    def parse(cls, value: str | None | Self) -> Self | None:
        if value is None or value == "":
            return None
        if isinstance(value, cls):
            return value
        return cls(value)


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
    pane_role: PaneRole | str | None
    grid_state: GridState
    pane_kind: PaneKind
    reserved: bool
    active: bool
    # Live @INSTANCE_ID stamp (the post-Slice-B reverse-lookup source of truth).
    # Empty when no agent is registered in the pane.
    instance_id: str = ""
    tombstone_target: str = ""
    tombstone_source: str = ""
    cwd: str = ""
    runtime_engine: str = ""
    wrapper_launch_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "pane_role", PaneRole.parse(self.pane_role))
        if not isinstance(self.grid_state, GridState):
            try:
                object.__setattr__(self, "grid_state", GridState(str(self.grid_state)))
            except ValueError as exc:
                raise ValueError(
                    f"invalid grid state for {self.pane_id}: {self.grid_state!r}"
                ) from exc
        if not isinstance(self.pane_kind, PaneKind):
            try:
                object.__setattr__(self, "pane_kind", PaneKind(str(self.pane_kind)))
            except ValueError as exc:
                raise ValueError(
                    f"invalid pane kind for {self.pane_id}: {self.pane_kind!r}"
                ) from exc


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
    grid_focus_active: bool = False
    grid_focus_pane: str = ""
    grid_focus_stash: str = ""
    side_focus_active: bool = False
    side_focus_pane: str = ""
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
    legion: str = ""
    tab_name: str = ""
    instance_type: str = ""
    engine: str = ""
    last_activity: str = ""
    stopped_at: str = ""
    created_at: str = ""
    # Persona identity. The state-hook dispatcher resolves singletons that share a
    # legion by primarch (e.g. `_resolve_administratum_instance` keys on
    # `primarch='administratum'`); the persona watchdog matches on it too.
    primarch: str = ""
    # Canonical persona identity. Post sync-decouple, /api/instances no longer
    # exposes legion/primarch/instance_type — it surfaces the instances.persona_id
    # JOIN as persona.slug plus the durable rank. These are the load-bearing
    # identity columns the watchdog must match on (mirrors
    # personas.resolve_live_persona_instance: persona slug + rank != 'retired').
    persona_slug: str = ""
    rank: str = ""

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
    tombstone_role: str = ""
    engine: str = ""
    source: str = "registry"


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
