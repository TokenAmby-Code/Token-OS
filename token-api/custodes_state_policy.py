"""Policy for turning state events into Custodes intervention prompts.

This module is deliberately pure: it accepts a normalized event and a snapshot
of current state, then returns either a structured intervention or None.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

V1_TRIGGERS = {
    "idle_timeout",
    "distraction_timeout",
    "break_exhausted",
    "phone_distraction_blocked",
    "desktop_mode_blocked",
    "enforcement_cascade_started",
}


@dataclass(frozen=True)
class StateEvent:
    event_type: str
    source: str
    instance_id: str | None = None
    severity: int | None = None
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class CustodesIntervention:
    event_type: str
    dedupe_key: str
    severity: int
    prompt: str
    reason: str
    payload: dict[str, Any]


def normalize_severity(value: int | str | None) -> int:
    if value is None:
        return 1
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def build_dedupe_key(event: StateEvent) -> str:
    payload = event.payload or {}
    subject = (
        payload.get("app")
        or payload.get("phone_app")
        or payload.get("desktop_mode")
        or payload.get("mode")
        or event.instance_id
        or "global"
    )
    return f"{event.event_type}:{event.source}:{subject}"


def _format_minutes(ms: Any) -> str | None:
    try:
        minutes = round(abs(int(ms)) / 60000)
    except (TypeError, ValueError):
        return None
    return f"-{minutes}m" if int(ms) < 0 else f"{minutes}m"


def _snapshot_items(snapshot: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    timer = snapshot.get("timer") or {}
    phone = snapshot.get("phone") or {}
    desktop = snapshot.get("desktop") or {}

    phone_app = payload.get("phone_app") or payload.get("app") or phone.get("current_app")
    timer_mode = payload.get("timer_mode") or timer.get("current_mode") or timer.get("mode")
    desktop_mode = payload.get("desktop_mode") or desktop.get("current_mode")
    break_balance = (
        payload.get("break_balance")
        or payload.get("break_balance_ms")
        or timer.get("break_balance")
        or timer.get("break_balance_ms")
    )

    items: list[str] = []
    if phone_app:
        items.append(f"phone_app={phone_app}")
    if timer_mode:
        items.append(f"timer_mode={timer_mode}")
    if desktop_mode:
        items.append(f"desktop_mode={desktop_mode}")
    formatted_balance = _format_minutes(break_balance)
    if formatted_balance:
        items.append(f"break_balance={formatted_balance}")
    if payload.get("reason"):
        items.append(f"reason={payload['reason']}")
    return items


def evaluate_state_event(
    event: StateEvent,
    snapshot: dict[str, Any] | None = None,
) -> CustodesIntervention | None:
    """Return a Custodes intervention for high-signal events only."""
    if event.event_type not in V1_TRIGGERS:
        return None

    payload = event.payload or {}
    snapshot = snapshot or {}
    severity = normalize_severity(event.severity)
    dedupe_key = build_dedupe_key(event)
    observed = ", ".join(_snapshot_items(snapshot, payload)) or "no extra state"

    direction = {
        "idle_timeout": "Intervene with the Emperor about low-hanging fruit to restart work.",
        "distraction_timeout": "Intervene immediately: close the distraction loop and redirect to one concrete work action.",
        "break_exhausted": "Intervene about exhausted break balance and restart work with the smallest next action.",
        "phone_distraction_blocked": "Intervene about the blocked phone distraction and redirect attention back to work.",
        "desktop_mode_blocked": "Intervene about the blocked desktop mode and redirect to the active task.",
        "enforcement_cascade_started": "Intervene because enforcement has escalated; get explicit closure from the Emperor.",
    }[event.event_type]

    prompt = (
        f"State hook: {event.event_type}. Observed {observed}. "
        f"{direction} Be direct; do not over-explain."
    )
    return CustodesIntervention(
        event_type=event.event_type,
        dedupe_key=dedupe_key,
        severity=severity,
        prompt=prompt,
        reason="v1_trigger",
        payload=payload,
    )
