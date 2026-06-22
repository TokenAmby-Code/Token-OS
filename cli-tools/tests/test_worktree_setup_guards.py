"""worktree-setup creation guards.

Two invariants, both load-bearing for the 2026-06-22 worktree-hygiene work:

1. main/master creation guard — agents work on feature branches; a worktree
   *on* main jams the CD bare-main fast-forward. worktree-setup must refuse a
   main/master target (admin escape: --allow-protected-branch). Detached/feature
   adds are unaffected (the deploy runtime is provisioned separately, detached).

2. quarantine bare guard — worktree-setup must REFUSE when the resolved
   BARE_REPO lives in a Synology recycle bin or a dated legacy archive. Cutting a
   worktree from such a bare binds its commondir into a purge target → silent
   data loss when the bin empties (the legacy-bare incident root cause).

The fixture builds a throwaway project (temp HOME, temp bare, temp conf) so
nothing touches the real NAS, ~/.config, or live worktrees.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKTREE_SETUP = ROOT / "cli-tools" / "bin" / "worktree-setup"


def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> dict[str, object]:
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    secrets = tmp_path / "secrets"
    secrets.mkdir()

    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "WORKTREE_PORTS_NO_FLOCK": "1",
        }
    )

    _git("init", "-b", "main", str(src), env=env)
    (src / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=src, env=env)
    _git("commit", "-m", "seed", cwd=src, env=env)
    bare = tmp_path / "proj.git"
    _git("clone", "--bare", str(src), str(bare), env=env)

    conf_dir = home / ".config" / "worktrees"
    conf_dir.mkdir(parents=True)
    parent = home / "worktrees" / "guardtest"

    def write_conf(bare_path: Path) -> None:
        (conf_dir / "guardtest.conf").write_text(
            f"BARE_REPO={bare_path}\nWORKTREE_PARENT={parent}\nSECRETS_DIR={secrets}\n"
        )

    write_conf(bare)

    def setup(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(WORKTREE_SETUP),
                *args,
                "--project",
                "guardtest",
                "--no-transplant",
                "--skip-sync",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    return {
        "home": home,
        "bare": bare,
        "parent": parent,
        "env": env,
        "setup": setup,
        "write_conf": write_conf,
        "tmp": tmp_path,
    }


# ── main/master creation guard ───────────────────────────────────────────────


def test_refuses_main_worktree(project) -> None:
    res = project["setup"]("main", "--existing")
    assert res.returncode != 0, res.stdout
    assert "protected" in res.stderr.lower() or "main/master" in res.stderr.lower()
    assert not (project["parent"] / "wt-main").exists()


def test_refuses_master_worktree(project) -> None:
    res = project["setup"]("master")
    assert res.returncode != 0
    assert not (project["parent"] / "wt-master").exists()


def test_allows_feature_branch(project) -> None:
    res = project["setup"]("feature-x")
    assert res.returncode == 0, res.stderr
    wt = project["parent"] / "wt-feature-x"
    assert wt.exists()
    assert _git("-C", str(wt), "branch", "--show-current", env=project["env"]) == "feature-x"


def test_admin_escape_allows_main(project) -> None:
    res = project["setup"]("main", "--existing", "--allow-protected-branch")
    assert res.returncode == 0, res.stderr
    assert (project["parent"] / "wt-main").exists()


# ── quarantine bare guard ────────────────────────────────────────────────────


def test_refuses_recycle_bin_bare(project) -> None:
    """A BARE_REPO inside #recycle must be refused — never bind a worktree there."""
    recycle_bare = project["tmp"] / "#recycle" / "proj.git"
    recycle_bare.parent.mkdir(parents=True)
    _git("clone", "--bare", str(project["bare"]), str(recycle_bare), env=project["env"])
    project["write_conf"](recycle_bare)

    res = project["setup"]("feature-y")
    # Assert the quarantine guard's own exit code (64), not just generic failure,
    # so the test pins down *which* guard fired.
    assert res.returncode == 64, res.stdout
    assert "recycle" in res.stderr.lower() or "quarantin" in res.stderr.lower()
    assert not (project["parent"] / "wt-feature-y").exists()


def test_refuses_dated_legacy_archive_bare(project) -> None:
    legacy_bare = project["tmp"] / "Token-OS.legacy-20260610" / "proj.git"
    legacy_bare.parent.mkdir(parents=True)
    _git("clone", "--bare", str(project["bare"]), str(legacy_bare), env=project["env"])
    project["write_conf"](legacy_bare)

    res = project["setup"]("feature-z")
    assert res.returncode == 64
    assert not (project["parent"] / "wt-feature-z").exists()
