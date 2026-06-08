"""compute_work_state discounts automated tmuxctl injections from productivity.

An automated injection (state-hook fanout / dispatch / enforcement) wakes an
agent pane, which bumps the instance's last_activity AND fires a prompt_submit
work_action. Both signals fed compute_work_state, reviving productivity_active so
the IDLE→BREAK clock kept resetting and never matured. The send gate now stamps a
per-pane automated-activation marker; these tests pin that compute_work_state
discounts a marked pane's reflex activity across BOTH vectors (instance/observed
loops and the work_action read), while a real human keystroke, an interleaved
human work_action, an expired marker, or activity that pre-dates the injection all
still anchor WORKING.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

import pytest

PANE = "%500"
SESSION_ID = "sess-1"


def _insert_instance(db_path) -> None:
    """Insert one local mechanicus agent instance, filling every NOT-NULL column."""
    with sqlite3.connect(db_path) as conn:
        cols = conn.execute("PRAGMA table_info(claude_instances)").fetchall()
        values: dict[str, Any] = {}
        for _cid, name, ctype, notnull, dflt, _pk in cols:
            if dflt is not None or not notnull:
                continue
            values[name] = 0 if ctype.upper() in ("INTEGER", "REAL") else "x"
        values.update(
            id=SESSION_ID,
            session_id=SESSION_ID,
            tab_name="admin",
            status="processing",
            engine="claude",
            working_dir="/work",
            tmux_pane=PANE,
            device_id="Mac-Mini",
            last_activity=datetime.now().isoformat(),
            legion="mechanicus",
            is_subagent=0,
        )
        keys = list(values)
        conn.execute(
            f"INSERT INTO claude_instances ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
            [values[k] for k in keys],
        )
        conn.commit()


def _set_last_activity(db_path, seconds_ago: float) -> None:
    ts = (datetime.now() - timedelta(seconds=seconds_ago)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE claude_instances SET last_activity = ? WHERE id = ?", (ts, SESSION_ID))
        conn.commit()


def _set_marker(db_path, injected_offset_s: float, ttl_s: float) -> None:
    injected = datetime.now() + timedelta(seconds=injected_offset_s)
    expires = injected + timedelta(seconds=ttl_s)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM automated_pane_activity")
        conn.execute(
            "INSERT INTO automated_pane_activity (tmux_pane, injected_at, expires_at, source, verb)"
            " VALUES (?, ?, ?, ?, ?)",
            (PANE, injected.isoformat(), expires.isoformat(), "test", "send-keys"),
        )
        conn.commit()


def _add_work_action(db_path, note: str, source: str = "prompt_submit") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO events (event_type, device_id, details) VALUES ('work_action', 't', ?)",
            (json.dumps({"source": source, "note": note}),),
        )
        conn.commit()


def _clear_events(db_path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM events")
        conn.commit()


@pytest.fixture
def work_state_env(app_env, monkeypatch):
    """app_env wired so PANE reads as a live local agent pane."""
    main = app_env.main

    async def _pane_rows():
        return [(PANE, "claude", "/work", "win", "/dev/ttys9")]

    async def _engine_by_tty():
        return {}

    monkeypatch.setattr(main, "_tmux_pane_rows", _pane_rows)
    monkeypatch.setattr(main, "_agent_engine_by_tty", _engine_by_tty)
    monkeypatch.setattr(main, "_pane_is_agent_from_snapshot", lambda c, t, m: (True, "claude"))
    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)
    _insert_instance(app_env.db_path)
    return app_env


async def test_no_marker_anchors_work(work_state_env):
    ws = await work_state_env.main.compute_work_state()
    assert ws.productivity_active is True
    assert ws.active_instance_count == 1


async def test_marker_discounts_both_vectors(work_state_env):
    # Live marker injected just before the reflex activity, no human typing, and a
    # prompt_submit work_action for this session — the canonical mechanicus loop.
    _set_marker(work_state_env.db_path, injected_offset_s=-1, ttl_s=90)
    _add_work_action(work_state_env.db_path, f"session_id={SESSION_ID}")
    ws = await work_state_env.main.compute_work_state()
    assert ws.productivity_active is False, "automated reflex wake must not anchor work"
    assert ws.active_instance_count == 0
    assert ws.observed_agent_count == 0  # the observed-loop vector must also be discounted
    assert ws.work_action_source is None  # the work_action vector must also be discounted


async def test_interleaved_human_work_action_still_anchors(work_state_env):
    _set_marker(work_state_env.db_path, injected_offset_s=-1, ttl_s=90)
    _add_work_action(work_state_env.db_path, f"session_id={SESSION_ID}")  # discounted automated
    _add_work_action(
        work_state_env.db_path, f"session_id={SESSION_ID}", source="tmux-typing-guard"
    )  # human
    ws = await work_state_env.main.compute_work_state()
    assert ws.productivity_active is True
    assert ws.work_action_source == "tmux-typing-guard"


async def test_human_typing_overrides_discount(work_state_env, monkeypatch):
    monkeypatch.setattr(work_state_env.main, "_typing_guard_active", lambda: True)
    _set_marker(work_state_env.db_path, injected_offset_s=-1, ttl_s=90)
    ws = await work_state_env.main.compute_work_state()
    assert ws.productivity_active is True
    assert ws.active_instance_count == 1


async def test_expired_marker_is_ignored(work_state_env):
    # injected 200s ago with a 90s TTL → expired → never loaded → no discount.
    _set_marker(work_state_env.db_path, injected_offset_s=-200, ttl_s=90)
    ws = await work_state_env.main.compute_work_state()
    assert ws.productivity_active is True
    assert ws.active_instance_count == 1


async def test_activity_predating_injection_is_not_discounted(work_state_env):
    # The >= injected_at guard: a pane working 30s before a 1s-old injection keeps
    # its real activity; only activity attributable to the injection is dropped.
    _set_last_activity(work_state_env.db_path, seconds_ago=30)
    _set_marker(work_state_env.db_path, injected_offset_s=-1, ttl_s=90)
    ws = await work_state_env.main.compute_work_state()
    assert ws.productivity_active is True
    assert ws.active_instance_count == 1
