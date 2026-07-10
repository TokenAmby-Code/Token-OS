from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import resolve_pane_in_snapshot


def pane(pid: str, role: str, window_index: int = 2, window_name: str = "somnium") -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pid,
        session_name="main",
        window_index=window_index,
        window_name=window_name,
        pane_index=0,
        width=80,
        height=24,
        current_command="zsh",
        tty="/dev/ttys000",
        pane_role=role,
        grid_state=GridState.UNKNOWN,
        pane_kind=PaneKind.UNKNOWN,
        reserved=False,
        active=False,
    )


def workspace() -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        session_name="main",
        windows=(
            WindowSnapshot(
                session_name="main",
                window_index=2,
                window_name="somnium",
                archetype=WindowArchetype.SOMNIUM,
                focused=False,
                grid_expanded="",
                grid_stash="",
                side_expanded="",
                panes=(
                    pane("%21", "somnium:N"),
                    pane("%22", "somnium:NE"),
                    pane("%23", "somnium:S"),
                    pane("%24", "somnium:SE"),
                ),
            ),
        ),
    )


def test_numeric_ne_resolves_to_real_somnium_ne_not_legacy_n():
    resolved = resolve_pane_in_snapshot(workspace(), "2:NE")
    assert resolved.pane_role == "somnium:NE"
    assert resolved.pane_id == "%22"


def test_numeric_se_resolves_to_somnium_se():
    resolved = resolve_pane_in_snapshot(workspace(), "2:SE")
    assert resolved.pane_role == "somnium:SE"
    assert resolved.pane_id == "%24"


@pytest.mark.parametrize("target", ["2:NW", "somnium:BR", "somnium:TL"])
def test_deprecated_public_aliases_rejected(target: str):
    with pytest.raises(ValueError, match="pane target not found"):
        resolve_pane_in_snapshot(workspace(), target)


def test_duplicate_public_roles_fail_loud() -> None:
    # Formerly first-writer-wins; that silent tie-break is how a
    # council:custodes-addressed report landed in council:malcador. Duplicate
    # public roles are an ambiguous address and must refuse resolution.
    snapshot = WorkspaceSnapshot(
        session_name="main",
        windows=(
            WindowSnapshot(
                session_name="main",
                window_index=2,
                window_name="somnium",
                archetype=WindowArchetype.SOMNIUM,
                focused=False,
                grid_expanded="",
                grid_stash="",
                side_expanded="",
                panes=(
                    pane("%31", "somnium:NE"),
                    pane("%32", "somnium:NE"),
                ),
            ),
        ),
    )

    with pytest.raises(ValueError, match="ambiguous"):
        resolve_pane_in_snapshot(snapshot, "somnium:NE")
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_pane_in_snapshot(snapshot, "2:NE")
