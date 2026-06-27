"""Wave 1 — phone media telemetry: passthrough assert, Spotify music, games guard.

Covers the server half of the passthrough-assert rewrite:
  - Bug C: Spotify play-edge lights the ♪ icon without entering the distraction
    pipeline (no enforce, is_distracted stays False).
  - Bug D: the games/backlog enforce is deferred and cancelled by a flash close.
  - YouTube passthrough/backfill already authoritative (regression guard).
"""

import asyncio
from types import SimpleNamespace


# --------------------------------------------------------------- Spotify (Bug C)
def test_spotify_play_true_lights_music_without_distraction(app_env):
    from fastapi.testclient import TestClient

    main = app_env.main
    client = TestClient(main.app)

    resp = client.post("/phone/event", json={"app": "Spotify", "play": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["app"] == "spotify"
    assert body["action"] == "open"
    assert body["play"] is True

    assert (main.PHONE_STATE.get("current_app") or "").lower() == "spotify"
    assert main.PHONE_STATE.get("is_distracted") is False

    icons = {i.key: i.active for i in main._activity_icons()}
    assert icons["spotify"] is True

    # Music must not flip the timer into a distraction.
    assert main.timer_engine.activity != main.Activity.DISTRACTION


def test_spotify_play_false_clears_current_app(app_env):
    from fastapi.testclient import TestClient

    main = app_env.main
    client = TestClient(main.app)

    client.post("/phone/event", json={"app": "Spotify", "play": "true"})
    assert (main.PHONE_STATE.get("current_app") or "").lower() == "spotify"

    resp = client.post("/phone/event", json={"app": "Spotify", "play": "false"})
    assert resp.status_code == 200
    assert resp.json()["action"] == "close"
    assert main.PHONE_STATE.get("current_app") in (None, "")


def test_spotify_play_edge_never_enforces(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    main = app_env.main
    fired = []
    monkeypatch.setattr(main, "start_enforcement_cascade", lambda app: fired.append(app))
    monkeypatch.setattr(
        main, "schedule_deferred_enforcement_cascade", lambda app: fired.append(app)
    )

    client = TestClient(main.app)
    client.post("/phone/event", json={"app": "Spotify", "play": "true"})
    assert fired == []


def test_non_media_non_youtube_play_edge_still_rejected(app_env):
    from fastapi.testclient import TestClient

    main = app_env.main
    client = TestClient(main.app)
    resp = client.post("/phone/event", json={"app": "Telegram", "play": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert body["play"] == "true"


# ----------------------------------------------------------- games guard (Bug D)
def _force_backlog(main, monkeypatch):
    """Drive handle_phone_activity into the break_secs < 0 (backlog) branch."""
    monkeypatch.setattr(main, "is_quiet_hours", lambda: False)
    main.DESKTOP_STATE["work_mode"] = "clocked_in"
    main.timer_engine._break_balance_ms = -60_000

    async def _fake_work_state():
        return SimpleNamespace(
            productivity_active=False,
            active_instance_count=0,
            observed_agent_count=0,
        )

    monkeypatch.setattr(main, "compute_work_state", _fake_work_state)

    async def _no_ack(**kwargs):
        return None

    monkeypatch.setattr(main, "maybe_create_backlog_violation_ack", _no_ack)


def test_backlog_open_defers_enforce(app_env, monkeypatch):
    main = app_env.main
    _force_backlog(main, monkeypatch)
    fired = []
    monkeypatch.setattr(main, "start_enforcement_cascade", lambda app: fired.append(app))

    async def go():
        # Reset phone state so this isn't treated as a duplicate open.
        main.PHONE_STATE["current_app"] = None
        await main.handle_phone_activity(
            main.PhoneActivityRequest(app="slay the spire", action="open", package="slay the spire")
        )
        # Deferred, NOT immediate.
        assert fired == []
        assert "slay the spire" in main._PENDING_PHONE_ENFORCE

    asyncio.run(go())
    main._cancel_pending_phone_enforce()


def test_flash_close_cancels_deferred_enforce(app_env, monkeypatch):
    main = app_env.main
    _force_backlog(main, monkeypatch)
    fired = []
    monkeypatch.setattr(main, "start_enforcement_cascade", lambda app: fired.append(app))
    monkeypatch.setattr(main, "PHONE_DISTRACTION_ENFORCE_DELAY_SECONDS", 5)

    async def go():
        main.PHONE_STATE["current_app"] = None
        await main.handle_phone_activity(
            main.PhoneActivityRequest(app="slay the spire", action="open", package="slay the spire")
        )
        assert "slay the spire" in main._PENDING_PHONE_ENFORCE
        # Flash: close arrives before the delay elapses.
        await main.handle_phone_activity(
            main.PhoneActivityRequest(
                app="slay the spire", action="close", package="slay the spire"
            )
        )
        assert "slay the spire" not in main._PENDING_PHONE_ENFORCE
        # Give any (cancelled) task a tick; it must never fire.
        await asyncio.sleep(0.05)
        assert fired == []

    asyncio.run(go())


def test_sustained_foreground_fires_once(app_env, monkeypatch):
    main = app_env.main
    fired = []
    monkeypatch.setattr(main, "start_enforcement_cascade", lambda app: fired.append(app))
    monkeypatch.setattr(main, "PHONE_DISTRACTION_ENFORCE_DELAY_SECONDS", 0)

    async def go():
        main.PHONE_STATE["current_app"] = "slay the spire"
        main.schedule_deferred_enforcement_cascade("slay the spire")
        await asyncio.sleep(0.05)
        assert fired == ["slay the spire"]

    asyncio.run(go())
    main._cancel_pending_phone_enforce()


def test_stop_cascade_cancels_pending(app_env, monkeypatch):
    main = app_env.main
    fired = []
    monkeypatch.setattr(main, "start_enforcement_cascade", lambda app: fired.append(app))
    monkeypatch.setattr(main, "PHONE_DISTRACTION_ENFORCE_DELAY_SECONDS", 5)

    async def go():
        main.PHONE_STATE["current_app"] = "slay the spire"
        main.schedule_deferred_enforcement_cascade("slay the spire")
        assert "slay the spire" in main._PENDING_PHONE_ENFORCE
        main.stop_enforcement_cascade(reason="test")
        assert main._PENDING_PHONE_ENFORCE == {}
        await asyncio.sleep(0.05)
        assert fired == []

    asyncio.run(go())


# ------------------------------------------------------- YouTube passthrough (C)
def test_youtube_backfill_opens_and_closes(app_env, monkeypatch):
    from fastapi.testclient import TestClient

    main = app_env.main
    # Keep the backfilled open out of the enforce path regardless of timer state.
    monkeypatch.setattr(main, "is_quiet_hours", lambda: True)
    client = TestClient(main.app)

    r = client.get(
        "/api/state/validate", params={"app": "youtube", "assert": "true", "backfill": "1"}
    )
    assert r.status_code == 200
    assert (main.PHONE_STATE.get("current_app") or "").lower() == "youtube"

    r = client.get(
        "/api/state/validate", params={"app": "youtube", "assert": "false", "backfill": "1"}
    )
    assert r.status_code == 200
    assert main.PHONE_STATE.get("current_app") in (None, "")
