"""token-restart is a git-aware smart deploy: sync the live checkout, then restart
ONLY the services whose files the pull changed.

These tests run the real token-restart with a stubbed `git` on PATH that fakes a
fast-forward and reports a controlled set of changed paths (STUB_CHANGED_PATHS).
Every side effect (launchctl/curl/ssh/osascript/uv/sleep/pgrep/push-mobile) is
stubbed to a logfile, so we assert exactly which services were (or were not)
restarted. STUB_NO_ADVANCE=1 makes the fake sync a no-op (HEAD already current),
which must fall back to a full restart of the standard set.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
TOKEN_RESTART = BIN / "token-restart"

TOKENAPI_LABEL = "ai.openclaw.tokenapi"
DISCORD_LABEL = "ai.tokenclaw.discord"
UID = str(os.getuid())

# Convenience matchers for the call log.
KICK_TOKENAPI = f"kickstart -k gui/501/{TOKENAPI_LABEL}"
KICK_DISCORD = f"kickstart -k gui/{UID}/{DISCORD_LABEL}"


def _stub_env(tmp_path: Path, changed_paths: str, *, no_advance: bool = False):
    """Stub git + side-effect tools on PATH. Returns (env, logfile)."""
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir(exist_ok=True)
    logfile = tmp_path / "calls.log"
    logfile.touch()

    # The live checkout the fake sync "advances". We point token-restart at it
    # via CD_LIVE_CHECKOUT (its explicit override seam) — NOT via IMPERIUM, which
    # nas-path.sh unconditionally re-derives from the machine config (so on a
    # Linux CI runner TOKEN_OS would be /mnt/imperium/Token-OS, which doesn't
    # exist, and resolve_live_checkout would abort → full-restart fallback). The
    # override must point at a real dir, so create it.
    repo = tmp_path / "Token-OS"
    repo.mkdir(exist_ok=True)

    # Simple logging stubs (log "<name> <args>", exit 0).
    for name in ["launchctl", "sleep", "ssh", "osascript", "uv", "pgrep", "push-mobile"]:
        p = stub_bin / name
        p.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{logfile}"\nexit 0\n')
        p.chmod(0o755)

    # curl: log + emit a connected payload (discord health greps for it) + exit 0
    # (token-api/WSL health checks only care about the exit code).
    curl = stub_bin / "curl"
    curl.write_text(
        f'#!/usr/bin/env bash\necho "curl $*" >> "{logfile}"\necho \'{{"connected": true}}\'\nexit 0\n'
    )
    curl.chmod(0o755)

    # uname → Darwin so nas-path.sh keeps us on the Mac-local path.
    uname = stub_bin / "uname"
    uname.write_text('#!/usr/bin/env bash\necho "Darwin"\n')
    uname.chmod(0o755)

    # git: fake a fast-forward + report STUB_CHANGED_PATHS for the diff. Handles
    # the `-C <dir>` prefix every call uses.
    git = stub_bin / "git"
    git.write_text(
        r"""#!/usr/bin/env bash
if [[ "$1" == "-C" ]]; then shift 2; fi
sub="$1"; shift || true
case "$sub" in
  fetch|merge|stash) exit 0 ;;
  rev-parse)
    case "$1" in
      --show-toplevel) echo "$STUB_REPO" ;;
      FETCH_HEAD)      echo "NEW111" ;;
      HEAD)            if [[ "${STUB_NO_ADVANCE:-}" == "1" ]]; then echo "NEW111"; else echo "OLD000"; fi ;;
      *)               echo "OLD000" ;;
    esac
    ;;
  diff)
    if [[ "$*" == *"--name-only"* ]]; then
      printf '%s\n' $STUB_CHANGED_PATHS
    else
      exit 0   # --quiet / --cached --quiet => clean working tree (no stash)
    fi
    ;;
  *) exit 0 ;;
esac
"""
    )
    git.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "IMPERIUM_MACHINE": "mac",
        "CD_LIVE_CHECKOUT": str(repo),
        "STUB_REPO": str(repo),
        "STUB_CHANGED_PATHS": changed_paths,
    }
    if no_advance:
        env["STUB_NO_ADVANCE"] = "1"
    return env, logfile


def _run(env: dict) -> subprocess.CompletedProcess:
    # Default invocation = sync + git-aware selective restart.
    return subprocess.run(
        [str(TOKEN_RESTART)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


# ── Selective restart: only the changed service bounces ──────


def test_token_api_change_restarts_only_token_api(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI in calls
    assert KICK_DISCORD not in calls
    assert "push-mobile -a" not in calls


def test_discord_change_restarts_only_discord(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "discord-daemon/daemon.js")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_DISCORD in calls
    assert KICK_TOKENAPI not in calls
    assert "push-mobile -a" not in calls


def test_mobile_change_runs_push_mobile_only(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "mobile/termux-toolbar-toggle")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert "push-mobile -a" in calls
    assert KICK_TOKENAPI not in calls
    assert KICK_DISCORD not in calls


def test_satellite_change_restarts_wsl_and_token_api(tmp_path: Path) -> None:
    # token-api/token-satellite.py runs on WSL but is also a token-api *.py.
    env, logfile = _stub_env(tmp_path, "token-api/token-satellite.py")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI in calls
    assert "/restart" in calls  # WSL satellite restart POST
    assert KICK_DISCORD not in calls


def test_multiple_changed_services_all_restart(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/routes/tts.py\ndiscord-daemon/daemon.js")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI in calls
    assert KICK_DISCORD in calls


# ── Nothing deployable / no-op / fallback ────────────────────


def test_docs_only_change_restarts_nothing(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "README.md\nTerra/Journal/Daily/2026-06-06.md")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI not in calls
    assert KICK_DISCORD not in calls
    assert "push-mobile -a" not in calls
    assert "No deployable services changed" in proc.stdout


def test_no_advance_falls_back_to_full_restart(tmp_path: Path) -> None:
    # Sync is a no-op (HEAD already current) → full restart of the standard set,
    # regardless of what the (irrelevant) diff would say.
    env, logfile = _stub_env(tmp_path, "discord-daemon/daemon.js", no_advance=True)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI in calls
    assert KICK_DISCORD in calls
    # mobile is NOT part of the full-restart standard set
    assert "push-mobile -a" not in calls
