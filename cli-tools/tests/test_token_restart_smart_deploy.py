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


def _stub_env(
    tmp_path: Path,
    changed_paths: str,
    *,
    no_advance: bool = False,
    merge_fail_times: int = 0,
    merge_fail_msg: str = "",
    merge_fail_genuine: bool = False,
):
    """Stub git + side-effect tools on PATH. Returns (env, logfile).

    merge_fail_times/merge_fail_msg/merge_fail_genuine drive the ff-merge stub so we
    can exercise the SMB-busy retry path. The git stub is a fresh process per call, so
    failure counting is stateful via a counter file under tmp_path. While the call
    count is <= merge_fail_times the stub prints merge_fail_msg to stderr and exits 1;
    afterwards it exits 0. merge_fail_genuine makes EVERY merge fail (a real non-ff
    that must never be retried)."""
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
    # the `-C <dir>` prefix every call uses. `merge` has its own stateful case so a
    # test can make the first N ff-merges fail (transient SMB lock) or fail always
    # (genuine non-ff) — see MERGE_FAIL_* / MERGE_COUNTER below.
    git = stub_bin / "git"
    git.write_text(
        r"""#!/usr/bin/env bash
if [[ "$1" == "-C" ]]; then shift 2; fi
sub="$1"; shift || true
case "$sub" in
  fetch|stash) exit 0 ;;
  merge)
    if [[ "${MERGE_FAIL_TIMES:-0}" != "0" ]]; then
      count=0
      [[ -f "$MERGE_COUNTER" ]] && count="$(cat "$MERGE_COUNTER")"
      count=$(( count + 1 ))
      echo "$count" > "$MERGE_COUNTER"
      if (( count <= MERGE_FAIL_TIMES )); then
        echo "$MERGE_FAIL_MSG" >&2
        exit 1
      fi
    fi
    exit 0
    ;;
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

    # A genuine non-ff fails on every attempt; a transient lock fails merge_fail_times.
    fail_times = 99 if merge_fail_genuine else merge_fail_times

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "IMPERIUM_MACHINE": "mac",
        "CD_LIVE_CHECKOUT": str(repo),
        "STUB_REPO": str(repo),
        "STUB_CHANGED_PATHS": changed_paths,
        "MERGE_FAIL_TIMES": str(fail_times),
        "MERGE_FAIL_MSG": merge_fail_msg,
        "MERGE_COUNTER": str(tmp_path / "merge_count.txt"),
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


# ── SMB-busy ff-merge resilience (the silent-stale-deploy fix) ────────


def test_transient_smb_lock_retries_then_syncs(tmp_path: Path) -> None:
    # First ff-merge hits a transient SMB EBUSY unlink lock and exits 1; the retry
    # helper backs off and the second merge succeeds. The sync must ADVANCE — i.e.
    # selectively restart only token-api — NOT silently degrade to a stale full
    # restart (the bug: stderr was discarded and EBUSY misread as "not a ff").
    env, logfile = _stub_env(
        tmp_path,
        "token-api/main.py",
        merge_fail_times=1,
        merge_fail_msg=(
            "error: unable to unlink old 'cli-tools/bin/tmux-pane-label': Resource busy"
        ),
    )
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    # FF succeeded (success marker present, abort marker absent) → SYNC advanced.
    assert "CD-sync: fast-forwarded" in proc.stdout
    assert "ABORTING sync" not in proc.stdout
    # The retry actually fired on the transient lock.
    assert "transient SMB lock" in proc.stdout
    # Selective restart: only token-api bounced. A degraded full restart would also
    # bounce discord, so its absence proves we did NOT degrade to a stale no-advance.
    assert KICK_TOKENAPI in calls
    assert KICK_DISCORD not in calls


def test_genuine_non_ff_degrades_and_surfaces_error(tmp_path: Path) -> None:
    # A real non-fast-forward (not a transient lock): the helper must NOT retry it
    # away. The sync aborts, degrades to a full restart of the standard set, AND the
    # real git error is surfaced — closing the silent-stale-deploy hole.
    env, logfile = _stub_env(
        tmp_path,
        "discord-daemon/daemon.js",
        merge_fail_genuine=True,
        merge_fail_msg="fatal: Not possible to fast-forward, aborting.",
    )
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    # No advance → fallback to the full standard-set restart (token-api + discord).
    assert KICK_TOKENAPI in calls
    assert KICK_DISCORD in calls
    # The failure is no longer silent: the real git stderr is echoed to the log.
    assert "Not possible to fast-forward" in proc.stdout
