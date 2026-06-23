"""tmuxctld fail-closed contract: when tmux is dead, ``/health`` reports the
graceful degraded state (200 + ``tmux_reachable:false``), data endpoints return
the error envelope at 200 (NEVER a 500), and a gated send surfaces the structured
``gated`` envelope."""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon
from tmuxctl.tmux_adapter import TmuxError, TmuxSendGated


class DeadTmuxAdapter:
    """tmux is unreachable: list/probe raise; any other call raises TmuxError."""

    def list_sessions(self):
        raise TmuxError("no server running")

    def run(self, *args, allow_failure=False):
        raise TmuxError("no server running")


class GatedAdapter:
    """tmux reachable, but the universal send gate suppresses the payload."""

    GATE = {"suppressed": True, "reason": "quiet-hours"}

    def list_sessions(self):
        return []

    def run(self, *args, allow_failure=False):
        return ""

    def send_text_then_submit(self, target, text, *, clear_prompt=False, **_kw):
        raise TmuxSendGated(self.GATE)


def _serve(adapter_factory):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=adapter_factory, version="t", sha="t"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(server, path):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(server, path, body):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_health_degraded_when_tmux_dead():
    server = _serve(DeadTmuxAdapter)
    try:
        status, payload = _get(server, "/health")
        assert status == 200
        assert payload["ok"] is True
        assert payload["tmux_reachable"] is False
    finally:
        server.shutdown()


def test_data_endpoint_never_500_when_tmux_dead():
    server = _serve(DeadTmuxAdapter)
    try:
        # freelist hits tmux; with tmux dead it must surface a 200 error envelope,
        # not a transport-level 500.
        status, payload = _get(server, "/freelist")
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"]  # a structured code, any value
    finally:
        server.shutdown()


def test_resolve_instance_fail_closed_when_tmux_dead():
    server = _serve(DeadTmuxAdapter)
    try:
        status, payload = _get(server, "/tmux/resolve-instance?instance_id=x")
        assert status == 200
        # Resolution errors are still enveloped at 200, never a 500.
        assert payload["ok"] is False
    finally:
        server.shutdown()


def test_send_gated_envelope():
    server = _serve(GatedAdapter)
    try:
        status, payload = _post(server, "/send-text", {"pane": "%1", "text": "hi", "submit": True})
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"] == "gated"
        # The structured gate result rides in detail so callers can re-queue.
        assert payload["error"]["detail"] == GatedAdapter.GATE
    finally:
        server.shutdown()
