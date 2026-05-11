from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from now_widget import NowWidgetTelemetry, compose_now_markdown, write_today_now_callout


def test_compose_now_markdown_structure_and_mst_timestamp():
    telemetry = NowWidgetTelemetry(
        timer={"current_mode": "working", "break_balance_ms": 12 * 60 * 1000},
        active_instances=["custodes-main", "mechanicus-pr-37"],
        location_zone="home",
        desktop_mode="code",
        recent_cascade=None,
    )
    now = datetime(2026, 5, 9, 16, 23, tzinfo=ZoneInfo("America/Phoenix"))

    body = compose_now_markdown(telemetry, now=now)

    assert "**Block:** 16:23 MST live snapshot" in body
    assert "**Posture:** custodes daily-note surface" in body
    assert "**Balance:** +12min · timer mode: WORKING" in body
    assert "**Active:** custodes-main, mechanicus-pr-37" in body
    assert "**Geofence:** home · desktop_mode: code" in body
    assert "**Cascade:** none in last hour" in body
    assert "*Last updated 16:23 MST*" in body


def test_compose_derives_v2_timer_mode_when_flat_mode_absent():
    telemetry = NowWidgetTelemetry(
        timer={"activity": "working", "productivity_active": False, "break_balance_ms": 0},
        active_instances=[],
        location_zone=None,
        desktop_mode=None,
        recent_cascade=None,
    )
    now = datetime(2026, 5, 9, 16, 23, tzinfo=ZoneInfo("America/Phoenix"))

    body = compose_now_markdown(telemetry, now=now)

    assert "timer mode: IDLE" in body


def test_write_today_now_callout_smoke_with_fixture_db(tmp_path):
    import json
    import sqlite3

    db = tmp_path / "agents.db"
    note_dir = tmp_path / "Daily"
    note_dir.mkdir()
    today = datetime(2026, 5, 9, 19, 0, tzinfo=ZoneInfo("America/Phoenix"))
    note = note_dir / "2026-05-09.md"
    note.write_text("# 2026-05-09\n\nManual text.\n", encoding="utf-8")

    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE timer_state (id INTEGER PRIMARY KEY, state_json TEXT)")
        conn.execute(
            "INSERT INTO timer_state (id, state_json) VALUES (1, ?)",
            (
                json.dumps(
                    {
                        "current_mode": "break",
                        "break_balance_ms": -5 * 60 * 1000,
                        "desktop_mode": "video",
                    }
                ),
            ),
        )
        conn.execute(
            """CREATE TABLE claude_instances (
                id TEXT,
                tab_name TEXT,
                working_dir TEXT,
                status TEXT,
                last_activity TEXT,
                tmux_pane TEXT,
                pane_label TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO claude_instances VALUES ('1', 'custodes-main', '/tmp/x', 'processing', '2026-05-09T18:59:00', '%1', 'palace:N')"
        )
        conn.execute("CREATE TABLE events (event_type TEXT, details TEXT, created_at TEXT)")

    result = write_today_now_callout(db, note_dir, today=today)

    text = note.read_text(encoding="utf-8")
    assert result.action == "appended"
    assert "Manual text." in text
    assert "<!-- callout:now BEGIN -->" in text
    assert "> **Balance:** -5min · timer mode: BREAK" in text
    assert "> **Active:** 1:N custodes-main" in text


def test_now_widget_active_instances_reject_claude_placeholder(tmp_path):
    import json
    import sqlite3

    from now_widget import load_telemetry

    db = tmp_path / "agents.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE timer_state (id INTEGER PRIMARY KEY, state_json TEXT)")
        conn.execute("INSERT INTO timer_state (id, state_json) VALUES (1, ?)", (json.dumps({}),))
        conn.execute(
            """CREATE TABLE claude_instances (
                id TEXT,
                tab_name TEXT,
                working_dir TEXT,
                status TEXT,
                last_activity TEXT,
                tmux_pane TEXT,
                pane_label TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO claude_instances VALUES ('1', 'Claude 08:14', '/tmp/x', 'processing', '2026-05-09T18:59:00', '%108', 'palace:NW')"
        )
        conn.execute(
            "INSERT INTO claude_instances VALUES ('2', 'Claude 08:15', '/tmp/y', 'idle', '2026-05-09T18:58:00', '%109', NULL)"
        )
        conn.execute("CREATE TABLE events (event_type TEXT, details TEXT, created_at TEXT)")

    telemetry = load_telemetry(db)

    assert telemetry.active_instances == ["1:NW", "%109"]
    assert all("Claude" not in surface for surface in telemetry.active_instances)
