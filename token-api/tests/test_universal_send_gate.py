"""Token-API side of the universal send-gate invariant.

(c) The morning quiet latch is session-driven: a day_started_at written by the
    schedule_fallback wake-anchor must NOT release it; only the official morning
    system may. This is the overnight-bypass root cause.
(d) The timer must not ORIGINATE Custodes interventions while quiet hours is
    active (defence in depth above the send gate).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PHX = ZoneInfo("America/Phoenix")


def _insert_day_state(db_path: str | Path, date: str, day_started_at: str, source: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO day_state (date, day_started_at, source) VALUES (?, ?, ?)",
            (date, day_started_at, source),
        )
        conn.commit()


# ---- (c) morning-latch source rule -----------------------------------------


def test_schedule_fallback_does_not_release_morning_latch(app_env: Any, monkeypatch: Any) -> None:
    shared = app_env.shared
    monkeypatch.setenv("TOKEN_API_QUIET_START_HOUR", "23")
    monkeypatch.setenv("TOKEN_API_QUIET_END_HOUR", "9")
    monkeypatch.setenv("TOKEN_API_QUIET_TIMEZONE", "America/Phoenix")
    shared.ensure_day_state_table_sync()
    _insert_day_state(
        app_env.db_path, "2026-05-27", "2026-05-27T08:30:00-07:00", "schedule_fallback"
    )

    status = shared.get_quiet_hours_status(now=datetime(2026, 5, 27, 7, 0, tzinfo=PHX))
    assert status["active"] is True, "schedule_fallback wake-anchor must not end quiet hours"


def test_official_morning_source_releases_morning_latch(app_env: Any, monkeypatch: Any) -> None:
    shared = app_env.shared
    monkeypatch.setenv("TOKEN_API_QUIET_START_HOUR", "23")
    monkeypatch.setenv("TOKEN_API_QUIET_END_HOUR", "9")
    monkeypatch.setenv("TOKEN_API_QUIET_TIMEZONE", "America/Phoenix")
    shared.ensure_day_state_table_sync()
    _insert_day_state(app_env.db_path, "2026-05-27", "2026-05-27T08:30:00-07:00", "morning")

    status = shared.get_quiet_hours_status(now=datetime(2026, 5, 27, 7, 0, tzinfo=PHX))
    assert status["active"] is False


# ---- (d) timer does not originate interventions during quiet ---------------


async def test_timer_intervention_suppressed_during_quiet(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    dispatched: list[str] = []

    async def _recording_handler(event_name, source, **kwargs):
        dispatched.append(event_name)
        return {"dispatched": True}

    monkeypatch.setattr(main, "handle_custodes_state_event", _recording_handler)
    # Quiet hours active by clock window (3 AM), regardless of timer mode.
    monkeypatch.setattr(
        main.shared,
        "get_quiet_hours_status",
        lambda now=None: {"active": True, "reason": "quiet_hours"},
    )

    result = await main._dispatch_timer_intervention(
        "idle_timeout", "timer_worker", payload={"timer_mode": "break"}
    )

    assert result is False, "timer must not originate an intervention during quiet hours"
    assert dispatched == [], "no custodes intervention may be dispatched during quiet hours"


async def test_timer_intervention_dispatches_outside_quiet(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    dispatched: list[str] = []

    async def _recording_handler(event_name, source, **kwargs):
        dispatched.append(event_name)
        return {"dispatched": True}

    monkeypatch.setattr(main, "handle_custodes_state_event", _recording_handler)
    monkeypatch.setattr(
        main.shared,
        "get_quiet_hours_status",
        lambda now=None: {"active": False, "reason": "outside_quiet_hours"},
    )
    monkeypatch.setattr(main, "is_quiet_hours", lambda now=None: False)

    result = await main._dispatch_timer_intervention(
        "idle_timeout", "timer_worker", payload={"timer_mode": "break"}
    )
    # Allow the scheduled task to run.
    import asyncio

    await asyncio.sleep(0)
    assert result is True
    assert dispatched == ["idle_timeout"]
