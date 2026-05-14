from __future__ import annotations

from .audience import audience_jump
from .enums import PaneKind
from .labels import canonical_pane_role
from .tmux_adapter import TmuxAdapter


def install_tombstone(
    adapter: TmuxAdapter,
    slot_pane: str,
    source_role: str,
    target_pane: str,
) -> str:
    """Mark ``slot_pane`` as a resolver tombstone pointing at ``target_pane``."""
    source_role = canonical_pane_role(source_role)
    target_pane_id = adapter.run(
        "display-message", "-t", target_pane, "-p", "#{pane_id}"
    ).strip()
    if not target_pane_id:
        raise ValueError(f"tombstone target not found: {target_pane}")
    slot_pane_id = adapter.run("display-message", "-t", slot_pane, "-p", "#{pane_id}").strip()
    if not slot_pane_id:
        raise ValueError(f"tombstone slot pane not found: {slot_pane}")
    for option, value in (
        ("@PANE_ID", source_role),
        ("@PANE_TYPE", PaneKind.TOMBSTONE.value),
        ("@TOMBSTONE_SOURCE", source_role),
        ("@TOMBSTONE_TARGET", target_pane_id),
    ):
        adapter.run("set-option", "-p", "-t", slot_pane_id, option, value)
    return f"installed tombstone {source_role} -> {target_pane_id}"


def jump_tombstone(adapter: TmuxAdapter, target: str, *, client: str = "") -> str:
    """Select the live pane resolved from a tombstone/logical target."""
    return audience_jump(adapter, target, client=client)
