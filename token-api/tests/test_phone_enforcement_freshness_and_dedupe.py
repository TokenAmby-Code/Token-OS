"""Regression coverage for phone enforcement's physical-action safety gates."""

import asyncio
import importlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
main = importlib.import_module("main")


@pytest.fixture(autouse=True)
def reset_phone_enforcement_state():
    main.PHONE_STATE.update(
        current_app=None,
        is_distracted=False,
        last_distraction_signal_mono=None,
        youtube_playback_active=False,
    )
    main._PHONE_ENFORCE_STATE.clear()
    main._custodes_state_debounce.clear()
    yield
    main._PHONE_ENFORCE_STATE.clear()
    main._custodes_state_debounce.clear()


def test_stale_phone_signal_never_reaches_pavlok_or_custodes(monkeypatch):
    async def run():
        main.PHONE_STATE.update(
            current_app="youtube",
            is_distracted=True,
            last_distraction_signal_mono=time.monotonic()
            - main.PHONE_DISTRACTION_SIGNAL_MAX_AGE_SECONDS
            - 1,
            youtube_playback_active=True,
        )
        calls = []

        async def unexpected_custodes(*args, **kwargs):
            calls.append("custodes")

        async def unexpected_enforce(*args, **kwargs):
            calls.append("enforce")
            return {"fired": True}

        monkeypatch.setattr(main, "handle_custodes_state_event", unexpected_custodes)
        monkeypatch.setattr(main, "enforce", unexpected_enforce)
        monkeypatch.setattr(main, "log_event", lambda *args, **kwargs: asyncio.sleep(0))
        result = await main._execute_phone_enforcement_transaction("youtube")
        assert result == {"fired": False, "reason": "phone_signal_stale"}
        assert calls == []

    asyncio.run(run())


def test_youtube_foreground_without_positive_playback_never_enforces(monkeypatch):
    async def run():
        main.PHONE_STATE.update(
            current_app="youtube",
            is_distracted=True,
            last_distraction_signal_mono=time.monotonic(),
            youtube_playback_active=False,
        )
        monkeypatch.setattr(main, "log_event", lambda *args, **kwargs: asyncio.sleep(0))
        result = await main._execute_phone_enforcement_transaction("youtube")
        assert result == {"fired": False, "reason": "youtube_playback_unverified"}

    asyncio.run(run())


def test_same_batch_three_identical_events_claim_once_before_dispatch(monkeypatch):
    """The pre-fix async DB decision allowed all three to inspect an empty cache."""

    async def run():
        dispatched = []

        async def slow_not_duplicate(key, severity):
            # Mirrors the old decision's pre-await cache read. The claim lock
            # makes the second and third calls observe the first reservation.
            cached = main._custodes_state_debounce.get(key)
            if cached and severity <= cached["severity"]:
                return True, "memory_debounce"
            await asyncio.sleep(0.01)
            return False, "not_duplicate"

        monkeypatch.setattr(main, "_custodes_state_dedupe_decision", slow_not_duplicate)

        async def one_claim():
            suppressed, reason = await main._custodes_state_dedupe_claim(
                "phone_distraction_enforce:phone:YouTube", 4
            )
            if not suppressed:
                dispatched.append(reason)
            return suppressed, reason

        results = await asyncio.gather(*(one_claim() for _ in range(3)))
        assert dispatched == ["claimed"]
        assert [result[0] for result in results].count(False) == 1
        assert [result[1] for result in results].count("memory_debounce") == 2

    asyncio.run(run())


def test_positive_youtube_playback_edge_restarts_policy_approved_enforcement(monkeypatch):
    async def run():
        main.PHONE_STATE.update(
            current_app="youtube", is_distracted=True, enforcement_eligible=True
        )
        calls = []
        monkeypatch.setattr(main, "start_enforcement_cascade", lambda app: calls.append(app))
        monkeypatch.setattr(
            main,
            "handle_phone_activity",
            lambda request: asyncio.sleep(
                0, result=main.PhoneActivityResponse(allowed=True, reason="already_tracked")
            ),
        )
        response = await main.handle_phone_system_event(
            main.PhoneSystemEventRequest(
                event="app_playback",
                app="Youtube",
                play=True,
                time=__import__("datetime").datetime.now().isoformat(),
            )
        )
        assert response["received"] is True
        assert calls == ["youtube"]

    asyncio.run(run())
