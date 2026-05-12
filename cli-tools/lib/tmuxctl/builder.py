"""Workspace construction. Builds a fresh managed tmux session from empty."""

from __future__ import annotations

import os

from .layout import WORKSPACE_LAYOUT
from .tmux_adapter import TmuxAdapter

SESSION_NAME = "main"
PALACE_WINDOW = "palace"
SOMNIUM_WINDOW = "somnium"
LEGION_WINDOW = "legion"
MECHANICUS_WINDOW = "mechanicus"
TUI_WINDOW = "tui"

DETACHED_W = 240
DETACHED_H = 60


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
    if suffix in {"W", "E", "WW", "EE"}:
        state = "side"
    elif suffix == "MON":
        state = "mini"
    else:
        state = "small"
    _set_pane_option(adapter, pane_id, "@GRID_STATE", state)


def _window_dim(adapter: TmuxAdapter, target: str, fmt: str) -> int:
    return int(adapter.run("display-message", "-t", target, "-p", fmt).strip())


def _pane_id(adapter: TmuxAdapter, target: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", "#{pane_id}").strip()


def _split_pane(
    adapter: TmuxAdapter,
    target: str,
    *args: str,
    cwd: str,
) -> str:
    return adapter.run(
        "split-window",
        *args,
        "-t",
        target,
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-c",
        cwd,
    ).strip()


def build_palace_window(adapter: TmuxAdapter, session: str, window: str = PALACE_WINDOW) -> None:
    """Build the 4-pane palace H layout: [W 30%] [N/S 40%] [E 30%].

    Layout:
      W = full-height west side
      N = center north
      S = center south
      E = full-height east side

    Side columns are bare shells in $HOME — no auto-launched program.
    The pane-died hook + tmux-pane-respawn handle restart-on-exit.
    """
    target = f"{session}:{window}"
    wdir = _window_dir(window)

    total_w = _window_dim(adapter, target, "#{window_width}")
    total_h = _window_dim(adapter, target, "#{window_height}")

    layout = WORKSPACE_LAYOUT.palace
    side_w = layout.side_width(total_w)
    half_h = total_h // 2

    west = _pane_id(adapter, f"{target}.1")
    center = _split_pane(
        adapter,
        west,
        "-h",
        "-l",
        str(layout.center_plus_east_split_width(total_w)),
        cwd=wdir,
    )
    east = _split_pane(adapter, center, "-h", "-l", str(side_w), cwd=wdir)
    south = _split_pane(adapter, center, "-v", "-l", str(half_h), cwd=wdir)

    _pane_tag(adapter, west, "palace:W")
    _pane_tag(adapter, center, "palace:N")
    _pane_tag(adapter, south, "palace:S")
    _pane_tag(adapter, east, "palace:E")

    for pane_id in (west, center, south, east):
        _set_pane_option(adapter, pane_id, "@GRID_RESERVED", "false")

    _set_window_option(adapter, target, "@FOCUSED", "false")
    _set_window_option(adapter, target, "@GRID_EXPANDED", "none")
    _set_window_option(adapter, target, "@SIDE_EXPANDED", "none")
    _set_window_option(adapter, target, "@GRID_STASH", "")

    adapter.run("select-pane", "-t", center)


def build_somnium_window(adapter: TmuxAdapter, session: str, window: str = SOMNIUM_WINDOW) -> None:
    """Build the 5-pane somnium layout: left side rail W + right 2x2.

    Layout (final):
      W  = full-height west pane
      N  = right grid north-west    NE = right grid north-east
      S  = right grid south-west    SE = right grid south-east
    """
    target = f"{session}:{window}"
    wdir = _window_dir(window)

    total_w = _window_dim(adapter, target, "#{window_width}")
    total_h = _window_dim(adapter, target, "#{window_height}")

    layout = WORKSPACE_LAYOUT.somnium
    _, east_grid_w = layout.grid_column_widths(total_w)
    half_h = total_h // 2

    west = _pane_id(adapter, f"{target}.1")
    right = _split_pane(
        adapter,
        west,
        "-h",
        "-l",
        str(layout.right_grid_split_width(total_w)),
        cwd=wdir,
    )
    ne = _split_pane(adapter, right, "-h", "-l", str(east_grid_w), cwd=wdir)
    south = _split_pane(adapter, right, "-v", "-l", str(half_h), cwd=wdir)
    se = _split_pane(adapter, ne, "-v", "-l", str(half_h), cwd=wdir)

    _pane_tag(adapter, west, "somnium:W")
    _pane_tag(adapter, right, "somnium:N")
    _pane_tag(adapter, south, "somnium:S")
    _pane_tag(adapter, ne, "somnium:NE")
    _pane_tag(adapter, se, "somnium:SE")

    for pane_id in (west, right, south, ne, se):
        _set_pane_option(adapter, pane_id, "@GRID_RESERVED", "false")

    _set_window_option(adapter, target, "@FOCUSED", "false")
    _set_window_option(adapter, target, "@GRID_EXPANDED", "none")
    _set_window_option(adapter, target, "@SIDE_EXPANDED", "none")
    _set_window_option(adapter, target, "@GRID_STASH", "")

    adapter.run("select-pane", "-t", west)


def build_legion_window(adapter: TmuxAdapter, session: str) -> None:
    """Build the legion stack window.

    Pane 1 is the Custodes orchestrator slot. If that orchestrator is promoted
    to an audience surface, this pane becomes its tombstone.
    """
    target = f"{session}:{LEGION_WINDOW}"
    adapter.run(
        "new-window",
        "-t",
        session,
        "-n",
        LEGION_WINDOW,
        "-d",
        "-c",
        _window_dir(LEGION_WINDOW),
    )
    _pane_tag(adapter, f"{target}.1", "legion:custodes")
    _set_pane_option(adapter, f"{target}.1", "@PANE_TYPE", "legion")


def build_mechanicus_window(adapter: TmuxAdapter, session: str) -> None:
    """Build the mechanicus stack window.

    Pane 1 is the Fabricator-General orchestrator slot. If that orchestrator is
    promoted to an audience surface, this pane becomes its tombstone. Worker
    panes are added by stack.add_stack_pane.
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
    _pane_tag(adapter, f"{target}.1", "mechanicus:fabricator-general")
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
    adapter.run("select-window", "-t", f"{session}:{PALACE_WINDOW}")


def attach_workspace(session: str = SESSION_NAME) -> None:
    """Attach an interactive client. Replaces the current process."""
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
