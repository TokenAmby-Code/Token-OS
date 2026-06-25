from __future__ import annotations

import pathlib
import sys
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import custodes
from tmuxctl.custodes import (
    active_agent_in_pane,
    assert_custodes,
    pane_has_active_agent,
    pane_has_active_claude,
)


class FakeAdapter:
    """Minimal TmuxAdapter stand-in for assert_custodes — only needs `run()`."""

    def __init__(self, *, pane_pid: int | str = "0") -> None:
        self.pane_pid = str(pane_pid)
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args, allow_failure: bool = False) -> str:  # noqa: D401
        self.calls.append(args)
        if args[0] == "display-message" and args[-1] == "#{pane_pid}":
            return self.pane_pid + "\n"
        return ""


def _tree(*, parent_pid: int, descendants: dict[int, tuple[int, str]]):
    """Build (children_by_ppid, command_by_pid) tuple for `_process_tree`.

    descendants maps pid → (ppid, command).
    """
    children: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    # ensure parent exists in the tree (no command itself)
    for pid, (ppid, command) in descendants.items():
        commands[pid] = command.lower()
        children.setdefault(ppid, []).append(pid)
    return children, commands


def test_detector_walks_bash_wrapper_to_claude():
    # pane_pid 14030 (bash) → 15215 (claude-wrapper.sh) → 15230 (claude)
    children, commands = _tree(
        parent_pid=14030,
        descendants={
            15215: (14030, "bash claude-wrapper.sh --dangerously-skip-permissions"),
            15230: (15215, "claude"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_has_active_claude(14030) is True


def test_detector_matches_execd_claude_as_pane_pid_with_no_children():
    # PR #366's persona-seat.sh `exec`s the engine, so #{pane_pid} IS the claude
    # process itself — no wrapper-bash parent, no descendants. The walk must match
    # the pane_pid process's OWN command, not only its (nonexistent) children.
    # Mirrors the live council:custodes seat: pane_pid=25905 command
    # `/Users/tokenclaw/.local/bin/claude.token-os-real …`, zero children.
    children, commands = _tree(
        parent_pid=19448,
        descendants={
            25905: (19448, "/Users/tokenclaw/.local/bin/claude.token-os-real --model opus"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_has_active_claude(25905) is True
        assert pane_has_active_agent(25905) is True
        assert active_agent_in_pane(25905) == (
            25905,
            "/users/tokenclaw/.local/bin/claude.token-os-real --model opus",
        )


def test_detector_matches_execd_codex_as_pane_pid_with_no_children():
    # Same exec'd-seat shape for a Codex persona seat — the agent IS the pane proc.
    children, commands = _tree(
        parent_pid=19448,
        descendants={
            25910: (19448, "/opt/homebrew/bin/node /opt/homebrew/bin/codex"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_has_active_agent(25910) is True
        assert pane_has_active_claude(25910) is False  # codex is not claude


def test_detector_false_for_execd_bare_shell_as_pane_pid():
    # Negative: an exec'd-style pane whose pane_pid IS a bare login shell (no agent
    # anywhere) must still read not-live — the seeded-pane-pid walk must not
    # false-positive on the shell itself and let the retire/respawn guards misfire.
    children, commands = _tree(
        parent_pid=400,
        descendants={
            500: (400, "-zsh"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_has_active_agent(500) is False
        assert pane_has_active_claude(500) is False
        assert active_agent_in_pane(500) is None


def test_detector_finds_claude_via_node_argv():
    children, commands = _tree(
        parent_pid=100,
        descendants={
            200: (100, "/usr/local/bin/node /Users/x/.claude/bin/claude.js"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_has_active_claude(100) is True


def test_agent_detector_finds_codex_runtime():
    children, commands = _tree(
        parent_pid=100,
        descendants={
            200: (100, "/opt/homebrew/bin/node /opt/homebrew/bin/codex"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_has_active_agent(100) is True
        assert pane_has_active_claude(100) is False


def test_agent_detector_finds_claude_runtime():
    children, commands = _tree(
        parent_pid=100,
        descendants={
            200: (100, "/usr/local/bin/node /Users/x/.claude/bin/claude.js"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_has_active_agent(100) is True
        assert pane_has_active_claude(100) is True


def test_agent_detector_false_for_missing_pid():
    assert pane_has_active_agent(None) is False
    assert pane_has_active_agent(0) is False


def test_agent_detector_false_when_ps_returns_empty():
    with patch.object(custodes, "_process_tree", return_value=({}, {})):
        assert pane_has_active_agent(1234) is False


def test_detector_false_for_plain_shell_pane():
    # bash pane with no descendants except a shell helper
    children, commands = _tree(
        parent_pid=500,
        descendants={
            501: (500, "less"),
        },
    )
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_has_active_claude(500) is False


def test_detector_false_for_missing_pid():
    assert pane_has_active_claude(None) is False
    assert pane_has_active_claude(0) is False


def test_detector_false_when_ps_returns_empty():
    with patch.object(custodes, "_process_tree", return_value=({}, {})):
        assert pane_has_active_claude(1234) is False


def test_assert_custodes_upserts_when_claude_alive():
    adapter = FakeAdapter(pane_pid=14030)
    with (
        patch.object(custodes, "_ensure_custodes_pane", return_value="%42"),
        patch.object(custodes, "pane_has_active_claude", return_value=True),
        patch.object(custodes, "_upsert_via_claude_cmd", return_value=(True, "ok")) as up,
        patch.object(custodes, "_launch_via_dispatch") as launch,
    ):
        result = assert_custodes(adapter, "hello custodes")

    up.assert_called_once_with("%42", "hello custodes")
    launch.assert_not_called()
    assert result["dispatched"] is True
    assert result["reason"] == "upserted_existing_pane"
    assert result["tmux_pane"] == "%42"
    assert result["pane_pid"] == 14030


def test_assert_custodes_launches_when_no_claude_in_tree():
    adapter = FakeAdapter(pane_pid=500)
    with (
        patch.object(custodes, "_ensure_custodes_pane", return_value="%99"),
        patch.object(custodes, "pane_has_active_claude", return_value=False),
        patch.object(custodes, "_upsert_via_claude_cmd") as up,
        patch.object(custodes, "_launch_via_dispatch", return_value=(True, "ok")) as launch,
    ):
        result = assert_custodes(adapter, "wake up")

    up.assert_not_called()
    launch.assert_called_once()
    pane_arg, file_arg = launch.call_args[0]
    assert pane_arg == "%99"
    assert str(file_arg).endswith(".md")
    assert result["dispatched"] is True
    assert result["reason"] == "launched_new_custodes"
    assert result["pane_pid"] == 500


def test_assert_custodes_propagates_dispatch_failure():
    adapter = FakeAdapter(pane_pid=500)
    with (
        patch.object(custodes, "_ensure_custodes_pane", return_value="%5"),
        patch.object(custodes, "pane_has_active_claude", return_value=False),
        patch.object(
            custodes,
            "_launch_via_dispatch",
            return_value=(False, "dispatch rc=66: prompt file not found"),
        ),
    ):
        result = assert_custodes(adapter, "msg")

    assert result["dispatched"] is False
    assert "launch_failed" in result["reason"]


def test_assert_custodes_handles_missing_pane_pid():
    adapter = FakeAdapter(pane_pid="")
    with (
        patch.object(custodes, "_ensure_custodes_pane", return_value="%7"),
        patch.object(custodes, "_process_tree", return_value=({}, {})),
        patch.object(custodes, "_launch_via_dispatch", return_value=(True, "ok")) as launch,
    ):
        result = assert_custodes(adapter, "msg")

    launch.assert_called_once()
    assert result["dispatched"] is True
    assert result["pane_pid"] is None
