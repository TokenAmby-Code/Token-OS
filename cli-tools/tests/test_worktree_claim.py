"""Gap 1 (D1/D2) — per-branch worktree claim + create-race lock for worktree-setup.

These exercise the LIVE guard this dispatch ships: a local-FS lock over the
(project, branch) create race plus a per-branch claim that refuses a 2nd
worktree on a live branch without --force.

The fixture builds a throwaway project (temp HOME, temp bare repo, temp conf)
so nothing touches the real NAS, ~/.config, or live worktrees.
"""

import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKTREE_SETUP = ROOT / "cli-tools" / "bin" / "worktree-setup"

RunFn = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class Project:
    parent: Path
    env: dict[str, str]
    run: RunFn


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    """A self-contained throwaway worktree project rooted at a temp HOME."""
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    src.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()

    base_env = dict(os.environ)
    base_env.update(
        {
            "HOME": str(home),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )

    # Seed a normal repo with one commit, then make a bare clone for the worktrees.
    _git("init", "-b", "main", str(src), env=base_env)
    (src / "README.md").write_text("seed\n", encoding="utf-8")
    _git("add", "-A", cwd=src, env=base_env)
    _git("commit", "-m", "seed", cwd=src, env=base_env)
    bare = tmp_path / "proj.git"
    _git("clone", "--bare", str(src), str(bare), env=base_env)

    conf_dir = home / ".config" / "worktrees"
    conf_dir.mkdir(parents=True)
    parent = home / "worktrees" / "clmtest"
    (conf_dir / "clmtest.conf").write_text(
        f"BARE_REPO={bare}\nWORKTREE_PARENT={parent}\nSECRETS_DIR={secrets}\n",
        encoding="utf-8",
    )

    def run(
        *args: str, timeout: int = 60, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(base_env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(WORKTREE_SETUP), *args, "--project", "clmtest", "--no-transplant", "--skip-sync"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return Project(parent=parent, env=base_env, run=run)


def test_require_free_flag_accepted(project: Project) -> None:
    """--require-free is a real flag and creates the first worktree cleanly."""
    res = project.run("alpha", "--require-free")
    assert res.returncode == 0, res.stderr
    assert (project.parent / "wt-alpha" / ".git").exists()


def test_force_flag_accepted(project: Project) -> None:
    """--force is a real flag and does not break a normal create."""
    res = project.run("solo", "--require-free", "--force")
    assert res.returncode == 0, res.stderr


def test_require_free_refuses_second_claim_without_force(project: Project) -> None:
    """A 2nd worktree on a live branch is refused without --force (D1)."""
    first = project.run("beta", "--require-free")
    assert first.returncode == 0, first.stderr

    # Drop the dir but leave the git worktree registration (a live claim that
    # the plain dir-existence guard would miss) to prove the claim is real.
    shutil.rmtree(project.parent / "wt-beta")

    second = project.run("beta", "--require-free")
    assert second.returncode != 0
    assert "claim" in second.stderr.lower() or "already" in second.stderr.lower()


def test_force_overrides_live_claim(project: Project) -> None:
    """--force lets a dispatch reclaim a branch already checked out (D1)."""
    first = project.run("gamma", "--require-free")
    assert first.returncode == 0, first.stderr
    shutil.rmtree(project.parent / "wt-gamma")

    forced = project.run("gamma", "--require-free", "--force")
    assert forced.returncode == 0, forced.stderr


def _assert_race_serializes(project: Project, branch: str, extra_env: dict[str, str]) -> None:
    results: dict[int, subprocess.CompletedProcess[str]] = {}

    def worker(idx: int) -> None:
        results[idx] = project.run(branch, "--require-free", extra_env=extra_env)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(r.returncode for r in results.values())
    assert codes[0] == 0, "exactly one create should succeed"
    assert codes[1] != 0, "the racing create should be refused, not also succeed"
    assert (project.parent / f"wt-{branch}" / ".git").exists()


def test_concurrent_create_race_serializes(project: Project) -> None:
    """Two concurrent --require-free creates of one branch: exactly one wins.

    Exercises whichever lock primitive the runner has (flock on Linux/CI).
    """
    _assert_race_serializes(project, "race", {})


def test_concurrent_create_race_serializes_mkdir_fallback(project: Project) -> None:
    """Same guarantee via the portable mkdir lock (the only macOS path).

    WORKTREE_CLAIM_NO_FLOCK forces the fallback so it is proven deterministically
    even on a flock-equipped CI runner.
    """
    _assert_race_serializes(project, "race-mkdir", {"WORKTREE_CLAIM_NO_FLOCK": "1"})


def test_main_master_refused_by_default(project: Project) -> None:
    for branch in ("main", "master"):
        res = project.run(branch)
        assert res.returncode == 64
        assert "main/master are protected" in res.stderr


def test_main_allowed_only_with_admin_flag(project: Project) -> None:
    res = project.run("main", "--allow-protected-branch", "--existing")
    assert res.returncode == 0, res.stderr
    assert (project.parent / "wt-main" / ".git").exists()
