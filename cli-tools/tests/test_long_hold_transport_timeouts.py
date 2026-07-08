"""Long-hold tmuxctld/Token-API callers must outwait daemon ceilings."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
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


def test_tmuxctld_ping_long_hold_routes_exceed_sixty_seconds() -> None:
    env = os.environ.copy()
    env.update({"TMUXCTLD_PING_PRINT_RESPONSE": "1", "TMUXCTLD_URL": "http://127.0.0.1:9"})
    cmd = "source cli-tools/lib/tmuxctld-timeouts.sh; tmuxctld_lifecycle_client_timeout"
    budget = subprocess.check_output(["bash", "-lc", cmd], cwd=ROOT, env=env, text=True).strip()
    assert float(budget) > 60.0
    text = (ROOT / "cli-tools/bin/tmuxctld-ping").read_text()
    assert (
        '"POST /reconcile"' in text and '"POST /close-pane"' in text and '"POST /send-text"' in text
    )
    assert "tmuxctld_lifecycle_client_timeout" in text
