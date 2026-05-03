"""Workspace construction. Builds a fresh palace + somnium session from empty."""

from __future__ import annotations

import os

from .tmux_adapter import TmuxAdapter

SESSION_NAME = "main"
PALACE_WINDOW = "palace"
SOMNIUM_WINDOW = "somnium"
LEGION_WINDOW = "legion"
MECHANICUS_WINDOW = "mechanicus"
TUI_WINDOW = "tui"

DETACHED_W = 240
DETACHED_H = 60

PALACE_SIDE_RATIO = 20
SOMNIUM_SIDE_RATIO = 33


def _home() -> str:
    return os.path.expanduser("~")


def _imperium_root() -> str:
    return os.environ.get("IMPERIUM", "/Volumes/Imperium")


def _window_dir(window: str) -> str:
    if window == TUI_WINDOW:
        return f"{_imperium_root()}/Token-OS/token-api"
    return _home()


def _set_pane_option(adapter: TmuxAdapter, pane_id: str, option: str, value: str) -> None:
    adapter.run("set-option", "-p", "-t", pane_id, option, value, allow_failure=True)


def _set_window_option(adapter: TmuxAdapter, target: str, option: str, value: str) -> None:
    adapter.run("set-option", "-w", "-t", target, option, value, allow_failure=True)


def _pane_tag(adapter: TmuxAdapter, pane_id: str, tag: str) -> None:
    """Set @PANE_ID and derive @GRID_STATE from the tag suffix."""
    _set_pane_option(adapter, pane_id, "@PANE_ID", tag)
    suffix = tag.split(":", 1)[-1]
    if suffix in {"WW", "EE"}:
        state = "side"
    elif suffix == "MON":
        state = "mini"
    else:
        state = "small"
    _set_pane_option(adapter, pane_id, "@GRID_STATE", state)


def _window_dim(adapter: TmuxAdapter, target: str, fmt: str) -> int:
    return int(adapter.run("display-message", "-t", target, "-p", fmt).strip())


def build_palace_window(adapter: TmuxAdapter, session: str, window: str = PALACE_WINDOW) -> None:
    """Build the 6-pane palace grid: [WW 20%] [2x2] [EE 20%].

    Layout (final, top row → bottom row, left → right):
      pane 1 = WW (full-height side)
      pane 2 = NW grid    pane 4 = NE grid
      pane 3 = SW grid    pane 5 = SE grid
      pane 6 = EE (full-height side)

    Side columns are bare shells in $HOME — no auto-launched program.
    The pane-died hook + tmux-pane-respawn handle restart-on-exit.
    """
    target = f"{session}:{window}"
    wdir = _window_dir(window)

    total_w = _window_dim(adapter, target, "#{window_width}")
    total_h = _window_dim(adapter, target, "#{window_height}")

    usable = total_w - 5  # 5 vertical pane borders for 6 columns
    side_w = (usable * PALACE_SIDE_RATIO) // 100
    center_w = usable - side_w * 2
    center_half = center_w // 2
    half_h = total_h // 2

    # Pane 1 starts as full-width. Carve the left side column off the right.
    adapter.run(
        "split-window",
        "-h",
        "-t",
        f"{target}.1",
        "-l",
        str(total_w - side_w - 1),
        "-c",
        wdir,
    )
    # Pane 1 = left side, pane 2 = rest. Carve right side column off pane 2.
    adapter.run("split-window", "-h", "-t", f"{target}.2", "-l", str(side_w), "-c", wdir)
    # Pane 1 = WW, pane 2 = center, pane 3 = EE. Split center horizontally.
    adapter.run(
        "split-window",
        "-h",
        "-t",
        f"{target}.2",
        "-l",
        str(center_half),
        "-c",
        wdir,
    )
    # Now: 1=WW, 2=center-left, 3=center-right, 4=EE. Vertical splits per column.
    adapter.run("split-window", "-v", "-t", f"{target}.2", "-l", str(half_h), "-c", wdir)
    adapter.run("split-window", "-v", "-t", f"{target}.4", "-l", str(half_h), "-c", wdir)

    _pane_tag(adapter, f"{target}.1", "palace:WW")
    _pane_tag(adapter, f"{target}.2", "palace:NW")
    _pane_tag(adapter, f"{target}.3", "palace:SW")
    _pane_tag(adapter, f"{target}.4", "palace:NE")
    _pane_tag(adapter, f"{target}.5", "palace:SE")
    _pane_tag(adapter, f"{target}.6", "palace:EE")

    for idx in range(1, 7):
        _set_pane_option(adapter, f"{target}.{idx}", "@GRID_RESERVED", "false")

    _set_window_option(adapter, target, "@FOCUSED", "false")
    _set_window_option(adapter, target, "@GRID_EXPANDED", "none")
    _set_window_option(adapter, target, "@SIDE_EXPANDED", "none")
    _set_window_option(adapter, target, "@GRID_STASH", "")

    adapter.run("select-pane", "-t", f"{target}.2")


def build_somnium_window(adapter: TmuxAdapter, session: str, window: str = SOMNIUM_WINDOW) -> None:
    """Build the 5-pane somnium grid: 2x2 + right TUI column.

    Layout (final):
      pane 1 = NW grid    pane 3 = NE grid    pane 5 = EE (TUI monitor)
      pane 2 = SW grid    pane 4 = SE grid
    """
    target = f"{session}:{window}"
    wdir = _window_dir(window)

    total_w = _window_dim(adapter, target, "#{window_width}")
    total_h = _window_dim(adapter, target, "#{window_height}")

    usable = total_w - 2  # pane borders
    right_w = (usable * SOMNIUM_SIDE_RATIO) // 100
    grid_w = usable - right_w
    grid_half = grid_w // 2
    half_h = total_h // 2

    # Split off right column from full-width pane 1.
    adapter.run("split-window", "-h", "-t", f"{target}.1", "-l", str(right_w), "-c", wdir)
    # Split grid area into left/right halves.
    adapter.run("split-window", "-h", "-t", f"{target}.1", "-l", str(grid_half), "-c", wdir)
    # Split each grid column vertically.
    adapter.run("split-window", "-v", "-t", f"{target}.1", "-l", str(half_h), "-c", wdir)
    adapter.run("split-window", "-v", "-t", f"{target}.3", "-l", str(half_h), "-c", wdir)

    _pane_tag(adapter, f"{target}.1", "somnium:NW")
    _pane_tag(adapter, f"{target}.2", "somnium:SW")
    _pane_tag(adapter, f"{target}.3", "somnium:NE")
    _pane_tag(adapter, f"{target}.4", "somnium:SE")
    _pane_tag(adapter, f"{target}.5", "somnium:EE")
    _set_pane_option(adapter, f"{target}.5", "@PANE_TYPE", "tui")

    for idx in range(1, 6):
        _set_pane_option(adapter, f"{target}.{idx}", "@GRID_RESERVED", "false")

    _set_window_option(adapter, target, "@FOCUSED", "false")
    _set_window_option(adapter, target, "@GRID_EXPANDED", "none")
    _set_window_option(adapter, target, "@SIDE_EXPANDED", "none")
    _set_window_option(adapter, target, "@GRID_STASH", "")

    adapter.run("send-keys", "-t", f"{target}.5", "exec tui-pane-guard", "Enter")
    adapter.run("select-pane", "-t", f"{target}.1")


def build_legion_window(adapter: TmuxAdapter, session: str) -> None:
    target = f"{session}:{LEGION_WINDOW}"
    adapter.run(
        "new-window", "-t", session, "-n", LEGION_WINDOW, "-d", "-c", _window_dir(LEGION_WINDOW)
    )
    _pane_tag(adapter, f"{target}.1", "legion:empty")
    _set_pane_option(adapter, f"{target}.1", "@PANE_TYPE", "legion")


def build_mechanicus_window(adapter: TmuxAdapter, session: str) -> None:
    """Build the mechanicus stack window. Pane 1 is the orchestrator anchor
    (future home for the fabricator-general persona), born as a clean shell —
    symmetric with build_legion_window. Worker panes are added by stack.add_stack_pane.
    """
    target = f"{session}:{MECHANICUS_WINDOW}"
    adapter.run(
        "new-window",
        "-t",
        session,
        "-n",
        MECHANICUS_WINDOW,
        "-d",
        "-c",
        _window_dir(MECHANICUS_WINDOW),
    )
    _pane_tag(adapter, f"{target}.1", "mechanicus:anchor")
    _set_pane_option(adapter, f"{target}.1", "@PANE_TYPE", "mechanicus")


def build_tui_window(adapter: TmuxAdapter, session: str) -> None:
    target = f"{session}:{TUI_WINDOW}"
    adapter.run("new-window", "-t", session, "-n", TUI_WINDOW, "-d", "-c", _window_dir(TUI_WINDOW))
    _pane_tag(adapter, f"{target}.1", "tui:1")
    _set_pane_option(adapter, f"{target}.1", "@PANE_TYPE", "tui")
    adapter.run("send-keys", "-t", f"{target}.1", "exec tui-pane-guard", "Enter")


def build_workspace(adapter: TmuxAdapter, session: str = SESSION_NAME) -> None:
    """Build the full somnium workspace from an empty server.

    Idempotent guard: if the session already exists, this is a no-op. The caller
    is responsible for tearing down first via the restart executor.
    """
    if adapter.has_session(session):
        return

    adapter.run(
        "new-session",
        "-d",
        "-s",
        session,
        "-n",
        PALACE_WINDOW,
        "-x",
        str(DETACHED_W),
        "-y",
        str(DETACHED_H),
        "-c",
        _window_dir(PALACE_WINDOW),
    )
    build_palace_window(adapter, session, PALACE_WINDOW)
    adapter.run(
        "new-window", "-t", session, "-n", SOMNIUM_WINDOW, "-d", "-c", _window_dir(SOMNIUM_WINDOW)
    )
    build_somnium_window(adapter, session, SOMNIUM_WINDOW)
    build_legion_window(adapter, session)
    build_mechanicus_window(adapter, session)
    build_tui_window(adapter, session)
    adapter.run("select-window", "-t", f"{session}:{PALACE_WINDOW}")


def attach_workspace(session: str = SESSION_NAME) -> None:
    """Attach an interactive client. Replaces the current process."""
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
