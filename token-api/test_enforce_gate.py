"""Coverage for the work-session enforcement sidecar gate.

The sidecar imposes mutual exclusion at the single ``enforce()`` chokepoint:
while a work session owns enforcement, every *generic* zap (the break-exhausted
cascade@50, the negative-break loop) is suppressed there, and only the work
session's own ``work_session_*`` zaps pass. The gate itself is a generic,
late-bound predicate in ``enforce.py``; the work-session knowledge lives in
``main._work_session_enforcement_gate`` / ``main.WORK_SESSION_ENFORCE_SOURCES``.

These tests exercise the generic gate hook in ``enforce.py`` directly, driving
it with a predicate that mirrors the real one in ``main`` (kept in sync via the
``WORK_SESSION_ENFORCE_SOURCES`` constant below). Importing the full ``main``
module just to reach the predicate would pull heavy import-time side effects
into the CI pytest run; the gate behaviour is fully observable at the chokepoint.
"""

from __future__ import annotations

import pytest

import enforce
from enforce import EnforceRequest

# Mirror of main.WORK_SESSION_ENFORCE_SOURCES — the work session's own two zap
# sources, the only ones allowed through the sidecar gate.
WORK_SESSION_ENFORCE_SOURCES = {"work_session_negative", "work_session_failed"}


@pytest.fixture
def gate_env(monkeypatch):
    """Neutralize the other guardrails + real Pavlok/notify side effects so a
    test observes only the sidecar gate's decision.

    Returns ``(calls, state)`` where ``calls["zaps"]`` records every Pavlok
    stimulus that actually reached the device, and ``state["work_session_active"]``
    is the mutable flag the injected gate predicate reads (stands in for
    ``timer_engine.work_session_active``).
    """
    calls = {"zaps": []}

    def fake_zap(*, stimulus_type, value, reason):
        calls["zaps"].append({"type": stimulus_type, "value": value, "reason": reason})
        return {"ok": True, "type": stimulus_type, "value": value}

    async def fake_notify(req):
        return {"ok": True}

    async def fake_log(event, details=None):
        return None

    monkeypatch.setattr(enforce, "send_pavlok_stimulus", fake_zap)
    monkeypatch.setattr(enforce, "dispatch_notification", fake_notify)
    monkeypatch.setattr(enforce, "log_event", fake_log)
    # Disable the other guardrails so they never pre-empt the gate decision.
    monkeypatch.setattr(enforce, "_is_quiet_hours", lambda: False)
    monkeypatch.setattr(enforce, "_typing_guard_active", lambda: False)
    monkeypatch.setattr(enforce, "_dictation_active", lambda: False)
    monkeypatch.setattr(enforce, "_in_meeting", lambda: False)

    state = {"work_session_active": False}

    def gate(source: str) -> str | None:
        # Identical logic to main._work_session_enforcement_gate.
        if state["work_session_active"] and source not in WORK_SESSION_ENFORCE_SOURCES:
            return "work_session_sidecar"
        return None

    monkeypatch.setattr(enforce, "_enforcement_gate", gate)
    return calls, state


async def _fire(source: str, intensity: int = 50) -> dict:
    return await enforce.enforce(EnforceRequest(message="test", intensity=intensity, source=source))


async def test_gate_suppresses_generic_sources_during_session(gate_env):
    calls, state = gate_env
    state["work_session_active"] = True
    for src in ("phone_distraction_youtube", "negative_break_loop", "api"):
        res = await _fire(src)
        assert res == {"fired": False, "blocked_by": "work_session_sidecar"}
    assert calls["zaps"] == []  # nothing reached the device


async def test_gate_allows_work_session_sources_during_session(gate_env):
    calls, state = gate_env
    state["work_session_active"] = True
    for src in ("work_session_negative", "work_session_failed"):
        res = await _fire(src, intensity=100)
        assert res["fired"] is True
    assert [z["value"] for z in calls["zaps"]] == [100, 100]


async def test_all_sources_fire_when_no_session(gate_env):
    calls, state = gate_env
    state["work_session_active"] = False
    for src in ("phone_distraction_youtube", "negative_break_loop", "work_session_negative"):
        res = await _fire(src)
        assert res["fired"] is True
    assert len(calls["zaps"]) == 3


async def test_no_gate_registered_is_noop(gate_env, monkeypatch):
    """A missing gate must never block — other subsystems stay unaffected."""
    calls, state = gate_env
    monkeypatch.setattr(enforce, "_enforcement_gate", None)
    state["work_session_active"] = True  # would block if the gate were live
    res = await _fire("phone_distraction_youtube")
    assert res["fired"] is True
    assert len(calls["zaps"]) == 1


async def test_storm_single_zap_on_failure_with_active_distraction(gate_env):
    """Storm regression (work-session physical-pass A2 finding #2).

    A work-session failure with an active phone distraction used to deliver TWO
    zaps in one event: the work-session authoritative zap@100, then the
    break-exhausted cascade's generic zap@50 (reached via
    ``enforce_break_exhausted_impl`` -> ``start_enforcement_cascade``). With the
    sidecar gate the cascade's generic ``phone_distraction_*`` zap is suppressed,
    so the same event lands exactly one zap — the work-session 100.
    """
    calls, state = gate_env
    state["work_session_active"] = True
    # 1. The work session fires its own authoritative zap@100 (allowlisted).
    r1 = await _fire("work_session_failed", intensity=100)
    # 2. enforce_break_exhausted_impl -> start_enforcement_cascade then fires the
    #    generic phone-distraction zap@50 — now suppressed by the gate.
    r2 = await _fire("phone_distraction_youtube", intensity=50)
    assert r1["fired"] is True
    assert r2 == {"fired": False, "blocked_by": "work_session_sidecar"}
    assert len(calls["zaps"]) == 1
    assert calls["zaps"][0]["value"] == 100


def test_predicate_matches_main_if_importable():
    """If ``main`` imports in this environment, assert the real predicate +
    allowlist match the mirror used above. Skipped (not failed) where importing
    ``main`` is not viable (e.g. minimal CI sandbox)."""
    try:
        import main
    except Exception as exc:  # noqa: BLE001 — any import failure → skip, never fail CI
        pytest.skip(f"main not importable in this environment: {exc}")
    assert main.WORK_SESSION_ENFORCE_SOURCES == WORK_SESSION_ENFORCE_SOURCES
    engine = main.timer_engine
    original = engine._work_session_active
    try:
        engine._work_session_active = True
        assert main._work_session_enforcement_gate("phone_distraction_x") == "work_session_sidecar"
        assert main._work_session_enforcement_gate("negative_break_loop") == "work_session_sidecar"
        assert main._work_session_enforcement_gate("work_session_negative") is None
        assert main._work_session_enforcement_gate("work_session_failed") is None
        engine._work_session_active = False
        assert main._work_session_enforcement_gate("phone_distraction_x") is None
    finally:
        engine._work_session_active = original
