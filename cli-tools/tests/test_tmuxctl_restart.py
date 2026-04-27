from __future__ import annotations

import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.api import build_client_attachments
from tmuxctl.enums import (
    AttachmentClass,
    CoherenceSeverity,
    GridState,
    InstanceStatus,
    LayoutOrigin,
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


def _pane(pane_id: str, role: str, *, command: str = "zsh") -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        window_name="palace",
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


def _workspace(*panes: PaneSnapshot) -> WorkspaceSnapshot:
    window = WindowSnapshot(
        session_name="main",
        window_index=1,
        window_name="palace",
        archetype=WindowArchetype.PALACE,
        layout_origin=LayoutOrigin.WSL,
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
        panes=tuple(panes),
    )
    return WorkspaceSnapshot(session_name="main", windows=(window,))


def _instance(
    instance_id: str,
    pane_label: str,
    *,
    status: InstanceStatus = InstanceStatus.IDLE,
    pre_stop_status: InstanceStatus = InstanceStatus.IDLE,
    last_activity: str = "2026-04-25T16:00:00+00:00",
    stopped_at: str = "",
    tmux_pane: str = "%1",
    is_subagent: bool = False,
) -> InstanceRegistryEntry:
    return InstanceRegistryEntry(
        instance_id=instance_id,
        device_id="Mac-Mini",
        pane_label=pane_label,
        tmux_pane=tmux_pane,
        working_dir="/mnt/imperium/Imperium-ENV",
        status=status,
        pre_stop_status=pre_stop_status,
        is_subagent=is_subagent,
        last_activity=last_activity,
        stopped_at=stopped_at,
    )


def _iso_ago(*, hours: int = 0, seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours, seconds=seconds)).isoformat()


def test_restart_plan_dedupes_by_pane_label_and_keeps_newest():
    workspace = _workspace(_pane("%1", "palace:TL"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
            instances=(
            _instance("old", "palace:TL", last_activity=_iso_ago(hours=30)),
            _instance("new", "palace:TL", last_activity=_iso_ago(hours=1)),
            ),
        )

    plan = build_restart_plan(workspace, registry)

    assert [resume.instance_id for resume in plan.resumes] == ["new"]
    assert any(issue.code == "duplicate_pane_label" for issue in plan.coherence_issues)


def test_restart_plan_includes_recent_stop_and_excludes_stale_activity():
    workspace = _workspace(_pane("%1", "palace:TL"), _pane("%2", "palace:TR"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
            instances=(
            _instance("stale", "palace:TL", last_activity=_iso_ago(hours=72)),
            _instance(
                "recent-stop",
                "palace:TR",
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
    workspace = _workspace(_pane("%2", "palace:TL", command="claude"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "palace:TL", tmux_pane="%1"),),
    )

    plan = build_restart_plan(workspace, registry)

    codes = {issue.code: issue.severity for issue in plan.coherence_issues}
    assert codes["pane_id_mismatch"] is CoherenceSeverity.WARNING
    assert codes["target_busy"] is CoherenceSeverity.ERROR


def test_hidden_but_legal_palace_side_is_planned_for_post_rebuild_resolution():
    workspace = _workspace(
        _pane("%1", "palace:TL"),
        _pane("%2", "palace:TR"),
        _pane("%3", "palace:BL"),
        _pane("%4", "palace:BR"),
    )
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc", "palace:SL", tmux_pane="%99"),),
    )

    plan = build_restart_plan(workspace, registry)

    assert len(plan.resumes) == 1
    assert plan.resumes[0].target_hidden_until_rebuild is True
    assert not any(issue.code == "missing_target_pane" for issue in plan.coherence_issues)


def test_build_client_attachments_classifies_local_remote_and_grouped():
    managed = (
        GroupedSessionSnapshot("main", "main", 0, "palace"),
        GroupedSessionSnapshot("phone", "main", 2, "warp"),
    )
    attachments = build_client_attachments(
        [
            {
                "client_tty": "/dev/ttys001",
                "session_name": "main",
                "client_name": "local",
                "window_index": "0",
                "window_name": "palace",
            },
            {
                "client_tty": "/dev/pts/4",
                "session_name": "phone",
                "client_name": "remote",
                "window_index": "2",
                "window_name": "warp",
            },
        ],
        managed_sessions=managed,
    )

    assert attachments[0].attachment_class is AttachmentClass.LOCAL_LEADER
    assert attachments[1].attachment_class is AttachmentClass.REMOTE_GROUPED


def test_dry_run_emits_deterministic_action_order():
    workspace = _workspace(_pane("%1", "palace:TL"))
    registry = InstanceRegistrySnapshot(
        device_id="Mac-Mini",
        instances=(_instance("abc12345", "palace:TL"),),
    )
    grouped = (
        GroupedSessionSnapshot("main", "main", 0, "palace"),
        GroupedSessionSnapshot("phone", "main", 2, "warp"),
    )
    attachments = build_client_attachments(
        [
            {
                "client_tty": "/dev/ttys001",
                "session_name": "main",
                "client_name": "local",
                "window_index": "0",
                "window_name": "palace",
            },
            {
                "client_tty": "/dev/pts/4",
                "session_name": "phone",
                "client_name": "remote",
                "window_index": "2",
                "window_name": "warp",
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
        "recreate workspace via tmux-workspace",
        "normalize managed windows before restore",
        "resume abc12345 into %1 with resume",
        "recreate grouped session phone on warp",
        "verify pane labels and resume outcomes",
    ]
