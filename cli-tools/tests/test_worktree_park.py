"""park_worktrees_off_main — the CD reconciler invariant (worktree-on-main jam).

Regression for the 2026-06-22 incident: a worktree checked out *on* branch
`main` carrying uncommitted edits jams the CD/pr-merge bare-main fast-forward —
`git fetch <remote> main:main` (and `git pull --ff-only`) refuse to update a
branch ref that is checked out in a linked worktree:

    fatal: refusing to fetch into branch 'refs/heads/main' checked out at '<wt>'

The reconciler parks any worktree found on main/master by detaching it in place
to its current SHA, which frees the branch ref so the FF succeeds. Detaching to
the same commit touches no files, so a dirty worktree keeps its edits. The bare
repo itself and already-detached worktrees (e.g. the deploy-owned live runtime,
which is detached HEAD by design) are never matched.

These tests source the shipped lib and call the function directly against a
throwaway bare repo + worktrees, so nothing touches the real NAS or live tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PARK_LIB = ROOT / "cli-tools" / "lib" / "worktree-park.sh"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _park(bare: Path) -> subprocess.CompletedProcess[str]:
    """Source the lib and invoke park_worktrees_off_main against `bare`."""
    script = f'source "{PARK_LIB}"; park_worktrees_off_main "{bare}"'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def _branch_of(wt: Path) -> str:
    """Symbolic branch name, or '' when detached."""
    res = subprocess.run(
        ["git", "-C", str(wt), "symbolic-ref", "-q", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    """A throwaway origin + local bare cache with linked worktrees."""
    env_args = [
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
    ]

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "f").write_text("v1\n")
    subprocess.run(["git", *env_args, "add", "-A"], cwd=seed, check=True)
    subprocess.run(["git", *env_args, "commit", "-qm", "c1"], cwd=seed, check=True)
    subprocess.run(["git", "push", "-q", str(origin), "HEAD:main"], cwd=seed, check=True)

    bare = tmp_path / "bare.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(origin), str(bare)], check=True)

    return tmp_path, bare, seed, origin


def _add_worktree(bare: Path, path: Path, ref: str, detach: bool = False) -> None:
    args = ["git", "--git-dir", str(bare), "worktree", "add", "-q"]
    if detach:
        args.append("--detach")
    args += [str(path), ref]
    subprocess.run(args, check=True, capture_output=True, text=True)


def test_parks_dirty_worktree_on_main_preserving_edits(repo) -> None:
    tmp, bare, _seed, _origin = repo
    wt = tmp / "wt-jam"
    _add_worktree(bare, wt, "main")
    (wt / "f").write_text("v1\nDIRTY\n")  # uncommitted edit

    assert _branch_of(wt) == "main"
    res = _park(bare)
    assert res.returncode == 0, res.stderr

    assert _branch_of(wt) == "", "worktree must be detached off main"
    # Dirty edit survives the detach (same SHA → no file changes).
    porcelain = _git("-C", str(wt), "status", "--porcelain", cwd=wt)
    assert "f" in porcelain, "uncommitted edits must be preserved"


def test_freed_ref_allows_bare_main_fast_forward(repo) -> None:
    tmp, bare, seed, origin = repo
    wt = tmp / "wt-jam"
    _add_worktree(bare, wt, "main")
    (wt / "f").write_text("v1\nDIRTY\n")

    # origin advances; bare main is now genuinely behind.
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-qm",
            "c2",
        ],
        cwd=seed,
        check=True,
    )
    subprocess.run(["git", "push", "-q", str(origin), "HEAD:main"], cwd=seed, check=True)

    # Before parking: the FF refspec is refused (the jam).
    blocked = subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", str(origin), "main:main"],
        capture_output=True,
        text=True,
    )
    assert blocked.returncode != 0
    assert "refusing to fetch" in blocked.stderr

    _park(bare)

    # After parking: the FF succeeds.
    ok = subprocess.run(
        ["git", "--git-dir", str(bare), "fetch", str(origin), "main:main"],
        capture_output=True,
        text=True,
    )
    assert ok.returncode == 0, ok.stderr


def test_master_is_also_parked(repo) -> None:
    tmp, bare, seed, _origin = repo
    # Create a master branch in the bare and check it out in a worktree.
    subprocess.run(["git", "--git-dir", str(bare), "branch", "master", "main"], check=True)
    wt = tmp / "wt-master"
    _add_worktree(bare, wt, "master")
    assert _branch_of(wt) == "master"

    _park(bare)
    assert _branch_of(wt) == "", "master worktree must be parked too"


def test_detached_worktree_untouched(repo) -> None:
    """The deploy-owned live runtime is detached HEAD — never matched/moved."""
    tmp, bare, _seed, _origin = repo
    wt = tmp / "wt-runtime"
    _add_worktree(bare, wt, "main", detach=True)
    before = _git("-C", str(wt), "rev-parse", "HEAD", cwd=wt)
    assert _branch_of(wt) == ""  # already detached

    res = _park(bare)
    assert res.returncode == 0, res.stderr
    after = _git("-C", str(wt), "rev-parse", "HEAD", cwd=wt)
    assert after == before, "detached runtime SHA must be untouched"


def test_feature_branch_worktree_untouched(repo) -> None:
    tmp, bare, _seed, _origin = repo
    subprocess.run(["git", "--git-dir", str(bare), "branch", "feature-x", "main"], check=True)
    wt = tmp / "wt-feature"
    _add_worktree(bare, wt, "feature-x")

    _park(bare)
    assert _branch_of(wt) == "feature-x", "feature worktree must stay on its branch"


def test_idempotent_no_worktrees_on_main(repo) -> None:
    """A clean fleet (nothing on main) parks nothing and exits 0."""
    tmp, bare, _seed, _origin = repo
    subprocess.run(["git", "--git-dir", str(bare), "branch", "feature-y", "main"], check=True)
    _add_worktree(bare, tmp / "wt-y", "feature-y")

    res = _park(bare)
    assert res.returncode == 0, res.stderr
    assert "parked" not in res.stdout
