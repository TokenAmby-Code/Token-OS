from __future__ import annotations

import os
import subprocess
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
TOKEN_RESTART = BIN / "token-restart"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=check,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test User",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test User",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )


def _rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", ref).stdout.strip()


def _git_dir(git_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        text=True,
        capture_output=True,
        check=True,
    )


def _stub_side_effect_tools(tmp_path: Path) -> Path:
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir(exist_ok=True)
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
        "curl",
    ]:
        p = stub_bin / name
        p.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        p.chmod(0o755)

    uname = stub_bin / "uname"
    uname.write_text('#!/usr/bin/env bash\necho "Darwin"\n', encoding="utf-8")
    uname.chmod(0o755)
    return stub_bin


def _make_repos(tmp_path: Path) -> tuple[Path, Path, Path, str, str]:
    upstream = tmp_path / "upstream"
    bare = tmp_path / "token-os.git"
    runtime = tmp_path / "runtime"

    subprocess.run(["git", "init", "-b", "main", str(upstream)], check=True, capture_output=True)
    (upstream / "README.md").write_text("c1\n", encoding="utf-8")
    _git(upstream, "add", "README.md")
    _git(upstream, "commit", "-m", "c1")
    c1 = _rev(upstream, "HEAD")

    subprocess.run(
        ["git", "clone", "--bare", str(upstream), str(bare)], check=True, capture_output=True
    )
    subprocess.run(["git", "clone", str(bare), str(runtime)], check=True, capture_output=True)

    (upstream / "docs").mkdir()
    (upstream / "docs" / "x.md").write_text("c2\n", encoding="utf-8")
    _git(upstream, "add", "docs/x.md")
    _git(upstream, "commit", "-m", "c2")
    c2 = _rev(upstream, "HEAD")

    assert _rev(runtime, "HEAD") == c1
    assert _rev(runtime, "origin/main") == c1
    return upstream, bare, runtime, c1, c2


def _env(tmp_path: Path, bare: Path, runtime: Path) -> dict[str, str]:
    stub_bin = _stub_side_effect_tools(tmp_path)
    return {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "IMPERIUM_MACHINE": "mac",
        "TOKEN_OS_BARE_REPO": str(bare),
        "CD_LIVE_CHECKOUT": str(runtime),
        "TOKEN_OS_WORKTREE_CONF": str(tmp_path / "missing.conf"),
        "TOKEN_OS_SECRETS_DIR": "",
        "TOKEN_SATELLITE_REFRESH_SECRET": "refresh-secret",
    }


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(TOKEN_RESTART)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_runtime_reconciles_head_and_origin_main_to_bare_main(tmp_path: Path) -> None:
    _upstream, bare, runtime, _c1, c2 = _make_repos(tmp_path)

    proc = _run(_env(tmp_path, bare, runtime))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _rev(runtime, "HEAD") == _rev(bare, "refs/heads/main") == c2
    assert _rev(runtime, "origin/main") == c2
    assert "runtime advanced" in proc.stdout


def _wip_branches(bare: Path) -> list[str]:
    out = _git_dir(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/wip/").stdout
    return [b for b in out.splitlines() if b.strip()]


def test_dirty_runtime_shunts_wip_to_branch_then_deploys_clean(tmp_path: Path) -> None:
    """The new invariant: a dirty runtime NEVER blocks the deploy. Stray WIP
    (tracked edits + untracked files) is auto-committed to a timestamped
    wip/live-dirty-<ts> branch, pushed to the bare cache, and the tree is then
    reset onto the target SHA. Replaces the #280 dirty-tree-abort."""
    _upstream, bare, runtime, _c1, c2 = _make_repos(tmp_path)
    (runtime / "README.md").write_text("unexpected runtime WIP\n", encoding="utf-8")
    (runtime / "STRAY.txt").write_text("stray untracked work\n", encoding="utf-8")

    proc = _run(_env(tmp_path, bare, runtime))

    # Deploy succeeded over a now-clean tree at the target SHA.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _rev(runtime, "HEAD") == _rev(bare, "refs/heads/main") == c2
    assert _git(runtime, "status", "--porcelain").stdout.strip() == ""

    # The WIP was preserved to a timestamped branch in the durable bare cache.
    wips = _wip_branches(bare)
    assert len(wips) == 1, wips
    wip = wips[0]
    assert wip.startswith("wip/live-dirty-"), wip
    assert "auto-preserving WIP" in proc.stdout
    assert "pushed to bare cache" in proc.stdout

    # Both the modified tracked file and the untracked file are recoverable.
    assert _git_dir(bare, "show", f"{wip}:README.md").stdout == "unexpected runtime WIP\n"
    assert _git_dir(bare, "show", f"{wip}:STRAY.txt").stdout == "stray untracked work\n"


def test_dirty_runtime_already_at_target_still_shunts_and_cleans(tmp_path: Path) -> None:
    """Even when the runtime is already at the target SHA, a dirty tree must be
    preserved and the tree reclaimed clean — dirt is never left to fester."""
    _upstream, bare, runtime, _c1, c2 = _make_repos(tmp_path)
    # Advance bare main to c2 and put the runtime exactly at c2 (already current).
    _git_dir(bare, "fetch", "origin", "main")
    _git_dir(bare, "update-ref", "refs/heads/main", c2)
    _git_dir(bare, "update-ref", "refs/remotes/origin/main", c2)
    _git(runtime, "fetch", str(bare), "+refs/heads/*:refs/remotes/local-bare/*")
    _git(runtime, "checkout", "--detach", c2)
    (runtime / "README.md").write_text("late dirty edit\n", encoding="utf-8")

    proc = _run(_env(tmp_path, bare, runtime))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _rev(runtime, "HEAD") == c2
    assert _git(runtime, "status", "--porcelain").stdout.strip() == ""
    wips = _wip_branches(bare)
    assert len(wips) == 1, wips
    assert _git_dir(bare, "show", f"{wips[0]}:README.md").stdout == "late dirty edit\n"


def test_no_advance_deploy_still_refreshes_runtime_origin_main(tmp_path: Path) -> None:
    _upstream, bare, runtime, c1, c2 = _make_repos(tmp_path)
    _git_dir(bare, "fetch", "origin", "main")
    _git_dir(bare, "update-ref", "refs/heads/main", c2)
    _git_dir(bare, "update-ref", "refs/remotes/origin/main", c2)
    _git(runtime, "fetch", str(bare), "+refs/heads/*:refs/remotes/local-bare/*")
    _git(runtime, "checkout", "--detach", c2)
    _git(runtime, "update-ref", "refs/remotes/origin/main", c1)

    proc = _run(_env(tmp_path, bare, runtime))

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _rev(runtime, "HEAD") == c2
    assert _rev(runtime, "origin/main") == c2
    assert "runtime already at" in proc.stdout
