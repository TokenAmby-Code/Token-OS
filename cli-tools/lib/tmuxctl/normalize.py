from __future__ import annotations

from .tmux_adapter import TmuxAdapter

GRID_STATE_SMALL = "small"
GRID_STATE_SIDE = "side"


def _window_base(window_name: str) -> str:
    return window_name.split("(", 1)[0]


def _show(adapter: TmuxAdapter, target: str, fmt: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", fmt).strip()


def _window_option(adapter: TmuxAdapter, target: str, option: str) -> str:
    return adapter.show_window_option(target, option)


def _set_window_option(adapter: TmuxAdapter, target: str, option: str, value: str) -> None:
    adapter.run("set-option", "-w", "-t", target, option, value)


def _set_pane_option(adapter: TmuxAdapter, pane_id: str, option: str, value: str) -> None:
    adapter.run("set-option", "-p", "-t", pane_id, option, value)


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
    rows: list[dict[str, str]] = []
    for line in adapter.run("list-panes", "-t", target, "-F", fmt).splitlines():
        pane_id, pane_role, grid_state, left, top, width, height = line.split("\t")
        rows.append(
            {
                "pane_id": pane_id,
                "pane_role": pane_role,
                "grid_state": grid_state,
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            }
        )
    return rows


def _grid_panes(adapter: TmuxAdapter, target: str) -> list[dict[str, str]]:
    return [row for row in _pane_rows(adapter, target) if row["grid_state"] == GRID_STATE_SMALL]


def _side_panes(adapter: TmuxAdapter, target: str) -> list[dict[str, str]]:
    return [row for row in _pane_rows(adapter, target) if row["grid_state"] == GRID_STATE_SIDE]


def _infer_layout_origin(window_base: str, side_count: int) -> str:
    if window_base in {"somnium", "bridge"}:
        return "mac"
    if window_base == "palace":
        if side_count >= 2:
            return "wsl"
        if side_count == 1:
            return "mac"
    return ""


def _current_path(adapter: TmuxAdapter, target: str) -> str:
    try:
        return _show(adapter, target, "#{pane_current_path}") or "~"
    except Exception:
        return "~"


def _first_pane_id(adapter: TmuxAdapter, target: str) -> str:
    return adapter.run("list-panes", "-t", target, "-F", "#{pane_id}").splitlines()[0]


def _last_pane_id(adapter: TmuxAdapter, target: str) -> str:
    return adapter.run("list-panes", "-t", target, "-F", "#{pane_id}").splitlines()[-1]


def _pane_tags(adapter: TmuxAdapter, target: str) -> list[str]:
    return adapter.run("list-panes", "-t", target, "-F", "#{@PANE_ID}").splitlines()


def _split_window(
    adapter: TmuxAdapter,
    target: str,
    before: bool,
    size: int,
    path: str,
) -> str:
    args = ["split-window", "-P", "-F", "#{pane_id}", "-c", path, "-l", str(size)]
    args.extend(["-f", "-h"])
    if before:
        args.append("-b")
    args.extend(["-t", target])
    return adapter.run(*args).strip()


def _ensure_palace_side_slots(adapter: TmuxAdapter, target: str, win_w: int) -> None:
    side_w = ((win_w - 5) * 20) // 100
    pane_tags = set(_pane_tags(adapter, target))
    path = _current_path(adapter, target)

    if "palace:SL" not in pane_tags:
        new_left = _split_window(adapter, _first_pane_id(adapter, target), True, side_w, path)
        _set_pane_option(adapter, new_left, "@PANE_ID", "palace:SL")
        _set_pane_option(adapter, new_left, "@GRID_STATE", GRID_STATE_SIDE)
        _set_pane_option(adapter, new_left, "@GRID_RESERVED", "false")

    pane_tags = set(_pane_tags(adapter, target))
    if "palace:SR" not in pane_tags:
        new_right = _split_window(adapter, _last_pane_id(adapter, target), False, side_w, path)
        _set_pane_option(adapter, new_right, "@PANE_ID", "palace:SR")
        _set_pane_option(adapter, new_right, "@GRID_STATE", GRID_STATE_SIDE)
        _set_pane_option(adapter, new_right, "@GRID_RESERVED", "false")


def _ensure_somnium_side_slot(adapter: TmuxAdapter, target: str, win_w: int) -> None:
    pane_tags = set(_pane_tags(adapter, target))
    if "somnium:SR" in pane_tags:
        return

    side_w = ((win_w - 2) * 33) // 100
    path = _current_path(adapter, target)
    new_right = _split_window(adapter, _last_pane_id(adapter, target), False, side_w, path)
    _set_pane_option(adapter, new_right, "@PANE_ID", "somnium:SR")
    _set_pane_option(adapter, new_right, "@GRID_STATE", GRID_STATE_SIDE)
    _set_pane_option(adapter, new_right, "@GRID_RESERVED", "false")
    _set_pane_option(adapter, new_right, "@PANE_TYPE", "tui")
    adapter.run("send-keys", "-t", new_right, "exec tui-pane-guard", "Enter")


def _reset_side_columns(adapter: TmuxAdapter, side_panes: list[dict[str, str]], win_w: int, layout_origin: str) -> None:
    if not side_panes:
        return

    if layout_origin == "wsl":
        desired = ((win_w - 5) * 20) // 100
    elif layout_origin == "mac":
        desired = ((win_w - 2) * 33) // 100
    else:
        return

    for pane in side_panes:
        adapter.run("resize-pane", "-t", pane["pane_id"], "-x", str(desired))


def _drop_side_panes(adapter: TmuxAdapter, side_panes: list[dict[str, str]]) -> None:
    for pane in side_panes:
        adapter.run("kill-pane", "-t", pane["pane_id"])


def _rebalance_grid(adapter: TmuxAdapter, target: str) -> None:
    grid_panes = _grid_panes(adapter, target)
    if len(grid_panes) < 2:
        return

    grid_left = min(int(pane["left"]) for pane in grid_panes)
    grid_right = max(int(pane["left"]) + int(pane["width"]) for pane in grid_panes)
    grid_top = min(int(pane["top"]) for pane in grid_panes)
    grid_bottom = max(int(pane["top"]) + int(pane["height"]) for pane in grid_panes)
    grid_w = grid_right - grid_left
    grid_h = grid_bottom - grid_top
    even_w = (grid_w - 1) // 2
    even_h = (grid_h - 1) // 2

    top_panes = [pane for pane in grid_panes if int(pane["top"]) == grid_top]
    origin_top = next((pane["pane_id"] for pane in top_panes if int(pane["left"]) == grid_left), "")
    far_top = next((pane["pane_id"] for pane in top_panes if int(pane["left"]) != grid_left), "")

    if origin_top:
        adapter.run("resize-pane", "-t", origin_top, "-x", str(even_w))
        adapter.run("resize-pane", "-t", origin_top, "-y", str(even_h))
    if far_top:
        adapter.run("resize-pane", "-t", far_top, "-y", str(even_h))


def _restore_expanded_grid(adapter: TmuxAdapter, session_name: str, target: str, grid_expanded: str, grid_stash: str) -> bool:
    if grid_expanded == "none" or not grid_stash:
        return False

    entries = [entry for entry in grid_stash.split(",") if entry]
    pane_specs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        pane_id, col, row = entry.split(":")
        pane_specs.append((pane_id, col, row))
        seen.add((col, row))

    if not adapter.run("display-message", "-t", grid_expanded, "-p", "#{pane_id}", allow_failure=True).strip():
        raise ValueError(f"expanded pane no longer exists: {grid_expanded}")

    exp_col = exp_row = ""
    for col in ("0", "1"):
        for row in ("0", "1"):
            if (col, row) not in seen:
                exp_col, exp_row = col, row
                break
        if exp_col:
            break

    v_partner = h_partner = diagonal = ""
    for pane_id, col, row in pane_specs:
        exists = adapter.run("display-message", "-t", pane_id, "-p", "#{pane_id}", allow_failure=True).strip()
        if not exists:
            raise ValueError(f"stashed pane no longer exists: {pane_id}")
        if col != exp_col and row == exp_row:
            v_partner = pane_id
        elif col == exp_col and row != exp_row:
            h_partner = pane_id
        elif col != exp_col and row != exp_row:
            diagonal = pane_id

    if v_partner:
        args = ["join-pane", "-d", "-h", "-t", grid_expanded, "-s", v_partner]
        if exp_col != "0":
            args.insert(3, "-b")
        adapter.run(*args)

    if h_partner:
        args = ["join-pane", "-d", "-v", "-t", grid_expanded, "-s", h_partner]
        if exp_row != "0":
            args.insert(3, "-b")
        adapter.run(*args)

    if diagonal and v_partner:
        args = ["join-pane", "-d", "-v", "-t", v_partner, "-s", diagonal]
        if exp_row != "0":
            args.insert(3, "-b")
        adapter.run(*args)

    for pane_id in (v_partner, h_partner, diagonal):
        if pane_id:
            _set_pane_option(adapter, pane_id, "@GRID_STATE", GRID_STATE_SMALL)

    _set_window_option(adapter, target, "@GRID_EXPANDED", "none")
    _set_window_option(adapter, target, "@GRID_STASH", "")

    stash_window = _window_base(_show(adapter, target, "#{window_name}"))
    stash_name = f"_stash_{stash_window}"
    existing = adapter.run("list-windows", "-t", session_name, "-F", "#{window_name}", allow_failure=True).splitlines()
    if stash_name in existing:
        pane_count = len(adapter.run("list-panes", "-t", f"{session_name}:{stash_name}", "-F", "#{pane_id}", allow_failure=True).splitlines())
        if pane_count <= 1:
            adapter.run("kill-window", "-t", f"{session_name}:{stash_name}", allow_failure=True)

    return True


def normalize_window(adapter: TmuxAdapter, session_name: str, window_index: int) -> str:
    target = f"{session_name}:{window_index}"
    window_name = _show(adapter, target, "#{window_name}")
    window_base = _window_base(window_name)

    grid_expanded = _window_option(adapter, target, "@GRID_EXPANDED") or "none"
    grid_stash = _window_option(adapter, target, "@GRID_STASH")
    restored = _restore_expanded_grid(adapter, session_name, target, grid_expanded, grid_stash)

    focused = (_window_option(adapter, target, "@FOCUSED") or "false") == "true"
    side_expanded = _window_option(adapter, target, "@SIDE_EXPANDED") or "none"
    win_w = int(_show(adapter, target, "#{window_width}"))

    side_panes = _side_panes(adapter, target)
    layout_origin = _window_option(adapter, target, "@LAYOUT_ORIGIN")
    if not layout_origin:
        layout_origin = _infer_layout_origin(window_base, len(side_panes))
        if layout_origin:
            _set_window_option(adapter, target, "@LAYOUT_ORIGIN", layout_origin)

    if not focused:
        if window_base == "palace" and layout_origin == "wsl" and side_expanded == "none" and len(side_panes) < 2:
            _ensure_palace_side_slots(adapter, target, win_w)
        if window_base in {"somnium", "bridge"} and layout_origin == "mac" and side_expanded == "none" and len(side_panes) < 1:
            _ensure_somnium_side_slot(adapter, target, win_w)
    else:
        _drop_side_panes(adapter, side_panes)

    side_panes = _side_panes(adapter, target)
    _reset_side_columns(adapter, side_panes, win_w, layout_origin)
    _rebalance_grid(adapter, target)

    _set_window_option(adapter, target, "@GRID_EXPANDED", "none")
    _set_window_option(adapter, target, "@SIDE_EXPANDED", "none")
    _set_window_option(adapter, target, "@GRID_STASH", "")

    if restored:
        return f"normalized {target} via restore"
    return f"normalized {target}"
