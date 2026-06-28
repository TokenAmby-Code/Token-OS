"""Tests for the timer-owned "all Claude instances stopped" state assertion.

Regression 2026-05-28: the parallel ``check_instance_count_pavlok`` detector
spoke "All Claude instances stopped" the instant the raw status count hit 0 at a
SessionEnd/DELETE — with no consult of the timer's productivity oracle — so it
false-fired during session churn (compaction / ``/clear`` / model switch /
reaper transient) while agents were visibly alive.

The fix strips that parallel detector and re-homes the assertion as a derived
output of the timer worker's ``productivity_inactive`` -> IDLE transition, which
is gated by ``compute_work_state()`` (live panes + observed agents + recent
work-action buffer). These tests pin both halves:

  * A SessionEnd that zeroes the raw count while ``compute_work_state`` would
    still report ``productivity_active=True`` (a recent ``work_action``) must
    emit NO phone-direct TTS at all.
  * The re-homed emitter (``_announce_idle_if_all_stopped``) records the
    ``all_instances_stopped`` state event on a genuine WORKING -> IDLE
    transition (and only then — silent when work is still active or the mode is
    not IDLE), backing the idle metric.

Update 2026-06-07 (Emperor): the SPOKEN assertion is DISABLED — it fired at the
momentary stop boundary and was hyper-spammy. The state event is retained (it
backs the idle metric and the frozen ``idle_buffer``/``idle`` namespace rework);
only the TTS is suppressed, so the emitter now never speaks and the quiet-hours
gate (which only guarded the speak) is gone.
"""

import sqlite3
import sys
from datetime import datetime

import pytest


def _insert_instance(db_path, instance_id, *, status="processing", is_subagent=0):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status,
            instance_type, engine, is_subagent, last_activity)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            instance_id,
            "Worker",
            "/tmp",
            "local",
            "Mac-Mini",
            status,
            "interactive",
            "claude",
            is_subagent,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _insert_recent_work_action(db_path):
    """A work_action inside the 3-min buffer keeps productivity_active=True even
    with zero registered instances (compute_work_state recent_work_action arm)."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_type, created_at, details) VALUES (?, datetime('now'), ?)",
        ("work_action", '{"source": "stream-deck", "note": "test"}'),
    )
    conn.commit()
    conn.close()


def test_session_end_zeroing_count_does_not_emit_all_stopped(app_env, monkeypatch):
    """SessionEnd dropping the count to 0 must NOT speak 'All Claude instances
    stopped'. The timer's oracle (recent work_action) still reports work active,
    and the SessionEnd hook no longer owns any all-stopped detection at all.

    RED against the stripped detector: ``check_instance_count_pavlok`` fired a
    phone-direct ``tts_text`` regardless of productivity the moment the raw count
    reached 0.
    """
    from fastapi.testclient import TestClient

    main = app_env.main
    hooks = sys.modules["routes.hooks"]
    phone_service = sys.modules["phone_service"]

    phone_calls = []

    def fake_send_to_phone(endpoint, params):
        phone_calls.append((endpoint, dict(params or {})))
        return {"success": True, "status_code": 200}

    # Intercept every phone-direct path the old detector could take.
    monkeypatch.setattr(phone_service, "_send_to_phone", fake_send_to_phone)
    # Keep the hook from spawning stop_hook subprocesses in the test.
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: None)

    _insert_instance(app_env.db_path, "sess-churn")
    _insert_recent_work_action(app_env.db_path)

    client = TestClient(main.app)
    resp = client.post("/api/hooks/SessionEnd", json={"session_id": "sess-churn"})
    assert resp.status_code == 200

    spoken = [params for (_endpoint, params) in phone_calls if params.get("tts_text")]
    assert spoken == [], f"SessionEnd must not emit a phone-direct TTS; got {spoken}"
    all_stopped = [
        params
        for (_endpoint, params) in phone_calls
        if "All Claude instances stopped" in str(params.get("tts_text", ""))
    ]
    assert all_stopped == [], f"'All Claude instances stopped' false-fired: {all_stopped}"


def test_delete_instance_zeroing_count_does_not_emit_all_stopped(app_env, monkeypatch):
    """The DELETE /api/instances/{id} path must likewise no longer own any
    all-stopped detection — the second stripped call site."""
    from fastapi.testclient import TestClient

    main = app_env.main
    phone_service = sys.modules["phone_service"]

    phone_calls = []

    def fake_send_to_phone(endpoint, params):
        phone_calls.append((endpoint, dict(params or {})))
        return {"success": True, "status_code": 200}

    monkeypatch.setattr(phone_service, "_send_to_phone", fake_send_to_phone)

    _insert_instance(app_env.db_path, "del-churn")
    _insert_recent_work_action(app_env.db_path)

    client = TestClient(main.app)
    resp = client.delete("/api/instances/del-churn")
    assert resp.status_code == 200

    all_stopped = [
        params
        for (_endpoint, params) in phone_calls
        if "All Claude instances stopped" in str(params.get("tts_text", ""))
    ]
    assert all_stopped == [], f"DELETE false-fired all-stopped: {all_stopped}"


@pytest.mark.asyncio
async def test_announce_idle_logs_event_but_does_not_speak(app_env, monkeypatch):
    """TTS DISABLED 2026-06-07 (Emperor): a genuine productivity_inactive
    WORKING -> IDLE transition still RECORDS the ``all_instances_stopped`` state
    event (it backs the idle metric and the frozen ``idle_buffer``/``idle``
    namespace rework) but speaks NOTHING — the spoken assertion was hyper-spammy
    at the momentary stop boundary. Returns False (never speaks)."""
    import asyncio

    main = app_env.main

    spoken = []
    monkeypatch.setattr(
        main, "speak_tts", lambda *a, **k: spoken.append((a, k)) or {"success": True}
    )
    logged = []

    async def fake_log_event(event_type, **kwargs):
        logged.append((event_type, kwargs))

    monkeypatch.setattr(main, "log_event", fake_log_event)
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda *a, **k: {"active": False})

    result = await main._announce_idle_if_all_stopped("working", "idle", False)
    assert result is False

    # The state event is still recorded (metric preserved) ...
    assert [et for (et, _k) in logged] == ["all_instances_stopped"], (
        f"expected the state event to be logged once, got {logged}"
    )
    # ... but no TTS is ever emitted (the spam is gone).
    await asyncio.sleep(0.1)
    assert spoken == [], f"expected no TTS emission, got {spoken}"


@pytest.mark.asyncio
async def test_announce_idle_silent_when_productivity_still_active(app_env, monkeypatch):
    """No emission while work is still active — the oracle, not a raw count,
    decides. This is the false-fire guard."""
    main = app_env.main

    spoken = []
    monkeypatch.setattr(
        main, "speak_tts", lambda *a, **k: spoken.append((a, k)) or {"success": True}
    )
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda *a, **k: {"active": False})

    result = await main._announce_idle_if_all_stopped("working", "working", True)
    assert result is False
    assert spoken == []


@pytest.mark.asyncio
async def test_announce_idle_silent_when_not_idle_mode(app_env, monkeypatch):
    """A productivity_inactive transition into BREAK (distraction-driven), not
    IDLE, is a different signal and must not speak the all-stopped assertion."""
    main = app_env.main

    spoken = []
    monkeypatch.setattr(
        main, "speak_tts", lambda *a, **k: spoken.append((a, k)) or {"success": True}
    )
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)
    monkeypatch.setattr(main.shared, "get_quiet_hours_status", lambda *a, **k: {"active": False})

    result = await main._announce_idle_if_all_stopped("working", "break", False)
    assert result is False
    assert spoken == []
