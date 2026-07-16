"""Workspace construction. Builds a fresh managed tmux session from empty."""

from __future__ import annotations

import os

from .invariants import assert_window_build_contract, side_roles_for
from .layout import WORKSPACE_LAYOUT
from .tmux_adapter import TmuxAdapter

SESSION_NAME = "main"
PALACE_WINDOW = "palace"
SOMNIUM_WINDOW = "somnium"
COUNCIL_WINDOW = "council"
MECHANICUS_WINDOW = "mechanicus"
RESERVISTS_WINDOW = "reservists"

DETACHED_W = WORKSPACE_LAYOUT.column.reference_total_width
DETACHED_H = 60


def _prepare_window_geometry(adapter: TmuxAdapter, target: str) -> None:
    if not hasattr(adapter, "list_panes"):
        return
    adapter.run(
        "resize-window",
        "-t",
        target,
        "-x",
        str(DETACHED_W),
        "-y",
        str(DETACHED_H),
        allow_failure=True,
    )


def _release_window_geometry(adapter: TmuxAdapter, target: str) -> None:
    """Let attached clients drive the window size after detached construction.

    ``resize-window`` is useful while building detached windows, but tmux also
    flips the target to ``window-size manual``. Leaving that bit set prevents a
    phone-sized client from reclaiming the layout when it attaches.
    """
    adapter.run(
        "set-option",
        "-w",
        "-t",
        target,
        "window-size",
        "latest",
        allow_failure=True,
    )


def _side_roles_for_window(window: str) -> set[str]:
    base = window.split("(", 1)[0]
    return side_roles_for(base)


def _assert_side_column_postcondition(
    adapter: TmuxAdapter, target: str, window: str, *, enforce_column_width: bool = True
) -> None:
    """Hard-fail if detached builds do not match the shared ColumnSpec width."""
    side_roles = _side_roles_for_window(window)
    if not side_roles or not hasattr(adapter, "list_panes"):
        return
    roles: list[str] = []
    side_widths: dict[str, int] = {}
    for pane in adapter.list_panes(target):
        role = adapter.show_pane_option(pane["pane_id"], "@PANE_ID")
        if not role:
            continue
        roles.append(role)
        if role in side_roles:
            side_widths[role] = int(pane["width"])
    expected_column_width = WORKSPACE_LAYOUT.column.width
    if not enforce_column_width and side_widths:
        expected_column_width = next(iter(side_widths.values()))
    assert_window_build_contract(
        window,
        roles,
        side_widths=side_widths,
        expected_column_width=expected_column_width,
    )


def _home() -> str:
    return os.path.expanduser("~")


# Windows whose panes seat Imperium personas/overseers. These launch from the
# Imperium-ENV vault (their canon home) instead of $HOME. The council page seats
# four fixed personas (custodes/malcador/administratum from the Imperium vault,
# plus the civic pax seat which re-resolves its Civic-vault dir at persona-assert
# time); mechanicus seats the Fabricator-General + orchestrator.
PERSONA_WINDOWS = {COUNCIL_WINDOW, MECHANICUS_WINDOW}


def _imperium_vault() -> str | None:
    """Resolve the machine-local Imperium-ENV vault, or None if absent."""
    vault = os.path.expanduser(os.environ.get("IMPERIUM_VAULT", "~/vaults/Imperium-ENV"))
    return vault if os.path.isdir(vault) else None


def _window_dir(window: str) -> str:
    """Start-directory for a window's panes.

    Persona windows (council, mechanicus) launch from the Imperium-ENV vault so
    seated personas (Custodes, Malcador, Fabricator-General, Administratum,
    orchestrator) open in their canon home rather than $HOME. Falls back to $HOME
    when the vault is not mounted so pane creation never fails on a missing SMB
    mount. The civic seats (council:pax, mechanicus:orchestrator) re-resolve their
    Civic-vault launch dir at persona-assert time. Non-persona windows (palace,
    somnium, reservists) stay in $HOME.
    """
    if window in PERSONA_WINDOWS:
        vault = _imperium_vault()
        if vault:
            return vault
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

    _prepare_window_geometry(adapter, target)
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

    _assert_side_column_postcondition(adapter, target, window)
    adapter.run("select-pane", "-t", center)
    _release_window_geometry(adapter, target)


def build_somnium_window(adapter: TmuxAdapter, session: str, window: str = SOMNIUM_WINDOW) -> None:
    """Build the 5-pane somnium layout: left side rail W + right 2x2.

    Layout (final):
      W  = full-height west pane
      N  = right grid north-west    NE = right grid north-east
      S  = right grid south-west    SE = right grid south-east
    """
    target = f"{session}:{window}"
    wdir = _window_dir(window)

    _prepare_window_geometry(adapter, target)
    total_w = _window_dim(adapter, target, "#{window_width}")
    total_h = _window_dim(adapter, target, "#{window_height}")

    layout = WORKSPACE_LAYOUT.somnium
    _, east_grid_w = layout.grid_column_widths(total_w)
    half_h = layout.grid_row_height(total_h)

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

    _assert_side_column_postcondition(adapter, target, window)
    adapter.run("select-pane", "-t", west)
    _release_window_geometry(adapter, target)


def _council_seat(
    adapter: TmuxAdapter,
    pane_id: str,
    role: str,
    grid_state: str,
    *,
    persona: bool,
) -> None:
    """Tag a council seat: @PANE_ID, explicit @GRID_STATE, and @PANE_TYPE.

    Council seats carry persona-named @PANE_IDs (``council:custodes``), not the
    positional labels the somnium grid uses, so the persona/stop-hook resolution
    stays label-keyed and page-index independent. Persona seats are typed
    ``council`` so the assertion + audience layers recognize them; the
    true-terminal seat is a plain shell and stays untyped.
    """
    _set_pane_option(adapter, pane_id, "@PANE_ID", role)
    _set_pane_option(adapter, pane_id, "@GRID_STATE", grid_state)
    _set_pane_option(adapter, pane_id, "@GRID_RESERVED", "false")
    if persona:
        _set_pane_option(adapter, pane_id, "@PANE_TYPE", "council")


def build_council_window(
    adapter: TmuxAdapter,
    session: str,
    window: str = COUNCIL_WINDOW,
    *,
    enforce_column_width: bool = True,
) -> None:
    """Build the council page: the somnium 5-pane geometry seating fixed personas.

    Geometry mirrors :func:`build_somnium_window` (a full-height west rail plus a
    right 2x2 grid), but each seat is a fixed persona by cardinal position:

      W  = council:custodes      NE = council:malcador
      N  = council:pax           SE = council:administratum
      S  = council:true-terminal (a plain shell — no persona, no agent)

    Custodes, Malcador and Administratum relocated here from the retired legion /
    mechanicus seats; Pax relocated from the retired civic page. Their persona
    identities (and civic launch dir for Pax) are owned by token-api / persona
    assertion — this builder only lays out the seats and stamps their labels.
    """
    target = f"{session}:{window}"
    wdir = _window_dir(window)

    _prepare_window_geometry(adapter, target)
    total_w = _window_dim(adapter, target, "#{window_width}")
    total_h = _window_dim(adapter, target, "#{window_height}")

    layout = WORKSPACE_LAYOUT.somnium
    _, east_grid_w = layout.grid_column_widths(total_w)
    half_h = layout.grid_row_height(total_h)

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

    _council_seat(adapter, west, "council:custodes", "side", persona=True)
    _council_seat(adapter, right, "council:pax", "small", persona=True)
    _council_seat(adapter, ne, "council:malcador", "small", persona=True)
    _council_seat(adapter, south, "council:true-terminal", "small", persona=False)
    _council_seat(adapter, se, "council:administratum", "small", persona=True)

    _set_window_option(adapter, target, "@FOCUSED", "false")
    _set_window_option(adapter, target, "@GRID_EXPANDED", "none")
    _set_window_option(adapter, target, "@SIDE_EXPANDED", "none")
    _set_window_option(adapter, target, "@GRID_STASH", "")

    _assert_side_column_postcondition(
        adapter, target, window, enforce_column_width=enforce_column_width
    )
    adapter.run("select-pane", "-t", west)
    _release_window_geometry(adapter, target)


def ensure_council_window(adapter: TmuxAdapter, session: str = SESSION_NAME) -> None:
    """Ensure the council window exists with its five seats; idempotent.

    The council page is normally laid out once by :func:`build_workspace`. This is
    the recovery fallback for callers that need a council seat (e.g. the Custodes
    asserter) when the page is somehow absent from a partial session — a full
    ``tx restart`` rebuilds it properly.
    """
    names = [
        name.split("(", 1)[0]
        for name in adapter.run(
            "list-windows", "-t", session, "-F", "#{window_name}", allow_failure=True
        ).splitlines()
    ]
    if COUNCIL_WINDOW in names:
        return
    adapter.run(
        "new-window", "-t", session, "-n", COUNCIL_WINDOW, "-d", "-c", _window_dir(COUNCIL_WINDOW)
    )
    build_council_window(adapter, session, COUNCIL_WINDOW, enforce_column_width=False)


def build_mechanicus_window(adapter: TmuxAdapter, session: str) -> None:
    """Build the mechanicus stack window.

    The left column contains the Fabricator-General (the stack orchestrator
    anchor, pane 1) over the orchestrator persona seat. The orchestrator (the
    civic dispatch seat) relocated here from the retired civic page; the
    Administratum seat moved to the council page. Worker panes are added by
    stack.add_stack_pane on the right stack — the shared flat worker stack.
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
    fabricator = f"{target}.1"
    orchestrator = _split_pane(
        adapter, fabricator, "-v", "-l", "50%", cwd=_window_dir(MECHANICUS_WINDOW)
    )
    _pane_tag(adapter, fabricator, "mechanicus:fabricator-general")
    _set_pane_option(adapter, fabricator, "@PANE_TYPE", "mechanicus")
    _pane_tag(adapter, orchestrator, "mechanicus:orchestrator")
    _set_pane_option(adapter, orchestrator, "@PANE_TYPE", "mechanicus")
    _release_window_geometry(adapter, target)


def build_reservists_window(adapter: TmuxAdapter, session: str) -> None:
    """Build the reservists stack window, split down the middle into two
    perpetual reservist persona seats.

    Both panes are always-on, singleton-style perpetual seats — the reservists
    page mirror of how mechanicus anchors the Fabricator-General + orchestrator.
    The ephemeral test/subagent pool (token-api-owned lifecycle) is layered on
    later; this builder only seats the two standing reservists.

    Left pane is the **civic reservist** — the standing civic day-job thread that
    the civic-thread fallthrough activates when no civic instance is alive. It is
    marked ``@CIVIC_RESERVIST 1`` so the orchestration harness can resolve it by
    pane option (see civic-thread). Right pane is the **token-os reservist** — the
    token-os mirror of the civic reservist, marked ``@TOKEN_OS_RESERVIST 1`` so a
    future token-os-thread resolver can find it the same way.

    Reconcile now pins and protects these two seats as the top row while still
    allowing dispatch to add reservist workers in a full-width band below them.
    """
    target = f"{session}:{RESERVISTS_WINDOW}"
    adapter.run(
        "new-window",
        "-t",
        session,
        "-n",
        RESERVISTS_WINDOW,
        "-d",
        "-c",
        _window_dir(RESERVISTS_WINDOW),
    )
    civic = f"{target}.1"
    token_os = _split_pane(adapter, civic, "-h", "-l", "50%", cwd=_window_dir(RESERVISTS_WINDOW))
    _pane_tag(adapter, civic, "reservists:civic")
    _set_pane_option(adapter, civic, "@PANE_TYPE", "reservists")
    _set_pane_option(adapter, civic, "@CIVIC_RESERVIST", "1")
    _pane_tag(adapter, token_os, "reservists:token-os")
    _set_pane_option(adapter, token_os, "@PANE_TYPE", "reservists")
    _set_pane_option(adapter, token_os, "@TOKEN_OS_RESERVIST", "1")
    _release_window_geometry(adapter, target)


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
    adapter.run(
        "new-window", "-t", session, "-n", COUNCIL_WINDOW, "-d", "-c", _window_dir(COUNCIL_WINDOW)
    )
    build_council_window(adapter, session, COUNCIL_WINDOW)
    build_mechanicus_window(adapter, session)
    build_reservists_window(adapter, session)
    adapter.run("select-window", "-t", f"{session}:{PALACE_WINDOW}")


def attach_workspace(session: str = SESSION_NAME) -> None:
    """Attach an interactive client. Replaces the current process."""
    os.execvp("tmux", ["tmux", "attach-session", "-t", session])
