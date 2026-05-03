import json
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest


def _rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


@pytest.mark.asyncio
async def test_expected_ack_creation_persists_deadlines_and_logs_event(app_env):
    ack = await app_env.main.create_expected_ack(
        source="test",
        instance_id="inst-1",
        reason="unit test ack",
        details={"kind": "unit"},
    )

    rows = _rows(
        app_env.db_path,
        "SELECT * FROM expected_acknowledgements WHERE id = ?",
        (ack["id"],),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "pending"
    assert row["source"] == "test"
    assert row["instance_id"] == "inst-1"

    created_at = datetime.fromisoformat(row["created_at"])
    ack_due_at = datetime.fromisoformat(row["ack_due_at"])
    level2_due_at = datetime.fromisoformat(row["level2_due_at"])
    pavlok_due_at = datetime.fromisoformat(row["pavlok_due_at"])
    assert ack_due_at - created_at == timedelta(minutes=5)
    assert level2_due_at - created_at == timedelta(minutes=10)
    assert pavlok_due_at - created_at == timedelta(minutes=15)

    event = _rows(
        app_env.db_path,
        "SELECT event_type, instance_id, details FROM events ORDER BY id DESC LIMIT 1",
    )[0]
    assert event["event_type"] == "expected_ack_created"
    assert event["instance_id"] == "inst-1"
    assert json.loads(event["details"])["id"] == ack["id"]


def test_enforcement_ack_endpoint_resolves_pending_ack(app_env):
    from fastapi.testclient import TestClient

    client = TestClient(app_env.main.app)
    ack = app_env.main._expected_ack_deadlines()
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """
        INSERT INTO expected_acknowledgements (
            id, source, instance_id, reason, status, created_at,
            ack_due_at, level2_due_at, pavlok_due_at, details_json
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            "ack-1",
            "test",
            "inst-1",
            "manual insert",
            ack["created_at"].isoformat(),
            ack["ack_due_at"].isoformat(),
            ack["level2_due_at"].isoformat(),
            ack["pavlok_due_at"].isoformat(),
            "{}",
        ),
    )
    conn.commit()
    conn.close()

    resp = client.post("/api/enforcement/ack", json={"ack_id": "ack-1"})
    assert resp.status_code == 200
    assert resp.json()["updated"] is True

    row = _rows(app_env.db_path, "SELECT status, acknowledged_at FROM expected_acknowledgements")[0]
    assert row["status"] == "acknowledged"
    assert row["acknowledged_at"]


def test_enforcement_expect_endpoint_creates_manual_ack(app_env):
    from fastapi.testclient import TestClient

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/api/enforcement/expect",
        json={
            "source": "manual",
            "reason": "manual enforcement test",
            "details": {"mode": "cooked_day"},
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    ack_id = body["ack"]["id"]

    row = _rows(
        app_env.db_path,
        "SELECT source, reason, status, details_json FROM expected_acknowledgements WHERE id = ?",
        (ack_id,),
    )[0]
    assert row["source"] == "manual"
    assert row["reason"] == "manual enforcement test"
    assert row["status"] == "pending"
    assert json.loads(row["details_json"])["mode"] == "cooked_day"


@pytest.mark.asyncio
async def test_expected_ack_escalation_ladder_paths(app_env, monkeypatch):
    calls = []

    async def fake_unified(level, message, **kwargs):
        calls.append(("unified", level, message, kwargs))
        return {"ok": True, "level": level}

    def fake_pavlok(stimulus_type, value, reason, respect_cooldown):
        calls.append(("pavlok", stimulus_type, value, reason, respect_cooldown))
        return {"success": True}

    monkeypatch.setattr(app_env.main, "unified_enforce", fake_unified)
    monkeypatch.setattr(app_env.main, "send_pavlok_stimulus", fake_pavlok)

    ack = await app_env.main.create_expected_ack(
        source="golden_throne",
        instance_id="inst-2",
        reason="GT follow-up",
    )

    await app_env.main._expected_ack_escalate(ack["id"], 1)
    await app_env.main._expected_ack_escalate(ack["id"], 2)
    await app_env.main._expected_ack_escalate(ack["id"], 3)

    assert calls[0][0:2] == ("unified", "notify")
    assert calls[1][0:2] == ("unified", "warn")
    assert calls[2] == ("pavlok", "zap", 30, "expected_ack_golden_throne", True)

    row = _rows(
        app_env.db_path,
        "SELECT status FROM expected_acknowledgements WHERE id = ?",
        (ack["id"],),
    )[0]
    assert row["status"] == "expired"


@pytest.mark.asyncio
async def test_expected_ack_level_is_idempotent(app_env, monkeypatch):
    calls = []

    async def fake_unified(level, message, **kwargs):
        calls.append((level, message, kwargs))
        return {"ok": True}

    monkeypatch.setattr(app_env.main, "unified_enforce", fake_unified)
    ack = await app_env.main.create_expected_ack(
        source="golden_throne",
        instance_id="inst-idempotent",
        reason="idempotency test",
    )

    first = await app_env.main._expected_ack_escalate(ack["id"], 1)
    second = await app_env.main._expected_ack_escalate(ack["id"], 1)

    assert first["level"] == 1
    assert second["skipped"] is True
    assert second["reason"] == "level_already_fired"
    assert len(calls) == 1
    row = _rows(
        app_env.db_path,
        "SELECT fired_levels_json FROM expected_acknowledgements WHERE id = ?",
        (ack["id"],),
    )[0]
    assert json.loads(row["fired_levels_json"]) == [1]


def test_pavlok_guardrails_cover_cap_cooldown_quiet_and_contexts(app_env):
    fixed = datetime(2026, 5, 1, 14, 0, 0)
    phone_service = sys.modules["phone_service"]
    phone_service.PAVLOK_STATE.update(
        {
            "zap_count_date": "2026-05-01",
            "zap_count": 6,
            "last_zap_at": None,
            "last_soft_at": None,
        }
    )
    assert phone_service._pavlok_guardrail_block("zap", fixed, True)["reason"] == "daily_zap_cap"

    phone_service.PAVLOK_STATE.update({"zap_count": 0, "last_zap_at": fixed.isoformat()})
    assert (
        phone_service._pavlok_guardrail_block("zap", fixed + timedelta(minutes=5), True)["reason"]
        == "cooldown"
    )

    phone_service.PAVLOK_STATE["last_zap_at"] = None
    phone_service.TTS_GLOBAL_MODE["mode"] = "silent"
    assert phone_service._pavlok_guardrail_block("zap", fixed, True)["reason"] == "quiet_mode"
    phone_service.TTS_GLOBAL_MODE["mode"] = "verbose"

    phone_service.DESKTOP_STATE["in_meeting"] = True
    assert phone_service._pavlok_guardrail_block("zap", fixed, True)["reason"] == "meeting"
    phone_service.DESKTOP_STATE["in_meeting"] = False


def test_phone_slay_the_spire_break_mode_does_not_create_ack(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"
    app_env.main.timer_engine._break_balance_ms = 60_000

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/phone",
        json={"app": "slay the spire", "action": "open", "package": "com.humble.slaythespire"},
    )

    assert resp.status_code == 200
    assert resp.json()["allowed"] is True
    assert resp.json()["reason"] == "break_time_available"
    rows = _rows(app_env.db_path, "SELECT * FROM expected_acknowledgements")
    assert rows == []


def test_phone_slay_the_spire_work_mode_creates_ack(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"
    app_env.main.timer_engine._break_balance_ms = 0

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/phone",
        json={"app": "slay the spire", "action": "open", "package": "com.humble.slaythespire"},
    )

    assert resp.status_code == 200
    assert resp.json()["allowed"] is True
    assert resp.json()["reason"] == "ack_required"
    rows = _rows(
        app_env.db_path, "SELECT source, reason, details_json FROM expected_acknowledgements"
    )
    assert len(rows) == 1
    assert rows[0]["source"] == "phone_gaming"
    assert "Slay the Spire" in rows[0]["reason"]
    assert json.loads(rows[0]["details_json"])["timer_mode"]


@pytest.mark.asyncio
async def test_sustained_phone_distraction_creates_ack_even_with_break_time(app_env):
    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"
    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "app_opened_at": (datetime.now() - timedelta(minutes=2)).isoformat(),
            "is_distracted": True,
        }
    )
    app_env.main.timer_engine._break_balance_ms = 10 * 60 * 1000

    ack = await app_env.main.maybe_create_phone_distraction_ack(
        app_name="youtube",
        display_name="YouTube",
        package="com.google.android.youtube",
        distraction_mode="video",
        trigger="unit_test",
        productivity_active=False,
    )

    assert ack is not None
    rows = _rows(
        app_env.db_path, "SELECT source, instance_id, reason FROM expected_acknowledgements"
    )
    assert rows[0]["source"] == "phone_distraction"
    assert rows[0]["instance_id"] == "phone_distraction:phone:youtube"
    assert "YouTube" in rows[0]["reason"]


def test_phone_open_in_backlog_creates_compressed_backlog_ack(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"
    app_env.main.timer_engine._break_balance_ms = -6_000
    monkeypatch.setattr(app_env.main, "start_enforcement_cascade", lambda app: None)

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/phone",
        json={"app": "youtube", "action": "open", "package": "com.google.android.youtube"},
    )

    assert resp.status_code == 200
    assert resp.json()["reason"] == "backlog_violation"
    rows = _rows(
        app_env.db_path,
        "SELECT source, status, created_at, ack_due_at, pavlok_due_at, details_json FROM expected_acknowledgements",
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "backlog_violation"
    created_at = datetime.fromisoformat(row["created_at"])
    ack_due_at = datetime.fromisoformat(row["ack_due_at"])
    pavlok_due_at = datetime.fromisoformat(row["pavlok_due_at"])
    assert ack_due_at - created_at == timedelta(seconds=0)
    assert pavlok_due_at - created_at == timedelta(seconds=15)
    assert json.loads(row["details_json"])["break_balance_ms"] < 0


def test_work_action_acknowledges_phone_and_backlog_acks(app_env):
    from fastapi.testclient import TestClient

    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
    for ack_id, source, instance_id in (
        ("phone-ack", "phone_distraction", "phone_distraction:phone:youtube"),
        ("backlog-ack", "backlog_violation", "backlog:phone:youtube"),
    ):
        conn.execute(
            """
            INSERT INTO expected_acknowledgements (
                id, source, instance_id, reason, status, created_at,
                ack_due_at, level2_due_at, pavlok_due_at, details_json
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                ack_id,
                source,
                instance_id,
                "pending work action test",
                now.isoformat(),
                (now + timedelta(seconds=1)).isoformat(),
                (now + timedelta(seconds=15)).isoformat(),
                (now + timedelta(seconds=15)).isoformat(),
                "{}",
            ),
        )
    conn.commit()
    conn.close()

    app_env.main.PHONE_STATE.update(
        {"current_app": "youtube", "app_opened_at": now.isoformat(), "is_distracted": True}
    )
    client = TestClient(app_env.main.app)
    resp = client.post("/api/work-action", json={"source": "unit", "note": "test"})

    assert resp.status_code == 200
    assert resp.json()["acknowledged_expected_acks"] == 2
    rows = _rows(
        app_env.db_path,
        "SELECT status FROM expected_acknowledgements ORDER BY id",
    )
    assert [row["status"] for row in rows] == ["acknowledged", "acknowledged"]
    assert app_env.main.PHONE_STATE["is_distracted"] is False


@pytest.mark.asyncio
async def test_quiet_state_buster_exits_quiet_and_sets_one_hour_resume(app_env):
    await app_env.main.enter_quiet_mode_internal(context="sleeping", source="unit_test")
    assert app_env.main.timer_engine.current_mode == app_env.main.TimerMode.QUIET

    result = await app_env.main.bust_quiet_state(
        "unit_test",
        "phone_distraction_open",
        {"app": "youtube"},
    )

    assert result["busted"] is True
    assert app_env.main.timer_engine.current_mode != app_env.main.TimerMode.QUIET
    assert app_env.main.scheduler.get_job(app_env.main.QUIET_RESUME_JOB_ID) is not None


@pytest.mark.asyncio
async def test_quiet_buster_activity_reschedules_only_existing_resume(app_env):
    result = await app_env.main.bust_quiet_state(
        "unit_test",
        "work_action",
        {"note": "not quiet and no pending resume"},
    )
    assert result == {"busted": False}

    await app_env.main.enter_quiet_mode_internal(context="sleeping", source="unit_test")
    await app_env.main.bust_quiet_state("unit_test", "phone_distraction_open", {"app": "youtube"})
    result = await app_env.main.bust_quiet_state(
        "unit_test",
        "work_action",
        {"note": "activity during busted quiet"},
    )
    assert result["resume_rescheduled"] is True


def test_work_state_counts_idle_tracked_instance_as_productive(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    async def no_observed_agents():
        return []

    async def tmux_pane_rows():
        return [("%42", "codex", "/Volumes/Imperium/Pax-ENV", "Codex Pax", "")]

    async def pane_exists(pane_id):
        return pane_id == "%42"

    monkeypatch.setattr(app_env.main, "_detect_tmux_agent_panes", no_observed_agents)
    monkeypatch.setattr(app_env.main, "_tmux_pane_rows", tmux_pane_rows)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status, last_activity, tmux_pane)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "inst-work-state",
            "session-work-state",
            "Codex Pax",
            "/Volumes/Imperium/Pax-ENV",
            "local",
            app_env.main.LOCAL_DEVICE_NAME,
            "idle",
            datetime.now().isoformat(),
            "%42",
        ),
    )
    conn.commit()
    conn.close()

    client = TestClient(app_env.main.app)
    resp = client.get("/api/work-state")

    assert resp.status_code == 200
    body = resp.json()
    assert body["productivity_active"] is True
    assert body["active_instance_count"] == 1
    assert body["active_instances"][0]["working_dir"] == "/Volumes/Imperium/Pax-ENV"


def test_work_state_resolves_noncanonical_tmux_target(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    async def no_observed_agents():
        return []

    async def tmux_pane_rows():
        return [("%42", "codex", "/Volumes/Imperium/Pax-ENV", "Codex Pax", "")]

    async def pane_exists(pane_id):
        return pane_id == "main:codex.1"

    async def resolve_pane_id(pane_id):
        return "%42" if pane_id == "main:codex.1" else None

    monkeypatch.setattr(app_env.main, "_detect_tmux_agent_panes", no_observed_agents)
    monkeypatch.setattr(app_env.main, "_tmux_pane_rows", tmux_pane_rows)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    monkeypatch.setattr(app_env.main, "_tmux_resolve_pane_id", resolve_pane_id)
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status, last_activity, tmux_pane)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "inst-work-state-noncanonical",
            "session-work-state-noncanonical",
            "Codex Pax",
            "/Volumes/Imperium/Pax-ENV",
            "local",
            app_env.main.LOCAL_DEVICE_NAME,
            "idle",
            datetime.now().isoformat(),
            "main:codex.1",
        ),
    )
    conn.commit()
    conn.close()

    client = TestClient(app_env.main.app)
    resp = client.get("/api/work-state")

    assert resp.status_code == 200
    body = resp.json()
    assert body["productivity_active"] is True
    assert body["active_instance_count"] == 1
    assert body["active_instances"][0]["tmux_pane"] == "main:codex.1"


def test_work_state_ignores_idle_tracked_instance_without_agent_process(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    async def no_observed_agents():
        return []

    async def tmux_pane_rows():
        return [("%42", "bash", "/Volumes/Imperium/Pax-ENV", "Codex Pax", "")]

    async def pane_exists(pane_id):
        return pane_id == "%42"

    monkeypatch.setattr(app_env.main, "_detect_tmux_agent_panes", no_observed_agents)
    monkeypatch.setattr(app_env.main, "_tmux_pane_rows", tmux_pane_rows)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status, last_activity, tmux_pane)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "inst-stale-work-state",
            "session-stale-work-state",
            "Codex Pax",
            "/Volumes/Imperium/Pax-ENV",
            "local",
            app_env.main.LOCAL_DEVICE_NAME,
            "idle",
            datetime.now().isoformat(),
            "%42",
        ),
    )
    conn.commit()
    conn.close()

    client = TestClient(app_env.main.app)
    resp = client.get("/api/work-state")

    assert resp.status_code == 200
    body = resp.json()
    assert body["productivity_active"] is False
    assert body["active_instance_count"] == 0
    assert body["reason"] == "no_live_agent"


def test_state_validate_app_assertion_uses_http_status(app_env):
    from fastapi.testclient import TestClient

    app_env.main.PHONE_STATE.update(
        {"current_app": None, "is_distracted": False, "last_activity": datetime.now().isoformat()}
    )
    client = TestClient(app_env.main.app)

    resp = client.post("/api/state/validate", json={"app": "youtube", "assert": "false"})
    assert resp.status_code == 200
    assert resp.json()["match"] is True
    assert resp.json()["observed"] is False

    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "is_distracted": True,
            "last_activity": datetime.now().isoformat(),
        }
    )
    resp = client.post("/api/state/validate", json={"app": "youtube", "assert": "false"})
    assert resp.status_code == 409
    assert resp.json()["match"] is False
    assert resp.json()["observed"] is True

    resp = client.post("/api/state/validate", json={"app": "youtube", "assert": "true"})
    assert resp.status_code == 200
    assert resp.json()["match"] is True


def test_state_validate_named_state_key(app_env):
    from fastapi.testclient import TestClient

    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "is_distracted": True,
            "last_activity": datetime.now().isoformat(),
        }
    )
    client = TestClient(app_env.main.app)

    resp = client.post(
        "/api/state/validate",
        json={"state": "phone.current_app", "assert": "youtube"},
    )
    assert resp.status_code == 200
    assert resp.json()["observed"] == "youtube"

    resp = client.post(
        "/api/state/validate",
        json={"state": "phone.is_distracted", "assert": "false"},
    )
    assert resp.status_code == 409
    assert resp.json()["expected"] is False
    assert resp.json()["observed"] is True


def test_state_validate_accepts_query_params(app_env):
    from fastapi.testclient import TestClient

    app_env.main.PHONE_STATE.update({"current_app": None, "is_distracted": False})
    client = TestClient(app_env.main.app)

    resp = client.get("/api/state/validate?app=youtube&assert=false")
    assert resp.status_code == 200
    assert resp.json()["match"] is True

    resp = client.post("/api/state/validate?app=youtube&assert=true")
    assert resp.status_code == 409
    assert resp.json()["match"] is False


@pytest.mark.asyncio
async def test_backlog_violation_not_recreated_for_same_open_phone_app(app_env):
    active_since = (datetime.now() - timedelta(minutes=2)).isoformat()
    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"
    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "app_opened_at": active_since,
            "is_distracted": True,
        }
    )
    app_env.main.timer_engine._break_balance_ms = -30_000
    ack = app_env.main._expected_ack_deadlines(now=datetime.now() - timedelta(seconds=20))
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """
        INSERT INTO expected_acknowledgements (
            id, source, instance_id, reason, status, created_at,
            ack_due_at, level2_due_at, pavlok_due_at, fired_levels_json, details_json
        ) VALUES (?, ?, ?, ?, 'expired', ?, ?, ?, ?, ?, ?)
        """,
        (
            "backlog-expired-same-open",
            "backlog_violation",
            "backlog:phone:youtube",
            "Backlog distraction: YouTube",
            ack["created_at"].isoformat(),
            ack["ack_due_at"].isoformat(),
            ack["level2_due_at"].isoformat(),
            ack["pavlok_due_at"].isoformat(),
            "[1, 2, 3]",
            json.dumps({"active_since": active_since}),
        ),
    )
    conn.commit()
    conn.close()

    created = await app_env.main.maybe_create_backlog_violation_ack(
        surface="phone",
        app_name="youtube",
        display_name="YouTube",
        trigger="unit",
    )

    assert created is None
    pending = _rows(
        app_env.db_path,
        """
        SELECT id FROM expected_acknowledgements
        WHERE source = 'backlog_violation' AND instance_id = 'backlog:phone:youtube'
          AND status = 'pending'
        """,
    )
    assert pending == []
    event = _rows(
        app_env.db_path,
        "SELECT event_type, details FROM events WHERE event_type = 'backlog_violation_ack_suppressed'",
    )[0]
    assert event["event_type"] == "backlog_violation_ack_suppressed"


def test_phone_close_acknowledges_pending_phone_ack(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"
    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "app_opened_at": (datetime.now() - timedelta(minutes=2)).isoformat(),
            "is_distracted": True,
        }
    )
    ack = app_env.main._expected_ack_deadlines()
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """
        INSERT INTO expected_acknowledgements (
            id, source, instance_id, reason, status, created_at,
            ack_due_at, level2_due_at, pavlok_due_at, details_json
        ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """,
        (
            "phone-ack-1",
            "phone_distraction",
            "phone_distraction:phone:youtube",
            "Phone distraction during work: YouTube",
            ack["created_at"].isoformat(),
            ack["ack_due_at"].isoformat(),
            ack["level2_due_at"].isoformat(),
            ack["pavlok_due_at"].isoformat(),
            "{}",
        ),
    )
    conn.commit()
    conn.close()

    client = TestClient(app_env.main.app)
    resp = client.post("/phone", json={"app": "youtube", "action": "close"})

    assert resp.status_code == 200
    row = _rows(
        app_env.db_path,
        "SELECT status, acknowledged_at FROM expected_acknowledgements WHERE id = 'phone-ack-1'",
    )[0]
    assert row["status"] == "acknowledged"
    assert row["acknowledged_at"]


def test_mewgenics_turn_in_work_mode_creates_ack(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/games/turn",
        json={
            "game": "mewgenics",
            "steam_app_id": "686060",
            "steam_app_name": "Mewgenics",
            "steam_exe": "Mewgenics.exe",
            "source": "ahk",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["reason"] == "ack_required"
    assert resp.json()["ack_id"]
    rows = _rows(app_env.db_path, "SELECT source, reason FROM expected_acknowledgements")
    assert rows[0]["source"] == "desktop_gaming"
    assert rows[0]["reason"] == "Mewgenics turn ended during work"
