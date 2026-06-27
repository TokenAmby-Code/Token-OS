"""Daemon-native pane occupancy/liveness ledger.

This module is the single tmuxctl source of truth for dispatch seat availability:
occupancy is derived from live tmux pane state plus the process-tree liveness
oracle, never from Token-API registry rows.  It is deliberately small and
stdlib-only so both the tmuxctld daemon handlers and the in-process service paths
consume the same ledger instead of growing split-brain guards.
"""

from __future__ import annotations

from dataclasses import dataclass

from .singleton_labels import canonical_singleton_label, is_persona_singleton_label
from .tmux_adapter import TmuxAdapter


@dataclass(frozen=True)
class PaneOccupancy:
    pane_id: str
    pane_role: str
    window_name: str
    pane_pid: int | None
    instance_id: str
    clean: bool
    live_agent: bool

    @property
    def singleton(self) -> bool:
        return is_persona_singleton_label(self.pane_role)

    @property
    def occupied(self) -> bool:
        # Stamps are advisory for occupancy.  Live process liveness and singleton
        # labels are sufficient to exclude a pane even when @INSTANCE_ID is empty,
        # stale, or contaminated.
        return bool(self.instance_id) or self.live_agent or self.singleton

    @property
    def dispatch_available(self) -> bool:
        return self.clean and not self.occupied


def _parse_pid(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _active_agent(pane_pid: int | None) -> bool:
    # Lazy import avoids the historical custodes.py -> stack.py -> _stack_core.py
    # cycle while still using the shared process-tree oracle.
    from .custodes import active_agent_in_pane

    return active_agent_in_pane(pane_pid) is not None


def scan_pane_occupancy(adapter: TmuxAdapter) -> list[PaneOccupancy]:
    """Return the live occupancy ledger for every pane in tmux.

    One tmux scan supplies pane labels/stamps/pids; process liveness is resolved
    through the shared Claude/Codex subtree oracle.  No DB rows participate.
    """
    raw = adapter.run(
        "list-panes",
        "-a",
        "-F",
        "\t".join(
            [
                "#{pane_id}",
                "#{@PANE_CLEAN}",
                "#{@INSTANCE_ID}",
                "#{@PANE_ID}",
                "#{window_name}",
                "#{pane_pid}",
            ]
        ),
        allow_failure=True,
    )
    ledger: list[PaneOccupancy] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        pane_id, clean, instance_id, pane_role, window_name, pane_pid_raw = parts
        role = canonical_singleton_label(pane_role.strip()) if pane_role.strip() else ""
        pane_pid = _parse_pid(pane_pid_raw)
        ledger.append(
            PaneOccupancy(
                pane_id=pane_id,
                pane_role=role,
                window_name=window_name.strip(),
                pane_pid=pane_pid,
                instance_id=instance_id.strip(),
                clean=clean.strip() == "1",
                live_agent=_active_agent(pane_pid),
            )
        )
    return ledger


def occupancy_for_pane(adapter: TmuxAdapter, pane: str) -> PaneOccupancy | None:
    """Resolve one pane and return its occupancy, or None if it vanished."""
    try:
        resolved = adapter._resolve_pane_target_arg(pane)
    except Exception:
        resolved = pane
    raw = adapter.run(
        "display-message",
        "-t",
        resolved,
        "-p",
        "\t".join(
            [
                "#{pane_id}",
                "#{@PANE_CLEAN}",
                "#{@INSTANCE_ID}",
                "#{@PANE_ID}",
                "#{window_name}",
                "#{pane_pid}",
            ]
        ),
        allow_failure=True,
    ).strip()
    if not raw:
        return None
    parts = raw.split("\t")
    if len(parts) != 6:
        return None
    pane_id, clean, instance_id, pane_role, window_name, pane_pid_raw = parts
    pane_pid = _parse_pid(pane_pid_raw)
    return PaneOccupancy(
        pane_id=pane_id,
        pane_role=canonical_singleton_label(pane_role.strip()) if pane_role.strip() else "",
        window_name=window_name.strip(),
        pane_pid=pane_pid,
        instance_id=instance_id.strip(),
        clean=clean.strip() == "1",
        live_agent=_active_agent(pane_pid),
    )


def assert_dispatch_target_available(adapter: TmuxAdapter, pane: str) -> PaneOccupancy:
    """Fail closed unless pane is safe for dispatch launcher bytes."""
    occupancy = occupancy_for_pane(adapter, pane)
    if occupancy is None:
        raise ValueError(f"pane target not found: {pane}")
    if occupancy.singleton:
        raise ValueError(
            f"dispatch target is protected singleton seat: {occupancy.pane_role or occupancy.pane_id}"
        )
    if occupancy.instance_id:
        raise ValueError(f"dispatch target is occupied: @INSTANCE_ID={occupancy.instance_id}")
    if occupancy.live_agent:
        raise ValueError(
            f"dispatch target has live Claude/Codex agent: pane_pid={occupancy.pane_pid}"
        )
    return occupancy


def looks_like_dispatch_launcher_payload(text: str) -> bool:
    value = (text or "").strip()
    if value == "clear":
        return True
    return "dispatch-agent." in value or "TOKEN_API_INTERNAL_DISPATCH=1" in value
