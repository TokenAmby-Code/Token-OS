"""Quiet-hours predicate tests for the send gate's standalone DB reader.

These pin the overnight-bypass fix: the morning quiet latch is released ONLY by
the official morning system, never by a schedule_fallback wake-anchor or a
prior-day row, and the gate reads the DB cold (no trust in any in-process
cache).
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pytest
import tmuxctl.send_gate as send_gate

PHX = ZoneInfo("America/Phoenix")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "agents.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE day_state (
            date TEXT PRIMARY KEY, day_started_at TEXT, source TEXT,
            details_json TEXT, created_at TEXT, updated_at TEXT)"""
    )
    conn.execute(
        "CREATE TABLE timer_state (id INTEGER PRIMARY KEY, state_json TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, instance_id TEXT, device_id TEXT, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("TOKEN_API_DB", str(path))
    monkeypatch.setenv("TOKEN_API_QUIET_START_HOUR", "23")
    monkeypatch.setenv("TOKEN_API_QUIET_END_HOUR", "9")
    monkeypatch.setenv("TOKEN_API_QUIET_TIMEZONE", "America/Phoenix")
    return path


def _set_day_state(path, date, day_started_at, source):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO day_state (date, day_started_at, source) VALUES (?, ?, ?)",
        (date, day_started_at, source),
    )
    conn.commit()
    conn.close()


def _set_timer_quiet(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO timer_state (id, state_json) VALUES (1, ?)",
        ('{"format_version": 2, "manual_mode": "quiet"}',),
    )
    conn.commit()
    conn.close()


def test_clock_window_active_at_night_with_empty_db(db):
    active, ctx = send_gate.quiet_hours_active(now=datetime(2026, 5, 27, 3, 0, tzinfo=PHX))
    assert active is True
    assert ctx["clock_segment"] == "morning_latch"


def test_outside_window_inactive(db):
    active, _ = send_gate.quiet_hours_active(now=datetime(2026, 5, 27, 14, 0, tzinfo=PHX))
    assert active is False


# The overnight regression: a schedule_fallback day_started_at must NOT release
# the morning quiet latch.
def test_schedule_fallback_day_start_does_not_release_morning_latch(db):
    _set_day_state(db, "2026-05-27", "2026-05-27T08:30:00-07:00", "schedule_fallback")
    active, ctx = send_gate.quiet_hours_active(now=datetime(2026, 5, 27, 7, 0, tzinfo=PHX))
    assert active is True, "schedule_fallback wake-anchor must not flip quiet hours off"
    assert ctx["morning_latch_released"] is False


def test_prior_day_row_does_not_leak_across_midnight(db):
    # Row exists for the prior day only; evaluating the next morning must stay quiet.
    _set_day_state(db, "2026-05-26", "2026-05-26T08:30:00-07:00", "morning")
    active, _ = send_gate.quiet_hours_active(now=datetime(2026, 5, 27, 7, 0, tzinfo=PHX))
    assert active is True


def test_official_morning_source_releases_latch(db):
    _set_day_state(db, "2026-05-27", "2026-05-27T08:30:00-07:00", "morning")
    active, ctx = send_gate.quiet_hours_active(now=datetime(2026, 5, 27, 7, 0, tzinfo=PHX))
    assert active is False
    assert ctx["morning_latch_released"] is True


def test_session_quiet_latch_holds_outside_window(db):
    # Nightly debrief latched QUIET; even at midday the gate stays closed.
    _set_timer_quiet(db)
    active, ctx = send_gate.quiet_hours_active(now=datetime(2026, 5, 27, 14, 0, tzinfo=PHX))
    assert active is True
    assert ctx["session_quiet_latch"] is True
