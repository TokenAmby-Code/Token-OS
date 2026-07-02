from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PR_STEP = REPO_ROOT / "cli-tools" / "bin" / "pr-step"


def run(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(cmd, cwd=cwd, env=merged, text=True, capture_output=True, check=True)


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "misleading-branch"], repo)
    run(["git", "config", "user.email", "test@example.invalid"], repo)
    run(["git", "config", "user.name", "Test User"], repo)
    (repo / "README.md").write_text("base\n")
    run(["git", "add", "README.md"], repo)
    run(["git", "commit", "-m", "chore: baseline"], repo)
    return repo


def bash_with_pr_step(
    body: str, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    script = f"""
set -euo pipefail
export PR_STEP_SOURCE_ONLY=1
source {str(PR_STEP)!r}
{body}
"""
    return run(["bash", "-c", script], cwd, env=env)


def test_commit_step_excludes_staged_worktree_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / ".worktree.env").write_text("# Do not commit\nPORT=9999\n")
    (repo / "real-change.txt").write_text("ship this\n")
    run(["git", "add", ".worktree.env"], repo)

    bash_with_pr_step('commit_if_needed ""', repo)

    committed_files = run(
        ["git", "show", "--name-only", "--pretty=", "HEAD"], repo
    ).stdout.splitlines()
    assert "real-change.txt" in committed_files
    assert ".worktree.env" not in committed_files
    tracked_env = run(["git", "ls-files", ".worktree.env"], repo).stdout.strip()
    assert tracked_env == ""


def test_default_commit_message_reflects_staged_change_not_branch_name(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    (repo / "actual-change.txt").write_text("actual\n")

    bash_with_pr_step('commit_if_needed ""', repo)

    subject = run(["git", "log", "-1", "--pretty=%s"], repo).stdout.strip()
    assert subject == "chore: add actual-change.txt"


def test_coderabbit_heartbeat_writes_visible_status(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    heartbeat = tmp_path / "heartbeat.log"

    bash_with_pr_step(
        'emit_coderabbit_heartbeat "CodeRabbit poll: still waiting"',
        repo,
        {"PR_STEP_HEARTBEAT_FILE": str(heartbeat)},
    )

    assert "CodeRabbit poll: still waiting" in heartbeat.read_text()


def test_findings_summary_filters_to_current_head_and_marks_historical(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    gh = fake_bin / "gh"
    gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/comments" ]]; then
  cat <<'JSON'
[
  {"user":{"login":"coderabbitai[bot]"},"path":".worktree.env","line":1,"commit_id":"oldhead","updated_at":"2026-06-29T17:00:00Z","body":"old env finding"},
  {"user":{"login":"coderabbitai[bot]"},"path":"cli-tools/bin/pr-step","line":42,"commit_id":"headnew","updated_at":"2026-06-29T18:00:00Z","body":"current head finding"}
]
JSON
else
  echo 'unexpected gh call' >&2
  exit 1
fi
"""
    )
    gh.chmod(0o755)

    result = bash_with_pr_step(
        "repo_slug() { echo owner/repo; }\nsummarize_actionable_findings 7 headnew",
        repo,
        {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert "cli-tools/bin/pr-step" in result.stdout
    assert "current head finding" in result.stdout
    assert ".worktree.env" not in result.stdout
    assert "historical/resolved CodeRabbit findings omitted: 1" in result.stdout
