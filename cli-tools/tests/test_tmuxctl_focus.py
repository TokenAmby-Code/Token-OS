from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.focus import build_focus_plan
from tmuxctl.models import PaneSnapshot, WindowSnapshot
from tmuxctl.revert import cleanup_transient_windows, is_transient_window_name


def _pane(
    pane_id: str, role: str, *, state: GridState = GridState.SMALL, active: bool = False
) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        window_name="palace",
        pane_index=int(pane_id.removeprefix("%")),
        width=40,
        height=20,
        current_command="zsh",
        tty="/dev/ttys001",
        pane_role=role,
        grid_state=state,
        pane_kind=PaneKind.UNKNOWN,
        reserved=False,
        active=active,
    )


def _window(*panes: PaneSnapshot, **kwargs) -> WindowSnapshot:
    return WindowSnapshot(
        session_name="main",
        window_index=1,
        window_name="palace",
        archetype=WindowArchetype.PALACE,
        focused=kwargs.pop("focused", False),
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
        panes=panes,
        **kwargs,
    )


class FakeAdapter:
    def __init__(self, windows: set[str] | None = None, exists: set[str] | None = None) -> None:
        self.windows = windows or set()
        self.exists = exists or {"%1", "%2", "%3", "%4", "%5", "%6"}

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:3] == ("list-windows", "-t", "main"):
            return "\n".join(f"{i}\t{name}" for i, name in enumerate(sorted(self.windows)))
        if args[0] == "display-message" and args[-1] == "#{pane_id}":
            target = args[args.index("-t") + 1]
            return f"{target}\n" if target in self.exists else ""
        return ""


def test_grid_focus_stashes_only_grid_siblings_and_preserves_side_panes():
    window = _window(
        _pane("%1", "palace:WW", state=GridState.SIDE),
        _pane("%2", "palace:NW", active=True),
        _pane("%3", "palace:NE"),
        _pane("%4", "palace:SW"),
        _pane("%5", "palace:SE"),
        _pane("%6", "palace:EE", state=GridState.SIDE),
    )

    plan = build_focus_plan(FakeAdapter(), window, "toggle")
    commands = [action.argv for action in plan.actions]

    assert plan.operation == "focus"
    assert any(
        cmd[:2] == ("break-pane", "-d") and "-s" in cmd and cmd[cmd.index("-s") + 1] == "%3"
        for cmd in commands
    )
    assert any(cmd[0] == "move-pane" and cmd[cmd.index("-s") + 1] == "%4" for cmd in commands)
    assert any(cmd[0] == "move-pane" and cmd[cmd.index("-s") + 1] == "%5" for cmd in commands)
    assert not any(cmd[0] in {"kill-pane", "swap-pane"} for cmd in commands)
    assert not any(
        "%1" in cmd and cmd[0] in {"break-pane", "move-pane", "kill-pane"} for cmd in commands
    )
    assert not any(
        "%6" in cmd and cmd[0] in {"break-pane", "move-pane", "kill-pane"} for cmd in commands
    )


def test_palace_h_grid_focus_stashes_only_one_center_sibling():
    window = _window(
        _pane("%1", "palace:W", state=GridState.SIDE),
        _pane("%2", "palace:N", active=True),
        _pane("%3", "palace:S"),
        _pane("%4", "palace:E", state=GridState.SIDE),
    )

    plan = build_focus_plan(FakeAdapter(exists={"%1", "%2", "%3", "%4"}), window, "toggle")
    moving = [
        action.argv for action in plan.actions if action.argv[0] in {"break-pane", "move-pane"}
    ]

    assert plan.operation == "focus"
    assert len(moving) == 1
    assert moving[0][moving[0].index("-s") + 1] == "%3"


def test_side_focus_widens_without_stashing_grid():
    window = _window(
        _pane("%1", "palace:WW", state=GridState.SIDE, active=True),
        _pane("%2", "palace:NW"),
        _pane("%3", "palace:NE"),
        _pane("%4", "palace:SW"),
        _pane("%5", "palace:SE"),
        _pane("%6", "palace:EE", state=GridState.SIDE),
    )

    plan = build_focus_plan(FakeAdapter(), window, "toggle")
    commands = [action.argv for action in plan.actions]

    assert plan.operation == "focus"
    assert plan.axis.value == "side"
    assert any(cmd[0] == "resize-pane" and cmd[cmd.index("-t") + 1] == "%1" for cmd in commands)
    assert not any(
        cmd[0] in {"break-pane", "move-pane", "kill-pane", "swap-pane"} for cmd in commands
    )


def test_grid_and_side_focus_state_can_coexist():
    window = _window(
        _pane("%1", "palace:WW", state=GridState.SIDE, active=True),
        _pane("%2", "palace:NW"),
        _pane("%6", "palace:EE", state=GridState.SIDE),
        focused=True,
        grid_focus_active=True,
        grid_focus_pane="%2",
        grid_focus_stash="%3:palace:NE:NE,%4:palace:SW:SW,%5:palace:SE:SE",
    )

    plan = build_focus_plan(FakeAdapter(), window, "toggle")
    set_options = [
        cmd for cmd in [a.argv for a in plan.actions] if cmd[:3] == ("set-option", "-w", "-t")
    ]

    assert plan.axis.value == "side"
    assert ("set-option", "-w", "-t", "main:1", "@FOCUS_SIDE_ACTIVE", "true") in set_options
    assert not any(cmd[-2] == "@FOCUS_GRID_ACTIVE" and cmd[-1] == "false" for cmd in set_options)


def test_expand_script_has_no_topology_or_audience_control_path():
    script = (ROOT / "bin" / "tmux-grid-expand").read_text()
    forbidden = ["move-pane", "join-pane", "swap-pane", "kill-pane", "tmuxctl audience"]
    assert not any(token in script for token in forbidden)
    assert "resize-pane -Z" in script


def test_grid_unfocus_plan_does_not_use_tiled_layout():
    window = _window(
        _pane("%2", "palace:NW", active=True),
        focused=True,
        grid_focus_active=True,
        grid_focus_pane="%2",
        grid_focus_stash="%3:palace:NE:NE,%4:palace:SW:SW,%5:palace:SE:SE",
    )

    plan = build_focus_plan(FakeAdapter(), window, "unfocus-grid")

    assert plan.operation == "unfocus"
    assert not any(
        action.argv[:2] == ("select-layout", "-t") and action.argv[-1] == "tiled"
        for action in plan.actions
    )


class FakeCleanupAdapter:
    def __init__(self) -> None:
        self.killed: list[str] = []

    def list_windows(self, session_name: str) -> list[dict[str, str]]:
        return [
            {"window_name": "palace"},
            {"window_name": "_stash_palace"},
            {"window_name": "_fstash_somnium"},
            {"window_name": "_focus_stash_palace"},
            {"window_name": "legion"},
        ]

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "kill-window":
            self.killed.append(args[args.index("-t") + 1])
        return ""


def test_transient_window_cleanup_is_centralized_for_all_stash_families():
    adapter = FakeCleanupAdapter()

    removed = cleanup_transient_windows(adapter, "main")  # type: ignore[arg-type]

    assert removed == ("_stash_palace", "_fstash_somnium", "_focus_stash_palace")
    assert adapter.killed == [
        "main:_stash_palace",
        "main:_fstash_somnium",
        "main:_focus_stash_palace",
    ]
    assert is_transient_window_name("_focus_stash_somnium")
    assert not is_transient_window_name("palace")


def test_grid_focus_refuses_when_another_grid_focus_is_active() -> None:
    window = _window(
        _pane("%2", "palace:N", active=True),
        _pane("%3", "palace:S"),
        focused=True,
        grid_focus_active=True,
        grid_focus_pane="%3",
        grid_focus_stash="%4:palace:E:E",
    )

    plan = build_focus_plan(FakeAdapter(), window, "focus-grid")

    assert plan.operation == "refuse"
    assert plan.reason == "grid focus already active; unfocus first"
    assert plan.actions == ()


def test_grid_focus_noops_when_active_grid_has_no_siblings() -> None:
    window = _window(_pane("%2", "palace:N", active=True))

    plan = build_focus_plan(FakeAdapter(), window, "focus-grid")

    assert plan.operation == "noop"
    assert plan.reason == "no grid siblings to stash"
    assert plan.actions == ()


def test_grid_focus_noops_when_active_pane_is_not_grid() -> None:
    window = _window(_pane("%1", "palace:W", state=GridState.SIDE, active=True))

    plan = build_focus_plan(FakeAdapter(), window, "focus-grid")

    assert plan.operation == "noop"
    assert plan.reason == "active pane is not a grid pane"
    assert plan.actions == ()


def test_grid_unfocus_refuses_malformed_or_missing_stash_state() -> None:
    cases = [
        ("%3:broken", "invalid focus stash entry: %3:broken", {"%2", "%3"}),
        ("%3:palace:X:X", "invalid restore coordinate: %3:palace:X:X", {"%2", "%3"}),
        ("%3:palace:S:S", "stashed pane is missing: %3", {"%2"}),
        (
            "%3:palace:S:S,%4:palace:S:S",
            "grid focus stash must contain exactly 1 or 3 panes",
            {"%2", "%3", "%4"},
        ),
    ]
    for stash, reason, exists in cases:
        window = _window(
            _pane("%2", "palace:N", active=True),
            focused=True,
            grid_focus_active=True,
            grid_focus_pane="%2",
            grid_focus_stash=stash,
        )

        plan = build_focus_plan(FakeAdapter(exists=exists), window, "unfocus-grid")

        assert plan.operation == "refuse"
        assert plan.reason == reason
        assert plan.actions == ()


def test_grid_unfocus_refuses_missing_focused_pane() -> None:
    window = _window(
        _pane("%2", "palace:N", active=True),
        focused=True,
        grid_focus_active=True,
        grid_focus_pane="%missing",
        grid_focus_stash="%3:palace:S:S",
    )

    plan = build_focus_plan(FakeAdapter(exists={"%3"}), window, "unfocus-grid")

    assert plan.operation == "refuse"
    assert plan.reason == "focused grid pane is missing"
    assert plan.actions == ()


def test_side_focus_noops_for_non_side_pane() -> None:
    window = _window(_pane("%2", "palace:N", active=True), _pane("%3", "palace:S"))

    plan = build_focus_plan(FakeAdapter(), window, "focus-side")

    assert plan.operation == "noop"
    assert plan.reason == "active pane is not a side pane"
    assert plan.actions == ()
