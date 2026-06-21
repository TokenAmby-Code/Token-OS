from __future__ import annotations

import os
import pathlib
import subprocess
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
TX = ROOT / "bin" / "tx"


def _write_fake_tmux(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    fake = tmp_path / "tmux"
    fake.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    fake.chmod(0o755)
    return fake


def _nonce(tmp_path: pathlib.Path) -> tuple[str, str]:
    nonce_file = tmp_path / "nonce"
    nonce = "test-nonce"
    nonce_file.write_text(nonce)
    return str(nonce_file), nonce


def _run_watchdog(
    tmp_path: pathlib.Path, *, fake_tmux_body: str, client_tty: str = "/dev/ttys000"
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path, pathlib.Path]:
    tmux_log = tmp_path / "tmux.log"
    watchdog_log = tmp_path / "watchdog.log"
    _write_fake_tmux(tmp_path, fake_tmux_body)
    nonce_file, nonce = _nonce(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env['PATH']}",
            "TMUX_FAKE_LOG": str(tmux_log),
            "TX_RESTART_WATCHDOG_LOG": str(watchdog_log),
            "TX_RESTART_WATCHDOG_OPEN_TERMINAL": "0",
            "IMPERIUM_MACHINE": "test",
        }
    )
    result = subprocess.run(
        [str(TX), "__restart_watchdog", "main", client_tty, "", "0", nonce_file, nonce],
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    return result, tmux_log, watchdog_log


def test_restart_watchdog_switches_client_stranded_outside_target_session(tmp_path: pathlib.Path):
    result, tmux_log, watchdog_log = _run_watchdog(
        tmp_path,
        fake_tmux_body=textwrap.dedent(
            r"""
            printf '%q ' "$0" "$@" >> "$TMUX_FAKE_LOG"; printf '\n' >> "$TMUX_FAKE_LOG"
            state="${TMUX_FAKE_LOG}.switched"
            case "${1:-}" in
              list-clients)
                if [[ " $* " == *" -t main "* ]]; then
                  # The operator tty starts outside main, then appears in main
                  # after switch-client succeeds.
                  [[ -f "$state" ]] && printf '/dev/ttys000\n'
                  exit 0
                fi
                # Session-blind list-clients sees the client stranded in _stash.
                printf '/dev/ttys000\n'
                exit 0
                ;;
              has-session)
                [[ "${2:-}" == "main" || "${3:-}" == "main" ]] && exit 0
                exit 1
                ;;
              switch-client)
                touch "$state"
                exit 0
                ;;
            esac
            exit 0
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "switch-client -c /dev/ttys000 -t main" in tmux_log.read_text()
    log = watchdog_log.read_text()
    assert "switched-client-to-session session=main client_tty=/dev/ttys000" in log
    assert "noop client-restored" not in log


def test_restart_watchdog_restored_check_is_scoped_to_target_session(tmp_path: pathlib.Path):
    result, tmux_log, watchdog_log = _run_watchdog(
        tmp_path,
        fake_tmux_body=textwrap.dedent(
            r"""
            printf '%q ' "$0" "$@" >> "$TMUX_FAKE_LOG"; printf '\n' >> "$TMUX_FAKE_LOG"
            case "${1:-}" in
              list-clients)
                if [[ " $* " == *" -t main "* ]]; then
                  printf '/dev/ttys000\n'
                  exit 0
                fi
                printf 'unexpected-session-blind-list\n'
                exit 0
                ;;
              has-session) exit 0 ;;
              switch-client) exit 0 ;;
            esac
            exit 0
            """
        ),
    )

    assert result.returncode == 0, result.stderr
    assert "noop client-restored session=main client_tty=/dev/ttys000" in watchdog_log.read_text()
    commands = tmux_log.read_text()
    assert "list-clients -t main" in commands
    assert "switch-client" not in commands


def test_restart_force_refuses_non_interactive_invocation(tmp_path: pathlib.Path):
    invocation_log = tmp_path / "invocations.log"
    # Strip agent/automation markers so this exercises the TTY guard specifically
    # (the test runner itself may run under Claude Code, which sets CLAUDECODE —
    # that would trip the earlier agent-context block instead). The agent guard is
    # covered in test_tx_restart_agent_guard.py; here we assert the non-agent,
    # non-TTY caller is refused by the --force/TTY guard.
    agent_markers = {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "TOKEN_API_SUBAGENT",
        "CODEX_PROFILE",
        "CODEX_HEADLESS",
        "CODEX_BRIDGE_ID",
        "TOKEN_API_CODEX_BRIDGE_ID",
        "TOKEN_API_CODEX_PROFILE",
    }
    env = {k: v for k, v in os.environ.items() if k not in agent_markers}
    env.update(
        {
            "TX_INVOCATION_LOG": str(invocation_log),
            "TX_RESTART_WATCHDOG_OPEN_TERMINAL": "0",
            "IMPERIUM_MACHINE": "test",
            # Make accidental tmuxctl execution fail loudly if the guard regresses.
            "PYTHONPATH": "",
        }
    )

    result = subprocess.run(
        [str(TX), "restart", "--force"],
        text=True,
        input="",
        capture_output=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 1
    assert "Refusing: --force requires an interactive terminal" in result.stderr
    assert "outcome=refused-force-no-tty" in invocation_log.read_text()
