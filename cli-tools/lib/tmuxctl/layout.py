from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction


def _floor_fraction(value: int, fraction: Fraction) -> int:
    return (value * fraction.numerator) // fraction.denominator


@dataclass(frozen=True)
class PalaceLayout:
    """Typed geometry for the palace H layout.

    Palace is horizontally:
      west side 30% | center stack 40% | east side 30%

    The center stack is split vertically into N/S panes; the side panes are
    full-height. Integer remainder columns stay in the center so the two side
    panes remain exactly equal.
    """

    side: Fraction = Fraction(3, 10)
    center: Fraction = Fraction(2, 5)
    vertical_borders: int = 2

    def __post_init__(self) -> None:
        if self.side <= 0 or self.center <= 0:
            raise ValueError("palace layout ratios must be positive")
        if self.side + self.center + self.side != 1:
            raise ValueError("palace layout must be side + center + side == 1")
        if self.vertical_borders != 2:
            raise ValueError("palace layout must have two vertical borders")

    def usable_width(self, total_width: int) -> int:
        return max(1, total_width - self.vertical_borders)

    def side_width(self, total_width: int) -> int:
        return max(1, _floor_fraction(self.usable_width(total_width), self.side))

    def center_width(self, total_width: int) -> int:
        return max(1, self.usable_width(total_width) - (2 * self.side_width(total_width)))

    def center_plus_east_split_width(self, total_width: int) -> int:
        """Width passed to the first split: center content + east content + border."""
        return self.center_width(total_width) + self.side_width(total_width) + 1


@dataclass(frozen=True)
class SomniumLayout:
    """Typed geometry for the somnium left-rail + 2x2 grid layout.

    Somnium is horizontally:
      west side 30% | right grid 70%

    The right grid is split into two columns. Integer remainder columns stay in
    the right grid so the left side pane remains the same width as palace sides.
    """

    west: Fraction = Fraction(3, 10)
    grid: Fraction = Fraction(7, 10)
    vertical_borders: int = 2

    def __post_init__(self) -> None:
        if self.west <= 0 or self.grid <= 0:
            raise ValueError("somnium layout ratios must be positive")
        if self.west + self.grid != 1:
            raise ValueError("somnium layout must be west + grid == 1")
        if self.vertical_borders != 2:
            raise ValueError("somnium layout must have two vertical borders")

    def usable_width(self, total_width: int) -> int:
        return max(1, total_width - self.vertical_borders)

    def west_width(self, total_width: int) -> int:
        return max(1, _floor_fraction(self.usable_width(total_width), self.west))

    def grid_width(self, total_width: int) -> int:
        return max(1, self.usable_width(total_width) - self.west_width(total_width))

    def grid_column_widths(self, total_width: int) -> tuple[int, int]:
        grid_width = self.grid_width(total_width)
        east = max(1, grid_width // 2)
        west = max(1, grid_width - east)
        return west, east

    def right_grid_split_width(self, total_width: int) -> int:
        """Width passed to the first split: right-grid content + internal border."""
        return self.grid_width(total_width) + 1


@dataclass(frozen=True)
class WorkspaceLayout:
    palace: PalaceLayout = field(default_factory=PalaceLayout)
    somnium: SomniumLayout = field(default_factory=SomniumLayout)

    def __post_init__(self) -> None:
        if self.palace.side != self.somnium.west:
            raise ValueError("palace side panes and somnium west pane must use the same ratio")


WORKSPACE_LAYOUT = WorkspaceLayout()
