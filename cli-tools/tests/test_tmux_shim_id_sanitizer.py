from __future__ import annotations

import os
import pathlib
import subprocess
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMUX_SHIM = ROOT / "bin" / "tmux"


def _fake_tmux(tmp_path: pathlib.Path) -> pathlib.Path:
    fake = tmp_path / "real-tmux"
    fake.write_text(
        textwrap.dedent(
            r"""
            #!/usr/bin/env bash
            set -euo pipefail
            if [[ "${1:-}" == "list-panes" && "${2:-}" == "-a" && "${3:-}" == "-F" && "${4:-}" == $'#{pane_id}\t#{@PANE_ID}' ]]; then
              printf '%%11\tpalace:N\n%%12\tmechanicus:3\n'
              exit 0
            fi
            case "$*" in
              "list-panes")
                printf '0: [80x24] [history 1/2000] %%11 (active)\n'
                ;;
              "lsp")
                printf '0: [80x24] [history 1/2000] %%11 (active)\n'
                ;;
              "list-panes -a -F #{pane_id} #{pane_current_command}")
                printf '%%11 zsh\n%%99 orphan\n'
                ;;
              "list-windows -a -F #{window_id} #{pane_id}")
                printf '@1 %%12\n'
                ;;
              "list-sessions -F #{session_id} #{pane_id}")
                printf '$1 %%11\n'
                ;;
              "display-message -p #{pane_id}")
                printf '%%11\n'
                ;;
              "display -p #{pane_id}")
                printf '%%11\n'
                ;;
              "capture-pane -p -t %11")
                printf 'Last raw panes: %%11 and %%99\n'
                ;;
              "capturep -p -t %11")
                printf 'Last raw panes: %%11 and %%99\n'
                ;;
              *)
                printf 'unexpected args: %s\n' "$*" >&2
                exit 64
                ;;
            esac
            """
        ).strip()
        + "\n"
    )
    fake.chmod(0o755)
    return fake


def _run_shim(tmp_path: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "IMPERIUM_TMUX_BIN": str(_fake_tmux(tmp_path)),
        "IMPERIUM_ALLOW_TMUX_FOCUS": "1",
        "IMPERIUM_ALLOW_MECHANICUS_FOCUS": "1",
    }
    env.pop("IMPERIUM_TMUX_SANITIZE_IDS", None)
    env.pop("IMPERIUM_TMUX_RAW", None)
    return subprocess.run([str(TMUX_SHIM), *args], text=True, capture_output=True, env=env)


def test_tmux_shim_sanitizes_id_printing_reads_by_default(tmp_path) -> None:
    proc = _run_shim(tmp_path, "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "palace:N zsh\nunresolved orphan\n"
    assert "%" not in proc.stdout


def test_tmux_shim_sanitizes_windows_sessions_display_and_capture(tmp_path) -> None:
    commands = [
        ("list-windows", "-a", "-F", "#{window_id} #{pane_id}"),
        ("list-sessions", "-F", "#{session_id} #{pane_id}"),
        ("display-message", "-p", "#{pane_id}"),
        ("display", "-p", "#{pane_id}"),
        ("capture-pane", "-p", "-t", "%11"),
        ("capturep", "-p", "-t", "%11"),
        ("list-panes",),
        ("lsp",),
    ]
    for command in commands:
        proc = _run_shim(tmp_path, *command)
        assert proc.returncode == 0, (command, proc.stderr)
        assert "%" not in proc.stdout, (command, proc.stdout)


def test_tmux_shim_instance_unset_clears_tint_first(tmp_path) -> None:
    log = tmp_path / "tmux.log"
    fake = tmp_path / "real-tmux-unset"
    fake.write_text(
        textwrap.dedent(
            r'''
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$TMUX_FAKE_LOG"
            exit 0
            '''
        ).strip()
        + "\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "IMPERIUM_TMUX_BIN": str(fake),
        "TMUX_FAKE_LOG": str(log),
        "IMPERIUM_ALLOW_TMUX_FOCUS": "1",
        "IMPERIUM_ALLOW_MECHANICUS_FOCUS": "1",
    }

    proc = subprocess.run(
        [str(TMUX_SHIM), "set-option", "-pu", "-t", "%11", "@INSTANCE_ID"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert log.read_text().splitlines() == [
        "set-option -pu -t %11 window-style",
        "set-option -pu -t %11 window-active-style",
        "set-option -pu -t %11 @INSTANCE_ID",
    ]


def test_tmux_shim_respawn_clears_runtime_state_first(tmp_path) -> None:
    log = tmp_path / "tmux.log"
    fake = tmp_path / "real-tmux-respawn"
    fake.write_text(
        textwrap.dedent(
            r'''
            #!/usr/bin/env bash
            set -euo pipefail
            printf '%s\n' "$*" >> "$TMUX_FAKE_LOG"
            exit 0
            '''
        ).strip()
        + "\n"
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "IMPERIUM_TMUX_BIN": str(fake),
        "TMUX_FAKE_LOG": str(log),
        "IMPERIUM_ALLOW_TMUX_FOCUS": "1",
        "IMPERIUM_ALLOW_MECHANICUS_FOCUS": "1",
    }

    proc = subprocess.run(
        [str(TMUX_SHIM), "respawn-pane", "-k", "-t", "%11"],
        text=True,
        capture_output=True,
        env=env,
    )

    lines = log.read_text().splitlines()
    assert proc.returncode == 0, proc.stderr
    assert lines[:3] == [
        "set-option -pu -t %11 window-style",
        "set-option -pu -t %11 window-active-style",
        "set-option -pu -t %11 @INSTANCE_ID",
    ]
    assert lines[-1] == "respawn-pane -k -t %11"
