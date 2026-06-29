from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
from pathlib import Path

CLI_TOOLS = Path(__file__).resolve().parents[1]
QUEUE = CLI_TOOLS / "bin" / "generic-token-api-durable-retry-outbox"
WATCHDOG = CLI_TOOLS / "Shell" / "tokenapi-watchdog"
GENERIC_HOOK = Path(__file__).resolve().parents[2] / "claude-config" / "hooks" / "generic-hook.sh"
CODEX_HOOK = CLI_TOOLS / "scripts" / "codex-hook-bridge.sh"
STOP_VALIDATOR = (
    Path(__file__).resolve().parents[2] / "claude-config" / "hooks" / "stop-validator.sh"
)
PERSONA_SEAT = CLI_TOOLS / "scripts" / "persona-seat.sh"


def _env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX_DB"] = str(tmp_path / "queue.sqlite3")
    env["GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX_LOG"] = str(tmp_path / "queue.log")
    return env


def _run_queue(
    tmp_path: Path, *args: str, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(QUEUE), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=_env(tmp_path),
        check=False,
    )


class _Server:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.received: list[tuple[str, dict]] = []
        self.statuses = statuses or []

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.received.append((self.path, json.loads(body or b"{}")))
                status = outer.statuses.pop(0) if outer.statuses else 200
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b'{"success":true}')

            def log_message(self, *a) -> None:
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/hooks"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)


def test_enqueue_is_idempotent_by_action_and_session_id(tmp_path: Path) -> None:
    payload = '{"session_id":"sid-1","x":1}'
    first = _run_queue(
        tmp_path,
        "enqueue",
        "--action-type",
        "SessionStart",
        "--url",
        "http://127.0.0.1:9/api/hooks/SessionStart",
        input_text=payload,
    )
    second = _run_queue(
        tmp_path,
        "enqueue",
        "--action-type",
        "SessionStart",
        "--url",
        "http://127.0.0.1:9/api/hooks/SessionStart",
        input_text=payload,
    )
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["queued"] is True
    assert json.loads(second.stdout)["duplicate"] is True
    status = _run_queue(tmp_path, "status")
    assert json.loads(status.stdout) == {"pending": 1}


def test_drain_replays_in_insert_order_and_marks_done(tmp_path: Path) -> None:
    server = _Server()
    try:
        for action, payload in [
            ("SessionStart", '{"session_id":"sid-a"}'),
            ("WrapperEnd", '{"wrapper_launch_id":"wrap-b"}'),
        ]:
            res = _run_queue(
                tmp_path,
                "enqueue",
                "--action-type",
                action,
                "--url",
                f"{server.url}/{action}",
                input_text=payload,
            )
            assert res.returncode == 0, res.stderr
        drain = _run_queue(tmp_path, "drain")
        assert drain.returncode == 0, drain.stderr
        assert json.loads(drain.stdout) == {"examined": 2, "failed": 0, "replayed": 2}
        assert [p for p, _ in server.received] == [
            "/api/hooks/SessionStart",
            "/api/hooks/WrapperEnd",
        ]
        assert _run_queue(tmp_path, "status").stdout.strip() == '{"done": 2}'
    finally:
        server.close()


def test_drain_stops_on_unreachable_preserving_order(tmp_path: Path) -> None:
    # Port 1 is unreachable; the following live item must not leapfrog it.
    first = _run_queue(
        tmp_path,
        "enqueue",
        "--action-type",
        "SessionStart",
        "--url",
        "http://127.0.0.1:1/api/hooks/SessionStart",
        input_text='{"session_id":"sid-dead"}',
    )
    assert first.returncode == 0, first.stderr
    server = _Server()
    try:
        second = _run_queue(
            tmp_path,
            "enqueue",
            "--action-type",
            "WrapperEnd",
            "--url",
            f"{server.url}/WrapperEnd",
            input_text='{"wrapper_launch_id":"wrap-live"}',
        )
        assert second.returncode == 0, second.stderr
        drain = _run_queue(tmp_path, "drain", "--timeout", "0.2")
        assert drain.returncode == 0, drain.stderr
        assert json.loads(drain.stdout)["replayed"] == 0
        assert server.received == []
        assert json.loads(_run_queue(tmp_path, "status").stdout) == {"pending": 2}
    finally:
        server.close()


def test_http_non_2xx_is_loud_terminal_not_requeued(tmp_path: Path) -> None:
    server = _Server(statuses=[503])
    try:
        _run_queue(
            tmp_path,
            "enqueue",
            "--action-type",
            "SessionStart",
            "--url",
            f"{server.url}/SessionStart",
            input_text='{"session_id":"sid-503"}',
        )
        drain = _run_queue(tmp_path, "drain")
        assert drain.returncode == 2
        assert json.loads(drain.stdout) == {"examined": 1, "failed": 1, "replayed": 0}
        assert json.loads(_run_queue(tmp_path, "status").stdout) == {"failed": 1}
    finally:
        server.close()


def test_watchdog_drains_only_on_down_to_up_transition(tmp_path: Path) -> None:
    server = _Server()
    env = _env(tmp_path)
    env.update(
        {
            "HOME": str(tmp_path),
            "PATH": f"{CLI_TOOLS / 'bin'}:{os.environ.get('PATH', '')}",
            "TOKEN_API_HEARTBEAT_FILE": str(tmp_path / "heartbeat.json"),
            "TOKEN_API_WATCHDOG_STATE_FILE": str(tmp_path / "watchdog.state"),
            "TOKEN_API_WATCHDOG_LOG": str(tmp_path / "watchdog.log"),
            "TOKEN_API_STALE_THRESHOLD": "180",
        }
    )
    try:
        _run_queue(
            tmp_path,
            "enqueue",
            "--action-type",
            "SessionStart",
            "--url",
            f"{server.url}/SessionStart",
            input_text='{"session_id":"sid-recover"}',
        )
        (tmp_path / "watchdog.state").write_text("down\n")
        (tmp_path / "heartbeat.json").write_text("{}")
        res = subprocess.run([str(WATCHDOG)], text=True, capture_output=True, env=env, check=False)
        assert res.returncode == 0, res.stderr
        assert [p for p, _ in server.received] == ["/api/hooks/SessionStart"]
        # A second healthy tick is not a poll and does not drain again.
        res2 = subprocess.run([str(WATCHDOG)], text=True, capture_output=True, env=env, check=False)
        assert res2.returncode == 0, res2.stderr
        assert len(server.received) == 1
    finally:
        server.close()


def test_hook_sources_queue_only_http_000_not_prepost_abort() -> None:
    hook = GENERIC_HOOK.read_text(encoding="utf-8")
    wrapper = (CLI_TOOLS / "lib" / "agent-wrapper-common.sh").read_text(encoding="utf-8")
    codex = CODEX_HOOK.read_text(encoding="utf-8")
    stop_validator = STOP_VALIDATOR.read_text(encoding="utf-8")
    persona_seat = PERSONA_SEAT.read_text(encoding="utf-8")
    assert 'HTTP_CODE" == "000"' in hook
    assert "_enqueue_hook_token_api_post" in hook
    assert "http=?" in hook or "pre-POST" in hook
    assert "token_wrapper_enqueue_hook_post" in wrapper
    assert '"$http_code" == "000"' in wrapper
    assert "generic-token-api-durable-retry-outbox" in codex
    assert '"$http_code" == "000"' in codex
    assert "generic-token-api-durable-retry-outbox" in stop_validator
    assert 'HTTP_CODE" == "000"' in stop_validator
    assert "token_wrapper_enqueue_hook_post" in persona_seat
