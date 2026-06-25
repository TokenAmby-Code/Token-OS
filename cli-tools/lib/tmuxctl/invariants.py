"""Pure contract assertions for tmuxctl layout and identity snapshots."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence

from .enums import GridState, PaneKind, WindowArchetype
from .labels import PALACE_GRID_ROLES, PALACE_ROLES, PALACE_SIDE_ROLES, SOMNIUM_GRID_ROLES
from .models import PaneRole, PaneSnapshot, WindowSnapshot

COUNCIL_ROLES = (
    "council:custodes",
    "council:pax",
    "council:malcador",
    "council:true-terminal",
    "council:administratum",
)
COUNCIL_GRID_ROLES = (
    "council:pax",
    "council:malcador",
    "council:true-terminal",
    "council:administratum",
)
COUNCIL_SIDE_ROLES = ("council:custodes",)


class InvariantViolation(ValueError):
    """Raised when a tmuxctl structural contract is violated."""


def _role_text(role: PaneRole | str | None) -> str:
    return str(role or "")


def assert_valid_pane_role(role: PaneRole | str | None) -> PaneRole | None:
    """Return a typed, canonical pane role or raise for malformed identity."""
    try:
        return PaneRole.parse(role)
    except ValueError as exc:
        raise InvariantViolation(str(exc)) from exc


def assert_valid_pane_kind(pane: PaneSnapshot) -> None:
    """Pane kind must be a typed PaneKind enum value, never an arbitrary string."""
    if not isinstance(pane.pane_kind, PaneKind):
        raise InvariantViolation(f"{pane.pane_id} has invalid pane kind: {pane.pane_kind!r}")


def _assert_role_set(window_name: str, roles: Sequence[str], required: Sequence[str]) -> None:
    counts = Counter(role for role in roles if role)
    missing = sorted(set(required) - set(counts))
    if missing:
        raise InvariantViolation(f"{window_name} missing required pane roles: {', '.join(missing)}")
    duplicates = sorted(role for role, count in counts.items() if count > 1)
    if duplicates:
        raise InvariantViolation(f"{window_name} duplicate pane roles: {', '.join(duplicates)}")


def assert_required_seats(window_name: str, roles: Iterable[PaneRole | str | None]) -> None:
    """Known layout windows must contain their required logical seats."""
    base = window_name.split("(", 1)[0]
    role_texts = [_role_text(assert_valid_pane_role(role)) for role in roles]
    if base == "palace":
        _assert_role_set(window_name, role_texts, PALACE_ROLES)
    elif base == "somnium":
        _assert_role_set(window_name, role_texts, ("somnium:W", *SOMNIUM_GRID_ROLES))
    elif base == "council":
        _assert_role_set(window_name, role_texts, COUNCIL_ROLES)


def assert_grid_cardinality(window_name: str, roles: Iterable[PaneRole | str | None]) -> None:
    """Known grid windows must have the exact number of grid panes."""
    base = window_name.split("(", 1)[0]
    role_texts = {_role_text(assert_valid_pane_role(role)) for role in roles}
    if base == "palace":
        actual = len(role_texts & set(PALACE_GRID_ROLES))
        expected = len(PALACE_GRID_ROLES)
    elif base == "somnium":
        actual = len(role_texts & set(SOMNIUM_GRID_ROLES))
        expected = len(SOMNIUM_GRID_ROLES)
    elif base == "council":
        actual = len(role_texts & set(COUNCIL_GRID_ROLES))
        expected = len(COUNCIL_GRID_ROLES)
    else:
        return
    if actual != expected:
        raise InvariantViolation(
            f"{window_name} grid cardinality violated: expected {expected}, actual {actual}"
        )


def assert_uniform_column_width(
    window_name: str,
    side_widths: Mapping[str, int],
    expected_width: int,
) -> None:
    """Every realized side-column pane must match the shared ColumnSpec width."""
    for role, actual_width in side_widths.items():
        if actual_width != expected_width:
            raise InvariantViolation(
                f"{window_name} side column {role} width contract violated: "
                f"expected {expected_width}, actual {actual_width}"
            )


def assert_window_snapshot(
    snapshot: WindowSnapshot, *, expected_column_width: int | None = None
) -> None:
    """Assert structural contracts over a captured window snapshot."""
    for pane in snapshot.panes:
        assert_valid_pane_role(pane.pane_role)
        assert_valid_pane_kind(pane)
    roles = [pane.pane_role for pane in snapshot.panes]
    assert_required_seats(snapshot.window_name, roles)
    assert_grid_cardinality(snapshot.window_name, roles)
    if expected_column_width is None:
        return
    side_roles = _side_roles_for(snapshot.window_name)
    if not side_roles:
        return
    side_widths = {
        _role_text(pane.pane_role): pane.width
        for pane in snapshot.panes
        if _role_text(pane.pane_role) in side_roles
    }
    assert_uniform_column_width(snapshot.window_name, side_widths, expected_column_width)


def _side_roles_for(window_name: str) -> set[str]:
    base = window_name.split("(", 1)[0]
    if base == "palace":
        return set(PALACE_SIDE_ROLES)
    if base == "somnium":
        return {"somnium:W"}
    if base == "council":
        return set(COUNCIL_SIDE_ROLES)
    return set()


def assert_window_build_contract(
    window_name: str,
    roles: Iterable[PaneRole | str | None],
    *,
    side_widths: Mapping[str, int],
    expected_column_width: int,
) -> None:
    """Assert build-time layout contracts from role/width rows."""
    role_tuple = tuple(assert_valid_pane_role(role) for role in roles)
    assert_required_seats(window_name, role_tuple)
    assert_grid_cardinality(window_name, role_tuple)
    assert_uniform_column_width(window_name, side_widths, expected_column_width)


def assert_known_archetype(snapshot: WindowSnapshot) -> None:
    if snapshot.archetype is WindowArchetype.UNKNOWN:
        raise InvariantViolation(f"{snapshot.window_name} has unknown window archetype")


def assert_side_pane_states(snapshot: WindowSnapshot) -> None:
    """Side roles in known side-column pages must carry GridState.SIDE."""
    side_roles = _side_roles_for(snapshot.window_name)
    for pane in snapshot.panes:
        if _role_text(pane.pane_role) in side_roles and pane.grid_state is not GridState.SIDE:
            raise InvariantViolation(f"{snapshot.window_name} {pane.pane_role} must be side state")
