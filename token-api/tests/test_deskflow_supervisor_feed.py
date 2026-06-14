"""The deskflow supervisor feeds connection state to token-api.

The supervisor (Shell/deskflow-client-supervisor.py) is the missing link in the
deskflow → desktop_mode → timer feed: it managed the deskflow-core client
lifecycle but never told token-api the client was connected, so the work model
had no auto active-process signal. ``post_deskflow_state`` is that feed — a
stdlib-only, best-effort POST to /api/desktop/deskflow. These tests pin its
contract (URL, payload, return value, never-raise) with a fake urlopen; the
token-api receiver end is covered by test_deskflow_desktop_active_work.
"""

import importlib.util
import json
from pathlib import Path
from typing import Any


def load_supervisor_module() -> Any:
    module_path = Path(__file__).resolve().parents[2] / "Shell" / "deskflow-client-supervisor.py"
    spec = importlib.util.spec_from_file_location(
        "deskflow_client_supervisor_feed_for_tests", module_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeResp:
    def __init__(self, code: int):
        self._code = code

    def getcode(self) -> int:
        return self._code

    def close(self) -> None:
        pass


def test_post_active_builds_correct_request():
    module = load_supervisor_module()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode())
        captured["content_type"] = req.get_header("Content-type")
        return _FakeResp(200)

    ok = module.post_deskflow_state(True, base="http://127.0.0.1:7777", urlopen=fake_urlopen)
    assert ok is True
    assert captured["url"] == "http://127.0.0.1:7777/api/desktop/deskflow"
    assert captured["method"] == "POST"
    assert captured["body"] == {"active": True, "source": "deskflow-supervisor"}
    assert captured["content_type"] == "application/json"


def test_post_inactive_sends_false():
    module = load_supervisor_module()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp(200)

    ok = module.post_deskflow_state(False, urlopen=fake_urlopen)
    assert ok is True
    assert captured["body"]["active"] is False


def test_post_non_2xx_returns_false():
    module = load_supervisor_module()

    def fake_urlopen(req, timeout=None):
        return _FakeResp(500)

    assert module.post_deskflow_state(True, urlopen=fake_urlopen) is False


def test_post_never_raises_on_network_error():
    """Telemetry must never crash the supervisor — a down token-api is tolerated."""
    module = load_supervisor_module()

    def boom(req, timeout=None):
        raise OSError("connection refused")

    assert module.post_deskflow_state(True, urlopen=boom) is False
