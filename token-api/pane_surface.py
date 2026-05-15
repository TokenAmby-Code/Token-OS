"""Human-safe pane surface formatting.

These helpers are used anywhere a Claude instance/pane identifier is surfaced to
the operator. In particular, they reject launch-placeholder tab names such as
``Claude 08:14`` so those placeholders cannot leak into TTS, push, Discord, or
the daily-note NOW widget.
"""

from __future__ import annotations

import re

DEFAULT_TAB_NAME_RX = re.compile(r"^Claude\s+\d{1,2}:\d{2}$")
RAW_TMUX_PANE_RX = re.compile(r"%\d+")

_PANE_PAGE_NUMBERS = {
    "palace": "1",
    "somnium": "2",
}


def is_meaningful_tab_name(tab_name: str | None) -> bool:
    """True when ``tab_name`` is not blank and not the default launch stamp."""
    return human_tab_name(tab_name) is not None


def human_tab_name(tab_name: str | None) -> str | None:
    """Return the cleaned human name, or None for placeholders/blank names."""
    if not tab_name:
        return None
    cleaned = tab_name.lstrip("✳⠐⠸ ").strip()
    if not cleaned:
        return None
    if DEFAULT_TAB_NAME_RX.match(cleaned):
        return None
    return cleaned


def pane_position_id(pane_label: str | None) -> str | None:
    """Return the stable page-number:slot position, e.g. ``1:N``.

    Only fixed two-dimensional workspaces have positional identity. Dynamic
    workspaces such as legion/mechanicus/custodes do not get fake positions.
    """
    if not pane_label:
        return None
    page, _, slot = pane_label.partition(":")
    page_number = _PANE_PAGE_NUMBERS.get(page)
    if page_number and slot:
        return f"{page_number}:{slot}"
    return None


def human_pane_surface(tab_name: str | None, tmux_pane: str | None, pane_label: str | None) -> str:
    """Return the operator-facing pane surface.

    Prefer ``<position> <name>`` when both are available. Never return a
    ``Claude HH:MM`` launch-placeholder name. Never return raw tmux ``%N``
    physical ids; those are internal adapter descriptors, not human surfaces.
    """
    position = pane_position_id(pane_label)
    name = human_tab_name(tab_name)
    label = human_pane_label(pane_label)
    if position and name:
        return f"{position} {name}"
    return position or name or label or "session"


def sanitize_human_surface(surface: str | None) -> str | None:
    """Remove physical tmux pane ids from a precomputed human surface."""
    if not surface:
        return None
    cleaned = RAW_TMUX_PANE_RX.sub("", str(surface))
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:,")
    return cleaned or None


def human_pane_label(pane_label: str | None) -> str | None:
    """Return a public non-positional pane label for dynamic workspaces.

    Palace/somnium are rendered through ``pane_position_id``. Dynamic panes
    such as legion/mechanicus do not have a fake 2-D coordinate, but their
    ``@PANE_ID`` role is still a public identifier and is safe to speak/display.
    """
    if not pane_label:
        return None
    label = pane_label.strip()
    if not label or label.startswith("%"):
        return None
    page, _, _slot = label.partition(":")
    if page in _PANE_PAGE_NUMBERS:
        return None
    return label
