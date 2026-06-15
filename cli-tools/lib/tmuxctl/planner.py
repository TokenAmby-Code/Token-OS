from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .enums import CoherenceSeverity, InstanceStatus, RestartPhase, ResumeDisposition
from .labels import (
    PALACE_ROLES,
    SOMNIUM_ROLES,
    canonical_pane_role,
)
from .models import (
    CoherenceIssue,
    InstanceRegistryEntry,
    InstanceRegistrySnapshot,
    PaneSnapshot,
    PlannedResume,
    RestartPlan,
    WorkspaceSnapshot,
)

UTC = timezone.utc  # noqa: UP017 - keep Python 3.9/3.10 compatibility for direct CLI use


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

    pane_by_label: dict[str, PaneSnapshot] = {}
    for pane in workspace.iter_panes():
        if pane.pane_role:
            pane_by_label.setdefault(canonical_pane_role(pane.pane_role), pane)
    legal_labels = _legal_restart_labels(workspace)
    issues: list[CoherenceIssue] = []
    resumes: list[PlannedResume] = []
    skipped: list[PlannedResume] = []

    # Restart restore is sourced from the pre-teardown tmux snapshot. A registry
    # row alone is never intent to resume: panes closed before `tx restart` have
    # no live @INSTANCE_ID stamp and therefore do not enter this candidate set.
    # The DB is metadata only (cwd/engine/rank/status/session), with synthetic
    # live candidates covering the case where registration readback failed but
    # the pane still carries runtime identity.
    candidate_instances = _live_snapshot_candidates(workspace, registry)
    candidates = _dedupe_candidates(candidate_instances)
    active_labels = [
        canonical_pane_role(inst.pane_label) for inst in candidates if _is_resumable(inst)
    ]
    for pane_label, count in Counter(active_labels).items():
        if pane_label and count > 1:
            issues.append(
                CoherenceIssue(
                    severity=CoherenceSeverity.WARNING,
                    code="duplicate_pane_label",
                    message=f"multiple resumable instances collapse to {pane_label}; newest candidate wins",
                    pane_label=pane_label,
                )
            )

    duplicate_claims = Counter(
        canonical_pane_role(inst.pane_label) for inst in candidate_instances if inst.pane_label
    )

    for pane_label, count in duplicate_claims.items():
        if count > 1:
            issues.append(
                CoherenceIssue(
                    severity=CoherenceSeverity.WARNING,
                    code="duplicate_pane_label",
                    message=f"multiple registry instances collapse to {pane_label}; stale losers skipped",
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
                    tombstone_role=_orchestrator_tombstone_role(inst, pane_label),
                    engine=inst.engine,
                    source="live_tmux",
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
                        tombstone_role=_orchestrator_tombstone_role(inst, pane_label),
                        engine=inst.engine,
                        source="live_tmux",
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

        current_command = target_pane.current_command.lower()
        if any(agent in current_command for agent in ("claude", "codex", "node")):
            # A full restart tears the managed session down before restore, so a
            # target pane already running an agent is the normal precondition for
            # resumable work, not a reason to abort the restart. Keep it visible
            # in the plan as a warning for diagnostics, but do not set
            # plan.has_errors. Otherwise `tx restart` wedges exactly when active
            # agents exist.
            issues.append(
                CoherenceIssue(
                    severity=CoherenceSeverity.WARNING,
                    code="target_busy",
                    message=f"target pane {target_pane.pane_id} already appears to be running an agent",
                    instance_id=inst.instance_id,
                    pane_label=pane_label,
                    pane_id=target_pane.pane_id,
                )
            )

        disposition = _resume_disposition(inst)
        planned = PlannedResume(
            instance_id=inst.instance_id,
            pane_label=pane_label,
            # Deliberately do not snapshot the volatile tmux %pane id. Restart
            # execution targets the durable public pane label and lets tmuxctl
            # resolve it live after rebuild.
            target_pane_id="",
            working_dir=inst.working_dir,
            disposition=disposition,
            reason=_resume_reason(inst, disposition),
            target_hidden_until_rebuild=False,
            tombstone_role=_orchestrator_tombstone_role(inst, pane_label),
            engine=inst.engine,
            source="live_tmux",
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


def _live_snapshot_candidates(
    workspace: WorkspaceSnapshot, registry: InstanceRegistrySnapshot
) -> list[InstanceRegistryEntry]:
    registry_by_id = {inst.instance_id: inst for inst in registry.instances if inst.instance_id}
    live: list[InstanceRegistryEntry] = []
    now = datetime.now(UTC).isoformat()
    for pane in workspace.iter_panes():
        pane_label = canonical_pane_role(pane.pane_role)
        if not pane_label or not pane.instance_id:
            continue
        base = registry_by_id.get(pane.instance_id)
        engine = _pane_engine(pane, base)
        working_dir = base.working_dir if base and base.working_dir else pane.cwd
        if base is None:
            base = InstanceRegistryEntry(
                instance_id=pane.instance_id,
                device_id=registry.device_id,
                pane_label=pane_label,
                tmux_pane=pane.pane_id,
                working_dir=working_dir,
                status=InstanceStatus.PROCESSING,
                pre_stop_status=InstanceStatus.UNKNOWN,
                last_activity=now,
                engine=engine,
            )
        else:
            base = replace(
                base,
                pane_label=pane_label,
                tmux_pane=pane.pane_id,
                working_dir=working_dir,
                engine=engine or base.engine,
            )
        if _is_candidate(base):
            live.append(base)
    return live


def _pane_engine(pane: PaneSnapshot, instance: InstanceRegistryEntry | None = None) -> str:
    for value in (pane.runtime_engine, instance.engine if instance else "", pane.current_command):
        text = (value or "").strip().lower()
        if "codex" in text:
            return "codex"
        if "claude" in text or "node" in text:
            return "claude"
    return ""


def _backfill_pane_labels_from_stamps(
    instances: tuple[InstanceRegistryEntry, ...],
    workspace: WorkspaceSnapshot,
) -> tuple[InstanceRegistryEntry, ...]:
    """Recover each instance's pane_label from the live ``@INSTANCE_ID`` stamp.

    Cutover Slice B (#84) repointed instance->pane reverse lookups to the live
    ``@INSTANCE_ID`` pane stamp and retired ``pane_label``/``tmux_pane`` from
    ``/api/instances``. The restart planner was the lone consumer still reading
    the now-absent ``pane_label``, so every instance fell out of the candidate
    set (see ``_is_candidate``) and nothing resumed across a ``tx restart``.

    Here we rebuild the reverse map from the live workspace: ``@INSTANCE_ID`` ->
    pane role. An instance whose registry ``pane_label`` is empty but whose id
    is stamped on a live pane inherits that pane's role. Instances that already
    carry a ``pane_label`` are left untouched, so a future registry that resumes
    serving pane labels keeps working unchanged.
    """
    label_by_instance: dict[str, str] = {}
    for pane in workspace.iter_panes():
        if pane.instance_id and pane.pane_role:
            label_by_instance.setdefault(pane.instance_id, canonical_pane_role(pane.pane_role))
    if not label_by_instance:
        return instances
    return tuple(
        inst
        if inst.pane_label or inst.instance_id not in label_by_instance
        else replace(inst, pane_label=label_by_instance[inst.instance_id])
        for inst in instances
    )


def _is_resumable(instance: InstanceRegistryEntry) -> bool:
    return instance.status in {
        InstanceStatus.IDLE,
        InstanceStatus.PROCESSING,
        InstanceStatus.STOPPED,
    }


def _is_candidate(instance: InstanceRegistryEntry) -> bool:
    return (
        bool(instance.pane_label)
        and bool(instance.instance_id)
        and not instance.is_subagent
        and instance.rank != "retired"
        and _is_resumable(instance)
    )


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
    # Legacy helper retained for external tests/imports. Normal restart planning
    # calls _live_snapshot_candidates and does not consider DB-only rows.
    return [instance for instance in instances if _is_candidate(instance)]


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
            legal.update(PALACE_ROLES)
        elif window.archetype.value == "somnium":
            legal.update(SOMNIUM_ROLES)
        elif window.archetype.value in {"legion_stack", "mechanicus_stack"}:
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
        return "instance has live tmux runtime state"
    return f"instance status {instance.status.value} is not resumable"


def _orchestrator_tombstone_role(instance: InstanceRegistryEntry, pane_label: str) -> str:
    legion = instance.legion.strip().lower()
    if legion == "custodes" and pane_label != "legion:custodes":
        return "legion:custodes"
    if legion == "fabricator" and pane_label != "mechanicus:fabricator-general":
        return "mechanicus:fabricator-general"
    return ""
