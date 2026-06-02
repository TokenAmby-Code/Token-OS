"""Gap 1 (D3) — dispatch default-on worktree create+enter, opt-out via --no-worktree.

A code-touching dispatch (one whose working dir is a configured project's prod
checkout) should isolate itself into a fresh per-branch worktree instead of
running in the shared checkout. These assert the *decision* via --dry-run, so no
real worktree is created and no agent is launched.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "cli-tools" / "bin" / "dispatch"


@dataclass
class Env:
    home: Path
    prod: Path
    parent: Path
    base: dict[str, str]


@pytest.fixture
def env(tmp_path: Path) -> Env:
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
    return Env(home=home, prod=prod, parent=parent, base=base)


def _run(env: Env, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", *args],
        env=env.base,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _worktree_line(stdout: str) -> str:
    return next(ln for ln in stdout.splitlines() if "worktree:" in ln)


def test_no_worktree_opt_out(env: Env) -> None:
    res = _run(env, "--no-worktree", "--dir", str(env.prod), "do the thing")
    assert res.returncode == 0, res.stderr
    assert "worktree:" in res.stdout
    assert "opt-out" in _worktree_line(res.stdout).lower()


def test_default_on_creates_for_prod_checkout(env: Env) -> None:
    res = _run(env, "--dir", str(env.prod), "--title", "Some Task", "do it")
    assert res.returncode == 0, res.stderr
    line = _worktree_line(res.stdout)
    assert "wt-" in line and ("create" in line.lower() or "enter" in line.lower())


def test_explicit_worktree_branch(env: Env) -> None:
    res = _run(env, "--dir", str(env.prod), "--worktree", "my-branch", "do it")
    assert res.returncode == 0, res.stderr
    assert "wt-my-branch" in _worktree_line(res.stdout)


def test_worktree_requires_branch_value(env: Env) -> None:
    # --worktree must not swallow the following flag as its value.
    res = _run(env, "--dir", str(env.prod), "--worktree", "--no-gt", "do it")
    assert res.returncode != 0
    assert "requires a branch name" in res.stderr.lower()


def test_already_isolated_worktree_skips(env: Env) -> None:
    wt = env.parent / "wt-existing"
    wt.mkdir()
    res = _run(env, "--dir", str(wt), "do it")
    assert res.returncode == 0, res.stderr
    line = _worktree_line(res.stdout).lower()
    assert "isolated" in line or "n/a" in line


def _instance_type_line(stdout: str) -> str:
    return next(ln for ln in stdout.splitlines() if "instance_type:" in ln)


def test_worktree_defaults_to_golden_throne(env: Env) -> None:
    # Custodes directive: a --worktree worker keeps the Golden Throne lane by
    # default (alive until session-doc victory criteria are expended). one_off is
    # an explicit opt-in only — never the default.
    res = _run(env, "--dir", str(env.prod), "--worktree", "my-branch", "do it")
    assert res.returncode == 0, res.stderr
    assert "golden_throne" in _instance_type_line(res.stdout)


def test_worktree_one_off_is_explicit_opt_in(env: Env) -> None:
    res = _run(env, "--dir", str(env.prod), "--worktree", "my-branch", "--no-gt", "do it")
    assert res.returncode == 0, res.stderr
    assert "one_off" in _instance_type_line(res.stdout)


def test_repo_resolves_secrets_dir_case_insensitively(env: Env) -> None:
    # The conf in the fixture is clmtest.conf; --repo CLMTEST must still resolve it
    # (real confs are e.g. Token-OS.conf but `--repo token-os` is natural).
    res = _run(env, "--repo", "CLMTEST", "--worktree", "rb", "do it")
    assert res.returncode == 0, res.stderr
    assert f"dir:             {env.prod}" in res.stdout
    # And it branches a worktree from that repo's checkout.
    assert "wt-rb" in _worktree_line(res.stdout)


def test_repo_unknown_errors(env: Env) -> None:
    res = _run(env, "--repo", "nonesuch", "--worktree", "rb", "do it")
    assert res.returncode == 66
    assert "no config" in res.stderr.lower()


def test_explicit_dir_wins_over_repo(env: Env) -> None:
    other = env.prod.parent / "elsewhere"
    other.mkdir()
    res = _run(env, "--repo", "clmtest", "--dir", str(other), "do it")
    assert res.returncode == 0, res.stderr
    assert f"dir:             {other}" in res.stdout
