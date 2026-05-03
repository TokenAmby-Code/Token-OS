from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from .enums import CoherenceSeverity, InstanceStatus, RestartPhase, ResumeDisposition
from .labels import (
    PALACE_GRID_ROLES,
    PALACE_SIDE_ROLES,
    SOMNIUM_GRID_ROLES,
    SOMNIUM_SIDE_ROLES,
    canonical_pane_role,
)
from .models import (
    CoherenceIssue,
    InstanceRegistryEntry,
    InstanceRegistrySnapshot,
    PlannedResume,
    RestartPlan,
    WorkspaceSnapshot,
)


def build_restart_plan(
    workspace: WorkspaceSnapshot,
    registry: InstanceRegistrySnapshot,
    *,
    client_attachments: tuple = (),
    grouped_sessions: tuple = (),
) -> RestartPlan:
    """Plan restart restoration from typed registry state plus live workspace.

    This is intentionally pure planning logic. It does not talk to Token-API or
    mutate tmux. The caller is expected to supply a live workspace snapshot and
    a registry snapshot that already reflects the current device's instance view.
    """

    pane_by_label = {
        canonical_pane_role(pane.pane_role): pane
        for pane in workspace.iter_panes()
        if pane.pane_role
    }
    legal_labels = _legal_restart_labels(workspace)
    issues: list[CoherenceIssue] = []
    resumes: list[PlannedResume] = []
    skipped: list[PlannedResume] = []

    candidates = _dedupe_candidates(_candidate_instances(registry.instances))
    active_labels = [
        canonical_pane_role(inst.pane_label) for inst in candidates if _is_resumable(inst)
    ]
    for pane_label, count in Counter(active_labels).items():
        if pane_label and count > 1:
            issues.append(
                CoherenceIssue(
                    severity=CoherenceSeverity.ERROR,
                    code="duplicate_pane_label",
                    message=f"multiple resumable instances claim {pane_label}",
                    pane_label=pane_label,
                )
            )

    duplicate_claims = Counter(
        canonical_pane_role(inst.pane_label)
        for inst in registry.instances
        if inst.pane_label and _is_candidate(inst)
    )

    for pane_label, count in duplicate_claims.items():
        if count > 1:
            issues.append(
                CoherenceIssue(
                    severity=CoherenceSeverity.ERROR,
                    code="duplicate_pane_label",
                    message=f"multiple resumable instances claim {pane_label}",
                    pane_label=pane_label,
                )
            )

    for inst in candidates:
        pane_label = canonical_pane_role(inst.pane_label)
        target_pane = pane_by_label.get(pane_label)
        if target_pane is None:
            if pane_label in legal_labels:
                disposition = _resume_disposition(inst)
                planned = PlannedResume(
                    instance_id=inst.instance_id,
                    pane_label=pane_label,
                    target_pane_id="",
                    working_dir=inst.working_dir,
                    disposition=disposition,
                    reason="target pane hidden in current topology; resolve after rebuild",
                    target_hidden_until_rebuild=True,
                )
                if disposition is ResumeDisposition.SKIP:
                    skipped.append(planned)
                else:
                    resumes.append(planned)
            else:
                issues.append(
                    CoherenceIssue(
                        severity=CoherenceSeverity.WARNING,
                        code="missing_target_pane",
                        message=f"registry pane label {pane_label} has no live managed pane",
                        instance_id=inst.instance_id,
                        pane_label=pane_label,
                    )
                )
                skipped.append(
                    PlannedResume(
                        instance_id=inst.instance_id,
                        pane_label=pane_label,
                        target_pane_id="",
                        working_dir=inst.working_dir,
                        disposition=ResumeDisposition.SKIP,
                        reason="target pane missing from workspace snapshot",
                    )
                )
            continue

        if inst.tmux_pane and inst.tmux_pane != target_pane.pane_id:
            issues.append(
                CoherenceIssue(
                    severity=CoherenceSeverity.WARNING,
                    code="pane_id_mismatch",
                    message=(
                        f"registry tmux pane {inst.tmux_pane} differs from current "
                        f"pane {target_pane.pane_id} for {pane_label}"
                    ),
                    instance_id=inst.instance_id,
                    pane_label=pane_label,
                    pane_id=target_pane.pane_id,
                )
            )

        if "claude" in target_pane.current_command:
            issues.append(
                CoherenceIssue(
                    severity=CoherenceSeverity.ERROR,
                    code="target_busy",
                    message=f"target pane {target_pane.pane_id} already appears to be running claude",
                    instance_id=inst.instance_id,
                    pane_label=pane_label,
                    pane_id=target_pane.pane_id,
                )
            )

        disposition = _resume_disposition(inst)
        planned = PlannedResume(
            instance_id=inst.instance_id,
            pane_label=pane_label,
            target_pane_id=target_pane.pane_id,
            working_dir=inst.working_dir,
            disposition=disposition,
            reason=_resume_reason(inst, disposition),
            target_hidden_until_rebuild=False,
        )
        if disposition is ResumeDisposition.SKIP:
            skipped.append(planned)
        else:
            resumes.append(planned)

    return RestartPlan(
        session_name=workspace.session_name,
        phase=RestartPhase.COHERENCE_CHECK,
        resumes=tuple(resumes),
        skipped=tuple(skipped),
        client_attachments=tuple(client_attachments),
        grouped_sessions=tuple(grouped_sessions),
        coherence_issues=tuple(issues),
    )


def _is_resumable(instance: InstanceRegistryEntry) -> bool:
    return instance.status in {
        InstanceStatus.IDLE,
        InstanceStatus.PROCESSING,
        InstanceStatus.STOPPED,
    }


def _is_candidate(instance: InstanceRegistryEntry) -> bool:
    return bool(instance.pane_label) and not instance.is_subagent and _is_resumable(instance)


def _parse_dt(raw: str) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _instance_sort_key(instance: InstanceRegistryEntry) -> float:
    last_activity = _parse_dt(instance.last_activity)
    stopped_at = _parse_dt(instance.stopped_at)
    timestamps = []
    for value in (last_activity, stopped_at):
        if value is not None:
            timestamps.append(value.timestamp())
    return max(timestamps, default=0.0)


def _recently_active(instance: InstanceRegistryEntry) -> bool:
    if instance.status is InstanceStatus.STOPPED:
        return False
    activity = _parse_dt(instance.last_activity)
    if activity is None:
        return True
    now = datetime.now(activity.tzinfo or UTC)
    if activity.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return activity >= now - timedelta(hours=24)


def _recently_stopped(instance: InstanceRegistryEntry) -> bool:
    if instance.status is not InstanceStatus.STOPPED:
        return False
    stopped = _parse_dt(instance.stopped_at)
    if stopped is None:
        return False
    now = datetime.now(stopped.tzinfo or UTC)
    if stopped.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    return stopped >= now - timedelta(seconds=60)


def _candidate_instances(
    instances: tuple[InstanceRegistryEntry, ...],
) -> list[InstanceRegistryEntry]:
    candidates: list[InstanceRegistryEntry] = []
    for instance in instances:
        if not _is_candidate(instance):
            continue
        if instance.status is InstanceStatus.STOPPED and not _recently_stopped(instance):
            continue
        if instance.status is not InstanceStatus.STOPPED and not _recently_active(instance):
            continue
        candidates.append(instance)
    return candidates


def _dedupe_candidates(
    instances: list[InstanceRegistryEntry],
) -> list[InstanceRegistryEntry]:
    by_label: dict[str, InstanceRegistryEntry] = {}
    for instance in sorted(instances, key=_instance_sort_key, reverse=True):
        by_label.setdefault(canonical_pane_role(instance.pane_label), instance)
    return list(by_label.values())


def _legal_restart_labels(workspace: WorkspaceSnapshot) -> set[str]:
    legal: set[str] = set()
    for window in workspace.windows:
        if window.archetype.value == "palace":
            legal.update(PALACE_SIDE_ROLES)
            legal.update(PALACE_GRID_ROLES)
        elif window.archetype.value == "somnium":
            legal.update(SOMNIUM_GRID_ROLES)
            legal.update(SOMNIUM_SIDE_ROLES)
        elif window.archetype.value in {"legion_stack", "mechanicus_stack", "tui_single"}:
            legal.update({pane.pane_role for pane in window.panes if pane.pane_role})
    return legal


def _resume_disposition(instance: InstanceRegistryEntry) -> ResumeDisposition:
    if not _is_resumable(instance):
        return ResumeDisposition.SKIP
    if instance.was_processing:
        return ResumeDisposition.RESUME_AND_CONTINUE
    return ResumeDisposition.RESUME


def _resume_reason(instance: InstanceRegistryEntry, disposition: ResumeDisposition) -> str:
    if disposition is ResumeDisposition.RESUME_AND_CONTINUE:
        return "instance was processing before restart"
    if disposition is ResumeDisposition.RESUME:
        return "instance has resumable registry state"
    return f"instance status {instance.status.value} is not resumable"
