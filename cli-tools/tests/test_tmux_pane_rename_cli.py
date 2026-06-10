from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "tmux-pane-rename"


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def do_PATCH(self):  # noqa: N802
        payload = self._read_json()
        type(self).requests.append({"method": "PATCH", "path": self.path, "payload": payload})
        body = json.dumps({"status": "renamed", "tab_name": "normalized-name"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        payload = self._read_json()
        type(self).requests.append({"method": "POST", "path": self.path, "payload": payload})
        body = json.dumps({"success": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def _make_fakes(tmp_path: Path, *, instance_id="inst-123") -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    lib_dir = tmp_path / "lib"
    bin_dir.mkdir()
    lib_dir.mkdir()
    (lib_dir / "nas-path.sh").write_text("")
    log = tmp_path / "agent.log"
    tmux = bin_dir / "tmux"
    tmux.write_text(
        f"""#!/usr/bin/env bash
if [[ "$1" == "show-options" ]]; then
  printf '%s\n' "${{FAKE_INSTANCE_ID-{instance_id}}}"
  exit 0
fi
if [[ "$1" == "display-message" ]]; then exit 0; fi
exit 0
"""
    )
    tmux.chmod(0o755)
    script = bin_dir / "tmux-pane-rename"
    script.write_text(SCRIPT.read_text())
    script.chmod(0o755)
    agent = bin_dir / "agent-cmd"
    agent.write_text(f"#!/usr/bin/env bash\nprintf '%s\n' \"$*\" >> {log}\n")
    agent.chmod(0o755)
    return {"bin": str(bin_dir), "script": str(script), "agent_log": str(log)}


def _serve():
    _Handler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_tmux_pane_rename_patches_instance_id_and_syncs_ui(tmp_path: Path):
    fakes = _make_fakes(tmp_path)
    server, thread = _serve()
    try:
        env = {
            **os.environ,
            "PATH": f"{fakes['bin']}:{os.environ.get('PATH', '')}",
            "TOKEN_API_URL": f"http://127.0.0.1:{server.server_port}",
        }
        proc = subprocess.run(
            [fakes["script"], "%123", "human name"],
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert proc.returncode == 0, proc.stderr
    assert _Handler.requests == [
        {
            "method": "PATCH",
            "path": "/api/instances/inst-123/rename",
            "payload": {"tab_name": "human name"},
        }
    ]
    assert Path(fakes["agent_log"]).read_text().strip() == "--pane %123 /rename normalized-name"


def test_tmux_pane_rename_empty_posts_naming_nudge_by_instance_id(tmp_path: Path):
    fakes = _make_fakes(tmp_path, instance_id="inst-empty")
    server, thread = _serve()
    try:
        env = {
            **os.environ,
            "PATH": f"{fakes['bin']}:{os.environ.get('PATH', '')}",
            "TOKEN_API_URL": f"http://127.0.0.1:{server.server_port}",
        }
        proc = subprocess.run(
            [fakes["script"], "%123", ""], text=True, capture_output=True, env=env, check=False
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert proc.returncode == 0, proc.stderr
    assert _Handler.requests == [
        {
            "method": "POST",
            "path": "/api/orchestrator/naming_nudge",
            "payload": {"instance_id": "inst-empty"},
        }
    ]


def test_tmux_pane_rename_missing_instance_id_fails_locally(tmp_path: Path):
    fakes = _make_fakes(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{fakes['bin']}:{os.environ.get('PATH', '')}",
        "FAKE_INSTANCE_ID": "",
        "TOKEN_API_URL": "http://127.0.0.1:9",
    }
    proc = subprocess.run(
        [fakes["script"], "%123", "name"], text=True, capture_output=True, env=env, check=False
    )

    assert proc.returncode == 1
