from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
AUDIT = ROOT / "bin" / "tmux-audit"

# list-panes -F is "#{pane_id}\t#{pane_current_command}\t#{@PANE_TYPE}\t#{@PANE_ID}"
LEGION_ROWS = "\n".join(
    [
        # Custodes idle at a shell prompt — protected by @PANE_TYPE=legion.
        "%C\tzsh\tlegion\tcouncil:custodes",
        # A demoted live agent — a stack-worker, but running claude, never idle.
        "%live\tclaude\tstack-worker\tlegion:1",
        # An untyped grid pane that wandered in — not an explicit stack-worker.
        "%grid\tzsh\t\tpalace:N",
        # A genuinely blank pending worker — the only legitimate cull target.
        "%idle\tzsh\tstack-worker\tlegion:2",
    ]
)

FAKE_TMUX = """#!/usr/bin/env bash
case "$1" in
  has-session) exit 0 ;;
  list-panes)
    tgt=""
    while [[ $# -gt 0 ]]; do [[ "$1" == "-t" ]] && tgt="$2"; shift; done
    [[ "$tgt" == *legion* ]] && printf '%s\\n' "$LEGION_ROWS"
    exit 0 ;;
  kill-pane)
    while [[ $# -gt 0 ]]; do [[ "$1" == "-t" ]] && echo "$2" >> "$KILL_LOG"; shift; done
    exit 0 ;;
  *) exit 0 ;;
esac
"""


def _run_backburner(tmp_path: pathlib.Path) -> list[str]:
    fake_dir = tmp_path / "bin"
    fake_dir.mkdir()
    fake_tmux = fake_dir / "tmux"
    fake_tmux.write_text(FAKE_TMUX)
    fake_tmux.chmod(0o755)

    kill_log = tmp_path / "kills"
    stamp = tmp_path / "stamp"

    env = dict(os.environ)
    env["PATH"] = f"{fake_dir}:{env['PATH']}"
    env["LEGION_ROWS"] = LEGION_ROWS
    env["KILL_LOG"] = str(kill_log)
    # Keep the audit's debounce stamp out of the shared /tmp location.
    env["TMUX_AUDIT_STAMP_FILE"] = str(stamp)

    subprocess.run(
        ["bash", str(AUDIT), "--force", "--only", "backburner"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    if not kill_log.exists():
        return []
    return [line for line in kill_log.read_text().splitlines() if line]


def test_backburner_never_culls_custodes_or_non_stack_workers(tmp_path):
    killed = _run_backburner(tmp_path)

    assert "%C" not in killed  # Custodes orchestrator
    assert "%live" not in killed  # demoted live agent
    assert "%grid" not in killed  # untyped grid pane


def test_backburner_still_reaps_blank_pending_stack_workers(tmp_path):
    killed = _run_backburner(tmp_path)

    assert "%idle" in killed
