from __future__ import annotations

import argparse
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .service import TmuxControlPlane


class TmuxctldServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 7778, control: Any | None = None):
        self.host = host
        self.port = port
        self.control = control if control is not None else TmuxControlPlane()
        self._httpd = self._make_httpd()
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def _make_httpd(self) -> ThreadingHTTPServer:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "tmuxctld/0.1"

            def log_message(self, fmt: str, *args: object) -> None:
                return

            def _json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib hook
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query)
                try:
                    if parsed.path == "/health":
                        self._json(200, {"ok": True, "service": "tmuxctld"})
                        return
                    if parsed.path == "/resolve-instance":
                        instance_id = (qs.get("instance_id") or [""])[0].strip()
                        if not instance_id:
                            self._json(400, {"ok": False, "error": "missing instance_id"})
                            return
                        self._json(200, outer.control.resolve_instance(instance_id))
                        return
                    if parsed.path == "/resolve-pane":
                        target = (qs.get("target") or [""])[0].strip()
                        if not target:
                            self._json(400, {"ok": False, "error": "missing target"})
                            return
                        # JSON mirrors `tmuxctl resolve-pane --format json`.
                        resolved = outer.control.resolve_pane(target)
                        values: dict[str, str] = {}
                        for line in resolved.splitlines():
                            if ": " in line:
                                key, value = line.split(": ", 1)
                                values[key] = value
                        self._json(200, values)
                        return
                    if parsed.path == "/instance-id-for-pane":
                        pane = (qs.get("pane") or [""])[0].strip()
                        if not pane:
                            self._json(400, {"ok": False, "error": "missing pane"})
                            return
                        proc = subprocess.run(
                            [
                                outer.control.adapter.tmux_binary,
                                "show-options",
                                "-pv",
                                "-t",
                                pane,
                                "@INSTANCE_ID",
                            ],
                            capture_output=True,
                            text=True,
                            timeout=1,
                            check=False,
                        )
                        value = proc.stdout.strip() if proc.returncode == 0 else ""
                        self._json(200, {"pane": pane, "instance_id": value})
                        return
                    self._json(404, {"ok": False, "error": "not found"})
                except Exception as exc:  # fail closed at the contract boundary
                    self._json(500, {"ok": False, "error": str(exc)})

        return ThreadingHTTPServer((self.host, self.port), Handler)

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def start_in_thread(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def create_app(
    host: str = "127.0.0.1", port: int = 7778, control: Any | None = None
) -> TmuxctldServer:
    return TmuxctldServer(host=host, port=port, control=control)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tmuxctld")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7778)
    args = parser.parse_args(argv)
    create_app(host=args.host, port=args.port).serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
