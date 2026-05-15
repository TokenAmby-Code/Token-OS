import json
import sqlite3

import pytest


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
    conn.close()

    assert row is not None
    assert row["old_mode"] == "working"
    assert row["new_mode"] == "multitasking"
    assert row["source"] == "timer_worker"
    assert json.loads(row["details"]) == details
