"""Regression pins for the tmux launch-command tombstone."""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMUX_SHIM = ROOT / "bin" / "tmux"


def _fake_real_tmux(tmp_path: pathlib.Path) -> pathlib.Path:
    fake = tmp_path / "real-tmux"
    fake.write_text('#!/usr/bin/env bash\nprintf \'%s\\0\' "$@" > "$TMUX_SHIM_TEST_LOG"\nexit 0\n')
    fake.chmod(0o755)
    return fake


def test_raw_human_attach_still_routes_noninteractive_to_tx_tombstone(
    tmp_path: pathlib.Path,
) -> None:
    env = os.environ.copy()
    env["IMPERIUM_TMUX_BIN"] = str(_fake_real_tmux(tmp_path))
    env.pop("IMPERIUM_TMUX_RAW", None)

    proc = subprocess.run(
        [str(TMUX_SHIM), "attach", "-t", "main"],
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 1
    assert "410 GONE: cli-tools/bin/tx" in proc.stderr
    assert "Human attach alias restored" in proc.stderr


def test_raw_env_remains_internal_escape_hatch(tmp_path: pathlib.Path) -> None:
    log = tmp_path / "argv0"
    env = os.environ.copy()
    env["IMPERIUM_TMUX_BIN"] = str(_fake_real_tmux(tmp_path))
    env["IMPERIUM_TMUX_RAW"] = "1"
    env["TMUX_SHIM_TEST_LOG"] = str(log)

    proc = subprocess.run(
        [str(TMUX_SHIM), "attach-session", "-t", "main"],
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 0, proc.stderr
    argv = [part.decode() for part in log.read_bytes().split(b"\0") if part]
    assert argv == ["attach-session", "-t", "main"]
