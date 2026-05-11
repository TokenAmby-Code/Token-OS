from __future__ import annotations

import os
from dataclasses import dataclass

from .labels import canonical_pane_role
from .stack import stack_base_of
from .tmux_adapter import TmuxAdapter

CUSTODES_ROLE = "legion:custodes"
REGIMENT_ROLE = "legion:regiment"
LEGION_COLLAPSED_HEIGHT = 3
LEGION_CUSTODES_RATIO = 40


@dataclass(frozen=True)
class LegionPane:
    pane_id: str
    role: str
    active: bool
    top: int
    height: int


def _show(adapter: TmuxAdapter, target: str, fmt: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", fmt, allow_failure=True).strip()


def _set_window_option(adapter: TmuxAdapter, target: str, option: str, value: str) -> None:
    adapter.run("set-option", "-w", "-t", target, option, value, allow_failure=True)


def _set_pane_option(adapter: TmuxAdapter, pane: str, option: str, value: str) -> None:
    adapter.run("set-option", "-p", "-t", pane, option, value, allow_failure=True)


def _window_base(name: str) -> str:
    return name.split("(", 1)[0]


def _pane_window(adapter: TmuxAdapter, pane: str) -> tuple[str, str, str]:
    raw = _show(adapter, pane, "#{session_name}\t#{window_index}\t#{window_name}")
    if not raw or "\t" not in raw:
        raise ValueError(f"pane not found: {pane}")
    session, window_index, window_name = raw.split("\t", 2)
    return session, window_index, window_name


def _target_for_pane(adapter: TmuxAdapter, pane: str) -> str:
    session, window_index, _ = _pane_window(adapter, pane)
    return f"{session}:{window_index}"


def _legion_panes(adapter: TmuxAdapter, target: str) -> list[LegionPane]:
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{@PANE_ID}",
            "#{pane_active}",
            "#{pane_top}",
            "#{pane_height}",
        ]
    )
    panes: list[LegionPane] = []
    for line in adapter.run("list-panes", "-t", target, "-F", fmt, allow_failure=True).splitlines():
        pane_id, role, active, top, height = line.split("\t")
        panes.append(
            LegionPane(
                pane_id=pane_id,
                role=canonical_pane_role(role),
                active=active == "1",
                top=int(top or 0),
                height=int(height or 0),
            )
        )
    return panes


def _custodes_and_regiments(panes: list[LegionPane]) -> tuple[LegionPane | None, list[LegionPane]]:
    custodes = next((pane for pane in panes if pane.role == CUSTODES_ROLE), None)
    regiments = [pane for pane in panes if pane.role != CUSTODES_ROLE]
    regiments.sort(key=lambda pane: (pane.top, pane.pane_id))
    return custodes, regiments


def _is_legion_window(window_name: str) -> bool:
    return stack_base_of(_window_base(window_name)) == "legion"


def focus_selected(adapter: TmuxAdapter, pane: str) -> str:
    """Make the selected legion regiment the only expanded right-side pane.

    Selecting Custodes intentionally does nothing. This function is idempotent
    and guarded so tmux hooks can call it on every pane selection.
    """
    session, window_index, window_name = _pane_window(adapter, pane)
    if not _is_legion_window(window_name):
        return f"noop legion focus {pane}: not a legion window"

    target = f"{session}:{window_index}"
    if adapter.show_window_option(target, "@LEGION_FOCUS_GUARD") == "true":
        return f"noop legion focus {pane}: guarded"

    panes = _legion_panes(adapter, target)
    selected = next((row for row in panes if row.pane_id == pane), None)
    if selected is None:
        return f"noop legion focus {pane}: pane not in window"
    if selected.role == CUSTODES_ROLE:
        return f"noop legion focus {pane}: custodes"

    return enforce_legion_layout(adapter, target, focused_pane=pane)


def enforce_legion_layout(adapter: TmuxAdapter, target: str, *, focused_pane: str = "") -> str:
    panes = _legion_panes(adapter, target)
    custodes, regiments = _custodes_and_regiments(panes)
    if custodes is None:
        raise ValueError(f"legion window must contain {CUSTODES_ROLE}")
    if not regiments:
        return f"noop legion layout {target}: no regiments"

    if not focused_pane:
        active = next((pane for pane in regiments if pane.active), None)
        focused_pane = active.pane_id if active else regiments[0].pane_id

    if focused_pane == custodes.pane_id:
        return f"noop legion layout {target}: custodes"
    if focused_pane not in {pane.pane_id for pane in regiments}:
        raise ValueError(f"focused pane is not a legion regiment: {focused_pane}")

    win_w = int(_show(adapter, target, "#{window_width}") or "0")
    win_h = int(_show(adapter, target, "#{window_height}") or "0")
    custodes_w = max(1, (win_w * LEGION_CUSTODES_RATIO) // 100)
    collapsed = [pane for pane in regiments if pane.pane_id != focused_pane]
    expanded_h = max(LEGION_COLLAPSED_HEIGHT, win_h - (len(collapsed) * (LEGION_COLLAPSED_HEIGHT + 1)))

    _set_window_option(adapter, target, "@LEGION_FOCUS_GUARD", "true")
    try:
        adapter.run("select-pane", "-t", custodes.pane_id, allow_failure=True)
        adapter.run("set-window-option", "-t", target, "main-pane-width", str(custodes_w), allow_failure=True)
        adapter.run("select-layout", "-t", target, "main-vertical", allow_failure=True)
        adapter.run("resize-pane", "-t", custodes.pane_id, "-x", str(custodes_w), allow_failure=True)

        for regiment in collapsed:
            adapter.run(
                "resize-pane",
                "-t",
                regiment.pane_id,
                "-y",
                str(LEGION_COLLAPSED_HEIGHT),
                allow_failure=True,
            )
        adapter.run("resize-pane", "-t", focused_pane, "-y", str(expanded_h), allow_failure=True)
        adapter.run("select-pane", "-t", focused_pane, allow_failure=True)
        _set_window_option(adapter, target, "@LEGION_FOCUSED_PANE", focused_pane)
    finally:
        _set_window_option(adapter, target, "@LEGION_FOCUS_GUARD", "false")

    return f"focused legion {focused_pane} in {target}"


def add_regiment_pane(
    adapter: TmuxAdapter,
    session: str,
    *,
    cwd: str | None = None,
    window: str = "legion",
) -> str:
    cwd = cwd or os.path.expanduser("~")
    target = f"{session}:{window}"
    names = [
        name.split("(", 1)[0]
        for name in adapter.run("list-windows", "-t", session, "-F", "#{window_name}", allow_failure=True).splitlines()
    ]
    if window not in names:
        adapter.run("new-window", "-t", session, "-n", window, "-d", "-c", cwd)

    panes = _legion_panes(adapter, target)
    custodes, regiments = _custodes_and_regiments(panes)
    if custodes is None:
        first = _show(adapter, target, "#{pane_id}")
        _set_pane_option(adapter, first, "@PANE_ID", CUSTODES_ROLE)
        _set_pane_option(adapter, first, "@PANE_TYPE", "legion")
        custodes = LegionPane(first, CUSTODES_ROLE, False, 0, 0)

    if not regiments:
        win_w = int(_show(adapter, target, "#{window_width}") or "240")
        right_w = max(1, win_w - ((win_w * LEGION_CUSTODES_RATIO) // 100) - 1)
        pane = adapter.run(
            "split-window",
            "-h",
            "-t",
            custodes.pane_id,
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-l",
            str(right_w),
            "-c",
            cwd,
        ).strip()
    else:
        focus = adapter.show_window_option(target, "@LEGION_FOCUSED_PANE") or regiments[0].pane_id
        pane = adapter.run(
            "split-window",
            "-v",
            "-t",
            focus,
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-l",
            str(LEGION_COLLAPSED_HEIGHT),
            "-c",
            cwd,
        ).strip()

    _set_pane_option(adapter, pane, "@PANE_ID", REGIMENT_ROLE)
    _set_pane_option(adapter, pane, "@PANE_TYPE", "legion")
    adapter.run("select-pane", "-T", "regiment", "-t", pane, allow_failure=True)
    enforce_legion_layout(adapter, target, focused_pane=pane)
    return pane
