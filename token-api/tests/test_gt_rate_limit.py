"""Tests for Golden Throne rolling-window rate limiting."""

import importlib
import sqlite3
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from apscheduler.triggers.date import DateTrigger

_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db.close()


@pytest.fixture
def gt_env(monkeypatch):
    """Reload Token-API modules against an isolated DB for each rate-limit test.

    The wider suite reloads ``main`` via ``app_env``. Importing main-level mutable
    globals once at collection time leaves tests pointing at stale objects after
    those reloads, so these tests intentionally resolve main/init_db fresh per
    test.
    """
    db_path = Path(_test_db.name)
    if db_path.exists():
        db_path.unlink()
    monkeypatch.setenv("TOKEN_API_DB", str(db_path))
    monkeypatch.delenv("GT_MAX_FIRES_PER_WINDOW", raising=False)
    monkeypatch.delenv("GT_RATE_WINDOW_SECONDS", raising=False)

    for name in ("shared", "db_schema", "init_db", "main"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)

    init_db = sys.modules["init_db"]
    main = sys.modules["main"]
    init_db.init_database()
    main._golden_throne_fire_times.clear()

    yield SimpleNamespace(db_path=db_path, main=main)

    main._golden_throne_fire_times.clear()
    if db_path.exists():
        db_path.unlink()


def _insert_instance(db_path: Path, instance_id: str | None = None) -> str:
    iid = instance_id or str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, instance_type, zealotry, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'idle', 'one_off', 4, ?, ?)""",
        (iid, str(uuid.uuid4()), f"gt-{iid[:8]}", "/tmp", now, now),
    )
    conn.commit()
    conn.close()
    return iid


def test_under_cap_records_fire_without_delay(gt_env, monkeypatch):
    monkeypatch.setenv("GT_MAX_FIRES_PER_WINDOW", "3")
    monkeypatch.setenv("GT_RATE_WINDOW_SECONDS", "60")

    for offset in (0, 1, 2):
        delay, details = gt_env.main._golden_throne_rate_limit_delay(now=1_000.0 + offset)
        assert delay is None
        assert details["recent_fires"] == offset + 1

    assert len(gt_env.main._golden_throne_fire_times) == 3


def test_at_cap_returns_delay_until_next_available_slot(gt_env, monkeypatch):
    monkeypatch.setenv("GT_MAX_FIRES_PER_WINDOW", "3")
    monkeypatch.setenv("GT_RATE_WINDOW_SECONDS", "60")
    gt_env.main._golden_throne_fire_times.extend([1_000.0, 1_001.0, 1_002.0])

    delay, details = gt_env.main._golden_throne_rate_limit_delay(now=1_010.0)

    assert delay == 50.0
    assert details["max_fires"] == 3
    assert details["window_seconds"] == 60
    assert details["recent_fires"] == 3
    assert (
        len(gt_env.main._golden_throne_fire_times) == 3
    )  # Deferred calls do not consume fire slots.


def test_env_override_changes_cap_and_window(gt_env, monkeypatch):
    monkeypatch.setenv("GT_MAX_FIRES_PER_WINDOW", "1")
    monkeypatch.setenv("GT_RATE_WINDOW_SECONDS", "10")

    delay, _ = gt_env.main._golden_throne_rate_limit_delay(now=100.0)
    assert delay is None

    delay, details = gt_env.main._golden_throne_rate_limit_delay(now=104.0)
    assert delay == 6.0
    assert details["max_fires"] == 1
    assert details["window_seconds"] == 10


@pytest.mark.asyncio
async def test_over_cap_defers_via_date_trigger_and_logs(gt_env, monkeypatch):
    monkeypatch.setenv("GT_MAX_FIRES_PER_WINDOW", "1")
    monkeypatch.setenv("GT_RATE_WINDOW_SECONDS", "60")
    instance_id = _insert_instance(gt_env.db_path)
    gt_env.main._golden_throne_fire_times.append(time.time())

    added_jobs = []

    class FakeScheduler:
        def add_job(self, *args, **kwargs):
            added_jobs.append((args, kwargs))

    events = []

    async def fake_log_event(event_type, **kwargs):
        events.append((event_type, kwargs))

    monkeypatch.setattr(gt_env.main, "scheduler", FakeScheduler())
    monkeypatch.setattr(gt_env.main, "log_event", fake_log_event)

    await gt_env.main.golden_throne_followup(instance_id)

    assert len(added_jobs) == 1
    args, kwargs = added_jobs[0]
    assert args[0] is gt_env.main.golden_throne_followup
    assert isinstance(args[1], DateTrigger)
    assert kwargs["args"] == [instance_id]
    assert kwargs["id"] == f"golden-throne-{instance_id}"
    assert kwargs["jobstore"] == "golden_throne"
    assert kwargs["replace_existing"] is True

    assert events
    event_type, payload = events[0]
    assert event_type == "gt_fire_deferred"
    assert payload["instance_id"] == instance_id
    assert payload["details"]["reason"] == "rate_limit"
    assert payload["details"]["max_fires"] == 1
    assert payload["details"]["window_seconds"] == 60
    assert payload["details"]["deferred_seconds"] > 0
