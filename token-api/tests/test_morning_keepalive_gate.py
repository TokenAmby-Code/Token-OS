"""Tests for the morning-keepalive gate and Custodes singleton-pane Discord routing.

Part A — the Stop-hook keepalive is gated on **Custodes persona identity** + an
first-class timer mode `morning_session`, NOT on instance_type=='sync' or
the legacy state file. Identity is resolved from the canonical instances/personas
join, so a resting Custodes owns keepalive only while timer mode is morning_session:
- custodes persona + timer not morning_session → clean Stop, NO keepalive re-injection
- custodes persona + timer morning_session     → keepalive re-injected (even sans sync mode)
- residual sync MODE (non-custodes)            → no keepalive
- a non-custodes / non-sync instance             → never reaches the keepalive
- POST /api/morning/end durably writes status="ended" to the state file

Part B — Custodes Discord injection resolves the target via the `legion:custodes`
pane marker, never via a synced/live DB-row hunt, succeeding even when the DB row is
stale/one_off/synced=0 as long as the pane is alive.
"""

import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

# ── Helpers ──────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, returncode=0):
        self.returncode = returncode

    @property
    def stderr(self):
        return SimpleNamespace(decode=lambda *a, **k: "")


def _insert_claude_row(conn, sid, *, instance_type, legion, status="idle"):
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, instance_type, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, ?, ?, ?)""",
        (
            sid,
            str(uuid.uuid4()),
            f"test-{sid[:8]}",
            "/tmp",
            status,
            legion,
            instance_type,
            now,
            now,
        ),
    )


def _insert_custodes_instance(
    db_path, *, instance_type="hook_driven", rank="overseer", status="idle"
):
    """A resting Custodes: an instances row with persona=custodes, rank=overseer,
    and NO sync mode by default. The keepalive must fire on persona identity
    alone, so instance_type defaults to hook_driven.

    The legacy_instances test surface is now a compatibility view over instances;
    seed through it for the legacy mode fields, then stamp the same row with the
    instances-table persona identity instead of inserting a duplicate primary key."""
    sid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    _insert_claude_row(
        conn,
        sid,
        instance_type=instance_type,
        legion="custodes",
        status=status,
    )
    persona_id = conn.execute("SELECT id FROM personas WHERE slug = 'custodes'").fetchone()[0]
    conn.execute(
        """UPDATE instances
              SET name = ?,
                  engine = 'claude',
                  working_dir = '/tmp',
                  device_id = 'Mac-Mini',
                  origin_type = 'local',
                  commander_type = 'emperor',
                  commander_id = NULL,
                  status = ?,
                  created_at = ?,
                  last_activity = ?,
                  persona_id = ?,
                  rank = ?,
                  automated = 0,
                  notification_mode = 'verbose',
                  interaction_mode = 'text'
            WHERE id = ?""",
        (f"Custodes-{sid[:6]}", status, now, now, persona_id, rank, sid),
    )
    conn.commit()
    conn.close()
    return sid


def _insert_plain_instance(db_path, *, instance_type="sync", legion="mechanicus"):
    """A claude_instances row with NO canonical custodes identity — used to exercise
    the residual sync-MODE branch (non-custodes) and the non-custodes/non-sync case."""
    sid = str(uuid.uuid4())
    conn = sqlite3.connect(db_path)
    _insert_claude_row(conn, sid, instance_type=instance_type, legion=legion)
    conn.commit()
    conn.close()
    return sid


def _enter_timer_morning(app_env: Any) -> None:
    now_ms = int(app_env.main.time.monotonic() * 1000)
    today = datetime.now().strftime("%Y-%m-%d")
    app_env.main.timer_engine.enter_morning_session(now_ms, today)


def _exit_timer_morning(app_env: Any) -> None:
    now_ms = int(app_env.main.time.monotonic() * 1000)
    if app_env.main.timer_engine.current_mode == app_env.main.TimerMode.MORNING_SESSION:
        app_env.main.timer_engine.resume(now_ms)


def _write_morning_state(status="launched", *, started_at=None, today=None, extra=None):
    import morning_session

    today = today or datetime.now().strftime("%Y-%m-%d")
    state_file = morning_session.morning_state_file(today)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "started_at": started_at or datetime.now().isoformat(),
        "status": status,
        "pane_id": "%42",
    }
    if extra:
        data.update(extra)
    state_file.write_text(json.dumps(data))
    return state_file


# ── Part A: keepalive gate ───────────────────────────────────


def test_custodes_no_active_morning_gets_clean_stop_no_keepalive(app_env, monkeypatch):
    """A resting Custodes (persona identity, NO sync mode) with no morning record →
    clean Stop, no re-injection. Identity alone reaches the gate; morning gates it."""
    hooks = sys.modules["routes.hooks"]
    sid = _insert_custodes_instance(app_env.db_path)

    calls = []

    async def fake_offloop(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)

    async def run():
        return await hooks.handle_stop({"session_id": sid})

    result = asyncio.run(run())
    assert result["action"] == "stop_processed"
    assert calls == []  # no keepalive claude-cmd delivery


def test_custodes_ended_morning_gets_clean_stop_no_keepalive(app_env, monkeypatch):
    """status='ended' → custodes identity is necessary but not sufficient; no keepalive."""
    hooks = sys.modules["routes.hooks"]
    sid = _insert_custodes_instance(app_env.db_path)
    _write_morning_state(status="ended")

    calls = []

    async def fake_offloop(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)

    result = asyncio.run(hooks.handle_stop({"session_id": sid}))
    assert result["action"] == "stop_processed"
    assert calls == []


def test_custodes_timer_morning_reinjects_keepalive_without_sync_mode(app_env, monkeypatch):
    """THE key case: a Custodes resolved by PERSONA identity (instance_type='hook_driven',
    NOT 'sync') WITH an active morning session still gets the keepalive — proving the
    gate is persona+morning, not sync."""
    hooks = sys.modules["routes.hooks"]
    sid = _insert_custodes_instance(app_env.db_path, instance_type="hook_driven")
    _enter_timer_morning(app_env)

    calls = []

    async def fake_offloop(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(0)

    # Pane geometry is resolved live from the oracle now (no stored tmux_pane):
    # the Custodes pane resolves to %42, the keepalive target.
    async def fake_resolve_pane(_instance_id):
        return ("%42", "main")

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)
    monkeypatch.setattr(hooks.shared, "resolve_instance_pane", fake_resolve_pane)

    result = asyncio.run(hooks.handle_stop({"session_id": sid}))
    assert result["action"] == "stop_processed_sync"
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "claude-cmd" and cmd[1] == "--pane" and cmd[2] == "%42"
    assert "morning session is still active" in cmd[3]


def test_custodes_old_state_file_does_not_keepalive_without_timer_mode(app_env, monkeypatch):
    """Legacy active/expired state-file data is audit-only; timer mode owns liveness."""
    hooks = sys.modules["routes.hooks"]
    import morning_session

    sid = _insert_custodes_instance(app_env.db_path)
    old = (datetime.now() - timedelta(hours=3)).isoformat()
    _write_morning_state(status="launched", started_at=old)

    calls = []

    async def fake_offloop(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)

    result = asyncio.run(hooks.handle_stop({"session_id": sid}))
    assert result["action"] == "stop_processed"
    assert calls == []

    state = morning_session.read_morning_state()
    assert state["status"] == "launched"


def test_residual_sync_mode_instance_does_not_keepalive(app_env, monkeypatch):
    """Residual sync mode is not morning liveness and never keepalives non-Custodes."""
    hooks = sys.modules["routes.hooks"]
    sid = _insert_plain_instance(app_env.db_path, instance_type="sync", legion="mechanicus")
    _enter_timer_morning(app_env)

    calls = []

    async def fake_offloop(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)

    result = asyncio.run(hooks.handle_stop({"session_id": sid}))
    assert result["action"] == "stop_processed"
    assert calls == []


def test_retired_sync_seat_never_keepalives(app_env, monkeypatch):
    """A RETIRED seat still carrying sync MODE must NOT keepalive — even with an
    active morning. A retired seat is dead identity; re-injecting a keepalive into
    its stale pane is exactly the GT phantom-dispatch the reader filter must stop.
    Mirrors test_residual_sync_mode_instance_also_keepalives, but retired."""
    hooks = sys.modules["routes.hooks"]
    sid = _insert_plain_instance(app_env.db_path, instance_type="sync", legion="mechanicus")
    # Retire the seat, then re-stamp sync MODE so the gate is what blocks it (not a
    # missing marker): a legacy retired row that still reads as sync.
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        "UPDATE instances SET rank = 'retired', golden_throne = 'sync' WHERE id = ?", (sid,)
    )
    conn.commit()
    conn.close()
    _enter_timer_morning(app_env)

    calls = []

    async def fake_offloop(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)

    result = asyncio.run(hooks.handle_stop({"session_id": sid}))
    assert result["action"] == "stop_processed"
    assert all(c[0] != "claude-cmd" for c in calls)


def test_non_custodes_non_sync_instance_never_reaches_keepalive(app_env, monkeypatch):
    """Neither a custodes persona nor sync mode → even with an active morning record,
    a plain one_off instance gets no keepalive."""
    hooks = sys.modules["routes.hooks"]
    sid = _insert_plain_instance(app_env.db_path, instance_type="one_off", legion="astartes")
    _write_morning_state(status="launched")

    calls = []

    async def fake_offloop(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc(0)

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_offloop)

    result = asyncio.run(hooks.handle_stop({"session_id": sid}))
    assert result["action"] == "stop_processed"
    assert all(c[0] != "claude-cmd" for c in calls)


def test_morning_end_writes_status_ended_to_state_file(app_env):
    """POST /api/morning/end durably flips the state file to status='ended'."""
    from fastapi.testclient import TestClient

    import morning_session

    _write_morning_state(status="launched")
    _enter_timer_morning(app_env)
    client = TestClient(app_env.main.app)

    resp = client.post("/api/morning/end")
    assert resp.status_code == 200
    body = resp.json()
    assert body["morning_status"] == "ended"
    assert body["status"] == "working"

    state = morning_session.read_morning_state()
    assert state["status"] == "ended"
    assert state["ended_by"] == "morning-end"


def test_timer_api_reports_morning_session(app_env):
    from fastapi.testclient import TestClient

    _enter_timer_morning(app_env)
    client = TestClient(app_env.main.app)

    resp = client.get("/api/timer")

    assert resp.status_code == 200
    body = resp.json()
    assert body["current_mode"] == "morning_session"
    assert body["manual_mode"] == "morning_session"


def test_morning_entry_resets_metrics_logs_and_injects(app_env, monkeypatch):
    main = app_env.main
    main.timer_engine._total_work_time_ms = 123_000
    main.timer_engine._total_break_time_ms = 456_000
    main.timer_engine._break_balance_ms = -789_000
    injected = []

    async def fake_inject(source):
        injected.append(source)
        return {"injected": True}

    monkeypatch.setattr(main, "_inject_custodes_morning_prompt", fake_inject)

    result = asyncio.run(main.enter_morning_session_internal(source="pytest", inject_prompt=True))

    assert result["current_mode"] == "morning_session"
    assert result["break_balance_ms"] == 0
    assert result["total_work_time_ms"] == 0
    assert result["total_break_time_ms"] == 0
    assert injected == ["pytest"]


def test_morning_entry_flushes_prior_reset_date_before_current_day_shift(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    today = datetime.now(main.ZoneInfo(main.MORNING_SESSION_TIMEZONE)).date().isoformat()
    prior_date = "2000-01-01" if today != "2000-01-01" else "1999-12-31"
    main.timer_engine._daily_start_date = prior_date
    calls = []

    async def fake_generate(date_str):
        calls.append(date_str)

    monkeypatch.setattr(main, "generate_daily_timer_analytics", fake_generate)
    monkeypatch.setattr(main, "_inject_custodes_morning_prompt", lambda source: None)

    result = asyncio.run(main.enter_morning_session_internal(source="pytest", inject_prompt=False))

    assert result["current_mode"] == "morning_session"
    assert calls == [prior_date]

    conn = sqlite3.connect(app_env.db_path)
    shift_triggers = [
        row[0] for row in conn.execute("SELECT trigger FROM timer_shifts ORDER BY id").fetchall()
    ]
    conn.close()
    assert shift_triggers[-1] == "morning_session_start"


def test_morning_entry_does_not_flush_when_reset_date_is_today(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    calls = []

    async def fake_generate(date_str):
        calls.append(date_str)

    monkeypatch.setattr(main, "generate_daily_timer_analytics", fake_generate)
    monkeypatch.setattr(main, "_inject_custodes_morning_prompt", lambda source: None)

    result = asyncio.run(main.enter_morning_session_internal(source="pytest", inject_prompt=False))

    assert result["current_mode"] == "morning_session"
    assert calls == []


def test_morning_enter_endpoint_uses_timer_path_without_prompt(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    main = app_env.main
    main.timer_engine._total_work_time_ms = 123_000
    main.timer_engine._total_break_time_ms = 456_000
    main.timer_engine._break_balance_ms = -789_000
    monkeypatch.setattr(
        main,
        "_inject_custodes_morning_prompt",
        lambda source: (_ for _ in ()).throw(AssertionError("prompt injection disabled")),
    )
    client = TestClient(main.app)

    resp = client.post("/api/morning/enter?inject_prompt=false")

    assert resp.status_code == 200
    body = resp.json()
    assert body["current_mode"] == "morning_session"
    assert body["break_balance_ms"] == 0
    assert body["total_work_time_ms"] == 0
    assert body["total_break_time_ms"] == 0
    assert main.timer_engine.current_mode == main.TimerMode.MORNING_SESSION


# ── Part B: Custodes Discord injection via the singleton pane marker ──


def _msg(channel_name="chat", content="hello"):
    return SimpleNamespace(channel_name=channel_name, content=content, target_tmux_pane=None)


def test_custodes_injection_resolves_via_pane_marker_not_synced(app_env, monkeypatch):
    """Custodes injection resolves via the legion:custodes marker and succeeds even when
    the DB row is stale (one_off, synced=0) — no synced/sync query gates the path."""
    main = app_env.main

    # A stale Custodes row: one_off + synced=0 but pane alive. A synced/live-row hunt
    # would target it only by accident; the marker is what must drive resolution.
    conn = sqlite3.connect(app_env.db_path)
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, instance_type, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'idle', 'custodes', 0, 'one_off', ?, ?)""",
        ("cust-stale", str(uuid.uuid4()), "cust", "/tmp", now, now),
    )
    conn.commit()
    conn.close()

    async def fake_find():
        return "%42"

    async def fake_assert(*a, **k):
        raise AssertionError("assert-instance must not be used when a marked pane is alive")

    # tmuxctl owns pane -> instance: the instance_id for the already-identified pane
    # now comes from the pane's live @INSTANCE_ID stamp, not a stored tmux_pane query.
    async def fake_stamp(pane):
        return "cust-stale" if pane == "%42" else None

    captured = {}

    async def fake_agent_cmd(legion, instance_id, tmux_pane, formatted, channel_name):
        captured.update(
            legion=legion, instance_id=instance_id, tmux_pane=tmux_pane, formatted=formatted
        )
        return True

    monkeypatch.setattr(main, "_find_custodes_tmux_pane", fake_find)
    monkeypatch.setattr(main, "_assert_and_send_custodes", fake_assert)
    monkeypatch.setattr(main.shared, "instance_id_for_pane", fake_stamp)
    monkeypatch.setattr(main, "_agent_cmd_inject", fake_agent_cmd)

    ok = asyncio.run(main._try_discord_injection("custodes", _msg()))
    assert ok is True
    assert captured["tmux_pane"] == "%42"
    # The pane's @INSTANCE_ID stamp supplied the instance_id for the marked pane.
    assert captured["instance_id"] == "cust-stale"
    assert captured["legion"] == "custodes"


def test_custodes_injection_no_pane_delegates_to_assert(app_env, monkeypatch):
    """No live marked pane → delegate upsert-vs-launch to assert-instance (not a DB hunt)."""
    main = app_env.main

    async def fake_find():
        return None

    async def fake_assert(formatted, *, source):
        return {"dispatched": True, "pane": "legion:custodes"}

    async def fake_agent_cmd(*a, **k):
        raise AssertionError("agent-cmd must not run when no marked pane is alive")

    monkeypatch.setattr(main, "_find_custodes_tmux_pane", fake_find)
    monkeypatch.setattr(main, "_assert_and_send_custodes", fake_assert)
    monkeypatch.setattr(main, "_agent_cmd_inject", fake_agent_cmd)

    ok = asyncio.run(main._try_discord_injection("custodes", _msg()))
    assert ok is True


def test_custodes_voice_target_resolves_via_marker(app_env, monkeypatch):
    """Voice target for Custodes resolves via the pane marker, not a synced DB row."""
    main = app_env.main

    async def fake_find():
        return "%42"

    async def fake_exists(pane):
        return pane == "%42"

    monkeypatch.setattr(main, "_find_custodes_tmux_pane", fake_find)
    monkeypatch.setattr(main, "_tmux_pane_exists", fake_exists)

    pane = asyncio.run(main._resolve_discord_voice_target("custodes", _msg()))
    assert pane == "%42"


# ── Part C: launch-side re-fire guard ────────────────────────


def test_run_morning_session_skips_when_already_launched(app_env):
    """A bare re-trigger while status=='launched' must not relaunch (double-trigger)."""
    import morning_session

    _write_morning_state("launched")
    assert morning_session.run_morning_session() == {"status": "already_launched"}


def test_run_morning_session_skips_relaunch_when_already_ended(app_env):
    """An already-ENDED day must NOT be resurrected by a stray /api/morning/start.

    This is the evening-misfire guard: the phone macro re-POSTed hours after the real
    morning ended; before the fix an "ended" record sailed past the guard and
    relaunched Custodes into the legion pane in the evening. Failure statuses are
    intentionally not guarded so a genuine retry still proceeds.
    """
    import morning_session

    _write_morning_state("ended", extra={"ended_by": "test"})
    assert morning_session.run_morning_session() == {"status": "already_ended"}
