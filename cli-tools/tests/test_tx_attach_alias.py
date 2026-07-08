"""Tests for the reduced human-only tx attach alias."""

from __future__ import annotations

import os
import pathlib
import pty
import subprocess
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
TX = ROOT / "bin" / "tx"


def _run_on_pty(
    argv: list[str], *, env: dict[str, str], timeout: float = 5.0
) -> subprocess.CompletedProcess[str]:
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            text=False,
            close_fds=True,
        )
        os.close(slave)
        slave = -1
        output = bytearray()
        deadline = time.time() + timeout
        while proc.poll() is None and time.time() < deadline:
            try:
                output.extend(os.read(master, 4096))
            except OSError:
                break
            time.sleep(0.01)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
            raise AssertionError(f"process timed out: {argv!r}")
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
        return subprocess.CompletedProcess(
            argv, proc.returncode, output.decode(errors="replace"), ""
        )
    finally:
        if slave != -1:
            os.close(slave)
        os.close(master)


def test_interactive_tx_execs_tmuxctld_attach(tmp_path: pathlib.Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "argv0"
    ctl = fakebin / "tmuxctld-ctl"
    ctl.write_text(f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > {log!s}\n")
    ctl.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"

    proc = _run_on_pty([str(TX)], env=env)

    assert proc.returncode == 0, proc.stdout
    argv = [part.decode() for part in log.read_bytes().split(b"\0") if part]
    assert argv == ["attach"]


def test_interactive_tx_start_passes_optional_session(tmp_path: pathlib.Path) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    log = tmp_path / "argv0"
    ctl = fakebin / "tmuxctld-ctl"
    ctl.write_text(f"#!/usr/bin/env bash\nprintf '%s\\0' \"$@\" > {log!s}\n")
    ctl.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fakebin}:{env['PATH']}"

    proc = _run_on_pty([str(TX), "start", "main"], env=env)

    assert proc.returncode == 0, proc.stdout
    argv = [part.decode() for part in log.read_bytes().split(b"\0") if part]
    assert argv == ["attach", "main"]
