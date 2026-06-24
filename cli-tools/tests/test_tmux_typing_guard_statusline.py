"""Typing-guard diagnostic/projection tests.

Live tmux status/borders do not invoke this command. It remains a manual
single-pane diagnostic and a one-shot expiry clearer for the event-updated
@GUARD projection.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STATUS = REPO / "cli-tools" / "bin" / "tmux-typing-guard-status"

STYLED = "#[fg=colour214]#[bold]⌨ GUARD#[default] "
MARKER = "#[fg=colour214]#[bold]⌨#[default]"


def _fake_tmux(tmp_path: Path) -> Path:
    fake = tmp_path / "tmux"
    fake.write_text(
        textwrap.dedent(
            r"""
            #!/usr/bin/env bash
            set -uo pipefail
            echo "$*" >> "${FAKE_TMUX_CALLS}"
            verb="${1:-}"; shift || true
            target=""; opt=""; val=""; prev=""
            for a in "$@"; do
              if [[ "$prev" == "-t" ]]; then target="$a"; fi
              if [[ "$a" == @* ]]; then opt="$a"; fi
              prev="$a"
            done
            case "$verb" in
              display-message)
                if [[ "$*" == *"#{pane_id}"* && "$*" != *"-t"* ]]; then
                  echo "${FAKE_ACTIVE_PANE:-%1}"
                fi
                ;;
              show-options|show)
                if [[ "$opt" == "@TYPING_LOCK_UNTIL" ]]; then
                  key="LOCK_${target//%/}"
                  printf '%s\n' "${!key:-}"
                else
                  exit 1
                fi
                ;;
              list-panes)
                for p in ${FAKE_PANES:-}; do echo "$p"; done
                ;;
              set-option|set)
                val="${@: -1}"
                printf '%s\t%s\t%s\n' "$opt" "$target" "$val" >> "${FAKE_SETOPT}"
                ;;
            esac
            """
        ).strip()
        + "\n"
    )
    fake.chmod(0o755)
    return fake


def _env(tmp_path: Path, *, active: str = "%1", locks: dict[str, float] | None = None) -> dict[str, str]:
    calls = tmp_path / "calls.log"
    setopt = tmp_path / "setopt.log"
    calls.write_text("")
    setopt.write_text("")
    fake = _fake_tmux(tmp_path)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}:{env.get('PATH', '')}",
            "IMPERIUM_TMUX_BIN": str(fake),
            "FAKE_TMUX_CALLS": str(calls),
            "FAKE_SETOPT": str(setopt),
            "FAKE_ACTIVE_PANE": active,
            "FAKE_PANES": "%1 %2",
        }
    )
    for pane, until in (locks or {}).items():
        env[f"LOCK_{pane.replace('%', '')}"] = str(until)
    return env


def _run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(STATUS), *args],
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
        check=False,
    )


def _setopts(env: dict[str, str]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in Path(env["FAKE_SETOPT"]).read_text().splitlines():
        parts = line.split("\t")
        while len(parts) < 3:
            parts.append("")
        rows.append((parts[0], parts[1], parts[2]))
    return rows


def _calls(env: dict[str, str]) -> str:
    return Path(env["FAKE_TMUX_CALLS"]).read_text()


def test_segment_active_when_pane_lock_is_future(tmp_path: Path) -> None:
    env = _env(tmp_path, locks={"%1": time.time() + 60})
    assert _run(env).stdout == STYLED
    assert _run(env, "--plain").stdout == "TYPE"


def test_segment_dark_when_pane_lock_is_absent_or_expired(tmp_path: Path) -> None:
    env = _env(tmp_path, locks={"%1": time.time() - 1})
    assert _run(env).stdout == ""
    assert _run(env, "--plain").stdout == ""


def test_publish_sets_one_pane_guard_without_scanning(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _run(env, "--publish", "-t", "%1", "--active")
    assert ("@GUARD", "%1", MARKER) in _setopts(env)
    assert "list-panes" not in _calls(env)


def test_publish_clears_one_pane_guard_without_scanning(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _run(env, "--publish", "-t", "%1", "--inactive")
    assert ("@GUARD", "%1", "") in _setopts(env)
    assert "list-panes" not in _calls(env)


def test_clear_expired_clears_only_target_when_lock_expired(tmp_path: Path) -> None:
    env = _env(tmp_path, locks={"%1": time.time() - 1, "%2": time.time() + 60})
    _run(env, "--clear-expired", "-t", "%1")
    assert ("@GUARD", "%1", "") in _setopts(env)
    assert "list-panes" not in _calls(env)
