from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import audience as audience_module
from tmuxctl.audience import _window_base
from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import PaneResolution, resolve_pane_in_snapshot
from tmuxctl.snapshot import _window_warnings


def _pane(
    pane_id: str,
    role: str,
    *,
    kind: PaneKind = PaneKind.UNKNOWN,
    target: str = "",
    window: str = "palace",
    window_index: int = 1,
) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=window_index,
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
    first = panes[0]
    return WorkspaceSnapshot(
        session_name="main",
        windows=(
            WindowSnapshot(
                session_name="main",
                window_index=first.window_index,
                window_name=first.window_name,
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
    workspace = _workspace(_pane("%1", "palace:S"))

    resolved = resolve_pane_in_snapshot(workspace, "%1")

    assert resolved.pane_id == "%1"
    assert resolved.chain == ("palace:S",)


def test_logical_slot_resolves_to_physical_pane():
    workspace = _workspace(_pane("%1", "palace:S"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:S")

    assert resolved.pane_id == "%1"


def test_canonical_logical_slot_resolves_to_canonical_pane():
    workspace = _workspace(_pane("%1", "palace:N"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:N")

    assert resolved.pane_id == "%1"


def test_canonical_logical_slot_resolves_existing_pane():
    workspace = _workspace(_pane("%1", "palace:N"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:N")

    assert resolved.pane_id == "%1"


def test_positional_window_index_slot_resolves_live_pane():
    workspace = _workspace(_pane("%1", "palace:N"))

    resolved = resolve_pane_in_snapshot(workspace, "1:N")

    assert resolved.pane_id == "%1"


def test_positional_window_index_canonical_slot_resolves_canonical_pane():
    workspace = _workspace(_pane("%1", "palace:N"))

    resolved = resolve_pane_in_snapshot(workspace, "1:N")

    assert resolved.pane_id == "%1"


def test_positional_window_name_slot_resolves_live_pane():
    workspace = _workspace(_pane("%2", "somnium:SE", window="somnium"))

    resolved = resolve_pane_in_snapshot(workspace, "somnium:SE")

    assert resolved.pane_id == "%2"


def test_single_tombstone_resolves_to_target():
    workspace = _workspace(
        _pane("%1", "palace:S", kind=PaneKind.TOMBSTONE, target="%9"),
        _pane("%9", "audience:palace:S", window="_palace_audience"),
    )

    resolved = resolve_pane_in_snapshot(workspace, "palace:S")

    assert resolved.pane_id == "%9"
    assert resolved.chain == ("palace:S", "audience:palace:S")


def test_double_tombstone_resolves_to_final_target():
    workspace = _workspace(
        _pane("%1", "legion:custodes", kind=PaneKind.TOMBSTONE, target="palace:N"),
        _pane("%2", "palace:N", kind=PaneKind.TOMBSTONE, target="%9"),
        _pane("%9", "audience:palace:N", window="_palace_audience"),
    )

    resolved = resolve_pane_in_snapshot(workspace, "legion:custodes")

    assert resolved.pane_id == "%9"
    assert resolved.chain == ("legion:custodes", "palace:N", "audience:palace:N")


def test_missing_tombstone_target_errors_clearly():
    workspace = _workspace(_pane("%1", "palace:S", kind=PaneKind.TOMBSTONE, target="%404"))

    with pytest.raises(ValueError, match="target not found: %404"):
        resolve_pane_in_snapshot(workspace, "palace:S")


def test_tombstone_cycle_errors_clearly():
    workspace = _workspace(
        _pane("%1", "palace:S", kind=PaneKind.TOMBSTONE, target="palace:N"),
        _pane("%2", "palace:N", kind=PaneKind.TOMBSTONE, target="palace:S"),
    )

    with pytest.raises(ValueError, match="tombstone cycle detected"):
        resolve_pane_in_snapshot(workspace, "palace:S")


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
        ["audience:palace:S", "audience:palace:N"],
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

    assert "audience pane role does not match window page: audience:somnium:W" in warnings


def test_audience_jump_reports_coordinate_id_not_percent_id(monkeypatch):
    class FakeAdapter:
        def show_pane_option(self, pane_id: str, option: str) -> str:
            if pane_id == "%9" and option == "@PANE_ID":
                return "audience:palace:N"
            return ""

    selected: list[str] = []

    monkeypatch.setattr(
        audience_module,
        "resolve_pane",
        lambda _adapter, _target: PaneResolution(
            requested="%1",
            pane_id="%9",
            pane_role="audience:palace:N",
            pane_kind=PaneKind.AUDIENCE,
            chain=("palace:N", "audience:palace:N"),
        ),
    )
    monkeypatch.setattr(
        audience_module,
        "_select_pane_for_client",
        lambda _adapter, pane_id, client="": selected.append(pane_id),
    )

    result = audience_module.audience_jump(FakeAdapter(), "%1")

    assert result == "selected palace:N via palace:N -> palace:N"
    assert "%9" not in result
    assert selected == ["%9"]


def test_numeric_legion_worker_abbreviation_resolves_by_window_index():
    workspace = _workspace(_pane("%5", "legion:5", window="legion", window_index=3))

    resolved = resolve_pane_in_snapshot(workspace, "3:5")

    assert resolved.pane_id == "%5"


def test_legion_custodes_has_zero_abbreviation():
    workspace = _workspace(_pane("%C", "legion:custodes", window="legion", window_index=3))

    resolved = resolve_pane_in_snapshot(workspace, "3:0")

    assert resolved.pane_id == "%C"


def test_mechanicus_fabricator_has_zero_abbreviation_and_admin_named_slot():
    workspace = _workspace(
        _pane("%F", "mechanicus:fabricator-general", window="mechanicus", window_index=4),
        _pane("%A", "mechanicus:admin", window="mechanicus", window_index=4),
    )

    assert resolve_pane_in_snapshot(workspace, "4:0").pane_id == "%F"
    assert resolve_pane_in_snapshot(workspace, "4:admin").pane_id == "%A"
