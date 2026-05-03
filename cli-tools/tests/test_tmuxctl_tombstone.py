from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.audience import _window_base
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import resolve_pane_in_snapshot


def _pane(
    pane_id: str,
    role: str,
    *,
    kind: PaneKind = PaneKind.UNKNOWN,
    target: str = "",
    window: str = "palace",
) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=1,
        window_name=window,
        pane_index=1,
        width=100,
        height=40,
        current_command="zsh",
        tty="/dev/ttys001",
        pane_role=role,
        grid_state=GridState.SMALL,
        pane_kind=kind,
        reserved=False,
        active=False,
        tombstone_target=target,
        tombstone_source=role if kind is PaneKind.TOMBSTONE else "",
    )


def _workspace(*panes: PaneSnapshot) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        session_name="main",
        windows=(
            WindowSnapshot(
                session_name="main",
                window_index=1,
                window_name="palace",
                archetype=WindowArchetype.PALACE,
                focused=False,
                grid_expanded="none",
                grid_stash="",
                side_expanded="none",
                panes=panes,
            ),
        ),
    )


def test_direct_physical_pane_resolves_to_itself():
    workspace = _workspace(_pane("%1", "palace:BL"))

    resolved = resolve_pane_in_snapshot(workspace, "%1")

    assert resolved.pane_id == "%1"
    assert resolved.chain == ("palace:BL",)


def test_logical_slot_resolves_to_physical_pane():
    workspace = _workspace(_pane("%1", "palace:BL"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:BL")

    assert resolved.pane_id == "%1"


def test_single_tombstone_resolves_to_target():
    workspace = _workspace(
        _pane("%1", "palace:BL", kind=PaneKind.TOMBSTONE, target="%9"),
        _pane("%9", "audience:palace:BL", window="_palace_audience"),
    )

    resolved = resolve_pane_in_snapshot(workspace, "palace:BL")

    assert resolved.pane_id == "%9"
    assert resolved.chain == ("palace:BL", "audience:palace:BL")


def test_double_tombstone_resolves_to_final_target():
    workspace = _workspace(
        _pane("%1", "legion:custodes", kind=PaneKind.TOMBSTONE, target="palace:TR"),
        _pane("%2", "palace:TR", kind=PaneKind.TOMBSTONE, target="%9"),
        _pane("%9", "audience:palace:TR", window="_palace_audience"),
    )

    resolved = resolve_pane_in_snapshot(workspace, "legion:custodes")

    assert resolved.pane_id == "%9"
    assert resolved.chain == ("legion:custodes", "palace:TR", "audience:palace:TR")


def test_missing_tombstone_target_errors_clearly():
    workspace = _workspace(_pane("%1", "palace:BL", kind=PaneKind.TOMBSTONE, target="%404"))

    with pytest.raises(ValueError, match="target not found: %404"):
        resolve_pane_in_snapshot(workspace, "palace:BL")


def test_tombstone_cycle_errors_clearly():
    workspace = _workspace(
        _pane("%1", "palace:BL", kind=PaneKind.TOMBSTONE, target="palace:TR"),
        _pane("%2", "palace:TR", kind=PaneKind.TOMBSTONE, target="palace:BL"),
    )

    with pytest.raises(ValueError, match="tombstone cycle detected"):
        resolve_pane_in_snapshot(workspace, "palace:BL")


def test_audience_window_base_strips_stack_spill_suffixes():
    assert _window_base("legion-2") == "legion"
    assert _window_base("mechanicus-12") == "mechanicus"
    assert _window_base("legion-2(3)") == "legion"


def test_audience_window_base_does_not_strip_non_audience_names():
    assert _window_base("mars-2") == "mars-2"
    assert _window_base("palace-west") == "palace-west"
