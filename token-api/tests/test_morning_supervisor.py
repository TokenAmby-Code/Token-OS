"""Unit tests for the morning-session supervisor (the redundant suspenders layer).

Covers the two things the plan calls out explicitly — history-derivation
(weekday->last weekday, weekend->last weekend) and the no-history
no-supervision path — plus the arm/poll/failure state machine:

  - derive_expected_wake: day-type matching, already_started pollution skip,
    today-exclusion, no-history -> None.
  - arm_morning_supervisor: schedules the relative poller; no-history and
    recover-past-window are no-ops.
  - _supervisor_poll_job: ack+custodes -> disarm; ack-no-custodes -> alert+retry;
    no-ack within grace -> wait; no-ack past grace -> alert + Custodes day-start backstop.
  - _handle_failure: no_ack latches day-start through /api/day-start/fire;
    ack_no_custodes retries /api/morning/start.

Run:
    cd token-api && .venv/bin/python -m pytest tests/test_morning_supervisor.py -v
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import morning_supervisor as ms

_TZ = ZoneInfo("America/Phoenix")


# ── Helpers ───────────────────────────────────────────────────


def _seed_events(db_path, rows):
    """rows = [(source, day_started_at_iso, already_started, created_at), ...]."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            instance_id TEXT,
            device_id TEXT,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    for source, dsa, already, created in rows:
        details = json.dumps({"source": source, "day_started_at": dsa, "already_started": already})
        conn.execute(
            "INSERT INTO events (event_type, details, created_at) VALUES ('day_start_fired', ?, ?)",
            (details, created),
        )
    conn.commit()
    conn.close()


class FakeScheduler:
    def __init__(self):
        self.jobs = {}
        self.removed = []

    def add_job(self, func, trigger, *, kwargs=None, id=None, **_):
        self.jobs[id] = {"func": func, "trigger": trigger, "kwargs": kwargs}

    def remove_job(self, job_id):
        if job_id in self.jobs:
            del self.jobs[job_id]
        self.removed.append(job_id)


async def _anoop(*a, **k):
    return None


# ── derive_expected_wake ──────────────────────────────────────


def test_derive_weekday_picks_last_weekday(tmp_path):
    db = tmp_path / "agents.db"
    _seed_events(
        db,
        [
            # A Sunday (weekend) ack at 07:50 — must be ignored on a weekday.
            ("alarm_silenced", "2026-05-31T07:50:00-07:00", False, "2026-05-31 14:50:00"),
            # A Thursday (weekday) ack at 08:10 — the one we want.
            ("alarm_silenced", "2026-06-04T08:10:00-07:00", False, "2026-06-04 15:10:00"),
        ],
    )
    # now = Friday 2026-06-05 (weekday)
    now = datetime(2026, 6, 5, 4, 0, tzinfo=_TZ)
    wake = asyncio.run(ms.derive_expected_wake(now_local=now, db_path=db))
    assert wake is not None
    assert (wake.hour, wake.minute) == (8, 10)


def test_derive_weekend_picks_last_weekend(tmp_path):
    db = tmp_path / "agents.db"
    _seed_events(
        db,
        [
            ("alarm_silenced", "2026-06-04T08:10:00-07:00", False, "2026-06-04 15:10:00"),
            ("alarm_silenced", "2026-05-31T07:50:00-07:00", False, "2026-05-31 14:50:00"),
        ],
    )
    # now = Saturday 2026-06-06 (weekend) → wants the Sunday 07:50, not Thu 08:10
    now = datetime(2026, 6, 6, 4, 0, tzinfo=_TZ)
    wake = asyncio.run(ms.derive_expected_wake(now_local=now, db_path=db))
    assert wake is not None
    assert (wake.hour, wake.minute) == (7, 50)


def test_derive_no_history_returns_none(tmp_path):
    db = tmp_path / "agents.db"
    _seed_events(db, [])  # table exists, no rows
    now = datetime(2026, 6, 5, 4, 0, tzinfo=_TZ)
    assert asyncio.run(ms.derive_expected_wake(now_local=now, db_path=db)) is None


def test_derive_skips_already_started_and_nonalarm_sources(tmp_path):
    db = tmp_path / "agents.db"
    _seed_events(
        db,
        [
            # Polluted: alarm_silenced but already_started → day_started_at is the
            # latched 08:30, NOT the real ack time. Must be skipped.
            ("alarm_silenced", "2026-06-04T08:30:00-07:00", True, "2026-06-04 15:30:00"),
            # Wrong source: the removed magic-number fallback. Must be skipped.
            ("schedule_fallback", "2026-06-04T08:30:00-07:00", False, "2026-06-04 15:30:01"),
        ],
    )
    now = datetime(2026, 6, 5, 4, 0, tzinfo=_TZ)
    assert asyncio.run(ms.derive_expected_wake(now_local=now, db_path=db)) is None


def test_derive_ignores_todays_own_ack(tmp_path):
    db = tmp_path / "agents.db"
    # Only a same-day ack exists — we predict BEFORE today's ack, so it's ignored.
    _seed_events(
        db,
        [("alarm_silenced", "2026-06-05T08:05:00-07:00", False, "2026-06-05 15:05:00")],
    )
    now = datetime(2026, 6, 5, 4, 0, tzinfo=_TZ)
    assert asyncio.run(ms.derive_expected_wake(now_local=now, db_path=db)) is None


# ── arm_morning_supervisor ────────────────────────────────────


def test_arm_no_history_does_not_schedule(tmp_path, monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(ms.shared, "scheduler", fake)
    monkeypatch.setattr(ms, "log_event", _anoop)
    monkeypatch.setattr(ms, "derive_expected_wake", _anoop)  # returns None

    now = datetime(2026, 6, 5, 4, 0, tzinfo=_TZ)
    result = asyncio.run(ms.arm_morning_supervisor(now_local=now))

    assert result == {"armed": False, "reason": "no_history"}
    assert ms.SUPERVISOR_POLL_JOB_ID not in fake.jobs


def test_arm_schedules_relative_poller(tmp_path, monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(ms.shared, "scheduler", fake)
    monkeypatch.setattr(ms, "log_event", _anoop)

    async def fake_derive(*, now_local=None, db_path=None):
        return now_local.replace(hour=8, minute=10)

    monkeypatch.setattr(ms, "derive_expected_wake", fake_derive)

    now = datetime(2026, 6, 5, 4, 0, tzinfo=_TZ)
    result = asyncio.run(ms.arm_morning_supervisor(now_local=now))

    assert result["armed"] is True
    assert result["expected_wake"] == "08:10"
    assert ms.SUPERVISOR_POLL_JOB_ID in fake.jobs
    job = fake.jobs[ms.SUPERVISOR_POLL_JOB_ID]
    assert job["kwargs"]["date_str"] == "2026-06-05"
    # deadline = anchor + 15min grace
    assert "08:25" in job["kwargs"]["deadline_iso"]


def test_arm_recover_past_window_does_not_schedule(monkeypatch):
    fake = FakeScheduler()
    monkeypatch.setattr(ms.shared, "scheduler", fake)
    monkeypatch.setattr(ms, "log_event", _anoop)

    async def fake_derive(*, now_local=None, db_path=None):
        return now_local.replace(hour=8, minute=0)

    monkeypatch.setattr(ms, "derive_expected_wake", fake_derive)

    # Restart at 09:00 — well past 08:00 + 15min grace.
    now = datetime(2026, 6, 5, 9, 0, tzinfo=_TZ)
    result = asyncio.run(ms.arm_morning_supervisor(recover=True, now_local=now))

    assert result == {"armed": False, "reason": "window_passed"}
    assert ms.SUPERVISOR_POLL_JOB_ID not in fake.jobs


# ── _supervisor_poll_job ──────────────────────────────────────


def _poll(
    monkeypatch,
    *,
    now,
    ack,
    morning_active=False,
    custodes=None,
    anchor="2026-06-05T08:00:00-07:00",
    deadline="2026-06-05T08:15:00-07:00",
):
    """Drive one poll tick with stubbed signals; return recorded failures + disarm.

    The health gate is ``morning_is_active`` (the deterministic state-file signal),
    not bare custodes-process liveness; ``custodes`` only feeds the ok-log detail.
    """
    fake = FakeScheduler()
    fake.jobs[ms.SUPERVISOR_POLL_JOB_ID] = {"placeholder": True}
    monkeypatch.setattr(ms.shared, "scheduler", fake)
    monkeypatch.setattr(ms, "log_event", _anoop)
    monkeypatch.setattr(ms, "quiet_hours_local_now", lambda: now)

    async def fake_ack(*, now_local=None, db_path=None):
        return ack

    async def fake_active(date_str=None):
        return morning_active

    async def fake_cust():
        return custodes

    failures = []

    async def fake_failure(*, failure_type, now_local, anchor_iso, ack):
        failures.append(failure_type)

    monkeypatch.setattr(ms, "ack_seen_today", fake_ack)
    monkeypatch.setattr(ms, "morning_is_active", fake_active)
    monkeypatch.setattr(ms, "custodes_running", fake_cust)
    monkeypatch.setattr(ms, "_handle_failure", fake_failure)

    asyncio.run(ms._supervisor_poll_job("2026-06-05", anchor, deadline))
    return failures, fake.removed


def test_poll_ack_and_active_morning_disarms(monkeypatch):
    now = datetime(2026, 6, 5, 8, 5, tzinfo=_TZ)
    failures, removed = _poll(
        monkeypatch,
        now=now,
        ack={"day_started_at": "x"},
        morning_active=True,
        custodes={"id": "abc"},
    )
    assert failures == []
    assert ms.SUPERVISOR_POLL_JOB_ID in removed


def test_poll_ack_but_morning_not_active_alerts_and_disarms(monkeypatch):
    # A custodes process is alive (the singleton always is), but the morning
    # session never confirmed active — the reactive launch failed. The supervisor
    # must catch this rather than be fooled by the resting singleton.
    now = datetime(2026, 6, 5, 8, 5, tzinfo=_TZ)
    failures, removed = _poll(
        monkeypatch,
        now=now,
        ack={"day_started_at": "x"},
        morning_active=False,
        custodes={"id": "resting-singleton"},
    )
    assert failures == ["ack_no_custodes"]
    assert ms.SUPERVISOR_POLL_JOB_ID in removed


def test_poll_no_ack_before_deadline_keeps_waiting(monkeypatch):
    now = datetime(2026, 6, 5, 8, 5, tzinfo=_TZ)  # before 08:15 deadline
    failures, removed = _poll(monkeypatch, now=now, ack=None)
    assert failures == []
    assert removed == []  # still armed


def test_poll_no_ack_past_deadline_alerts(monkeypatch):
    now = datetime(2026, 6, 5, 8, 20, tzinfo=_TZ)  # past 08:15 deadline
    failures, removed = _poll(monkeypatch, now=now, ack=None)
    assert failures == ["no_ack"]
    assert ms.SUPERVISOR_POLL_JOB_ID in removed


# ── _handle_failure recovery actions ──────────────────────────


def _capture_failure(monkeypatch):
    posts = []
    notices = []

    async def fake_post(path, json_body=None):
        posts.append((path, json_body))
        return {"ok": True}

    async def fake_notify(message):
        notices.append(message)
        return {"ok": True}

    monkeypatch.setattr(ms, "log_event", _anoop)
    monkeypatch.setattr(ms, "_post_local", fake_post)
    monkeypatch.setattr(ms, "_notify", fake_notify)
    monkeypatch.setattr(ms, "_discord_alert", _anoop)
    monkeypatch.setattr(ms, "_backup_message_to_custodes", _anoop)
    return posts, notices


def test_handle_failure_no_ack_fires_custodes_day_start_backstop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts, notices = _capture_failure(monkeypatch)
    now = datetime(2026, 6, 5, 8, 20, tzinfo=_TZ)
    asyncio.run(
        ms._handle_failure(
            failure_type="no_ack",
            now_local=now,
            anchor_iso="2026-06-05T08:00:00-07:00",
            ack=None,
        )
    )
    # The Emperor is alerted, and recovery goes through the single day-start
    # latch before the morning fan-out starts Custodes. This is not a bare
    # /api/morning/start phantom.
    assert notices and "firing the Custodes day-start backstop now" in notices[0]
    assert any(path == "/api/day-start/fire" for path, _ in posts)
    assert any(
        path == "/api/day-start/fire"
        and body["source"] == "custodes"
        and body["details"]["reason"] == "morning_supervisor_no_ack_backstop"
        for path, body in posts
    )
    assert not any(path == "/api/morning/start" for path, _ in posts)


def test_handle_failure_ack_no_custodes_relaunches(monkeypatch: pytest.MonkeyPatch) -> None:
    posts, notices = _capture_failure(monkeypatch)
    now = datetime(2026, 6, 5, 8, 5, tzinfo=_TZ)
    asyncio.run(
        ms._handle_failure(
            failure_type="ack_no_custodes",
            now_local=now,
            anchor_iso="2026-06-05T08:00:00-07:00",
            ack={"day_started_at": "2026-06-05T08:00:00-07:00"},
        )
    )
    # A real ack exists, so retrying the launch is legitimate recovery.
    assert any(path == "/api/morning/start" for path, _ in posts)
    assert not any(path == "/api/day-start/fire" for path, _ in posts)
    assert notices
