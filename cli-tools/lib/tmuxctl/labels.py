from __future__ import annotations

from collections.abc import Iterable

POSITION_ALIASES = {
    "TL": "NW",
    "TR": "NE",
    "BL": "SW",
    "BR": "SE",
    "SL": "WW",
    "SR": "EE",
}

POSITION_LEGACY_ALIASES = {value: key for key, value in POSITION_ALIASES.items()}

CARDINAL_GRID = ("NW", "NE", "SW", "SE")
CARDINAL_SIDE = ("WW", "EE")

PALACE_GRID_ROLES = tuple(f"palace:{slot}" for slot in CARDINAL_GRID)
PALACE_SIDE_ROLES = tuple(f"palace:{slot}" for slot in CARDINAL_SIDE)
SOMNIUM_GRID_ROLES = tuple(f"somnium:{slot}" for slot in CARDINAL_GRID)
SOMNIUM_SIDE_ROLES = ("somnium:EE",)


def canonical_position(position: str) -> str:
    return POSITION_ALIASES.get(position, position)


def legacy_position(position: str) -> str:
    return POSITION_LEGACY_ALIASES.get(position, position)


def canonical_pane_role(role: str) -> str:
    if role.startswith("audience:"):
        return f"audience:{canonical_pane_role(role.removeprefix('audience:'))}"
    if ":" not in role:
        return role
    page, position = role.rsplit(":", 1)
    return f"{page}:{canonical_position(position)}"


def legacy_pane_role(role: str) -> str:
    if role.startswith("audience:"):
        return f"audience:{legacy_pane_role(role.removeprefix('audience:'))}"
    if ":" not in role:
        return role
    page, position = role.rsplit(":", 1)
    return f"{page}:{legacy_position(position)}"


def pane_role_aliases(role: str) -> tuple[str, ...]:
    canonical = canonical_pane_role(role)
    legacy = legacy_pane_role(canonical)
    if legacy == canonical:
        return ()
    return (legacy,)


def indexable_pane_roles(role: str) -> Iterable[str]:
    if not role:
        return ()
    canonical = canonical_pane_role(role)
    return dict.fromkeys((role, canonical, *pane_role_aliases(canonical)))
