from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "instance-name"


class _RenameHandler(BaseHTTPRequestHandler):
    received: dict | None = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).received = {"path": self.path, "payload": payload}
        body = json.dumps(
            {"status": "renamed", "instance_id": "abcdef1234567890", "tab_name": payload["tab_name"]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _run(args, *, env=None):
    merged_env = {**os.environ, **(env or {})}
    return subprocess.run(
        [str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
        env=merged_env,
    )


def test_instance_name_cli_posts_tmux_pane_and_name():
    _RenameHandler.received = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RenameHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proc = _run(
            ["anti-archaeology-cli"],
            env={
                "TMUX_PANE": "%99",
                "TOKEN_API_URL": f"http://127.0.0.1:{server.server_port}",
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert proc.returncode == 0, proc.stderr
    assert "Renamed to: anti-archaeology-cli" in proc.stdout
    assert _RenameHandler.received == {
        "path": "/api/instance/rename",
        "payload": {"tmux_pane": "%99", "tab_name": "anti-archaeology-cli"},
    }


def test_instance_name_cli_requires_tmux_pane():
    env = {"TOKEN_API_URL": "http://127.0.0.1:9"}
    env["TMUX_PANE"] = ""
    proc = _run(["valid-name"], env=env)

    assert proc.returncode == 2
    assert "TMUX_PANE" in proc.stderr


def test_instance_name_cli_rejects_invalid_name_before_posting():
    proc = _run(["Claude 13:14"], env={"TMUX_PANE": "%99", "TOKEN_API_URL": "http://127.0.0.1:9"})

    assert proc.returncode == 2
    assert "placeholder" in proc.stderr
