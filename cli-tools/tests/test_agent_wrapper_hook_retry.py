"""Tests for the bounded retry belt + failure-cause tally in token_wrapper_post_hook.

Socket activation is the primary defense against restart-window hook drops; this
client-side belt covers the residuals (backlog overflow, a request killed
mid-flight) and tags the cause so we can quantify drop sources. The belt must
stay fire-and-forget (always return 0) and bounded (never hang on a down server).
"""

from __future__ import annotations

import http.server
import subprocess
import threading
import time
from pathlib import Path

CLI_TOOLS = Path(__file__).resolve().parents[1]
COMMON = CLI_TOOLS / "lib" / "agent-wrapper-common.sh"


def _run_post_hook(
    api_url: str, fail_log: Path, action: str = "SessionStart"
) -> subprocess.CompletedProcess[str]:
    script = f"""
set +e
source {str(COMMON)!r}
API_URL={api_url!r}
TOKEN_WRAPPER_HOOK_FAILURE_LOG={str(fail_log)!r}
token_wrapper_post_hook {action!r} '{{"session_id":"t"}}'
echo "RC=$?"
"""
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True)


def test_post_hook_succeeds_against_live_server(tmp_path: Path) -> None:
    received: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            received.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"success":true}')

        def log_message(self, *a) -> None:  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        fail_log = tmp_path / "fail.log"
        res = _run_post_hook(f"http://127.0.0.1:{port}", fail_log)
        assert "RC=0" in res.stdout, res.stderr
        assert received == ["/api/hooks/SessionStart"]
        # A 200 must NOT record a failure.
        assert not fail_log.exists()
    finally:
        server.shutdown()


def test_post_hook_tags_conn_refused_and_is_bounded(tmp_path: Path) -> None:
    # Port 1 is reserved/unused → immediate connection-refused on every attempt.
    fail_log = tmp_path / "fail.log"
    start = time.monotonic()
    res = _run_post_hook("http://127.0.0.1:1", fail_log)
    elapsed = time.monotonic() - start

    # Fire-and-forget: always returns 0 even when the POST never lands.
    assert "RC=0" in res.stdout, res.stderr
    # Bounded: --retry-max-time 12 caps the window; refused is fast so it should
    # finish well under that. Generous ceiling guards against an unbounded hang.
    assert elapsed < 15, f"retry belt not bounded: {elapsed:.1f}s"
    # Cause tagged for the instrumentation tally.
    assert fail_log.exists()
    line = fail_log.read_text().strip()
    assert line.endswith("\tSessionStart\tconn-refused"), line


def test_retry_flags_present_in_source() -> None:
    src = COMMON.read_text(encoding="utf-8")
    for flag in ("--retry", "--retry-connrefused", "--retry-delay", "--retry-max-time"):
        assert flag in src, f"missing retry flag {flag}"
