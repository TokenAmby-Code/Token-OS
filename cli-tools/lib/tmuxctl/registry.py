"""Instance registry types and normalization helpers for tmuxctl restart flows.

The control plane should treat registry state as the primary source of intent
for restart planning. Live tmux state is still read for coherence checks and
transport verification, but the planner should not depend on ad hoc snapshot
files as its main authority.
"""

from __future__ import annotations

from .enums import InstanceStatus
from .models import InstanceRegistryEntry, InstanceRegistrySnapshot

# /api/instances serves the token-api status vocabulary (instance_registry.py
# VALID_STATUSES), which renames "processing" to "working" and adds the
# mid-conversation states below. Anything unrecognized falls to UNKNOWN, and
# UNKNOWN instances are not resumable — so an unmapped live status silently
# drops the instance from every restart plan.
_ACTIVE_STATUSES = {
    "processing",
    "working",
    "questioning",
    "preplanning",
    "planning",
    "compacting",
    "reviewing",
}


def normalize_instance_status(value: str | None) -> InstanceStatus:
    if not value:
        return InstanceStatus.UNKNOWN
    raw = value.strip().lower().replace("-", "_")
    if raw in _ACTIVE_STATUSES:
        return InstanceStatus.PROCESSING
    if raw in {"idle", "victorious"}:
        return InstanceStatus.IDLE
    if raw == "stopped":
        return InstanceStatus.STOPPED
    return InstanceStatus.UNKNOWN


def build_registry_snapshot(
    *,
    device_id: str,
    instances: list[dict[str, object]] | tuple[dict[str, object], ...],
) -> InstanceRegistrySnapshot:
    normalized: list[InstanceRegistryEntry] = []
    for row in instances:
        row_device_id = str(row.get("device_id", "") or "")
        if row_device_id and device_id and row_device_id != device_id:
            continue
        # Canonical persona identity from the instances.persona_id JOIN.
        # /api/instances nests it as persona.slug; the flat persona_slug/
        # profile_name aliases are accepted as fallbacks for older shapes.
        persona_obj = row.get("persona")
        persona_slug = (persona_obj.get("slug") if isinstance(persona_obj, dict) else None) or (
            row.get("persona_slug") or row.get("profile_name") or ""
        )
        normalized.append(
            InstanceRegistryEntry(
                instance_id=str(row.get("id", "") or ""),
                device_id=row_device_id,
                pane_label=str(row.get("pane_label", "") or ""),
                tmux_pane=str(row.get("tmux_pane", "") or ""),
                working_dir=str(row.get("working_dir", "") or ""),
                status=normalize_instance_status(str(row.get("status", "") or "")),
                pre_stop_status=normalize_instance_status(
                    str(row.get("pre_stop_status", "") or "")
                ),
                is_subagent=bool(row.get("is_subagent", False)),
                legion=str(row.get("legion", "") or ""),
                tab_name=str(row.get("tab_name", "") or ""),
                instance_type=str(row.get("instance_type", "") or ""),
                engine=str(row.get("engine", "") or ""),
                last_activity=str(row.get("last_activity", "") or ""),
                stopped_at=str(row.get("stopped_at", "") or ""),
                primarch=str(row.get("primarch", "") or ""),
                persona_slug=str(persona_slug or ""),
                rank=str(row.get("rank", "") or ""),
            )
        )
    return InstanceRegistrySnapshot(device_id=device_id, instances=tuple(normalized))
