"""Tests for Custodes state-event ingestion."""

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

main = None
_test_db_path: Path | None = None


@pytest.fixture(autouse=True)
def _init_db(app_env):
    global main, _test_db_path
    main = app_env.main
    _test_db_path = app_env.db_path
    main._custodes_state_debounce.clear()
    yield
    main._custodes_state_debounce.clear()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def _db_path() -> Path:
    assert _test_db_path is not None
    return _test_db_path


def _insert_instance(*, legion="custodes", synced=1, status="idle", tmux_pane="%5"):
    iid = str(uuid.uuid4())
    conn = sqlite3.connect(_db_path())
    now = "2026-04-25T12:00:00"
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, tmux_pane, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, ?, ?, ?, ?)""",
        (
            iid,
            str(uuid.uuid4()),
            "custodes-test",
            "/tmp",
            status,
            legion,
            synced,
            tmux_pane,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()
    return iid


def _events(event_type):
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM events WHERE event_type = ? ORDER BY id ASC",
        (event_type,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def test_no_live_custodes_launches_replacement(client, monkeypatch):
    launches = []

    async def fake_find():
        return None

    async def fake_launch(prompt):
        launches.append(prompt)
        return {"dispatched": True, "reason": "launched_new_custodes", "tmux_pane": "%9"}

    monkeypatch.setattr(main, "_find_custodes_tmux_pane", fake_find)
    monkeypatch.setattr(main, "_launch_custodes_for_intervention", fake_launch)

    # Enforcement hook (phone_distraction_blocked) still escalates to Custodes.
    resp = client.post(
        "/api/custodes/state-event",
        json={
            "event_type": "phone_distraction_blocked",
            "source": "phone",
            "payload": {"app": "youtube"},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert data["intervention_dispatched"] is True
    assert data["reason"] == "launched_new_custodes"
    assert len(launches) == 1
    assert len(_events("custodes_state_event")) == 1
    assert len(_events("custodes_intervention")) == 1


def test_db_miss_recovers_visible_custodes_tmux_pane(client, monkeypatch):
    injections = []

    async def fake_find():
        return "%310"

    async def fake_inject(prompt, tmux_pane, *, instance_id=None):
        injections.append((prompt, tmux_pane, instance_id))
        return {
            "dispatched": True,
            "reason": "dispatched",
            "tmux_pane": tmux_pane,
            "instance_id": instance_id,
        }

    async def fake_launch(prompt):
        raise AssertionError("should recover pane before launching")

    monkeypatch.setattr(main, "_find_custodes_tmux_pane", fake_find)
    monkeypatch.setattr(main, "_inject_custodes_prompt_to_pane", fake_inject)
    monkeypatch.setattr(main, "_launch_custodes_for_intervention", fake_launch)

    # Enforcement hook recovers + injects into the live Custodes pane.
    resp = client.post(
        "/api/custodes/state-event",
        json={
            "event_type": "desktop_mode_blocked",
            "source": "desktop",
            "payload": {"desktop_mode": "video"},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["intervention_dispatched"] is True
    assert data["reason"] == "recovered_tmux_pane"
    assert injections[0][1] == "%310"


def test_enforcement_dispatches_behavioral_prompt_once(client, monkeypatch):
    _insert_instance()
    calls = []

    async def fake_dispatch(prompt):
        calls.append(prompt)
        return {"dispatched": True, "reason": "dispatched", "instance_id": "custodes-1"}

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fake_dispatch)

    resp = client.post(
        "/api/custodes/state-event",
        json={
            "event_type": "phone_distraction_blocked",
            "source": "phone",
            "payload": {"app": "slay_the_spire"},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["intervention_dispatched"] is True
    assert data["routed_to"] == "custodes"
    assert data["classification"] == "enforcement"
    assert len(calls) == 1
    # Custodes gets the behavioral directive, NOT the observed-state metadata.
    assert "Enforcement hook: phone_distraction_blocked." in calls[0]
    assert "Observed" not in calls[0]
    assert "phone_app=" not in calls[0]


def test_state_event_routes_to_administratum_not_custodes(client, monkeypatch):
    _insert_instance()

    async def fail_dispatch(prompt):
        raise AssertionError("state hook must not reach Custodes")

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fail_dispatch)

    resp = client.post(
        "/api/custodes/state-event",
        json={
            "event_type": "idle_timeout",
            "source": "timer_worker",
            "payload": {"phone_app": "slay_the_spire"},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["intervention_dispatched"] is False
    assert data["routed_to"] == "administratum"
    assert data["classification"] == "state"
    assert data["reason"] == "routed_to_administratum"
    # No live recorder pane in the test DB → record is a no-op, not an error.
    assert data["administratum_delivery"]["reason"] == "no_administratum_pane"
    # State events are logged under administratum_record, never custodes_intervention.
    assert len(_events("administratum_record")) == 1
    assert len(_events("custodes_intervention")) == 0


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


@pytest.mark.asyncio
async def test_expected_ack_intervention_cancels_if_ack_resolved_during_dispatch(monkeypatch):
    """Regression: app close/negative-edge after state event must cancel queued delivery."""
    ack_id = "race-ack"
    conn = sqlite3.connect(_db_path())
    now = "2026-05-10T08:09:16"
    conn.execute(
        """INSERT INTO expected_acknowledgements
           (id, source, instance_id, reason, status, created_at,
            ack_due_at, level2_due_at, pavlok_due_at, fired_levels_json, details_json)
           VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
        (
            ack_id,
            "phone_gaming",
            "phone_gaming:phone:slay_the_spire",
            "Phone gaming during work: Slay the Spire",
            now,
            now,
            now,
            now,
            json.dumps([1, 2]),
            json.dumps({"app": "slay_the_spire", "display_name": "Slay the Spire"}),
        ),
    )
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, tmux_pane, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', 'idle', 'custodes', 1, NULL, ?, ?)""",
        (
            "custodes-race",
            str(uuid.uuid4()),
            "custodes-test",
            "/tmp",
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()

    async def fake_find_custodes_pane():
        await main._resolve_expected_ack(
            ack_id=ack_id,
            source=None,
            instance_id=None,
            status="acknowledged",
        )
        return "%99"

    async def fail_inject(prompt, tmux_pane, *, instance_id=None):
        raise AssertionError("stale intervention reached pane injection after ack was resolved")

    monkeypatch.setattr(main, "_find_custodes_tmux_pane", fake_find_custodes_pane)
    monkeypatch.setattr(main, "_inject_custodes_prompt_to_pane", fail_inject)
    monkeypatch.setattr(
        main,
        "send_pavlok_stimulus",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale intervention reached Pavlok")
        ),
    )

    result = await main.handle_custodes_state_event(
        "expected_ack_escalated",
        "phone_gaming",
        instance_id="phone_gaming:phone:slay_the_spire",
        severity=5,
        payload={
            "ack_id": ack_id,
            "level": 3,
            "reason": "Phone gaming during work: Slay the Spire",
            "app": "slay_the_spire",
        },
    )

    assert result["intervention_dispatched"] is False
    assert result["reason"] == "intervention_canceled_by_negative_edge"
    assert _events("custodes_intervention") == []
    canceled = _events("intervention_canceled_by_negative_edge")
    assert len(canceled) == 1
    details = json.loads(canceled[0]["details"])
    assert details["ack_id"] == ack_id
    assert details["ack_status"] == "acknowledged"
    assert details["stage"] == "pre_pane_inject"


@pytest.mark.asyncio
async def test_snapshot_counts_custodes_cascade_state_events():
    conn = sqlite3.connect(_db_path())
    conn.execute(
        "INSERT INTO events (event_type, device_id, details) VALUES (?, ?, ?)",
        (
            "custodes_state_event",
            "askq_ladder",
            json.dumps(
                {
                    "event_type": "enforcement_cascade_started",
                    "source": "askq_ladder",
                    "payload": {"ack_source": "askuserquestion"},
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    assert (await main._custodes_state_snapshot())["cascade_count_today"] == 1

    conn = sqlite3.connect(_db_path())
    conn.execute(
        "INSERT INTO events (event_type, device_id, details) VALUES (?, ?, ?)",
        (
            "custodes_state_event",
            "golden_throne",
            json.dumps(
                {
                    "event_type": "enforcement_cascade_started",
                    "source": "golden_throne",
                    "payload": {"ack_source": "golden_throne"},
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    assert (await main._custodes_state_snapshot())["cascade_count_today"] == 2
