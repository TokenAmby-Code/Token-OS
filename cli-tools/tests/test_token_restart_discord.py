"""token-restart must own the authoritative Discord daemon restart path.

The Discord daemon is launchd-supervised (label ai.tokenclaw.discord). The only
correct way to bounce it is `launchctl kickstart -k`; the legacy pidfile+nohup
path split-brains. These tests assert token-restart issues the launchd command
for `--discord`, folds Discord into the full restart, and surfaces it in status.

Side effects (launchctl/curl/ssh/osascript/uv/sleep/pgrep) are stubbed onto PATH
so nothing real is restarted; stubs append "<name> <args>" to a log file.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
TOKEN_RESTART = BIN / "token-restart"

DISCORD_LABEL = "ai.tokenclaw.discord"
TOKENAPI_LABEL = "ai.openclaw.tokenapi"
# token-restart targets the Discord daemon at the invoking user's launchd domain.
UID = str(os.getuid())


def _stub_env(tmp_path: Path, names: list[str]) -> tuple[dict, Path]:
    """Create logging stub executables on PATH. Returns (env, logfile)."""
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir(exist_ok=True)
    logfile = tmp_path / "calls.log"
    logfile.touch()
    for name in names:
        p = stub_bin / name
        p.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{logfile}"\nexit 0\n')
        p.chmod(0o755)
    # nas-path.sh (sourced by token-restart) re-detects the machine via `uname`
    # and overrides IMPERIUM_MACHINE, so on a Linux CI runner is_mac() would be
    # false and the script proxies to Mac over SSH. Force Darwin so the Mac-local
    # path runs deterministically on any platform.
    uname_stub = stub_bin / "uname"
    uname_stub.write_text('#!/usr/bin/env bash\necho "Darwin"\n')
    uname_stub.chmod(0o755)
    # check_discord_health greps the /status body for "connected": true, so the
    # curl stub must emit a connected payload to exercise the healthy path.
    curl_stub = stub_bin / "curl"
    curl_stub.write_text(
        f'#!/usr/bin/env bash\necho "curl $*" >> "{logfile}"\necho \'{{"connected": true}}\'\n'
    )
    curl_stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "IMPERIUM_MACHINE": "mac",
    }
    return env, logfile


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(TOKEN_RESTART), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_discord_flag_kickstarts_launchd_label(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, ["launchctl", "sleep"])
    proc = _run(["--discord"], env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert f"kickstart -k gui/{UID}/{DISCORD_LABEL}" in calls


def test_discord_flag_does_not_touch_token_api(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, ["launchctl", "sleep"])
    proc = _run(["--discord"], env)
    assert proc.returncode == 0, proc.stderr
    # --discord must bounce ONLY the daemon, never the token-api service.
    assert TOKENAPI_LABEL not in logfile.read_text()


def test_full_restart_includes_discord(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, ["launchctl", "sleep", "ssh", "osascript", "uv", "pgrep"])
    # --no-sync == the old bare `token-restart`: skip the git sync and full-restart
    # the standard set on current code. (A bare `token-restart` now syncs by
    # default; the git-aware selective path is covered in test_token_restart_smart_deploy.)
    proc = _run(["--no-sync"], env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    # token-api now restarts via a graceful drain — restart_mac queries the
    # launchd pid (`launchctl print <mac label>`) instead of `kickstart -k`.
    assert f"print gui/501/{TOKENAPI_LABEL}" in calls
    assert f"kickstart -k gui/501/{TOKENAPI_LABEL}" not in calls
    # Discord still bounces via kickstart -k.
    assert f"kickstart -k gui/{UID}/{DISCORD_LABEL}" in calls


def test_status_lists_discord(tmp_path: Path) -> None:
    env, _ = _stub_env(tmp_path, ["launchctl", "ssh", "pgrep", "sleep"])
    proc = _run(["--status"], env)
    assert proc.returncode == 0, proc.stderr
    assert "Discord" in proc.stdout


def test_help_documents_discord_flag(tmp_path: Path) -> None:
    env, _ = _stub_env(tmp_path, ["launchctl"])
    proc = _run(["--help"], env)
    assert proc.returncode == 0, proc.stderr
    assert "--discord" in proc.stdout
