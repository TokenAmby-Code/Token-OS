import os
import shlex
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "cli-tools" / "scripts" / "runtime-write-protect.sh"


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
    assert "/mnt/imperium/runtimes/token-os/live" not in proc.stdout
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
    subprocess.run(["git", "add", "tool.sh"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "core.filemode", "true"], cwd=root, check=True)

    proc = run("lock", str(root))

    assert proc.returncode == 0, proc.stderr
    assert (
        subprocess.check_output(["git", "config", "core.filemode"], cwd=root, text=True).strip()
        == "false"
    )
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True) == ""
