from __future__ import annotations

import http.server
import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
OUTBOX = ROOT / "cli-tools" / "bin" / "generic-token-api-durable-retry-outbox"


def _outbox_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX_DB"] = str(tmp_path / "outbox.sqlite3")
    env["GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX_LOG"] = str(tmp_path / "outbox.log")
    return env


def _outbox(
    tmp_path: Path, *args: str, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(OUTBOX), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=_outbox_env(tmp_path),
        check=False,
    )


class _TokenApiBridge:
    def __init__(self, client: TestClient) -> None:
        self.calls: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.calls.append(self.path)
                resp = client.post(self.path, json=json.loads(body or b"{}"))
                self.send_response(resp.status_code)
                self.end_headers()
                self.wfile.write(resp.content)

            def log_message(self, *a) -> None:
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)


def test_dropped_sessionstart_ping_has_no_row_until_outbox_replay_then_identity_lands(
    app_env, tmp_path: Path, monkeypatch
) -> None:
    """Confirm W2 diagnosis and recovery without mutating the live registry.

    A Token-API-down SessionStart POST cannot create a row/tmux stamp/tint. The
    generic durable outbox preserves that exact hook intent; replaying it after
    recovery creates the row and runs the same SessionStart stamp/tint path.
    """

    hooks = sys.modules["routes.hooks"]
    shared = sys.modules["shared"]
    stamped: list[tuple[str, str]] = []
    tinted: list[tuple[str, str, str]] = []

    async def fake_subprocess(args, *, timeout=None, stdout=None, stderr=None, env=None):
        arglist = tuple(args)
        if arglist[:4] == ("tmux", "set-option", "-p", "-t") and "@INSTANCE_ID" in arglist:
            stamped.append((arglist[4], arglist[-1]))
        return subprocess.CompletedProcess(args=arglist, returncode=0, stdout=b"", stderr=b"")

    async def fake_tint(db, instance_id: str, pane: str, *, source: str):
        tinted.append((instance_id, pane, source))

    monkeypatch.setattr(hooks, "_run_subprocess_offloop", fake_subprocess)
    monkeypatch.setattr(shared, "apply_instance_pane_tint", fake_tint)

    payload = {
        "session_id": "queued-session",
        "cwd": str(tmp_path),
        "tmux_pane": "%outbox",
        "env": {"TOKEN_API_ENGINE": "claude"},
    }

    # Token-API down / dropped ping: no handler ran, therefore no row, stamp, or tint.
    enq = _outbox(
        tmp_path,
        "enqueue",
        "--action-type",
        "SessionStart",
        "--url",
        "http://127.0.0.1:1/api/hooks/SessionStart",
        input_text=json.dumps(payload),
    )
    assert enq.returncode == 0, enq.stderr
    conn = sqlite3.connect(app_env.db_path)
    assert (
        conn.execute("SELECT 1 FROM instances WHERE id = ?", (payload["session_id"],)).fetchone()
        is None
    )
    conn.close()
    assert stamped == []
    assert tinted == []

    # Recovery: replay the same hook intent through Token-API. Identity lands via
    # hook-owned registration; no agent self-registration/PATCH is involved.
    bridge = _TokenApiBridge(TestClient(app_env.main.app))
    try:
        # Rewrite the queued URL from the deliberately-dead proof endpoint to the
        # recovered dev bridge; this mirrors the same intent hitting a live server
        # without requiring a real port-7777 outage in unit tests.
        db = sqlite3.connect(tmp_path / "outbox.sqlite3")
        db.execute(
            "UPDATE hook_posts SET url = ? WHERE idempotency_key = ?",
            (f"{bridge.base}/api/hooks/SessionStart", "SessionStart:queued-session"),
        )
        db.commit()
        db.close()

        drain = _outbox(tmp_path, "drain")
        assert drain.returncode == 0, drain.stderr
        assert json.loads(drain.stdout) == {"examined": 1, "failed": 0, "replayed": 1}
    finally:
        bridge.close()

    conn = sqlite3.connect(app_env.db_path)
    row = conn.execute(
        "SELECT profile_name, legion FROM legacy_instances WHERE id = ?",
        (payload["session_id"],),
    ).fetchone()
    conn.close()
    assert row == ("blood-angels", "astartes")
    assert ("%outbox", payload["session_id"]) in stamped
    assert (payload["session_id"], "%outbox", "SessionStart") in tinted
