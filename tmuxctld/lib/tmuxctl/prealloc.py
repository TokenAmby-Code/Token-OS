"""Pre-allocated fixed-pane page allocation helpers.

Palace and somnium are not spawnable stacks.  They are fixed pane sets whose
slots are reused in place; ``:new`` allocation on those pages must therefore
pick an existing free slot from the daemon occupancy ledger instead of calling
the stack-spawn path.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .labels import canonical_pane_role
from .teardown import SLOT_WINDOWS, window_base

PREALLOC_PAGE_ROLES: dict[str, tuple[str, ...]] = {
    # Dispatch allocation order is intentionally not the layout/declaration
    # order.  The public ``:new`` allocator must be deterministic and stable:
    # palace: N -> S -> E -> W; somnium: N -> NE -> SE -> S -> W.
    "palace": ("palace:N", "palace:S", "palace:E", "palace:W"),
    "somnium": ("somnium:N", "somnium:NE", "somnium:SE", "somnium:S", "somnium:W"),
}


def is_prealloc_page(page: str) -> bool:
    """Return whether ``page`` is a fixed pre-allocated pane page."""

    return window_base(page) in SLOT_WINDOWS


def ordered_prealloc_roles(page: str) -> tuple[str, ...]:
    """Configured greedy order for a pre-allocated page's fixed pane set."""

    return PREALLOC_PAGE_ROLES.get(window_base(page), ())


def first_free_prealloc_role(page: str, freelist: Iterable[Mapping[str, object]]) -> str | None:
    """Choose the first free pane on ``page`` in configured fixed-slot order.

    ``freelist`` is expected to come from the daemon's ledger-derived freelist:
    each row has at least ``pane_role`` and ``window_name``.  No DB or parallel
    occupancy source participates here.
    """

    base = window_base(page)
    order = ordered_prealloc_roles(base)
    if not order:
        return None

    free_roles: set[str] = set()
    for row in freelist:
        role = canonical_pane_role(str(row.get("pane_role") or row.get("pane_id") or ""))
        if not role:
            continue
        row_window = window_base(str(row.get("window_name") or role.split(":", 1)[0]))
        if row_window != base:
            continue
        if role.startswith(f"{base}:"):
            free_roles.add(role)

    for role in order:
        if role in free_roles:
            return role
    return None
