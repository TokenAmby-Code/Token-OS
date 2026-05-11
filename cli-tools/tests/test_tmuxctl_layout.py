from __future__ import annotations

import pathlib
import sys
from fractions import Fraction

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.layout import PalaceLayout, SomniumLayout, WorkspaceLayout
from tmuxctl.labels import PALACE_SIDE_ROLES, SOMNIUM_SIDE_ROLES


def test_workspace_layout_ratios_are_canonical() -> None:
    layout = WorkspaceLayout()

    assert layout.palace.side == Fraction(3, 10)
    assert layout.palace.center == Fraction(2, 5)
    assert layout.somnium.west == layout.palace.side
    assert layout.somnium.grid == Fraction(7, 10)
    assert PALACE_SIDE_ROLES == ("palace:W", "palace:E")
    assert SOMNIUM_SIDE_ROLES == ("somnium:W",)


def test_layout_widths_keep_side_panes_equal_and_center_remainder_typed() -> None:
    layout = WorkspaceLayout()

    # Detached builder default: 240 columns, two vertical borders -> 238 content columns.
    assert layout.palace.usable_width(240) == 238
    assert layout.palace.side_width(240) == 71
    assert layout.palace.center_width(240) == 96
    assert layout.palace.center_plus_east_split_width(240) == 168

    assert layout.somnium.usable_width(240) == 238
    assert layout.somnium.west_width(240) == layout.palace.side_width(240)
    assert layout.somnium.grid_width(240) == 167
    assert layout.somnium.grid_column_widths(240) == (84, 83)
    assert layout.somnium.right_grid_split_width(240) == 168


def test_layout_rejects_ratio_drift() -> None:
    with pytest.raises(ValueError):
        PalaceLayout(side=Fraction(1, 4), center=Fraction(2, 5))
    with pytest.raises(ValueError):
        SomniumLayout(west=Fraction(1, 3), grid=Fraction(7, 10))
    with pytest.raises(ValueError):
        WorkspaceLayout(somnium=SomniumLayout(west=Fraction(1, 5), grid=Fraction(4, 5)))
