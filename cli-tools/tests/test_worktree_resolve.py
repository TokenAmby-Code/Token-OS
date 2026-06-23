"""resolve_worktree_for_branch — the pr-merge wrong-worktree footgun.

Regression for the cwd-vs-branch bug: `pr-merge <N>` derived the *branch* to
delete from the PR (`gh pr view ... .headRefName`) but the *worktree* to remove
from the current working directory (`WORKTREE_PATH="$REPO_ROOT"`). Running it
from an unrelated worktree therefore merged the right branch while removing the
WRONG worktree — the caller's cwd — orphaning the PR's real worktree.

The fix resolves the removal target from the PR's head branch instead of cwd:
`resolve_worktree_for_branch <branch>` walks `git worktree list --porcelain`
and echoes the worktree path checked out on refs/heads/<branch>, or nothing when
the branch is not checked out in any linked worktree. A detached worktree (the
deploy-owned live runtime) and the bare repo itself carry no `branch` line and
are never matched.

These tests source the shipped lib and call the function directly against a
throwaway bare repo + worktrees, so nothing touches the real NAS or live tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RESOLVE_LIB = ROOT / "cli-tools" / "lib" / "worktree-resolve.sh"

# (tmp_path, bare, seed, origin) — see the `repo` fixture.
RepoFixture = tuple[Path, Path, Path, Path]


def _resolve(bare: Path, branch: str) -> str:
    """Source the lib and invoke resolve_worktree_for_branch against `bare`.

    Pass the lib path, branch, and bare git-dir as positional args ($1/$2/$3)
    rather than interpolating them into the command body — avoids quoting
    surprises if a path ever contains special characters.
    """
    res = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; resolve_worktree_for_branch "$2" "$3"',
            "_",
            str(RESOLVE_LIB),
            branch,
            str(bare),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> RepoFixture:
    """A throwaway origin + local bare cache with linked worktrees."""
    env_args = ["-c", "user.email=t@t", "-c", "user.name=t"]

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


def _branch(bare: Path, name: str) -> None:
    subprocess.run(
        ["git", "--git-dir", str(bare), "branch", name, "main"],
        check=True,
        capture_output=True,
    )


def test_resolves_worktree_holding_the_branch(repo: RepoFixture) -> None:
    tmp, bare, _seed, _origin = repo
    _branch(bare, "feature-x")
    wt = tmp / "wt-feature-x"
    _add_worktree(bare, wt, "feature-x")

    assert _resolve(bare, "feature-x") == str(wt)


def test_picks_the_right_worktree_among_several(repo: RepoFixture) -> None:
    """The footgun scenario: two live worktrees, each on its own branch.

    Resolving branch-b must return wt-b — NOT wt-a (the caller's cwd in the
    original bug) and NOT the first/main entry.
    """
    tmp, bare, _seed, _origin = repo
    _branch(bare, "branch-a")
    _branch(bare, "branch-b")
    wt_a = tmp / "wt-a"
    wt_b = tmp / "wt-b"
    _add_worktree(bare, wt_a, "branch-a")
    _add_worktree(bare, wt_b, "branch-b")

    assert _resolve(bare, "branch-b") == str(wt_b)
    assert _resolve(bare, "branch-a") == str(wt_a)


def test_empty_when_branch_not_checked_out(repo: RepoFixture) -> None:
    """Branch exists but is not checked out in any worktree → no target."""
    tmp, bare, _seed, _origin = repo
    _branch(bare, "lonely")  # created, never added as a worktree

    assert _resolve(bare, "lonely") == ""


def test_empty_for_unknown_branch(repo: RepoFixture) -> None:
    _tmp, bare, _seed, _origin = repo
    assert _resolve(bare, "does-not-exist") == ""


def test_detached_worktree_never_matched(repo: RepoFixture) -> None:
    """A detached worktree (the live runtime) carries no `branch` line."""
    tmp, bare, _seed, _origin = repo
    wt = tmp / "wt-runtime"
    _add_worktree(bare, wt, "main", detach=True)

    # Detached HEAD at main's commit, but resolving 'main' must not return it.
    assert _resolve(bare, "main") != str(wt)


def test_does_not_substring_match_branch_names(repo: RepoFixture) -> None:
    """'feat' must not match a worktree on 'feature-x' (exact match only)."""
    tmp, bare, _seed, _origin = repo
    _branch(bare, "feature-x")
    _add_worktree(bare, tmp / "wt-feature-x", "feature-x")

    assert _resolve(bare, "feat") == ""
    assert _resolve(bare, "feature") == ""
