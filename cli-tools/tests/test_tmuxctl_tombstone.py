from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.audience import _window_base
from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import resolve_pane_in_snapshot
from tmuxctl.snapshot import _window_warnings


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
    workspace = _workspace(_pane("%1", "palace:SW"))

    resolved = resolve_pane_in_snapshot(workspace, "%1")

    assert resolved.pane_id == "%1"
    assert resolved.chain == ("palace:SW",)


def test_logical_slot_resolves_to_physical_pane():
    workspace = _workspace(_pane("%1", "palace:SW"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:SW")

    assert resolved.pane_id == "%1"


def test_legacy_logical_slot_alias_resolves_to_canonical_pane():
    workspace = _workspace(_pane("%1", "palace:NW"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:TL")

    assert resolved.pane_id == "%1"


def test_canonical_logical_slot_resolves_to_legacy_pane_before_mutation():
    workspace = _workspace(_pane("%1", "palace:TL"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:NW")

    assert resolved.pane_id == "%1"


def test_single_tombstone_resolves_to_target():
    workspace = _workspace(
        _pane("%1", "palace:SW", kind=PaneKind.TOMBSTONE, target="%9"),
        _pane("%9", "audience:palace:SW", window="_palace_audience"),
    )

    resolved = resolve_pane_in_snapshot(workspace, "palace:SW")

    assert resolved.pane_id == "%9"
    assert resolved.chain == ("palace:SW", "audience:palace:SW")


def test_double_tombstone_resolves_to_final_target():
    workspace = _workspace(
        _pane("%1", "legion:custodes", kind=PaneKind.TOMBSTONE, target="palace:NE"),
        _pane("%2", "palace:NE", kind=PaneKind.TOMBSTONE, target="%9"),
        _pane("%9", "audience:palace:NE", window="_palace_audience"),
    )

    resolved = resolve_pane_in_snapshot(workspace, "legion:custodes")

    assert resolved.pane_id == "%9"
    assert resolved.chain == ("legion:custodes", "palace:NE", "audience:palace:NE")


def test_missing_tombstone_target_errors_clearly():
    workspace = _workspace(_pane("%1", "palace:SW", kind=PaneKind.TOMBSTONE, target="%404"))

    with pytest.raises(ValueError, match="target not found: %404"):
        resolve_pane_in_snapshot(workspace, "palace:SW")


def test_tombstone_cycle_errors_clearly():
    workspace = _workspace(
        _pane("%1", "palace:SW", kind=PaneKind.TOMBSTONE, target="palace:NE"),
        _pane("%2", "palace:NE", kind=PaneKind.TOMBSTONE, target="palace:SW"),
    )

    with pytest.raises(ValueError, match="tombstone cycle detected"):
        resolve_pane_in_snapshot(workspace, "palace:SW")


def test_audience_window_base_strips_stack_spill_suffixes():
    assert _window_base("legion-2") == "legion"
    assert _window_base("mechanicus-12") == "mechanicus"
    assert _window_base("legion-2(3)") == "legion"


def test_audience_window_base_does_not_strip_non_audience_names():
    assert _window_base("mars-2") == "mars-2"
    assert _window_base("palace-west") == "palace-west"


def test_audience_window_warns_when_slot_is_split():
    warnings = _window_warnings(
        "_palace_audience",
        ["audience:palace:SW", "audience:palace:NE"],
        ["%1", "%2"],
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
    )

    assert "audience window should contain exactly one pane" in warnings
    assert "audience window should contain exactly one audience pane" in warnings


def test_audience_window_warns_when_page_type_does_not_match():
    warnings = _window_warnings(
        "_palace_audience",
        ["audience:somnium:SW"],
        ["%1"],
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
    )

    assert "audience pane role does not match window page: audience:somnium:SW" in warnings
