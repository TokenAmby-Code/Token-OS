from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PING = ROOT / "bin" / "tmuxctld-ping"
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon


class RecordingHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8")
        self.__class__.calls.append(
            {
                "method": "POST",
                "path": self.path,
                "body": json.loads(body),
                "content_type": self.headers.get("Content-Type"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "11")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_GET(self):  # noqa: N802
        self.__class__.calls.append(
            {
                "method": "GET",
                "path": self.path,
                "body": None,
                "content_type": self.headers.get("Content-Type"),
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "11")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_args):
        return


def _serve():
    RecordingHandler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class StubAdapter:
    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:  # noqa: ARG002
        return ""


def _serve_tmuxctld():
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0),
        adapter_factory=StubAdapter,
        version="9.9.9",
        sha="deadbee",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.ready.wait(timeout=5), "server thread never signalled ready"
    return server


def test_tmuxctld_ping_posts_key_value_payload_to_configured_daemon_url() -> None:
    server = _serve()
    try:
        env = os.environ.copy()
        env["TMUXCTLD_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
        proc = subprocess.run(
            [str(PING), "POST", "/event", "event=pane-died", "pane=%42"],
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert RecordingHandler.calls == [
            {
                "method": "POST",
                "path": "/event",
                "body": {"event": "pane-died", "pane": "%42"},
                "content_type": "application/json",
            }
        ]
    finally:
        server.shutdown()


def test_tmuxctld_ping_get_encodes_key_values_as_query() -> None:
    server = _serve()
    try:
        env = os.environ.copy()
        env["TMUXCTLD_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
        proc = subprocess.run(
            [str(PING), "GET", "/resolve-pane", "target=mechanicus:2", "format=physical"],
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ""
        assert RecordingHandler.calls == [
            {
                "method": "GET",
                "path": "/resolve-pane?target=mechanicus%3A2&format=physical",
                "body": None,
                "content_type": None,
            }
        ]
    finally:
        server.shutdown()


def test_tmuxctld_ping_is_executable_and_contains_no_feature_routing() -> None:
    assert os.access(PING, os.X_OK)
    body = PING.read_text(encoding="utf-8")
    assert "tmux-pane-respawn" not in body
    assert "pane-died" not in body
    assert "@PANE_ID" not in body
    assert "@PANE_TYPE" not in body


def test_tmuxctld_ping_exits_nonzero_on_transport_failure() -> None:
    env = os.environ.copy()
    env["TMUXCTLD_URL"] = "http://127.0.0.1:1"
    env["TMUXCTLD_CONNECT_TIMEOUT"] = "0.2"
    env["TMUXCTLD_MAX_TIME"] = "0.5"
    proc = subprocess.run(
        [str(PING), "POST", "/event", "event=x"],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "tmuxctld-ping: POST /event failed" in proc.stderr


def test_typing_guard_arm_uses_control_plane_timeout_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-keystroke arm hook must not inherit the generic 3s ping ceiling."""

    calls: list[list[str]] = []

    def slow_arm_main(argv: list[str] | None = None) -> int:
        calls.append(list(argv or []))
        time.sleep(3.4)
        print(json.dumps({"kind": "human", "until": 400, "active": True, "marker": "⌨"}))
        return 0

    monkeypatch.setattr(daemon.typing_guard_state, "main", slow_arm_main)
    monkeypatch.setattr(daemon, "_schedule_typing_guard_expiry_rehydrate", lambda _payload: None)
    server = _serve_tmuxctld()
    try:
        env = os.environ.copy()
        env["TMUXCTLD_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
        env["TMUXCTLD_MAX_TIME"] = "0.5"
        proc = subprocess.run(
            [
                str(PING),
                "POST",
                "/typing-guard-state",
                "cmd=arm",
                "pane=%42",
                "seconds=300",
                "now=100",
            ],
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 0, proc.stderr
        assert calls and calls[0][0] == "arm"
    finally:
        server.shutdown()
