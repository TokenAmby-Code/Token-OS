"""worktree-hygiene — enumerate / park / reanchor.

Covers the one-time cleanup tooling for the 2026-06-22 incident:
  - park: free a worktree off its branch without losing uncommitted edits
    (dirty WIP is committed to a collision-safe wip/ branch + pushed first).
  - reanchor: re-point a worktree whose commondir lives in a quarantined bare
    (recycle bin / dated legacy archive) back to the canonical bare, in place,
    with no work loss.
  - enumerate: read-only evidence report that flags LEGACY-bound worktrees.

Throwaway HOME + bares; nothing touches the real NAS or live worktrees.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HYGIENE = ROOT / "cli-tools" / "bin" / "worktree-hygiene"


def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def env_project(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, env=env)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=env)
    (seed / "f").write_text("v1\n")
    _git("add", "-A", cwd=seed, env=env)
    _git("commit", "-qm", "c1", cwd=seed, env=env)
    _git("push", "-q", str(origin), "HEAD:main", cwd=seed, env=env)

    canon = tmp_path / "canon.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(origin), str(canon)], check=True, env=env)

    parent = home / "worktrees" / "Token-OS"
    conf_dir = home / ".config" / "worktrees"
    conf_dir.mkdir(parents=True)
    (conf_dir / "hyg.conf").write_text(
        f"BARE_REPO={canon}\nCD_BARE_REPO={canon}\nWORKTREE_PARENT={parent}\nSECRETS_DIR={tmp_path}\n"
    )
    parent.mkdir(parents=True)

    def hyg(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(HYGIENE), *args, "--project", "hyg"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return {
        "tmp": tmp_path,
        "env": env,
        "origin": origin,
        "canon": canon,
        "parent": parent,
        "hyg": hyg,
    }


def test_park_clean_worktree_detaches(env_project) -> None:
    canon, env, parent = env_project["canon"], env_project["env"], env_project["parent"]
    wt = parent / "wt-feat"
    _git("--git-dir", str(canon), "worktree", "add", "-q", "-b", "feat", str(wt), "main", env=env)
    assert _git("-C", str(wt), "branch", "--show-current", env=env) == "feat"

    res = env_project["hyg"]("park", str(wt))
    assert res.returncode == 0, res.stderr
    # Detached HEAD ⇒ symbolic-ref exits non-zero (no branch).
    assert (
        subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"], env=env).returncode
        != 0
    ), "worktree must be detached after park"


def test_park_dirty_preserves_wip_branch(env_project) -> None:
    canon, env, parent = env_project["canon"], env_project["env"], env_project["parent"]
    wt = parent / "wt-dirty"
    _git("--git-dir", str(canon), "worktree", "add", "-q", "-b", "dirty", str(wt), "main", env=env)
    (wt / "f").write_text("v1\nUNSAVED\n")  # dirty

    res = env_project["hyg"]("park", str(wt))
    assert res.returncode == 0, res.stderr
    # A wip/park-* branch must now exist on the canonical bare carrying the edit.
    branches = _git("--git-dir", str(canon), "branch", "--list", "wip/park-*", env=env)
    assert "wip/park-" in branches, branches
    # Worktree is detached and the file still holds the edit.
    assert (
        subprocess.run(["git", "-C", str(wt), "symbolic-ref", "-q", "HEAD"], env=env).returncode
        != 0
    )
    assert "UNSAVED" in (wt / "f").read_text()


def test_reanchor_moves_commondir_to_canonical(env_project) -> None:
    tmp, env, canon, parent = (
        env_project["tmp"],
        env_project["env"],
        env_project["canon"],
        env_project["parent"],
    )
    # Build a quarantined (legacy) bare and a worktree bound to it.
    legacy = tmp / "#recycle" / "Token-OS.legacy-20260610" / "token-os.git"
    legacy.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(env_project["origin"]), str(legacy)],
        check=True,
        env=env,
    )
    wt = parent / "wt-legacy-bound"
    _git(
        "--git-dir",
        str(legacy),
        "worktree",
        "add",
        "-q",
        "-b",
        "fix/legacy",
        str(wt),
        "main",
        env=env,
    )
    (wt / "f").write_text("v1\nlegacy-work\n")
    _git("-C", str(wt), "commit", "-qam", "legacy commit", env=env)
    (wt / "f").write_text("v1\nlegacy-work\nDIRTY\n")  # leave a dirty edit too
    head = _git("-C", str(wt), "rev-parse", "HEAD", env=env)

    res = env_project["hyg"]("reanchor", str(wt))
    assert res.returncode == 0, res.stderr + res.stdout

    common = _git("-C", str(wt), "rev-parse", "--git-common-dir", env=env)
    assert Path(common).resolve() == canon.resolve(), common
    assert _git("-C", str(wt), "rev-parse", "HEAD", env=env) == head
    assert _git("-C", str(wt), "branch", "--show-current", env=env) == "fix/legacy"
    assert "DIRTY" in (wt / "f").read_text(), "uncommitted edit must survive reanchor"
    # canonical bare now lists the worktree
    listing = _git("--git-dir", str(canon), "worktree", "list", env=env)
    assert "wt-legacy-bound" in listing


def test_enumerate_flags_legacy_bound(env_project) -> None:
    tmp, env, canon, parent = (
        env_project["tmp"],
        env_project["env"],
        env_project["canon"],
        env_project["parent"],
    )
    # one canonical worktree + one legacy-bound worktree
    _git(
        "--git-dir",
        str(canon),
        "worktree",
        "add",
        "-q",
        "-b",
        "ok",
        str(parent / "wt-ok"),
        "main",
        env=env,
    )
    legacy = tmp / "#recycle" / "token-os.git"
    legacy.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(env_project["origin"]), str(legacy)],
        check=True,
        env=env,
    )
    _git("--git-dir", str(legacy), "worktree", "add", "-q", str(parent / "wt-bad"), "main", env=env)

    res = env_project["hyg"]("enumerate")
    assert res.returncode == 0, res.stderr
    assert "LEGACY" in res.stdout
    assert "LEGACY-bound" in res.stderr or "LEGACY-bound" in res.stdout
