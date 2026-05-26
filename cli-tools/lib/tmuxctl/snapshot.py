from __future__ import annotations

from collections import Counter

from .enums import GridState, PaneKind, WindowArchetype
from .labels import (
    PALACE_GRID_ROLES,
    PALACE_ROLES,
    PALACE_SIDE_ROLES,
    SOMNIUM_GRID_ROLES,
    SOMNIUM_ROLES,
    SOMNIUM_SIDE_ROLES,
    canonical_pane_role,
)
from .models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from .tmux_adapter import TmuxAdapter, TmuxError

AUDIENCE_WINDOW_PAGES = {
    "_palace_audience": "palace",
    "_somnium_audience": "somnium",
    "_legion_audience": "legion",
    "_mechanicus_audience": "mechanicus",
}


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


def _parse_grid_stash(value: str) -> list[str]:
    if not value:
        return []
    pane_ids: list[str] = []
    for entry in value.split(","):
        if not entry:
            continue
        pane_ids.append(entry.split(":", 1)[0])
    return pane_ids


def _infer_archetype(window_name: str) -> WindowArchetype:
    base = window_name.split("(", 1)[0]
    if base == "palace":
        return WindowArchetype.PALACE
    if base == "somnium":
        return WindowArchetype.SOMNIUM
    if base == "legion":
        return WindowArchetype.LEGION_STACK
    if base in {"mechanicus", "mars", "kreig"}:
        return WindowArchetype.MECHANICUS_STACK
    return WindowArchetype.UNKNOWN


def _window_warnings(
    window_name: str,
    pane_roles: list[str],
    pane_ids: list[str],
    focused: bool,
    grid_expanded: str,
    grid_stash: str,
    side_expanded: str,
    grid_focus_active: bool = False,
    grid_focus_stash: str = "",
) -> tuple[str, ...]:
    warnings: list[str] = []
    pane_roles = [canonical_pane_role(role) for role in pane_roles]
    role_counts = Counter(role for role in pane_roles if role)
    visible_panes = set(pane_ids)
    stash_panes = set(_parse_grid_stash(grid_stash))

    duplicated = [role for role, count in role_counts.items() if count > 1]
    if duplicated:
        warnings.append(f"duplicate @PANE_ID values: {', '.join(sorted(duplicated))}")

    expanded_role = None
    if grid_expanded and grid_expanded != "none":
        if grid_expanded in visible_panes:
            for pane_id, pane_role in zip(pane_ids, pane_roles, strict=False):
                if pane_id == grid_expanded:
                    expanded_role = pane_role
                    break
        else:
            warnings.append(f"@GRID_EXPANDED points to missing pane '{grid_expanded}'")

    window_base = window_name.split("(", 1)[0]

    audience_page = AUDIENCE_WINDOW_PAGES.get(window_base)
    if audience_page:
        if len(pane_ids) != 1:
            warnings.append("audience window should contain exactly one pane")
        audience_roles = [role for role in pane_roles if role.startswith("audience:")]
        if len(audience_roles) != 1:
            warnings.append("audience window should contain exactly one audience pane")
        for role in audience_roles:
            if not role.startswith(f"audience:{audience_page}:"):
                warnings.append(f"audience pane role does not match window page: {role}")

    if window_base == "palace":
        required = set(PALACE_ROLES)
        extra = sorted(set(role_counts) - required)
        if extra:
            warnings.append(f"unexpected palace roles: {', '.join(extra)}")
        grid_required = set(PALACE_GRID_ROLES)
        missing_grid = sorted(grid_required - set(role_counts))
        if expanded_role:
            if len(missing_grid) != len(stash_panes):
                warnings.append("expanded palace grid stash does not match missing grid panes")
            elif len(stash_panes) != len(PALACE_GRID_ROLES) - 1:
                warnings.append(
                    f"expanded palace grid should stash exactly {len(PALACE_GRID_ROLES) - 1} panes"
                )
        elif missing_grid and not grid_focus_active:
            warnings.append(f"missing palace grid roles: {', '.join(missing_grid)}")
        elif missing_grid and grid_focus_active:
            focus_stash_count = len([entry for entry in grid_focus_stash.split(",") if entry])
            if focus_stash_count != len(missing_grid):
                warnings.append("focused palace grid stash does not match missing grid panes")

        # @FOCUSED is now a coarse compatibility marker. Typed focus axes decide
        # whether side panes should be visible; partial grid focus intentionally
        # preserves palace sides.

        if not focused:
            side_required = set(PALACE_SIDE_ROLES)
            missing_side = sorted(side_required - set(role_counts))
            if missing_side:
                warnings.append(f"missing palace side roles: {', '.join(missing_side)}")

    if window_base == "somnium":
        required = set(SOMNIUM_ROLES)
        extra = sorted(set(role_counts) - required)
        if extra:
            warnings.append(f"unexpected somnium roles: {', '.join(extra)}")
        grid_required = set(SOMNIUM_GRID_ROLES)
        missing_grid = sorted(grid_required - set(role_counts))
        if expanded_role:
            if len(missing_grid) != len(stash_panes):
                warnings.append("expanded somnium grid stash does not match missing grid panes")
            elif len(stash_panes) != 3:
                warnings.append("expanded somnium grid should stash exactly 3 panes")
        elif missing_grid and not grid_focus_active:
            warnings.append(f"missing somnium grid roles: {', '.join(missing_grid)}")
        elif missing_grid and grid_focus_active:
            focus_stash_count = len([entry for entry in grid_focus_stash.split(",") if entry])
            if focus_stash_count != len(missing_grid):
                warnings.append("focused somnium grid stash does not match missing grid panes")

        if not focused:
            side_required = set(SOMNIUM_SIDE_ROLES)
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
    focused = adapter.show_window_option(target, "@FOCUSED") == "true"
    grid_expanded = adapter.show_window_option(target, "@GRID_EXPANDED") or "none"
    grid_stash = adapter.show_window_option(target, "@GRID_STASH")
    side_expanded = adapter.show_window_option(target, "@SIDE_EXPANDED") or "none"
    grid_focus_active = adapter.show_window_option(target, "@FOCUS_GRID_ACTIVE") == "true"
    grid_focus_pane = adapter.show_window_option(target, "@FOCUS_GRID_PANE")
    grid_focus_stash = adapter.show_window_option(target, "@FOCUS_GRID_STASH")
    side_focus_active = adapter.show_window_option(target, "@FOCUS_SIDE_ACTIVE") == "true"
    side_focus_pane = adapter.show_window_option(target, "@FOCUS_SIDE_PANE")

    panes: list[PaneSnapshot] = []
    pane_roles: list[str] = []
    pane_ids: list[str] = []
    for record in pane_records:
        pane_id = record["pane_id"]
        pane_ids.append(pane_id)
        pane_role = canonical_pane_role(adapter.show_pane_option(pane_id, "@PANE_ID"))
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
                tombstone_target=adapter.show_pane_option(pane_id, "@TOMBSTONE_TARGET"),
                tombstone_source=adapter.show_pane_option(pane_id, "@TOMBSTONE_SOURCE"),
            )
        )

    archetype = _infer_archetype(window_name)
    warnings = _window_warnings(
        window_name=window_name,
        pane_roles=pane_roles,
        pane_ids=pane_ids,
        focused=focused,
        grid_expanded=grid_expanded,
        grid_stash=grid_stash,
        side_expanded=side_expanded,
        grid_focus_active=grid_focus_active,
        grid_focus_stash=grid_focus_stash,
    )
    return WindowSnapshot(
        session_name=session_name,
        window_index=window_index,
        window_name=window_name,
        archetype=archetype,
        focused=focused,
        grid_expanded=grid_expanded,
        grid_stash=grid_stash,
        side_expanded=side_expanded,
        grid_focus_active=grid_focus_active,
        grid_focus_pane=grid_focus_pane,
        grid_focus_stash=grid_focus_stash,
        side_focus_active=side_focus_active,
        side_focus_pane=side_focus_pane,
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
            continue
    return WorkspaceSnapshot(session_name=session_name, windows=tuple(windows))
