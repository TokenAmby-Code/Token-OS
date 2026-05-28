import json
import sqlite3

import pytest


@pytest.mark.asyncio
async def test_timer_write_sample_persists_read_model_point(app_env):
    work_state = {
        "productivity_active": True,
        "active_instance_count": 3,
        "processing_recent_count": 2,
        "observed_agent_count": 4,
    }

    await app_env.shared.timer_write_sample(source="pytest_periodic", work_state=work_state)

    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT mode, activity, productivity_active, break_balance_ms,
                  active_instance_count, processing_recent_count,
                  observed_agent_count, desktop_mode, phone_app, source
           FROM timer_samples
           WHERE source = ?""",
        ("pytest_periodic",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["mode"] == app_env.shared.timer_engine.current_mode.value
    assert row["activity"] == app_env.shared.timer_engine.activity.value
    assert row["productivity_active"] == 1
    assert row["break_balance_ms"] == app_env.shared.timer_engine.break_balance_ms
    assert row["active_instance_count"] == 3
    assert row["processing_recent_count"] == 2
    assert row["observed_agent_count"] == 4
    assert row["source"] == "pytest_periodic"


@pytest.mark.asyncio
async def test_timer_log_shift_serializes_nested_details_dict(app_env):
    details = {
        "productivity_active": True,
        "active_instance_count": 2,
        "telemetry": {
            "sources": ["db", "tmux"],
            "weights": {"processing": 1.0, "idle": 0.5},
        },
    }

    await app_env.shared.timer_log_shift(
        "working",
        "multitasking",
        trigger="productivity_active",
        source="timer_worker",
        details=details,
    )

    conn = sqlite3.connect(app_env.db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT old_mode, new_mode, trigger, source, details
           FROM timer_shifts
           WHERE trigger = ?""",
        ("productivity_active",),
    ).fetchone()
    sample = conn.execute(
        """SELECT mode, activity, productivity_active, break_balance_ms,
                  work_time_ms, source
           FROM timer_samples
           WHERE source = ?
           ORDER BY id DESC
           LIMIT 1""",
        ("timer_worker",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["old_mode"] == "working"
    assert row["new_mode"] == "multitasking"
    assert row["source"] == "timer_worker"
    assert json.loads(row["details"]) == details
    assert sample is not None
    assert sample["mode"] == app_env.shared.timer_engine.current_mode.value
    assert sample["activity"] == app_env.shared.timer_engine.activity.value
    assert sample["productivity_active"] in (0, 1)
    assert sample["break_balance_ms"] == app_env.shared.timer_engine.break_balance_ms
    assert sample["work_time_ms"] == app_env.shared.timer_engine.total_work_time_ms
