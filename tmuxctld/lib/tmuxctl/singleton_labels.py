from __future__ import annotations

from .labels import canonical_pane_role

PERSONA_SINGLETON_LABELS = frozenset(
    {
        "council:custodes",
        "legion:custodes",
        "council:malcador",
        "council:administratum",
        "mechanicus:admin",
        "council:pax",
        "mechanicus:fabricator-general",
        "mechanicus:orchestrator",
    }
)


def canonical_singleton_label(label: str | None) -> str:
    raw = (label or "").strip()
    if not raw:
        return ""
    canonical = canonical_pane_role(raw)
    if canonical in PERSONA_SINGLETON_LABELS:
        return canonical
    return raw if raw in PERSONA_SINGLETON_LABELS else canonical


def is_persona_singleton_label(label: str | None) -> bool:
    return canonical_singleton_label(label) in PERSONA_SINGLETON_LABELS
