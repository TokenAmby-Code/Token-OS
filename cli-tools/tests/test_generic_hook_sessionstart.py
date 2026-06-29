"""SessionStart registration must NOT silently swallow a failed/dropped POST.

The wound (2026-06-28, fleet-wide "registration down ~2.4h" + cold-start worker
deaths): a bare ``claude``/``codex`` launch registers ONLY through Claude Code's
``generic-hook.sh`` SessionStart hook — there is no dispatch warming and no other
re-registration leg until the next full ``tx restart``. A transient SessionStart
failure (503 fail-loud / DB-lock / timeout / connection-refused) was swallowed by
``trap 'exit 0' EXIT`` and an unchecked ``$RESPONSE``, permanently stranding the
pane with no row, no ``@INSTANCE_ID``, and — fatally — no error.

Fix under test (client-side, ``generic-hook.sh`` only; the server already
fail-louds via 503 + INSERT retry, see token-api/tests/test_session_start_*):
1. The SessionStart POST captures BOTH the HTTP status and body and validates a
   2xx + ``{"success": true}`` reply. ``curl -s`` does not fail on a 503, so the
   server's bounded fail-loud would otherwise be swallowed exactly like a 200.
2. The ``trap 'exit 0'`` is GATED: a SessionStart that never confirms a bound row
   exits NON-ZERO and writes a durable, greppable failure record (token-api may
   itself be down, so the signal must not depend on it) plus a stderr line.
3. Every OTHER hook stays best-effort exit-0 (never blocks Claude Code).

These are pure subprocess invocations of the shell hook with a FAKE/stub
token-api over loopback. They never touch live tmux, the runtime checkout, the
live DB, or the developer's real ``~/.claude`` (HOME is redirected to tmp). The
5-guarantee binding proof is a SEPARATE live manual verification.
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "claude-config" / "hooks" / "generic-hook.sh"


def _env(tmp_home: Path, api_url: str, fail_log: Path, action: str = "SessionStart") -> dict:
    # Minimal env: no IMPERIUM/CIVIC/TOKEN_OS roots and no cli-tools/bin on PATH,
    # so claude-cmd / pending-ui-flush resolve to the `false` no-op and the hook
    # performs zero tmux/pane side effects. HOME → tmp keeps every artifact (logs,
    # session-pid cache, ui-flush sweep) off the developer's real ~/.claude.
    return {
        "HOME": str(tmp_home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "TOKEN_API_URL": api_url,
        "SESSIONSTART_FAILURE_LOG": str(fail_log),
        "HOOK_ACTION_TYPE": action,
    }


def _run_hook(
    env: dict, payload: str = '{"session_id":"raw-test-uuid"}'
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class _StubServer:
    """Loopback token-api stub returning a fixed status + body for every POST."""

    def __init__(self, status: int, body: bytes) -> None:
        self.received: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0) or 0)
                self.rfile.read(length)
                outer.received.append(self.path)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a) -> None:  # silence
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        # shutdown() stops serve_forever() but leaves the listening socket bound;
        # server_close() releases the FD so repeated tests can't leak/port-bind.
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# SUCCESS: a confirmed 2xx + {"success": true} binds the row → exit 0, no log.
# --------------------------------------------------------------------------- #


def test_sessionstart_success_is_silent_exit_0(tmp_path: Path) -> None:
    server = _StubServer(
        200, b'{"success":true,"action":"registered","instance_id":"raw-test-uuid"}'
    )
    try:
        fail_log = tmp_path / "sessionstart-failures.log"
        res = _run_hook(_env(tmp_path, server.url, fail_log))
        assert res.returncode == 0, (res.returncode, res.stderr)
        assert server.received == ["/api/hooks/SessionStart"]
        # A confirmed registration must NEVER record a failure.
        assert not fail_log.exists(), fail_log.read_text() if fail_log.exists() else ""
    finally:
        server.stop()


# --------------------------------------------------------------------------- #
# FAILURE IS VISIBLE: the server fail-louds (503) → non-zero exit + durable log.
# --------------------------------------------------------------------------- #


def test_sessionstart_503_failloud_is_visible(tmp_path: Path) -> None:
    server = _StubServer(503, b'{"detail":"SessionStart registration write failed [db-locked]"}')
    try:
        fail_log = tmp_path / "sessionstart-failures.log"
        res = _run_hook(_env(tmp_path, server.url, fail_log))
        # NOT silently swallowed: the gated trap surfaces a non-zero exit.
        assert res.returncode != 0, "503 fail-loud was swallowed as exit 0"
        # Durable, greppable record — does not depend on the (possibly-down) api.
        assert fail_log.exists(), "no visible failure record written"
        body = fail_log.read_text()
        assert "FAILED" in body and "503" in body, body
        # Interactive surface: stderr names the failure + where to look.
        assert "FAILED" in res.stderr, res.stderr
    finally:
        server.stop()


def test_sessionstart_conn_refused_is_visible_and_bounded(tmp_path: Path) -> None:
    # Bind+close an ephemeral port so it is guaranteed unused → connection-refused
    # on every retry (hard-coding port 1 is environment-dependent).
    fail_log = tmp_path / "sessionstart-failures.log"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        unused_port = sock.getsockname()[1]
    start = time.monotonic()
    res = _run_hook(_env(tmp_path, f"http://127.0.0.1:{unused_port}", fail_log))
    elapsed = time.monotonic() - start

    assert res.returncode != 0, "connection-refused was swallowed as exit 0"
    assert fail_log.exists(), "no visible failure record written"
    # Bounded: --retry-max-time 12 caps the window. Generous ceiling guards a hang.
    assert elapsed < 20, f"SessionStart hook not bounded: {elapsed:.1f}s"


# --------------------------------------------------------------------------- #
# NON-CRITICAL hooks stay best-effort: a down server must NOT make them loud.
# --------------------------------------------------------------------------- #


def test_noncritical_hook_stays_exit_0_when_api_down(tmp_path: Path) -> None:
    fail_log = tmp_path / "sessionstart-failures.log"
    res = _run_hook(
        _env(tmp_path, "http://127.0.0.1:1", fail_log, action="Stop"), payload='{"session_id":"x"}'
    )
    assert res.returncode == 0, (res.returncode, res.stderr)
    # Only SessionStart owns the registration failure log.
    assert not fail_log.exists()


# --------------------------------------------------------------------------- #
# Source guards: the gated trap + bounded retry must stay present.
# --------------------------------------------------------------------------- #


def test_trap_is_gated_in_source() -> None:
    src = HOOK.read_text(encoding="utf-8")
    # The blanket `trap 'exit 0' EXIT` must be gone — replaced by a gated handler.
    assert "trap 'exit 0' EXIT" not in src, "blanket exit-0 trap still masks SessionStart"
    assert "REGISTRATION_OK" in src
    assert "SESSIONSTART_CRITICAL" in src


def test_bounded_retry_flags_present_in_source() -> None:
    src = HOOK.read_text(encoding="utf-8")
    for flag in ("--retry", "--retry-connrefused", "--retry-delay", "--retry-max-time"):
        assert flag in src, f"missing bounded-retry flag {flag}"


# --------------------------------------------------------------------------- #
# REG-ROOT: raw SessionStart must be SELF-SUFFICIENT. A transient pre-POST
# subprocess-spawn / resource failure (EMFILE, fork-exhaustion, a
# token-api-restart fd-pressure race, a stale-NAS $HOME) must NOT abort the hook
# under `set -euo pipefail` before the registration curl — the sole registration
# leg of a bare launch. Pinned root: an unguarded `mkdir`/`echo >` aborted
# pre-POST, so no row / no @INSTANCE_ID / no persona/tint were ever created.
# --------------------------------------------------------------------------- #


def _failing_mkdir_bin(tmp_path: Path) -> Path:
    """A `mkdir` shadow that fails like an EMFILE / fork-exhaustion spawn would.
    Prepended to PATH so every mkdir the hook runs pre-POST hits it."""
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    fail_mkdir = stub_bin / "mkdir"
    fail_mkdir.write_text(
        "#!/bin/bash\necho 'mkdir: fork: Resource temporarily unavailable' >&2\nexit 1\n"
    )
    fail_mkdir.chmod(0o755)
    return stub_bin


def test_sessionstart_reaches_post_despite_transient_prepost_spawn_failure(
    tmp_path: Path,
) -> None:
    # The logs dir already exists (the common case): only the transient spawn
    # fails, not a persistent inability to create it. curl -o writes its response
    # file under this dir, so it must be present.
    (tmp_path / ".claude" / "logs").mkdir(parents=True)
    stub_bin = _failing_mkdir_bin(tmp_path)

    server = _StubServer(
        200, b'{"success":true,"action":"registered","instance_id":"raw-test-uuid"}'
    )
    try:
        fail_log = tmp_path / "sessionstart-failures.log"
        env = _env(tmp_path, server.url, fail_log)
        # Shadow `mkdir` first on PATH so the unguarded-spawn abort would fire.
        env["PATH"] = f"{stub_bin}:{env['PATH']}"
        res = _run_hook(env)
        # Self-sufficient: the registration POST landed despite the mkdir failure.
        # (Before the fix the hook aborted under errexit and the POST never
        # reached the server — a silent strand.)
        assert server.received == ["/api/hooks/SessionStart"], (
            "transient pre-POST mkdir failure aborted the hook before registration"
        )
        # A confirmed registration → clean exit 0, no failure record.
        assert res.returncode == 0, (res.returncode, res.stderr)
        assert not fail_log.exists(), fail_log.read_text() if fail_log.exists() else ""
    finally:
        server.stop()


def test_prepost_writes_are_guarded_in_source() -> None:
    src = HOOK.read_text(encoding="utf-8")
    # The reg-critical marker must be set from the env BEFORE any subprocess spawn,
    # so even an abort during early setup is loud (gated trap) — never a silent
    # exit-0 strand.
    assert '[[ "${HOOK_ACTION_TYPE:-}" == "SessionStart" ]] && SESSIONSTART_CRITICAL=1' in src, (
        "SESSIONSTART_CRITICAL is not marked from the env before the first spawn"
    )
    # The pre-POST logs + session-pid-cache writes must be guarded so a transient
    # spawn/resource failure cannot abort before the registration curl.
    assert 'mkdir -p "${HOME}/.claude/logs" 2>/dev/null || true' in src
    assert 'mkdir -p "${HOME}/.claude/session-pids" 2>/dev/null || true' in src
    assert (
        'echo "$SESSION_ID" > "${HOME}/.claude/session-pids/${CLAUDE_PID}" 2>/dev/null || true'
        in src
    )
