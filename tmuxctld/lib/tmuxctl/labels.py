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

# Canonical workspace roles after the 2026 tmuxctl layout overhaul.
#
# palace:  4-pane H layout: W | N/S | E
# somnium: 5-pane layout: W | N/NE/S/SE
PALACE_GRID_ROLES = ("palace:N", "palace:S")
PALACE_SIDE_ROLES = ("palace:W", "palace:E")
PALACE_ROLES = PALACE_SIDE_ROLES[:1] + PALACE_GRID_ROLES + PALACE_SIDE_ROLES[1:]

SOMNIUM_SIDE_ROLES = ("somnium:W",)
SOMNIUM_GRID_ROLES = ("somnium:N", "somnium:NE", "somnium:S", "somnium:SE")
SOMNIUM_ROLES = SOMNIUM_SIDE_ROLES + SOMNIUM_GRID_ROLES

PAGE_POSITION_ALIASES = {
    "palace": {
        "WW": "W",
        "EE": "E",
        "SL": "W",
        "SR": "E",
        "NW": "N",
        "NE": "N",
        "TL": "N",
        "TR": "N",
        "SW": "S",
        "SE": "S",
        "BL": "S",
        "BR": "S",
    },
    "somnium": {
        "NW": "W",
        "SW": "W",
        "TL": "W",
        "BL": "W",
        # old right-grid slots keep their right-column semantics
        "TR": "NE",
        "BR": "SE",
    },
}

PAGE_LEGACY_POSITION_ALIASES = {
    "palace": {
        "W": "WW",
        "E": "EE",
        "N": "NW",
        "S": "SW",
    },
    "somnium": {
        "W": "NW",
        # NB: no "N": "NE" reverse alias. Under the 5-pane somnium layout NE is a
        # FIRST-CLASS native pane (SOMNIUM_GRID_ROLES), so aliasing canonical N's
        # legacy form to NE made the somnium:N pane index under the somnium:NE
        # address too — colliding with the real somnium:NE pane and poisoning it
        # as ``ambiguous`` (the 2026-07-11 somnium:NE pane_unresolved outage).
        # Legacy NE requests still resolve: canonical_pane_role() maps them
        # forward on the request side, so no reverse-index key is needed.
    },
}


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
    position = PAGE_POSITION_ALIASES.get(page, {}).get(position, canonical_position(position))
    return f"{page}:{position}"


def legacy_pane_role(role: str) -> str:
    if role.startswith("audience:"):
        return f"audience:{legacy_pane_role(role.removeprefix('audience:'))}"
    if ":" not in role:
        return role
    page, position = role.rsplit(":", 1)
    position = PAGE_LEGACY_POSITION_ALIASES.get(page, {}).get(position, legacy_position(position))
    return f"{page}:{position}"


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
