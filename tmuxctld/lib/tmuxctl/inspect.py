from __future__ import annotations

from .models import (
    PaneSnapshot,
    RestartExecutionResult,
    RestartPlan,
    WindowSnapshot,
    WorkspaceSnapshot,
)
from .revert import is_transient_window_name

CANONICAL_WINDOWS = {"palace", "somnium", "council", "mechanicus", "reservists"}
# Stack windows may spill into sibling windows suffixed `-N` (e.g. mechanicus-2).
# These match a canonical base and should not flag as missing or unknown.
STACK_BASES = {"mechanicus", "mars", "kreig", "reservists"}


def _canonical_base(window_name: str) -> str:
    """Strip any `-N` spill suffix; return canonical base name."""
    base = window_name.split("(", 1)[0]
    head, sep, tail = base.rpartition("-")
    if sep and tail.isdigit() and head in STACK_BASES:
        return head
    return base


def render_workspace(snapshot: WorkspaceSnapshot, *, physical: bool = False) -> str:
    lines = [f"session {snapshot.session_name}"]
    for window in snapshot.windows:
        lines.extend(render_window_lines(window, physical=physical))
    return "\n".join(lines)


def render_window(snapshot: WindowSnapshot, *, physical: bool = False) -> str:
    return "\n".join(render_window_lines(snapshot, physical=physical))


def render_pane(snapshot: PaneSnapshot, *, physical: bool = False) -> str:
    # Canonical id (the @PANE_ID role) is the sole external identity; the raw
    # physical %NN is volatile and gated behind --physical.
    role = snapshot.pane_role or "(unset)"
    active = " active" if snapshot.active else ""
    reserved = " reserved" if snapshot.reserved else ""
    header = f"pane {role}"
    lines = [header, f"  role: {role}"]
    if physical:
        lines.append(f"  physical: {snapshot.pane_id}")
    lines.extend(
        [
            f"  window: {snapshot.session_name}:{snapshot.window_index} {snapshot.window_name}",
            f"  size: {snapshot.width}x{snapshot.height}",
            f"  state: grid={snapshot.grid_state.value} kind={snapshot.pane_kind.value}{active}{reserved}",
            f"  tombstone: source={snapshot.tombstone_source or '(unset)'} target={snapshot.tombstone_target or '(unset)'}",
            f"  process: {snapshot.current_command}",
            f"  tty: {snapshot.tty}",
        ]
    )
    return "\n".join(lines)


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
        target = resume.pane_label or resume.target_pane_id or "<unresolved>"
        tombstone = f" tombstone={resume.tombstone_role}" if resume.tombstone_role else ""
        lines.append(
            "  "
            f"resume {resume.instance_id[:8]} pane={resume.pane_label} "
            f"target={target} mode={resume.disposition.value}{tombstone}"
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
    window_bases = {_canonical_base(window.window_name) for window in snapshot.windows}
    pane_ids = {pane.pane_id for pane in snapshot.iter_panes()}
    pane_roles = {pane.pane_role for pane in snapshot.iter_panes() if pane.pane_role}

    referenced_focus_stash = {
        f"_focus_stash_{window.window_name.split('(', 1)[0]}"
        for window in snapshot.windows
        if window.grid_focus_active and window.grid_focus_stash
    }

    missing_windows = sorted(CANONICAL_WINDOWS - window_bases)
    if missing_windows:
        issues.append(f"missing canonical windows: {', '.join(missing_windows)}")

    for window in snapshot.windows:
        raw_base = window.window_name.split("(", 1)[0]
        if is_transient_window_name(window.window_name) and raw_base not in referenced_focus_stash:
            issues.append(f"orphan transient window: {window.window_name}")
        if raw_base.startswith("_focus_stash_"):
            if not any(pane.pane_role for pane in window.panes):
                issues.append(f"empty/broken focus stash window: {window.window_name}")
        if window.grid_expanded != "none":
            issues.append(f"{window.target} has transient @GRID_EXPANDED={window.grid_expanded}")
        if window.grid_stash:
            issues.append(f"{window.target} has transient @GRID_STASH set")
        if window.side_expanded != "none":
            issues.append(f"{window.target} has transient @SIDE_EXPANDED={window.side_expanded}")
        if window.grid_focus_active:
            if not window.grid_focus_pane or window.grid_focus_pane not in pane_ids:
                issues.append(
                    f"{window.target} has broken grid focus pane: {window.grid_focus_pane or '(unset)'}"
                )
            stash_ids = [
                entry.split(":", 1)[0] for entry in window.grid_focus_stash.split(",") if entry
            ]
            missing = [pane_id for pane_id in stash_ids if pane_id not in pane_ids]
            if missing:
                issues.append(
                    f"{window.target} has missing grid focus stash panes: {', '.join(missing)}"
                )
            expected_stash_size = 0
            if window.archetype.value == "palace":
                expected_stash_size = 1
            elif window.archetype.value == "somnium":
                expected_stash_size = 3
            if expected_stash_size and len(stash_ids) not in {0, expected_stash_size}:
                issues.append(
                    f"{window.target} has invalid grid focus stash size: {len(stash_ids)}"
                )
        elif window.grid_focus_stash:
            issues.append(f"{window.target} has stale @FOCUS_GRID_STASH set")
        if window.side_focus_active and window.side_focus_pane not in pane_ids:
            issues.append(
                f"{window.target} has broken side focus pane: {window.side_focus_pane or '(unset)'}"
            )
        for warning in window.warnings:
            issues.append(f"{window.target}: {warning}")
        for pane in window.panes:
            if pane.pane_kind.value == "tombstone" and not pane.tombstone_target:
                issues.append(f"{pane.pane_id} tombstone missing @TOMBSTONE_TARGET")
            elif pane.pane_kind.value == "tombstone":
                target_exists = (
                    pane.tombstone_target in pane_ids or pane.tombstone_target in pane_roles
                )
                if not target_exists:
                    issues.append(
                        f"{pane.pane_id} tombstone target missing: {pane.tombstone_target}"
                    )

    lines = [f"doctor session={snapshot.session_name}"]
    if not issues:
        lines.append("  ok")
    else:
        for issue in issues:
            lines.append(f"  ! {issue}")
    return "\n".join(lines)


def render_window_lines(snapshot: WindowSnapshot, *, physical: bool = False) -> list[str]:
    lines = [
        (
            f"- {snapshot.target} {snapshot.window_name} "
            f"[{snapshot.archetype.value}] "
            f"focused={'true' if snapshot.focused else 'false'} "
            f"grid={snapshot.grid_expanded} "
            f"stash={'set' if snapshot.grid_stash else 'none'} "
            f"side={snapshot.side_expanded} "
            f"focus_grid={snapshot.grid_focus_pane if snapshot.grid_focus_active else 'none'} "
            f"focus_side={snapshot.side_focus_pane if snapshot.side_focus_active else 'none'}"
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
        tombstone = ""
        if pane.pane_kind.value == "tombstone":
            tombstone = f" -> {pane.tombstone_target or '?'}"
        # Canonical role is the default identity; the raw physical %NN is gated
        # behind --physical so the normal path never leaks volatile tmux ids.
        physical_prefix = f"{pane.pane_id} " if physical else ""
        lines.append(
            "    "
            f"{physical_prefix}{role} "
            f"{pane.width}x{pane.height} "
            f"grid={pane.grid_state.value} kind={pane.pane_kind.value} "
            f"cmd={pane.current_command}{tombstone}{suffix}"
        )
    return lines
