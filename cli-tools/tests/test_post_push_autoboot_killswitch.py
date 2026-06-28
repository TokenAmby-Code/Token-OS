"""Regression guard: the per-worktree post-push hook must NOT auto-boot a dev
token-api unless WORKTREE_DEV_AUTOBOOT=1.

An armed hook boots the full app — including the enforce loop, whose phone/Pavlok
client always hits the real devices regardless of which DB it reads. Against a
stale .dev-agents.db it manufactures phantom "break debt" and fires real
notifications + zaps (split-brain). These tests pin the kill-switch closed by
default and verify it opens only on explicit opt-in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POST_PUSH = ROOT / "cli-tools" / "git-hooks" / "post-push"


def _run(cwd: Path, env_extra: dict[str, str], home: Path) -> subprocess.CompletedProcess[str]:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(POST_PUSH)],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )


def test_post_push_skips_when_autoboot_unset(tmp_path: Path) -> None:
    # Flag unset → the gate fires before any work; no dev server, no pid file.
    result = _run(tmp_path, env_extra={}, home=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "auto-boot disabled" in result.stdout
    assert not (tmp_path / ".worktree-dev.pid").exists()
    assert not (tmp_path / ".worktree-dev.log").exists()


def test_post_push_skips_when_autoboot_not_one(tmp_path: Path) -> None:
    # Any value other than exactly "1" stays disabled (fail-closed).
    for val in ("0", "true", "yes", ""):
        result = _run(tmp_path, env_extra={"WORKTREE_DEV_AUTOBOOT": val}, home=tmp_path)
        assert result.returncode == 0, result.stderr
        assert "auto-boot disabled" in result.stdout, f"val={val!r} should stay disabled"
        assert not (tmp_path / ".worktree-dev.pid").exists()


def test_post_push_opens_gate_when_armed_then_refuses_live_db(tmp_path: Path) -> None:
    # Armed (=1) → passes the kill-switch and reaches the existing DB guard, which
    # refuses to boot a token-api with TOKEN_API_DB unset. This proves the gate
    # opens WITHOUT actually launching a server.
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    (tmp_path / "token-api").mkdir()
    (tmp_path / "token-api" / "main.py").write_text("# fixture\n", encoding="utf-8")
    (tmp_path / ".worktree.env").write_text("PORT=7123\n", encoding="utf-8")

    result = _run(tmp_path, env_extra={"WORKTREE_DEV_AUTOBOOT": "1"}, home=tmp_path)

    assert result.returncode == 0, result.stderr
    assert "auto-boot disabled" not in result.stdout
    # Gate opened; existing fail-closed DB guard catches it (no server booted).
    assert "TOKEN_API_DB not set" in result.stdout
    assert not (tmp_path / ".worktree-dev.pid").exists()
