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
    morning = datetime(2026, 5, 10, 8, 30, tzinfo=tz)
    night = datetime(2026, 5, 10, 23, 30, tzinfo=tz)

    assert app_env.main._is_quiet_hours(morning) is True

    state = app_env.shared.set_day_started_at_sync(
        source="test",
        at=morning,
        db_path=app_env.db_path,
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
