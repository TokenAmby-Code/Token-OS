import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo


def _row(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(query, params).fetchone()
    conn.close()
    return dict(row) if row else None


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

    # A non-official source (the automated schedule_fallback wake-anchor) must
    # NOT release the morning latch — that was the overnight-bypass regression.
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


def test_wake_anchor_schedule_sync_reads_daily_note_frontmatter(app_env):
    import asyncio

    import routes.day_start as day_start

    daily_dir = app_env.db_path.parent / "Imperium-ENV" / "Terra" / "Journal" / "Daily"
    daily_dir.mkdir(parents=True)
    (daily_dir / "2026-05-10.md").write_text(
        "---\ntitle: 2026-05-10\nwake_anchor: 09:15\n---\n\n# Daily\n",
        encoding="utf-8",
    )

    result = asyncio.run(
        day_start.sync_day_start_schedule_from_daily_note(
            date_str="2026-05-10",
            db_path=app_env.db_path,
        )
    )

    assert result == {
        "wake_anchor": "09:15",
        "cron": "15 9 * * *",
        "task_id": "day_start_schedule_fallback",
    }
    row = _row(
        app_env.db_path,
        "SELECT schedule FROM scheduled_tasks WHERE id = 'day_start_schedule_fallback'",
    )
    assert row["schedule"] == "15 9 * * *"


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
