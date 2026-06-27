"""Enforcement-hook → Custodes routing leaks (L1/L2 + observability + D5).

Pins the dispatch-brief contract for `Mars/Sessions/enforcement-hook-custodes-routing`:

  * L1 — `phone_distraction_enforce` is a recognized ENFORCEMENT trigger and
    routes to Custodes (it used to drop as `no_policy_match`).
  * L2 — a Custodes-bound enforcement whose pane write is suppressed by the
    typing-guard send gate is HELD-AND-QUEUED (reason `deferred`), then flushed
    when typing stops; a stale defer is dropped deliberately with a logged
    `stale_after_typing` reason. "We don't lose, we stall."
  * Observability — every drop logs an explicit, distinct reason in `events`.
  * D5 — pure state-class hooks route to Administratum only, never Custodes.
"""

from __future__ import annotations

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
    main._custodes_enforcement_defer_queue.clear()
    # Keep tests deterministic: never let wall-clock quiet hours gate dispatch.
    yield
    main._custodes_state_debounce.clear()
    main._custodes_enforcement_defer_queue.clear()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    return TestClient(main.app)


def _db_path() -> Path:
    assert _test_db_path is not None
    return _test_db_path


def _insert_instance(*, legion="custodes", synced=1, status="idle"):
    iid = str(uuid.uuid4())
    conn = sqlite3.connect(_db_path())
    now = "2026-05-31T12:00:00"
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, legion, synced, registered_at, last_activity)
           VALUES (?, ?, ?, ?, 'local', 'Mac-Mini', ?, ?, ?, ?, ?)""",
        (
            iid,
            str(uuid.uuid4()),
            "custodes-test",
            "/tmp",
            status,
            legion,
            synced,
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
        "SELECT * FROM events WHERE event_type = ? ORDER BY id ASC", (event_type,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ── (a) L1: phone_distraction_enforce classifies enforcement + routes Custodes ─


def test_phone_distraction_enforce_routes_to_custodes(client, monkeypatch):
    _insert_instance()
    calls = []

    async def fake_dispatch(prompt):
        calls.append(prompt)
        return {"dispatched": True, "reason": "dispatched", "instance_id": "custodes-1"}

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fake_dispatch)
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)

    resp = client.post(
        "/api/custodes/state-event",
        json={
            "event_type": "phone_distraction_enforce",
            "source": "phone",
            "severity": 4,
            "payload": {"phone_app": "youtube", "app": "youtube", "level": 2},
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["reason"] != "no_policy_match"
    assert data["classification"] == "enforcement"
    assert data["routed_to"] == "custodes"
    assert data["intervention_dispatched"] is True
    assert len(calls) == 1
    assert "Enforcement hook: phone_distraction_enforce." in calls[0]


# ── (b) L2: typing_guard hold-and-queue, flush, staleness re-check ─────────────


def _gated_dispatch_result():
    return {
        "dispatched": False,
        "gated": True,
        "gate_reason": "typing_guard",
        "reason": "send_gated:typing_guard",
    }


def test_enforcement_under_typing_guard_is_deferred_not_dropped(client, monkeypatch):
    _insert_instance()

    async def gated_dispatch(prompt):
        return _gated_dispatch_result()

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", gated_dispatch)
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)

    resp = client.post(
        "/api/custodes/state-event",
        json={
            "event_type": "phone_distraction_blocked",
            "source": "phone",
            "severity": 4,
            "payload": {"app": "DIAGNOSTIC-TEST", "phone_app": "DIAGNOSTIC-TEST"},
        },
    )

    data = resp.json()
    # Held-and-queued, NOT dropped as send_text_failed.
    assert data["reason"] == "deferred"
    assert data["intervention_dispatched"] is False
    assert len(main._custodes_enforcement_defer_queue) == 1
    # The deferral is observable in the event log with an explicit reason.
    interventions = _events("custodes_intervention")
    assert any(
        json.loads(e["details"]).get("delivery", {}).get("reason") == "deferred"
        for e in interventions
    )


async def test_deferred_enforcement_flushes_when_typing_stops(monkeypatch):
    _insert_instance()
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)

    gated = {"calls": 0}

    async def dispatch(prompt):
        gated["calls"] += 1
        if gated["calls"] == 1:
            return _gated_dispatch_result()
        return {"dispatched": True, "reason": "dispatched", "instance_id": "custodes-1"}

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", dispatch)

    # First delivery is gated → deferred.
    result = await main.handle_custodes_state_event(
        "phone_distraction_blocked",
        "phone",
        severity=4,
        payload={"app": "youtube", "phone_app": "youtube"},
    )
    assert result["reason"] == "deferred"
    assert len(main._custodes_enforcement_defer_queue) == 1

    # Typing has stopped, and the enforcement is still warranted: it flushes.
    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)

    async def warranted(event, intervention):
        return True, "warranted"

    monkeypatch.setattr(main, "_enforcement_still_warranted", warranted)

    # Typing stops → the still-queued item flushes to Custodes on the next drain.
    flushed = await main._custodes_enforcement_defer_flush_once()

    assert len(main._custodes_enforcement_defer_queue) == 0
    assert any(r.get("dispatched") for r in flushed)
    dispatched_logs = [
        json.loads(e["details"])
        for e in _events("custodes_intervention")
        if json.loads(e["details"]).get("delivery", {}).get("dispatched")
    ]
    assert dispatched_logs, "flushed delivery must be logged as dispatched"


async def test_stale_deferred_enforcement_is_dropped_deliberately(monkeypatch):
    _insert_instance()
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)

    async def gated_dispatch(prompt):
        return _gated_dispatch_result()

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", gated_dispatch)

    result = await main.handle_custodes_state_event(
        "phone_distraction_blocked",
        "phone",
        severity=4,
        payload={"app": "youtube", "phone_app": "youtube"},
    )
    assert result["reason"] == "deferred"
    assert len(main._custodes_enforcement_defer_queue) == 1

    # Typing has stopped so the item is eligible to flush...
    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)

    # ...but the work signal made it moot (typing IS the appeal): drop, do not fire.
    async def moot(event, intervention):
        return False, "phone_distraction_cleared"

    monkeypatch.setattr(main, "_enforcement_still_warranted", moot)

    async def fail_dispatch(prompt):
        raise AssertionError("stale enforcement must not reach Custodes")

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fail_dispatch)

    flushed = await main._custodes_enforcement_defer_flush_once()

    assert len(main._custodes_enforcement_defer_queue) == 0
    assert any(r.get("reason") == "stale_after_typing" for r in flushed)
    drops = [
        json.loads(e["details"])
        for e in _events("custodes_intervention")
        if json.loads(e["details"]).get("delivery", {}).get("reason") == "stale_after_typing"
    ]
    assert drops, "deliberate stale drop must be logged with reason stale_after_typing"


# ── (c) Observability: every drop logs an explicit, distinct reason ───────────


def test_dedupe_drop_logs_explicit_reason(client, monkeypatch):
    _insert_instance()
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)

    async def fake_dispatch(prompt):
        return {"dispatched": True, "reason": "dispatched", "instance_id": "custodes-1"}

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fake_dispatch)
    body = {
        "event_type": "phone_distraction_blocked",
        "source": "phone",
        "severity": 2,
        "payload": {"app": "youtube", "phone_app": "youtube"},
    }

    first = client.post("/api/custodes/state-event", json=body)
    second = client.post("/api/custodes/state-event", json=body)

    assert first.json()["intervention_dispatched"] is True
    assert second.json()["reason"] == "memory_debounce"
    # The suppressed (dropped) second event must leave an explicit drop record —
    # leaks must be visible in the log, not inferred from the absence of a send.
    drops = _events("custodes_intervention_dropped")
    assert any(json.loads(e["details"]).get("reason") == "memory_debounce" for e in drops)


# ── (d) D5 regression: state-class hooks route to Administratum only ───────────


@pytest.mark.parametrize(
    "event_type,payload",
    [
        ("idle_timeout", {"timer_mode": "break"}),
        ("distraction_timeout", {"timer_mode": "distracted"}),
        ("break_exhausted", {"break_balance_ms": -60000}),
    ],
)
def test_state_class_hook_routes_administratum_only(client, monkeypatch, event_type, payload):
    _insert_instance()

    async def fail_dispatch(prompt):
        raise AssertionError(f"state hook {event_type} must not reach Custodes")

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fail_dispatch)
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)

    resp = client.post(
        "/api/custodes/state-event",
        json={
            "event_type": event_type,
            "source": "timer_worker",
            "severity": 2,
            "payload": payload,
        },
    )

    data = resp.json()
    assert data["routed_to"] == "administratum"
    assert data["classification"] == "state"
    assert data["intervention_dispatched"] is False
    assert len(_events("custodes_intervention")) == 0


# ── (e) TTS languishing is internal-only, never paged ───────────────────────
#
# D2 freeze (2026-06-27): ``tts_queue_languishing`` can remain a recognized
# diagnostic label, including depth-sensitive keys for observability, but it is
# detached from the Custodes/Administratum paging path. Older emitters may still
# declare event_class="enforcement"; the router must ignore that declaration for
# this label and log it internally only.


def _wire_admin_recorder(monkeypatch):
    """Stub a live Administratum recorder pane and count its pane injections.

    The Custodes path is mocked separately, so `_inject_custodes_prompt_to_pane`
    is reached only by the Administratum record sink — its call count is the
    number of Admin record deliveries. Fake pane id + no-op log so the test never
    touches live tmux or the vault (hook-tests-no-live-tmux).
    """
    admin_injects: list[str] = []

    async def fake_resolve():
        return {"id": "administratum-rec", "tmux_pane": "%fake-admin", "tab_name": "administratum"}

    async def fake_inject(prompt, tmux_pane, *, instance_id=None, cancel_check=None):
        admin_injects.append(prompt)
        return {"dispatched": True, "reason": "dispatched", "instance_id": instance_id}

    async def fake_log(*a, **k):
        return None

    async def fake_snapshot():
        return {
            "open_panes": 9,
            "active_threads": {"count": 4},
            "timer": {"break_balance_ms": 3300000},
        }

    monkeypatch.setattr(main, "_resolve_administratum_instance", fake_resolve)
    monkeypatch.setattr(main, "_inject_custodes_prompt_to_pane", fake_inject)
    monkeypatch.setattr(main, "_append_administratum_log", fake_log)
    monkeypatch.setattr(main, "_custodes_state_snapshot", fake_snapshot)
    monkeypatch.setattr(main, "is_quiet_hours", lambda *a, **k: False)
    return admin_injects


def _languishing_payload(pause_queue_length):
    return {
        "app": "tts_queue",
        "queue": "pause",
        "pause_queue_length": pause_queue_length,
        "threshold": 5,
    }


async def _set_live_tts_pause_queue(length: int) -> None:
    """Seed the live TTS source-of-truth for genuine languishing hook tests."""
    from routes import tts

    async with tts.tts_queue_lock:
        tts.pause_queue.clear()
        for n in range(length):
            tts.pause_queue.append(
                tts.TTSQueueItem(
                    instance_id=f"tts-{n}",
                    message=f"queued {n}",
                    voice="Daniel",
                    sound="none",
                    tab_name=f"tts-tab-{n}",
                    queue_target="pause",
                )
            )


async def _clear_live_tts_pause_queue() -> None:
    from routes import tts

    async with tts.tts_queue_lock:
        tts.pause_queue.clear()


def test_languishing_dedupe_key_distinguishes_depth():
    """The diagnostic key keeps depth so internal records remain distinguishable."""
    from custodes_state_policy import StateEvent, build_dedupe_key

    def key(depth):
        return build_dedupe_key(
            StateEvent(
                event_type="tts_queue_languishing",
                source="tts_queue",
                payload=_languishing_payload(depth),
            )
        )

    assert key(6) != key(7) != key(8)
    assert key(6) != "tts_queue_languishing:tts_queue:tts_queue"
    assert key(6).endswith(":len=6")


async def test_languishing_declared_enforcement_routes_internal_only(monkeypatch) -> None:
    """Even if declared as enforcement, languishing is internal/log-only."""
    admin_injects = _wire_admin_recorder(monkeypatch)

    async def fail_custodes_dispatch(prompt):  # pragma: no cover - assertion path
        raise AssertionError("tts_queue_languishing must not reach Custodes")

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fail_custodes_dispatch)

    try:
        await _set_live_tts_pause_queue(6)
        result = await main.handle_custodes_state_event(
            "tts_queue_languishing",
            "tts_queue",
            severity=3,
            payload=_languishing_payload(6),
            event_class="enforcement",
        )
    finally:
        await _clear_live_tts_pause_queue()

    assert result["intervention_dispatched"] is False
    assert result["classification"] == "internal"
    assert result["routed_to"] == "internal"
    assert result["reason"] == "internal_label_only"
    assert len(admin_injects) == 0
    assert len(main._custodes_enforcement_defer_queue) == 0


async def test_persistent_languishing_records_without_escalating(monkeypatch) -> None:
    """A worsening queue can keep internal records without paging anyone."""
    admin_injects = _wire_admin_recorder(monkeypatch)

    async def fail_custodes_dispatch(prompt):  # pragma: no cover - assertion path
        raise AssertionError("tts_queue_languishing must not reach Custodes")

    monkeypatch.setattr(main, "_dispatch_custodes_intervention", fail_custodes_dispatch)

    try:
        for depth in (6, 7, 8):
            await _set_live_tts_pause_queue(depth)
            result = await main.handle_custodes_state_event(
                "tts_queue_languishing",
                "tts_queue",
                severity=3,
                payload=_languishing_payload(depth),
                event_class="enforcement",
            )
            assert result["intervention_dispatched"] is False
            assert result["classification"] == "internal"
            assert result["routed_to"] == "internal"
            assert result["reason"] == "internal_label_only"
    finally:
        await _clear_live_tts_pause_queue()

    assert len(admin_injects) == 0
    assert len(main._custodes_enforcement_defer_queue) == 0


# ── enforce()/Pavlok stays typing_guard-blocked (D2 physical-path guardrail) ──


async def test_enforce_blocks_physical_pavlok_under_typing_guard(app_env, monkeypatch):
    import enforce as enforce_mod

    fired = {"pavlok": 0}

    def fake_pavlok(*a, **k):
        fired["pavlok"] += 1
        return {"fired": True}

    monkeypatch.setattr(enforce_mod, "send_pavlok_stimulus", fake_pavlok)
    enforce_mod.init_deps(
        is_quiet_hours=lambda *a, **k: False,
        typing_guard_active=lambda: True,
    )

    result = await enforce_mod.enforce(
        enforce_mod.EnforceRequest(message="Close youtube", intensity=50, source="test")
    )

    assert result["fired"] is False
    assert result["blocked_by"] == "typing_guard"
    assert fired["pavlok"] == 0
