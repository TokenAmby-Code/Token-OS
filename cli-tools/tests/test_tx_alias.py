"""`tx` is only a human convenience alias for tmuxctld startup."""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
TX = ROOT / "bin" / "tx"


def _run_tx(
    tmp_path: pathlib.Path, *args: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    ctl_log = tmp_path / "ctl.argv0"
    fake_ctl = tmp_path / "tmuxctld-ctl"
    fake_ctl.write_text(f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > {ctl_log!s}\nexit 0\n")
    fake_ctl.chmod(0o755)
    env = os.environ.copy()
    env["TMUXCTLD_CTL_BIN"] = str(fake_ctl)

    proc = subprocess.run(
        [str(TX), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    argv = (
        [part.decode() for part in ctl_log.read_bytes().split(b"\0") if part]
        if ctl_log.exists()
        else []
    )
    return proc, argv


def test_bare_tx_execs_tmuxctld_attach(tmp_path: pathlib.Path) -> None:
    proc, argv = _run_tx(tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert argv == ["attach"]


def test_tx_start_execs_tmuxctld_attach(tmp_path: pathlib.Path) -> None:
    proc, argv = _run_tx(tmp_path, "start")

    assert proc.returncode == 0, proc.stderr
    assert argv == ["attach"]


def test_tx_attach_execs_tmuxctld_attach(tmp_path: pathlib.Path) -> None:
    proc, argv = _run_tx(tmp_path, "attach")

    assert proc.returncode == 0, proc.stderr
    assert argv == ["attach"]


def test_unsupported_tx_subcommands_do_not_call_old_wrapper_logic(tmp_path: pathlib.Path) -> None:
    proc, argv = _run_tx(tmp_path, "restart")

    assert proc.returncode == 64
    assert "unsupported command 'restart'" in proc.stderr
    assert "tmuxctld-ctl attach" in proc.stderr
    assert "tmuxctld-ctl workspace --rebuild" in proc.stderr
    assert argv == []


def test_tx_start_rejects_extra_arguments(tmp_path: pathlib.Path) -> None:
    proc, argv = _run_tx(tmp_path, "start", "sandbox")

    assert proc.returncode == 64
    assert "only 'tx', 'tx start', and 'tx attach' are supported" in proc.stderr
    assert argv == []
