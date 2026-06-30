from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .enums import GridState, WindowArchetype
from .labels import (
    PALACE_ROLES,
    PALACE_SIDE_ROLES,
    SOMNIUM_ROLES,
    SOMNIUM_SIDE_ROLES,
)
from .layout import WORKSPACE_LAYOUT
from .models import WindowSnapshot
from .snapshot import build_window_snapshot
from .tmux_adapter import TmuxAdapter

TRANSIENT_WINDOW_PREFIXES = ("_stash_", "_fstash_", "_focus_stash_")
TRANSIENT_WINDOW_OPTIONS = {
    "@FOCUSED": "false",
    "@GRID_EXPANDED": "none",
    "@GRID_STASH": "",
    "@SIDE_EXPANDED": "none",
    "@GENERIC_EXPANDED": "none",
    "@GENERIC_STASH": "",
    "@FOCUS_GRID_ACTIVE": "false",
    "@FOCUS_GRID_PANE": "",
    "@FOCUS_GRID_STASH": "",
    "@FOCUS_SIDE_ACTIVE": "false",
    "@FOCUS_SIDE_PANE": "",
}
TRANSIENT_PANE_OPTIONS = (
    "@FOCUS_SOURCE_WINDOW",
    "@FOCUS_SOURCE_ROLE",
    "@FOCUS_AXIS",
    "@FOCUS_RESTORE_POSITION",
)


class EnforcementMode(str, Enum):
    REPAIR = "repair"
    REFUSE = "refuse"


@dataclass(frozen=True)
class EnforcementResult:
    target: str
    archetype: WindowArchetype
    ok: bool
    repaired: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [
            f"enforce {self.target} [{self.archetype.value}] ok={'true' if self.ok else 'false'}"
        ]
        for item in self.repaired:
            lines.append(f"  repaired: {item}")
        for item in self.violations:
            lines.append(f"  ! {item}")
        return "\n".join(lines)


def _show(adapter: TmuxAdapter, target: str, fmt: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", fmt).strip()


def _window_base(name: str) -> str:
    return name.split("(", 1)[0]


def _set_window_option(adapter: TmuxAdapter, target: str, option: str, value: str) -> None:
    adapter.run("set-option", "-w", "-t", target, option, value, allow_failure=True)


def _set_pane_option(adapter: TmuxAdapter, target: str, option: str, value: str) -> None:
    adapter.run("set-option", "-p", "-t", target, option, value, allow_failure=True)


def _clear_pane_option(adapter: TmuxAdapter, target: str, option: str) -> None:
    adapter.run("set-option", "-pu", "-t", target, option, allow_failure=True)


def is_transient_window_name(window_name: str) -> bool:
    base = _window_base(window_name)
    return base.startswith(TRANSIENT_WINDOW_PREFIXES)


def cleanup_transient_windows(adapter: TmuxAdapter, session_name: str) -> tuple[str, ...]:
    removed: list[str] = []
    for record in adapter.list_windows(session_name):
        name = record["window_name"]
        if not is_transient_window_name(name):
            continue
        adapter.run("kill-window", "-t", f"{session_name}:{name}", allow_failure=True)
        removed.append(name)
    return tuple(removed)


def _pane_rows(adapter: TmuxAdapter, target: str) -> list[dict[str, str]]:
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{@PANE_ID}",
            "#{@GRID_STATE}",
            "#{pane_left}",
            "#{pane_top}",
            "#{pane_width}",
            "#{pane_height}",
        ]
    )
    rows = []
    for line in adapter.run("list-panes", "-t", target, "-F", fmt, allow_failure=True).splitlines():
        pane_id, role, state, left, top, width, height = line.split("\t")
        rows.append(
            {
                "pane_id": pane_id,
                "role": role,
                "state": state,
                "left": int(left or 0),
                "top": int(top or 0),
                "width": int(width or 0),
                "height": int(height or 0),
            }
        )
    return rows


def _validate(window: WindowSnapshot) -> list[str]:
    roles = [p.pane_role for p in window.panes if p.pane_role]
    role_set = set(roles)
    if window.archetype is WindowArchetype.PALACE:
        required = set(PALACE_ROLES)
    elif window.archetype is WindowArchetype.SOMNIUM:
        required = set(SOMNIUM_ROLES)
    else:
        return []
    missing = sorted(required - role_set)
    extra = sorted(role_set - required)
    duplicates = sorted(role for role in role_set if roles.count(role) > 1)
    tui_panes = [p.pane_id for p in window.panes if p.pane_kind.value == "tui"]
    extra_focus = []
    if window.grid_focus_active or window.grid_focus_stash or window.grid_focus_pane:
        extra_focus.append("grid focus markers")
    if window.side_focus_active or window.side_focus_pane:
        extra_focus.append("side focus markers")
    out = []
    if missing:
        out.append(f"missing roles: {', '.join(missing)}")
    if extra:
        out.append(f"unexpected roles: {', '.join(extra)}")
    if duplicates:
        out.append(f"duplicate roles: {', '.join(duplicates)}")
    if len(roles) != len(required):
        out.append(f"expected exactly {len(required)} canonical panes, found {len(roles)}")
    if tui_panes:
        out.append(f"default tui panes are forbidden here: {', '.join(tui_panes)}")
    if extra_focus:
        out.append(f"transient markers still set: {', '.join(extra_focus)}")
    return out


def _resize_side_columns(
    adapter: TmuxAdapter, rows: list[dict[str, str]], win_w: int, *, archetype: WindowArchetype
) -> None:
    if archetype is WindowArchetype.PALACE:
        desired = WORKSPACE_LAYOUT.palace.side_width(win_w)
        side_roles = set(PALACE_SIDE_ROLES)
    elif archetype is WindowArchetype.SOMNIUM:
        desired = WORKSPACE_LAYOUT.somnium.west_width(win_w)
        side_roles = set(SOMNIUM_SIDE_ROLES)
    else:
        return
    for row in rows:
        if row["role"] in side_roles or row["state"] == GridState.SIDE.value:
            _set_pane_option(adapter, row["pane_id"], "@GRID_STATE", GridState.SIDE.value)
            adapter.run(
                "resize-pane", "-t", row["pane_id"], "-x", str(max(1, desired)), allow_failure=True
            )


def _rebalance_grid(adapter: TmuxAdapter, rows: list[dict[str, str]]) -> None:
    grid = [r for r in rows if r["state"] == GridState.SMALL.value]
    if len(grid) != 4:
        if len(grid) == 2:
            north = next((r for r in grid if r["role"].endswith(":N")), None)
            if north:
                top = min(r["top"] for r in grid)
                bottom = max(r["top"] + r["height"] for r in grid)
                even_h = max(1, (bottom - top - 1) // 2)
                adapter.run(
                    "resize-pane", "-t", north["pane_id"], "-y", str(even_h), allow_failure=True
                )
        return
    left = min(r["left"] for r in grid)
    right = max(r["left"] + r["width"] for r in grid)
    top = min(r["top"] for r in grid)
    bottom = max(r["top"] + r["height"] for r in grid)
    even_w = max(1, (right - left - 1) // 2)
    even_h = max(1, (bottom - top - 1) // 2)
    nw = min(grid, key=lambda r: (r["top"], r["left"]))
    ne_candidates = [r for r in grid if r is not nw and r["top"] == nw["top"]]
    adapter.run("resize-pane", "-t", nw["pane_id"], "-x", str(even_w), allow_failure=True)
    adapter.run("resize-pane", "-t", nw["pane_id"], "-y", str(even_h), allow_failure=True)
    if ne_candidates:
        adapter.run(
            "resize-pane", "-t", ne_candidates[0]["pane_id"], "-y", str(even_h), allow_failure=True
        )


def enforce_known_window_state(
    adapter: TmuxAdapter,
    session_name: str,
    window_index: int,
    *,
    cleanup_stash_windows: bool = True,
    mode: EnforcementMode = EnforcementMode.REPAIR,
) -> EnforcementResult:
    target = f"{session_name}:{window_index}"
    before = build_window_snapshot(adapter, session_name, window_index)
    violations = _validate(before)
    if mode is EnforcementMode.REFUSE and violations:
        return EnforcementResult(target, before.archetype, False, violations=tuple(violations))

    repaired: list[str] = []
    for pane in before.panes:
        for option in TRANSIENT_PANE_OPTIONS:
            _clear_pane_option(adapter, pane.pane_id, option)
    repaired.append("cleared pane focus metadata")

    for option, value in TRANSIENT_WINDOW_OPTIONS.items():
        _set_window_option(adapter, target, option, value)
    repaired.append("cleared transient window metadata")

    win_w = int(_show(adapter, target, "#{window_width}"))
    rows = _pane_rows(adapter, target)
    _resize_side_columns(adapter, rows, win_w, archetype=before.archetype)
    rows = _pane_rows(adapter, target)
    _rebalance_grid(adapter, rows)
    repaired.append("restored canonical pane ratios")

    if cleanup_stash_windows:
        removed = cleanup_transient_windows(adapter, session_name)
        if removed:
            repaired.append(f"removed transient windows: {', '.join(removed)}")

    after = build_window_snapshot(adapter, session_name, window_index)
    post = _validate(after)
    return EnforcementResult(
        target=target,
        archetype=after.archetype,
        ok=not post,
        repaired=tuple(repaired),
        violations=tuple(post),
    )
