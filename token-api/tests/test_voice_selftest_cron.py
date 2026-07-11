"""Voice selftest pre-morning cron wiring (PR B).

The scheduled task only proves the wiring: `voice_selftest_morning` resolves in
TASK_REGISTRY and POSTs the full-variant probe to the discord daemon. The
daemon owns all surfacing (events row always; alerts channel on fail/degraded),
so the cron result is just a fired/failed record.
"""

import asyncio
import sys


def test_voice_selftest_task_registered(app_env):
    main = sys.modules["main"]
    assert "voice_selftest_morning" in main.TASK_REGISTRY
    assert callable(main.TASK_REGISTRY["voice_selftest_morning"])


def test_voice_selftest_task_posts_full_variant(app_env, monkeypatch):
    main = sys.modules["main"]
    calls = {}

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"overall": "pass", "probe_id": "probe-1"}

    class FakeClient:
        def __init__(self, timeout=None):
            calls["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            calls["url"] = url
            calls["json"] = json
            return FakeResponse()

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeClient)

    result = asyncio.run(main.run_morning_voice_selftest())

    assert calls["url"].endswith("/voice/selftest")
    assert calls["json"] == {"variant": "full", "trigger": "cron"}
    # Client timeout must outlast the probe's own 60s hard deadline.
    assert calls["timeout"] > 60
    assert result["overall"] == "pass"


def test_voice_selftest_task_records_daemon_down_as_failure(app_env, monkeypatch):
    main = sys.modules["main"]

    class DownClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            raise ConnectionError("daemon down")

    logged = []

    async def fake_log_event(event_type, instance_id=None, details=None):
        logged.append({"event_type": event_type, "details": details})

    monkeypatch.setattr(main.httpx, "AsyncClient", DownClient)
    monkeypatch.setattr(main, "log_event", fake_log_event)

    result = asyncio.run(main.run_morning_voice_selftest())

    assert "error" in result
    assert logged and logged[0]["event_type"] == "voice_selftest"
    assert logged[0]["details"]["overall"] == "fail"
