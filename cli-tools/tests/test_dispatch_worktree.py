"""Gap 1 (D3) — dispatch default-on worktree create+enter, opt-out via --no-worktree.

A code-touching dispatch (one whose working dir is a configured project's prod
checkout) should isolate itself into a fresh per-branch worktree instead of
running in the shared checkout. These assert the *decision* via --dry-run, so no
real worktree is created and no agent is launched.
"""

import os
import shlex
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
        f"BARE_REPO={tmp_path / 'proj.git'}\nWORKTREE_PARENT={parent}\nSECRETS_DIR={prod / 'secrets'}\nPROTECTED_ROOT={prod}\nRUNTIME_CHECKOUT={tmp_path / 'runtime'}\nLOCAL_BARE_MAIN_SYNC=true\n",
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
    other = env.prod.parent / "nonproject"
    other.mkdir()
    res = _run(env, "--no-worktree", "--dir", str(other), "do the thing")
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


def test_explicit_worktree_branch_survives_dispatch_reinvocation(env: Env) -> None:
    res = _run(env, "--dir", str(env.prod), "--worktree", "my-branch", "do it")
    assert res.returncode == 0, res.stderr
    assert "dispatch_command:" in res.stdout
    assert "--worktree my-branch" in res.stdout


def test_worktree_requires_branch_value(env: Env) -> None:
    # --worktree must not swallow the following flag as its value.
    res = _run(env, "--dir", str(env.prod), "--worktree", "--no-gt", "do it")
    assert res.returncode != 0
    assert "requires a branch name" in res.stderr.lower()


def test_already_isolated_worktree_skips(env: Env) -> None:
    wt = env.parent / "wt-existing"
    _git("init", "-b", "existing", str(wt), env=env.base)
    _git(
        "-C",
        str(wt),
        "remote",
        "add",
        "origin",
        "git@github.com:Someone/Other.git",
        env=env.base,
    )
    res = _run(env, "--dir", str(wt), "do it")
    assert res.returncode == 0, res.stderr
    line = _worktree_line(res.stdout).lower()
    assert "isolated" in line or "n/a" in line


def test_already_isolated_non_git_dir_is_refused(env: Env) -> None:
    wt = env.parent / "wt-not-git"
    wt.mkdir()
    res = _run(env, "--dir", str(wt), "do it")
    assert res.returncode == 64
    assert "not a git worktree" in res.stderr


def test_already_isolated_rejects_github_com_substring_origin(env: Env) -> None:
    wt = env.parent / "wt-evil-origin"
    _git("init", "-b", "evil-origin", str(wt), env=env.base)
    _git(
        "-C",
        str(wt),
        "remote",
        "add",
        "origin",
        "https://github.com.evil/Someone/Other.git",
        env=env.base,
    )
    res = _run(env, "--dir", str(wt), "do it")
    assert res.returncode == 64
    assert "does not map to GitHub" in res.stderr


def _instance_type_line(stdout: str) -> str:
    return next(ln for ln in stdout.splitlines() if "instance_type:" in ln)


def test_worktree_defaults_to_one_off(env: Env) -> None:
    # Temporary default while Golden Throne is not reliable enough for general
    # dispatch. Golden Throne remains available through explicit --gt.
    res = _run(env, "--dir", str(env.prod), "--worktree", "my-branch", "do it")
    assert res.returncode == 0, res.stderr
    assert "one_off" in _instance_type_line(res.stdout)


def test_worktree_golden_throne_is_explicit_opt_in(env: Env) -> None:
    res = _run(env, "--dir", str(env.prod), "--worktree", "my-branch", "--gt", "do it")
    assert res.returncode == 0, res.stderr
    assert "golden_throne" in _instance_type_line(res.stdout)


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


def test_no_worktree_refuses_protected_root(env: Env) -> None:
    res = _run(env, "--no-worktree", "--dir", str(env.prod), "do the thing")
    assert res.returncode == 64
    assert "refusing to dispatch into protected/runtime/secrets/bare root" in res.stderr


def test_runtime_root_also_forces_worktree(env: Env) -> None:
    runtime = env.prod.parent / "runtime"
    runtime.mkdir()
    res = _run(env, "--dir", str(runtime), "--worktree", "runtime-fix", "do it")
    assert res.returncode == 0, res.stderr
    assert "wt-runtime-fix" in _worktree_line(res.stdout)


# --- 2a / P3: Token-OS `--repo` must anchor on BARE_REPO, never the legacy ---------
# PROTECTED_ROOT. The Token-OS conf points PROTECTED_ROOT at the archived legacy NAS
# tree (Token-OS.legacy-20260610) which is a write-guard, not a base to branch from.
# When BARE_REPO is present on disk, `--repo` must resolve `dir:` to it and redirect
# into a fresh worktree (worktree-setup always cuts the worktree from BARE_REPO).


@dataclass
class BareEnv:
    home: Path
    bare: Path
    legacy: Path
    parent: Path
    base: dict[str, str]


@pytest.fixture
def bare_env(tmp_path: Path) -> BareEnv:
    """Token-OS-shaped conf: a real on-disk BARE_REPO plus a distinct legacy
    PROTECTED_ROOT (the archive guard) and a separate RUNTIME_CHECKOUT."""
    home = tmp_path / "home"
    (home / ".config" / "worktrees").mkdir(parents=True)
    bare = tmp_path / "token-os.git"
    bare.mkdir()  # present on disk → preferred anchor
    legacy = tmp_path / "Token-OS.legacy-20260610"
    legacy.mkdir()
    runtime = tmp_path / "live"
    runtime.mkdir()
    parent = home / "worktrees" / "Token-OS"
    parent.mkdir(parents=True)
    (home / ".config" / "worktrees" / "Token-OS.conf").write_text(
        f"BARE_REPO={bare}\nWORKTREE_PARENT={parent}\nSECRETS_DIR={tmp_path / 'config'}\n"
        f"PROTECTED_ROOT={legacy}\nRUNTIME_CHECKOUT={runtime}\nLOCAL_BARE_MAIN_SYNC=true\n",
        encoding="utf-8",
    )
    base = dict(os.environ)
    base["HOME"] = str(home)
    return BareEnv(home=home, bare=bare, legacy=legacy, parent=parent, base=base)


def _run_bare(env: BareEnv, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", *args],
        env=env.base,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_repo_anchors_on_bare_not_legacy_protected_root(bare_env: BareEnv) -> None:
    res = _run_bare(bare_env, "--repo", "Token-OS", "--worktree", "X", "do it")
    assert res.returncode == 0, res.stderr
    assert f"dir:             {bare_env.bare}" in res.stdout, res.stdout
    # And it must NOT anchor on the legacy archive.
    assert str(bare_env.legacy) not in res.stdout, res.stdout


def test_repo_bare_anchor_redirects_into_worktree(bare_env: BareEnv) -> None:
    res = _run_bare(bare_env, "--repo", "Token-OS", "--worktree", "X", "do it")
    assert res.returncode == 0, res.stderr
    assert "wt-X" in _worktree_line(res.stdout), res.stdout


def test_resume_with_worktree_enters_existing_branch_worktree(env: Env, tmp_path: Path) -> None:
    import sqlite3

    wt = env.parent / "wt-resume-branch"
    wt.mkdir()
    db = tmp_path / "agents.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            f"""
            CREATE TABLE session_documents (id INTEGER, file_path TEXT);
            CREATE TABLE personas (id TEXT PRIMARY KEY, slug TEXT);
            CREATE TABLE instances (
              id TEXT PRIMARY KEY, name TEXT, engine TEXT, launcher TEXT, target_working_dir TEXT,
              working_dir TEXT, dispatch_session_doc_path TEXT, session_doc_id INTEGER,
              golden_throne TEXT, zealotry TEXT, dispatch_target TEXT, dispatch_window TEXT,
              dispatch_mode TEXT, dispatch_slot TEXT, launch_mode TEXT, tmux_pane TEXT,
              persona_id TEXT, commander_type TEXT, commander_id TEXT, discord_hosted TEXT,
              discord_channel TEXT, discord_bot TEXT, pane_label TEXT,
              last_activity TEXT
            );
            INSERT INTO instances (id, name, engine, working_dir, golden_throne, zealotry, last_activity, commander_type)
            VALUES ('resume-id', 'Resume', 'claude', '{env.prod}', NULL, '3', '2026-06-18', 'emperor');
            """
        )
    env.base["TOKEN_API_DB"] = str(db)

    res = _run(env, "--id", "resume-id", "--worktree", "resume-branch", "continue")
    assert res.returncode == 0, res.stderr
    assert f"dir:             {wt}" in res.stdout
    assert "resume entered existing" in _worktree_line(res.stdout)


def test_stack_dispatch_accepts_noisy_tmuxctl_pane_output(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    rec = tmp_path / "tmux_calls.txt"
    fake_tmuxctl = fake_bin / "tmuxctl"
    fake_tmuxctl.write_text(
        "#!/usr/bin/env bash\n"
        # Noisy multi-line emit: `tail -n1` must pick the real canonical id off the
        # last line, then dispatch materializes physical at the raw-tmux send.
        'if [[ "$1" == "stack" && "$2" == "dispatch" ]]; then echo "note: created pane"; echo "mechanicus:5"; exit 0; fi\n'
        'if [[ "$1" == "resolve-pane" && "$2" == "--format" && "$3" == "physical" ]]; then echo "%88"; exit 0; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_tmuxctl.chmod(0o755)
    fake_tmux = fake_bin / "tmux"
    fake_tmux.write_text(
        f'#!/usr/bin/env bash\nfor a in "$@"; do printf "%s\\0" "$a" >> {shlex.quote(str(rec))}; done\nexit 0\n',
        encoding="utf-8",
    )
    fake_tmux.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"
    env["TOKEN_API_DB"] = str(tmp_path / "missing.db")

    res = subprocess.run(
        [
            str(DISPATCH),
            "--target",
            "mechanicus:new",
            "--dir",
            str(ROOT),
            "--no-worktree",
            "--no-gt",
            "noop",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )
    assert res.returncode == 0, res.stderr
    assert "non-canonical id" not in res.stderr
    assert "%88" in rec.read_bytes().decode("utf-8", "replace")


def _git(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass
class TokenOsWorktreeEnv:
    home: Path
    parent: Path
    worktree: Path
    base: dict[str, str]


@pytest.fixture
def token_os_worktree_env(tmp_path: Path) -> TokenOsWorktreeEnv:
    home = tmp_path / "home"
    parent = home / "worktrees" / "Token-OS"
    worktree = parent / "wt-remote-guard"
    conf_dir = home / ".config" / "worktrees"
    conf_dir.mkdir(parents=True)
    parent.mkdir(parents=True)
    worktree.mkdir()
    bare = tmp_path / "token-os.git"
    bare.mkdir()
    secrets = tmp_path / "config"
    secrets.mkdir()
    (conf_dir / "Token-OS.conf").write_text(
        f"BARE_REPO={bare}\nWORKTREE_PARENT={parent}\nSECRETS_DIR={secrets}\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["GIT_AUTHOR_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = "t@t"
    env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    env["IMPERIUM"] = str(tmp_path / "Imperium")
    _git("init", "-b", "remote-guard", str(worktree), env=env)
    return TokenOsWorktreeEnv(home=home, parent=parent, worktree=worktree, base=env)


def _run_token_os_worktree(env: TokenOsWorktreeEnv, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(DISPATCH), "--dry-run", "--direct", "--dir", str(env.worktree), *args],
        env=env.base,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_dispatch_worktree_refuses_dead_token_os_origin(
    token_os_worktree_env: TokenOsWorktreeEnv,
) -> None:
    _git(
        "-C",
        str(token_os_worktree_env.worktree),
        "remote",
        "add",
        "origin",
        f"{token_os_worktree_env.base['IMPERIUM']}/token-os.git",
        env=token_os_worktree_env.base,
    )

    res = _run_token_os_worktree(token_os_worktree_env, "--worktree", "remote-guard", "do it")
    assert res.returncode == 64
    assert "worktree is not PR-capable" in res.stderr
    assert "TokenAmby-Code/Token-OS" in res.stderr


def test_dispatch_worktree_accepts_token_os_github_origin(
    token_os_worktree_env: TokenOsWorktreeEnv,
) -> None:
    _git(
        "-C",
        str(token_os_worktree_env.worktree),
        "remote",
        "add",
        "origin",
        "git@github.com:TokenAmby-Code/Token-OS.git",
        env=token_os_worktree_env.base,
    )

    res = _run_token_os_worktree(token_os_worktree_env, "--worktree", "remote-guard", "do it")
    assert res.returncode == 0, res.stderr
    assert "already isolated worktree" in _worktree_line(res.stdout)


def test_dispatch_worktree_refuses_github_com_substring_origin(
    token_os_worktree_env: TokenOsWorktreeEnv,
) -> None:
    _git(
        "-C",
        str(token_os_worktree_env.worktree),
        "remote",
        "add",
        "origin",
        "https://github.com.evil/TokenAmby-Code/Token-OS.git",
        env=token_os_worktree_env.base,
    )

    res = _run_token_os_worktree(token_os_worktree_env, "--worktree", "remote-guard", "do it")
    assert res.returncode == 64
    assert "TokenAmby-Code/Token-OS" in res.stderr
