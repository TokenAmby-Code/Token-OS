from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "tmux-pane-rename"


class H(BaseHTTPRequestHandler):
    requests = []

    def _j(self):
        return json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode() or "{}"
        )

    def do_PATCH(self):
        type(self).requests.append({"method": "PATCH", "path": self.path, "payload": self._j()})
        body = json.dumps({"status": "renamed", "tab_name": "normalized-name"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        type(self).requests.append({"method": "POST", "path": self.path, "payload": self._j()})
        body = b'{"success":true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        return


def _fakes(tmp_path, iid="inst-123"):
    b = tmp_path / "bin"
    lib_dir = tmp_path / "lib"
    b.mkdir()
    lib_dir.mkdir()
    (lib_dir / "nas-path.sh").write_text("")
    log = tmp_path / "agent.log"
    (b / "tmux").write_text(
        f'#!/usr/bin/env bash\nif [[ "$1" == "show-options" ]]; then printf "%s\\n" "${{FAKE_INSTANCE_ID-{iid}}}"; exit 0; fi\nexit 0\n'
    )
    (b / "tmux").chmod(0o755)
    s = b / "tmux-pane-rename"
    s.write_text(SCRIPT.read_text())
    s.chmod(0o755)
    a = b / "agent-cmd"
    a.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {log}\n')
    a.chmod(0o755)
    return b, s, log


def _serve():
    H.requests = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, th


def test_tmux_pane_rename_patches_instance_id_and_syncs_ui(tmp_path):
    b, s, log = _fakes(tmp_path)
    srv, th = _serve()
    try:
        p = subprocess.run(
            [str(s), "%123", "human name"],
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "PATH": f"{b}:{os.environ.get('PATH', '')}",
                "TOKEN_API_URL": f"http://127.0.0.1:{srv.server_port}",
            },
            check=False,
        )
    finally:
        srv.shutdown()
        th.join(timeout=2)
    assert p.returncode == 0, p.stderr
    assert H.requests == [
        {
            "method": "PATCH",
            "path": "/api/instances/inst-123/rename",
            "payload": {"tab_name": "human name"},
        }
    ]
    assert log.read_text().strip() == "--pane %123 /rename normalized-name"


def test_tmux_pane_rename_empty_posts_naming_nudge_by_instance_id(tmp_path):
    b, s, _ = _fakes(tmp_path, "inst-empty")
    srv, th = _serve()
    try:
        p = subprocess.run(
            [str(s), "%123", ""],
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "PATH": f"{b}:{os.environ.get('PATH', '')}",
                "TOKEN_API_URL": f"http://127.0.0.1:{srv.server_port}",
            },
            check=False,
        )
    finally:
        srv.shutdown()
        th.join(timeout=2)
    assert p.returncode == 0, p.stderr
    assert H.requests == [
        {
            "method": "POST",
            "path": "/api/orchestrator/naming_nudge",
            "payload": {"instance_id": "inst-empty"},
        }
    ]


def test_tmux_pane_rename_missing_instance_id_fails_locally(tmp_path):
    b, s, _ = _fakes(tmp_path)
    p = subprocess.run(
        [str(s), "%123", "name"],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{b}:{os.environ.get('PATH', '')}",
            "FAKE_INSTANCE_ID": "",
            "TOKEN_API_URL": "http://127.0.0.1:9",
        },
        check=False,
    )
    assert p.returncode == 1
