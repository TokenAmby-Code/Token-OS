from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "cli-tools" / "lib" / "tmux-guard.sh"
AGENT_CMD = REPO / "cli-tools" / "bin" / "agent-cmd"
BRIEF = REPO / "cli-tools" / "bin" / "brief"
TMUX_SHIM = REPO / "cli-tools" / "bin" / "tmux"
STATUS = REPO / "cli-tools" / "bin" / "tmux-typing-guard-status"


def _fake_tmux(tmp_path: Path) -> Path:
    fake = tmp_path / "tmux"
    fake.write_text(
        textwrap.dedent(
            """
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> "${FAKE_TMUX_CALLS}"
            case "${1:-}" in
              capture-pane)
                cat "${FAKE_TMUX_CAPTURE}"
                ;;
              display-message)
                if [[ "$*" == *"#{client_activity}"* ]]; then echo "${FAKE_TMUX_CLIENT_ACTIVITY:-}";
                elif [[ "$*" == *"#{?pane_active,1,0}#{?window_active,1,0}"* ]]; then echo "${FAKE_TMUX_ATTENDED:-11}";
                elif [[ "$*" == *"#{pane_id}	#{session_name}"* ]]; then printf '%%1\ts\t0\tw\t0\t80\t24\tbash\t/dev/ttys001\t1\n';
                elif [[ "$*" == *"#{session_name}:#{window_index}"* ]]; then echo "s:0";
                elif [[ "$*" == *"#{pane_pid}"* ]]; then echo "1";
                elif [[ "$*" == *"#{pane_id}"* ]]; then echo "%1"; else echo ""; fi
                ;;
              list-clients)
                echo "${FAKE_TMUX_TARGET_CLIENT_ACTIVITY:-${FAKE_TMUX_CLIENT_ACTIVITY:-}}"
                ;;
              list-panes)
                echo "%1"
                ;;
              list-windows)
                echo -e "s\t0\tw"
                ;;
              show-options)
                exit 1
                ;;
              set-option)
                exit 0
                ;;
              send-keys|send-key|send)
                echo "ALLOW=${TMUX_SEND_GATE_ALLOW:-} SEND $*" >> "${FAKE_TMUX_SENT}"
                ;;
              *)
                ;;
            esac
            """
        ).strip()
        + "\n"
    )
    fake.chmod(0o755)
    return fake


def _env(tmp_path: Path, capture_text: str) -> dict[str, str]:
    capture = tmp_path / "capture.txt"
    capture.write_text(capture_text)
    calls = tmp_path / "calls.log"
    sent = tmp_path / "sent.log"
    calls.write_text("")
    sent.write_text("")
    fake = _fake_tmux(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            "FAKE_TMUX_CAPTURE": str(capture),
            "FAKE_TMUX_CALLS": str(calls),
            "FAKE_TMUX_SENT": str(sent),
            "TMUX_GUARD_REAL_TMUX": str(fake),
            "TMUX_GUARD_STATE_DIR": str(tmp_path / "guard-state"),
            "TMUX_GUARD_LOG": str(tmp_path / "guard.jsonl"),
            "TMUX_GUARD_TTL": "300",
            "TMUX_SEND_GATE_POLICY": "pierce",
        }
    )
    return env


def _bash(
    script: str, env: dict[str, str], timeout: float = 2.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", script],
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout,
        check=False,
    )


def test_wait_for_clear_blocks_loudly_and_structured_logs(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft\n")
    env["TMUX_GUARD_NOW"] = "1000"
    proc = _bash(f'source "{GUARD}"; tmux_wait_for_clear %1 0', env)

    assert proc.returncode == 1
    assert "tmux-guard: BLOCKED send-keys to %1" in proc.stderr
    records = [json.loads(line) for line in Path(env["TMUX_GUARD_LOG"]).read_text().splitlines()]
    assert records[-1]["event"] == "blocked"
    assert records[-1]["pane"] == "%1"
    assert records[-1]["reason"] == "user_input_pending"


def test_stamp_once_no_sliding_window_and_hard_ttl_self_heals(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft keeps changing\n")
    script = f'''
      set +e
      source "{GUARD}"
      TMUX_GUARD_NOW=1000 tmux_wait_for_clear %1 0; echo first:$?
      TMUX_GUARD_NOW=1100 tmux_wait_for_clear %1 0; echo second:$?
      TMUX_GUARD_NOW=1301 tmux_wait_for_clear %1 0; echo after_ttl:$?
      TMUX_GUARD_NOW=1302 tmux_wait_for_clear %1 0; echo still_after_ttl:$?
      cat "$(tmux_guard_stamp_file %1)"
    '''
    proc = _bash(script, env)

    assert proc.returncode == 0
    assert "first:1" in proc.stdout
    assert "second:1" in proc.stdout
    assert "after_ttl:0" in proc.stdout
    assert "still_after_ttl:0" in proc.stdout
    assert "started_at=1000" in proc.stdout
    assert "state=expired" in proc.stdout


def test_empty_or_just_submitted_pane_clears_stamp_and_never_blocks(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft\n")
    capture = Path(env["FAKE_TMUX_CAPTURE"])
    script = f'''
      set +e
      source "{GUARD}"
      TMUX_GUARD_NOW=1000 tmux_wait_for_clear %1 0; echo dirty:$?
      printf '> \n' > "{capture}"
      TMUX_GUARD_NOW=1001 tmux_wait_for_clear %1 0; echo submitted:$?
      if [[ -e "$(tmux_guard_stamp_file %1)" ]]; then echo stamp:present; else echo stamp:gone; fi
      TMUX_GUARD_NOW=1002 tmux_wait_for_clear %1 0; echo empty_again:$?
    '''
    proc = _bash(script, env)

    assert proc.returncode == 0
    assert "dirty:1" in proc.stdout
    assert "submitted:0" in proc.stdout
    assert "stamp:gone" in proc.stdout
    assert "empty_again:0" in proc.stdout


def test_freshly_cleared_claude_prompt_after_clear_echo_drops_stamp(tmp_path: Path) -> None:
    env = _env(tmp_path, "❯\u00a0draft\n")
    capture = Path(env["FAKE_TMUX_CAPTURE"])
    script = f'''
      set +e
      source "{GUARD}"
      TMUX_GUARD_NOW=1000 tmux_wait_for_clear %1 0; echo dirty:$?
      cat > "{capture}" <<'EOF'
❯ /clear
❯\u00a0
────────────────────────────────────────────────────────────────────────────────
  ... 0/200k $0.00
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
EOF
      TMUX_GUARD_NOW=1001 tmux_wait_for_clear %1 0; echo cleared:$?
      if [[ -e "$(tmux_guard_stamp_file %1)" ]]; then echo stamp:present; else echo stamp:gone; fi
    '''
    proc = _bash(script, env)

    assert proc.returncode == 0
    assert "dirty:1" in proc.stdout
    assert "cleared:0" in proc.stdout
    assert "stamp:gone" in proc.stdout


def test_status_segment_uses_global_recent_client_activity(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft\n")
    now = int(time.time())
    env["FAKE_TMUX_CLIENT_ACTIVITY"] = str(now)
    first = subprocess.run(
        [sys.executable, str(STATUS), "--plain"],
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )
    env["FAKE_TMUX_CLIENT_ACTIVITY"] = str(now - 2)
    second = subprocess.run(
        [sys.executable, str(STATUS), "--plain"],
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )
    Path(env["FAKE_TMUX_CAPTURE"]).write_text("> \n")
    env["FAKE_TMUX_CLIENT_ACTIVITY"] = str(now - 60)
    env["FAKE_TMUX_TARGET_CLIENT_ACTIVITY"] = str(now - 60)
    expired = subprocess.run(
        [sys.executable, str(STATUS), "--plain"],
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )

    assert first.stdout == "TYPE"
    assert second.stdout == "TYPE"
    assert expired.stdout == ""


def test_tmux_send_guarded_does_not_silently_swallow_block(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft\n")
    env["TMUX_GUARD_NOW"] = "1000"
    proc = _bash(f'source "{GUARD}"; tmux_send_guarded -t %1 -l peer-bytes', env)

    assert proc.returncode == 1
    assert "tmux-guard: BLOCKED send-keys to %1" in proc.stderr
    assert Path(env["FAKE_TMUX_SENT"]).read_text() == ""


def test_tmux_guard_skip_escape_hatch_bypasses_block(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft\n")
    env["TMUX_GUARD_SKIP"] = "1"
    proc = _bash(f'source "{GUARD}"; tmux_send_guarded -t %1 -l peer-bytes', env)

    assert proc.returncode == 0
    assert "tmux-guard: BLOCKED" not in proc.stderr
    assert "SEND send-keys -t %1 -l peer-bytes" in Path(env["FAKE_TMUX_SENT"]).read_text()


def test_tmux_shim_delays_under_typing_guard_then_delivers(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft\n")
    env.pop("TMUX_SEND_GATE_POLICY", None)
    env["IMPERIUM_TMUX_BIN"] = env["TMUX_GUARD_REAL_TMUX"]
    env["FAKE_TMUX_TARGET_CLIENT_ACTIVITY"] = str(int(time.time()))
    capture = Path(env["FAKE_TMUX_CAPTURE"])
    proc = subprocess.run(
        [
            "bash",
            "-lc",
            f"(sleep 0.2; printf '> \\n' > '{capture}') & exec '{TMUX_SHIM}' send-keys -t %1 peer-bytes",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=3,
        check=False,
    )

    assert proc.returncode == 0
    sent = Path(env["FAKE_TMUX_SENT"]).read_text()
    assert "SEND send-keys -t %1 peer-bytes" in sent
    assert "ALLOW= SEND" in sent


def test_tmux_shim_cancel_policy_suppresses_without_writing(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft\n")
    env["TMUX_SEND_GATE_POLICY"] = "cancel"
    env["IMPERIUM_TMUX_BIN"] = env["TMUX_GUARD_REAL_TMUX"]
    env["FAKE_TMUX_TARGET_CLIENT_ACTIVITY"] = str(int(time.time()))
    proc = subprocess.run(
        ["bash", str(TMUX_SHIM), "send-keys", "-t", "%1", "peer-bytes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )

    assert proc.returncode == 0
    assert Path(env["FAKE_TMUX_SENT"]).read_text() == ""


def test_tmux_shim_raw_read_is_unaffected(tmp_path: Path) -> None:
    env = _env(tmp_path, "visible\n")
    env["IMPERIUM_TMUX_BIN"] = env["TMUX_GUARD_REAL_TMUX"]
    env["IMPERIUM_TMUX_RAW"] = "1"
    env["FAKE_TMUX_TARGET_CLIENT_ACTIVITY"] = str(int(time.time()))
    proc = subprocess.run(
        ["bash", str(TMUX_SHIM), "capture-pane", "-t", "%1", "-p"],
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "visible\n"
    assert Path(env["FAKE_TMUX_SENT"]).read_text() == ""


def test_tmux_shim_empty_pane_ignores_unrelated_global_typing_gate(tmp_path: Path) -> None:
    env = _env(tmp_path, "> \n")
    env["IMPERIUM_TMUX_BIN"] = env["TMUX_GUARD_REAL_TMUX"]
    env.pop("TMUX_SEND_GATE_POLICY", None)
    env["FAKE_TMUX_CLIENT_ACTIVITY"] = str(int(time.time()))
    env["FAKE_TMUX_TARGET_CLIENT_ACTIVITY"] = str(int(time.time()) - 60)
    proc = subprocess.run(
        ["bash", str(TMUX_SHIM), "send-keys", "-t", "%1", "peer-bytes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=2,
        check=False,
    )

    assert proc.returncode == 0
    assert "tmux-guard: BLOCKED" not in proc.stderr
    sent = Path(env["FAKE_TMUX_SENT"]).read_text()
    assert "SEND send-keys -t %1 peer-bytes" in sent
    assert "ALLOW= SEND" in sent


def test_agent_cmd_queues_and_delivers_after_typing_guard_clears(tmp_path: Path) -> None:
    env = _env(tmp_path, "> draft\n")
    env.pop("TMUX_SEND_GATE_POLICY", None)
    env["IMPERIUM_TMUX_BIN"] = env["TMUX_GUARD_REAL_TMUX"]
    env["FAKE_TMUX_TARGET_CLIENT_ACTIVITY"] = str(int(time.time()))
    stub = tmp_path / "tmuxctl-stub"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'pane=""\n'
        'text=""\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        '    --pane) pane="$2"; shift 2 ;;\n'
        '    --text) text="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f'exec "{TMUX_SHIM}" send-keys -t "$pane" -l "$text"\n'
    )
    stub.chmod(0o755)
    env["TMUXCTL_BIN"] = str(stub)
    capture = Path(env["FAKE_TMUX_CAPTURE"])
    proc = subprocess.run(
        [
            "bash",
            "-lc",
            f"(sleep 0.2; printf '> \\n' > '{capture}') & exec '{AGENT_CMD}' --pane %1 'hello from peer'",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=4,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["verification_status"] == "sent"
    assert payload["pane"] == "%1"
    sent = Path(env["FAKE_TMUX_SENT"]).read_text()
    assert "hello from peer" in sent
    assert "ALLOW= SEND" in sent


class _BriefHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps(
                {
                    "status": "ok",
                    "delivered": 0,
                    "resolved": [
                        {
                            "pane_id": "%1",
                            "position_id": "palace:N",
                            "status": "blocked",
                            "reason": "send_gated:typing_guard",
                        }
                    ],
                    "unresolved": [],
                }
            ).encode()
        )

    def log_message(self, *_: object) -> None:
        return


def test_brief_surfaces_blocked_as_not_delivered(tmp_path: Path) -> None:
    server = HTTPServer(("127.0.0.1", 0), _BriefHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = os.environ.copy()
        env["TOKEN_API_URL"] = f"http://127.0.0.1:{server.server_port}"
        proc = subprocess.run(
            [sys.executable, str(BRIEF), "--pane", "%1", "msg"],
            text=True,
            capture_output=True,
            env=env,
            timeout=2,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert proc.returncode == 1
    assert "delivered=0/1" in proc.stdout
    assert "blocked" in proc.stderr
