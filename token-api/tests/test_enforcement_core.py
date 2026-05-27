import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


def _rows(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


def _insert_gt_instance(db_path, instance_id="gt-dispatch", *, tmux_pane="%10"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status,
            instance_type, engine, tmux_pane, zealotry)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            instance_id,
            "GT Dispatch",
            "/tmp",
            "local",
            "Mac-Mini",
            "idle",
            "golden_throne",
            "codex",
            tmux_pane,
            10,
        ),
    )
    conn.commit()
    conn.close()


def test_expected_ack_deadlines_use_compressed_ladder_defaults(app_env):
    now = datetime(2026, 5, 3, 12, 0, 0)

    deadlines = app_env.main._expected_ack_deadlines(now=now)

    assert deadlines["ack_due_at"] == now + timedelta(seconds=90)
    assert deadlines["level2_due_at"] == now + timedelta(seconds=180)
    assert deadlines["pavlok_due_at"] == now + timedelta(seconds=180)


def test_stop_evaluator_parse_suppresses_no_content_noise(app_env):
    noisy_block = (
        "VERDICT: BLOCK I cannot analyze the message because no transcript "
        "content was provided - I need the actual agent message to evaluate"
    )

    assert app_env.main._parse_evaluator_result("action_validator", noisy_block) == (
        False,
        "",
        False,
    )

    noisy_unstructured = (
        "No transcript or final message was provided for analysis - the context appears incomplete"
    )

    assert app_env.main._parse_evaluator_result(
        "action_validator",
        noisy_unstructured,
    ) == (False, "", False)


def test_stop_evaluator_parse_allows_real_block(app_env):
    text = "VERDICT: BLOCK The agent should run the tests itself instead of asking the user"

    should_nudge, finding, needs_jury = app_env.main._parse_evaluator_result(
        "action_validator",
        text,
    )

    assert should_nudge is True
    assert needs_jury is False
    assert "run the tests itself" in finding


def test_stop_evaluator_parse_suppresses_placeholder_plan_noise(app_env):
    text = (
        'VERDICT: BLOCK The Plan section currently shows "No plan defined yet." '
        "but significant activity has occurred with defined remaining steps. "
        "The Plan should be updated to document the milestones"
    )

    assert app_env.main._parse_evaluator_result("plan_auditor", text) == (
        False,
        "",
        False,
    )


def test_stop_evaluator_parse_allows_real_plan_auditor_block(app_env):
    text = (
        "VERDICT: BLOCK The Plan section currently shows migration complete "
        "but tests failed after the latest run. The Plan should be updated to "
        "include fixing the failing migration tests"
    )

    should_nudge, finding, needs_jury = app_env.main._parse_evaluator_result(
        "plan_auditor",
        text,
    )

    assert should_nudge is True
    assert needs_jury is False
    assert "tests failed" in finding


def test_enforcement_state_payload_keeps_internal_ack_names_out_of_app_slots(app_env):
    for source, internal_name in (
        ("askq_ladder", "askuserquestion-019e1274"),
        ("golden_throne", "golden_throne-019e1274"),
    ):
        payload = app_env.main._enforcement_state_payload(source=source, app=internal_name)

        assert "app" not in payload
        assert payload["phone_app"] is None
        assert payload["ack_source"] == internal_name

    phone_payload = app_env.main._enforcement_state_payload(source="phone", app="slay_the_spire")
    assert phone_payload["app"] == "slay_the_spire"
    assert phone_payload["phone_app"] == "slay_the_spire"
    assert "ack_source" not in phone_payload


def test_golden_throne_transport_uses_instance_engine(app_env):
    assert app_env.main._agent_engine({"engine": "codex"}) == "codex"
    assert app_env.main._agent_engine({"launcher": "codex-dispatch"}) == "codex"
    assert app_env.main._agent_is_alive_command("codex", "codex") is True
    assert app_env.main._agent_is_alive_command("codex", "claude") is False

    cmd = app_env.main._agent_resume_command(
        "codex",
        "session-1",
        "/Volumes/Imperium/Imperium-ENV",
        "/tmp/sop.md",
    )
    assert cmd.startswith("cd /Volumes/Imperium/Imperium-ENV && ")
    assert "codex-dispatch" in cmd
    assert "--resume-session session-1" in cmd
    assert "session-1" in cmd


@pytest.mark.asyncio
async def test_golden_throne_does_not_create_ack_when_dispatch_fails(app_env, monkeypatch):
    _insert_gt_instance(app_env.db_path, "gt-dispatch-fail")
    calls = []

    async def no_label(pane):
        return None

    async def pane_exists(pane):
        return True

    async def fake_subprocess_exec(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("tmux", "display-message"):
            return _FakeProc(0, b"codex\n", b"")
        raise AssertionError(f"unexpected raw tmux send: {args}")

    async def fake_tmux_send_payload_then_submit(pane, payload, **kwargs):
        calls.append(("tmuxctl", "send-text-then-submit", pane, payload, kwargs))
        return {
            "returncode": 7,
            "stdout": "attempted",
            "stderr": "send failed",
            "operation": "tmuxctl.send_text_then_submit",
        }

    monkeypatch.setattr(app_env.main, "_load_golden_throne_sop", lambda: "resume work")
    monkeypatch.setattr(app_env.main, "_tmux_pane_label", no_label)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    monkeypatch.setattr(
        app_env.main, "_tmux_send_payload_then_submit", fake_tmux_send_payload_then_submit
    )
    monkeypatch.setattr(app_env.main.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await app_env.main.golden_throne_followup("gt-dispatch-fail")

    assert calls == [
        ("tmux", "display-message", "-t", "%10", "-p", "#{pane_current_command}"),
        (
            "tmuxctl",
            "send-text-then-submit",
            "%10",
            "resume work",
            {},
        ),
    ]
    assert _rows(app_env.db_path, "SELECT * FROM expected_acknowledgements") == []
    queue_rows = _rows(
        app_env.db_path,
        "SELECT status, last_error FROM pane_write_queue WHERE instance_id = ?",
        ("gt-dispatch-fail",),
    )
    assert queue_rows[0]["status"] == "failed"
    assert queue_rows[0]["last_error"] == "send failed"
    instance_row = _rows(
        app_env.db_path,
        "SELECT gt_resume_count FROM claude_instances WHERE id = ?",
        ("gt-dispatch-fail",),
    )[0]
    assert instance_row["gt_resume_count"] == 0
    events = _rows(
        app_env.db_path,
        "SELECT event_type, details FROM events ORDER BY id",
    )
    assert [row["event_type"] for row in events] == ["golden_throne_dispatch_failed"]
    details = json.loads(events[0]["details"])
    assert details["returncode"] == 7
    assert details["stderr"] == "send failed"


@pytest.mark.asyncio
async def test_golden_throne_validated_dispatch_counts_without_ack(app_env, monkeypatch):
    _insert_gt_instance(app_env.db_path, "gt-dispatch-ok")

    async def pane_label(pane):
        return "palace:NE"

    async def pane_exists(pane):
        return True

    async def fake_subprocess_exec(*args, **kwargs):
        if args[:2] == ("tmux", "display-message"):
            return _FakeProc(0, b"codex\n", b"")
        raise AssertionError(f"unexpected raw tmux send: {args}")

    async def fake_tmux_send_payload_then_submit(pane, payload, **kwargs):
        assert pane == "%10"
        assert payload == "resume work"
        return {
            "returncode": 0,
            "stdout": "injected\ninjected",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
        }

    monkeypatch.setattr(app_env.main, "_load_golden_throne_sop", lambda: "resume work")
    monkeypatch.setattr(app_env.main, "_tmux_pane_label", pane_label)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    monkeypatch.setattr(
        app_env.main,
        "_send_to_phone",
        lambda *args, **kwargs: {"success": True, "status_code": 200},
    )
    monkeypatch.setattr(
        app_env.main, "_tmux_send_payload_then_submit", fake_tmux_send_payload_then_submit
    )
    monkeypatch.setattr(app_env.main.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await app_env.main.golden_throne_followup("gt-dispatch-ok")

    assert (
        _rows(
            app_env.db_path,
            "SELECT * FROM expected_acknowledgements WHERE source = 'golden_throne'",
        )
        == []
    )
    queue_rows = _rows(
        app_env.db_path,
        "SELECT status, last_result_json FROM pane_write_queue WHERE instance_id = ?",
        ("gt-dispatch-ok",),
    )
    assert queue_rows[0]["status"] == "sent"
    queue_result = json.loads(queue_rows[0]["last_result_json"])
    assert queue_result["returncode"] == 0
    assert queue_result["stdout"] == "injected\ninjected"
    instance_row = _rows(
        app_env.db_path,
        "SELECT gt_resume_count FROM claude_instances WHERE id = ?",
        ("gt-dispatch-ok",),
    )[0]
    assert instance_row["gt_resume_count"] == 1

    events = _rows(app_env.db_path, "SELECT event_type FROM events ORDER BY id")
    assert [row["event_type"] for row in events][:2] == [
        "golden_throne_resume_counted",
        "golden_throne_dispatch_validated",
    ]
    assert "expected_ack_created" not in [row["event_type"] for row in events]


@pytest.mark.asyncio
async def test_golden_throne_followup_defers_dispatch_during_quiet_hours(app_env, monkeypatch):
    _insert_gt_instance(app_env.db_path, "gt-quiet-dispatch")
    scheduled = []

    async def fake_schedule(instance, reason="stop_hook"):
        scheduled.append((instance["id"], reason))
        return {"scheduled": True, "reason": reason}

    monkeypatch.setattr(app_env.main, "schedule_golden_throne_followup", fake_schedule)
    monkeypatch.setattr(
        app_env.shared,
        "get_quiet_hours_status",
        lambda now=None: {
            "active": True,
            "reason": "quiet_hours",
            "quiet_start": 23,
            "quiet_end": 9,
            "timezone": "America/Phoenix",
            "local_time": "2026-05-07T23:30:00-07:00",
        },
    )

    await app_env.main.golden_throne_followup("gt-quiet-dispatch")

    assert scheduled == [("gt-quiet-dispatch", "quiet-hours-deferred-dispatch")]
    events = _rows(app_env.db_path, "SELECT event_type, details FROM events ORDER BY id")
    assert [row["event_type"] for row in events] == [
        "golden_throne_dispatch_suppressed_quiet_hours"
    ]
    details = json.loads(events[0]["details"])
    assert details["quiet_hours"]["active"] is True
    assert details["rescheduled"]["scheduled"] is True


@pytest.mark.asyncio
async def test_pane_write_queue_submits_with_separate_literal_text_and_enter(app_env, monkeypatch):
    calls = []

    async def pane_has_input(pane):
        return False

    async def fake_send_payload_then_submit(pane, payload):
        calls.append((pane, payload))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
        }

    monkeypatch.setattr(app_env.main, "_tmux_pane_has_pending_input", pane_has_input)
    monkeypatch.setattr(
        app_env.main,
        "_tmux_send_payload_then_submit",
        fake_send_payload_then_submit,
    )
    queued = await app_env.main.enqueue_pane_write(
        instance_id="gt-enter-regression",
        tmux_pane="%10",
        source="golden_throne",
        purpose="followup",
        payload="resume work",
    )

    result = (await app_env.main.process_pane_write_queue_once(queued["id"]))[0]

    assert result["status"] == "sent"
    assert result["operation"] == "tmuxctl.send_text_then_submit"
    assert calls == [("%10", "resume work")]


@pytest.mark.asyncio
async def test_pane_write_queue_rejects_empty_target(app_env):
    with pytest.raises(ValueError, match="concrete tmux pane"):
        await app_env.main.enqueue_pane_write(
            instance_id="gt-empty-pane",
            tmux_pane="",
            source="golden_throne",
            purpose="followup",
            payload="resume work",
        )


def test_golden_throne_human_surface_includes_page_number(app_env):
    assert (
        app_env.main._golden_throne_human_surface("ignored", "%10", "palace:NW") == "1:NW ignored"
    )
    assert (
        app_env.main._golden_throne_human_surface("ignored", "%11", "somnium:SE") == "2:SE ignored"
    )


@pytest.mark.asyncio
async def test_golden_throne_detects_codex_below_bash_and_does_not_resume(app_env, monkeypatch):
    _insert_gt_instance(app_env.db_path, "gt-bash-codex", tmux_pane="%134")
    calls = []

    async def pane_label(pane):
        return "palace:NE"

    async def pane_exists(pane):
        return True

    async def has_agent_process(pane, engine):
        assert pane == "%134"
        assert engine == "codex"
        return True

    async def fake_subprocess_exec(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("tmux", "display-message"):
            return _FakeProc(0, b"bash\n", b"")
        raise AssertionError(f"unexpected subprocess: {args}")

    async def fake_tmux_send_payload_then_submit(pane, payload, **kwargs):
        calls.append(("tmuxctl", "send-text-then-submit", pane, payload, kwargs))
        assert pane == "%134"
        assert payload == "resume work"
        return {
            "returncode": 0,
            "stdout": "injected",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
        }

    monkeypatch.setattr(app_env.main, "_load_golden_throne_sop", lambda: "resume work")
    monkeypatch.setattr(app_env.main, "_tmux_pane_label", pane_label)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    monkeypatch.setattr(app_env.main, "_tmux_pane_has_agent_process", has_agent_process)
    monkeypatch.setattr(app_env.main, "_send_to_phone", lambda *args, **kwargs: {"success": True})
    monkeypatch.setattr(
        app_env.main, "_tmux_send_payload_then_submit", fake_tmux_send_payload_then_submit
    )
    monkeypatch.setattr(app_env.main.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await app_env.main.golden_throne_followup("gt-bash-codex")

    queue_row = _rows(
        app_env.db_path,
        "SELECT tmux_pane, payload, status FROM pane_write_queue WHERE instance_id = ?",
        ("gt-bash-codex",),
    )[0]
    assert queue_row["tmux_pane"] == "%134"
    assert queue_row["payload"] == "resume work"
    assert queue_row["status"] == "sent"
    events = _rows(app_env.db_path, "SELECT event_type, details FROM events ORDER BY id")
    validated = [
        json.loads(row["details"])
        for row in events
        if row["event_type"] == "golden_throne_dispatch_validated"
    ][0]
    assert validated["agent_alive"] is True
    assert validated["transport"] == "send-keys"


@pytest.mark.asyncio
async def test_golden_throne_empty_legion_pane_fails_closed(app_env, monkeypatch):
    _insert_gt_instance(app_env.db_path, "gt-empty-legion", tmux_pane="%134")
    calls = []

    async def pane_label(pane):
        return "palace:NE"

    async def pane_exists(pane):
        return pane == "%134"

    async def no_agent_process(pane, engine):
        return False

    async def empty_legion():
        return ""

    async def fake_subprocess_exec(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("tmux", "display-message"):
            return _FakeProc(0, b"bash\n", b"")
        if args[:2] == ("tmux", "send-keys"):
            raise AssertionError("empty legion target must not be sent")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr(app_env.main, "_load_golden_throne_sop", lambda: "resume work")
    monkeypatch.setattr(app_env.main, "_tmux_pane_label", pane_label)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    monkeypatch.setattr(app_env.main, "_tmux_pane_has_agent_process", no_agent_process)
    monkeypatch.setattr(app_env.main, "_get_or_create_legion_pane", empty_legion)
    monkeypatch.setattr(app_env.main.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await app_env.main.golden_throne_followup("gt-empty-legion")

    assert (
        _rows(
            app_env.db_path,
            "SELECT * FROM pane_write_queue WHERE instance_id = ?",
            ("gt-empty-legion",),
        )
        == []
    )
    instance_row = _rows(
        app_env.db_path,
        "SELECT gt_resume_count FROM claude_instances WHERE id = ?",
        ("gt-empty-legion",),
    )[0]
    assert instance_row["gt_resume_count"] == 0
    events = _rows(app_env.db_path, "SELECT event_type, details FROM events ORDER BY id")
    assert [row["event_type"] for row in events] == ["golden_throne_dispatch_failed"]
    details = json.loads(events[0]["details"])
    assert details["transport"] == "resume"
    assert "concrete tmux pane" in details["error"]


@pytest.mark.asyncio
async def test_golden_throne_typing_block_defers_without_counting(app_env, monkeypatch):
    _insert_gt_instance(app_env.db_path, "gt-dispatch-defer")

    async def pane_label(pane):
        return "palace:NE"

    async def pane_exists(pane):
        return True

    async def pane_has_input(pane):
        return True

    async def fake_subprocess_exec(*args, **kwargs):
        if args[:2] == ("tmux", "display-message"):
            return _FakeProc(0, b"codex\n", b"")
        raise AssertionError(f"unexpected send while pane has user input: {args}")

    monkeypatch.setattr(app_env.main, "_load_golden_throne_sop", lambda: "resume work")
    monkeypatch.setattr(app_env.main, "_tmux_pane_label", pane_label)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    monkeypatch.setattr(app_env.main, "_tmux_pane_has_pending_input", pane_has_input)
    monkeypatch.setattr(app_env.main.asyncio, "create_subprocess_exec", fake_subprocess_exec)

    await app_env.main.golden_throne_followup("gt-dispatch-defer")

    assert (
        _rows(
            app_env.db_path,
            "SELECT * FROM expected_acknowledgements WHERE source = 'golden_throne'",
        )
        == []
    )
    queue_row = _rows(
        app_env.db_path,
        "SELECT status, last_error, last_result_json FROM pane_write_queue WHERE instance_id = ?",
        ("gt-dispatch-defer",),
    )[0]
    assert queue_row["status"] == "cancelled"
    assert queue_row["last_error"] == "dispatch_deferred_rescheduled"
    result = json.loads(queue_row["last_result_json"])
    assert result["reason"] == "dispatch_deferred_rescheduled"
    instance_row = _rows(
        app_env.db_path,
        "SELECT gt_resume_count FROM claude_instances WHERE id = ?",
        ("gt-dispatch-defer",),
    )[0]
    assert instance_row["gt_resume_count"] == 0
    events = _rows(app_env.db_path, "SELECT event_type FROM events ORDER BY id")
    assert [row["event_type"] for row in events] == [
        "golden_throne_scheduled",
        "golden_throne_dispatch_deferred",
    ]


@pytest.mark.asyncio
async def test_golden_throne_prompt_submit_cancels_pending_pane_writes(app_env):
    _insert_gt_instance(app_env.db_path, "gt-prompt-seen")
    queued = await app_env.main.enqueue_pane_write(
        instance_id="gt-prompt-seen",
        tmux_pane="%10",
        source="golden_throne",
        purpose="followup",
        payload="resume work",
    )

    result = await app_env.main.golden_throne_user_activity(
        "gt-prompt-seen",
        source="prompt_submit",
    )

    assert result["cancelled_pane_writes"] == 1
    row = _rows(
        app_env.db_path,
        "SELECT status, cancelled_at FROM pane_write_queue WHERE id = ?",
        (queued["id"],),
    )[0]
    assert row["status"] == "cancelled"
    assert row["cancelled_at"]


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
    assert ack_due_at - created_at == timedelta(seconds=90)
    assert level2_due_at - created_at == timedelta(seconds=180)
    assert pavlok_due_at - created_at == timedelta(seconds=180)

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
async def test_golden_throne_user_activity_cancels_queue_and_resets_resume_count(app_env):
    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status,
            instance_type, engine, gt_resume_count, gt_resume_window_started_at, gt_last_resume_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "gt-active",
            "gt-active",
            "GT Active",
            "/tmp",
            "local",
            "Mac-Mini",
            "idle",
            "golden_throne",
            "codex",
            1,
            now.isoformat(),
            now.isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    await app_env.main.enqueue_pane_write(
        instance_id="gt-active",
        tmux_pane="%10",
        source="golden_throne",
        purpose="followup",
        payload="resume work",
    )

    result = await app_env.main.golden_throne_user_activity("gt-active", source="prompt_submit")

    assert result["cancelled_pane_writes"] == 1
    row = _rows(
        app_env.db_path,
        """
        SELECT gt_resume_count, gt_resume_window_started_at, status
        FROM claude_instances WHERE id = ?
        """,
        ("gt-active",),
    )[0]
    queue_row = _rows(
        app_env.db_path,
        "SELECT status, cancelled_at FROM pane_write_queue WHERE instance_id = ?",
        ("gt-active",),
    )[0]
    assert row["gt_resume_count"] == 0
    assert row["gt_resume_window_started_at"] is None
    assert row["status"] == "idle"
    assert queue_row["status"] == "cancelled"
    assert queue_row["cancelled_at"]


@pytest.mark.asyncio
async def test_golden_throne_schedule_shifts_fire_at_past_quiet_hours(app_env, monkeypatch):
    class FakeScheduler:
        def __init__(self):
            self.jobs = []

        def add_job(self, func, trigger, **kwargs):
            self.jobs.append({"func": func, "trigger": trigger, "kwargs": kwargs})

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(app_env.main, "scheduler", fake_scheduler)
    monkeypatch.setattr(
        app_env.shared,
        "get_quiet_hours_status",
        lambda now=None: {
            "active": True,
            "reason": "quiet_hours",
            "quiet_start": 23,
            "quiet_end": 9,
            "timezone": "America/Phoenix",
            "local_time": "2026-05-07T23:30:00-07:00",
        },
    )

    result = await app_env.main.schedule_golden_throne_followup(
        {
            "id": "gt-quiet",
            "instance_type": "golden_throne",
            "zealotry": 10,
            "engine": "codex",
        },
        reason="unit-test",
    )

    expected_fire_at = datetime.fromisoformat("2026-05-08T09:05:00-07:00")
    assert result["scheduled"] is True
    assert result["quiet_hours_shifted"] is True
    assert datetime.fromisoformat(result["fire_at"]) == expected_fire_at
    assert fake_scheduler.jobs[0]["trigger"].run_date == expected_fire_at


@pytest.mark.asyncio
async def test_golden_throne_startup_recovery_restores_recent_quiet_rows(app_env, monkeypatch):
    scheduled = []

    async def fake_schedule(instance, reason="stop_hook"):
        scheduled.append((instance["id"], reason))
        return {"scheduled": True, "reason": reason}

    monkeypatch.setattr(app_env.main, "schedule_golden_throne_followup", fake_schedule)
    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
    conn.executemany(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status,
            instance_type, engine, zealotry, stopped_at, last_activity, gt_last_resume_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "gt-recent",
                "gt-recent",
                "GT Recent",
                "/tmp",
                "local",
                "Mac-Mini",
                "stopped",
                "golden_throne",
                "codex",
                10,
                (now - timedelta(minutes=5)).isoformat(),
                (now - timedelta(minutes=5)).isoformat(),
                None,
            ),
            (
                "gt-idle",
                "gt-idle",
                "GT Idle",
                "/tmp",
                "local",
                "Mac-Mini",
                "idle",
                "golden_throne",
                "codex",
                5,
                None,
                (now - timedelta(minutes=3)).isoformat(),
                None,
            ),
            (
                "gt-stale",
                "gt-stale",
                "GT Stale",
                "/tmp",
                "local",
                "Mac-Mini",
                "stopped",
                "golden_throne",
                "codex",
                10,
                (now - timedelta(minutes=45)).isoformat(),
                (now - timedelta(minutes=45)).isoformat(),
                None,
            ),
        ],
    )
    conn.commit()
    conn.close()

    recovered = await app_env.main.recover_recent_stopped_golden_throne_timers(
        lookback_minutes=30,
    )

    assert scheduled == [
        ("gt-idle", "startup-recover-quiet"),
        ("gt-recent", "startup-recover-quiet"),
    ]
    assert [item["instance_id"] for item in recovered] == ["gt-idle", "gt-recent"]


@pytest.mark.asyncio
async def test_golden_throne_startup_recovery_skips_stopped_shell_pane(app_env, monkeypatch):
    scheduled = []

    async def fake_schedule(instance, reason="stop_hook"):
        scheduled.append((instance["id"], reason))
        return {"scheduled": True, "reason": reason}

    async def pane_exists(pane):
        return True

    async def current_command(pane):
        return "bash"

    async def no_agent_process(pane, engine):
        return False

    monkeypatch.setattr(app_env.main, "schedule_golden_throne_followup", fake_schedule)
    monkeypatch.setattr(app_env.main, "_tmux_pane_exists", pane_exists)
    monkeypatch.setattr(app_env.main, "_tmux_pane_current_command", current_command)
    monkeypatch.setattr(app_env.main, "_tmux_pane_has_agent_process", no_agent_process)

    now = datetime.now()
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id, status,
            instance_type, engine, zealotry, tmux_pane, stopped_at, last_activity)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "gt-stopped-shell",
            "gt-stopped-shell",
            "GT Stopped Shell",
            "/tmp",
            "local",
            "Mac-Mini",
            "stopped",
            "golden_throne",
            "codex",
            10,
            "%132",
            (now - timedelta(minutes=5)).isoformat(),
            (now - timedelta(minutes=5)).isoformat(),
        ),
    )
    conn.commit()
    conn.close()

    recovered = await app_env.main.recover_recent_stopped_golden_throne_timers(
        lookback_minutes=30,
    )

    assert scheduled == []
    assert recovered == []
    events = _rows(app_env.db_path, "SELECT event_type, details FROM events ORDER BY id")
    assert [row["event_type"] for row in events] == ["golden_throne_recovery_skipped_stale_pane"]
    details = json.loads(events[0]["details"])
    assert details["reason"] == "stale_reused_or_empty_pane"
    assert details["tmux_pane"] == "%132"


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


def test_phone_slay_the_spire_productivity_contributes_composite_timer_state(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    async def _active_work_state():
        return SimpleNamespace(
            productivity_active=True,
            active_instance_count=1,
            observed_agent_count=0,
        )

    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"
    app_env.main.timer_engine.set_productivity(True, 1_000)
    app_env.main.timer_engine.set_activity(
        app_env.main.Activity.WORKING,
        is_scrolling_gaming=False,
        now_mono_ms=1_000,
    )
    monkeypatch.setattr(app_env.main, "compute_work_state", _active_work_state)

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/phone",
        json={"app": "slay the spire", "action": "open", "package": "com.humble.slaythespire"},
    )

    assert resp.status_code == 200
    assert resp.json()["allowed"] is True
    assert resp.json()["reason"] == "productivity_active"
    assert app_env.main.timer_engine.current_mode.value == "multitasking"
    phone_app_shifts = _rows(
        app_env.db_path, "SELECT * FROM timer_shifts WHERE trigger = 'phone_app'"
    )
    assert phone_app_shifts == []
    composite_shifts = _rows(
        app_env.db_path,
        "SELECT old_mode, new_mode, trigger, source, phone_app FROM timer_shifts",
    )
    assert len(composite_shifts) == 1
    assert composite_shifts[0]["old_mode"] == "working"
    assert composite_shifts[0]["new_mode"] == "multitasking"
    assert composite_shifts[0]["trigger"] == "phone_distraction"
    assert composite_shifts[0]["source"] == "macrodroid"
    assert composite_shifts[0]["phone_app"] == "slay the spire"
    observed = _rows(
        app_env.db_path,
        "SELECT details FROM events WHERE event_type = 'phone_distraction_observed'",
    )
    assert len(observed) == 1
    details = json.loads(observed[0]["details"])
    assert details["app"] == "slay the spire"
    assert details["distraction_mode"] == "gaming"
    assert details["old_timer_mode"] == "working"
    assert details["timer_mode"] == "multitasking"
    assert details["productivity_active"] is True
    assert details["timer_updated"] is True
    assert details["count"] == 1


def test_work_action_preserves_phone_and_desktop_distraction_sources(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["current_mode"] = "gaming"
    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "app_opened_at": datetime.now().isoformat(),
            "is_distracted": True,
            "last_activity": datetime.now().isoformat(),
        }
    )
    now_ms = int(app_env.main.time.monotonic() * 1000)
    app_env.main.timer_engine.set_productivity(True, now_ms)
    app_env.main.timer_engine.set_activity(
        app_env.main.Activity.DISTRACTION,
        is_scrolling_gaming=True,
        now_mono_ms=now_ms,
    )

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/api/work-action",
        json={"source": "unit_test", "note": "work while phone video continues"},
    )

    assert resp.status_code == 200
    assert app_env.main.PHONE_STATE["current_app"] == "youtube"
    assert app_env.main.PHONE_STATE["is_distracted"] is True
    assert app_env.main.DESKTOP_STATE["current_mode"] == "gaming"
    assert app_env.main.timer_engine.activity == app_env.main.Activity.DISTRACTION
    assert app_env.main.timer_engine.current_mode == app_env.main.TimerMode.MULTITASKING


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
    assert app_env.main.PHONE_STATE["current_app"] == "youtube"
    assert app_env.main.PHONE_STATE["is_distracted"] is True


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
    monkeypatch.setattr(app_env.main, "_resolve_tmux_pane_id_for_read_model", resolve_pane_id)
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
    assert body["reason"] == "no_recent_work_activity"


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



def test_desktop_clear_preserves_active_phone_distraction(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["current_mode"] = "video"
    app_env.main.DESKTOP_STATE["work_mode"] = "clocked_in"
    app_env.main.DESKTOP_STATE["startup_grace_secs"] = 0
    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "app_opened_at": datetime.now().isoformat(),
            "is_distracted": True,
            "last_activity": datetime.now().isoformat(),
        }
    )
    now_ms = int(app_env.main.time.monotonic() * 1000)
    app_env.main.timer_engine.set_productivity(True, now_ms)
    app_env.main.timer_engine.set_activity(
        app_env.main.Activity.DISTRACTION,
        is_scrolling_gaming=False,
        now_mono_ms=now_ms,
    )

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/desktop",
        json={"detected_mode": "silence", "window_title": "Desktop", "source": "pytest"},
    )

    assert resp.status_code == 200, resp.text
    assert app_env.main.DESKTOP_STATE["current_mode"] == "silence"
    assert app_env.main.PHONE_STATE["current_app"] == "youtube"
    assert app_env.main.PHONE_STATE["is_distracted"] is True
    assert app_env.main.timer_engine.activity == app_env.main.Activity.DISTRACTION
    assert app_env.main.timer_engine.current_mode == app_env.main.TimerMode.MULTITASKING


def test_phone_close_recomputes_activity_without_erasing_desktop(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["current_mode"] = "video"
    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "app_opened_at": datetime.now().isoformat(),
            "is_distracted": True,
            "last_activity": datetime.now().isoformat(),
        }
    )
    now_ms = int(app_env.main.time.monotonic() * 1000)
    app_env.main.timer_engine.set_productivity(True, now_ms)
    app_env.main.timer_engine.set_activity(
        app_env.main.Activity.DISTRACTION,
        is_scrolling_gaming=False,
        now_mono_ms=now_ms,
    )

    client = TestClient(app_env.main.app)
    resp = client.post("/phone", json={"app": "youtube", "action": "close"})

    assert resp.status_code == 200, resp.text
    assert app_env.main.PHONE_STATE["current_app"] is None
    assert app_env.main.PHONE_STATE["is_distracted"] is False
    assert app_env.main.DESKTOP_STATE["current_mode"] == "video"
    assert app_env.main.timer_engine.activity == app_env.main.Activity.DISTRACTION
    assert app_env.main.timer_engine.current_mode == app_env.main.TimerMode.MULTITASKING


def test_phone_close_returns_to_working_when_no_attention_sources_remain(app_env):
    from fastapi.testclient import TestClient

    app_env.main.DESKTOP_STATE["current_mode"] = "silence"
    app_env.main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "app_opened_at": datetime.now().isoformat(),
            "is_distracted": True,
            "last_activity": datetime.now().isoformat(),
        }
    )
    now_ms = int(app_env.main.time.monotonic() * 1000)
    app_env.main.timer_engine.set_productivity(True, now_ms)
    app_env.main.timer_engine.set_activity(
        app_env.main.Activity.DISTRACTION,
        is_scrolling_gaming=False,
        now_mono_ms=now_ms,
    )

    client = TestClient(app_env.main.app)
    resp = client.post("/phone", json={"app": "youtube", "action": "close"})

    assert resp.status_code == 200, resp.text
    assert app_env.main.PHONE_STATE["current_app"] is None
    assert app_env.main.PHONE_STATE["is_distracted"] is False
    assert app_env.main.timer_engine.activity == app_env.main.Activity.WORKING
    assert app_env.main.timer_engine.current_mode == app_env.main.TimerMode.WORKING

def test_mewgenics_turn_legacy_endpoint_does_not_create_ack(app_env):
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
    assert resp.json()["reason"] == "observational_only"
    assert resp.json()["ack_id"] is None
    rows = _rows(app_env.db_path, "SELECT source, reason FROM expected_acknowledgements")
    assert rows == []


def test_mewgenics_space_break_mode_logs_without_zap(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    calls = []
    monkeypatch.setattr(
        app_env.main,
        "send_pavlok_stimulus",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True},
    )
    monkeypatch.setattr(app_env.main, "is_quiet_hours", lambda *args, **kwargs: False)
    app_env.main.timer_engine.enter_break(0)

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/api/telemetry/mewgenics-space",
        json={"event": "mewgenics_space", "source": "ahk", "ts": "20260503135500"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"recorded": True, "reason": "break_mode", "zap_fired": False}
    assert calls == []
    rows = _rows(app_env.db_path, "SELECT event_type, details FROM events ORDER BY id DESC LIMIT 1")
    assert rows[0]["event_type"] == "mewgenics_space"
    assert json.loads(rows[0]["details"])["timer_mode"] == "break"


def test_mewgenics_space_second_working_press_zaps_directly(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    calls = []

    def fake_send(stimulus_type, value, reason, respect_cooldown=True):
        calls.append((stimulus_type, value, reason, respect_cooldown))
        return {"success": True}

    monkeypatch.setattr(app_env.main, "send_pavlok_stimulus", fake_send)
    monkeypatch.setattr(app_env.main, "is_quiet_hours", lambda *args, **kwargs: False)
    client = TestClient(app_env.main.app)

    first = client.post("/api/telemetry/mewgenics-space", json={"event": "mewgenics_space"})
    second = client.post("/api/telemetry/mewgenics-space", json={"event": "mewgenics_space"})

    assert first.status_code == 200
    assert first.json()["reason"] == "armed"
    assert first.json()["zap_fired"] is False
    assert second.status_code == 200
    assert second.json()["reason"] == "direct_zap"
    assert second.json()["zap_fired"] is True
    assert calls == [
        ("zap", app_env.main.PAVLOK_CONFIG.get("friday_zap_value", 30), "mewgenics_space", True)
    ]
    rows = _rows(app_env.db_path, "SELECT source FROM expected_acknowledgements")
    assert rows == []


def test_mewgenics_space_work_action_interleave_prevents_second_press_zap(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    calls = []
    monkeypatch.setattr(
        app_env.main,
        "send_pavlok_stimulus",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"success": True},
    )
    monkeypatch.setattr(app_env.main, "is_quiet_hours", lambda *args, **kwargs: False)
    client = TestClient(app_env.main.app)

    first = client.post("/api/telemetry/mewgenics-space", json={"event": "mewgenics_space"})
    work = client.post("/api/work-action", json={"source": "true", "note": "stream deck"})
    second = client.post("/api/telemetry/mewgenics-space", json={"event": "mewgenics_space"})

    assert first.status_code == 200
    assert work.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"recorded": True, "reason": "armed", "zap_fired": False}
    assert calls == []


def test_media_pause_routes_to_phone_youtube_when_telemetry_active(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    calls = []

    def fake_send_to_phone(endpoint, params):
        calls.append((endpoint, params))
        return {"success": True, "status_code": 200}

    app_env.main.PHONE_STATE["current_app"] = "youtube"
    app_env.main.PHONE_STATE["is_distracted"] = True
    app_env.main.AUDIO_PROXY_STATE["phone_connected"] = True
    monkeypatch.setattr(app_env.main, "_send_to_phone", fake_send_to_phone)

    client = TestClient(app_env.main.app)
    resp = client.post("/api/media/pause", json={"source": "pytest"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "phone_youtube"
    assert body["handled"] is True
    assert body["audio_proxy_connected"] is True
    assert calls == [("/pause", {"source": "pytest"})]


def test_media_pause_falls_back_to_tts_when_phone_youtube_inactive(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    calls = []

    def fake_tts_control(command):
        calls.append(command)
        return {"success": True, "status_code": 200}

    app_env.main.PHONE_STATE["current_app"] = None
    app_env.main.PHONE_STATE["is_distracted"] = False
    monkeypatch.setattr(app_env.main, "send_tts_transport_control", fake_tts_control)

    client = TestClient(app_env.main.app)
    resp = client.post("/api/media/pause", json={"source": "pytest"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["target"] == "tts"
    assert body["handled"] is True
    assert calls == ["toggle"]
