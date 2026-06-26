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
STATE = REPO / "cli-tools" / "bin" / "tmux-typing-guard-state"


def _fake_tmux(tmp_path: Path) -> Path:
    fake = tmp_path / "tmux"
    fake.write_text(
        textwrap.dedent(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            echo "$*" >> "${FAKE_TMUX_CALLS}"
            key() { printf '%s' "$1" | sed 's/[^A-Za-z0-9_.:%-]/_/g'; }
            target=""
            prev=""
            for a in "$@"; do
              if [[ "$prev" == "-t" ]]; then target="$a"; fi
              prev="$a"
            done
            case "${1:-}" in
              display-message)
                if [[ "$*" == *"#{client_activity}"* ]]; then echo "${FAKE_TMUX_CLIENT_ACTIVITY:-}";
                elif [[ "$*" == *"#{?pane_active,1,0}#{?window_active,1,0}"* ]]; then echo "${FAKE_TMUX_ATTENDED:-11}";
                elif [[ "$*" == *"#{pane_id}    #{session_name}"* ]]; then printf '%%1\ts\t0\tw\t0\t80\t24\tbash\t/dev/ttys001\t1\n';
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
              show-options|show)
                if [[ "$*" == *"@TYPING_LOCK_UNTIL"* ]]; then
                  f="${FAKE_LOCK_DIR:-}/$(key "${target:-%1}")"
                  if [[ -n "${FAKE_LOCK_DIR:-}" && -f "$f" ]]; then cat "$f"; else echo "${FAKE_TYPING_LOCK_UNTIL:-}"; fi
                  exit 0
                fi
                if [[ "$*" == *"@TYPING_PENDING_UNTIL"* ]]; then
                  f="${FAKE_PENDING_DIR:-}/$(key "${target:-%1}")"
                  if [[ -n "${FAKE_PENDING_DIR:-}" && -f "$f" ]]; then cat "$f"; else echo "${FAKE_TYPING_PENDING_UNTIL:-}"; fi
                  exit 0
                fi
                exit 1
                ;;
              set-option|set)
                printf '%s\n' "$*" >> "${FAKE_TMUX_SETOPT}"
                exit 0
                ;;
              run-shell)
                printf '%s\n' "$*" >> "${FAKE_TMUX_RUNSHELL}"
                exit 0
                ;;
              send-keys|send-key|send)
                echo "ALLOW=${TMUX_SEND_GATE_ALLOW:-} SEND $*" >> "${FAKE_TMUX_SENT}"
                ;;
              *) ;;
            esac
            """
        ).strip()
        + "\n"
    )
    fake.chmod(0o755)
    return fake


def _env(tmp_path: Path) -> dict[str, str]:
    calls = tmp_path / "calls.log"
    sent = tmp_path / "sent.log"
    setopt = tmp_path / "setopt.log"
    runshell = tmp_path / "runshell.log"
    for path in (calls, sent, setopt, runshell):
        path.write_text("")
    fake = _fake_tmux(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            "FAKE_TMUX_CALLS": str(calls),
            "FAKE_TMUX_SENT": str(sent),
            "FAKE_TMUX_SETOPT": str(setopt),
            "FAKE_TMUX_RUNSHELL": str(runshell),
            "TMUX_GUARD_REAL_TMUX": str(fake),
            "IMPERIUM_TMUX_BIN": str(fake),
            "TMUX_GUARD_LOG": str(tmp_path / "guard.jsonl"),
            "TMUX_SEND_GATE_POLICY": "pierce",
            "TMUX_GUARD_PYTHON": sys.executable,
            # agent-cmd prefers the tmuxctld daemon; point it at a closed port so
            # these CLI/stub-path tests deterministically exercise the
            # daemon-unreachable fallback (direct tmuxctl via TMUXCTL_BIN),
            # regardless of whether a live daemon happens to be running.
            "TMUXCTLD_URL": "http://127.0.0.1:1",
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


def test_legacy_guard_no_longer_uses_stamp_files_or_prompt_scraping() -> None:
    text = GUARD.read_text(encoding="utf-8")
    forbidden = [
        "capture-pane",
        "tmux_guard_stamp_file",
        "tmux_guard_write_stamp",
        "tmux_guard_clear_stamp",
        "TMUX_GUARD_STATE_DIR",
        ".stamp",
        "prompt/input line",
    ]
    for needle in forbidden:
        assert needle not in text
    assert "tmuxctl.send_gate typing" in text


def test_no_guard_assignment_contains_literal_pending() -> None:
    for rel in (
        "cli-tools/tmux/tmux-base.conf",
        "cli-tools/bin/tmux-typing-guard-state",
        "cli-tools/lib/tmuxctl/typing_guard_state.py",
    ):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "⌨ PENDING" not in text
        assert '@GUARD" "PENDING' not in text


def test_state_helper_arm_sets_yellow_marker_without_sleep_poll(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(STATE), "arm", "--pane", "%1", "--seconds", "300", "--now", "1000"],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0
    setopt = Path(env["FAKE_TMUX_SETOPT"]).read_text()
    assert "@TYPING_LOCK_UNTIL 1300" in setopt
    assert "@TYPING_PENDING_UNTIL" in setopt and "-pu" in setopt
    guard_rows = [line for line in setopt.splitlines() if " @GUARD " in line]
    assert guard_rows == ["set-option -p -t %1 @GUARD #[fg=colour214,bold]⌨#[default]"]
    assert Path(env["FAKE_TMUX_RUNSHELL"]).read_text() == ""


def test_state_helper_pending_sets_red_marker_and_unsets_lock(tmp_path: Path) -> None:
    env = _env(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(STATE), "pending", "--pane", "%1", "--seconds", "15", "--now", "1000"],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )
    assert proc.returncode == 0
    setopt = Path(env["FAKE_TMUX_SETOPT"]).read_text()
    assert "@TYPING_PENDING_UNTIL 1015" in setopt
    assert "@TYPING_LOCK_UNTIL" in setopt and "-pu" in setopt
    guard_rows = [line for line in setopt.splitlines() if " @GUARD " in line]
    assert guard_rows == ["set-option -p -t %1 @GUARD #[fg=red,bold]⌨#[default]"]


def test_wait_for_clear_blocks_on_canonical_typing_guard_and_logs(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["FAKE_TYPING_LOCK_UNTIL"] = str(int(time.time()) + 200)
    proc = _bash(f'source "{GUARD}"; tmux_wait_for_clear %1 0', env)

    assert proc.returncode == 1
    assert "typing guard active" in proc.stderr
    records = [json.loads(line) for line in Path(env["TMUX_GUARD_LOG"]).read_text().splitlines()]
    assert records[-1]["event"] == "blocked"
    assert records[-1]["pane"] == "%1"
    assert records[-1]["reason"] == "typing_guard"
    assert "capture-pane" not in Path(env["FAKE_TMUX_CALLS"]).read_text()


def test_wait_for_clear_allows_when_canonical_guard_clear(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["FAKE_TYPING_LOCK_UNTIL"] = ""
    proc = _bash(f'source "{GUARD}"; tmux_wait_for_clear %1 0', env)

    assert proc.returncode == 0
    assert "BLOCKED" not in proc.stderr


def test_tmux_send_guarded_does_not_silently_swallow_block(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["FAKE_TYPING_PENDING_UNTIL"] = str(int(time.time()) + 200)
    proc = _bash(f'source "{GUARD}"; tmux_send_guarded -t %1 -l peer-bytes', env)

    assert proc.returncode == 1
    assert "typing guard active" in proc.stderr
    assert Path(env["FAKE_TMUX_SENT"]).read_text() == ""


def test_tmux_guard_skip_escape_hatch_bypasses_block(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["FAKE_TYPING_LOCK_UNTIL"] = str(int(time.time()) + 200)
    env["TMUX_GUARD_SKIP"] = "1"
    proc = _bash(f'source "{GUARD}"; tmux_send_guarded -t %1 -l peer-bytes', env)

    assert proc.returncode == 0
    assert "tmux-guard: BLOCKED" not in proc.stderr
    assert "SEND send-keys -t %1 -l peer-bytes" in Path(env["FAKE_TMUX_SENT"]).read_text()


def test_tmux_shim_delays_under_typing_guard_then_delivers(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.pop("TMUX_SEND_GATE_POLICY", None)
    env["FAKE_TYPING_LOCK_UNTIL"] = str(int(time.time()) + 1)
    proc = subprocess.run(
        ["bash", str(TMUX_SHIM), "send-keys", "-t", "%1", "peer-bytes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0
    sent = Path(env["FAKE_TMUX_SENT"]).read_text()
    assert "SEND send-keys -t %1 peer-bytes" in sent
    assert "ALLOW= SEND" in sent


def test_tmux_shim_cancel_policy_suppresses_without_writing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["TMUX_SEND_GATE_POLICY"] = "cancel"
    env["FAKE_TYPING_LOCK_UNTIL"] = str(int(time.time()) + 200)
    proc = subprocess.run(
        ["bash", str(TMUX_SHIM), "send-keys", "-t", "%1", "peer-bytes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )

    assert proc.returncode == 0
    assert Path(env["FAKE_TMUX_SENT"]).read_text() == ""


def test_tmux_shim_unlocked_pane_is_deliverable(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.pop("TMUX_SEND_GATE_POLICY", None)
    env["FAKE_TYPING_LOCK_UNTIL"] = ""
    proc = subprocess.run(
        ["bash", str(TMUX_SHIM), "send-keys", "-t", "%1", "peer-bytes"],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )

    assert proc.returncode == 0
    sent = Path(env["FAKE_TMUX_SENT"]).read_text()
    assert "SEND send-keys -t %1 peer-bytes" in sent
    assert "ALLOW= SEND" in sent


def test_agent_cmd_queues_and_delivers_after_typing_guard_clears(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.pop("TMUX_SEND_GATE_POLICY", None)
    env["FAKE_TYPING_LOCK_UNTIL"] = str(int(time.time()) + 1)
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
    proc = subprocess.run(
        [str(AGENT_CMD), "--pane", "%1", "hello from peer"],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["verification_status"] == "sent"
    sent = Path(env["FAKE_TMUX_SENT"]).read_text()
    assert "hello from peer" in sent
    assert "ALLOW= SEND" in sent


def test_agent_cmd_envelope_carries_recovery_fields_on_fallback(tmp_path: Path) -> None:
    """The agent-cmd JSON envelope always surfaces the swallowed-submit recovery
    fields the daemon exposes (memories: ack-is-hack, no-error-suppressing-debounce).
    On the daemon-unreachable fallback path they default to mouse-free safe values:
    the direct tmuxctl send neither holds the green agent guard nor sniffs for a
    swallowed submit, so it reports no hold, no detection, no recovery."""
    env = _env(tmp_path)
    env.pop("TMUX_SEND_GATE_POLICY", None)
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
    proc = subprocess.run(
        [str(AGENT_CMD), "--pane", "%1", "hello from peer"],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    # All four recovery keys present, never dropped from the envelope ...
    assert payload["guard_held"] is False
    assert payload["swallowed_submit_detected"] is False
    assert payload["recovery_attempts"] == 0
    assert payload["failures"] == []
    # ... and typed as a real array/bools/int, not quoted strings.
    assert isinstance(payload["failures"], list)
    assert isinstance(payload["guard_held"], bool)
    assert isinstance(payload["recovery_attempts"], int)


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
            timeout=15,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join(timeout=15)

    assert proc.returncode == 1
    assert "delivered=0/1" in proc.stdout
    assert "blocked" in proc.stderr
