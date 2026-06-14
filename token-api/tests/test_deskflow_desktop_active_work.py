"""Deskflow KVM presence is auto active-process work evidence for the timer.

The Mac deskflow-client supervisor reports that the deskflow client is connected
(the Emperor is at his desk). token-api was missing this feed entirely: with no
local agent pane, no tmux typing, and no explicit work_action, genuine desk work
fell through compute_work_state to productivity_hold='none' → the timer went
IDLE → IDLE_BREAK (no amnesty), logging idle_break all day at 0h work.

These tests pin the restored behaviour:
  (a) a FRESH deskflow-active heartbeat sets productivity_active=True with
      productivity_hold='active_process', and the timer settles on WORKING (never
      IDLE_BREAK) when fed that productivity;
  (b) the signal is freshness-bounded (a stale/cleared heartbeat does not anchor
      work) and COMPLEMENTS — never suppresses — the explicit work-action model;
  (c) the POST /api/desktop/deskflow feed receiver updates DESKTOP_STATE so the
      supervisor's heartbeat actually reaches the work model.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(app_env, monkeypatch):
    """app_env with no agent panes and no human typing — desk work with nothing
    but the deskflow signal to anchor it."""
    main = app_env.main

    async def _no_pane_rows():
        return []

    async def _no_engine_by_tty():
        return {}

    monkeypatch.setattr(main, "_tmux_pane_rows", _no_pane_rows)
    monkeypatch.setattr(main, "_agent_engine_by_tty", _no_engine_by_tty)
    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)
    return app_env


def _set_deskflow(main, *, active: bool, last_seen_age_s: float | None = 0.0) -> None:
    main.DESKTOP_STATE["deskflow_active"] = active
    if last_seen_age_s is None:
        main.DESKTOP_STATE["deskflow_last_seen"] = None
    else:
        ts = datetime.now() - timedelta(seconds=last_seen_age_s)
        main.DESKTOP_STATE["deskflow_last_seen"] = ts.isoformat()


# ── (a) fresh deskflow active anchors work as active_process ────────────────────


async def test_fresh_deskflow_active_holds_open_as_active_process(env):
    _set_deskflow(env.main, active=True, last_seen_age_s=1)
    ws = await env.main.compute_work_state()
    assert ws.productivity_active is True
    assert ws.productivity_hold == "active_process"
    assert ws.reason == "deskflow_desktop_active"


async def test_fresh_deskflow_active_keeps_timer_off_idle_break(env):
    """The whole point: real desk work must not log idle_break.

    Drive the timer the way the worker does — set_productivity from the computed
    work_state — and confirm it settles on WORKING, never IDLE/IDLE_BREAK.
    """
    _set_deskflow(env.main, active=True, last_seen_age_s=1)
    ws = await env.main.compute_work_state()

    engine = env.main.timer_engine
    engine.set_activity(env.main.Activity.WORKING, is_scrolling_gaming=False, now_mono_ms=0)
    engine.set_productivity(ws.productivity_active, 1000)

    assert ws.productivity_active is True
    assert engine.effective_mode == env.main.TimerMode.WORKING
    assert engine.effective_mode != env.main.TimerMode.IDLE_BREAK


# ── (b) freshness-bounded + complements explicit work-action ────────────────────


async def test_stale_deskflow_heartbeat_does_not_anchor_work(env):
    # Older than DESKFLOW_ACTIVE_TTL_SECONDS → treated as absent.
    _set_deskflow(env.main, active=True, last_seen_age_s=env.main.DESKFLOW_ACTIVE_TTL_SECONDS + 30)
    ws = await env.main.compute_work_state()
    assert ws.productivity_active is False
    assert ws.productivity_hold == "none"


async def test_cleared_deskflow_does_not_anchor_work(env):
    _set_deskflow(env.main, active=False, last_seen_age_s=1)
    ws = await env.main.compute_work_state()
    assert ws.productivity_active is False
    assert ws.productivity_hold == "none"


async def test_explicit_work_action_still_wins_its_own_hold(env):
    """Auto-detection complements, not replaces: an explicit work_action keeps its
    work_action_buffer hold even while deskflow is also active."""
    _set_deskflow(env.main, active=True, last_seen_age_s=1)
    with __import__("sqlite3").connect(env.db_path) as conn:
        conn.execute(
            "INSERT INTO events (event_type, device_id, details) VALUES ('work_action', 't', ?)",
            ('{"source": "stream-deck", "note": "deck-button"}',),
        )
        conn.commit()
    ws = await env.main.compute_work_state()
    assert ws.productivity_active is True
    assert ws.productivity_hold == "work_action_buffer"
    assert ws.work_action_source == "stream-deck"


# ── (c) the POST feed receiver wires the supervisor heartbeat into the model ─────


def test_deskflow_endpoint_sets_active_state(env):
    client = TestClient(env.main.app)
    resp = client.post("/api/desktop/deskflow", json={"active": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deskflow_active"] is True
    assert body["deskflow_last_seen"]
    assert body["ttl_seconds"] == env.main.DESKFLOW_ACTIVE_TTL_SECONDS
    assert env.main.DESKTOP_STATE["deskflow_active"] is True


def test_deskflow_endpoint_clears_active_state(env):
    client = TestClient(env.main.app)
    client.post("/api/desktop/deskflow", json={"active": True})
    resp = client.post("/api/desktop/deskflow", json={"active": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["deskflow_active"] is False
    assert env.main.DESKTOP_STATE["deskflow_active"] is False


async def test_endpoint_then_work_state_anchors_work(env):
    """End-to-end feed contract: POST active → compute_work_state anchors work."""
    client = TestClient(env.main.app)
    client.post("/api/desktop/deskflow", json={"active": True})
    ws = await env.main.compute_work_state()
    assert ws.productivity_active is True
    assert ws.productivity_hold == "active_process"
