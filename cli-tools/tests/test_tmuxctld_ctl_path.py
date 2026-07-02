"""PATH exposure for the canonical tmuxctld control CLI."""

from __future__ import annotations

import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
CLI_CTL = ROOT / "cli-tools" / "bin" / "tmuxctld-ctl"


def test_tmuxctld_ctl_is_available_from_cli_tools_path() -> None:
    env = os.environ.copy()
    env["PATH"] = f"{ROOT / 'cli-tools' / 'bin'}:{env['PATH']}"

    proc = subprocess.run(
        ["tmuxctld-ctl", "--help"],
        env=env,
        text=True,
        capture_output=True,
    )

    assert CLI_CTL.exists()
    assert proc.returncode == 0, proc.stderr
    assert "attach [session]" in proc.stdout
