from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.enums import (
    AttachmentClass,
    CoherenceSeverity,
    GridState,
    PaneKind,
    RestartPhase,
    ResumeDisposition,
    WindowArchetype,
)
from tmuxctl.inspect import render_doctor, render_restart_plan, render_restart_result
from tmuxctl.models import (
    ClientAttachment,
    CoherenceIssue,
    GroupedSessionSnapshot,
    PaneSnapshot,
    PlannedResume,
    RestartAction,
    RestartExecutionResult,
    RestartPlan,
    ResumeResult,
    WindowSnapshot,
    WorkspaceSnapshot,
)


def _pane(
    pane_id: str,
    role: str | None,
    *,
    kind: PaneKind = PaneKind.UNKNOWN,
    target: str = "",
    window: str = "palace",
) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        window_name=window,
        pane_index=0,
        width=80,
        height=24,
        current_command="zsh",
        tty="/dev/ttys001",
        pane_role=role,
        grid_state=GridState.UNKNOWN,
        pane_kind=kind,
        reserved=False,
        active=False,
        tombstone_target=target,
    )


def _window(
    name: str,
    panes: tuple[PaneSnapshot, ...] = (),
    *,
    index: int = 1,
    archetype: WindowArchetype = WindowArchetype.PALACE,
    grid_expanded: str = "none",
    grid_stash: str = "",
    side_expanded: str = "none",
    grid_focus_active: bool = False,
    grid_focus_pane: str = "",
    grid_focus_stash: str = "",
    side_focus_active: bool = False,
    side_focus_pane: str = "",
    warnings: tuple[str, ...] = (),
) -> WindowSnapshot:
    return WindowSnapshot(
        session_name="main",
        window_index=index,
        window_name=name,
        archetype=archetype,
        focused=False,
        grid_expanded=grid_expanded,
        grid_stash=grid_stash,
        side_expanded=side_expanded,
        grid_focus_active=grid_focus_active,
        grid_focus_pane=grid_focus_pane,
        grid_focus_stash=grid_focus_stash,
        side_focus_active=side_focus_active,
        side_focus_pane=side_focus_pane,
        panes=panes,
        warnings=warnings,
    )


def test_render_doctor_reports_workspace_transient_focus_and_tombstone_warnings() -> None:
    snapshot = WorkspaceSnapshot(
        session_name="main",
        windows=(
            _window(
                "palace",
                (
                    _pane("%1", "palace:N"),
                    _pane("%2", "palace:S", kind=PaneKind.TOMBSTONE),
                    _pane("%3", "palace:E", kind=PaneKind.TOMBSTONE, target="palace:Z"),
                ),
                grid_expanded="%1",
                grid_stash="%2:10x10",
                side_expanded="%4",
                grid_focus_active=True,
                grid_focus_pane="%404",
                grid_focus_stash="%missing:palace:NE:NE,%gone:palace:SE:SE",
                side_focus_active=True,
                side_focus_pane="%405",
                warnings=("ratio drift",),
            ),
            _window("_stash_orphan", (_pane("%10", None, window="_stash_orphan"),), index=8),
            _window("_focus_stash_broken", (), index=9),
            _window(
                "mechanicus-2",
                (_pane("%20", "mechanicus:1", window="mechanicus-2"),),
                index=3,
                archetype=WindowArchetype.MECHANICUS_STACK,
                grid_focus_stash="%stale:mechanicus:1:1",
            ),
        ),
    )

    out = render_doctor(snapshot)

    expected = [
        "doctor session=main",
        "missing canonical windows: council, reservists, somnium",
        "main:1 has transient @GRID_EXPANDED=%1",
        "main:1 has transient @GRID_STASH set",
        "main:1 has transient @SIDE_EXPANDED=%4",
        "main:1 has broken grid focus pane: %404",
        "main:1 has missing grid focus stash panes: %missing, %gone",
        "main:1 has invalid grid focus stash size: 2",
        "main:1 has broken side focus pane: %405",
        "main:1: ratio drift",
        "%2 tombstone missing @TOMBSTONE_TARGET",
        "%3 tombstone target missing: palace:Z",
        "orphan transient window: _stash_orphan",
        "orphan transient window: _focus_stash_broken",
        "empty/broken focus stash window: _focus_stash_broken",
        "main:3 has stale @FOCUS_GRID_STASH set",
    ]
    for line in expected:
        assert line in out


def test_render_doctor_ok_when_no_issues_and_stack_suffix_counts_as_canonical() -> None:
    snapshot = WorkspaceSnapshot(
        session_name="main",
        windows=(
            _window("palace", (_pane("%1", "palace:N"),), index=1),
            _window(
                "somnium", (_pane("%2", "somnium:N"),), index=2, archetype=WindowArchetype.SOMNIUM
            ),
            _window(
                "council",
                (_pane("%3", "council:custodes"),),
                index=3,
                archetype=WindowArchetype.COUNCIL,
            ),
            _window(
                "mechanicus-2",
                (_pane("%4", "mechanicus:1"),),
                index=4,
                archetype=WindowArchetype.MECHANICUS_STACK,
            ),
            _window(
                "reservists",
                (_pane("%5", "reservists:civic"),),
                index=5,
                archetype=WindowArchetype.MECHANICUS_STACK,
            ),
        ),
    )

    assert render_doctor(snapshot) == "doctor session=main\n  ok"


def test_render_restart_plan_exposes_operator_facing_details() -> None:
    plan = RestartPlan(
        session_name="main",
        phase=RestartPhase.COHERENCE_CHECK,
        resumes=(
            PlannedResume(
                instance_id="abcdef123456",
                pane_label="council:custodes",
                target_pane_id="%41",
                working_dir="/tmp/work",
                disposition=ResumeDisposition.RESUME_AND_CONTINUE,
                reason="live",
                tombstone_role="legion:custodes",
            ),
        ),
        skipped=(
            PlannedResume(
                instance_id="skipme123456",
                pane_label="",
                target_pane_id="",
                working_dir="/tmp/work",
                disposition=ResumeDisposition.SKIP,
                reason="stale",
            ),
        ),
        client_attachments=(
            ClientAttachment(
                client_tty="/dev/ttys010",
                session_name="main",
                is_remote=False,
                selected_window_index=4,
                selected_window_name="council",
                attachment_class=AttachmentClass.LOCAL_LEADER,
            ),
        ),
        grouped_sessions=(GroupedSessionSnapshot("aux", "main", 2, "somnium"),),
        coherence_issues=(
            CoherenceIssue(
                severity=CoherenceSeverity.WARNING,
                code="target_busy",
                message="pane busy",
                pane_label="council:custodes",
            ),
        ),
    )

    out = render_restart_plan(plan)

    assert "restart-plan session=main phase=coherence_check" in out
    assert "resumes: 1" in out and "skipped: 1" in out and "clients: 1" in out
    assert "! warning target_busy [council:custodes] pane busy" in out
    assert "client /dev/ttys010 session=main local_leader window=council" in out
    assert "grouped aux leader=main window=somnium" in out
    assert (
        "resume abcdef12 pane=council:custodes target=council:custodes mode=resume_and_continue tombstone=legion:custodes"
        in out
    )
    assert "skip skipme12 pane=(unset) reason=stale" in out


def test_render_restart_result_exposes_actions_resume_outcomes_and_violations() -> None:
    plan = RestartPlan(session_name="main", phase=RestartPhase.COHERENCE_CHECK)
    result = RestartExecutionResult(
        session_name="main",
        phase=RestartPhase.VERIFY,
        plan=plan,
        actions=(RestartAction(RestartPhase.REBUILD, "rebuilt main"),),
        resume_results=(
            ResumeResult(
                instance_id="abcdef123456",
                pane_label="council:custodes",
                target_pane_id="%41",
                disposition=ResumeDisposition.RESUME,
                success=True,
                message="resumed",
            ),
            ResumeResult(
                instance_id="deadbeef1234",
                pane_label="mechanicus:1",
                target_pane_id="",
                disposition=ResumeDisposition.RESUME,
                success=False,
                message="missing target",
            ),
        ),
        coherence_issues=(
            CoherenceIssue(CoherenceSeverity.ERROR, "postcheck", "failed", pane_id="%99"),
        ),
        postcondition_violations=("persona missing",),
        clients_parked=2,
        clients_detached=1,
        clients_restored=1,
        grouped_sessions_recreated=3,
    )

    out = render_restart_result(result)

    assert "restart session=main phase=verify" in out
    assert "resumes: attempted=2 succeeded=1 failed=1" in out
    assert "clients: parked=2 detached=1 restored=1" in out
    assert "grouped sessions recreated: 3" in out
    assert "! error postcheck [%99] failed" in out
    assert "! violation persona missing" in out
    assert "rebuild: rebuilt main" in out
    assert "resume[ok] abcdef12 pane=council:custodes target=%41 resumed" in out
    assert "resume[fail] deadbeef pane=mechanicus:1 target=<post-rebuild> missing target" in out
