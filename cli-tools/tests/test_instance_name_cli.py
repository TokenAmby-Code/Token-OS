from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "instance-name"


class H(BaseHTTPRequestHandler):
    received = None

    def do_PATCH(self):
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
        type(self).received = {"method": "PATCH", "path": self.path, "payload": payload}
        body = json.dumps(
            {
                "status": "renamed",
                "instance_id": self.path.split("/")[3],
                "tab_name": payload["tab_name"],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).received = {"method": "POST", "path": self.path}
        self.send_response(500)
        self.end_headers()

    def log_message(self, *a):
        return


def _tmux(tmp_path, iid="abcdef1234567890"):
    b = tmp_path / "bin"
    b.mkdir()
    t = b / "tmux"
    t.write_text(
        f'#!/usr/bin/env bash\nif [[ "$1" == "show-options" ]]; then printf "%s\\n" "${{FAKE_INSTANCE_ID-{iid}}}"; exit 0; fi\nexit 0\n'
    )
    t.chmod(0o755)
    return b


def _run(args, env=None):
    return subprocess.run(
        [str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def test_instance_name_cli_patches_instance_id_from_tmux_pane(tmp_path):
    H.received = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    b = _tmux(tmp_path)
    try:
        p = _run(
            ["anti-archaeology-cli", "--no-ui"],
            env={
                "PATH": f"{b}:{os.environ.get('PATH', '')}",
                "TMUX_PANE": "%99",
                "TOKEN_API_URL": f"http://127.0.0.1:{srv.server_port}",
            },
        )
    finally:
        srv.shutdown()
        th.join(timeout=2)
    assert p.returncode == 0, p.stderr
    assert H.received == {
        "method": "PATCH",
        "path": "/api/instances/abcdef1234567890/rename",
        "payload": {"tab_name": "anti-archaeology-cli"},
    }


def test_instance_name_cli_explicit_id_uses_patch_not_pane_post():
    H.received = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        p = _run(
            ["explicit-name", "--id", "inst-explicit", "--no-ui"],
            env={"TOKEN_API_URL": f"http://127.0.0.1:{srv.server_port}"},
        )
    finally:
        srv.shutdown()
        th.join(timeout=2)
    assert p.returncode == 0, p.stderr
    assert H.received == {
        "method": "PATCH",
        "path": "/api/instances/inst-explicit/rename",
        "payload": {"tab_name": "explicit-name"},
    }


def test_instance_name_cli_requires_tmux_pane_without_explicit_id():
    p = _run(
        ["valid-name", "--no-ui"], env={"TOKEN_API_URL": "http://127.0.0.1:9", "TMUX_PANE": ""}
    )
    assert p.returncode == 2
    assert "TMUX_PANE" in p.stderr


def test_instance_name_cli_requires_pane_instance_id(tmp_path):
    b = _tmux(tmp_path)
    p = _run(
        ["valid-name", "--no-ui"],
        env={
            "PATH": f"{b}:{os.environ.get('PATH', '')}",
            "TMUX_PANE": "%99",
            "FAKE_INSTANCE_ID": "",
            "TOKEN_API_URL": "http://127.0.0.1:9",
        },
    )
    assert p.returncode == 1
    assert "@INSTANCE_ID" in p.stderr


def test_instance_name_cli_rejects_invalid_name_before_patching():
    p = _run(["Claude 13:14", "--no-ui"], env={"TOKEN_API_URL": "http://127.0.0.1:9"})
    assert p.returncode == 2
    assert "placeholder" in p.stderr
