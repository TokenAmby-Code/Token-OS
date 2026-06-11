"""Worktree create→delete lifecycle — one-shot, leak-free abstraction guards.

Regression tests for the 2026-06-10 cutover-validation leak sweep:

- worktree-delete silently aborted mid-teardown (set -euo pipefail + grep rc=1)
  for any worktree whose .worktree.env lacked SESSION_DOC_ID — i.e. every
  dispatch-created worktree. Port freed, dir/branch/alias leaked.
- worktree-setup only recognized the askCivic secrets layout (.env,
  widget/.env.*, deploy/*.json); Token-OS secrets (config.json, token-api/.env)
  were silently skipped.
- new branches were cut from bare HEAD without fetching origin (stale base).
- deleting an unmerged branch had no guard (-D, data loss in cleanup sweeps).
- interrupted setups left an unresumable half-state.
- the port registry's flock-only lock no-ops on macOS (duplicate assignment),
  and entries for out-of-band-deleted worktrees were never pruned.

The fixture builds a throwaway project (temp HOME, temp bare repo, temp conf)
so nothing touches the real NAS, ~/.config, or live worktrees.
"""

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKTREE_SETUP = ROOT / "cli-tools" / "bin" / "worktree-setup"
WORKTREE_DELETE = ROOT / "cli-tools" / "bin" / "worktree-delete"

RunFn = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class Project:
    home: Path
    src: Path
    bare: Path
    secrets: Path
    parent: Path
    env: dict[str, str]
    setup: RunFn
    delete: RunFn


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    res = subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    )
    return res.stdout.strip()


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
            # Force the portable lock path so macOS behavior is what CI proves.
            "WORKTREE_PORTS_NO_FLOCK": "1",
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
    parent = home / "worktrees" / "lcytest"
    (conf_dir / "lcytest.conf").write_text(
        f"BARE_REPO={bare}\nWORKTREE_PARENT={parent}\nSECRETS_DIR={secrets}\n",
        encoding="utf-8",
    )

    def _run(script: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), *args, "--project", "lcytest"],
            env=base_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def setup(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return _run(WORKTREE_SETUP, *args, "--no-transplant", "--skip-sync", timeout=timeout)

    def delete(*args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return _run(WORKTREE_DELETE, *args, timeout=timeout)

    return Project(
        home=home,
        src=src,
        bare=bare,
        secrets=secrets,
        parent=parent,
        env=base_env,
        setup=setup,
        delete=delete,
    )


def _registry(project: Project) -> dict[str, int]:
    reg = project.home / ".local" / "state" / "imperium" / "worktree-ports.json"
    if not reg.exists():
        return {}
    return json.loads(reg.read_text(encoding="utf-8"))


# ── delete: the P0 silent mid-teardown abort ─────────────────────────────────


def test_delete_completes_without_session_doc_id(project: Project) -> None:
    """A dispatch-shaped worktree (.worktree.env without SESSION_DOC_ID) tears
    down completely: dir, branch, alias, port registration all gone."""
    res = project.setup("alpha")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-alpha"
    env_file = wt / ".worktree.env"
    assert env_file.exists()
    assert "SESSION_DOC_ID" not in env_file.read_text(encoding="utf-8")

    res = project.delete("alpha", "-b")
    assert res.returncode == 0, res.stderr
    assert not wt.exists(), "worktree dir must be removed"
    assert (
        _git("--git-dir", str(project.bare), "branch", "--list", "alpha", env=project.env) == ""
    ), "local branch must be deleted"
    aliases = (
        (project.home / ".cd_quick_aliases").read_text(encoding="utf-8")
        if (project.home / ".cd_quick_aliases").exists()
        else ""
    )
    assert "alpha=" not in aliases, "cd alias must be removed"
    assert str(wt) not in _registry(project), "port registration must be freed"


# ── setup: secrets mirroring ─────────────────────────────────────────────────


def test_secrets_mirrored_at_relative_paths(project: Project) -> None:
    """Every regular file in SECRETS_DIR lands at its relative path — including
    nested layouts the old pattern-match skipped (config.json, token-api/.env)."""
    (project.secrets / ".env").write_text("ROOT=1\n", encoding="utf-8")
    (project.secrets / "config.json").write_text("{}\n", encoding="utf-8")
    (project.secrets / "token-api").mkdir()
    (project.secrets / "token-api" / ".env").write_text("API=1\n", encoding="utf-8")
    (project.secrets / "widget").mkdir()
    (project.secrets / "widget" / ".env.prod").write_text("W=1\n", encoding="utf-8")

    res = project.setup("secrets-branch")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-secrets-branch"
    assert (wt / ".env").exists()
    assert (wt / "config.json").exists()
    assert (wt / "token-api" / ".env").exists()
    assert (wt / "widget" / ".env.prod").exists()


# ── setup: fresh base from origin ────────────────────────────────────────────


def test_new_branch_based_on_fetched_origin_main(project: Project) -> None:
    """A new branch is cut from origin's current default branch even when the
    local bare main is stale (missed CD deploy)."""
    # Advance origin (src) past what the bare clone knows.
    (project.src / "newfile.txt").write_text("ahead\n", encoding="utf-8")
    _git("add", "-A", cwd=project.src, env=project.env)
    _git("commit", "-m", "ahead-of-bare", cwd=project.src, env=project.env)
    origin_head = _git("rev-parse", "main", cwd=project.src, env=project.env)
    bare_head = _git("--git-dir", str(project.bare), "rev-parse", "HEAD", env=project.env)
    assert origin_head != bare_head, "precondition: bare must be stale"

    res = project.setup("fresh-base")
    assert res.returncode == 0, res.stderr
    wt_head = _git("rev-parse", "HEAD", cwd=project.parent / "wt-fresh-base", env=project.env)
    assert wt_head == origin_head, "worktree must start at origin main, not stale bare HEAD"


# ── setup: explicit resume of an interrupted run ─────────────────────────────


def test_resume_finishes_interrupted_setup(project: Project) -> None:
    """--resume completes a half-built worktree; without it, the existing dir
    is still refused (dispatch's 1-branch-1-worktree contract)."""
    # Simulate the interruption: worktree registered + checked out, nothing else.
    wt = project.parent / "wt-halfway"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(
        "--git-dir",
        str(project.bare),
        "worktree",
        "add",
        "-b",
        "halfway",
        str(wt),
        env=project.env,
    )
    assert not (wt / ".worktree.env").exists()

    refused = project.setup("halfway")
    assert refused.returncode != 0
    assert "--resume" in refused.stderr

    resumed = project.setup("halfway", "--resume")
    assert resumed.returncode == 0, resumed.stderr
    assert (wt / ".worktree.env").exists(), "remaining steps must run on resume"

    wrong = project.setup("other-branch-name", "--resume")
    assert wrong.returncode == 0, "resume of a brand-new branch is a plain create"


def test_resume_refuses_branch_mismatch(project: Project) -> None:
    """--resume never adopts a directory holding a different branch."""
    res = project.setup("first")
    assert res.returncode == 0, res.stderr
    # Point a second branch name at the first branch's directory.
    wt = project.parent / "wt-first"
    assert wt.exists()
    mismatch = project.setup("first", "--resume")
    assert mismatch.returncode == 0, "same branch resume is fine"
    # A different branch whose wt- dir holds another checked-out branch must
    # refuse — a real worktree, not just an empty directory, so a regression
    # in branch-mismatch detection can't slip past on the dir-exists path.
    _git(
        "--git-dir",
        str(project.bare),
        "worktree",
        "add",
        "-b",
        "occupier",
        str(project.parent / "wt-second"),
        env=project.env,
    )
    refused = project.setup("second", "--resume")
    assert refused.returncode != 0
    assert "Cannot resume" in refused.stderr
    assert "occupier" in refused.stderr


# ── delete: merged-guard + remote deletion ───────────────────────────────────


def test_delete_branch_refuses_unmerged_without_force(project: Project) -> None:
    res = project.setup("risky")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-risky"
    (wt / "wip.txt").write_text("unmerged work\n", encoding="utf-8")
    _git("add", "-A", cwd=wt, env=project.env)
    _git("commit", "-m", "unmerged", cwd=wt, env=project.env)

    refused = project.delete("risky", "-b")
    assert refused.returncode == 65
    assert "not merged" in refused.stderr
    # Branch must survive the refusal.
    assert _git("--git-dir", str(project.bare), "branch", "--list", "risky", env=project.env) != ""

    forced = project.delete("risky", "-b", "--force")
    assert forced.returncode == 0, forced.stderr
    assert _git("--git-dir", str(project.bare), "branch", "--list", "risky", env=project.env) == ""


def test_delete_remote_branch(project: Project) -> None:
    res = project.setup("remote-gone")
    assert res.returncode == 0, res.stderr
    # Publish the branch to origin (the fixture src repo).
    _git("--git-dir", str(project.bare), "push", "origin", "remote-gone", env=project.env)
    assert _git("branch", "--list", "remote-gone", cwd=project.src, env=project.env) != ""

    done = project.delete("remote-gone", "-b", "--delete-remote")
    assert done.returncode == 0, done.stderr
    assert _git("branch", "--list", "remote-gone", cwd=project.src, env=project.env) == "", (
        "remote branch must be deleted on origin"
    )


# ── ports: macOS-path locking + stale-entry pruning ──────────────────────────


def test_ports_distinct_under_portable_lock(project: Project) -> None:
    """Sequential creates under the mkdir lock get distinct ports (the macOS
    flock no-op handed one port to two worktrees in production)."""
    assert project.setup("port-a").returncode == 0
    assert project.setup("port-b").returncode == 0
    reg = _registry(project)
    ports = list(reg.values())
    assert len(ports) == len(set(ports)), f"duplicate port assigned: {reg}"


def test_delete_prunes_stale_port_entries(project: Project) -> None:
    """Out-of-band-deleted worktrees lose their port registration on the next
    worktree-delete run (pool can't exhaust on ghosts)."""
    assert project.setup("stays").returncode == 0
    assert project.setup("ghosted").returncode == 0
    ghost_dir = project.parent / "wt-ghosted"
    # Out-of-band deletion (no worktree-delete): rm the tree directly.
    import shutil

    shutil.rmtree(ghost_dir)
    assert str(ghost_dir) in _registry(project)

    res = project.delete("stays", "-b")
    assert res.returncode == 0, res.stderr
    reg = _registry(project)
    assert str(ghost_dir) not in reg, "stale entry must be pruned"
    assert str(project.parent / "wt-stays") not in reg


# ── edge cases proven during the 2026-06-10 backlog sweep ────────────────────


def test_slash_branch_full_lifecycle(project: Project) -> None:
    """Branch names with `/` survive create→delete: the alias upsert/removal
    must not feed the name into a sed address (delimiter collision, proven
    live on fix/token-os-env-runtime-derivation)."""
    res = project.setup("feat/slashy")
    assert res.returncode == 0, res.stderr
    aliases_file = project.home / ".cd_quick_aliases"
    assert "feat/slashy=" in aliases_file.read_text(encoding="utf-8")

    done = project.delete("feat/slashy", "-b")
    assert done.returncode == 0, done.stderr
    assert "feat/slashy=" not in aliases_file.read_text(encoding="utf-8")
    assert "sed:" not in done.stderr, "no sed errors on slash branches"
    assert not (project.parent / "wt-feat" / "slashy").exists()


def test_delete_resolves_renamed_worktree_dir(project: Project) -> None:
    """When the dir isn't wt-<branch> (dispatch named it from --worktree),
    delete resolves the branch's registered worktree from git."""
    custom_dir = project.parent / "wt-custom-name"
    custom_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(
        "--git-dir",
        str(project.bare),
        "worktree",
        "add",
        "-b",
        "real-branch",
        str(custom_dir),
        env=project.env,
    )

    done = project.delete("real-branch", "-b")
    assert done.returncode == 0, done.stderr
    assert not custom_dir.exists(), "the registered (renamed) dir must be removed"
    assert (
        _git("--git-dir", str(project.bare), "branch", "--list", "real-branch", env=project.env)
        == ""
    )


def test_delete_handles_orphaned_dir(project: Project) -> None:
    """A dir whose git registration is already gone (external worktree
    remove/prune raced us — pr-merge does this) is still cleaned up."""
    res = project.setup("orphan")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-orphan"

    # Orphan it: stash the dir, prune the registration, restore the dir.
    import shutil

    stash = project.parent / "stash-orphan"
    shutil.move(str(wt), str(stash))
    _git("--git-dir", str(project.bare), "worktree", "prune", env=project.env)
    shutil.move(str(stash), str(wt))

    done = project.delete("orphan", "-b")
    assert done.returncode == 0, done.stderr
    assert not wt.exists(), "orphaned dir must be removed"
    assert _git("--git-dir", str(project.bare), "branch", "--list", "orphan", env=project.env) == ""
