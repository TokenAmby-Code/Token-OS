"""Tests for Custodes state-event ingestion."""

import json
import os
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest


_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db.close()
os.environ["TOKEN_API_DB"] = _test_db.name

import main
from init_db import init_database


@pytest.fixture(autouse=True)
def _init_db():
    if Path(_test_db.name).exists():
        Path(_test_db.name).unlink()
    init_database()
    main._custodes_state_debounce.clear()
    yield
    main._custodes_state_debounce.clear()
    if Path(_test_db.name).exists():
        Path(_test_db.name).unlink()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def _insert_instance(*, legion="custodes", synced=1, status="idle", tmux_pane="%5"):
    iid = str(uuid.uuid4())
    conn = sqlite3.connect(_test_db.name)
    now = "2026-04-25T12:00:00"
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, tmux_pane, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, ?, ?, ?, ?)""",
        (iid, str(uuid.uuid4()), "custodes-test", "/tmp", status, legion, synced, tmux_pane, now, now),
    )
    conn.commit()
    conn.close()
    return iid


def _events(event_type):
    conn = sqlite3.connect(_test_db.name)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM events WHERE event_type = ? ORDER BY id ASC",
        (event_type,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_no_live_custodes_logs_event_but_does_not_dispatch(client):
    resp = client.post(
        "/api/custodes/state-event",
        json={"event_type": "idle_timeout", "source": "timer_worker"},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert data["intervention_dispatched"] is False
    assert data["reason"] == "no_live_custodes_singleton"
    assert len(_events("custodes_state_event")) == 1
    assert len(_events("custodes_intervention")) == 1


def test_live_custodes_dispatches_once(client, monkeypatch):
    _insert_instance()
    calls = []

    async def fake_dispatch(prompt):
        calls.append(prompt)
        return {"dispatched": True, "reason": "dispatched", "instance_id": "custodes-1"}

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fake_dispatch)

    resp = client.post(
        "/api/custodes/state-event",
        json={
            "event_type": "idle_timeout",
            "source": "timer_worker",
            "payload": {"phone_app": "slay_the_spire"},
        },
    )

    assert resp.status_code == 200
    assert resp.json()["intervention_dispatched"] is True
    assert len(calls) == 1
    assert "State hook: idle_timeout." in calls[0]


def test_duplicate_event_within_debounce_is_suppressed(client, monkeypatch):
    _insert_instance()
    calls = []

    async def fake_dispatch(prompt):
        calls.append(prompt)
        return {"dispatched": True, "reason": "dispatched", "instance_id": "custodes-1"}

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fake_dispatch)
    body = {
        "event_type": "phone_distraction_blocked",
        "source": "phone",
        "severity": 2,
        "payload": {"app": "youtube"},
    }

    first = client.post("/api/custodes/state-event", json=body)
    second = client.post("/api/custodes/state-event", json=body)

    assert first.json()["intervention_dispatched"] is True
    assert second.json()["intervention_dispatched"] is False
    assert second.json()["reason"] == "memory_debounce"
    assert len(calls) == 1


def test_duplicate_event_log_suppresses_after_memory_clear(client, monkeypatch):
    _insert_instance()
    calls = []

    async def fake_dispatch(prompt):
        calls.append(prompt)
        return {"dispatched": True, "reason": "dispatched", "instance_id": "custodes-1"}

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fake_dispatch)
    body = {
        "event_type": "desktop_mode_blocked",
        "source": "desktop",
        "severity": 2,
        "payload": {"desktop_mode": "video"},
    }

    first = client.post("/api/custodes/state-event", json=body)
    main._custodes_state_debounce.clear()
    second = client.post("/api/custodes/state-event", json=body)

    assert first.json()["intervention_dispatched"] is True
    assert second.json()["intervention_dispatched"] is False
    assert second.json()["reason"] == "event_log_debounce"
    assert len(calls) == 1


def test_higher_severity_repeat_bypasses_debounce(client, monkeypatch):
    _insert_instance()
    calls = []

    async def fake_dispatch(prompt):
        calls.append(prompt)
        return {"dispatched": True, "reason": "dispatched", "instance_id": "custodes-1"}

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fake_dispatch)
    base = {
        "event_type": "enforcement_cascade_started",
        "source": "phone",
        "payload": {"app": "youtube"},
    }

    low = client.post("/api/custodes/state-event", json={**base, "severity": 2})
    high = client.post("/api/custodes/state-event", json={**base, "severity": 3})

    assert low.json()["intervention_dispatched"] is True
    assert high.json()["intervention_dispatched"] is True
    assert len(calls) == 2
    details = [json.loads(event["details"]) for event in _events("custodes_intervention")]
    assert [detail["severity"] for detail in details] == [2, 3]
