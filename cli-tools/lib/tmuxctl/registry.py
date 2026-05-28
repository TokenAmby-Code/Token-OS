"""Instance registry types and normalization helpers for tmuxctl restart flows.

The control plane should treat registry state as the primary source of intent
for restart planning. Live tmux state is still read for coherence checks and
transport verification, but the planner should not depend on ad hoc snapshot
files as its main authority.
"""

from __future__ import annotations

from .enums import InstanceStatus
from .models import InstanceRegistryEntry, InstanceRegistrySnapshot


def normalize_instance_status(value: str | None) -> InstanceStatus:
    if not value:
        return InstanceStatus.UNKNOWN
    raw = value.strip().lower().replace("-", "_")
    if raw == "processing":
        return InstanceStatus.PROCESSING
    if raw == "idle":
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
            )
        )
    return InstanceRegistrySnapshot(device_id=device_id, instances=tuple(normalized))
