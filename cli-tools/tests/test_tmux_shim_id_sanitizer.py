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
            if [[ -n "${TMUX_FAKE_LOG:-}" ]]; then
              printf '%s\n' "$*" >>"$TMUX_FAKE_LOG"
            fi
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
              "display-message -c /dev/ttys003 -p #{pane_id}")
                printf '%%11\n'
                ;;
              "display-message #{pane_id}")
                printf '%%11\n'
                ;;
              "display-message -p [#{pane_id}]")
                printf '[%%11]\n'
                ;;
              "display-message -t %11 -p #{session_name}:#{window_index}")
                printf 'palace:0\n'
                ;;
              "display-message -t %11 -p ")
                ;;
              "display-message -t palace:0 -p #{window_zoomed_flag}")
                printf '0\n'
                ;;
              set-option\ -w\ -t\ palace:0\ @GRID_EXPANDED\ none|\
              set-option\ -w\ -t\ palace:0\ @GRID_STASH\ |\
              set-option\ -w\ -t\ palace:0\ @GENERIC_EXPANDED\ none|\
              set-option\ -w\ -t\ palace:0\ @GENERIC_STASH\ |\
              set-option\ -w\ -t\ palace:0\ @SIDE_EXPANDED\ none)
                ;;
              "resize-pane -Z -t %11")
                ;;
              "capture-pane -p -t %11")
                printf 'Last raw panes: %%11 and %%99\n'
                ;;
              "capturep -p -t %11")
                printf 'Last raw panes: %%11 and %%99\n'
                ;;
              "select-pane -t %11")
                printf 'selected %%11\n'
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


def _run_shim(
    tmp_path: pathlib.Path, *args: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "IMPERIUM_TMUX_BIN": str(_fake_tmux(tmp_path)),
        "IMPERIUM_ALLOW_TMUX_FOCUS": "1",
        "IMPERIUM_ALLOW_MECHANICUS_FOCUS": "1",
    }
    env.pop("IMPERIUM_TMUX_SANITIZE_IDS", None)
    env.pop("IMPERIUM_TMUX_RAW", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run([str(TMUX_SHIM), *args], text=True, capture_output=True, env=env)


def test_tmux_shim_sanitizes_id_printing_reads_by_default(tmp_path) -> None:
    proc = _run_shim(tmp_path, "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "palace:N zsh\nunresolved orphan\n"
    assert "%" not in proc.stdout


def test_tmux_shim_sanitizes_windows_sessions_and_capture(tmp_path) -> None:
    # Listing + capture surfaces are human-readable and must always sanitize.
    commands = [
        ("list-windows", "-a", "-F", "#{window_id} #{pane_id}"),
        ("list-sessions", "-F", "#{session_id} #{pane_id}"),
        ("capture-pane", "-p", "-t", "%11"),
        ("capturep", "-p", "-t", "%11"),
        ("list-panes",),
        ("lsp",),
    ]
    for command in commands:
        proc = _run_shim(tmp_path, *command)
        assert proc.returncode == 0, (command, proc.stderr)
        assert "%" not in proc.stdout, (command, proc.stdout)


def test_programmatic_pure_pane_id_print_passes_through_raw(tmp_path) -> None:
    """`display-message -p '#{pane_id}'` is a programmatic id capture.

    tmux-grid-expand (and any script resolving its own pane) reads the RAW
    physical id on stdout and feeds it straight back as a ``-t`` target.
    Rewriting it to a public id (e.g. ``council:pax``) yields an unresolvable
    target ("can't find session: council"). The pure-id print form must
    therefore pass through UNTRANSLATED — including the ``-c <client>`` shape
    tmux-grid-expand actually uses.
    """
    for command in (
        ("display-message", "-p", "#{pane_id}"),
        ("display", "-p", "#{pane_id}"),
        ("display-message", "-c", "/dev/ttys003", "-p", "#{pane_id}"),
    ):
        proc = _run_shim(tmp_path, *command)
        assert proc.returncode == 0, (command, proc.stderr)
        assert proc.stdout == "%11\n", (command, proc.stdout)


def test_tmux_grid_expand_pane_id_round_trips_as_resolvable_target(tmp_path) -> None:
    """Regression: the id grid-expand fetches must be reusable as a ``-t`` target.

    Step 1 mirrors grid-expand's ``TARGET_PANE=$(tmux display-message -p
    '#{pane_id}')``; step 2 mirrors its subsequent ``tmux display-message -t
    "$TARGET_PANE" ...``. With the over-translation bug, step 1 returned a
    public id that made step 2 fail. The fetched id must be the physical
    ``%11`` and resolve cleanly when handed back.
    """
    fetch = _run_shim(tmp_path, "display-message", "-p", "#{pane_id}")
    assert fetch.returncode == 0, fetch.stderr
    target = fetch.stdout.strip()
    assert target == "%11", target

    reuse = _run_shim(
        tmp_path, "display-message", "-t", target, "-p", "#{session_name}:#{window_index}"
    )
    assert reuse.returncode == 0, (target, reuse.stderr)
    assert reuse.stdout == "palace:0\n", reuse.stdout


def test_tmux_grid_expand_direct_client_path_uses_only_local_tmux(tmp_path) -> None:
    """Prefix-e regression: the direct helper resolves the client pane and zooms
    locally. No tmuxctld transport is involved, so a degraded daemon cannot emit a
    false status-line expand failure after native zoom succeeds.
    """
    log = tmp_path / "tmux.log"
    fake_tmux = _fake_tmux(tmp_path)
    tmux_cmd = tmp_path / "tmux"
    tmux_cmd.symlink_to(fake_tmux)
    env = {
        **os.environ,
        "TMUX_FAKE_LOG": str(log),
        "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}",
    }

    proc = subprocess.run(
        [str(ROOT / "bin" / "tmux-grid-expand"), "--client", "/dev/ttys003"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    calls = log.read_text(encoding="utf-8")
    assert "display-message -c /dev/ttys003 -p #{pane_id}" in calls
    assert "resize-pane -Z -t %11" in calls
    assert "tmuxctld" not in calls


def test_human_facing_display_still_sanitizes(tmp_path) -> None:
    """Human-facing renders still leak-proof: a pure id WITHOUT ``-p`` (status
    line) and an id EMBEDDED in printed text are both sanitized to public ids."""
    status = _run_shim(tmp_path, "display-message", "#{pane_id}")
    assert status.returncode == 0, status.stderr
    assert status.stdout == "palace:N\n", status.stdout
    assert "%" not in status.stdout

    embedded = _run_shim(tmp_path, "display-message", "-p", "[#{pane_id}]")
    assert embedded.returncode == 0, embedded.stderr
    assert embedded.stdout == "[palace:N]\n", embedded.stdout
    assert "%" not in embedded.stdout


def test_tmux_raw_env_disables_read_sanitizer(tmp_path) -> None:
    proc = _run_shim(tmp_path, "list-panes", extra_env={"IMPERIUM_TMUX_RAW": "1"})

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "0: [80x24] [history 1/2000] %11 (active)\n"


def test_non_read_physical_target_command_passes_through_without_sanitizing(tmp_path) -> None:
    proc = _run_shim(tmp_path, "select-pane", "-t", "%11")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "selected %11\n"
