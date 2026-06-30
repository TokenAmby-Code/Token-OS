from __future__ import annotations

import pathlib
import sys
import time
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import custodes
from tmuxctl.dispatch_liveness import live_agents_in_dir, pane_is_live


class FakePaneAdapter:
    """Serves ``#{pane_pid}`` lookups and a ``list-panes -a`` (id\\tcwd\\tborn) scan."""

    def __init__(
        self,
        *,
        pane_pids: dict[str, int] | None = None,
        panes: list[tuple[str, str]] | None = None,
        born: dict[str, str] | None = None,
    ) -> None:
        self.pane_pids = pane_pids or {}
        self.panes = panes or []  # (pane_id, cwd)
        self.born = born or {}  # pane_id -> @PANE_BORN epoch stamp
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[0] == "display-message" and args[-1] == "#{pane_pid}":
            pane = args[args.index("-t") + 1]
            pid = self.pane_pids.get(pane)
            return f"{pid}\n" if pid is not None else ""
        if args[0] == "list-panes" and "-a" in args:
            return (
                "\n".join(
                    f"{pane_id}\t{cwd}\t{self.born.get(pane_id, '')}" for pane_id, cwd in self.panes
                )
                + "\n"
            )
        return ""


def _tree(parent_pid: int, descendants: dict[int, tuple[int, str]]):
    children: dict[int, list[int]] = {}
    commands: dict[int, str] = {}
    for pid, (ppid, command) in descendants.items():
        commands[pid] = command.lower()
        children.setdefault(ppid, []).append(pid)
    return children, commands


def test_pane_is_live_true_for_live_agent() -> None:
    adapter = FakePaneAdapter(pane_pids={"%9": 14030})
    children, commands = _tree(14030, {15230: (14030, "codex")})
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_is_live(adapter, "%9") is True


def test_pane_is_live_false_for_bare_shell() -> None:
    adapter = FakePaneAdapter(pane_pids={"%9": 500})
    children, commands = _tree(500, {501: (500, "-zsh")})
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert pane_is_live(adapter, "%9") is False


def test_pane_is_live_false_for_empty_pane() -> None:
    assert pane_is_live(FakePaneAdapter(), "") is False


def test_live_agents_in_dir_matches_cwd_and_liveness(tmp_path: pathlib.Path) -> None:
    work = tmp_path / "wt-target"
    work.mkdir()
    other = tmp_path / "wt-other"
    other.mkdir()
    adapter = FakePaneAdapter(
        pane_pids={"%live": 14030, "%idle": 500, "%elsewhere": 9000},
        panes=[
            ("%live", str(work)),  # live agent in the target dir → match
            ("%idle", str(work)),  # same dir but no agent → skip
            ("%elsewhere", str(other)),  # live agent in a different dir → skip
        ],
    )
    children, commands = _tree(0, {})
    # Both %live and %elsewhere host a live agent; only %live is rooted in `work`.
    children = {14030: [15230], 9000: [9001]}
    commands = {15230: "claude", 9001: "codex"}
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        matches = live_agents_in_dir(adapter, str(work))

    assert [m.pane_id for m in matches] == ["%live"]
    assert matches[0].agent_command == "claude"


def test_live_agents_in_dir_excludes_self_pane(tmp_path: pathlib.Path) -> None:
    work = tmp_path / "wt-target"
    work.mkdir()
    adapter = FakePaneAdapter(
        pane_pids={"%self": 14030},
        panes=[("%self", str(work))],
    )
    children = {14030: [15230]}
    commands = {15230: "claude"}
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        # The dispatcher's own pane must not refuse its own launch.
        assert live_agents_in_dir(adapter, str(work), exclude_pane="%self") == []


def test_live_agents_in_dir_empty_when_no_server() -> None:
    # A dead/absent tmux server yields no panes → no matches, no crash.
    assert live_agents_in_dir(FakePaneAdapter(panes=[]), "/some/dir") == []


def test_live_agents_in_dir_refuses_cold_starting_pane(tmp_path: pathlib.Path) -> None:
    # A pane rooted in the target worktree whose agent is NOT yet observable but
    # whose @PANE_BORN is fresh is cold-starting. The false-fail retry race would
    # otherwise stack a duplicate; the guard must fail closed and refuse it.
    work = tmp_path / "wt-target"
    work.mkdir()
    adapter = FakePaneAdapter(
        pane_pids={"%booting": 14030},
        panes=[("%booting", str(work))],
        born={"%booting": str(time.time())},
    )
    children, commands = _tree(0, {})  # no live agent process anywhere
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        matches = live_agents_in_dir(adapter, str(work))

    assert [m.pane_id for m in matches] == ["%booting"]
    assert matches[0].agent_pid is None


def test_live_agents_in_dir_ignores_old_born_idle_pane(tmp_path: pathlib.Path) -> None:
    # An idle pane born long ago (past boot grace) with no live agent is a genuine
    # free worktree — not a duplicate to refuse.
    work = tmp_path / "wt-target"
    work.mkdir()
    adapter = FakePaneAdapter(
        pane_pids={"%idle": 500},
        panes=[("%idle", str(work))],
        born={"%idle": str(time.time() - 10_000)},
    )
    children, commands = _tree(0, {})
    with patch.object(custodes, "_process_tree", return_value=(children, commands)):
        assert live_agents_in_dir(adapter, str(work)) == []
