"""token-restart serializes concurrent deploys under an atomic mkdir lockdir and
coalesces a trailing re-sync so rapid back-to-back merges converge on the newest
bare-main SHA — without two CD actors (the webhook spawn + a manual run) racing on
the uchg-locked runtime .git and emitting the false "failed to fetch bare refs" /
"failed to write-protect" failures that motivated the mutex.

Reuses the smart-deploy stub harness (stubbed git + launchctl/curl/ssh/sleep/…),
pointing TOKEN_OS_DEPLOY_LOCKDIR at a per-test lockdir so we can pre-seed holders
and observe the mutex/coalescing behaviour deterministically.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from test_token_restart_smart_deploy import RESTART_TOKENAPI, _run, _stub_env


def _lockdir(tmp_path: Path) -> Path:
    return tmp_path / "token-os-cd-deploy.lock"


def test_contender_queues_and_bows_out_when_holder_is_live(tmp_path: Path) -> None:
    # A second token-restart that finds a LIVE holder must queue a trailing
    # re-sync (redeploy-pending sentinel), print the "deploy already in progress"
    # notice, exit 0, and NOT run full_restart or disturb the holder's lock.
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    lockdir = _lockdir(tmp_path)
    lockdir.mkdir()
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (lockdir / "owner").write_text(f"{holder.pid} NEW111\n")
        env["TOKEN_OS_DEPLOY_LOCKDIR"] = str(lockdir)

        proc = _run(env)

        assert proc.returncode == 0, proc.stderr
        assert "deploy already in progress" in proc.stdout
        assert (lockdir / "redeploy-pending").exists(), "must queue a trailing re-sync"
        assert RESTART_TOKENAPI not in logfile.read_text(), "contender must not deploy"
        assert (lockdir / "owner").exists(), "contender must not steal the live lock"
    finally:
        holder.terminate()
        holder.wait()


def test_stale_lock_from_dead_pid_is_reclaimed(tmp_path: Path) -> None:
    # A lockdir left by a crashed deploy (owner PID no longer alive) must be
    # reclaimed: the next token-restart deletes it, deploys, and cleans up.
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    lockdir = _lockdir(tmp_path)
    lockdir.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()  # reap → PID is now dead
    (lockdir / "owner").write_text(f"{dead.pid} DEAD000\n")
    env["TOKEN_OS_DEPLOY_LOCKDIR"] = str(lockdir)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert "reclaiming stale lock" in proc.stderr
    assert RESTART_TOKENAPI in logfile.read_text(), "reclaiming deploy must proceed"
    assert not lockdir.exists(), "mutex released after the deploy"


def _launchctl_touches_pending_once(tmp_path: Path, logfile: Path, lockdir: Path) -> None:
    """Rewrite the launchctl stub so the FIRST launchctl call (the pid query at
    the start of the PRIMARY deploy's restart_mac) drops a redeploy-pending
    sentinel — simulating a contender that queued a trailing re-sync mid-deploy —
    and never again."""
    launchctl = tmp_path / "stubbin" / "launchctl"
    launchctl.write_text(
        f"""#!/usr/bin/env bash
echo "launchctl $*" >> "{logfile}"
marker="{lockdir}/.contender-fired"
if [[ ! -e "$marker" ]]; then
  touch "{lockdir}/redeploy-pending" 2>/dev/null || true
  touch "$marker" 2>/dev/null || true
fi
exit 0
"""
    )
    launchctl.chmod(0o755)


def test_holder_runs_one_trailing_resync_when_contender_queues(tmp_path: Path) -> None:
    # The holder, after its primary deploy, must perform exactly ONE trailing
    # re-sync when a contender queued one mid-deploy — converging on the newest
    # main without looping (the sentinel is cleared before the re-run, and our
    # stub only queues once).
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    lockdir = _lockdir(tmp_path)
    env["TOKEN_OS_DEPLOY_LOCKDIR"] = str(lockdir)
    _launchctl_touches_pending_once(tmp_path, logfile, lockdir)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert "trailing re-sync 1/3" in proc.stdout
    assert "trailing re-sync 2/3" not in proc.stdout, "must not loop on a single queue"
    assert not lockdir.exists(), "mutex released after the trailing re-sync"


def test_trailing_resync_noops_when_nothing_new(tmp_path: Path) -> None:
    # When the trailing re-sync re-resolves and finds the runtime already at the
    # newest main (no advance), it must NO-OP — not fall back to a full restart of
    # every service. The primary here is a no-advance full restart; the queued
    # trailing run then converges to a clean no-op.
    #
    # Invariant: this no-op is now contingent on running==checkout. _stub_env
    # defaults STUB_RUNNING_SHA to the git stub's HEAD (NEW111 under no_advance),
    # so the live process is NOT stale and the trailing run legitimately no-ops.
    # The stale-process counterpart (running != checkout) is exercised by
    # test_trailing_resync_restarts_when_process_stale below.
    env, logfile = _stub_env(tmp_path, "token-api/main.py", no_advance=True)
    lockdir = _lockdir(tmp_path)
    env["TOKEN_OS_DEPLOY_LOCKDIR"] = str(lockdir)
    _launchctl_touches_pending_once(tmp_path, logfile, lockdir)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert "trailing re-sync 1/3" in proc.stdout
    assert "runtime already at newest main" in proc.stdout
    assert not lockdir.exists()


def test_single_deploy_acquires_and_releases_cleanly(tmp_path: Path) -> None:
    # The uncontended path: a lone deploy acquires the mutex, deploys, and removes
    # the lockdir — no leftover lock to block the next deploy.
    env, logfile = _stub_env(tmp_path, "token-api/main.py")
    lockdir = _lockdir(tmp_path)
    env["TOKEN_OS_DEPLOY_LOCKDIR"] = str(lockdir)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert RESTART_TOKENAPI in logfile.read_text()
    assert "deploy already in progress" not in proc.stdout
    assert not lockdir.exists(), "mutex must be released on a clean exit"


def test_trailing_resync_restarts_when_process_stale(tmp_path: Path) -> None:
    # Regression for the stranded socket-activation cutover (#345): a trailing
    # coalescing re-sync that finds NO new commits must STILL bounce token-api
    # when the live process is serving stale code (its /health git_sha != the
    # checkout HEAD). The checkout advanced without a paired restart, so the
    # deploy is NOT actually finished — "nothing further to redeploy" would
    # strand the old process forever.
    env, logfile = _stub_env(tmp_path, "token-api/main.py", no_advance=True)
    env["STUB_RUNNING_SHA"] = "STALE999"  # live process != checkout HEAD (NEW111)
    lockdir = _lockdir(tmp_path)
    env["TOKEN_OS_DEPLOY_LOCKDIR"] = str(lockdir)
    _launchctl_touches_pending_once(tmp_path, logfile, lockdir)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert "trailing re-sync 1/3" in proc.stdout
    assert RESTART_TOKENAPI in logfile.read_text(), "stale live process must bounce"
    assert "live token-api serving" in proc.stdout, "must report the stale state"
    assert "nothing further to redeploy" not in proc.stdout, (
        "must NOT claim convergence while the process is stale"
    )
    assert not lockdir.exists()


def test_already_at_sha_deploy_restarts_when_process_stale(tmp_path: Path) -> None:
    # Uncontended counterpart: a plain `token-restart --sync` that finds the
    # checkout already at the newest SHA (no advance, no contender) lands in the
    # full-restart `else` branch — but the staleness check guarantees token-api
    # bounces even though THIS invocation advanced nothing. Closes skip path #4
    # (already-at-SHA short-circuit leaving the process stale).
    env, logfile = _stub_env(tmp_path, "token-api/main.py", no_advance=True)
    env["STUB_RUNNING_SHA"] = "STALE999"

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert RESTART_TOKENAPI in logfile.read_text(), "stale live process must bounce"


def test_docs_only_deploy_restarts_stale_process(tmp_path: Path) -> None:
    # Skip path #2: a smart deploy that advanced but changed ONLY non-service
    # files (docs) would print "No deployable services changed" and skip every
    # restart — stranding a process that was already stale from an earlier
    # skipped bounce. The staleness check forces a token-api restart instead.
    env, logfile = _stub_env(tmp_path, "README.md")  # advance=True, docs-only
    env["STUB_RUNNING_SHA"] = "STALE999"  # process stale vs checkout HEAD (OLD000)

    proc = _run(env)

    assert proc.returncode == 0, proc.stderr
    assert RESTART_TOKENAPI in logfile.read_text(), "stale process must bounce"
    assert "No deployable services changed" not in proc.stdout
