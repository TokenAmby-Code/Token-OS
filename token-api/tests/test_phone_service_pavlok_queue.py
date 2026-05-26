from __future__ import annotations

from datetime import datetime, timedelta

import pytest


@pytest.fixture()
def phone_service_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_API_DB", str(tmp_path / "agents.db"))
    import phone_service

    phone_service.PAVLOK_CONFIG.update(
        {
            "enabled": True,
            "token": "test-token",
            "min_gap_seconds": 2.0,
            "zap_cooldown_seconds": 20 * 60,
            "soft_cooldown_seconds": 3 * 60,
            "daily_zap_cap": 6,
        }
    )
    phone_service.PAVLOK_STATE.update(
        {
            "last_stimulus_at": None,
            "last_zap_at": None,
            "last_soft_at": None,
            "zap_count_date": None,
            "zap_count": 0,
        }
    )
    phone_service.PHONE_STATE["reachable"] = None
    phone_service.TTS_GLOBAL_MODE["mode"] = "normal"
    phone_service.DESKTOP_STATE["in_meeting"] = False
    phone_service.DESKTOP_STATE["work_mode"] = None
    phone_service.DESKTOP_STATE["club_context"] = False
    phone_service.DESKTOP_STATE["driving_context"] = False
    phone_service.DESKTOP_STATE["medical_context"] = False
    phone_service.DESKTOP_STATE["location_zone"] = None
    monkeypatch.setattr(phone_service, "_last_pavlok_dispatch_monotonic", None)
    return phone_service


def test_pavlok_phone_success_suppresses_api_fallback(phone_service_env, monkeypatch):
    phone_service = phone_service_env
    phone_calls = []

    def fake_phone(endpoint, params):
        phone_calls.append((endpoint, params))
        return {"success": True, "status_code": 200}

    monkeypatch.setattr(phone_service, "_send_to_phone_raw", fake_phone)
    monkeypatch.setattr(
        phone_service.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("Pavlok API fallback should not run"),
    )

    result = phone_service.send_pavlok_stimulus("beep", 25, "pytest", respect_cooldown=False)

    assert result["success"] is True
    assert result["accepted"] is True
    assert result["intent_sent"] is True
    assert result["transport"] == "phone"
    assert phone_calls == [("/zap", {"action": "beep", "intensity": 25})]


def test_pavlok_phone_failure_falls_back_to_api(phone_service_env, monkeypatch):
    phone_service = phone_service_env

    class Response:
        status_code = 200

    monkeypatch.setattr(
        phone_service,
        "_send_to_phone_raw",
        lambda endpoint, params: {"success": False, "status_code": 503},
    )
    api_calls = []

    def fake_post(url, headers, json, timeout):
        api_calls.append((url, headers, json, timeout))
        return Response()

    monkeypatch.setattr(phone_service.requests, "post", fake_post)

    result = phone_service.send_pavlok_stimulus("zap", 30, "pytest", respect_cooldown=False)

    assert result["success"] is True
    assert result["accepted"] is True
    assert result["intent_sent"] is False
    assert result["transport"] == "api"
    assert api_calls[0][2] == {"stimulus": {"stimulusType": "zap", "stimulusValue": 30}}


def test_pavlok_min_gap_is_applied_between_stimuli(phone_service_env, monkeypatch):
    phone_service = phone_service_env
    sleeps = []
    monotonic_values = iter([101.0, 101.0])

    monkeypatch.setattr(phone_service, "_last_pavlok_dispatch_monotonic", 100.0)
    monkeypatch.setattr(phone_service.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(phone_service.time, "sleep", lambda delay: sleeps.append(delay))
    monkeypatch.setattr(
        phone_service,
        "_send_to_phone_raw",
        lambda endpoint, params: {"success": True, "status_code": 200},
    )

    result = phone_service.send_pavlok_stimulus("vibe", 20, "pytest", respect_cooldown=False)

    assert result["success"] is True
    assert sleeps == [1.0]


def test_concurrent_style_zap_and_beep_serialize_through_one_lane(phone_service_env, monkeypatch):
    phone_service = phone_service_env
    call_order = []

    def fake_phone(endpoint, params):
        call_order.append(("start", params["action"]))
        call_order.append(("end", params["action"]))
        return {"success": True, "status_code": 200}

    monkeypatch.setattr(phone_service, "_send_to_phone_raw", fake_phone)
    monkeypatch.setattr(phone_service.time, "sleep", lambda delay: call_order.append(("sleep", delay)))

    zap = phone_service.send_pavlok_stimulus("zap", 30, "pytest", respect_cooldown=False)
    beep = phone_service.send_pavlok_stimulus("beep", 30, "pytest", respect_cooldown=False)

    assert zap["success"] is True
    assert beep["success"] is True
    assert call_order[0:2] == [("start", "zap"), ("end", "zap")]
    assert call_order[2][0] == "sleep"
    assert call_order[3:5] == [("start", "beep"), ("end", "beep")]


def test_send_to_phone_strips_notify_pavlok_params_and_queues(phone_service_env, monkeypatch):
    phone_service = phone_service_env
    pavlok_calls = []
    raw_calls = []

    def fake_pavlok(stimulus_type, value, reason, respect_cooldown=True):
        pavlok_calls.append((stimulus_type, value, reason, respect_cooldown))
        return {"success": True, "type": stimulus_type, "value": value}

    def fake_raw(endpoint, params):
        raw_calls.append((endpoint, params))
        return {"success": True, "status_code": 200}

    monkeypatch.setattr(phone_service, "send_pavlok_stimulus", fake_pavlok)
    monkeypatch.setattr(phone_service, "_send_to_phone_raw", fake_raw)

    result = phone_service._send_to_phone(
        "/notify",
        {"vibe": 30, "beep": 0, "tts_text": "hello", "banner_text": "hi"},
    )

    assert result["success"] is True
    assert pavlok_calls == [("vibe", 30, "phone_params_notify", True)]
    assert raw_calls == [("/notify", {"tts_text": "hello", "banner_text": "hi"})]


def test_send_to_phone_strips_enforce_zap_and_keeps_notification_params(
    phone_service_env, monkeypatch
):
    phone_service = phone_service_env
    pavlok_calls = []
    raw_calls = []

    monkeypatch.setattr(
        phone_service,
        "send_pavlok_stimulus",
        lambda stimulus_type, value, reason, respect_cooldown=True: pavlok_calls.append(
            (stimulus_type, value, reason, respect_cooldown)
        )
        or {"success": True},
    )
    monkeypatch.setattr(
        phone_service,
        "_send_to_phone_raw",
        lambda endpoint, params: raw_calls.append((endpoint, params))
        or {"success": True, "status_code": 200},
    )

    result = phone_service._send_to_phone(
        "/enforce",
        {"zap": 50, "tts_text": "close it", "banner_text": "enforcement active"},
    )

    assert result["success"] is True
    assert pavlok_calls == [("zap", 50, "phone_params_enforce", True)]
    assert raw_calls == [("/enforce", {"tts_text": "close it", "banner_text": "enforcement active"})]


def test_guardrails_block_before_queue_dispatch(phone_service_env, monkeypatch):
    phone_service = phone_service_env
    phone_service.TTS_GLOBAL_MODE["mode"] = "muted"
    monkeypatch.setattr(
        phone_service,
        "_send_to_phone_raw",
        lambda *args, **kwargs: pytest.fail("guardrail block must not dispatch to phone"),
    )
    monkeypatch.setattr(
        phone_service.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("guardrail block must not dispatch to API"),
    )

    result = phone_service.send_pavlok_stimulus("zap", 30, "pytest", respect_cooldown=False)

    assert result["success"] is False
    assert result["blocked_by_guardrail"] is True
    assert result["reason"] == "quiet_mode"


def test_daily_zap_cap_still_blocks(phone_service_env, monkeypatch):
    phone_service = phone_service_env
    now = datetime.now()
    phone_service.PAVLOK_STATE.update(
        {
            "zap_count_date": now.date().isoformat(),
            "zap_count": 6,
            "last_zap_at": (now - timedelta(days=1)).isoformat(),
            "last_stimulus_at": None,
        }
    )
    monkeypatch.setattr(
        phone_service,
        "_send_to_phone_raw",
        lambda *args, **kwargs: pytest.fail("daily cap block must not dispatch"),
    )

    result = phone_service.send_pavlok_stimulus("zap", 30, "pytest", respect_cooldown=False)

    assert result["success"] is False
    assert result["reason"] == "daily_zap_cap"
