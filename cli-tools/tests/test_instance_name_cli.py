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

    def do_PATCH(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        type(self).received = {"method": "PATCH", "path": self.path, "payload": payload}
        body = json.dumps(
            {
                "status": "renamed",
                "instance_id": self.path.split("/")[3],
                "tab_name": payload["tab_name"],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        type(self).received = {"method": "POST", "path": self.path}
        self.send_response(500)
        self.end_headers()

    def log_message(self, *_args):
        return


def _fake_tmux_bin(tmp_path: Path, *, instance_id: str = "abcdef1234567890") -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(
        f"""#!/usr/bin/env bash
if [[ "$1" == "show-options" ]]; then
  printf '%s\n' "${{FAKE_INSTANCE_ID-{instance_id}}}"
  exit 0
fi
exit 0
"""
    )
    tmux.chmod(0o755)
    return bin_dir


def _run(args, *, env=None):
    return subprocess.run(
        [str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, **(env or {})},
    )


def test_instance_name_cli_patches_instance_id_from_tmux_pane(tmp_path: Path):
    _RenameHandler.received = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RenameHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bin_dir = _fake_tmux_bin(tmp_path)
    try:
        proc = _run(
            ["anti-archaeology-cli", "--no-ui"],
            env={
                "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
                "TMUX_PANE": "%99",
                "TOKEN_API_URL": f"http://127.0.0.1:{server.server_port}",
            },
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert proc.returncode == 0, proc.stderr
    assert _RenameHandler.received == {
        "method": "PATCH",
        "path": "/api/instances/abcdef1234567890/rename",
        "payload": {"tab_name": "anti-archaeology-cli"},
    }


def test_instance_name_cli_explicit_id_uses_patch_not_pane_post():
    _RenameHandler.received = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RenameHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        proc = _run(
            ["explicit-name", "--id", "inst-explicit", "--no-ui"],
            env={"TOKEN_API_URL": f"http://127.0.0.1:{server.server_port}"},
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert proc.returncode == 0, proc.stderr
    assert _RenameHandler.received == {
        "method": "PATCH",
        "path": "/api/instances/inst-explicit/rename",
        "payload": {"tab_name": "explicit-name"},
    }


def test_instance_name_cli_requires_tmux_pane_without_explicit_id():
    proc = _run(
        ["valid-name", "--no-ui"], env={"TOKEN_API_URL": "http://127.0.0.1:9", "TMUX_PANE": ""}
    )
    assert proc.returncode == 2
    assert "TMUX_PANE" in proc.stderr


def test_instance_name_cli_requires_pane_instance_id(tmp_path: Path):
    bin_dir = _fake_tmux_bin(tmp_path)
    proc = _run(
        ["valid-name", "--no-ui"],
        env={
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "TMUX_PANE": "%99",
            "FAKE_INSTANCE_ID": "",
            "TOKEN_API_URL": "http://127.0.0.1:9",
        },
    )
    assert proc.returncode == 1
    assert "@INSTANCE_ID" in proc.stderr


def test_instance_name_cli_rejects_invalid_name_before_patching():
    proc = _run(["Claude 13:14", "--no-ui"], env={"TOKEN_API_URL": "http://127.0.0.1:9"})
    assert proc.returncode == 2
    assert "placeholder" in proc.stderr
