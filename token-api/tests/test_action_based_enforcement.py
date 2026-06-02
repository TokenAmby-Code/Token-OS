"""Action-based enforcement response (ack → action redesign).

These tests pin the new model: enforcement is satisfied/deferred by work-signal
ACTIONS, and the ack is demoted to a Pavlok-connectivity confirm with a one-time
break boost. See Mars/Tasks/design-ack-to-action-based-enforcement-response.

The two responses MUST stay distinct:
  - SATISFY (cancel): a signal that clears the triggering condition resolves the
    pending ack via _resolve_expected_ack with a logged resolved_by reason.
  - DEFER (stall, don't lose): ambient live work (typing / dictation / voice)
    stalls enforcement — Pavlok stays blocked, the ack is NOT resolved, and a
    defer reason is logged.
"""

import json
import sqlite3
from datetime import datetime, timedelta


def _rows(db_path, query, params=()) -> list:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(query, params).fetchall()


def _insert_pending_ack(db_path, main, *, ack_id, source, instance_id) -> None:
    now = datetime.now()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO expected_acknowledgements (
                id, source, instance_id, reason, status, created_at,
                ack_due_at, level2_due_at, pavlok_due_at, details_json
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """,
            (
                ack_id,
                source,
                instance_id,
                "pending enforcement test",
                now.isoformat(),
                (now + timedelta(seconds=1)).isoformat(),
                (now + timedelta(seconds=15)).isoformat(),
                (now + timedelta(seconds=15)).isoformat(),
                "{}",
            ),
        )
        conn.commit()


# (a) work-action SATISFIES a pending enforcement via the unified work signal,
#     logging an explicit resolved_by reason, and never fires Pavlok.
def test_work_action_satisfies_enforcement_with_resolved_by_reason(app_env) -> None:
    from fastapi.testclient import TestClient

    main = app_env.main
    _insert_pending_ack(
        app_env.db_path,
        main,
        ack_id="phone-ack",
        source="phone_distraction",
        instance_id="phone_distraction:phone:youtube",
    )

    client = TestClient(main.app)
    resp = client.post("/api/work-action", json={"source": "stream-deck", "note": "paperwork"})
    assert resp.status_code == 200

    rows = _rows(app_env.db_path, "SELECT status FROM expected_acknowledgements")
    assert [r["status"] for r in rows] == ["acknowledged"]

    resolved_events = _rows(
        app_env.db_path,
        "SELECT details FROM events WHERE event_type = 'expected_ack_acknowledged'",
    )
    assert len(resolved_events) == 1
    assert json.loads(resolved_events[0]["details"])["resolved_by"] == "work_signal:work_action"

    # SATISFY must not fire the shock.
    assert (
        _rows(app_env.db_path, "SELECT id FROM events WHERE event_type = 'pavlok_stimulus'") == []
    )

    # And it emits one canonical work_signal event.
    signals = _rows(
        app_env.db_path,
        "SELECT details FROM events WHERE event_type = 'work_signal'",
    )
    assert len(signals) == 1
    sig = json.loads(signals[0]["details"])
    assert sig["kind"] == "work_action"
    assert sig["disposition"] == "satisfy"


# (b) dictation start DEFERS: enforcement is not resolved and not fired, Pavlok
#     stays blocked while dictation is active, and a defer reason is logged.
def test_dictation_start_defers_without_resolving(app_env, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import enforce as enforce_mod

    main = app_env.main
    _insert_pending_ack(
        app_env.db_path,
        main,
        ack_id="phone-ack",
        source="phone_distraction",
        instance_id="phone_distraction:phone:youtube",
    )

    client = TestClient(main.app)
    resp = client.post("/api/dictation", params={"active": True})
    assert resp.status_code == 200

    # DEFER: the ack is untouched.
    rows = _rows(app_env.db_path, "SELECT status FROM expected_acknowledgements")
    assert [r["status"] for r in rows] == ["pending"]
    assert (
        _rows(
            app_env.db_path, "SELECT id FROM events WHERE event_type = 'expected_ack_acknowledged'"
        )
        == []
    )

    # A defer reason is logged (not silent).
    signals = _rows(app_env.db_path, "SELECT details FROM events WHERE event_type = 'work_signal'")
    assert len(signals) == 1
    sig = json.loads(signals[0]["details"])
    assert sig["kind"] == "dictation"
    assert sig["disposition"] == "defer"

    # Pavlok stays blocked while dictation is live: a concurrent enforce defers.
    # (Stub the typing guard off so the dictation guard is the one under test —
    # the test runner itself is inside tmux, which trips client_activity.)
    import asyncio

    monkeypatch.setattr(enforce_mod, "_typing_guard_active", lambda: False)
    out = asyncio.run(
        enforce_mod.enforce(enforce_mod.EnforceRequest(message="Close youtube", source="test"))
    )
    assert out["fired"] is False
    assert out["blocked_by"] == "dictation"


# (c) a completed voice transcription emits a canonical work_signal event.
def test_voice_transcription_complete_emits_work_signal(app_env, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    main = app_env.main

    async def _fake_voice_draft(request):
        return {"drafted": True}

    monkeypatch.setattr(main, "_handle_discord_voice_draft", _fake_voice_draft)

    client = TestClient(main.app)
    resp = client.post(
        "/api/discord/message",
        json={
            "message_id": "voice-1",
            "channel_id": "voice-custodes",
            "channel_name": "voice-custodes",
            "author": {"id": "u1", "username": "voice", "bot": False},
            "content": "log a work action for the paperwork I just did",
            "is_voice": True,
            "bot_name": "custodes",
        },
    )
    assert resp.status_code == 200

    signals = _rows(app_env.db_path, "SELECT details FROM events WHERE event_type = 'work_signal'")
    assert len(signals) == 1
    sig = json.loads(signals[0]["details"])
    assert sig["kind"] == "voice_transcription"


# (d) the ack is DEMOTED: pressing it never resolves enforcement; the first ack
#     in a sequence grants exactly one ~30s break credit, the second grants none.
def test_ack_is_demoted_to_connectivity_confirm_with_one_time_boost(app_env, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    main = app_env.main

    monkeypatch.setattr(main, "check_phone_reachable", lambda: {"reachable": True})

    # A fresh sequence: create_expected_ack should reset the boost flag.
    import asyncio

    ack = asyncio.run(
        main.create_expected_ack(
            source="phone_distraction",
            instance_id="phone_distraction:phone:youtube",
            reason="Phone distraction during work",
            details={},
        )
    )

    main.timer_engine._break_balance_ms = 0
    boost_ms = main.ACK_FIRST_BREAK_BOOST_SECONDS * 1000

    client = TestClient(main.app)

    before = main.timer_engine.break_balance_ms
    resp = client.post("/api/enforcement/ack", json={"ack_id": ack["id"]})
    assert resp.status_code == 200
    after_first = main.timer_engine.break_balance_ms

    # Ack does NOT resolve enforcement.
    rows = _rows(app_env.db_path, "SELECT status FROM expected_acknowledgements")
    assert [r["status"] for r in rows] == ["pending"]

    # First ack grants exactly one ~30s boost.
    assert boost_ms - 1500 <= (after_first - before) <= boost_ms + 1500

    # Second ack in the same sequence grants no further boost.
    resp2 = client.post("/api/enforcement/ack", json={"ack_id": ack["id"]})
    assert resp2.status_code == 200
    after_second = main.timer_engine.break_balance_ms
    assert abs(after_second - after_first) < 1000

    rows = _rows(app_env.db_path, "SELECT status FROM expected_acknowledgements")
    assert [r["status"] for r in rows] == ["pending"]


# (e) typing_guard active → Pavlok blocked AND a defer disposition logged.
def test_typing_guard_blocks_pavlok_and_logs_defer(app_env, monkeypatch) -> None:
    import asyncio

    import enforce as enforce_mod

    monkeypatch.setattr(enforce_mod, "_typing_guard_active", lambda: True)

    out = asyncio.run(
        enforce_mod.enforce(enforce_mod.EnforceRequest(message="Close youtube", source="test"))
    )
    assert out["fired"] is False
    assert out["blocked_by"] == "typing_guard"

    blocked = _rows(
        app_env.db_path,
        "SELECT details FROM events WHERE event_type = 'enforce_blocked'",
    )
    assert len(blocked) == 1
    details = json.loads(blocked[0]["details"])
    assert details["reason"] == "typing_guard"
    # DEFER, not silent suppression: typing is the appeal, we stall.
    assert details["disposition"] == "defer"
