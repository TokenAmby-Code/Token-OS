"""token-restart is a git-aware smart deploy: sync the live checkout, then restart
the services whose files the pull changed, plus token-api as the deploy-proof
anchor for /health.git_sha.

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
TMUXCTLD_LABEL = "ai.tokenclaw.tmuxctld"
UID = str(os.getuid())

# Convenience matchers for the call log. token-api now restarts via a graceful
# drain (SIGTERM to the launchd pid, KeepAlive respawns onto the retained socket)
# rather than `kickstart -k`; restart_mac's signature in the log is the
# `launchctl print <mac label>` pid query, emitted only when restart_mac runs.
# Discord still bounces via `kickstart -k`.
RESTART_TOKENAPI = f"print gui/{UID}/{TOKENAPI_LABEL}"
KICK_DISCORD = f"kickstart -k gui/{UID}/{DISCORD_LABEL}"
# tmuxctld bounces via launchctl directly (like Discord): kickstart -k for a
# code change, bootout+bootstrap for a plist change.
KICK_TMUXCTLD = f"kickstart -k gui/{UID}/{TMUXCTLD_LABEL}"
BOOTOUT_TMUXCTLD = f"bootout gui/{UID}/{TMUXCTLD_LABEL}"
BOOTSTRAP_TMUXCTLD = f"bootstrap gui/{UID}"


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
    (repo / "token-api" / "web" / "ops").mkdir(parents=True, exist_ok=True)
    (repo / "token-api" / "web" / "contracts").mkdir(parents=True, exist_ok=True)
    (repo / "discord-daemon").mkdir(parents=True, exist_ok=True)
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
        "npm",
        "tmux",
        "tmuxctl",
        "tx",
    ]:
        p = stub_bin / name
        p.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{logfile}"\nexit 0\n')
        p.chmod(0o755)

    # curl: log + emit a connected payload (discord health greps for it) + exit 0.
    # For token-restart's -w '%{http_code}' refresh path, emit a bare 200.
    # The /health payload self-reports git_sha = STUB_RUNNING_SHA so token-restart
    # can compare the live process's launched SHA against the checkout HEAD.
    curl = stub_bin / "curl"
    curl.write_text(
        f"""#!/usr/bin/env bash
echo "curl $*" >> "{logfile}"
for arg in "$@"; do
  if [[ "$arg" == *"%{{http_code}}"* ]]; then echo 200; exit 0; fi
done
health_counter="${{STUB_CURL_HEALTH_COUNTER:-}}"
if [[ -n "$health_counter" ]]; then
  count=0; [[ -f "$health_counter" ]] && count="$(cat "$health_counter")"
  count=$((count + 1)); echo "$count" > "$health_counter"
  if (( count <= 1 )); then sha="${{STUB_RUNNING_SHA:-}}"; else sha="NEW111"; fi
else
  sha="${{STUB_RUNNING_SHA:-}}"
fi
echo '{{"connected": true, "git_sha": "'"$sha"'"}}'
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
if [[ ( "$sub" == "checkout" || "$sub" == "clean" ) && "$*" == *"token-api/ui/ops"* && -n "${STUB_OPS_DIRT_CLEARED:-}" ]]; then
  touch "$STUB_OPS_DIRT_CLEARED"
  exit 0
fi
if [[ "$sub" == "status" && "${STUB_DIRTY_RUNTIME:-}" == "1" ]]; then
  dirty_paths="${STUB_DIRTY_PATHS:-token-api/main.py}"
  pathspec=""
  prev=""
  for arg in "$@"; do
    if [[ "$prev" == "--" ]]; then pathspec="$arg"; fi
    prev="$arg"
  done
  for p in $dirty_paths; do
    if [[ -n "${STUB_OPS_DIRT_CLEARED:-}" && -f "$STUB_OPS_DIRT_CLEARED" && ( "$p" == token-api/ui/ops/* || "$p" == token-api/ui/ops ) ]]; then
      continue
    fi
    if [[ -n "$pathspec" && "$p" != "$pathspec"/* && "$p" != "$pathspec" ]]; then
      continue
    fi
    echo " M $p"
  done
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
  show-ref)
    # Only the shunt's wip/live-dirty-* refs are reported absent (so the
    # uniqueness loop terminates); any other ref-existence check is unaffected.
    case "$*" in
      *wip/live-dirty-*) exit 1 ;;
      *) exit 0 ;;
    esac
    ;;
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
        "STUB_OPS_DIRT_CLEARED": str(tmp_path / "ops_dirt_cleared"),
        "STUB_CURL_HEALTH_COUNTER": str(tmp_path / "curl_health_count"),
        # Default the post-restart /health SHA to the deploy target so the new
        # deploy-verification gate can pass under stubs. Tests can still opt into
        # a stale process by overriding env["STUB_RUNNING_SHA"] (e.g. "STALE999").
        "STUB_RUNNING_SHA": "NEW111",
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
    assert RESTART_TOKENAPI in calls
    assert KICK_DISCORD not in calls
    assert "push-mobile -a" not in calls


def test_discord_change_restarts_discord_and_verifies_token_api(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "discord-daemon/daemon.js")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_DISCORD in calls
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    assert "push-mobile -a" not in calls


def test_mobile_change_runs_push_mobile_and_verifies_token_api(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "mobile/termux-toolbar-toggle")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert "push-mobile -a" in calls
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    assert KICK_DISCORD not in calls


def test_satellite_or_refresh_helper_change_refreshes_wsl_and_verifies_token_api(
    tmp_path: Path,
) -> None:
    env, logfile = _stub_env(
        tmp_path,
        "token-api/token-satellite.py\ntoken-api/scripts/token-satellite-refresh",
    )
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    assert "/runtime/refresh" in calls
    assert "/restart" not in calls
    assert KICK_DISCORD not in calls


def test_ahk_and_cli_changes_refresh_wsl_runtime(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "ahk/foo.ahk\ncli-tools/lib/nas-path.sh")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert "/runtime/refresh" in calls
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    assert KICK_DISCORD not in calls


def test_token_api_lockfile_change_restarts_mac_and_refreshes_wsl(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/uv.lock")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls
    assert "/runtime/refresh" in calls
    assert KICK_DISCORD not in calls


def test_multiple_changed_services_all_restart(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/routes/tts.py\ndiscord-daemon/daemon.js")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls
    assert KICK_DISCORD in calls


# ── tmuxctld daemon: area change → launchd bounce (mirrors discord) ──────────


def test_tmuxctld_daemon_code_change_restarts_tmuxctld(tmp_path: Path) -> None:
    # A change in the daemon code area bounces the tmuxctld daemon (launchctl
    # kickstart -k, like discord). token-api also restarts so /health.git_sha
    # verifies the merged SHA. The tmuxctl subtree lives under root tmuxctld/lib; daemon code is Mac-local.
    env, logfile = _stub_env(tmp_path, "tmuxctld/lib/tmuxctl/daemon.py")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TMUXCTLD in calls
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    assert KICK_DISCORD not in calls
    # Every advanced merge now refreshes/verifies the WSL satellite runtime SHA,
    # even if the changed service is Mac-local.
    assert "/runtime/refresh" in calls
    # The daemon bounce must NOT shell out to the tmux/tmuxctl/tx fleet tooling.
    _assert_no_fleet_wipe(calls)


def test_tmuxctld_entrypoint_change_restarts_tmuxctld_and_verifies_token_api(
    tmp_path: Path,
) -> None:
    # The Mac-local entrypoint routes to the daemon restart only — no WSL refresh.
    env, logfile = _stub_env(tmp_path, "tmuxctld/bin/tmuxctld")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert KICK_TMUXCTLD in calls
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    assert KICK_DISCORD not in calls
    assert "/runtime/refresh" in calls
    _assert_no_fleet_wipe(calls)


def test_tmuxctld_plist_change_reinstalls_daemon(tmp_path: Path) -> None:
    # A plist change is not honored by `kickstart -k`, so the daemon must be
    # reinstalled: copy the synced plist into LaunchAgents, then bootout+bootstrap.
    # TMUXCTLD_SRC_PLIST/TMUXCTLD_PLIST are overridden to tmp paths so the test
    # never touches the live LaunchAgent.
    env, logfile = _stub_env(tmp_path, "tmuxctld/launchd/ai.tokenclaw.tmuxctld.plist")
    src_plist = tmp_path / "src.tmuxctld.plist"
    src_plist.write_text("<plist>synced</plist>\n")
    dst_plist = tmp_path / "LaunchAgents" / "ai.tokenclaw.tmuxctld.plist"
    env["TMUXCTLD_SRC_PLIST"] = str(src_plist)
    env["TMUXCTLD_PLIST"] = str(dst_plist)

    proc = _run(env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = logfile.read_text()
    # Plist path uses bootout+bootstrap, NOT kickstart.
    assert BOOTOUT_TMUXCTLD in calls
    assert f"{BOOTSTRAP_TMUXCTLD} {dst_plist}" in calls
    assert KICK_TMUXCTLD not in calls
    # The freshly-synced plist was copied into the (overridden) LaunchAgents path.
    assert dst_plist.exists()
    assert dst_plist.read_text() == src_plist.read_text()
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    _assert_no_fleet_wipe(calls)


# ── Ops cockpit deploy-time bundle refresh ────────────────────


def test_ops_source_change_rebuilds_bundle_restarts_token_api_and_refreshes_tabs(
    tmp_path: Path,
) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/web/ops/src/App.tsx")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = logfile.read_text()
    assert "npm ci --no-audit --no-fund" in calls
    assert "npm run build" in calls
    assert RESTART_TOKENAPI in calls
    assert "osascript" in calls
    assert "ops-ui" in proc.stdout


def test_ops_committed_bundle_change_also_rebuilds_bundle(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/ui/ops/index.html")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = logfile.read_text()
    assert "npm ci --no-audit --no-fund" in calls
    assert "npm run build" in calls
    assert RESTART_TOKENAPI in calls
    assert "ops-ui" in proc.stdout


def test_discord_deps_refresh_failure_degrades_but_token_api_still_restarts(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "discord-daemon/package-lock.json")
    npm = Path(env["PATH"].split(os.pathsep, 1)[0]) / "npm"
    npm.write_text(
        f"""#!/usr/bin/env bash
echo "npm $*" >> "{logfile}"
case "$PWD" in
  *discord-daemon*) echo "simulated npm EACCES" >&2; exit 13;;
esac
exit 0
"""
    )
    npm.chmod(0o755)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls, "Token-API must restart even after sidecar refresh failure"
    assert "discord-daemon deps refresh failed; degrading" in proc.stdout
    assert "Sidecars:" in proc.stdout and "discord-daemon-deps-refresh" in proc.stdout
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout


def test_docs_only_change_does_not_run_npm_without_generated_ops_dirt(
    tmp_path: Path,
) -> None:
    env, logfile = _stub_env(tmp_path, "README.md")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "npm " not in logfile.read_text()


def test_generated_ops_dirt_is_discarded_and_rebuilt_not_shunted(
    tmp_path: Path,
) -> None:
    env, logfile = _stub_env(tmp_path, "README.md")
    env["STUB_DIRTY_RUNTIME"] = "1"
    env["STUB_DIRTY_PATHS"] = "token-api/ui/ops/index.html"
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = logfile.read_text()
    assert "discarding generated token-api/ui/ops runtime dirt" in proc.stdout
    assert "auto-preserving WIP" not in proc.stdout
    assert "npm ci --no-audit --no-fund" in calls
    assert "npm run build" in calls
    assert RESTART_TOKENAPI in calls


def test_mixed_runtime_dirt_still_uses_wip_preservation(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "README.md")
    env["STUB_DIRTY_RUNTIME"] = "1"
    env["STUB_DIRTY_PATHS"] = "token-api/ui/ops/index.html token-api/main.py"
    proc = _run(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "auto-preserving WIP to wip/live-dirty-" in proc.stdout
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls


# ── Nothing deployable / no-op / fallback ────────────────────


def test_docs_only_change_still_restarts_token_api_for_deploy_proof(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "README.md\nTerra/Journal/Daily/2026-06-06.md")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    assert KICK_DISCORD not in calls
    assert "push-mobile -a" not in calls


def test_tmux_config_change_sources_running_server_once_without_service_restart(
    tmp_path: Path,
) -> None:
    env, logfile = _stub_env(tmp_path, "cli-tools/tmux/tmux-base.conf")
    stub_tmux = Path(env["PATH"].split(os.pathsep, 1)[0]) / "tmux"
    env["IMPERIUM_TMUX_BIN"] = str(stub_tmux)
    env["HOME"] = str(tmp_path)
    (tmp_path / ".tmux.conf").write_text("# test config; stub tmux only\n")

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = logfile.read_text()
    assert "tmux has-session" in calls
    assert f"tmux source-file {tmp_path / '.tmux.conf'}" in calls
    assert RESTART_TOKENAPI in calls
    assert "deploy verified: /health git_sha=NEW111" in proc.stdout
    assert KICK_DISCORD not in calls
    assert "push-mobile -a" not in calls


def test_no_advance_falls_back_to_full_restart(tmp_path: Path) -> None:
    # Sync is a no-op (HEAD already current) → full restart of the standard set,
    # regardless of what the (irrelevant) diff would say.
    env, logfile = _stub_env(tmp_path, "discord-daemon/daemon.js", no_advance=True)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls
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
    assert RESTART_TOKENAPI not in calls
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
    assert RESTART_TOKENAPI in calls
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
    assert RESTART_TOKENAPI in logfile.read_text()


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
    # For non-tmux-config deploys, token-restart should not shell to tmux/tmuxctl/tx at all.
    # (tmux config changes are the one explicit exception: they source ~/.tmux.conf.)
    for tool in ("tmux ", "tmuxctl ", "tx "):
        assert tool not in calls, f"deploy unexpectedly invoked {tool!r}:\n{calls}"


def test_mac_affecting_deploy_reloads_without_wiping_fleet(tmp_path: Path) -> None:
    # A token-api change restarts the Mac service (kickstart -k) but must not
    # touch the tmux fleet.
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls  # it DID reload the service…
    _assert_no_fleet_wipe(calls)  # …without wiping the fleet.


def test_no_advance_full_restart_still_never_wipes_fleet(tmp_path: Path) -> None:
    # The heaviest path: a no-advance sync falls back to a FULL restart of the
    # standard set (token-api + discord). Even then — deploy everything, but
    # never `tx restart`.
    env, logfile = _stub_env(tmp_path, "discord-daemon/daemon.js", no_advance=True)
    proc = _run(env)
    assert proc.returncode == 0, proc.stderr
    calls = logfile.read_text()
    assert RESTART_TOKENAPI in calls
    assert KICK_DISCORD in calls
    _assert_no_fleet_wipe(calls)


def test_opt_in_tmux_geometry_trace_captures_labeled_snapshots(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    env["TOKEN_RESTART_TRACE_TMUX_GEOMETRY"] = "1"
    trace_log = tmp_path / "trace" / "token-restart-tmux-geometry.log"
    env["TOKEN_RESTART_TMUX_GEOMETRY_LOG"] = str(trace_log)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    calls = logfile.read_text()
    assert "tmux list-clients -F" in calls
    assert "tmux list-windows -a -F" in calls
    assert "tmux show-options -gqv window-size" in calls
    assert "tmux show-options -gqv aggressive-resize" in calls

    trace = trace_log.read_text()
    for label in (
        "start full_restart",
        "before restart_mac",
        "after token-api health/git-sha verification",
        "before Ops browser refresh",
        "after Ops browser refresh",
        "final summary",
    ):
        assert label in trace
    assert "-- attached clients --" in trace
    assert "-- windows --" in trace
    assert "-- global options --" in trace


def test_opt_in_tmux_geometry_trace_failures_are_fail_soft(tmp_path: Path) -> None:
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    env["TOKEN_RESTART_TRACE_TMUX_GEOMETRY"] = "1"
    trace_log = tmp_path / "trace" / "token-restart-tmux-geometry.log"
    env["TOKEN_RESTART_TMUX_GEOMETRY_LOG"] = str(trace_log)
    tmux = Path(env["PATH"].split(os.pathsep, 1)[0]) / "tmux"
    tmux.write_text(
        f'''#!/usr/bin/env bash
echo "tmux $*" >> "{logfile}"
echo "simulated tmux failure" >&2
exit 88
'''
    )
    tmux.chmod(0o755)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert RESTART_TOKENAPI in logfile.read_text()
    assert "simulated tmux failure" in trace_log.read_text()


def test_trailing_resync_clears_pinned_sha_to_converge_newest_main(tmp_path: Path) -> None:
    """Regression for #413/#418-style lag: a lock contender queued a trailing
    re-sync, but the holder kept its original TOKEN_RESTART_TARGET_SHA pinned.
    The trailing pass must unset that target so resolve_deploy_target uses the
    freshly fetched bare main instead of falsely no-oping on the older SHA.
    """
    env, _logfile = _stub_env(tmp_path, "token-api/main.py")
    script = f"""
set -euo pipefail
source {str(TOKEN_RESTART)!r}
TOKEN_RESTART_TARGET_SHA=oldpinned
SYNC_DID_ADVANCE=true
SYNC_CHANGED_PATHS=stale
RESTART_TOKENAPI=true
_reset_deploy_run_state
[[ -z "${{TOKEN_RESTART_TARGET_SHA}}" ]] || {{ echo "still pinned: $TOKEN_RESTART_TARGET_SHA"; exit 42; }}
[[ "$SYNC_DID_ADVANCE" == false ]]
[[ -z "$SYNC_CHANGED_PATHS" ]]
[[ "$RESTART_TOKENAPI" == false ]]
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_deploy_verify_alarm_fails_when_health_sha_mismatch(tmp_path: Path) -> None:
    env, _logfile = _stub_env(tmp_path, "token-api/main.py")
    env["STUB_RUNNING_SHA"] = "STALE999"
    env.pop("STUB_CURL_HEALTH_COUNTER", None)
    proc = _run(env)
    assert proc.returncode != 0
    assert "DEPLOY VERIFY ALARM" in proc.stdout
    assert "STALE999" in proc.stdout


def test_superseded_deploy_drops_current_restart_set(tmp_path: Path) -> None:
    """Last-merge-wins invariant: while holding the deploy mutex, a pending
    sentinel means a newer webhook arrived. The current deploy must stop doing
    obsolete restarts and let the trailing pass re-sync newest main.
    """
    env, _logfile = _stub_env(tmp_path, "token-api/main.py")
    lockdir = tmp_path / "deploy.lock"
    lockdir.mkdir()
    (lockdir / "redeploy-pending").write_text("1")
    script = f"""
set -euo pipefail
source {str(TOKEN_RESTART)!r}
DEPLOY_LOCKDIR={str(lockdir)!r}
DEPLOY_LOCK_HELD=true
abort_deploy_if_superseded "unit-test"
rm -f "$DEPLOY_LOCKDIR/redeploy-pending"
if abort_deploy_if_superseded "unit-test"; then
  echo "false supersede"
  exit 42
fi
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "deploy superseded during unit-test" in proc.stdout
