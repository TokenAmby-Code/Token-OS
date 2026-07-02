from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "cli-tools" / "lib" / "tmux-guard.sh"
TMUX_SHIM = REPO / "cli-tools" / "bin" / "tmux"


class _GuardHandler(BaseHTTPRequestHandler):
    active = False
    seen: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append(payload)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {"active": type(self).active, "kind": "human" if type(self).active else "off"}
            ).encode()
        )

    def log_message(self, *_: object) -> None:
        return


def _server(active: bool):
    _GuardHandler.active = active
    _GuardHandler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _GuardHandler)
    thread = Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread


def _fake_tmux(tmp_path: Path) -> Path:
    fake = tmp_path / "tmux"
    fake.write_text(
        textwrap.dedent(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            case "${1:-}" in
              send-keys|send-key|send)
                echo "ALLOW=${TMUX_SEND_GATE_ALLOW:-} SEND $*" >> "${FAKE_TMUX_SENT}"
                ;;
              display-message)
                if [[ "$*" == *"#{pane_id}"* ]]; then echo "%1"; fi
                ;;
              *) : ;;
            esac
            """
        ).strip()
        + "\n"
    )
    fake.chmod(0o755)
    return fake


def _env(tmp_path: Path, url: str) -> dict[str, str]:
    sent = tmp_path / "sent.log"
    sent.write_text("")
    fake = _fake_tmux(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            "FAKE_TMUX_SENT": str(sent),
            "TMUX_GUARD_REAL_TMUX": str(fake),
            "IMPERIUM_TMUX_BIN": str(fake),
            "TMUX_GUARD_LOG": str(tmp_path / "guard.jsonl"),
            "TMUXCTLD_URL": url,
            "TMUX_GUARD_PYTHON": sys.executable,
        }
    )
    return env


def _bash(
    script: str, env: dict[str, str], timeout: float = 5.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", script],
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def test_shell_guard_reads_daemon_status_directly(tmp_path: Path) -> None:
    server, thread = _server(active=True)
    try:
        env = _env(tmp_path, f"http://127.0.0.1:{server.server_port}")
        proc = _bash(f'source "{GUARD}"; tmux_typing_guard_active %1', env)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert proc.returncode == 0
    assert _GuardHandler.seen[-1] == {"cmd": "status", "pane": "%1"}


def test_shell_guard_blocks_without_writing_when_daemon_reports_active(tmp_path: Path) -> None:
    server, thread = _server(active=True)
    try:
        env = _env(tmp_path, f"http://127.0.0.1:{server.server_port}")
        proc = _bash(f'source "{GUARD}"; tmux_send_guarded -t %1 -l peer-bytes', env)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert proc.returncode == 1
    assert "typing guard active" in proc.stderr
    assert Path(env["FAKE_TMUX_SENT"]).read_text() == ""


def test_shell_guard_sends_when_daemon_reports_clear(tmp_path: Path) -> None:
    server, thread = _server(active=False)
    try:
        env = _env(tmp_path, f"http://127.0.0.1:{server.server_port}")
        proc = _bash(f'source "{GUARD}"; tmux_send_guarded -t %1 -l peer-bytes', env)
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert proc.returncode == 0
    assert "SEND send-keys -t %1 -l peer-bytes" in Path(env["FAKE_TMUX_SENT"]).read_text()


def test_tmux_shim_uses_same_daemon_backed_guard(tmp_path: Path) -> None:
    server, thread = _server(active=False)
    try:
        env = _env(tmp_path, f"http://127.0.0.1:{server.server_port}")
        proc = subprocess.run(
            ["bash", str(TMUX_SHIM), "send-keys", "-t", "%1", "peer-bytes"],
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert proc.returncode == 0
    assert "SEND send-keys -t %1 peer-bytes" in Path(env["FAKE_TMUX_SENT"]).read_text()
