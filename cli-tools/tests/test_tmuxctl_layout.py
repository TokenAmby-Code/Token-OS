from __future__ import annotations

import pathlib
import sys
from fractions import Fraction

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.builder import DETACHED_W, _assert_side_column_postcondition
from tmuxctl.enums import GridState, PaneKind
from tmuxctl.invariants import InvariantViolation, assert_grid_cardinality, assert_required_seats
from tmuxctl.labels import PALACE_SIDE_ROLES, SOMNIUM_SIDE_ROLES
from tmuxctl.layout import ColumnSpec, PalaceLayout, SomniumLayout, WorkspaceLayout
from tmuxctl.models import PaneRole, PaneSnapshot


class WidthAdapter:
    def __init__(self, rows: list[tuple[str, str, int]]):
        self.rows = rows

    def list_panes(self, target: str) -> list[dict[str, str]]:
        return [
            {"pane_id": pane_id, "width": str(width), "window_name": target.split(":", 1)[-1]}
            for pane_id, _role, width in self.rows
        ]

    def show_pane_option(self, pane_id: str, option: str) -> str:
        assert option == "@PANE_ID"
        for row_pane_id, role, _width in self.rows:
            if row_pane_id == pane_id:
                return role
        return ""


def test_workspace_layout_ratios_are_canonical() -> None:
    layout = WorkspaceLayout()

    assert layout.column.ratio == Fraction(3, 10)
    assert layout.palace.side == layout.column
    assert layout.palace.center == Fraction(2, 5)
    assert layout.somnium.west == layout.column
    assert layout.somnium.grid == Fraction(7, 10)
    assert PALACE_SIDE_ROLES == ("palace:W", "palace:E")
    assert SOMNIUM_SIDE_ROLES == ("somnium:W",)


def test_layout_widths_keep_side_panes_equal_and_center_remainder_typed() -> None:
    layout = WorkspaceLayout()

    # Detached builder default: 239 columns, shared ColumnSpec -> 71-column sides.
    assert DETACHED_W == layout.column.reference_total_width == 239
    assert layout.column.usable_width == 237
    assert layout.column.width == 71
    assert layout.palace.usable_width(239) == 237
    assert layout.palace.side_width(239) == 71
    assert layout.palace.center_width(239) == 95
    assert layout.palace.center_plus_east_split_width(239) == 167

    assert layout.somnium.usable_width(239) == 237
    assert layout.somnium.west_width(239) == layout.palace.side_width(239)
    assert layout.somnium.grid_width(239) == 167
    assert layout.somnium.grid_column_widths(239) == (83, 83)
    assert layout.somnium.grid_row_height(60) == 29
    assert layout.somnium.right_grid_split_width(239) == 167


def test_somnium_right_grid_cells_are_equal_sized() -> None:
    layout = WorkspaceLayout()

    west, east = layout.somnium.grid_column_widths(layout.column.reference_total_width)
    north, south = (layout.somnium.grid_row_height(60), layout.somnium.grid_row_height(60))

    assert west == east == 83
    assert north == south == 29


def test_layout_rejects_ratio_drift() -> None:
    with pytest.raises(ValueError):
        PalaceLayout(side=ColumnSpec(ratio=Fraction(1, 4)), center=Fraction(2, 5))
    with pytest.raises(ValueError):
        SomniumLayout(west=ColumnSpec(ratio=Fraction(1, 3)), grid=Fraction(7, 10))
    with pytest.raises(ValueError):
        WorkspaceLayout(somnium=SomniumLayout(west=ColumnSpec(ratio=Fraction(1, 5))))


def test_builder_column_postcondition_rejects_off_by_one_width() -> None:
    adapter = WidthAdapter(
        [
            ("%1", "palace:W", 70),
            ("%2", "palace:N", 95),
            ("%3", "palace:S", 95),
            ("%4", "palace:E", 71),
        ]
    )

    with pytest.raises(
        InvariantViolation, match="palace side column palace:W.*expected 71, actual 70"
    ):
        _assert_side_column_postcondition(adapter, "main:palace", "palace")


def test_builder_column_postcondition_accepts_uniform_side_columns() -> None:
    adapter = WidthAdapter(
        [
            ("%1", "palace:W", 71),
            ("%2", "palace:N", 95),
            ("%3", "palace:S", 95),
            ("%4", "palace:E", 71),
        ]
    )

    _assert_side_column_postcondition(adapter, "main:palace", "palace")


def test_builder_column_postcondition_relaxes_recovery_width_only() -> None:
    adapter = WidthAdapter(
        [
            ("%1", "council:custodes", 60),
            ("%2", "council:pax", 58),
            ("%3", "council:malcador", 58),
            ("%4", "council:true-terminal", 58),
            ("%5", "council:administratum", 58),
        ]
    )

    _assert_side_column_postcondition(
        adapter, "main:council", "council", enforce_column_width=False
    )


def test_pane_role_is_typed_and_canonicalized() -> None:
    pane = PaneSnapshot(
        pane_id="%1",
        session_name="main",
        window_index=1,
        window_name="somnium",
        pane_index=1,
        width=71,
        height=60,
        current_command="zsh",
        tty="/dev/ttys001",
        pane_role="somnium:NW",
        grid_state=GridState.SIDE,
        pane_kind=PaneKind.UNKNOWN,
        reserved=False,
        active=True,
    )

    assert isinstance(pane.pane_role, PaneRole)
    assert pane.pane_role == "somnium:W"


def test_pane_role_rejects_raw_tmux_identity() -> None:
    with pytest.raises(ValueError, match="invalid pane role"):
        PaneRole("%12")


def test_invariants_enforce_required_seats_and_grid_cardinality() -> None:
    roles = ["somnium:W", "somnium:N", "somnium:NE", "somnium:S", "somnium:SE"]

    assert_required_seats("somnium", roles)
    assert_grid_cardinality("somnium", roles)

    with pytest.raises(InvariantViolation, match="missing required pane roles: somnium:SE"):
        assert_required_seats("somnium", roles[:-1])
    with pytest.raises(InvariantViolation, match="grid cardinality violated: expected 4, actual 3"):
        assert_grid_cardinality("somnium", roles[:-1])
