"""Inspect surfaces must not leak raw physical tmux ids (`%NN`) by default.

The canonical-id campaign makes `tmuxctl {page}:{id}` (the pane role) the sole
external pane identity. `tmuxctl inspect workspace` / `window` / `pane` used to
print the volatile physical `%NN` inline next to the canonical role — that leak
is what let an orchestrator obtain raw `%ids` on the normal path. Default render
must be canonical-only; physical ids stay reachable behind `--physical`.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.inspect import render_pane, render_window, render_workspace
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot

RAW_ID_RE = re.compile(r"%\d+")


def _pane(pane_id: str, role: str, *, kind: PaneKind = PaneKind.MECHANICUS) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=3,
        window_name="mechanicus",
        pane_index=0,
        width=120,
        height=40,
        current_command="claude",
        tty="/dev/ttys003",
        pane_role=role,
        grid_state=GridState.SMALL,
        pane_kind=kind,
        reserved=False,
        active=True,
    )


def _window() -> WindowSnapshot:
    return WindowSnapshot(
        session_name="main",
        window_index=3,
        window_name="mechanicus",
        archetype=WindowArchetype.MECHANICUS_STACK,
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
        panes=(
            _pane("%29", "mechanicus:fabricator-general"),
            _pane("%80", "mechanicus:1"),
        ),
    )


def _workspace() -> WorkspaceSnapshot:
    return WorkspaceSnapshot(session_name="main", windows=(_window(),))


def test_workspace_default_is_canonical_only() -> None:
    out = render_workspace(_workspace())
    assert not RAW_ID_RE.search(out), f"raw physical id leaked: {out!r}"
    assert "mechanicus:fabricator-general" in out
    assert "mechanicus:1" in out


def test_workspace_physical_flag_restores_raw_ids() -> None:
    out = render_workspace(_workspace(), physical=True)
    assert "%29" in out
    assert "%80" in out
    assert "mechanicus:fabricator-general" in out


def test_window_default_is_canonical_only() -> None:
    out = render_window(_window())
    assert not RAW_ID_RE.search(out), f"raw physical id leaked: {out!r}"
    assert "mechanicus:1" in out


def test_window_physical_flag_restores_raw_ids() -> None:
    out = render_window(_window(), physical=True)
    assert "%29" in out and "%80" in out


def test_pane_default_is_canonical_only() -> None:
    out = render_pane(_pane("%29", "mechanicus:fabricator-general"))
    assert not RAW_ID_RE.search(out), f"raw physical id leaked: {out!r}"
    assert "mechanicus:fabricator-general" in out


def test_pane_physical_flag_restores_raw_id() -> None:
    out = render_pane(_pane("%29", "mechanicus:fabricator-general"), physical=True)
    assert "%29" in out


def test_unset_role_pane_default_hides_physical_id() -> None:
    # A pane with no @PANE_ID role has only its physical id; default must still
    # not leak it (the role falls back to a canonical placeholder).
    out = render_window(
        WindowSnapshot(
            session_name="main",
            window_index=3,
            window_name="mechanicus",
            archetype=WindowArchetype.MECHANICUS_STACK,
            focused=False,
            grid_expanded="none",
            grid_stash="",
            side_expanded="none",
            panes=(_pane("%99", "", kind=PaneKind.UNKNOWN),),
        )
    )
    assert not RAW_ID_RE.search(out), f"raw physical id leaked: {out!r}"
