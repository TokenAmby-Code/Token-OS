"""The discord-daemon CLI must never run the legacy pidfile+nohup path.

The daemon is launchd-supervised. The old `start`/`restart` (nohup node) and
`stop` (kill pidfile PID) split-brain against launchd's KeepAlive. These tests
assert the mutating subcommands now route to the authoritative launchd path:
  - restart/start -> delegate to `token-restart --discord`
  - stop          -> `launchctl bootout` (real, supervised stop)
and that they do NOT spawn a daemon directly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
DISCORD_DAEMON = BIN / "discord-daemon"

DISCORD_LABEL = "ai.tokenclaw.discord"
# discord-daemon stop boots out the daemon in the invoking user's launchd domain.
UID = str(os.getuid())


def _stub_env(tmp_path: Path, names: list[str]) -> tuple[dict, Path]:
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir(exist_ok=True)
    logfile = tmp_path / "calls.log"
    logfile.touch()
    for name in names:
        p = stub_bin / name
        p.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{logfile}"\nexit 0\n')
        p.chmod(0o755)
    env = {**os.environ, "PATH": f"{stub_bin}:{os.environ['PATH']}"}
    return env, logfile


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(DISCORD_DAEMON), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_restart_delegates_to_token_restart(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, ["token-restart", "nohup", "node"])
    proc = _run(["restart"], env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert "token-restart --discord" in calls
    # Must NOT spawn a daemon directly (the split-brain path).
    assert "nohup" not in calls
    assert "node" not in calls


def test_start_delegates_to_token_restart(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, ["token-restart", "nohup", "node"])
    proc = _run(["start"], env)
    assert proc.returncode == 0, proc.stderr
    assert "token-restart --discord" in logfile.read_text()


def test_stop_uses_launchctl_bootout(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, ["launchctl"])
    proc = _run(["stop"], env)
    assert proc.returncode == 0, proc.stderr
    assert f"bootout gui/{UID}/{DISCORD_LABEL}" in logfile.read_text()


def test_unknown_command_still_rejected(tmp_path: Path) -> None:
    env, _ = _stub_env(tmp_path, ["launchctl"])
    proc = _run(["bogus"], env)
    assert proc.returncode != 0
