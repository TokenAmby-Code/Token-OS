from __future__ import annotations

import json
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


def install_fake_curl(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / "curl.calls.jsonl"
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
python3 - "$CURL_LOG" "$@" <<'PY'
import json, sys
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[2:]) + "\\n")
PY
"""
    )
    curl.chmod(0o755)
    return fake_bin, log


def curl_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def curl_json_bodies(log: Path, endpoint: str | None = None) -> list[dict[str, object]]:
    bodies: list[dict[str, object]] = []
    for args in curl_calls(log):
        if endpoint is not None and not any(endpoint in arg for arg in args):
            continue
        body = args[args.index("-d") + 1]
        bodies.append(json.loads(body))
    return bodies


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


def test_mark_instance_status_reviewing_sends_workflow_payload(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        "mark_instance_status reviewing",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "TOKEN_API_INSTANCE_ID": "inst-123",
            "TOKEN_API_URL": "http://token-api.test",
        },
    )

    calls = curl_calls(curl_log)
    assert len(calls) == 1
    assert "PATCH" in calls[0]
    assert calls[0][-1] == "http://token-api.test/api/instances/inst-123/status"
    body = curl_json_bodies(curl_log)[0]
    assert body == {
        "status": "reviewing",
        "workflow_state": "review_mode",
        "next_required_action": "review",
        "next_action_owner": "human",
    }


def test_pr_step_does_not_arm_generic_plan_hook_at_startup(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        """
assert_repo() { :; }
current_pr_number() { echo 22; }
current_pr_url() { echo https://github.com/owner/repo/pull/22; }
mark_pr_flag() { :; }
commit_if_needed() { return 1; }
push_branch() { :; }
checks_green() { return 0; }
summarize_pr() { :; }
main --no-merge
""",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "TOKEN_API_INSTANCE_ID": "inst-123",
            "TMUX_PANE": "%20",
        },
    )

    hooks = curl_json_bodies(curl_log, "/api/hooks/subscribe")
    assert hooks == []


def test_review_completion_arms_contextual_plan_followup(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        """
assert_repo() { :; }
current_pr_number() { echo 17; }
current_pr_url() { echo https://github.com/owner/repo/pull/17; }
mark_instance_status() { :; }
mark_pr_flag() { :; }
commit_if_needed() { return 1; }
push_branch() { :; }
checks_green() { return 1; }
review_pr_normal() { return 1; }
summarize_pr() { :; }
main --no-merge
""",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "TOKEN_API_INSTANCE_ID": "inst-123",
            "TMUX_PANE": "%20",
        },
    )

    hooks = curl_json_bodies(curl_log, "/api/hooks/subscribe")
    assert len(hooks) == 1
    assert hooks[0]["purpose"] == "pr_step_plan"
    assert hooks[0]["event"] == "stop"
    assert hooks[0]["delivery"] == "prompt"
    assert hooks[0]["oneshot"] is True
    assert hooks[0]["target_pane"] == "%20"
    assert hooks[0]["subscriber_pane"] == "%20"
    assert hooks[0]["payload"] == (
        "/plan PR #17 review returned "
        "(https://github.com/owner/repo/pull/17); plan fixes or next review action."
    )


def test_merge_completion_overwrites_with_contextual_merge_followup(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        """
assert_repo() { :; }
current_pr_number() { echo 17; }
current_pr_url() { echo https://github.com/owner/repo/pull/17; }
mark_instance_status() { :; }
mark_pr_flag() { :; }
commit_if_needed() { return 1; }
push_branch() { :; }
_checks_green_calls=0
checks_green() {
    _checks_green_calls=$((_checks_green_calls + 1))
    [[ $_checks_green_calls -ge 2 ]]
}
review_pr_normal() { return 0; }
summarize_pr() { :; }
merge_pr_normal() { return 0; }
main
""",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "TOKEN_API_INSTANCE_ID": "inst-123",
            "TMUX_PANE": "%20",
        },
    )

    hooks = curl_json_bodies(curl_log, "/api/hooks/subscribe")
    assert [hook["purpose"] for hook in hooks] == ["pr_step_plan", "pr_step_plan"]
    assert "review returned" in str(hooks[0]["payload"])
    assert hooks[-1]["payload"] == (
        "/plan PR #17 merged "
        "(https://github.com/owner/repo/pull/17); summarize closure and update context."
    )


def test_plan_followup_missing_instance_or_pane_is_noop(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        """
unset TOKEN_API_INSTANCE_ID TMUX_PANE TOKEN_API_DISPATCH_RESOLVED_PANE
arm_pr_plan_followup review 17 https://github.com/owner/repo/pull/17 "plan fixes or next review action."
export TOKEN_API_INSTANCE_ID=inst-123
unset TMUX_PANE TOKEN_API_DISPATCH_RESOLVED_PANE
arm_pr_plan_followup merge 17 https://github.com/owner/repo/pull/17 "summarize closure and update context."
""",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
        },
    )

    assert curl_calls(curl_log) == []


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
