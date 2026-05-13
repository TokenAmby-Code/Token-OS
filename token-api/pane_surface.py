"""Helpers for stable, human-readable pane/instance surface names."""
import re

DEFAULT_TAB_NAME_RX = re.compile(r"^Claude(?:\s+\d{1,2}:\d{2})?$", re.IGNORECASE)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lstrip("✳ ").strip()
    return text or None


def is_meaningful_tab_name(tab_name: str | None) -> bool:
    text = _clean(tab_name)
    if not text:
        return False
    return not DEFAULT_TAB_NAME_RX.match(text)


def human_tab_name(tab_name: str | None) -> str | None:
    text = _clean(tab_name)
    if not text or DEFAULT_TAB_NAME_RX.match(text):
        return None
    return text


def human_pane_surface(
    tab_name: str | None,
    tmux_pane: str | None = None,
    pane_label: str | None = None,
) -> str:
    name = human_tab_name(tab_name)
    if name:
        return name
    label = _clean(pane_label)
    if label:
        return label
    pane = _clean(tmux_pane)
    if pane:
        return pane
    return "agent"
