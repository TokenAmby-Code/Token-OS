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
    "enforcement_cascade_escalate",
    "expected_ack_escalated",
}

# Hooks that carry a physical enforcement action (Pavlok / window-close / ack
# ladder). These remain Custodes' surface (the escalation tier) — everything
# else in V1_TRIGGERS is pure state and routes to Administratum only.
# Boundary chosen by the Emperor 2026-05-30: "Pavlok-fired = enforcement."
# See Terra/Ultramar/{Custodes Trinity, Inter-Persona Communication}.
ENFORCEMENT_TRIGGERS = {
    "enforcement_cascade_started",
    "enforcement_cascade_escalate",
    "expected_ack_escalated",
    "phone_distraction_blocked",
    "desktop_mode_blocked",
}


def classify_trigger(event_type: str) -> str:
    """Classify a recognized trigger as 'enforcement' or 'state'.

    State hooks → Administratum (recorder). Enforcement hooks → Custodes
    (escalator), with a record-keeping copy to Administratum.
    """
    return "enforcement" if event_type in ENFORCEMENT_TRIGGERS else "state"


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
    # `observed` is the raw metadata line (record-keeping → Administratum).
    # `behavioral_prompt` is the metadata-stripped directive sent to Custodes
    # for enforcement hooks. Defaults keep older constructors working.
    observed: str = ""
    behavioral_prompt: str = ""


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
        payload.get("phone_app")
        or payload.get("app")
        or payload.get("ack_source")
        or payload.get("desktop_mode")
        or payload.get("mode")
        or event.instance_id
        or "global"
    )
    base = f"{event.event_type}:{event.source}:{subject}"
    if event.event_type == "enforcement_cascade_escalate" and payload.get("level") is not None:
        return f"{base}:level={payload['level']}"
    if event.event_type == "expected_ack_escalated":
        ack_id = payload.get("ack_id")
        level = payload.get("level")
        if ack_id is not None and level is not None:
            return f"{base}:ack={ack_id}:level={level}"
        if level is not None:
            return f"{base}:level={level}"
    return base


def _format_minutes(ms: Any) -> str | None:
    try:
        minutes = round(abs(int(ms)) / 60000)
    except (TypeError, ValueError):
        return None
    return f"-{minutes}m" if int(ms) < 0 else f"{minutes}m"


PHONE_APP_SOURCES = {
    "phone",
    "phone_detection",
    "phone_distraction",
    "phone_gaming",
    "backlog_violation",
}


def _source_allows_app_as_phone_app(source: str) -> bool:
    return source in PHONE_APP_SOURCES or source.startswith("phone_")


def _snapshot_items(
    snapshot: dict[str, Any],
    payload: dict[str, Any],
    source: str,
) -> list[str]:
    timer = snapshot.get("timer") or {}
    phone = snapshot.get("phone") or {}
    desktop = snapshot.get("desktop") or {}

    payload_app = payload.get("app")
    explicit_phone_app = payload.get("phone_app")
    ack_source = payload.get("ack_source")
    phone_app = explicit_phone_app
    app = None
    if not phone_app and payload_app:
        if _source_allows_app_as_phone_app(source):
            phone_app = payload_app
        else:
            app = payload_app
    if not phone_app and not app and _source_allows_app_as_phone_app(source):
        phone_app = phone.get("current_app")
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
    if app:
        items.append(f"app={app}")
    if ack_source:
        items.append(f"ack_source={ack_source}")
    if timer_mode:
        items.append(f"timer_mode={timer_mode}")
    if desktop_mode:
        items.append(f"desktop_mode={desktop_mode}")
    formatted_balance = _format_minutes(break_balance)
    if formatted_balance:
        items.append(f"break_balance={formatted_balance}")
    if payload.get("reason"):
        items.append(f"reason={payload['reason']}")

    cascades_today = snapshot.get("cascade_count_today")
    if cascades_today is not None:
        items.append(f"cascades_today={cascades_today}")
    open_panes = snapshot.get("open_panes")
    if open_panes is not None:
        items.append(f"open_panes={open_panes}")
    threads = snapshot.get("active_threads")
    if isinstance(threads, dict):
        thread_count = threads.get("count")
        if thread_count is not None:
            items.append(f"active_threads={thread_count}")
        names = threads.get("names") or []
        if names:
            joined = ",".join(str(n) for n in list(names)[:3])
            items.append(f"thread_names={joined}")
    elif threads is not None:
        items.append(f"active_threads={threads}")

    if payload.get("level") is not None:
        items.append(f"level={payload['level']}")
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
    observed = ", ".join(_snapshot_items(snapshot, payload, event.source)) or "no extra state"

    direction = {
        "idle_timeout": "Intervene with the Emperor about low-hanging fruit to restart work.",
        "distraction_timeout": "Intervene immediately: close the distraction loop and redirect to one concrete work action.",
        "break_exhausted": "Intervene about exhausted break balance and restart work with the smallest next action.",
        "phone_distraction_blocked": "Intervene about the blocked phone distraction and redirect attention back to work.",
        "desktop_mode_blocked": "Intervene about the blocked desktop mode and redirect to the active task.",
        "enforcement_cascade_started": "Intervene because enforcement has escalated; get explicit closure from the Emperor.",
        "enforcement_cascade_escalate": "Intervene about active escalation; the loop is escalating — get explicit closure now.",
        "expected_ack_escalated": "Intervene about the missed acknowledgement ladder; mirror the Discord-channel cascade and pull the Emperor back to the work surface.",
    }[event.event_type]

    prompt = (
        f"State hook: {event.event_type}. Observed {observed}. "
        f"{direction} Be direct; do not over-explain. "
        "AFK rule: state hooks imply the Emperor is not watching this thread. "
        "Reach him out-of-band — TTS (`tts ...` / /api/notify), "
        "or the Discord daily thread. "
        "Do NOT reply with in-thread text only; in-thread text is invisible until he returns."
    )
    # Metadata-stripped variant for Custodes: behavioral directive only, no
    # observed-state dump. The full metadata lives in the Administratum record.
    behavioral_prompt = (
        f"Enforcement hook: {event.event_type}. {direction} "
        "Be direct; do not over-explain. "
        "AFK rule: the Emperor is not watching this thread — reach him out-of-band "
        "(TTS via `tts ...` / /api/notify, or the Discord daily thread). "
        "Do NOT reply with in-thread text only; it is invisible until he returns."
    )
    return CustodesIntervention(
        event_type=event.event_type,
        dedupe_key=dedupe_key,
        severity=severity,
        prompt=prompt,
        reason="v1_trigger",
        payload=payload,
        observed=observed,
        behavioral_prompt=behavioral_prompt,
    )
