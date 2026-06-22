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
import stat
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

    # The deploy-owned runtime checkout the fake sync "advances". CD_LIVE_CHECKOUT
    # is the backward-compatible override seam; TOKEN_OS_BARE_REPO points at a
    # fake bare skeleton. Both need to exist for the new protected-main topology.
    repo = tmp_path / "runtime"
    repo.mkdir(exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    bare = tmp_path / "token-os.git"
    bare.mkdir(exist_ok=True)

    # Simple logging stubs (log "<name> <args>", exit 0). tmux/tmuxctl/tx are
    # stubbed too so that if token-restart ever shelled out to the session-
    # destructive `tx restart`/`tmuxctl restart`, it would show in the call log —
    # see test_deploy_never_wipes_the_tmux_fleet.
    for name in [
        "launchctl",
        "sleep",
        "ssh",
        "osascript",
        "uv",
        "pgrep",
        "push-mobile",
        "tmux",
        "tmuxctl",
        "tx",
    ]:
        p = stub_bin / name
        p.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{logfile}"\nexit 0\n')
        p.chmod(0o755)

    # curl: log + emit a connected payload (discord health greps for it) + exit 0.
    # For token-restart's -w '%{http_code}' refresh path, emit a bare 200.
    curl = stub_bin / "curl"
    curl.write_text(
        f"""#!/usr/bin/env bash
echo "curl $*" >> "{logfile}"
for arg in "$@"; do
  if [[ "$arg" == *"%{{http_code}}"* ]]; then echo 200; exit 0; fi
done
echo '{{"connected": true}}'
exit 0
"""
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
git_cwd=""
if [[ "$1" == "-C" ]]; then git_cwd="$2"; shift 2; fi
if [[ "$1" == --git-dir=* ]]; then shift; fi
if [[ "$1" == "--git-dir" ]]; then shift 2; fi
sub="$1"; shift || true
if [[ "$sub" == "status" && "${STUB_DIRTY_RUNTIME:-}" == "1" ]]; then
  echo " M token-api/main.py"
  exit 0
fi
if [[ "${STUB_REQUIRE_RUNTIME_WRITABLE:-}" == "1" && ( "$sub" == "fetch" || "$sub" == "checkout" ) && -n "$git_cwd" ]]; then
  if [[ ! -w "$git_cwd/.git" ]]; then
    echo "runtime git dir is locked during $sub" >&2
    exit 77
  fi
fi
case "$sub" in
  fetch|stash|update-ref|checkout|cat-file) exit 0 ;;
  status) exit 0 ;;
  merge-base)
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
    args="$*"
    case "$args" in
      *--show-toplevel*) echo "$STUB_REPO" ;;
      *--is-bare-repository*) echo "true" ;;
      *FETCH_HEAD*) echo "NEW111" ;;
      *refs/heads/main*) if [[ "${STUB_BARE_OLD:-}" == "1" ]]; then echo "OLD000"; else echo "NEW111"; fi ;;
      *\^\{commit\}*) echo "NEW111" ;;
      *HEAD*)            if [[ "${STUB_NO_ADVANCE:-}" == "1" ]]; then echo "NEW111"; else echo "OLD000"; fi ;;
      *)                 echo "OLD000" ;;
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
        "TOKEN_OS_BARE_REPO": str(bare),
        "TOKEN_SATELLITE_REFRESH_SECRET": "refresh-secret",
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


def test_satellite_or_refresh_helper_change_refreshes_wsl_only(tmp_path: Path) -> None:
    env, logfile = _stub_env(
        tmp_path,
        "token-api/token-satellite.py\ntoken-api/scripts/token-satellite-refresh",
    )
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI not in calls
    assert "/runtime/refresh" in calls
    assert "/restart" not in calls
    assert KICK_DISCORD not in calls


def test_ahk_and_cli_changes_refresh_wsl_runtime(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "ahk/foo.ahk\ncli-tools/lib/nas-path.sh")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert "/runtime/refresh" in calls
    assert KICK_TOKENAPI not in calls
    assert KICK_DISCORD not in calls


def test_token_api_lockfile_change_restarts_mac_and_refreshes_wsl(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/uv.lock")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI in calls
    assert "/runtime/refresh" in calls
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


def test_bare_main_non_ff_aborts_without_restart(tmp_path: Path) -> None:
    env, logfile = _stub_env(
        tmp_path,
        "discord-daemon/daemon.js",
        merge_fail_genuine=True,
        merge_fail_msg="fatal: Not possible to fast-forward, aborting.",
    )
    env["STUB_BARE_OLD"] = "1"
    proc = _run(env)
    assert proc.returncode != 0
    assert "bare main is not a fast-forward" in proc.stdout
    calls = logfile.read_text()
    assert KICK_TOKENAPI not in calls
    assert KICK_DISCORD not in calls


def test_dirty_runtime_shunts_then_restarts_changed_service(tmp_path: Path) -> None:
    """New invariant: a dirty runtime NEVER blocks the deploy. The stub reports a
    dirty `git status` (STUB_DIRTY_RUNTIME=1); token-restart must auto-preserve the
    WIP to a wip/live-dirty-<ts> branch and STILL advance + restart the changed
    service rather than aborting. (Replaces the old #280 dirty-tree-abort.) The
    real branch-create/commit/push mechanics are covered against real git in
    test_token_restart_runtime_reconcile.py."""
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    env["STUB_DIRTY_RUNTIME"] = "1"
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "auto-preserving WIP to wip/live-dirty-" in proc.stdout
    calls = logfile.read_text()
    assert KICK_TOKENAPI in calls
    assert KICK_DISCORD not in calls


def _has_any_write_bit(path: Path) -> bool:
    return bool(path.lstat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def test_locked_runtime_is_unlocked_for_git_sync_then_relocked(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    runtime = Path(env["CD_LIVE_CHECKOUT"])
    git_dir = runtime / ".git"

    # Match production: deploy-owned runtime starts FS-locked, including .git.
    subprocess.run(
        [
            str(Path(__file__).resolve().parents[1] / "scripts" / "runtime-write-protect.sh"),
            "lock",
            str(runtime),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    assert not _has_any_write_bit(git_dir)

    # The git stub fails fetch/checkout unless token-restart lifted the lock first.
    env["STUB_REQUIRE_RUNTIME_WRITABLE"] = "1"
    proc = _run(env)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "runtime advanced" in proc.stdout
    assert "runtime write-protected" in proc.stdout
    assert not _has_any_write_bit(runtime)
    assert not _has_any_write_bit(git_dir)
    assert KICK_TOKENAPI in logfile.read_text()


def test_cd_bare_repo_config_is_distinct_from_worktree_bare_repo(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "README.md")
    worktree_bare = tmp_path / "worktree-token-os.git"
    cd_bare = tmp_path / "cd-token-os.git"
    worktree_bare.mkdir()
    cd_bare.mkdir()
    conf = tmp_path / "Token-OS.conf"
    conf.write_text(
        f"BARE_REPO={worktree_bare}\n"
        f"CD_BARE_REPO={cd_bare}\n"
        f"RUNTIME_CHECKOUT={env['CD_LIVE_CHECKOUT']}\n",
        encoding="utf-8",
    )
    env.pop("TOKEN_OS_BARE_REPO", None)
    env["TOKEN_OS_WORKTREE_CONF"] = str(conf)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    assert f"bare skeleton {cd_bare}" in proc.stdout
    assert f"bare skeleton {worktree_bare}" not in proc.stdout


# ── CD invariant: deploy reloads services, NEVER wipes the tmux fleet ────────

# The session-destructive tmux rebuild (`tx restart` → `tmuxctl restart
# --execute` → kill-session). token-restart is the CD reload path and must NEVER
# emit any of these — a deploy reloads launchd services and exits; wiping the
# live fleet is an operator-only `tx restart`, never an automatic consequence of
# a merge. tmux/tmuxctl/tx are logging stubs (see _stub_env), so any such call
# would land in the call log.
_DESTRUCTIVE_TMUX = (
    "tmuxctl restart",
    "restart --execute",
    "kill-session",
    "tx restart",
    "tx -r",
    "__tmuxctl_restart",
)


def _assert_no_fleet_wipe(calls: str) -> None:
    for bad in _DESTRUCTIVE_TMUX:
        assert bad not in calls, (
            f"deploy must never emit destructive tmux command {bad!r}:\n{calls}"
        )
    # token-restart should not shell to tmux/tmuxctl/tx at all.
    for tool in ("tmux ", "tmuxctl ", "tx "):
        assert tool not in calls, f"deploy unexpectedly invoked {tool!r}:\n{calls}"


def test_mac_affecting_deploy_reloads_without_wiping_fleet(tmp_path: Path) -> None:
    # A token-api change restarts the Mac service (kickstart -k) but must not
    # touch the tmux fleet.
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI in calls  # it DID reload the service…
    _assert_no_fleet_wipe(calls)  # …without wiping the fleet.


def test_no_advance_full_restart_still_never_wipes_fleet(tmp_path: Path) -> None:
    # The heaviest path: a no-advance sync falls back to a FULL restart of the
    # standard set (token-api + discord). Even then — deploy everything, but
    # never `tx restart`.
    env, logfile = _stub_env(tmp_path, "discord-daemon/daemon.js", no_advance=True)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TOKENAPI in calls
    assert KICK_DISCORD in calls
    _assert_no_fleet_wipe(calls)
