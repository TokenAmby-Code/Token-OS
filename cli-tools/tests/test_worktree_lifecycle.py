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
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
            "TOKEN_API_URL": "disabled",
            "TMUXCTLD_URL": "disabled",
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


def _lease_dirs(project: Project) -> set[str]:
    lease_dir = project.home / ".local" / "state" / "imperium" / "worktree-port-leases"
    dirs: set[str] = set()
    for lease in lease_dir.glob("*.lease"):
        values: dict[str, str] = {}
        for line in lease.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip("'\"")
        if values.get("dir"):
            dirs.add(values["dir"])
    return dirs


class _FakeTeardownDaemon:
    def __init__(
        self, *, instance_by_branch: dict[str, dict] | None = None, result: dict | None = None
    ):
        self.instance_by_branch = instance_by_branch or {}
        self.static_result = result
        self.requests: list[dict] = []
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                outer.requests.append(body)
                if self.path != "/worktree/teardown":
                    payload = {"ok": False, "error": {"message": "bad path"}}
                elif outer.static_result is not None:
                    payload = {"ok": True, "result": dict(outer.static_result)}
                else:
                    from tmuxctl.worktree_lifecycle import teardown_worktree

                    branch = body.get("branch") or Path(
                        str(body.get("worktree"))
                    ).name.removeprefix("wt-")
                    instance = outer.instance_by_branch.get(branch, {})
                    payload = {
                        "ok": True,
                        "result": teardown_worktree(
                            body.get("worktree"),
                            instance=instance,
                            delete_remote=bool(body.get("delete_remote", True)),
                        ),
                    }
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        assert self.server is not None
        self.server.shutdown()
        self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)


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

    res = project.delete("alpha", "-b", "--force")
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

    with _FakeTeardownDaemon(
        result={"status": "preserved", "reason": "branch_not_merged", "worktree": str(wt)}
    ) as daemon:
        project.env["TMUXCTLD_URL"] = daemon.url
        refused = project.delete("risky", "-b")

    assert refused.returncode == 0
    assert daemon.requests == [{"worktree": str(wt), "branch": "risky", "delete_remote": True}]
    assert "preserved worktree" in refused.stderr
    # Worktree/branch/peripherals must survive the daemon gate refusal.
    assert wt.exists()
    assert _git("--git-dir", str(project.bare), "branch", "--list", "risky", env=project.env) != ""
    aliases = (project.home / ".cd_quick_aliases").read_text(encoding="utf-8")
    assert "risky=" in aliases, "cd alias must be kept on preserve"

    forced = project.delete("risky", "-b", "--force")
    assert forced.returncode == 0, forced.stderr
    assert _git("--git-dir", str(project.bare), "branch", "--list", "risky", env=project.env) == ""


def test_delete_remote_branch(project: Project) -> None:
    res = project.setup("remote-gone")
    assert res.returncode == 0, res.stderr
    # Publish the branch to origin (the fixture src repo).
    _git("--git-dir", str(project.bare), "push", "origin", "remote-gone", env=project.env)
    assert _git("branch", "--list", "remote-gone", cwd=project.src, env=project.env) != ""

    done = project.delete("remote-gone", "-b", "--delete-remote", "--force")
    assert done.returncode == 0, done.stderr
    assert _git("branch", "--list", "remote-gone", cwd=project.src, env=project.env) == "", (
        "remote branch must be deleted on origin"
    )


def test_default_delete_routes_through_daemon_and_removes_merged(project: Project) -> None:
    res = project.setup("route-cli-merged")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-route-cli-merged"
    _git("--git-dir", str(project.bare), "push", "origin", "route-cli-merged", env=project.env)
    assert _git("branch", "--list", "route-cli-merged", cwd=project.src, env=project.env) != ""

    with _FakeTeardownDaemon(
        instance_by_branch={
            "route-cli-merged": {
                "id": "inst-cli-merged",
                "pr_state": "merged",
                "working_dir": str(wt),
            }
        }
    ) as daemon:
        project.env["TMUXCTLD_URL"] = daemon.url
        done = project.delete("route-cli-merged")

    assert done.returncode == 0, done.stderr
    assert daemon.requests == [
        {"worktree": str(wt), "branch": "route-cli-merged", "delete_remote": True}
    ]
    assert not wt.exists(), "daemon route removed the worktree"
    assert (
        _git(
            "--git-dir", str(project.bare), "branch", "--list", "route-cli-merged", env=project.env
        )
        == ""
    )
    assert _git("branch", "--list", "route-cli-merged", cwd=project.src, env=project.env) == ""
    assert str(wt) not in _registry(project), "post-remove peripheral freed the port"


def test_default_delete_daemon_down_fails_closed_no_local_fallback(project: Project) -> None:
    res = project.setup("route-down")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-route-down"
    project.env["TMUXCTLD_URL"] = "http://127.0.0.1:9"

    failed = project.delete("route-down")

    assert failed.returncode == 70
    assert "no local fallback" in failed.stderr
    assert wt.exists(), "daemon-down default path must preserve the worktree"
    assert _git("--git-dir", str(project.bare), "branch", "--list", "route-down", env=project.env)
    aliases = (project.home / ".cd_quick_aliases").read_text(encoding="utf-8")
    assert "route-down=" in aliases, "peripherals must not fire on route error"


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
    assert str(ghost_dir) in _lease_dirs(project)

    res = project.delete("stays", "-b", "--force")
    assert res.returncode == 0, res.stderr
    leases = _lease_dirs(project)
    assert str(ghost_dir) not in leases, "stale lease must be pruned"
    assert str(project.parent / "wt-stays") not in leases


# ── edge cases proven during the 2026-06-10 backlog sweep ────────────────────


def test_slash_branch_full_lifecycle(project: Project) -> None:
    """Branch names with `/` survive create→delete: the alias upsert/removal
    must not feed the name into a sed address (delimiter collision, proven
    live on fix/token-os-env-runtime-derivation)."""
    res = project.setup("feat/slashy")
    assert res.returncode == 0, res.stderr
    aliases_file = project.home / ".cd_quick_aliases"
    assert "feat/slashy=" in aliases_file.read_text(encoding="utf-8")

    done = project.delete("feat/slashy", "-b", "--force")
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

    done = project.delete("real-branch", "-b", "--force")
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

    done = project.delete("orphan", "-b", "--force")
    assert done.returncode == 0, done.stderr
    assert not wt.exists(), "orphaned dir must be removed"
    assert _git("--git-dir", str(project.bare), "branch", "--list", "orphan", env=project.env) == ""


# ── WrapperEnd deferred cleanup ───────────────────────────────────────────────


def test_wrapperend_deletes_merged_worktree(project: Project) -> None:
    """WrapperEnd owns deferred worktree teardown: a PR-marked-merged
    linked worktree is removed only after the agent wrapper has ended."""
    from tmuxctl.worktree_lifecycle import cleanup_worktree_on_wrapper_end

    res = project.setup("wrapper-merged")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-wrapper-merged"

    result = cleanup_worktree_on_wrapper_end(
        wt,
        instance={"id": "inst-merged", "pr_state": "merged", "working_dir": str(wt)},
    )

    assert result["status"] == "removed"
    assert not wt.exists(), "merged branch worktree must be removed at WrapperEnd"


def test_wrapperend_preserves_merged_but_dirty_worktree(project: Project) -> None:
    """Even a merged PR marker never overrides local uncommitted/untracked work."""
    from tmuxctl.worktree_lifecycle import cleanup_worktree_on_wrapper_end

    res = project.setup("wrapper-dirty")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-wrapper-dirty"
    (wt / "untracked-user-work.txt").write_text("do not delete\n", encoding="utf-8")

    result = cleanup_worktree_on_wrapper_end(
        wt,
        instance={"id": "inst-dirty", "pr_state": "merged", "working_dir": str(wt)},
    )

    assert result["status"] == "preserved"
    assert result["reason"] == "dirty_worktree"
    assert wt.exists()


def test_wrapperend_preserves_unmerged_worktree(project: Project) -> None:
    """WrapperEnd must preserve unmerged worktrees; shutdown is not data loss."""
    from tmuxctl.worktree_lifecycle import cleanup_worktree_on_wrapper_end

    res = project.setup("wrapper-open")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-wrapper-open"
    (wt / "wip.txt").write_text("still open\n", encoding="utf-8")
    _git("add", "-A", cwd=wt, env=project.env)
    _git("commit", "-m", "still-open", cwd=wt, env=project.env)

    result = cleanup_worktree_on_wrapper_end(
        wt,
        instance={"id": "inst-open", "pr_state": "open", "working_dir": str(wt)},
    )

    assert result["status"] == "preserved"
    assert result["reason"] == "branch_not_merged"
    assert wt.exists(), "unmerged worktree must survive WrapperEnd"


def test_wrapperend_handler_removes_worktree_without_pane_kill(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tmuxctld WrapperEnd hook wires deferred worktree cleanup without adding
    lifecycle pane-kill side effects."""
    from tmuxctl import daemon

    res = project.setup("wrapper-hook-merged")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-wrapper-hook-merged"

    class FakeAdapter:
        def __init__(self) -> None:
            self.commands: list[tuple[str, ...]] = []

        def run(self, *args: str, allow_failure: bool = False) -> str:
            del allow_failure
            self.commands.append(tuple(args))
            if args[:1] == ("display-message",) and "#{pane_id}" in args:
                return "%99\n"
            if args[:1] == ("display-message",) and "#{window_name}" in args:
                return "palace\n"
            return ""

        def show_pane_option(self, pane: str, option: str) -> str:
            assert pane == "%99"
            return {
                "@TOKEN_API_WRAPPER_ID": "wrap-99",
                "@TOKEN_API_WRAPPER_LAUNCH_ID": "",
                "@PANE_ID": "palace:N",
                "@INSTANCE_ID": "inst-99",
                "@TOKEN_API_CWD": str(wt),
            }.get(option, "")

    class FakeControl:
        def __init__(self) -> None:
            self.adapter = FakeAdapter()

        def ledger_close(self, wrapper_id: str) -> dict:
            assert wrapper_id == "wrap-99"
            return {"closed": True, "row": {"instance_id": "inst-99", "working_dir": str(wt)}}

        def teardown_pane(
            self,
            pane: str,
            *,
            pane_label: str = "",
            window_name: str | None = None,
            source: str = "",
        ) -> dict:
            assert pane == "%99"
            assert pane_label == "palace:N"
            assert window_name == "palace"
            assert source == "wrapperend"
            return {"ok": True, "result": {"status": "cleared_in_place", "pane": pane}}

    monkeypatch.setattr(
        daemon,
        "_fetch_instance_for_wrapperend",
        lambda instance_id: {"id": instance_id, "pr_state": "merged", "working_dir": str(wt)},
    )
    control = FakeControl()

    result = daemon._h_hook_wrapperend(
        control,
        {"wrapper_id": "wrap-99", "tmux_pane": "%99", "cwd": str(wt), "env": {}},
    )

    assert result["worktree_cleanup"]["status"] == "removed"
    assert not wt.exists()
    assert all("kill-pane" not in command for command in control.adapter.commands)


def _bare_branch_exists(project: Project, branch: str) -> bool:
    return _git("--git-dir", str(project.bare), "branch", "--list", branch, env=project.env) != ""


def _commit_divergent(project: Project, wt: Path, text: str) -> None:
    """Commit on the worktree branch so its tip is NOT an ancestor of main.

    This is the squash-merge shape: the branch carries commits that a squash
    rewrote into a new SHA on main, so ``git merge-base --is-ancestor`` can
    never prove the merge — only a durable ``pr_state=merged`` marker can.
    """
    (wt / "squashed-work.txt").write_text(text, encoding="utf-8")
    _git("add", "-A", cwd=wt, env=project.env)
    _git("commit", "-m", text, cwd=wt, env=project.env)


def test_wrapperend_prunes_branch_ref_on_merged_removal(project: Project) -> None:
    """Removing a provably-merged worktree also prunes its (now-merged) branch
    ref from the bare — refs must not accumulate after teardown."""
    from tmuxctl.worktree_lifecycle import cleanup_worktree_on_wrapper_end

    res = project.setup("ref-merged")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-ref-merged"
    assert _bare_branch_exists(project, "ref-merged")

    result = cleanup_worktree_on_wrapper_end(
        wt,
        instance={"id": "inst-refmerged", "pr_state": "merged", "working_dir": str(wt)},
    )

    assert result["status"] == "removed"
    assert not wt.exists()
    assert not _bare_branch_exists(project, "ref-merged"), (
        "merged branch ref must be pruned alongside the worktree"
    )


def test_wrapperend_deletes_squash_merged_via_pr_state(project: Project) -> None:
    """The #543 cleanup fires for a squash-merged branch: git can't prove the
    merge (tip is not an ancestor of main), but pr_state=merged is ground truth
    → worktree removed and branch ref pruned."""
    from tmuxctl.worktree_lifecycle import cleanup_worktree_on_wrapper_end

    res = project.setup("squash-yes")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-squash-yes"
    _commit_divergent(project, wt, "squash-yes-body")

    result = cleanup_worktree_on_wrapper_end(
        wt,
        instance={"id": "inst-sqyes", "pr_state": "merged", "working_dir": str(wt)},
    )

    assert result["status"] == "removed"
    assert result["merge_reason"] == "instance_pr_state_merged"
    assert not wt.exists()
    assert not _bare_branch_exists(project, "squash-yes")


def test_wrapperend_preserves_squash_merged_without_pr_state(project: Project) -> None:
    """The exact #538 fail-safe: a squash-merged branch whose instance row has
    NO pr_state can't be proven merged → preserved, branch ref survives."""
    from tmuxctl.worktree_lifecycle import cleanup_worktree_on_wrapper_end

    res = project.setup("squash-no")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-squash-no"
    _commit_divergent(project, wt, "squash-no-body")

    result = cleanup_worktree_on_wrapper_end(
        wt,
        instance={"id": "inst-sqno", "working_dir": str(wt)},
    )

    assert result["status"] == "preserved"
    assert result["reason"] == "branch_not_merged"
    assert result["merge_reason"] == "unproven"
    assert wt.exists(), "unprovable squash-merge must be preserved (fail-safe)"
    assert _bare_branch_exists(project, "squash-no"), "preserved branch ref must survive"


def test_wrapperend_preserves_closed_pr_state(project: Project) -> None:
    """pr_state=closed never authorizes removal, even on a clean worktree."""
    from tmuxctl.worktree_lifecycle import cleanup_worktree_on_wrapper_end

    res = project.setup("pr-closed")
    assert res.returncode == 0, res.stderr
    wt = project.parent / "wt-pr-closed"
    _commit_divergent(project, wt, "closed-body")

    result = cleanup_worktree_on_wrapper_end(
        wt,
        instance={"id": "inst-closed", "pr_state": "closed", "working_dir": str(wt)},
    )

    assert result["status"] == "preserved"
    assert result["reason"] == "branch_not_merged"
    assert result["merge_reason"] == "instance_pr_state_closed"
    assert wt.exists()
    assert _bare_branch_exists(project, "pr-closed")


def test_pr_step_and_pr_merge_do_not_remove_worktrees() -> None:
    """Regression: pr-step/pr-merge may merge, but worktree removal is not in
    their execution path; WrapperEnd is the only owner of teardown."""
    for script in (ROOT / "cli-tools" / "bin" / "pr-step", ROOT / "cli-tools" / "bin" / "pr-merge"):
        text = script.read_text(encoding="utf-8")
        assert "git worktree remove" not in text
        assert "worktree remove" not in text


# ── universal gate: gated remote-ref deletion ─────────────────────────────────


def _src_branch_exists(project: Project, branch: str) -> bool:
    """The origin (fixture ``src`` repo) still carries ``branch``."""
    return _git("branch", "--list", branch, cwd=project.src, env=project.env) != ""


def _publish_to_origin(project: Project, branch: str) -> None:
    """Push a worktree branch up to origin so remote-deletion has a target."""
    _git("--git-dir", str(project.bare), "push", "origin", branch, env=project.env)
    assert _src_branch_exists(project, branch), "precondition: branch published to origin"


def test_teardown_deletes_local_and_remote_ref_when_merged(project: Project) -> None:
    """The universal gate's complete teardown: a merged worktree loses its local
    worktree, its local branch ref, AND its remote branch ref (drains the pileup
    the audit flagged — WrapperEnd previously pruned only the local ref)."""
    from tmuxctl.worktree_lifecycle import teardown_worktree

    assert project.setup("merged-full").returncode == 0
    wt = project.parent / "wt-merged-full"
    _publish_to_origin(project, "merged-full")

    result = teardown_worktree(
        wt, instance={"id": "inst-mf", "pr_state": "merged", "working_dir": str(wt)}
    )

    assert result["status"] == "removed"
    assert result["remote_prune"] == "deleted"
    assert not wt.exists(), "local worktree removed"
    assert not _bare_branch_exists(project, "merged-full"), "local branch ref pruned"
    assert not _src_branch_exists(project, "merged-full"), "remote branch ref deleted"


def test_teardown_preserves_local_and_remote_when_unmerged(project: Project) -> None:
    """Unmerged/dirty → EVERYTHING preserved (local worktree, local ref, remote
    ref) on the shared entrypoint. Shutdown is never data loss."""
    from tmuxctl.worktree_lifecycle import teardown_worktree

    assert project.setup("unmerged-full").returncode == 0
    wt = project.parent / "wt-unmerged-full"
    _publish_to_origin(project, "unmerged-full")
    _commit_divergent(project, wt, "unmerged-body")

    result = teardown_worktree(
        wt, instance={"id": "inst-uf", "pr_state": "open", "working_dir": str(wt)}
    )

    assert result["status"] == "preserved"
    assert result["reason"] == "branch_not_merged"
    assert "remote_prune" not in result, "preserve path never reaches remote deletion"
    assert wt.exists(), "unmerged worktree preserved"
    assert _bare_branch_exists(project, "unmerged-full"), "local ref preserved"
    assert _src_branch_exists(project, "unmerged-full"), "remote ref preserved"


def test_teardown_never_remote_deletes_open_pr_head(project: Project) -> None:
    """PR-orphan guard: a branch whose PR is open (pr_state=open) is never
    remote-deleted — deleting an open PR's head branch orphans/closes it. The
    merge gate blocks first, so the remote ref is untouched even on a clean tree."""
    from tmuxctl.worktree_lifecycle import teardown_worktree

    assert project.setup("open-pr-head").returncode == 0
    wt = project.parent / "wt-open-pr-head"
    _publish_to_origin(project, "open-pr-head")  # clean worktree, but PR is open

    result = teardown_worktree(
        wt, instance={"id": "inst-op", "pr_state": "open", "working_dir": str(wt)}
    )

    assert result["status"] == "preserved"
    assert result["merge_reason"] == "instance_pr_state_open"
    assert _src_branch_exists(project, "open-pr-head"), "open-PR head ref must survive"


def test_teardown_preserves_remote_on_ancestor_only_merge(project: Project) -> None:
    """Merge proven only by local ancestry (no authoritative pr_state) removes the
    local worktree+ref but PRESERVES the remote ref: without pr_state we cannot
    rule out an open PR, so the remote is the conservative side of the gate."""
    from tmuxctl.worktree_lifecycle import teardown_worktree

    # A branch whose tip IS an ancestor of main (fast-forward-shaped): setup cuts
    # it from origin/main and we add no commits, so is-ancestor proves the merge.
    assert project.setup("ff-merged").returncode == 0
    wt = project.parent / "wt-ff-merged"
    _publish_to_origin(project, "ff-merged")

    result = teardown_worktree(wt, instance={"id": "inst-ff", "working_dir": str(wt)})

    assert result["status"] == "removed"
    assert result["merge_reason"].startswith("head_ancestor_of_")
    assert result["remote_prune"] == "preserved"
    assert result["remote_prune_reason"] == "no_open_pr_unconfirmed"
    assert not wt.exists(), "locally-proven-merged worktree removed"
    assert not _bare_branch_exists(project, "ff-merged"), "local ref pruned"
    assert _src_branch_exists(project, "ff-merged"), "remote ref preserved (open PR unconfirmed)"


def test_teardown_refuses_to_purge_cwd(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    """getcwd immunity on every entrypoint: teardown never removes the directory
    the calling process still stands in, even when merge+cleanliness would allow."""
    from tmuxctl.worktree_lifecycle import teardown_worktree

    assert project.setup("cwd-self").returncode == 0
    wt = project.parent / "wt-cwd-self"
    monkeypatch.chdir(wt)  # pytest restores cwd on teardown

    result = teardown_worktree(
        wt, instance={"id": "inst-cwd", "pr_state": "merged", "working_dir": str(wt)}
    )

    assert result["status"] == "preserved"
    assert result["reason"] == "would_remove_cwd"
    assert wt.exists(), "must not self-purge the caller's cwd"


# ── on-demand daemon route: same executor, same gate ─────────────────────────


def test_worktree_teardown_route_removes_merged(project: Project) -> None:
    """The tmuxctld /worktree/teardown route runs the SAME gate + remote deletion
    as WrapperEnd, driven by a passed pr_state (no token-api fetch needed)."""
    from tmuxctl import daemon

    assert project.setup("route-merged").returncode == 0
    wt = project.parent / "wt-route-merged"
    _publish_to_origin(project, "route-merged")

    # control is unused by the route (teardown is worktree-only, no tmux).
    result = daemon._h_worktree_teardown(
        None, {"worktree": str(wt), "branch": "route-merged", "pr_state": "merged"}
    )

    assert result["status"] == "removed"
    assert result["remote_prune"] == "deleted"
    assert not wt.exists()
    assert not _src_branch_exists(project, "route-merged")


def test_worktree_teardown_route_requires_worktree() -> None:
    """The route rejects a missing worktree path loudly rather than guessing."""
    from tmuxctl import daemon

    with pytest.raises(ValueError):
        daemon._h_worktree_teardown(None, {"branch": "whatever"})
