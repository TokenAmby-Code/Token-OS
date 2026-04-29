from __future__ import annotations

from .models import (
    PaneSnapshot,
    RestartExecutionResult,
    RestartPlan,
    WindowSnapshot,
    WorkspaceSnapshot,
)


CANONICAL_WINDOWS = {"palace", "somnium", "legion", "mechanicus", "tui"}


def render_workspace(snapshot: WorkspaceSnapshot) -> str:
    lines = [f"session {snapshot.session_name}"]
    for window in snapshot.windows:
        lines.extend(render_window_lines(window))
    return "\n".join(lines)


def render_window(snapshot: WindowSnapshot) -> str:
    return "\n".join(render_window_lines(snapshot))


def render_pane(snapshot: PaneSnapshot) -> str:
    role = snapshot.pane_role or "(unset)"
    active = " active" if snapshot.active else ""
    reserved = " reserved" if snapshot.reserved else ""
    return "\n".join(
        [
            f"pane {snapshot.pane_id}",
            f"  role: {role}",
            f"  window: {snapshot.session_name}:{snapshot.window_index} {snapshot.window_name}",
            f"  size: {snapshot.width}x{snapshot.height}",
            f"  state: grid={snapshot.grid_state.value} kind={snapshot.pane_kind.value}{active}{reserved}",
            f"  process: {snapshot.current_command}",
            f"  tty: {snapshot.tty}",
        ]
    )


def render_restart_plan(plan: RestartPlan) -> str:
    lines = [
        f"restart-plan session={plan.session_name} phase={plan.phase.value}",
        f"  resumes: {len(plan.resumes)}",
        f"  skipped: {len(plan.skipped)}",
        f"  clients: {len(plan.client_attachments)}",
    ]
    for issue in plan.coherence_issues:
        target = issue.pane_label or issue.pane_id or issue.instance_id or "workspace"
        lines.append(f"  ! {issue.severity.value} {issue.code} [{target}] {issue.message}")
    for attachment in plan.client_attachments:
        scope = attachment.attachment_class.value
        lines.append(
            f"  client {attachment.client_tty} session={attachment.session_name} {scope} window={attachment.selected_window_name or attachment.selected_window_index}"
        )
    for grouped in plan.grouped_sessions:
        if grouped.session_name != grouped.leader_session_name:
            lines.append(
                "  "
                f"grouped {grouped.session_name} leader={grouped.leader_session_name} "
                f"window={grouped.selected_window_name or grouped.selected_window_index}"
            )
    for resume in plan.resumes:
        target = resume.target_pane_id or "<post-rebuild>"
        lines.append(
            "  "
            f"resume {resume.instance_id[:8]} pane={resume.pane_label} "
            f"target={target} mode={resume.disposition.value}"
        )
    for resume in plan.skipped:
        lines.append(
            "  "
            f"skip {resume.instance_id[:8]} pane={resume.pane_label or '(unset)'} "
            f"reason={resume.reason}"
        )
    return "\n".join(lines)


def render_restart_result(result: RestartExecutionResult) -> str:
    lines = [
        f"restart session={result.session_name} phase={result.phase.value}",
        f"  resumes: attempted={result.resumes_attempted} succeeded={result.resumes_succeeded} failed={result.resumes_failed}",
        f"  clients: parked={result.clients_parked} detached={result.clients_detached} restored={result.clients_restored}",
        f"  grouped sessions recreated: {result.grouped_sessions_recreated}",
    ]
    for issue in result.coherence_issues:
        target = issue.pane_label or issue.pane_id or issue.instance_id or "workspace"
        lines.append(f"  ! {issue.severity.value} {issue.code} [{target}] {issue.message}")
    for violation in result.postcondition_violations:
        lines.append(f"  ! violation {violation}")
    for action in result.actions:
        lines.append(f"  {action.phase.value}: {action.description}")
    for resume in result.resume_results:
        outcome = "ok" if resume.success else "fail"
        lines.append(
            f"  resume[{outcome}] {resume.instance_id[:8]} pane={resume.pane_label} target={resume.target_pane_id or '<post-rebuild>'} {resume.message}"
        )
    return "\n".join(lines)


def render_doctor(snapshot: WorkspaceSnapshot) -> str:
    issues: list[str] = []
    window_bases = {window.window_name.split("(", 1)[0] for window in snapshot.windows}

    missing_windows = sorted(CANONICAL_WINDOWS - window_bases)
    if missing_windows:
        issues.append(f"missing canonical windows: {', '.join(missing_windows)}")

    for window in snapshot.windows:
        base = window.window_name.split("(", 1)[0]
        if base.startswith("_stash_") or base.startswith("_fstash_"):
            issues.append(f"orphan transient window: {window.window_name}")
        if window.grid_expanded != "none":
            issues.append(f"{window.target} has transient @GRID_EXPANDED={window.grid_expanded}")
        if window.grid_stash:
            issues.append(f"{window.target} has transient @GRID_STASH set")
        if window.side_expanded != "none":
            issues.append(f"{window.target} has transient @SIDE_EXPANDED={window.side_expanded}")
        for warning in window.warnings:
            issues.append(f"{window.target}: {warning}")

    lines = [f"doctor session={snapshot.session_name}"]
    if not issues:
        lines.append("  ok")
    else:
        for issue in issues:
            lines.append(f"  ! {issue}")
    return "\n".join(lines)


def render_window_lines(snapshot: WindowSnapshot) -> list[str]:
    lines = [
        (
            f"- {snapshot.target} {snapshot.window_name} "
            f"[{snapshot.archetype.value}] "
            f"focused={'true' if snapshot.focused else 'false'} "
            f"grid={snapshot.grid_expanded} "
            f"stash={'set' if snapshot.grid_stash else 'none'} "
            f"side={snapshot.side_expanded}"
        )
    ]
    for warning in snapshot.warnings:
        lines.append(f"    ! {warning}")
    for pane in snapshot.panes:
        role = pane.pane_role or "(unset)"
        flags: list[str] = []
        if pane.active:
            flags.append("active")
        if pane.reserved:
            flags.append("reserved")
        suffix = f" [{' '.join(flags)}]" if flags else ""
        lines.append(
            "    "
            f"{pane.pane_id} {role} "
            f"{pane.width}x{pane.height} "
            f"grid={pane.grid_state.value} kind={pane.pane_kind.value} "
            f"cmd={pane.current_command}{suffix}"
        )
    return lines
