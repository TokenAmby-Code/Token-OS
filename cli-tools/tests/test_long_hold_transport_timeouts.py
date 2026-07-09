"""Long-hold tmuxctld/Token-API callers must outwait daemon ceilings."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    loader = importlib.machinery.SourceFileLoader(name, str(ROOT / rel))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_talk_send_transport_timeout_exceeds_send_ceiling() -> None:
    talk = _load("talk_cli_timeout", "cli-tools/bin/talk")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"status":"open","talk_id":"t1"}'

    def fake_urlopen(_req, timeout):
        seen["timeout"] = timeout
        return Resp()

    with patch.object(talk.urllib.request, "urlopen", fake_urlopen):
        out = talk._post("/api/talk/send", {})
    assert out["status"] == "open"
    assert seen["timeout"] > talk.SEND_HOLD_CEILING_SECONDS


def test_brief_send_transport_timeout_exceeds_send_ceiling() -> None:
    brief = _load("brief_cli_timeout", "cli-tools/bin/brief")
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"status":"ok"}'

    def fake_urlopen(_req, timeout):
        seen["timeout"] = timeout
        return Resp()

    with patch.object(brief.urllib.request, "urlopen", fake_urlopen):
        out = brief._post("/api/brief/send", {})
    assert out["status"] == "ok"
    assert seen["timeout"] > 60.0


def test_brief_main_timeout_surfaces_correlation_handle_not_traceback() -> None:
    brief = _load("brief_cli_timeout_main", "cli-tools/bin/brief")

    def fake_urlopen(_req, timeout):
        raise TimeoutError("timed out")

    stdout = io.StringIO()
    with patch.object(brief.urllib.request, "urlopen", fake_urlopen):
        with contextlib.redirect_stdout(stdout):
            rc = brief.main(["--json", "--pane", "mechanicus:fabricator-general", "probe"])

    payload = brief.json.loads(stdout.getvalue())
    assert rc == 1
    assert payload["status"] == "send_timeout"
    assert payload["delivery"]["status"] == "unknown"
    assert payload["delivery"]["correlation_handle"]["panes"] == ["mechanicus:fabricator-general"]


def test_tmuxctld_ping_lifecycle_routes_have_no_client_transfer_timeout() -> None:
    text = (ROOT / "cli-tools/bin/tmuxctld-ping").read_text()
    assert '"POST /reconcile"|"POST /close-pane"|"POST /close"|"POST /stack/add")' in text
    assert 'MAX_TIME="${TMUXCTLD_MAX_TIME:-0}"' in text
    assert "tmuxctld_lifecycle_client_timeout" not in text
    assert '"POST /send-text")' in text
    assert "tmuxctld_send_client_timeout" in text


def test_work_loop_stack_add_uses_lifecycle_transport_budget() -> None:
    text = (ROOT / "cli-tools/bin/work-loop").read_text()
    assert "tmuxctld_result_max_time()" in text
    assert '"POST /reconcile"|"POST /close-pane"|"POST /close"|"POST /stack/add")' in text
    assert 'TMUXCTLD_MAX_TIME="${TMUXCTLD_MAX_TIME:-$(tmuxctld_send_client_timeout)}"' not in text
