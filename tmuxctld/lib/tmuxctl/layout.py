from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import cast

DEFAULT_COLUMN_TOTAL_WIDTH = 239


def _floor_fraction(value: int, fraction: Fraction) -> int:
    return (value * fraction.numerator) // fraction.denominator


@dataclass(frozen=True)
class ColumnSpec:
    """Shared side-column geometry contract.

    ``width`` is the reference detached-build width. Runtime layouts must call
    :meth:`width_for_total` so side columns remain proportional to the current
    client/window width (phone, laptop, desktop, etc.).
    """

    ratio: Fraction = Fraction(3, 10)
    reference_total_width: int = DEFAULT_COLUMN_TOTAL_WIDTH
    vertical_borders: int = 2

    def __post_init__(self) -> None:
        if self.ratio <= 0 or self.ratio >= 1:
            raise ValueError("column ratio must be positive and less than 1")
        if self.reference_total_width <= self.vertical_borders:
            raise ValueError("column reference width must exceed vertical borders")
        if self.vertical_borders < 0:
            raise ValueError("column vertical border count must be non-negative")

    @property
    def usable_width(self) -> int:
        return max(1, self.reference_total_width - self.vertical_borders)

    @property
    def width(self) -> int:
        return max(1, _floor_fraction(self.usable_width, self.ratio))

    def width_for_total(self, total_width: int) -> int:
        """Return this column's proportional width for a concrete window width."""
        return max(1, _floor_fraction(max(1, total_width - self.vertical_borders), self.ratio))


@dataclass(frozen=True)
class PalaceLayout:
    """Typed geometry for the palace H layout.

    Palace is horizontally:
      west side column | center stack flex | east side column

    The side columns use the shared :class:`ColumnSpec` ratio; integer remainder
    columns stay in the center so the two side panes remain exactly equal.
    """

    side: ColumnSpec | Fraction = field(default_factory=ColumnSpec)
    center: Fraction = Fraction(2, 5)
    vertical_borders: int = 2

    def __post_init__(self) -> None:
        if isinstance(self.side, Fraction):
            object.__setattr__(self, "side", ColumnSpec(ratio=self.side))
        if not isinstance(self.side, ColumnSpec):
            raise ValueError("palace side must be a ColumnSpec")
        if self.center <= 0:
            raise ValueError("palace center ratio must be positive")
        if self.side.ratio + self.center + self.side.ratio != 1:
            raise ValueError("palace layout must be side + center + side == 1")
        if self.vertical_borders != self.side.vertical_borders:
            raise ValueError("palace vertical borders must match side ColumnSpec")

    def usable_width(self, total_width: int) -> int:
        return max(1, total_width - self.vertical_borders)

    def side_width(self, total_width: int) -> int:
        return cast(ColumnSpec, self.side).width_for_total(total_width)

    def center_width(self, total_width: int) -> int:
        return max(1, self.usable_width(total_width) - (2 * self.side_width(total_width)))

    def center_plus_east_split_width(self, total_width: int) -> int:
        """Width passed to the first split: center content + east content + border."""
        return self.center_width(total_width) + self.side_width(total_width) + 1


@dataclass(frozen=True)
class SomniumLayout:
    """Typed geometry for the somnium left-rail + 2x2 grid layout.

    Somnium is horizontally:
      west side column | right grid flex

    The west side uses the shared :class:`ColumnSpec` ratio; integer remainder
    columns stay in the right grid so the side column remains uniform across
    pages at the same current width.
    """

    west: ColumnSpec | Fraction = field(default_factory=ColumnSpec)
    grid: Fraction = Fraction(7, 10)
    vertical_borders: int = 2

    def __post_init__(self) -> None:
        if isinstance(self.west, Fraction):
            object.__setattr__(self, "west", ColumnSpec(ratio=self.west))
        if not isinstance(self.west, ColumnSpec):
            raise ValueError("somnium west must be a ColumnSpec")
        if self.grid <= 0:
            raise ValueError("somnium grid ratio must be positive")
        if self.west.ratio + self.grid != 1:
            raise ValueError("somnium layout must be west + grid == 1")
        if self.vertical_borders != self.west.vertical_borders:
            raise ValueError("somnium vertical borders must match west ColumnSpec")

    def usable_width(self, total_width: int) -> int:
        return max(1, total_width - self.vertical_borders)

    def west_width(self, total_width: int) -> int:
        return cast(ColumnSpec, self.west).width_for_total(total_width)

    def grid_width(self, total_width: int) -> int:
        return max(1, total_width - self.west_width(total_width) - 1)

    def grid_column_widths(self, total_width: int) -> tuple[int, int]:
        grid_width = self.grid_width(total_width)
        east = max(1, (grid_width - 1) // 2)
        west = max(1, grid_width - east - 1)
        return west, east

    def right_grid_split_width(self, total_width: int) -> int:
        """Width passed to the first split: right-grid content including its border."""
        return self.grid_width(total_width)

    def grid_row_height(self, total_height: int) -> int:
        return max(1, (total_height - 2) // 2)


@dataclass(frozen=True)
class WorkspaceLayout:
    column: ColumnSpec = field(default_factory=ColumnSpec)
    palace: PalaceLayout | None = None
    somnium: SomniumLayout | None = None

    def __post_init__(self) -> None:
        palace = self.palace or PalaceLayout(side=self.column)
        somnium = self.somnium or SomniumLayout(west=self.column)
        object.__setattr__(self, "palace", palace)
        object.__setattr__(self, "somnium", somnium)
        if palace.side != self.column or somnium.west != self.column:
            raise ValueError("palace and somnium side panes must share the same ColumnSpec")
        if cast(ColumnSpec, palace.side).width != cast(ColumnSpec, somnium.west).width:
            raise ValueError("palace and somnium side panes must realize the same column width")


WORKSPACE_LAYOUT = WorkspaceLayout()
