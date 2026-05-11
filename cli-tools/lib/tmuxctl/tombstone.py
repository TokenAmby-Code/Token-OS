from __future__ import annotations

import shlex

from .enums import PaneKind
from .labels import canonical_pane_role
from .resolver import resolve_pane
from .tmux_adapter import TmuxAdapter


def _show(adapter: TmuxAdapter, target: str, fmt: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", fmt).strip()


def _pane_option(adapter: TmuxAdapter, pane_id: str, option: str) -> str:
    return adapter.show_pane_option(pane_id, option)


def _switch_client(adapter: TmuxAdapter, target_window: str, client: str = "") -> None:
    if client:
        adapter.run("switch-client", "-c", client, "-t", target_window, allow_failure=True)
        return
    adapter.run("switch-client", "-t", target_window, allow_failure=True)


def _select_pane_for_client(adapter: TmuxAdapter, pane_id: str, client: str = "") -> None:
    target_window = _show(adapter, pane_id, "#{session_name}:#{window_index}")
    _switch_client(adapter, target_window, client)
    adapter.run("select-window", "-t", target_window, allow_failure=True)
    adapter.run("select-pane", "-t", pane_id, allow_failure=True)


def _display_role(adapter: TmuxAdapter, target: str) -> str:
    if not target:
        return ""
    role = _pane_option(adapter, target, "@PANE_ID")
    if not role:
        return target
    return canonical_pane_role(role).removeprefix("audience:")


def _display_chain(chain: tuple[str, ...]) -> str:
    return " -> ".join(role.removeprefix("audience:") for role in chain)


def install_tombstone(
    adapter: TmuxAdapter,
    slot_pane: str,
    source_role: str,
    target_pane: str,
    *,
    grid_state: str = "small",
    reserved: str = "false",
) -> str:
    """Turn an existing stable slot pane into a tombstone for a promoted pane.

    This is intentionally independent from expand/zoom. A tombstone preserves a
    canonical logical slot such as ``legion:custodes`` while the real process is
    promoted elsewhere. The slot remains resolvable by @PANE_ID and points at
    the live target via @TOMBSTONE_TARGET.
    """
    slot = _show(adapter, slot_pane, "#{pane_id}")
    target = _show(adapter, target_pane, "#{pane_id}")
    source = canonical_pane_role(source_role)
    commands: list[tuple[str, ...]] = [
        ("set-option", "-p", "-t", slot, "@PANE_ID", source),
        ("set-option", "-p", "-t", slot, "@PANE_TYPE", PaneKind.TOMBSTONE.value),
        ("set-option", "-p", "-t", slot, "@GRID_STATE", grid_state),
        ("set-option", "-p", "-t", slot, "@GRID_RESERVED", reserved),
        ("set-option", "-p", "-t", slot, "@TOMBSTONE_SOURCE", source),
        ("set-option", "-p", "-t", slot, "@TOMBSTONE_TARGET", target),
    ]
    args: list[str] = []
    for index, command in enumerate(commands):
        if index:
            args.append(";")
        args.extend(command)
    adapter.run(*args, allow_failure=True)
    adapter.send_keys(
        slot,
        f"exec tmux-tombstone {shlex.quote(source)} {shlex.quote(target)}",
        "Enter",
    )
    return f"installed tombstone {source} -> {target}"


def jump_tombstone(adapter: TmuxAdapter, target: str, *, client: str = "") -> str:
    """Resolve a pane or logical role through tombstones and select the live pane."""
    resolved = resolve_pane(adapter, target)
    _select_pane_for_client(adapter, resolved.pane_id, client)
    chain = _display_chain(resolved.chain)
    destination = _display_role(adapter, resolved.pane_id)
    return f"selected {destination}" + (f" via {chain}" if chain else "")

