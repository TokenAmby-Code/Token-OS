from __future__ import annotations

import pathlib
import sys
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import custodes
from tmuxctl.liveness import detect_pane_tui, instance_live_tui


class FakeLivenessAdapter:
    """Minimal adapter: serves pane_pid lookups and a stamped list-panes scan.

    ``pane_pids`` maps a pane id to its shell pid (the value tmux reports for
    ``#{pane_pid}``); a missing pane returns an empty string (dead pane).
    ``stamped`` is the ``-a`` scan: list of (pane_id, instance_id, pane_pid).
    """

    def __init__(
        self,
        *,
        pane_pids: dict[str, int] | None = None,
        stamped: list[tuple[str, str, int]] | None = None,
    ) -> None:
        self.pane_pids = pane_pids or {}
        self.stamped = stamped or []
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[0] == "display-message" and args[-1] == "#{pane_pid}":
            pane = args[args.index("-t") + 1]
            pid = self.pane_pids.get(pane)
            return f"{pid}\n" if pid is not None else ""
        if args[0] == "list-panes" and "-a" in args:
            return (
                "\n".join(f"{pane_id}\t{iid}\t{pid}" for pane_id, iid, pid in self.stamped) + "\n"
            )
        return ""


def _tree(parent_pid: int, descendants: dict[int, tuple[int, str]]):
    children: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    for pid, (ppid, command) in descendants.items():
        commands[pid] = command.lower()
        children.setdefault(ppid, []).append(pid)
    return children, commands


def test_detect_live_claude_tui_is_live():
    adapter = FakeLivenessAdapter(pane_pids={"%9": 14030})
    children, commands = _tree(
        14030,
        {
            15215: (14030, "bash claude-wrapper.sh --dangerously-skip-permissions"),
            15230: (15215, "claude"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        tui = detect_pane_tui(adapter, "%9")

    assert tui.live is True
    assert tui.pane_pid == 14030
    # Fail-closed substring match (custodes semantics) reports the first live
    # agent process under the pane — the wrapper or the engine, both alive.
    assert tui.agent_pid in {15215, 15230}


def test_detect_bare_idle_shell_is_not_live():
    adapter = FakeLivenessAdapter(pane_pids={"%9": 500})
    children, commands = _tree(500, {501: (500, "-zsh")})
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        tui = detect_pane_tui(adapter, "%9")

    assert tui.live is False
    assert tui.agent_pid is None


def test_detect_dead_pane_is_not_live():
    # Pane carries no pid (display-message returns empty) → truly-dead pane.
    adapter = FakeLivenessAdapter(pane_pids={})
    with patch.object(custodes, "_process_tree", return_value=({}, {})):
        tui = detect_pane_tui(adapter, "%gone")

    assert tui.live is False
    assert tui.pane_pid is None


def test_instance_live_tui_resolved_pane_live():
    adapter = FakeLivenessAdapter(pane_pids={"%9": 14030})
    children, commands = _tree(14030, {15230: (14030, "claude")})
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        tui = instance_live_tui(adapter, "iid-1", "%9")

    assert tui is not None
    assert tui.pane_id == "%9"
    assert tui.live is True


def test_instance_live_tui_divergence_sweep_finds_stamped_live_pane():
    # The registry resolved a stale pane (%dead, gone), but the live TUI runs on
    # %live which still carries @INSTANCE_ID == iid-1. The sweep must catch it.
    adapter = FakeLivenessAdapter(
        pane_pids={"%live": 14030},
        stamped=[("%live", "iid-1", 14030), ("%other", "iid-2", 22000)],
    )
    children, commands = _tree(14030, {15230: (14030, "claude")})
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        tui = instance_live_tui(adapter, "iid-1", "%dead")

    assert tui is not None
    assert tui.pane_id == "%live"
    assert tui.live is True


def test_instance_live_tui_idle_husk_returns_none():
    # Resolved pane is gone, no stamped pane carries the instance → reapable.
    adapter = FakeLivenessAdapter(pane_pids={}, stamped=[])
    with patch.object(custodes, "_process_tree", return_value=({}, {})):
        assert instance_live_tui(adapter, "iid-1", "%gone") is None
