import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _row(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(query, params).fetchone()
    conn.close()
    return dict(row) if row else None


def _count_events(db_path, event_type):
    conn = sqlite3.connect(db_path)
    count = conn.execute(
        "SELECT COUNT(*) FROM events WHERE event_type = ?", (event_type,)
    ).fetchone()[0]
    conn.close()
    return count


def test_day_state_table_exists(app_env):
    row = _row(
        app_env.db_path,
        "SELECT name FROM sqlite_master WHERE type='table' AND name='day_state'",
    )

    assert row["name"] == "day_state"


def test_quiet_hours_morning_latch_released_by_day_start(app_env):
    tz = ZoneInfo("America/Phoenix")
    # Inside the morning-latch window under the default 7am quiet boundary.
    morning = datetime(2026, 5, 10, 6, 30, tzinfo=tz)
    night = datetime(2026, 5, 10, 23, 30, tzinfo=tz)

    assert app_env.main._is_quiet_hours(morning) is True

    # A non-official source must NOT release the morning latch — that was the
    # overnight-bypass regression. (schedule_fallback is used here only as a
    # representative non-official source; the magic-number fallback cron that
    # produced it has been removed — wake is event-driven via alarm_silenced.)
    app_env.shared.set_day_started_at_sync(
        source="schedule_fallback",
        at=morning,
        db_path=app_env.db_path,
        force=True,
    )
    assert app_env.main._is_quiet_hours(morning) is True

    # The official morning system (e.g. alarm_silenced) releases it early.
    state = app_env.shared.set_day_started_at_sync(
        source="alarm_silenced",
        at=morning,
        db_path=app_env.db_path,
        force=True,
    )

    assert state["date"] == "2026-05-10"
    assert app_env.main._is_quiet_hours(morning) is False
    assert app_env.main._is_quiet_hours(night) is True


def test_day_start_endpoint_sets_state_and_fanout(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    import routes.day_start as day_start

    async def fake_phone_reachability(_state):
        return {"status": "ok", "reachable": True}

    monkeypatch.setattr(day_start, "_consumer_phone_reachability", fake_phone_reachability)

    client = TestClient(app_env.main.app)
    resp = client.post(
        "/api/day-start/fire",
        json={"source": "test", "details": {"alarm": "unit"}},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["already_started"] is False
    assert data["day_state"]["day_started_at"]
    assert {item["consumer"] for item in data["fanout"]} >= {
        "quiet_hours",
        "tts_suppression",
        "phone_reachability_check",
    }

    row = _row(
        app_env.db_path, "SELECT * FROM day_state WHERE date = ?", (data["day_state"]["date"],)
    )
    assert row["day_started_at"] == data["day_state"]["day_started_at"]

    second = client.post("/api/day-start/fire", json={"source": "test"}).json()
    assert second["already_started"] is True
    assert second["fanout"] == []


def test_alarm_silenced_fires_day_start_and_launches_morning(app_env, monkeypatch):
    """Silencing the Hatch alarm fires day-start and launches the morning session.

    Pins Problem B's wiring: the alarm-silenced ingress calls
    fire_day_start_internal(source="alarm_silenced"), which latches day_state
    (non-null) and runs the fan-out whose custodes_morning_session consumer
    launches morning. A re-silence is idempotent — no second launch.
    """
    from fastapi.testclient import TestClient

    import routes.day_start as day_start

    launches: list[bool] = []

    async def fake_morning():
        launches.append(True)
        return {"status": "ok", "result": "launched", "pane_id": "%99"}

    async def fake_phone(_state):
        return {"status": "ok", "reachable": True}

    async def fake_rebind():
        return {"status": "ok", "rebound": [], "skipped": []}

    # Stub the network/DB-touching consumers so the test exercises the wiring,
    # not localhost:7777 or the daily-note rebind path.
    monkeypatch.setattr(day_start, "_consumer_custodes_morning_session", fake_morning)
    monkeypatch.setattr(day_start, "_consumer_phone_reachability", fake_phone)
    monkeypatch.setattr(day_start, "_consumer_custodes_doc_rebind", fake_rebind)

    client = TestClient(app_env.main.app)
    resp = client.post("/api/morning/alarm-silenced")

    assert resp.status_code == 200
    data = resp.json()
    assert data["already_started"] is False
    # day_state goes non-null — the core "no morning session at all" fix.
    assert data["day_state"]["day_started_at"]
    assert data["day_state"]["source"] == "alarm_silenced"
    # The morning session was launched via the day-start fan-out.
    assert launches == [True]

    row = _row(
        app_env.db_path,
        "SELECT * FROM day_state WHERE date = ?",
        (data["day_state"]["date"],),
    )
    assert row["day_started_at"] == data["day_state"]["day_started_at"]
    assert row["source"] == "alarm_silenced"

    # Idempotent: a re-silence does not re-fire the fan-out or re-launch morning.
    second = client.post("/api/morning/alarm-silenced").json()
    assert second["already_started"] is True
    assert launches == [True]


# ── Task D: daily_note_creation consumer tests ────────────────────────────────


def test_daily_note_creation_consumer_skipped_when_nas_not_mounted(monkeypatch):
    """Consumer returns skipped immediately when /Volumes/Imperium is not mounted."""
    import routes.day_start as day_start

    monkeypatch.setattr(day_start, "_nas_is_mounted", lambda: False)
    result = asyncio.run(day_start._consumer_daily_note_creation())
    assert result["status"] == "skipped"
    assert "NAS" in result["reason"]


def test_daily_note_creation_consumer_creates_note_when_nas_mounted(app_env, monkeypatch):
    """Consumer creates the Terra daily note when NAS is mounted and note is absent."""
    import routes.day_start as day_start

    monkeypatch.setattr(day_start, "_nas_is_mounted", lambda: True)
    result = asyncio.run(day_start._consumer_daily_note_creation())
    assert result["status"] == "ok"
    assert result["already_existed"] is False
    assert Path(result["path"]).exists()


def test_daily_note_creation_consumer_idempotent(app_env, monkeypatch):
    """Consumer returns ok with already_existed=True when the note already exists."""
    import routes.day_start as day_start

    monkeypatch.setattr(day_start, "_nas_is_mounted", lambda: True)
    first = asyncio.run(day_start._consumer_daily_note_creation())
    assert first["status"] == "ok"
    assert first["already_existed"] is False

    second = asyncio.run(day_start._consumer_daily_note_creation())
    assert second["status"] == "ok"
    assert second["already_existed"] is True


def test_daily_note_creation_ok_no_missed_event(app_env, monkeypatch):
    """fire_day_start_internal with NAS mounted: note created, daily_note_creation_missed NOT emitted."""
    from fastapi.testclient import TestClient

    import routes.day_start as day_start

    async def fake_morning():
        return {"status": "ok", "result": "launched", "pane_id": "%99"}

    async def fake_phone(_state):
        return {"status": "ok", "reachable": True}

    async def fake_rebind():
        return {"status": "ok", "rebound": [], "skipped": []}

    monkeypatch.setattr(day_start, "_nas_is_mounted", lambda: True)
    monkeypatch.setattr(day_start, "_consumer_custodes_morning_session", fake_morning)
    monkeypatch.setattr(day_start, "_consumer_phone_reachability", fake_phone)
    monkeypatch.setattr(day_start, "_consumer_custodes_doc_rebind", fake_rebind)

    client = TestClient(app_env.main.app)
    resp = client.post("/api/day-start/fire", json={"source": "test_dry_run", "force": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True

    note_item = next(
        (item for item in data["fanout"] if item["consumer"] == "daily_note_creation"),
        None,
    )
    assert note_item is not None
    assert note_item["result"]["status"] == "ok"

    # No missed-note alert emitted on success.
    assert _count_events(app_env.db_path, "daily_note_creation_missed") == 0


def test_daily_note_creation_failure_emits_missed_event(app_env, monkeypatch):
    """fire_day_start_internal: consumer exception emits daily_note_creation_missed event."""
    from fastapi.testclient import TestClient

    import routes.day_start as day_start

    async def fake_morning():
        return {"status": "ok", "result": "launched", "pane_id": "%99"}

    async def fake_phone(_state):
        return {"status": "ok", "reachable": True}

    async def fake_rebind():
        return {"status": "ok", "rebound": [], "skipped": []}

    async def failing_note_creation():
        raise RuntimeError("obsidian CLI unavailable in test")

    monkeypatch.setattr(day_start, "_nas_is_mounted", lambda: True)
    monkeypatch.setattr(day_start, "_consumer_custodes_morning_session", fake_morning)
    monkeypatch.setattr(day_start, "_consumer_phone_reachability", fake_phone)
    monkeypatch.setattr(day_start, "_consumer_custodes_doc_rebind", fake_rebind)
    monkeypatch.setattr(day_start, "_consumer_daily_note_creation", failing_note_creation)

    client = TestClient(app_env.main.app)
    resp = client.post("/api/day-start/fire", json={"source": "test_dry_run", "force": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True

    note_item = next(
        (item for item in data["fanout"] if item["consumer"] == "daily_note_creation"),
        None,
    )
    assert note_item is not None
    assert note_item["success"] is False

    # Missed-note alert was emitted.
    assert _count_events(app_env.db_path, "daily_note_creation_missed") == 1
