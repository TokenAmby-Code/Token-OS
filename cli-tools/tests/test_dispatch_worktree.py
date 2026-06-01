"""Gap 1 (D3) — dispatch default-on worktree create+enter, opt-out via --no-worktree.

A code-touching dispatch (one whose working dir is a configured project's prod
checkout) should isolate itself into a fresh per-branch worktree instead of
running in the shared checkout. These assert the *decision* via --dry-run, so no
real worktree is created and no agent is launched.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "cli-tools" / "bin" / "dispatch"


@pytest.fixture
def env(tmp_path):
    """Temp HOME with a worktree conf whose prod checkout is `prod`."""
    home = tmp_path / "home"
    (home / ".config" / "worktrees").mkdir(parents=True)
    prod = tmp_path / "prod"
    prod.mkdir()
    parent = home / "worktrees" / "clmtest"
    parent.mkdir(parents=True)
    (home / ".config" / "worktrees" / "clmtest.conf").write_text(
        f"BARE_REPO={tmp_path / 'proj.git'}\nWORKTREE_PARENT={parent}\nSECRETS_DIR={prod}\n",
        encoding="utf-8",
    )
    base = dict(os.environ)
    base["HOME"] = str(home)
    return type("Env", (), {"home": home, "prod": prod, "parent": parent, "base": base})


def _run(env, *args):
    return subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", *args],
        env=env.base,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_no_worktree_opt_out(env):
    res = _run(env, "--no-worktree", "--dir", str(env.prod), "do the thing")
    assert res.returncode == 0, res.stderr
    assert "worktree:" in res.stdout
    line = next(ln for ln in res.stdout.splitlines() if "worktree:" in ln)
    assert "opt-out" in line.lower()


def test_default_on_creates_for_prod_checkout(env):
    res = _run(env, "--dir", str(env.prod), "--title", "Some Task", "do it")
    assert res.returncode == 0, res.stderr
    line = next(ln for ln in res.stdout.splitlines() if "worktree:" in ln)
    assert "wt-" in line and ("create" in line.lower() or "enter" in line.lower())


def test_explicit_worktree_branch(env):
    res = _run(env, "--dir", str(env.prod), "--worktree", "my-branch", "do it")
    assert res.returncode == 0, res.stderr
    line = next(ln for ln in res.stdout.splitlines() if "worktree:" in ln)
    assert "wt-my-branch" in line


def test_already_isolated_worktree_skips(env):
    wt = env.parent / "wt-existing"
    wt.mkdir()
    res = _run(env, "--dir", str(wt), "do it")
    assert res.returncode == 0, res.stderr
    line = next(ln for ln in res.stdout.splitlines() if "worktree:" in ln)
    assert "isolated" in line.lower() or "n/a" in line.lower()
