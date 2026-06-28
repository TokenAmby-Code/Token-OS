"""Wave 1 — phone media telemetry: passthrough assert, Spotify music, games guard.

Covers the server half of the passthrough-assert rewrite:
  - Bug C: Spotify play-edge lights the ♪ icon without entering the distraction
    pipeline (no enforce, is_distracted stays False).
  - Bug D: the games/backlog enforce is deferred and cancelled by a flash close.
  - YouTube passthrough/backfill already authoritative (regression guard).
"""

import asyncio
import time
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


# --------------------------------------------------- phone eject + ramp (Phase 2)
def _patch_cascade_io(main, monkeypatch):
    """Record enforce() + eject dispatch from a real start_enforcement_cascade.

    Patches the three side-effecting collaborators (enforce, the Custodes state
    event, the phone eject transport) so the cascade's own cooldown/ramp/eject
    logic runs unmocked. Clears per-app ramp state so each test starts at rep 0.
    """
    monkeypatch.setattr(main, "is_quiet_hours", lambda: False)
    enforce_calls = []
    eject_calls = []

    async def fake_enforce(request):
        enforce_calls.append(request)
        return {"fired": True, "intensity": request.intensity}

    async def fake_custodes(*args, **kwargs):
        return None

    def fake_eject(method="redirect"):
        eject_calls.append(method)
        return {"success": True}

    monkeypatch.setattr(main, "enforce", fake_enforce)
    monkeypatch.setattr(main, "handle_custodes_state_event", fake_custodes)
    monkeypatch.setattr(main, "_send_eject_to_phone", fake_eject)
    main._PHONE_ENFORCE_STATE.clear()
    return enforce_calls, eject_calls


def test_sustained_enforce_ejects_once_no_banner(app_env, monkeypatch):
    """Sustained foreground enforce → exactly one eject(method=redirect), and the
    EnforceRequest carries notify=False (no focus-stealing banner) at rep1=40."""
    main = app_env.main
    enforce_calls, eject_calls = _patch_cascade_io(main, monkeypatch)

    async def go():
        main.start_enforcement_cascade("slay the spire")
        await asyncio.sleep(0.05)  # let ensure_future(enforce) run
        assert eject_calls == ["redirect"]
        assert len(enforce_calls) == 1
        assert enforce_calls[0].notify is False
        assert enforce_calls[0].intensity == 40

    asyncio.run(go())
    main._PHONE_ENFORCE_STATE.clear()


def test_flash_close_no_eject(app_env, monkeypatch):
    """A flash (open→close inside the deferral window) cancels the deferred
    enforce so neither the enforce nor the eject ever fires."""
    main = app_env.main
    _force_backlog(main, monkeypatch)
    enforce_calls, eject_calls = _patch_cascade_io(main, monkeypatch)
    monkeypatch.setattr(main, "PHONE_DISTRACTION_ENFORCE_DELAY_SECONDS", 5)

    async def go():
        main.PHONE_STATE["current_app"] = None
        await main.handle_phone_activity(
            main.PhoneActivityRequest(app="slay the spire", action="open", package="slay the spire")
        )
        assert "slay the spire" in main._PENDING_PHONE_ENFORCE
        await main.handle_phone_activity(
            main.PhoneActivityRequest(
                app="slay the spire", action="close", package="slay the spire"
            )
        )
        await asyncio.sleep(0.05)
        assert eject_calls == []
        assert enforce_calls == []

    asyncio.run(go())


def test_phone_path_sends_zap_and_eject_no_notify(app_env, monkeypatch):
    """End-to-end through the real enforce + phone transport: the phone path hits
    /zap and /eject but NEVER /notify (no-warnings decree, notify suppressed)."""
    main = app_env.main
    import phone_service

    monkeypatch.setattr(main, "is_quiet_hours", lambda: False)
    monkeypatch.setattr(main, "_is_quiet_hours", lambda: False, raising=False)
    monkeypatch.setitem(phone_service.PAVLOK_CONFIG, "enabled", True)

    async def fake_custodes(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "handle_custodes_state_event", fake_custodes)

    endpoints = []

    def fake_raw(endpoint, params=None):
        endpoints.append(endpoint)
        return {"success": True, "status_code": 200}

    monkeypatch.setattr(phone_service, "_send_to_phone_raw", fake_raw)
    main._PHONE_ENFORCE_STATE.clear()

    async def go():
        main.start_enforcement_cascade("slay the spire")
        await asyncio.sleep(0.05)
        assert "/eject" in endpoints
        assert "/zap" in endpoints
        assert "/notify" not in endpoints

    asyncio.run(go())
    main._PHONE_ENFORCE_STATE.clear()


def test_intensity_ramps_across_enforces(app_env, monkeypatch):
    """Successive enforces ramp intensity 40, 50, 60 (min(30+10*rep, 100)) and
    each fires its own eject. Cooldown disabled to isolate the ramp."""
    main = app_env.main
    enforce_calls, eject_calls = _patch_cascade_io(main, monkeypatch)
    monkeypatch.setattr(main, "PHONE_ENFORCE_MIN_GAP_SECONDS", 0)

    async def go():
        main.start_enforcement_cascade("slay the spire")
        main.start_enforcement_cascade("slay the spire")
        main.start_enforcement_cascade("slay the spire")
        await asyncio.sleep(0.05)
        assert [c.intensity for c in enforce_calls] == [40, 50, 60]
        assert eject_calls == ["redirect", "redirect", "redirect"]

    asyncio.run(go())
    main._PHONE_ENFORCE_STATE.clear()


def test_cooldown_skips_second_enforce(app_env, monkeypatch):
    """A second enforce inside the min-gap is skipped entirely — no re-zap, no
    re-eject, and the ramp does not advance."""
    main = app_env.main
    enforce_calls, eject_calls = _patch_cascade_io(main, monkeypatch)
    monkeypatch.setattr(main, "PHONE_ENFORCE_MIN_GAP_SECONDS", 999)

    async def go():
        main.start_enforcement_cascade("slay the spire")
        main.start_enforcement_cascade("slay the spire")  # inside gap → skipped
        await asyncio.sleep(0.05)
        assert len(enforce_calls) == 1
        assert eject_calls == ["redirect"]
        assert main._PHONE_ENFORCE_STATE["slay the spire"]["rep"] == 1

    asyncio.run(go())
    main._PHONE_ENFORCE_STATE.clear()


def test_flap_close_keeps_ramp_clean_close_resets(app_env, monkeypatch):
    """A close→reopen flap (recent fire) keeps the ramp; only a clean close
    (sustained gap since the last fire) resets rep to 0."""
    main = app_env.main
    monkeypatch.setattr(main, "PHONE_ENFORCE_MIN_GAP_SECONDS", 45)
    now = time.monotonic()

    # Seed state as if rep1 just fired.
    main._PHONE_ENFORCE_STATE["slay the spire"] = {"rep": 1, "last_fired_mono": now}
    # Immersive flap close (recent fire, within gap) → ramp kept.
    main._maybe_reset_phone_enforce_state("slay the spire")
    assert main._PHONE_ENFORCE_STATE.get("slay the spire", {}).get("rep") == 1

    # Clean close (sustained gap → backdate the last fire) → ramp reset.
    main._PHONE_ENFORCE_STATE["slay the spire"]["last_fired_mono"] = now - 100
    main._maybe_reset_phone_enforce_state("slay the spire")
    assert "slay the spire" not in main._PHONE_ENFORCE_STATE


def test_clean_close_resets_ramp_via_close_path(app_env, monkeypatch):
    """The ramp reset is wired into the real app-close path: a clean close clears
    the per-app state (gap 0 makes every close 'clean')."""
    main = app_env.main
    enforce_calls, eject_calls = _patch_cascade_io(main, monkeypatch)
    monkeypatch.setattr(main, "PHONE_ENFORCE_MIN_GAP_SECONDS", 0)

    async def go():
        main.PHONE_STATE["current_app"] = "slay the spire"
        main.start_enforcement_cascade("slay the spire")  # rep1
        await asyncio.sleep(0.05)
        assert main._PHONE_ENFORCE_STATE["slay the spire"]["rep"] == 1
        await main.handle_phone_activity(
            main.PhoneActivityRequest(
                app="slay the spire", action="close", package="slay the spire"
            )
        )
        assert "slay the spire" not in main._PHONE_ENFORCE_STATE

    asyncio.run(go())
    main._PHONE_ENFORCE_STATE.clear()


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
