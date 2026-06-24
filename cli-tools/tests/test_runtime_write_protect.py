import os
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "cli-tools" / "scripts" / "runtime-write-protect.sh"

HAVE_CHFLAGS = shutil.which("chflags") is not None
requires_chflags = pytest.mark.skipif(
    not HAVE_CHFLAGS, reason="user-immutable layer is BSD/macOS only (chflags)"
)


@pytest.fixture(autouse=True)
def _clear_immutable_after(tmp_path):
    # lock sets the BSD user-immutable flag (uchg); pytest's tmp_path teardown
    # cannot rmtree immutable files. Clear flags after every test so cleanup
    # succeeds. No-op where chflags is absent (Linux).
    yield
    if HAVE_CHFLAGS:
        subprocess.run(
            ["chflags", "-R", "nouchg", str(tmp_path)],
            check=False,
            capture_output=True,
        )


def run(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
        **kwargs,
    )


def has_any_write(path: Path) -> bool:
    return bool(path.lstat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def has_owner_write(path: Path) -> bool:
    return bool(path.lstat().st_mode & stat.S_IWUSR)


def is_immutable(path: Path) -> bool:
    # st_flags carries BSD file flags on macOS; UF_IMMUTABLE == 0x2 (uchg).
    return bool(getattr(path.lstat(), "st_flags", 0) & stat.UF_IMMUTABLE)


def test_lock_removes_write_bits_without_following_symlinks(tmp_path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    child = root / "token-api"
    child.mkdir()
    file = child / "app.py"
    file.write_text("print('x')\n")
    os.chmod(root, 0o777)
    os.chmod(child, 0o777)
    os.chmod(file, 0o666)
    secret = tmp_path / "secret.txt"
    secret.write_text("keep writable\n")
    (root / "config.json").symlink_to(secret)

    proc = run("lock", str(root))

    assert proc.returncode == 0, proc.stderr
    assert "locked" in proc.stdout
    assert not has_any_write(root)
    assert not has_any_write(child)
    assert not has_any_write(file)
    assert has_any_write(secret), "must not chmod symlink targets outside runtime"


def test_symlink_root_is_rejected(tmp_path) -> None:
    real_root = tmp_path / "real-runtime"
    real_root.mkdir()
    symlink_root = tmp_path / "runtime-link"
    symlink_root.symlink_to(real_root, target_is_directory=True)

    proc = run("lock", str(symlink_root))

    assert proc.returncode == 1
    assert "must not be a symlink" in proc.stderr


def test_locked_runtime_rejects_plain_shell_write(tmp_path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    file = root / "x.txt"
    file.write_text("old\n")
    assert run("lock", str(root)).returncode == 0

    proc = subprocess.run(
        ["bash", "-lc", f"printf new > {shlex.quote(str(file))}"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert proc.returncode != 0
    assert file.read_text() == "old\n"


def test_unlock_restores_owner_write(tmp_path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    file = root / "x.py"
    file.write_text("x\n")
    assert run("lock", str(root)).returncode == 0

    proc = run("unlock", str(root))

    assert proc.returncode == 0, proc.stderr
    assert has_owner_write(root)
    assert has_owner_write(file)


def test_assert_locked_fails_when_any_write_bit_remains(tmp_path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "x").write_text("x")

    assert run("assert-locked", str(root)).returncode == 1
    assert run("lock", str(root)).returncode == 0
    assert run("assert-locked", str(root)).returncode == 0


def _is_0755_dir(path: Path) -> bool:
    return path.is_dir() and (path.lstat().st_mode & 0o777) == 0o755


def test_lock_creates_missing_writable_queue_on_fresh_tree(tmp_path) -> None:
    # Fresh deploy: pending/ doesn't exist yet. Lock must CREATE it 0755 inside
    # the otherwise-frozen tree — the app can't mkdir it under a read-only root.
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "app.py").write_text("print('x')\n")

    proc = run("lock", str(root))

    assert proc.returncode == 0, proc.stderr
    pending = root / "pending"
    assert _is_0755_dir(pending), f"pending must be 0755, got {oct(pending.lstat().st_mode)}"
    # The frozen source stays read-only...
    assert not has_any_write(root / "app.py")
    assert not has_any_write(root)
    # ...but a runtime write into the queue succeeds.
    job = pending / "job.json"
    job.write_text("{}")
    assert job.read_text() == "{}"
    job.unlink()  # pragma-once delete needs a writable parent dir


def test_lock_regrants_write_to_existing_queue_and_contents(tmp_path) -> None:
    # Re-deploy: pending/ exists with a queued job, frozen read-only by the
    # tree-wide freeze. Lock must re-grant owner write to the dir AND its
    # contents so the daemon can drain/remove queued files.
    root = tmp_path / "runtime"
    root.mkdir()
    pending = root / "pending"
    pending.mkdir()
    job = pending / "queued.json"
    job.write_text('{"id": 1}')
    os.chmod(job, 0o444)
    os.chmod(pending, 0o555)

    proc = run("lock", str(root))

    assert proc.returncode == 0, proc.stderr
    assert has_owner_write(pending)
    assert has_owner_write(job), "queued files must stay removable after lock"
    job.unlink()


def test_status_and_assert_locked_ignore_writable_queue(tmp_path) -> None:
    # A correctly-locked tree has a writable pending/ by design; status and
    # assert-locked must report it as locked, not falsely "unlocked".
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "app.py").write_text("x\n")
    assert run("lock", str(root)).returncode == 0
    assert has_owner_write(root / "pending")  # exemption is writable

    assert "locked" in run("status", str(root)).stdout
    assert run("assert-locked", str(root)).returncode == 0


def test_assert_locked_still_fails_on_write_bit_outside_queue(tmp_path) -> None:
    # The exemption must not blind the verifier to a stray writable source file.
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "pending").mkdir()
    leak = root / "app.py"
    leak.write_text("x\n")
    os.chmod(leak, 0o644)

    assert run("assert-locked", str(root)).returncode == 1


def test_writable_dirs_override_via_env(tmp_path) -> None:
    # The exemption list is overridable for additional runtime-writable dirs.
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "app.py").write_text("x\n")
    env = {**os.environ, "TOKEN_OS_RUNTIME_WRITABLE_DIRS": "spool:var/queue"}

    proc = run("lock", str(root), env=env)

    assert proc.returncode == 0, proc.stderr
    assert _is_0755_dir(root / "spool")
    assert _is_0755_dir(root / "var" / "queue")
    # default 'pending' is NOT created when the override replaces the list
    assert not (root / "pending").exists()
    assert run("assert-locked", str(root), env=env).returncode == 0


def test_lock_refuses_symlinked_queue(tmp_path) -> None:
    # A symlink where the writable dir is expected must be refused, never
    # chmod'd through to its target.
    root = tmp_path / "runtime"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o555)  # read-only target; lock must not widen it
    (root / "pending").symlink_to(outside, target_is_directory=True)

    proc = run("lock", str(root))

    assert proc.returncode == 1
    assert "symlink" in proc.stderr
    assert not has_any_write(outside), "must not widen perms through the symlink"


def test_lock_warns_and_fails_when_chmod_is_a_noop(tmp_path) -> None:
    # Network mounts (SMB/CIFS) silently ignore chmod, so the tree stays
    # writable while chmod "succeeds". Simulate that with a no-op chmod shim on
    # PATH: lock must detect the residual write bits, warn, and exit nonzero
    # rather than falsely report "locked".
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "app.py").write_text("print('x')\n")
    os.chmod(root, 0o777)

    binshim = tmp_path / "bin"
    binshim.mkdir()
    shim = binshim / "chmod"
    shim.write_text("#!/usr/bin/env bash\nexit 0\n")
    os.chmod(shim, 0o755)

    env = {**os.environ, "PATH": f"{binshim}{os.pathsep}{os.environ['PATH']}"}
    proc = run("lock", str(root), env=env)

    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "did NOT take" in proc.stderr
    assert "locked" not in proc.stdout
    assert has_any_write(root), "no-op chmod must leave write bits"


def test_default_roots_exclude_unprotectable_network_mounts(tmp_path) -> None:
    # default_roots() must list only local-filesystem runtimes. NAS/CIFS paths
    # (SMB) silently no-op chmod, so the boundary can't enforce there; the tool
    # must not claim to protect a path it can't lock. `status` with no path args
    # iterates default_roots(), so its output is how we observe the root list.
    env = {**os.environ, "TOKEN_OS_RUNTIME_CHECKOUT": str(tmp_path / "live")}
    proc = run("status", env=env)

    assert proc.returncode == 0, proc.stderr
    assert "/Volumes/Imperium/runtimes/token-os/live" not in proc.stdout
    assert "/mnt/imperium" not in proc.stdout
    # the local default root is still present (here: the overridden checkout)
    assert str(tmp_path / "live") in proc.stdout


def test_lock_sets_git_filemode_false_so_status_stays_clean(tmp_path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    file = root / "tool.sh"
    file.write_text("#!/usr/bin/env bash\necho hi\n")
    os.chmod(file, 0o755)
    # The runtime checkout gitignores the auto-created queue dir; mirror that so
    # lock creating `pending/` doesn't dirty status (a dirty runtime aborts deploys).
    (root / ".gitignore").write_text("/pending/\n")
    subprocess.run(["git", "add", "tool.sh", ".gitignore"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "core.filemode", "true"], cwd=root, check=True)

    proc = run("lock", str(root))

    assert proc.returncode == 0, proc.stderr
    assert (
        subprocess.check_output(["git", "config", "core.filemode"], cwd=root, text=True).strip()
        == "false"
    )
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True) == ""


# --- user-immutable layer (BSD/macOS) ----------------------------------------


@requires_chflags
def test_lock_sets_immutable_on_frozen_paths(tmp_path) -> None:
    # The belt: frozen source carries the user-immutable flag, the writable
    # queue does not.
    root = tmp_path / "runtime"
    root.mkdir()
    file = root / "app.py"
    file.write_text("print('x')\n")

    assert run("lock", str(root)).returncode == 0

    assert is_immutable(file), "frozen file must be immutable"
    assert is_immutable(root), "frozen root dir must be immutable"
    assert not is_immutable(root / "pending"), "writable queue must stay mutable"


@requires_chflags
def test_immutable_defeats_chmod_uplus_w_bypass(tmp_path) -> None:
    # The exact incident vector: an agent (the owning uid) runs `chmod u+w` then
    # writes. With uchg set, chmod itself returns EPERM and the write fails.
    root = tmp_path / "runtime"
    root.mkdir()
    file = root / "app.py"
    file.write_text("old\n")
    assert run("lock", str(root)).returncode == 0

    bypass = subprocess.run(
        [
            "bash",
            "-lc",
            f"chmod u+w {shlex.quote(str(file))} && printf new > {shlex.quote(str(file))}",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert bypass.returncode != 0, "chmod u+w bypass must fail under uchg"
    assert file.read_text() == "old\n"


@requires_chflags
def test_immutable_blocks_delete_and_recreate(tmp_path) -> None:
    # uchg also prevents rm/rename, so an agent cannot delete-and-recreate a
    # frozen file to sidestep the content-write block.
    root = tmp_path / "runtime"
    root.mkdir()
    file = root / "app.py"
    file.write_text("old\n")
    assert run("lock", str(root)).returncode == 0

    rm = subprocess.run(
        ["bash", "-lc", f"rm -f {shlex.quote(str(file))}"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert rm.returncode != 0
    assert file.exists() and file.read_text() == "old\n"


@requires_chflags
def test_unlock_clears_immutable_so_deploy_can_write(tmp_path) -> None:
    # Deploy unlocks before git sync; uchg must be gone or git can't overwrite.
    root = tmp_path / "runtime"
    root.mkdir()
    file = root / "app.py"
    file.write_text("old\n")
    assert run("lock", str(root)).returncode == 0
    assert is_immutable(file)

    assert run("unlock", str(root)).returncode == 0

    assert not is_immutable(file)
    file.write_text("new\n")  # deploy-style overwrite now succeeds
    assert file.read_text() == "new\n"


@requires_chflags
def test_relock_is_idempotent_on_already_immutable_tree(tmp_path) -> None:
    # A re-deploy locks an already-frozen, already-immutable tree. lock must
    # clear flags first so its chmod/mkdir dance runs, then re-apply uchg.
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "app.py").write_text("x\n")
    assert run("lock", str(root)).returncode == 0

    proc = run("lock", str(root))

    assert proc.returncode == 0, proc.stderr
    assert "locked" in proc.stdout
    assert is_immutable(root / "app.py")


@requires_chflags
def test_assert_locked_fails_when_immutable_flag_missing(tmp_path) -> None:
    # Drift detection: a frozen file whose immutable flag was cleared (the agent
    # bypass, or a file created after the last freeze) reads as unlocked even
    # with no write bit set.
    root = tmp_path / "runtime"
    root.mkdir()
    file = root / "app.py"
    file.write_text("x\n")
    assert run("lock", str(root)).returncode == 0
    assert run("assert-locked", str(root)).returncode == 0

    subprocess.run(["chflags", "nouchg", str(file)], check=True)
    # No write bit yet — only the immutable flag is gone.
    assert not has_any_write(file)

    proc = run("assert-locked", str(root))
    assert proc.returncode == 1
    assert "unlocked" in run("status", str(root)).stdout


# --- transient git lock files (.git/**/*.lock) are never frozen ---------------


def _seed_git_lock_files(root: Path) -> tuple[Path, Path]:
    """Create stray transient git lock files at the top of .git/ and nested under
    refs/, returning (HEAD.lock, refs/heads/main.lock)."""
    gitdir = root / ".git"
    (gitdir / "refs" / "heads").mkdir(parents=True)
    head_lock = gitdir / "HEAD.lock"
    head_lock.write_text("ref: refs/heads/main\n")
    ref_lock = gitdir / "refs" / "heads" / "main.lock"
    ref_lock.write_text("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n")
    return head_lock, ref_lock


def test_lock_ignores_transient_git_lock_files(tmp_path: Path) -> None:
    # A git lock file (HEAD.lock, refs/**/*.lock) is transient by design: a
    # concurrent deploy creates and deletes it mid-operation. Freezing one would
    # break git, and a lock that vanishes between `find` and the batched `-exec`
    # is the exact ENOENT ("No such file or directory") that falsely failed the
    # whole lock in the concurrent-deploy race. lock must skip them and still
    # succeed — while keeping a tracked `uv.lock` frozen (the prune is .git-scoped).
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "app.py").write_text("print('x')\n")
    (root / "token-api").mkdir()
    uvlock = root / "token-api" / "uv.lock"
    uvlock.write_text("# tracked lockfile — must stay frozen\n")
    head_lock, ref_lock = _seed_git_lock_files(root)

    proc = run("lock", str(root))

    assert proc.returncode == 0, proc.stderr
    assert "locked" in proc.stdout
    assert "No such file" not in proc.stderr
    # Tracked uv.lock is frozen (proves the prune is scoped to `.git`, not all *.lock).
    assert not has_any_write(uvlock)
    if HAVE_CHFLAGS:
        assert is_immutable(uvlock), "tracked uv.lock must still be frozen"
        # The transient git locks must NOT carry uchg — proof they were pruned
        # from the freeze (uchg on them would break the next git operation).
        assert not is_immutable(head_lock)
        assert not is_immutable(ref_lock)


def test_assert_locked_ignores_transient_git_lock_files(tmp_path: Path) -> None:
    # A correctly-locked tree may carry a stray .git/**/*.lock (writable + not
    # immutable, because the freeze pruned it). The verify passes must prune them
    # too, so assert-locked/status report the tree as locked rather than falsely
    # "unlocked". (Seeded before lock — once the root is frozen+immutable, mkdir
    # under .git is impossible anyway, which is precisely why the locks must be
    # left mutable and ignored.)
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "app.py").write_text("print('x')\n")
    head_lock, _ = _seed_git_lock_files(root)
    assert run("lock", str(root)).returncode == 0
    # The lock file stayed writable (not pulled into the freeze)...
    assert has_owner_write(head_lock)
    # ...yet the tree still verifies as locked.
    assert run("assert-locked", str(root)).returncode == 0
    assert "locked" in run("status", str(root)).stdout
