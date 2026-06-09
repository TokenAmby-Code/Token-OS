from __future__ import annotations

import pathlib
import sys
from datetime import UTC, datetime, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.api import build_client_attachments
from tmuxctl.builder import build_workspace
from tmuxctl.enums import (
    AttachmentClass,
    CoherenceSeverity,
    GridState,
    InstanceStatus,
    PaneKind,
    ResumeDisposition,
    WindowArchetype,
)
from tmuxctl.executor import RestartExecutor
from tmuxctl.models import (
    GroupedSessionSnapshot,
    InstanceRegistryEntry,
    InstanceRegistrySnapshot,
    PaneSnapshot,
    WindowSnapshot,
    WorkspaceSnapshot,
)
from tmuxctl.planner import build_restart_plan


def _pane(
    pane_id: str, role: str, *, command: str = "zsh", window: str = "somnium"
) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        window_name=window,
        pane_index=0,
        width=100,
        height=40,
        current_command=command,
        tty="/dev/ttys001",
        pane_role=role,
        grid_state=GridState.SMALL,
        pane_kind=PaneKind.UNKNOWN,
        reserved=False,
        active=False,
    )


def _workspace(
    *panes: PaneSnapshot,
    window: str = "somnium",
    archetype: WindowArchetype = WindowArchetype.SOMNIUM,
) -> WorkspaceSnapshot:
    window_snapshot = WindowSnapshot(
        session_name="main",
        window_index=1,
        window_name=window,
        archetype=archetype,
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
        panes=tuple(panes),
    )
    return WorkspaceSnapshot(session_name="main", windows=(window_snapshot,))


def _instance(
    instance_id: str,
    pane_label: str,
    *,
    status: InstanceStatus = InstanceStatus.IDLE,
    pre_stop_status: InstanceStatus = InstanceStatus.IDLE,
    last_activity: str | None = None,
    stopped_at: str = "",
    tmux_pane: str = "%1",
    is_subagent: bool = False,
    legion: str = "",
) -> InstanceRegistryEntry:
    return InstanceRegistryEntry(
        instance_id=instance_id,
        device_id="Mac-Mini",
        pane_label=pane_label,
        tmux_pane=tmux_pane,
        working_dir="/Volumes/Imperium/Imperium-ENV",
        status=status,
        pre_stop_status=pre_stop_status,
        is_subagent=is_subagent,
        legion=legion,
        last_activity=last_activity if last_activity is not None else _iso_ago(hours=1),
        stopped_at=stopped_at,
    )


def _iso_ago(*, hours: int = 0, seconds: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours, seconds=seconds)).isoformat()


def test_restart_plan_dedupes_by_pane_label_and_keeps_newest():
    workspace = _workspace(_pane("%1", "somnium:NW"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance("old", "somnium:NW", last_activity=_iso_ago(hours=2)),
            _instance("new", "somnium:NW", last_activity=_iso_ago(hours=1)),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    assert [resume.instance_id for resume in plan.resumes] == ["new"]
    assert any(
        issue.code == "duplicate_pane_label" and issue.severity is CoherenceSeverity.WARNING
        for issue in plan.coherence_issues
    )


def test_restart_plan_ignores_stale_stopped_duplicate_claims():
    workspace = _workspace(_pane("%1", "somnium:NW"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance(
                "old-stopped",
                "somnium:NW",
                status=InstanceStatus.STOPPED,
                stopped_at=_iso_ago(hours=1),
                tmux_pane="%9",
            ),
            _instance("active", "somnium:NW", last_activity=_iso_ago(seconds=30)),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    assert [resume.instance_id for resume in plan.resumes] == ["active"]
    assert not any(issue.code == "duplicate_pane_label" for issue in plan.coherence_issues)


def test_restart_plan_includes_recent_stop_and_excludes_stale_activity():
    workspace = _workspace(_pane("%1", "somnium:NW"), _pane("%2", "somnium:NE"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance("stale", "somnium:NW", last_activity=_iso_ago(hours=72)),
            _instance(
                "recent-stop",
                "somnium:NE",
                status=InstanceStatus.STOPPED,
                pre_stop_status=InstanceStatus.PROCESSING,
                stopped_at=_iso_ago(seconds=30),
                tmux_pane="%9",
            ),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    assert [resume.instance_id for resume in plan.resumes] == ["recent-stop"]
    assert plan.resumes[0].disposition is ResumeDisposition.RESUME_AND_CONTINUE


def test_restart_plan_flags_busy_targets_and_pane_id_drift():
    workspace = _workspace(_pane("%2", "somnium:NW", command="claude"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "somnium:NW", tmux_pane="%1"),),
    )

    plan = build_restart_plan(workspace, registry)

    codes = {issue.code: issue.severity for issue in plan.coherence_issues}
    assert codes["pane_id_mismatch"] is CoherenceSeverity.WARNING
    assert codes["target_busy"] is CoherenceSeverity.WARNING
    assert not plan.has_errors


def test_restart_plan_flags_codex_targets_busy():
    workspace = _workspace(_pane("%2", "somnium:NW", command="codex"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "somnium:NW", tmux_pane="%2"),),
    )

    plan = build_restart_plan(workspace, registry)

    codes = {issue.code: issue.severity for issue in plan.coherence_issues}
    assert codes["target_busy"] is CoherenceSeverity.WARNING
    assert not plan.has_errors


def test_restart_plan_marks_promoted_custodes_for_legion_tombstone():
    workspace = _workspace(_pane("%2", "somnium:NE"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("custodes", "somnium:NE", tmux_pane="%2", legion="custodes"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert plan.resumes[0].pane_label == "somnium:NE"
    assert plan.resumes[0].tombstone_role == "legion:custodes"


def test_restart_plan_marks_promoted_fabricator_for_mechanicus_tombstone():
    workspace = _workspace(_pane("%2", "palace:N", window="palace"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("fabricator", "palace:NE", tmux_pane="%2", legion="fabricator"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert plan.resumes[0].pane_label == "palace:N"
    assert plan.resumes[0].tombstone_role == "mechanicus:fabricator-general"


def test_hidden_but_legal_palace_side_is_planned_for_post_rebuild_resolution():
    workspace = _workspace(
        _pane("%1", "palace:N", window="palace"),
        _pane("%3", "palace:S", window="palace"),
        window="palace",
        archetype=WindowArchetype.PALACE,
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "palace:W", tmux_pane="%99"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert len(plan.resumes) == 1
    assert plan.resumes[0].target_hidden_until_rebuild is True
    assert not any(issue.code == "missing_target_pane" for issue in plan.coherence_issues)


def test_palace_happy_path_resumes_grid_and_side_labels():
    workspace = _workspace(
        _pane("%1", "palace:W", window="palace"),
        _pane("%2", "palace:N", window="palace"),
        _pane("%3", "palace:S", window="palace"),
        _pane("%6", "palace:E", window="palace"),
        window="palace",
        archetype=WindowArchetype.PALACE,
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(
            _instance("alpha", "palace:N", tmux_pane="%2"),
            _instance("beta", "palace:E", tmux_pane="%6"),
        ),
    )

    plan = build_restart_plan(workspace, registry)

    resumed = {resume.instance_id: resume.target_pane_id for resume in plan.resumes}
    assert resumed == {"alpha": "%2", "beta": "%6"}
    assert all(not resume.target_hidden_until_rebuild for resume in plan.resumes)


def test_build_client_attachments_classifies_local_remote_and_grouped():
    managed = (
        GroupedSessionSnapshot("main", "main", 0, "somnium"),
        GroupedSessionSnapshot("phone", "main", 2, "somnium"),
    )
    attachments = build_client_attachments(
        [
            {
                "client_tty": "/dev/ttys001",
                "session_name": "main",
                "client_name": "local",
                "window_index": "0",
                "window_name": "somnium",
            },
            {
                "client_tty": "/dev/pts/4",
                "session_name": "phone",
                "client_name": "remote",
                "window_index": "2",
                "window_name": "somnium",
            },
        ],
        managed_sessions=managed,
    )

    assert attachments[0].attachment_class is AttachmentClass.LOCAL_LEADER
    assert attachments[1].attachment_class is AttachmentClass.REMOTE_GROUPED


def test_dry_run_emits_deterministic_action_order():
    workspace = _workspace(_pane("%1", "somnium:NW"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc12345", "somnium:NW"),),
    )
    grouped = (
        GroupedSessionSnapshot("main", "main", 0, "somnium"),
        GroupedSessionSnapshot("phone", "main", 2, "somnium"),
    )
    attachments = build_client_attachments(
        [
            {
                "client_tty": "/dev/ttys001",
                "session_name": "main",
                "client_name": "local",
                "window_index": "0",
                "window_name": "somnium",
            },
            {
                "client_tty": "/dev/pts/4",
                "session_name": "phone",
                "client_name": "remote",
                "window_index": "2",
                "window_name": "somnium",
            },
        ],
        managed_sessions=grouped,
    )
    plan = build_restart_plan(
        workspace,
        registry,
        client_attachments=attachments,
        grouped_sessions=grouped,
    )

    result = RestartExecutor().dry_run(plan)
    descriptions = [action.description for action in result.actions]

    assert descriptions == [
        "freeze workspace, grouped sessions, clients, and registry inputs",
        "park client /dev/ttys001 (local_leader)",
        "detach client /dev/pts/4 (remote_grouped)",
        "kill grouped session phone",
        "kill leader session main",
        "recreate workspace via builder.build_workspace",
        "normalize managed windows before restore",
        "clear transient stash windows",
        "resume abc12345 into %1 with resume",
        "recreate grouped session phone on somnium",
        "verify pane labels and resume outcomes",
    ]


class FakeBuilderAdapter:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.windows: dict[str, list[str]] = {}
        self.panes: dict[str, list[str]] = {}
        self.pane_options: dict[str, dict[str, str]] = {}
        self.window_options: dict[str, dict[str, str]] = {}

    def has_session(self, session_name: str) -> bool:
        return session_name in self.sessions

    def run(self, *args: str, allow_failure: bool = False) -> str:
        cmd = args[0]
        if cmd == "new-session":
            session = args[args.index("-s") + 1]
            window = args[args.index("-n") + 1]
            self.sessions.add(session)
            self.windows[session] = [window]
            self.panes[f"{session}:{window}"] = [f"{session}:{window}.1"]
            return ""
        if cmd == "new-window":
            session = args[args.index("-t") + 1]
            window = args[args.index("-n") + 1]
            self.windows.setdefault(session, []).append(window)
            self.panes[f"{session}:{window}"] = [f"{session}:{window}.1"]
            return ""
        if cmd == "display-message":
            target = args[args.index("-t") + 1]
            fmt = args[-1]
            if fmt == "#{window_width}":
                return "240\n"
            if fmt == "#{window_height}":
                return "60\n"
            if fmt == "#{pane_id}":
                return f"{target}\n"
            return "\n"
        if cmd == "split-window":
            target = args[args.index("-t") + 1]
            window_target = target.rsplit(".", 1)[0]
            pane_list = self.panes.setdefault(window_target, [f"{window_target}.1"])
            new_pane = f"{window_target}.{len(pane_list) + 1}"
            pane_list.append(new_pane)
            if "-P" in args:
                return f"{new_pane}\n"
            return ""
        if cmd == "set-option":
            option = args[-2]
            value = args[-1]
            target = args[args.index("-t") + 1]
            if "-p" in args:
                self.pane_options.setdefault(target, {})[option] = value
            elif "-w" in args:
                self.window_options.setdefault(target, {})[option] = value
            return ""
        if cmd in {"send-keys", "select-pane", "select-window"}:
            return ""
        raise AssertionError(f"unhandled tmux command in fake adapter: {args}")


def test_builder_creates_canonical_workspace_roles():
    adapter = FakeBuilderAdapter()

    build_workspace(adapter, "main")  # type: ignore[arg-type]

    assert adapter.windows["main"] == [
        "palace",
        "somnium",
        "legion",
        "mechanicus",
        "reservists",
    ]
    roles = {
        target: options.get("@PANE_ID")
        for target, options in adapter.pane_options.items()
        if "@PANE_ID" in options
    }
    assert {
        "palace:W",
        "palace:N",
        "palace:S",
        "palace:E",
    } <= set(roles.values())
    assert {
        "somnium:W",
        "somnium:N",
        "somnium:NE",
        "somnium:S",
        "somnium:SE",
    } <= set(roles.values())
    assert roles["main:legion.1"] == "legion:custodes"
    assert roles["main:mechanicus.1"] == "mechanicus:fabricator-general"
    assert roles["main:reservists.1"] == "reservists:civic"
    assert adapter.pane_options["main:legion.1"]["@PANE_TYPE"] == "legion"
    assert adapter.pane_options["main:mechanicus.1"]["@PANE_TYPE"] == "mechanicus"
    assert adapter.pane_options["main:reservists.1"]["@PANE_TYPE"] == "reservists"
    # The civic reservist pane carries the hook the civic-thread fallthrough resolves.
    assert adapter.pane_options["main:reservists.1"]["@CIVIC_RESERVIST"] == "1"
    pane_types = [options.get("@PANE_TYPE") for options in adapter.pane_options.values()]
    assert "tui" not in pane_types
