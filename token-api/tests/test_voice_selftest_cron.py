"""Voice selftest pre-morning cron wiring (PR B).

The scheduled task only proves the wiring: `voice_selftest_morning` resolves in
TASK_REGISTRY and POSTs the full-variant probe to the discord daemon. The
daemon owns all surfacing (events row always; alerts channel on fail/degraded),
so the cron result is just a fired/failed record.
"""

import asyncio
import sys


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300
        self._payload = payload or {}
        self.content = b"{}" if self._payload is not None else b""

    def json(self):
        return self._payload


def _fake_client_class(calls, response=None, exc=None):
    """Fake httpx.AsyncClient: records the post, returns `response` or raises `exc`."""

    class FakeClient:
        def __init__(self, timeout=None):
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *excinfo):
            return False

        async def post(self, url, json=None):
            calls["url"] = url
            calls["json"] = json
            if exc is not None:
                raise exc
            return response

    return FakeClient


def _capture_log_event(monkeypatch, main):
    logged = []

    async def fake_log_event(event_type, instance_id=None, details=None):
        logged.append({"event_type": event_type, "details": details})

    monkeypatch.setattr(main, "log_event", fake_log_event)
    return logged


def test_voice_selftest_task_registered(app_env):
    main = sys.modules["main"]
    assert "voice_selftest_morning" in main.TASK_REGISTRY
    assert callable(main.TASK_REGISTRY["voice_selftest_morning"])


def test_voice_selftest_task_posts_full_variant(app_env, monkeypatch):
    main = sys.modules["main"]
    calls = {}
    response = _FakeResponse(payload={"overall": "pass", "probe_id": "probe-1"})
    monkeypatch.setattr(main.httpx, "AsyncClient", _fake_client_class(calls, response=response))

    result = asyncio.run(main.run_morning_voice_selftest())

    assert calls["url"].endswith("/voice/selftest")
    assert calls["json"] == {"variant": "full", "trigger": "cron"}
    # Client timeout must outlast the probe's own 60s hard deadline.
    assert calls["timeout"] > 60
    assert result["overall"] == "pass"


def test_voice_selftest_task_records_non_2xx_as_failure(app_env, monkeypatch):
    main = sys.modules["main"]
    calls = {}
    response = _FakeResponse(status_code=409, payload={"errorCode": "probe_in_progress"})
    monkeypatch.setattr(main.httpx, "AsyncClient", _fake_client_class(calls, response=response))
    logged = _capture_log_event(monkeypatch, main)

    result = asyncio.run(main.run_morning_voice_selftest())

    assert result == {"error": "HTTP 409", "status_code": 409}
    assert logged and logged[0]["event_type"] == "voice_selftest"
    assert logged[0]["details"]["overall"] == "fail"
    assert logged[0]["details"]["detail"] == {"errorCode": "probe_in_progress"}


def test_voice_selftest_task_records_daemon_down_as_failure(app_env, monkeypatch):
    main = sys.modules["main"]
    calls = {}
    monkeypatch.setattr(
        main.httpx, "AsyncClient", _fake_client_class(calls, exc=ConnectionError("daemon down"))
    )
    logged = _capture_log_event(monkeypatch, main)

    result = asyncio.run(main.run_morning_voice_selftest())

    assert "error" in result
    assert logged and logged[0]["event_type"] == "voice_selftest"
    assert logged[0]["details"]["overall"] == "fail"
