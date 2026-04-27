from __future__ import annotations

from collections import Counter

from .enums import GridState, LayoutOrigin, PaneKind, WindowArchetype
from .models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from .tmux_adapter import TmuxAdapter, TmuxError


def _parse_grid_state(value: str) -> GridState:
    try:
        return GridState(value)
    except ValueError:
        return GridState.UNKNOWN


def _parse_pane_kind(value: str) -> PaneKind:
    try:
        return PaneKind(value)
    except ValueError:
        return PaneKind.UNKNOWN


def _parse_layout_origin(value: str) -> LayoutOrigin:
    try:
        return LayoutOrigin(value)
    except ValueError:
        return LayoutOrigin.UNKNOWN


def _parse_grid_stash(value: str) -> list[str]:
    if not value:
        return []
    pane_ids: list[str] = []
    for entry in value.split(","):
        if not entry:
            continue
        pane_ids.append(entry.split(":", 1)[0])
    return pane_ids


def _infer_archetype(window_name: str, _layout_origin: LayoutOrigin) -> WindowArchetype:
    if window_name == "palace":
        return WindowArchetype.PALACE
    if window_name in {"somnium", "bridge"}:
        return WindowArchetype.SOMNIUM
    if window_name == "legion":
        return WindowArchetype.LEGION_STACK
    if window_name in {"mechanicus", "mars", "kreig"}:
        return WindowArchetype.MECHANICUS_STACK
    if window_name == "tui":
        return WindowArchetype.TUI_SINGLE
    return WindowArchetype.UNKNOWN


def _window_warnings(
    window_name: str,
    layout_origin: LayoutOrigin,
    pane_roles: list[str],
    pane_ids: list[str],
    focused: bool,
    grid_expanded: str,
    grid_stash: str,
    side_expanded: str,
) -> tuple[str, ...]:
    warnings: list[str] = []
    role_counts = Counter(role for role in pane_roles if role)
    visible_panes = set(pane_ids)
    stash_panes = set(_parse_grid_stash(grid_stash))

    if window_name in {"palace", "somnium", "bridge"} and layout_origin == LayoutOrigin.UNKNOWN:
        warnings.append("missing @LAYOUT_ORIGIN on managed window")

    duplicated = [role for role, count in role_counts.items() if count > 1]
    if duplicated:
        warnings.append(f"duplicate @PANE_ID values: {', '.join(sorted(duplicated))}")

    expanded_role = None
    if grid_expanded and grid_expanded != "none":
        if grid_expanded in visible_panes:
            for pane_id, pane_role in zip(pane_ids, pane_roles):
                if pane_id == grid_expanded:
                    expanded_role = pane_role
                    break
        else:
            warnings.append(f"@GRID_EXPANDED points to missing pane '{grid_expanded}'")

    if window_name == "palace":
        grid_required = {"palace:TL", "palace:TR", "palace:BL", "palace:BR"}
        missing_grid = sorted(grid_required - set(role_counts))
        if expanded_role:
            if len(missing_grid) != len(stash_panes):
                warnings.append("expanded palace grid stash does not match missing grid panes")
            elif len(stash_panes) != 3:
                warnings.append("expanded palace grid should stash exactly 3 panes")
        elif missing_grid:
            warnings.append(f"missing palace grid roles: {', '.join(missing_grid)}")

        if focused:
            focused_sides = sorted({"palace:SL", "palace:SR"} & set(role_counts))
            if focused_sides:
                warnings.append(
                    f"focused palace should not expose side roles: {', '.join(focused_sides)}"
                )

        if not focused and side_expanded == "none":
            side_required = {"palace:SL", "palace:SR"}
            missing_sides = sorted(side_required - set(role_counts))
            if missing_sides:
                warnings.append(f"missing palace side roles: {', '.join(missing_sides)}")

    if window_name in {"somnium", "bridge"}:
        grid_required = {"somnium:TL", "somnium:TR", "somnium:BL", "somnium:BR"}
        missing_grid = sorted(grid_required - set(role_counts))
        if expanded_role:
            if len(missing_grid) != len(stash_panes):
                warnings.append("expanded somnium grid stash does not match missing grid panes")
            elif len(stash_panes) != 3:
                warnings.append("expanded somnium grid should stash exactly 3 panes")
        elif missing_grid:
            warnings.append(f"missing somnium grid roles: {', '.join(missing_grid)}")

        if focused and "somnium:SR" in role_counts:
            warnings.append("focused somnium should not expose somnium:SR")

        if not focused:
            side_required = {"somnium:SR"}
            missing_side = sorted(side_required - set(role_counts))
            if missing_side:
                warnings.append(f"missing somnium side roles: {', '.join(missing_side)}")

    if expanded_role and not grid_stash:
        warnings.append("grid expanded is set but @GRID_STASH is empty")

    if grid_stash and not expanded_role:
        warnings.append("@GRID_STASH is set but @GRID_EXPANDED does not point to a visible pane")

    if side_expanded and side_expanded != "none":
        if side_expanded not in visible_panes:
            warnings.append(f"@SIDE_EXPANDED points to missing pane '{side_expanded}'")

    return tuple(warnings)


def build_window_snapshot(
    adapter: TmuxAdapter, session_name: str, window_index: int
) -> WindowSnapshot:
    target = f"{session_name}:{window_index}"
    pane_records = adapter.list_panes(target)
    if not pane_records:
        raise ValueError(f"window has no panes: {target}")

    window_name = pane_records[0]["window_name"]
    layout_origin = _parse_layout_origin(adapter.show_window_option(target, "@LAYOUT_ORIGIN"))
    focused = adapter.show_window_option(target, "@FOCUSED") == "true"
    grid_expanded = adapter.show_window_option(target, "@GRID_EXPANDED") or "none"
    grid_stash = adapter.show_window_option(target, "@GRID_STASH")
    side_expanded = adapter.show_window_option(target, "@SIDE_EXPANDED") or "none"

    panes: list[PaneSnapshot] = []
    pane_roles: list[str] = []
    pane_ids: list[str] = []
    for record in pane_records:
        pane_id = record["pane_id"]
        pane_ids.append(pane_id)
        pane_role = adapter.show_pane_option(pane_id, "@PANE_ID")
        pane_roles.append(pane_role)
        panes.append(
            PaneSnapshot(
                pane_id=pane_id,
                session_name=record["session_name"],
                window_index=int(record["window_index"]),
                window_name=record["window_name"],
                pane_index=int(record["pane_index"]),
                width=int(record["width"]),
                height=int(record["height"]),
                current_command=record["current_command"],
                tty=record["tty"],
                pane_role=pane_role,
                grid_state=_parse_grid_state(adapter.show_pane_option(pane_id, "@GRID_STATE")),
                pane_kind=_parse_pane_kind(adapter.show_pane_option(pane_id, "@PANE_TYPE")),
                reserved=adapter.show_pane_option(pane_id, "@GRID_RESERVED") == "true",
                active=record["active"] == "1",
            )
        )

    archetype = _infer_archetype(window_name, layout_origin)
    warnings = _window_warnings(
        window_name=window_name,
        layout_origin=layout_origin,
        pane_roles=pane_roles,
        pane_ids=pane_ids,
        focused=focused,
        grid_expanded=grid_expanded,
        grid_stash=grid_stash,
        side_expanded=side_expanded,
    )

    return WindowSnapshot(
        session_name=session_name,
        window_index=window_index,
        window_name=window_name,
        archetype=archetype,
        layout_origin=layout_origin,
        focused=focused,
        grid_expanded=grid_expanded,
        grid_stash=grid_stash,
        side_expanded=side_expanded,
        panes=tuple(sorted(panes, key=lambda pane: pane.pane_index)),
        warnings=warnings,
    )


def build_workspace_snapshot(adapter: TmuxAdapter, session_name: str) -> WorkspaceSnapshot:
    windows = []
    for record in adapter.list_windows(session_name):
        try:
            windows.append(
                build_window_snapshot(adapter, session_name, int(record["window_index"]))
            )
        except (TmuxError, ValueError):
            # Stash windows can disappear mid-scan after a retract/normalize.
            continue
    return WorkspaceSnapshot(session_name=session_name, windows=tuple(windows))
