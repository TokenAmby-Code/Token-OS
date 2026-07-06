from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "token-satellite-refresh"


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    full_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    if env:
        full_env.update(env)
    return subprocess.check_output(cmd, cwd=cwd, env=full_env, text=True, stderr=subprocess.STDOUT)


def _chmod_tree(root: Path, file_mode: int, dir_mode: int) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        path.chmod(dir_mode if path.is_dir() else file_mode)
    root.chmod(dir_mode)


def test_satellite_refresh_unlocks_and_reprotects_readonly_runtime_checkout(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _run(["git", "init", "-b", "main"], cwd=upstream)
    (upstream / "docs").mkdir()
    (upstream / "docs" / "state.txt").write_text("old\n", encoding="utf-8")
    (upstream / "token-api" / "scripts").mkdir(parents=True)
    repo_helper = upstream / "token-api" / "scripts" / "token-satellite-refresh"
    repo_helper.write_text("#!/usr/bin/env bash\necho helper\n", encoding="utf-8")
    repo_helper.chmod(0o755)
    _run(["git", "add", "."], cwd=upstream)
    _run(["git", "commit", "-m", "initial"], cwd=upstream)

    bare = tmp_path / "token-os.git"
    _run(["git", "clone", "--bare", str(upstream), str(bare)])
    live = tmp_path / "live"
    _run(["git", "clone", str(bare), str(live)])
    _run(["git", "config", "core.filemode", "false"], cwd=live)
    _chmod_tree(live, 0o444, 0o555)

    _chmod_tree(upstream, 0o644, 0o755)
    (upstream / "docs" / "state.txt").write_text("new\n", encoding="utf-8")
    _run(["git", "add", "."], cwd=upstream)
    _run(["git", "commit", "-m", "update"], cwd=upstream)
    target_sha = _run(["git", "rev-parse", "HEAD"], cwd=upstream).strip()

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"changed_paths":["docs/state.txt"],"requested_at":"test"}\n', encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    systemctl.chmod(0o755)
    flock = bin_dir / "flock"
    flock.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    flock.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "TOKEN_OS_CD_BARE_REPO": str(bare),
        "TOKEN_OS_RUNTIME_CHECKOUT": str(live),
        "TOKEN_OS_REFRESH_WINDOWS_AHK": "never",
        "TOKEN_SATELLITE_REFRESH_HELPER_INSTALL": str(tmp_path / "installed-helper"),
    }
    try:
        proc = subprocess.run(
            ["bash", str(SCRIPT), target_sha, str(manifest)],
            text=True,
            capture_output=True,
            env={**os.environ, **env},
            check=False,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert _run(["git", "rev-parse", "HEAD"], cwd=live).strip() == target_sha
        assert not (live.stat().st_mode & stat.S_IWUSR)
    finally:
        for root in (live, upstream, bare):
            if root.exists():
                _chmod_tree(root, 0o644, 0o755)
