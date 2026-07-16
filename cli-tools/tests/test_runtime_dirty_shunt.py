from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHUNT = ROOT / "cli-tools/lib/runtime-dirty-shunt.sh"


def run(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, text=True, capture_output=True).stdout


def setup_repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    seed, bare, runtime = tmp_path / "seed", tmp_path / "cache.git", tmp_path / "live"
    seed.mkdir()
    run("git", "init", cwd=seed)
    run("git", "config", "user.name", "test", cwd=seed)
    run("git", "config", "user.email", "test@example.invalid", cwd=seed)
    (seed / "tracked.txt").write_text("base\n")
    run("git", "add", ".", cwd=seed)
    run("git", "commit", "-m", "base", cwd=seed)
    base = run("git", "rev-parse", "HEAD", cwd=seed).strip()
    run("git", "clone", "--bare", str(seed), str(bare), cwd=tmp_path)
    (seed / "target.txt").write_text("target\n")
    run("git", "add", ".", cwd=seed)
    run("git", "commit", "-m", "target", cwd=seed)
    target = run("git", "rev-parse", "HEAD", cwd=seed).strip()
    run("git", "push", str(bare), "HEAD:main", cwd=seed)
    run("git", "clone", str(bare), str(runtime), cwd=tmp_path)
    run("git", "checkout", "--detach", base, cwd=runtime)
    return bare, runtime, base, target


def invoke(
    runtime: Path, bare: Path, target: str, *, bad_bare: bool = False
) -> subprocess.CompletedProcess[str]:
    actual_bare = runtime / "missing.git" if bad_bare else bare
    script = f'source "{SHUNT}"; runtime_shunt_dirty "{runtime}" "{actual_bare}" Token-OS "{target}" k12-test'
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True)


def test_dirty_runtime_is_losslessly_committed_and_receipted(tmp_path: Path) -> None:
    bare, runtime, base, target = setup_repo(tmp_path)
    (runtime / "tracked.txt").write_text("dirty tracked\n")
    (runtime / "untracked.txt").write_text("dirty untracked\n")
    result = invoke(runtime, bare, target)
    assert result.returncode == 0, result.stderr
    branch = run(
        "git",
        "--git-dir",
        str(bare),
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads/wip",
        cwd=tmp_path,
    ).strip()
    assert branch.startswith("wip/live-dirty-")
    recovered = run("git", "--git-dir", str(bare), "show", f"{branch}:tracked.txt", cwd=tmp_path)
    assert recovered == "dirty tracked\n"
    assert (
        run("git", "--git-dir", str(bare), "show", f"{branch}:untracked.txt", cwd=tmp_path)
        == "dirty untracked\n"
    )
    receipts = list((bare / "runtime-recovery-receipts").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["old_sha"] == base and receipt["target_sha"] == target
    assert receipt["recovery_branch"] == branch


def test_clean_runtime_is_a_noop(tmp_path: Path) -> None:
    bare, runtime, base, target = setup_repo(tmp_path)
    result = invoke(runtime, bare, target)
    assert result.returncode == 0
    assert run("git", "rev-parse", "HEAD", cwd=runtime).strip() == base
    assert not (bare / "runtime-recovery-receipts").exists()


def test_preservation_failure_does_not_clean_or_checkout_runtime(tmp_path: Path) -> None:
    bare, runtime, base, target = setup_repo(tmp_path)
    (runtime / "tracked.txt").write_text("must survive\n")
    result = invoke(runtime, bare, target, bad_bare=True)
    assert result.returncode != 0
    # The failed push may have committed locally, but it must never advance to
    # the deploy target or discard the recoverable content.
    assert run("git", "rev-parse", "HEAD", cwd=runtime).strip() != target
    assert run("git", "show", "HEAD:tracked.txt", cwd=runtime) == "must survive\n"
