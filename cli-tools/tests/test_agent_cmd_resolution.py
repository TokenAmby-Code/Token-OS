#!/usr/bin/env python3
"""Regression tests for agent-cmd's daemon-backed target resolution."""

import json
import os
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

AGENT_CMD = Path(__file__).parents[1] / "bin" / "agent-cmd"
INSTANCE_ID = "019f70ef-6325-7cf0-abf6-c4ea2d73e32e"


class ResolveHandler(BaseHTTPRequestHandler):
    mappings = {
        ("target", "palace:E"): "%21",
        ("target", "2:NE"): "%34",
        ("instance_id", INSTANCE_ID): "%21",
    }

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        result = ""
        if parsed.path == "/resolve-pane" and query.get("format") == ["physical"]:
            for key in ("target", "instance_id"):
                if key in query:
                    result = self.mappings.get((key, query[key][0]), "")
                    break
        payload = {"ok": bool(result)}
        if result:
            payload["result"] = result
        else:
            payload["error"] = {"code": "not_found"}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


class AgentCmdResolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ResolveHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.env = os.environ | {"TMUXCTLD_URL": f"http://127.0.0.1:{cls.server.server_port}"}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def resolve(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(AGENT_CMD), "--resolve-only", *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_page_cardinal_label_resolves(self) -> None:
        result = self.resolve("--pane", "palace:E")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "%21")

    def test_numeric_page_label_resolves(self) -> None:
        result = self.resolve("--pane", "2:NE")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "%34")

    def test_instance_resolves_through_same_ledger(self) -> None:
        result = self.resolve("--instance", INSTANCE_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "%21")

    def test_absent_label_fails_closed(self) -> None:
        result = self.resolve("--pane", "missing:seat")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pane target not found: missing:seat", result.stderr)


if __name__ == "__main__":
    unittest.main()
