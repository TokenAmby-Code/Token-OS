"""Human-safe pane surface formatting.

These helpers are used anywhere a Claude instance/pane identifier is surfaced to
the operator. In particular, they reject launch-placeholder tab names such as
``Claude 08:14`` so those placeholders cannot leak into TTS, push, Discord, or
the daily-note NOW widget.
"""

from __future__ import annotations

import re

DEFAULT_TAB_NAME_RX = re.compile(r"^Claude\s+\d{1,2}:\d{2}$")
# Placeholder stems with optional numeric collision/monotonic suffix:
# needs-name, needs-name-2, needs-session-name-345, unnamed-session-3, session-doc-12, session
PLACEHOLDER_TAB_NAME_RX = re.compile(
    r"^(?:needs-name|needs-session-name|unnamed-session|session-doc|session)(?:-\d+)?$"
)
RAW_TMUX_PANE_RX = re.compile(r"%\d+")


def is_meaningful_tab_name(tab_name: str | None) -> bool:
    """True when ``tab_name`` is not blank and not the default launch stamp."""
    return human_tab_name(tab_name) is not None


def is_placeholder_tab_name(tab_name: str | None) -> bool:
    """True when ``tab_name`` is a launch/placeholder stub, not an agent name.

    Canonical predicate shared by the naming-nudge gate (main) and the
    ``instance_named`` victory criterion (session_doc_helpers). Catches the
    ``needs-name``/``session``/``unnamed-session`` placeholder stems (with an
    optional numeric suffix) and the ``Claude HH:MM`` launch stamp. A blank
    name is *not* a placeholder (callers gate naming on a real placeholder, not
    on absence), matching the historical behavior.
    """
    if not tab_name:
        return False
    cleaned = tab_name.lstrip("✳⠐⠸ ").strip()
    if not cleaned:
        return False
    if PLACEHOLDER_TAB_NAME_RX.match(cleaned):
        return True
    return bool(DEFAULT_TAB_NAME_RX.match(cleaned))


def human_tab_name(tab_name: str | None) -> str | None:
    """Return the cleaned human name, or None for placeholders/blank names."""
    if not tab_name:
        return None
    cleaned = tab_name.lstrip("✳⠐⠸ ").strip()
    if not cleaned:
        return None
    if cleaned in {"needs-name", "needs-session-name"} or DEFAULT_TAB_NAME_RX.match(cleaned):
        return None
    return cleaned


def pane_position_id(pane_label: str | None) -> str | None:
    """Return the stable public ``{page}:{id}`` pane identifier.

    Raw tmux physical ids (``%NNN``) are internal-only and never returned.
    """
    if not pane_label:
        return None
    label = pane_label.strip()
    if not label or RAW_TMUX_PANE_RX.search(label):
        return None
    page, sep, pane_id = label.partition(":")
    if not sep or not page or not pane_id or ":" in pane_id:
        return None
    return f"{page}:{pane_id}"


def human_pane_surface(tab_name: str | None, tmux_pane: str | None, pane_label: str | None) -> str:
    """Return the operator-facing pane surface.

    Prefer ``<{page}:{id}> <name>`` when both are available. Never return a
    ``Claude HH:MM`` launch-placeholder name. Never return raw tmux ``%N``
    physical ids; those are internal adapter descriptors, not human surfaces.
    """
    position = pane_position_id(pane_label)
    name = human_tab_name(tab_name)
    if position and name:
        return f"{position} {name}"
    return position or name or "session"


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

    Compatibility wrapper for older callers. All public labels, including
    palace/somnium, are now rendered as canonical ``{page}:{id}``.
    """
    return pane_position_id(pane_label)
