from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .enums import GridState, WindowArchetype
from .labels import PALACE_SIDE_ROLES, SOMNIUM_SIDE_ROLES, canonical_pane_role
from .models import PaneSnapshot, WindowSnapshot
from .snapshot import build_window_snapshot
from .tmux_adapter import TmuxAdapter

SIDE_FOCUS_RATIO = 40
SIDE_ROLES = set(PALACE_SIDE_ROLES + SOMNIUM_SIDE_ROLES)


class FocusAxis(str, Enum):
    GRID = "grid"
    SIDE = "side"


@dataclass(frozen=True)
class FocusAction:
    argv: tuple[str, ...]
    allow_failure: bool = False


@dataclass(frozen=True)
class FocusPlan:
    axis: FocusAxis
    operation: str
    window_target: str
    active_pane: str
    actions: tuple[FocusAction, ...]
    reason: str = ""


_GRID_POS = {
    "N": (0, 0),
    "NW": (0, 0),
    "NE": (1, 0),
    "S": (0, 1),
    "SW": (0, 1),
    "SE": (1, 1),
}


def _show(adapter: TmuxAdapter, target: str, fmt: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", fmt).strip()


def _window_base(name: str) -> str:
    return name.split("(", 1)[0]


def _pane_exists(adapter: TmuxAdapter, pane_id: str) -> bool:
    return bool(
        adapter.run(
            "display-message", "-t", pane_id, "-p", "#{pane_id}", allow_failure=True
        ).strip()
    )


def _window_names(adapter: TmuxAdapter, session_name: str) -> set[str]:
    return {
        row.split("\t", 1)[1]
        for row in adapter.run(
            "list-windows",
            "-t",
            session_name,
            "-F",
            "#{window_index}\t#{window_name}",
            allow_failure=True,
        ).splitlines()
        if "\t" in row
    }


def _stash_name(window: WindowSnapshot) -> str:
    return f"_focus_stash_{_window_base(window.window_name)}"


def _coord(role: str) -> tuple[int, int] | None:
    role = canonical_pane_role(role)
    if ":" not in role:
        return None
    return _GRID_POS.get(role.rsplit(":", 1)[1])


def _grid_panes(window: WindowSnapshot) -> list[PaneSnapshot]:
    return [
        p
        for p in window.panes
        if p.grid_state is GridState.SMALL and _coord(p.pane_role) is not None
    ]


def _side_panes(window: WindowSnapshot) -> list[PaneSnapshot]:
    return [
        p
        for p in window.panes
        if p.grid_state is GridState.SIDE or canonical_pane_role(p.pane_role) in SIDE_ROLES
    ]


def _active_pane(window: WindowSnapshot) -> PaneSnapshot:
    for pane in window.panes:
        if pane.active:
            return pane
    return window.panes[0]


def _set_window(option: str, value: str, target: str) -> FocusAction:
    return FocusAction(("set-option", "-w", "-t", target, option, value), True)


def _set_pane(pane: str, option: str, value: str) -> FocusAction:
    return FocusAction(("set-option", "-p", "-t", pane, option, value), True)


def _clear_pane(pane: str, option: str) -> FocusAction:
    return FocusAction(("set-option", "-pu", "-t", pane, option), True)


def _stash_window_target(window: WindowSnapshot) -> str:
    return f"{window.session_name}:{_stash_name(window)}"


def plan_focus_grid(
    adapter: TmuxAdapter, window: WindowSnapshot, active: PaneSnapshot
) -> FocusPlan:
    if window.grid_focus_active and window.grid_focus_pane == active.pane_id:
        return plan_unfocus_grid(adapter, window, active)
    if window.grid_focus_active:
        return FocusPlan(
            FocusAxis.GRID,
            "refuse",
            window.target,
            active.pane_id,
            (),
            "grid focus already active; unfocus first",
        )

    grid = _grid_panes(window)
    if active not in grid:
        return FocusPlan(
            FocusAxis.GRID,
            "noop",
            window.target,
            active.pane_id,
            (),
            "active pane is not a grid pane",
        )
    siblings = [p for p in grid if p.pane_id != active.pane_id]
    if not siblings:
        return FocusPlan(
            FocusAxis.GRID, "noop", window.target, active.pane_id, (), "no grid siblings to stash"
        )

    stash = _stash_name(window)
    stash_target = _stash_window_target(window)
    actions: list[FocusAction] = []
    names = _window_names(adapter, window.session_name)
    stash_exists = stash in names
    manifest: list[str] = []
    for i, pane in enumerate(siblings):
        pos = pane.pane_role.rsplit(":", 1)[1] if ":" in pane.pane_role else str(i)
        manifest.append(f"{pane.pane_id}:{pane.pane_role}:{pos}")
        actions.extend(
            [
                _set_pane(pane.pane_id, "@FOCUS_SOURCE_WINDOW", window.target),
                _set_pane(pane.pane_id, "@FOCUS_SOURCE_ROLE", pane.pane_role),
                _set_pane(pane.pane_id, "@FOCUS_AXIS", FocusAxis.GRID.value),
                _set_pane(pane.pane_id, "@FOCUS_RESTORE_POSITION", pos),
            ]
        )
        if not stash_exists:
            actions.append(
                FocusAction(("break-pane", "-d", "-s", pane.pane_id, "-n", stash), False)
            )
            stash_exists = True
        else:
            actions.append(
                FocusAction(("move-pane", "-d", "-s", pane.pane_id, "-t", stash_target), False)
            )
    actions.extend(
        [
            _set_window("@FOCUSED", "true", window.target),
            _set_window("@FOCUS_GRID_ACTIVE", "true", window.target),
            _set_window("@FOCUS_GRID_PANE", active.pane_id, window.target),
            _set_window("@FOCUS_GRID_STASH", ",".join(manifest), window.target),
            _set_window("@GRID_EXPANDED", "none", window.target),
            _set_window("@GRID_STASH", "", window.target),
        ]
    )
    return FocusPlan(FocusAxis.GRID, "focus", window.target, active.pane_id, tuple(actions))


def plan_unfocus_grid(
    adapter: TmuxAdapter, window: WindowSnapshot, active: PaneSnapshot | None = None
) -> FocusPlan:
    focus_pane = window.grid_focus_pane or (active.pane_id if active else "")
    if not window.grid_focus_active:
        return FocusPlan(
            FocusAxis.GRID, "noop", window.target, focus_pane, (), "grid focus is not active"
        )
    if not focus_pane or not _pane_exists(adapter, focus_pane):
        return FocusPlan(
            FocusAxis.GRID, "refuse", window.target, focus_pane, (), "focused grid pane is missing"
        )

    focus_role = ""
    for pane in window.panes:
        if pane.pane_id == focus_pane:
            focus_role = pane.pane_role
            break
    focus_coord = _coord(focus_role)
    if focus_coord is None:
        return FocusPlan(
            FocusAxis.GRID, "refuse", window.target, focus_pane, (), "focused pane has no grid role"
        )

    entries: list[tuple[str, str, tuple[int, int]]] = []
    for entry in [e for e in window.grid_focus_stash.split(",") if e]:
        parts = entry.split(":")
        if len(parts) < 3:
            return FocusPlan(
                FocusAxis.GRID,
                "refuse",
                window.target,
                focus_pane,
                (),
                f"invalid focus stash entry: {entry}",
            )
        pane_id = parts[0]
        role = ":".join(parts[1:-1])
        coord = _coord(role)
        if coord is None:
            coord = _GRID_POS.get(parts[-1])
        if coord is None:
            return FocusPlan(
                FocusAxis.GRID,
                "refuse",
                window.target,
                focus_pane,
                (),
                f"invalid restore coordinate: {entry}",
            )
        if not _pane_exists(adapter, pane_id):
            return FocusPlan(
                FocusAxis.GRID,
                "refuse",
                window.target,
                focus_pane,
                (),
                f"stashed pane is missing: {pane_id}",
            )
        entries.append((pane_id, role, coord))

    if len(entries) not in {1, 3}:
        return FocusPlan(
            FocusAxis.GRID,
            "refuse",
            window.target,
            focus_pane,
            (),
            "grid focus stash must contain exactly 1 or 3 panes",
        )

    fx, fy = focus_coord
    h_partner = next((e for e in entries if e[2][1] == fy and e[2][0] != fx), None)
    v_partner = next((e for e in entries if e[2][0] == fx and e[2][1] != fy), None)
    diagonal = next((e for e in entries if e[2][0] != fx and e[2][1] != fy), None)
    actions: list[FocusAction] = []
    if h_partner:
        args = ["join-pane", "-d", "-h", "-t", focus_pane, "-s", h_partner[0]]
        if fx == 1:
            args.insert(3, "-b")
        actions.append(FocusAction(tuple(args), False))
    if v_partner:
        args = ["join-pane", "-d", "-v", "-t", focus_pane, "-s", v_partner[0]]
        if fy == 1:
            args.insert(3, "-b")
        actions.append(FocusAction(tuple(args), False))
    if diagonal and h_partner:
        args = ["join-pane", "-d", "-v", "-t", h_partner[0], "-s", diagonal[0]]
        if fy == 1:
            args.insert(3, "-b")
        actions.append(FocusAction(tuple(args), False))
    for pane_id, role, _ in entries:
        actions.extend(
            [
                _set_pane(pane_id, "@GRID_STATE", GridState.SMALL.value),
                _set_pane(pane_id, "@PANE_ID", role),
                _clear_pane(pane_id, "@FOCUS_SOURCE_WINDOW"),
                _clear_pane(pane_id, "@FOCUS_SOURCE_ROLE"),
                _clear_pane(pane_id, "@FOCUS_AXIS"),
                _clear_pane(pane_id, "@FOCUS_RESTORE_POSITION"),
            ]
        )
    actions.extend(
        [
            _set_window("@FOCUS_GRID_ACTIVE", "false", window.target),
            _set_window("@FOCUS_GRID_PANE", "", window.target),
            _set_window("@FOCUS_GRID_STASH", "", window.target),
            _set_window("@GRID_EXPANDED", "none", window.target),
            _set_window("@GRID_STASH", "", window.target),
        ]
    )
    if not window.side_focus_active:
        actions.append(_set_window("@FOCUSED", "false", window.target))
    return FocusPlan(FocusAxis.GRID, "unfocus", window.target, focus_pane, tuple(actions))


def plan_focus_side(window: WindowSnapshot, active: PaneSnapshot) -> FocusPlan:
    if window.side_focus_active and window.side_focus_pane == active.pane_id:
        return plan_unfocus_side(window, active)
    if active not in _side_panes(window):
        return FocusPlan(
            FocusAxis.SIDE,
            "noop",
            window.target,
            active.pane_id,
            (),
            "active pane is not a side pane",
        )
    desired = max(1, (sum(p.width for p in window.panes) * SIDE_FOCUS_RATIO) // 100)
    actions = (
        _set_window("@FOCUSED", "true", window.target),
        _set_window("@FOCUS_SIDE_ACTIVE", "true", window.target),
        _set_window("@FOCUS_SIDE_PANE", active.pane_id, window.target),
        _set_window("@SIDE_EXPANDED", "none", window.target),
        FocusAction(("resize-pane", "-t", active.pane_id, "-x", str(desired)), True),
    )
    return FocusPlan(FocusAxis.SIDE, "focus", window.target, active.pane_id, actions)


def plan_unfocus_side(window: WindowSnapshot, active: PaneSnapshot | None = None) -> FocusPlan:
    pane_id = window.side_focus_pane or (active.pane_id if active else "")
    if not window.side_focus_active:
        return FocusPlan(
            FocusAxis.SIDE, "noop", window.target, pane_id, (), "side focus is not active"
        )
    actions: list[FocusAction] = [
        _set_window("@FOCUS_SIDE_ACTIVE", "false", window.target),
        _set_window("@FOCUS_SIDE_PANE", "", window.target),
        _set_window("@SIDE_EXPANDED", "none", window.target),
    ]
    if not window.grid_focus_active:
        actions.append(_set_window("@FOCUSED", "false", window.target))
    return FocusPlan(FocusAxis.SIDE, "unfocus", window.target, pane_id, tuple(actions))


def build_focus_plan(adapter: TmuxAdapter, window: WindowSnapshot, mode: str) -> FocusPlan:
    active = _active_pane(window)
    if mode == "focus-grid":
        return plan_focus_grid(adapter, window, active)
    if mode == "unfocus-grid":
        return plan_unfocus_grid(adapter, window, active)
    if mode == "focus-side":
        return plan_focus_side(window, active)
    if mode == "unfocus-side":
        return plan_unfocus_side(window, active)
    if mode != "toggle":
        raise ValueError(f"unknown focus mode: {mode}")
    if active in _side_panes(window):
        return plan_focus_side(window, active)
    if active.grid_state is GridState.SMALL:
        return plan_focus_grid(adapter, window, active)
    if window.grid_focus_active:
        return plan_unfocus_grid(adapter, window, active)
    if window.side_focus_active:
        return plan_unfocus_side(window, active)
    return FocusPlan(
        FocusAxis.GRID, "noop", window.target, active.pane_id, (), "active pane is not focusable"
    )


def execute_focus_plan(adapter: TmuxAdapter, plan: FocusPlan) -> str:
    if plan.operation == "refuse":
        raise ValueError(plan.reason)
    for action in plan.actions:
        adapter.run(*action.argv, allow_failure=action.allow_failure)
    if plan.operation in {"focus", "unfocus"}:
        # Re-read once: this catches gross tmux failures while keeping the executor simple.
        _show(adapter, plan.window_target, "#{window_name}")
    suffix = f": {plan.reason}" if plan.reason else ""
    return f"{plan.operation} {plan.axis.value} {plan.window_target}{suffix}"


def focus_window(adapter: TmuxAdapter, session_name: str, window_index: int, mode: str) -> str:
    window = build_window_snapshot(adapter, session_name, window_index)
    if window.archetype in {WindowArchetype.LEGION_STACK, WindowArchetype.MECHANICUS_STACK}:
        from .legion import enforce_stack_layout

        active = _active_pane(window)
        return enforce_stack_layout(adapter, window.target, focused_pane=active.pane_id)
    if window.archetype not in {WindowArchetype.PALACE, WindowArchetype.SOMNIUM}:
        return f"noop focus {window.target}: unsupported window {window.window_name}"
    plan = build_focus_plan(adapter, window, mode)
    result = execute_focus_plan(adapter, plan)
    if plan.operation == "unfocus":
        from .revert import enforce_known_window_state

        enforced = enforce_known_window_state(adapter, session_name, window_index)
        if not enforced.ok:
            raise ValueError("; ".join(enforced.violations))
        return f"{result}; restored known state"
    return result
