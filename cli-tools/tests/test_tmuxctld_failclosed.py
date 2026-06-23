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

    def list_sessions(self) -> list:
        raise TmuxError("no server running")

    def run(self, *args: str, allow_failure: bool = False) -> str:
        raise TmuxError("no server running")


class GatedAdapter:
    """tmux reachable, but the universal send gate suppresses the payload."""

    GATE = {"suppressed": True, "reason": "quiet-hours"}

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        return ""

    def send_text_then_submit(
        self, target: str, text: str, *, clear_prompt: bool = False, **_kw
    ) -> None:
        raise TmuxSendGated(self.GATE)


class SendKeysGatedAdapter:
    """run() suppresses a send the way the real gate does — sets
    last_send_gate_result and returns '' WITHOUT raising (the caller must notice)."""

    GATE = {"suppressed": True, "reason": "keystroke-lock"}

    def __init__(self) -> None:
        self.last_send_gate_result = None

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:1] == ("send-keys",):
            self.last_send_gate_result = self.GATE
            return ""
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)


def _serve(adapter_factory):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=adapter_factory, version="t", sha="t"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # Gate on the real ready event — no sleep-based race before hitting endpoints.
    assert server.ready.wait(timeout=5), "server thread never signalled ready"
    return server


def _get(server, path: str):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(server, path: str, body):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_health_degraded_when_tmux_dead() -> None:
    server = _serve(DeadTmuxAdapter)
    try:
        status, payload = _get(server, "/health")
        assert status == 200
        assert payload["ok"] is True
        assert payload["tmux_reachable"] is False
    finally:
        server.shutdown()


def test_data_endpoint_never_500_when_tmux_dead() -> None:
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


def test_resolve_instance_fail_closed_when_tmux_dead() -> None:
    server = _serve(DeadTmuxAdapter)
    try:
        status, payload = _get(server, "/tmux/resolve-instance?instance_id=x")
        assert status == 200
        # Resolution errors are still enveloped at 200, never a 500.
        assert payload["ok"] is False
    finally:
        server.shutdown()


def test_send_gated_envelope() -> None:
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


def test_send_keys_gated_envelope() -> None:
    # send-keys goes through adapter.run(), which SUPPRESSES SILENTLY (no raise);
    # the handler must notice last_send_gate_result and surface the gated envelope
    # instead of falsely reporting sent:True.
    server = _serve(SendKeysGatedAdapter)
    try:
        status, payload = _post(server, "/tmux/send-keys", {"pane": "%1", "command": "hi"})
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"] == "gated"
        assert payload["error"]["detail"] == SendKeysGatedAdapter.GATE
    finally:
        server.shutdown()
