"""Tests for atomic Pavlok+TTS cascade pairing."""

import importlib

import pytest


@pytest.fixture
def main_module():
    # Import lazily so existing main-importing tests can set TOKEN_API_DB first
    # during collection. These unit tests monkeypatch all IO touched by the helper.
    return importlib.import_module("main")


@pytest.mark.asyncio
async def test_shock_fires_tts_fires(monkeypatch, main_module):
    order = []
    logged = []

    def fake_pavlok(stimulus_type, value, reason, respect_cooldown):
        order.append(("pavlok", stimulus_type, value, reason, respect_cooldown))
        return {"success": True, "type": stimulus_type, "value": value, "status_code": 200}

    def fake_phone(endpoint, params):
        order.append(("phone", endpoint, params))
        return {"success": True, "status_code": 200}

    async def fake_discord(*args, **kwargs):
        order.append(("discord", args, kwargs))

    async def fake_log_event(event, **kwargs):
        logged.append((event, kwargs))

    monkeypatch.setattr(main_module, "send_pavlok_stimulus", fake_pavlok)
    monkeypatch.setattr(main_module, "_send_to_phone", fake_phone)
    monkeypatch.setattr(main_module, "_send_discord_fallback", fake_discord)
    monkeypatch.setattr(main_module, "log_event", fake_log_event)

    result = await main_module.fire_cascade_event(2, "ack-1", {"app": "twitter"})

    assert result["dispatch_result"] == "fired"
    assert order[0][0] == "pavlok"
    assert order[1][0] == "phone"
    assert order[1][1] == "/notify"
    assert order[1][2]["tts_text"] == "Close twitter"
    assert not any(event == "cascade_event_skipped" for event, _ in logged)


@pytest.mark.asyncio
async def test_shock_skips_tts_skips(monkeypatch, main_module):
    phone_calls = []
    discord_calls = []
    logged = []

    def fake_pavlok(stimulus_type, value, reason, respect_cooldown):
        return {"skipped": True, "reason": "disabled"}

    def fake_phone(endpoint, params):
        phone_calls.append((endpoint, params))
        return {"success": True}

    async def fake_discord(*args, **kwargs):
        discord_calls.append((args, kwargs))

    async def fake_log_event(event, **kwargs):
        logged.append((event, kwargs))

    monkeypatch.setattr(main_module, "send_pavlok_stimulus", fake_pavlok)
    monkeypatch.setattr(main_module, "_send_to_phone", fake_phone)
    monkeypatch.setattr(main_module, "_send_discord_fallback", fake_discord)
    monkeypatch.setattr(main_module, "log_event", fake_log_event)

    result = await main_module.fire_cascade_event(2, "ack-2", {"app": "twitter"})

    assert result["dispatch_result"] == "skipped"
    assert phone_calls == []
    assert discord_calls == []
    skipped = [entry for entry in logged if entry[0] == "cascade_event_skipped"]
    assert len(skipped) == 1
    assert skipped[0][1]["details"]["ack_id"] == "ack-2"


@pytest.mark.asyncio
async def test_shock_failure_tts_skips(monkeypatch, main_module):
    phone_calls = []
    discord_calls = []
    logged = []

    def fake_pavlok(stimulus_type, value, reason, respect_cooldown):
        return {"success": False, "error": "timeout", "reason": reason}

    def fake_phone(endpoint, params):
        phone_calls.append((endpoint, params))
        return {"success": True}

    async def fake_discord(*args, **kwargs):
        discord_calls.append((args, kwargs))

    async def fake_log_event(event, **kwargs):
        logged.append((event, kwargs))

    monkeypatch.setattr(main_module, "send_pavlok_stimulus", fake_pavlok)
    monkeypatch.setattr(main_module, "_send_to_phone", fake_phone)
    monkeypatch.setattr(main_module, "_send_discord_fallback", fake_discord)
    monkeypatch.setattr(main_module, "log_event", fake_log_event)

    result = await main_module.fire_cascade_event(3, "ack-3", {"app": "youtube"})

    assert result["dispatch_result"] == "unreachable"
    assert phone_calls == []
    assert discord_calls == []
    skipped = [entry for entry in logged if entry[0] == "cascade_event_skipped"]
    assert len(skipped) == 1
    assert skipped[0][1]["details"]["dispatch_result"] == "unreachable"
