"""Human supervision/AUQ anchors share one idle-break exemption predicate."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any

import pytest

PANE = "%812"
SESSION_ID = "sess-human-supervision"


def _insert_instance(db_path, *, last_activity: datetime, hook_driven: int = 0) -> None:
    with sqlite3.connect(db_path) as conn:
        cols = conn.execute("PRAGMA table_info(legacy_instances)").fetchall()
        values: dict[str, Any] = {}
        for _cid, name, ctype, notnull, dflt, _pk in cols:
            if dflt is not None or not notnull:
                continue
            values[name] = 0 if ctype.upper() in ("INTEGER", "REAL") else "x"
        values.update(
            id=SESSION_ID,
            session_id=SESSION_ID,
            tab_name="supervised",
            status="working",
            engine="claude",
            working_dir="/work",
            device_id="Mac-Mini",
            last_activity=last_activity.isoformat(),
            legion="mechanicus",
            is_subagent=0,
            hook_driven=hook_driven,
        )
        keys = list(values)
        conn.execute(
            f"INSERT INTO legacy_instances ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
            [values[k] for k in keys],
        )
        conn.commit()


def _add_work_action(db_path, *, minutes_ago: int, source: str = "prompt_submit") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO events (event_type, instance_id, device_id, details, created_at)
            VALUES ('work_action', ?, 'test', ?, datetime('now', ?))
            """,
            (
                SESSION_ID,
                json.dumps({"source": source, "note": f"session_id={SESSION_ID}"}),
                f"-{minutes_ago} minutes",
            ),
        )
        conn.commit()


@pytest.fixture
def supervised_env(app_env, monkeypatch):
    main = app_env.main

    async def _pane_rows():
        return [(PANE, "claude", "/work", "win", "/dev/ttys9")]

    async def _engine_by_tty():
        return {}

    async def _live_panes():
        return [
            {
                "pane_id": PANE,
                "pane_pid": 1234,
                "instance_id": SESSION_ID,
                "pane_label": None,
                "pane_role": "mechanicus:admin",
                "current_command": "node",
            }
        ]

    monkeypatch.setattr(main, "_tmux_pane_rows", _pane_rows)
    monkeypatch.setattr(main, "_agent_engine_by_tty", _engine_by_tty)
    monkeypatch.setattr(main, "_live_agent_panes", _live_panes)
    monkeypatch.setattr(main, "_pane_is_agent_from_snapshot", lambda c, t, m: (True, "claude"))
    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)
    return app_env


async def test_human_work_action_holds_full_supervisory_window(supervised_env):
    _insert_instance(supervised_env.db_path, last_activity=datetime.now() - timedelta(hours=1))
    _add_work_action(supervised_env.db_path, minutes_ago=14)

    ws = await supervised_env.main.compute_work_state()

    assert ws.productivity_active is True
    assert ws.productivity_hold == "work_action_buffer"
    assert ws.within_human_work_action_window is True
    assert ws.idle_timeout_exempt is True
    assert ws.work_action_buffer_remaining_seconds > 5 * 60
    assert ws.idle_timeout_exempt == supervised_env.main.human_supervision_idle_exempt(
        human_anchored=ws.human_anchored_instance_count > 0,
        within_human_work_action_window=ws.within_human_work_action_window,
    )


async def test_hook_driven_work_action_still_ages_out_on_short_path(supervised_env):
    _insert_instance(
        supervised_env.db_path,
        last_activity=datetime.now() - timedelta(hours=1),
        hook_driven=1,
    )
    _add_work_action(supervised_env.db_path, minutes_ago=14)

    ws = await supervised_env.main.compute_work_state()

    assert ws.productivity_active is False
    assert ws.work_action_source is None
    assert ws.within_human_work_action_window is False
    assert ws.idle_timeout_exempt is False


def test_timer_idle_break_suppressed_by_shared_predicate(supervised_env):
    from timer import IDLE_TIMEOUT_FROM_WORKING_MS, TimerEngine, TimerEvent, TimerMode

    engine = TimerEngine(now_mono_ms=0)
    engine.set_productivity(False, 0)
    engine.idle_timeout_exempt = supervised_env.main.human_supervision_idle_exempt(
        human_anchored=False,
        within_human_work_action_window=True,
    )

    result = engine.tick(IDLE_TIMEOUT_FROM_WORKING_MS + 1, "2026-06-23")

    assert TimerEvent.IDLE_TIMEOUT not in result.events
    assert engine.current_mode == TimerMode.IDLE
    assert engine.break_balance_ms == 0


def test_ask_user_question_answer_sets_anchor_and_status_stop_clears(supervised_env):
    main = supervised_env.main
    _insert_instance(supervised_env.db_path, last_activity=datetime.now() - timedelta(hours=1))

    asyncio.run(main.hook_work_action_callback("ask_user_question_answered", session_id=SESSION_ID))

    with sqlite3.connect(supervised_env.db_path) as conn:
        row = conn.execute(
            "SELECT human_anchored_at, human_anchor_source FROM instances WHERE id = ?",
            (SESSION_ID,),
        ).fetchone()
        assert row[0] is not None
        assert row[1] == "ask_user_question_answered"
        # Simulate >20 minutes with no further input; the AUQ run anchor must still hold.
        conn.execute(
            "UPDATE instances SET last_activity = datetime('now', '-1 hour') WHERE id = ?",
            (SESSION_ID,),
        )
        conn.execute("UPDATE events SET created_at = datetime('now', '-30 minutes')")
        conn.commit()

    ws = asyncio.run(main.compute_work_state())
    assert ws.productivity_active is True
    assert ws.productivity_hold == "human_anchor"
    assert ws.human_anchored_instance_count == 1
    assert ws.idle_timeout_exempt is True

    async def _stop():
        import aiosqlite

        from instance_mutation import sanctioned_update_instance

        async with aiosqlite.connect(supervised_env.db_path) as db:
            await sanctioned_update_instance(
                db,
                instance_id=SESSION_ID,
                updates={"status": "stopped"},
                mutation_type="instance_stopped",
                write_source="test",
                actor="test-stop",
            )
            await db.commit()

    asyncio.run(_stop())
    with sqlite3.connect(supervised_env.db_path) as conn:
        row = conn.execute(
            "SELECT human_anchored_at, human_anchor_source FROM instances WHERE id = ?",
            (SESSION_ID,),
        ).fetchone()
    assert row == (None, None)


def test_stop_hook_clears_human_anchor(supervised_env):
    _insert_instance(supervised_env.db_path, last_activity=datetime.now())
    with sqlite3.connect(supervised_env.db_path) as conn:
        conn.execute(
            """
            UPDATE instances
               SET human_anchored_at = datetime('now'),
                   human_anchor_source = 'ask_user_question_answered'
             WHERE id = ?
            """,
            (SESSION_ID,),
        )
        conn.commit()

    sys.modules["stop_hook"].clear_human_anchor_on_stop(SESSION_ID)

    with sqlite3.connect(supervised_env.db_path) as conn:
        row = conn.execute(
            "SELECT human_anchored_at, human_anchor_source FROM instances WHERE id = ?",
            (SESSION_ID,),
        ).fetchone()
    assert row == (None, None)
