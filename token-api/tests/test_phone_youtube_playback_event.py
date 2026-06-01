import pytest


@pytest.mark.asyncio
async def test_youtube_play_true_maps_to_phone_open(app_env, monkeypatch):
    main = app_env.main
    captured = []

    async def fake_handle_phone_activity(request):
        captured.append(request)
        return main.PhoneActivityResponse(allowed=True, reason="ok", message="ok")

    monkeypatch.setattr(main, "handle_phone_activity", fake_handle_phone_activity)
    monkeypatch.setattr(main, "stop_enforcement_cascade", lambda reason="app_close": None)

    result = await main.handle_phone_system_event(
        main.PhoneSystemEventRequest(app="Youtube", play="true")
    )

    assert result["event"] == "app_playback"
    assert result["app"] == "youtube"
    assert result["action"] == "open"
    assert result["play"] is True
    assert captured[0].app == "youtube"
    assert captured[0].action == "open"


@pytest.mark.asyncio
async def test_youtube_play_false_maps_to_phone_close(app_env, monkeypatch):
    main = app_env.main
    captured = []

    async def fake_handle_phone_activity(request):
        captured.append(request)
        return main.PhoneActivityResponse(allowed=True, reason="closed", message="closed")

    monkeypatch.setattr(main, "handle_phone_activity", fake_handle_phone_activity)
    monkeypatch.setattr(main, "stop_enforcement_cascade", lambda reason="app_close": None)

    result = await main.handle_phone_system_event(
        main.PhoneSystemEventRequest(app="Youtube", play="false")
    )

    assert result["event"] == "app_playback"
    assert result["app"] == "youtube"
    assert result["action"] == "close"
    assert result["play"] is False
    assert captured[0].app == "youtube"
    assert captured[0].action == "close"


@pytest.mark.asyncio
async def test_direct_app_event_still_works_without_trigger_text(app_env, monkeypatch):
    main = app_env.main
    captured = []

    async def fake_handle_phone_activity(request):
        captured.append(request)
        return main.PhoneActivityResponse(allowed=True, reason="ok", message="ok")

    monkeypatch.setattr(main, "handle_phone_activity", fake_handle_phone_activity)

    result = await main.handle_phone_system_event(
        main.PhoneSystemEventRequest(event="app_open", app="Youtube")
    )

    assert result["event"] == "app_open"
    assert result["app"] == "youtube"
    assert result["action"] == "open"
    assert captured[0].app == "youtube"
    assert captured[0].action == "open"


@pytest.mark.asyncio
async def test_invalid_play_value_is_rejected(app_env, monkeypatch):
    main = app_env.main

    async def fail_if_called(_request):  # pragma: no cover - assertion path
        raise AssertionError("invalid play must not reach phone activity handler")

    monkeypatch.setattr(main, "handle_phone_activity", fail_if_called)

    result = await main.handle_phone_system_event(
        main.PhoneSystemEventRequest(app="Youtube", play="maybe")
    )

    assert result["received"] is True
    assert result["error"] == "invalid play param; expected true/false"
