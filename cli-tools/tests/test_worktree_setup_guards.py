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
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKTREE_SETUP = ROOT / "cli-tools" / "bin" / "worktree-setup"
Project = dict[str, Any]


def _git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> Project:
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
            "IMPERIUM": str(tmp_path / "Imperium"),
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

    def write_named_conf(
        project_name: str, bare_path: Path, parent_path: Path | None = None
    ) -> None:
        (conf_dir / f"{project_name}.conf").write_text(
            f"BARE_REPO={bare_path}\n"
            f"WORKTREE_PARENT={parent_path or parent}\n"
            f"SECRETS_DIR={secrets}\n"
        )

    def setup(
        *args: str, project_name: str = "guardtest", timeout: int = 60
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                str(WORKTREE_SETUP),
                *args,
                "--project",
                project_name,
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
        "write_named_conf": write_named_conf,
        "tmp": tmp_path,
    }


# ── main/master creation guard ───────────────────────────────────────────────


def test_refuses_main_worktree(project: Project) -> None:
    res = project["setup"]("main", "--existing")
    assert res.returncode != 0, res.stdout
    assert "protected" in res.stderr.lower() or "main/master" in res.stderr.lower()
    assert not (project["parent"] / "wt-main").exists()


def test_refuses_master_worktree(project: Project) -> None:
    res = project["setup"]("master")
    assert res.returncode != 0
    assert not (project["parent"] / "wt-master").exists()


def test_allows_feature_branch(project: Project) -> None:
    res = project["setup"]("feature-x")
    assert res.returncode == 0, res.stderr
    wt = project["parent"] / "wt-feature-x"
    assert wt.exists()
    assert _git("-C", str(wt), "branch", "--show-current", env=project["env"]) == "feature-x"


def test_admin_escape_allows_main(project: Project) -> None:
    res = project["setup"]("main", "--existing", "--allow-protected-branch")
    assert res.returncode == 0, res.stderr
    assert (project["parent"] / "wt-main").exists()


# ── quarantine bare guard ────────────────────────────────────────────────────


def test_refuses_recycle_bin_bare(project: Project) -> None:
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


def test_refuses_dated_legacy_archive_bare(project: Project) -> None:
    legacy_bare = project["tmp"] / "Token-OS.legacy-20260610" / "proj.git"
    legacy_bare.parent.mkdir(parents=True)
    _git("clone", "--bare", str(project["bare"]), str(legacy_bare), env=project["env"])
    project["write_conf"](legacy_bare)

    res = project["setup"]("feature-z")
    assert res.returncode == 64
    assert not (project["parent"] / "wt-feature-z").exists()


# ── canonical remote provenance guard ───────────────────────────────────────


def test_token_os_github_origin_forces_worktree_origin_and_github(project: Project) -> None:
    parent = project["home"] / "worktrees" / "Token-OS"
    project["write_named_conf"]("Token-OS", project["bare"], parent)
    _git(
        "--git-dir",
        str(project["bare"]),
        "remote",
        "set-url",
        "origin",
        "git@github.com:TokenAmby-Code/Token-OS.git",
        env=project["env"],
    )
    _git(
        "--git-dir",
        str(project["bare"]),
        "branch",
        "feature-remote-guard",
        "main",
        env=project["env"],
    )

    res = project["setup"]("feature-remote-guard", "--existing", project_name="Token-OS")
    assert res.returncode == 0, res.stderr
    wt = parent / "wt-feature-remote-guard"
    assert (
        _git("-C", str(wt), "remote", "get-url", "origin", env=project["env"])
        == "git@github.com:TokenAmby-Code/Token-OS.git"
    )
    assert (
        _git("-C", str(wt), "remote", "get-url", "github", env=project["env"])
        == "git@github.com:TokenAmby-Code/Token-OS.git"
    )


def test_token_os_refuses_dead_nas_origin_before_worktree_create(project: Project) -> None:
    parent = project["home"] / "worktrees" / "Token-OS"
    project["write_named_conf"]("Token-OS", project["bare"], parent)
    _git(
        "--git-dir",
        str(project["bare"]),
        "remote",
        "set-url",
        "origin",
        f"{project['env']['IMPERIUM']}/token-os.git",
        env=project["env"],
    )

    res = project["setup"]("dead-remote", project_name="Token-OS")
    assert res.returncode == 64
    assert "dead" in res.stderr.lower() or "quarantined" in res.stderr.lower()
    assert not (parent / "wt-dead-remote").exists()


def test_token_os_refuses_recycle_origin_before_worktree_create(project: Project) -> None:
    parent = project["home"] / "worktrees" / "Token-OS"
    project["write_named_conf"]("Token-OS", project["bare"], parent)
    _git(
        "--git-dir",
        str(project["bare"]),
        "remote",
        "set-url",
        "origin",
        str(project["tmp"] / "#recycle" / "token-os.git"),
        env=project["env"],
    )

    res = project["setup"]("recycle-remote", project_name="Token-OS")
    assert res.returncode == 64
    assert "recycle" in res.stderr.lower() or "quarantined" in res.stderr.lower()
    assert not (parent / "wt-recycle-remote").exists()


def test_token_os_refuses_non_token_os_github_origin(project: Project) -> None:
    parent = project["home"] / "worktrees" / "Token-OS"
    project["write_named_conf"]("Token-OS", project["bare"], parent)
    _git(
        "--git-dir",
        str(project["bare"]),
        "remote",
        "set-url",
        "origin",
        "git@github.com:Someone/Other.git",
        env=project["env"],
    )

    res = project["setup"]("wrong-github", project_name="Token-OS")
    assert res.returncode == 64
    assert "Token-OS worktrees must use GitHub origin" in res.stderr
    assert not (parent / "wt-wrong-github").exists()


def test_token_os_refuses_plaintext_http_github_origin(project: Project) -> None:
    parent = project["home"] / "worktrees" / "Token-OS"
    project["write_named_conf"]("Token-OS", project["bare"], parent)
    _git(
        "--git-dir",
        str(project["bare"]),
        "remote",
        "set-url",
        "origin",
        "http://github.com/TokenAmby-Code/Token-OS.git",
        env=project["env"],
    )

    res = project["setup"]("http-github", project_name="Token-OS")
    assert res.returncode == 64
    assert "Token-OS worktrees must use GitHub origin" in res.stderr
    assert not (parent / "wt-http-github").exists()


def test_non_token_project_is_not_hardcoded_to_token_os_remote(project: Project) -> None:
    parent = project["home"] / "worktrees" / "OtherProject"
    project["write_named_conf"]("OtherProject", project["bare"], parent)
    _git(
        "--git-dir",
        str(project["bare"]),
        "remote",
        "set-url",
        "origin",
        "git@github.com:Someone/Other.git",
        env=project["env"],
    )
    _git("--git-dir", str(project["bare"]), "branch", "other-feature", "main", env=project["env"])

    res = project["setup"]("other-feature", "--existing", project_name="OtherProject")
    assert res.returncode == 0, res.stderr
    wt = parent / "wt-other-feature"
    assert (
        _git("-C", str(wt), "remote", "get-url", "origin", env=project["env"])
        == "git@github.com:Someone/Other.git"
    )


def test_refuses_missing_bare_origin(project: Project) -> None:
    _git("--git-dir", str(project["bare"]), "remote", "remove", "origin", env=project["env"])

    res = project["setup"]("missing-remote")
    assert res.returncode == 64
    assert "origin remote is empty" in res.stderr
