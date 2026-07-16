from __future__ import annotations

import os
import pty
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TX = ROOT / "cli-tools" / "bin" / "tx"


def _run_in_pty(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            args,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            text=True,
        )
    finally:
        os.close(slave)
    output = b""
    while True:
        try:
            chunk = os.read(master, 4096)
        except OSError:
            break
        if not chunk:
            break
        output += chunk
    os.close(master)
    return subprocess.CompletedProcess(args, process.wait(), output.decode(errors="replace"), "")


def test_tx_runs_only_the_bounded_rote_walk_then_launches_codex(tmp_path: Path) -> None:
    trace = tmp_path / "trace"
    prompt = tmp_path / "prompt"
    ctl = tmp_path / "tmuxctld-ctl"
    ctl.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$TX_TRACE"\n'
        'case "$1" in attach) exit 1;; *) exit 0;; esac\n',
        encoding="utf-8",
    )
    ctl.chmod(0o755)
    codex = tmp_path / "codex"
    codex.write_text(
        '#!/bin/sh\nprintf \'%s\' "$1" > "$TX_PROMPT"\n',
        encoding="utf-8",
    )
    codex.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TMUXCTLD_CTL_BIN": str(ctl),
        "CODEX_BIN": str(codex),
        "TX_REPORT_DIR": str(tmp_path / "reports"),
        "TX_TRACE": str(trace),
        "TX_PROMPT": str(prompt),
    }
    result = _run_in_pty([str(TX)], env)

    assert result.returncode == 0, result.stdout
    assert trace.read_text(encoding="utf-8").splitlines() == [
        "attach",
        "status",
        "start",
        "status",
        "logs err 80",
        "attach",
    ]
    reports = list((tmp_path / "reports").glob("tx-rote-walk.*.txt"))
    assert len(reports) == 1
    report = reports[0].read_text(encoding="utf-8")
    assert "initial attach exit: 1" in report
    assert "final managed attach" in report
    assert str(reports[0]) in prompt.read_text(encoding="utf-8")


def test_tx_rejects_parameters_without_running_recovery(tmp_path: Path) -> None:
    result = subprocess.run([str(TX), "restart"], text=True, capture_output=True)
    assert result.returncode == 64
    assert "no arguments" in result.stderr
