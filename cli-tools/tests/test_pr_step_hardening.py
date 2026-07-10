from __future__ import annotations

import json
import os
import shlex
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
import json, os, sys
args = sys.argv[2:]
with open(sys.argv[1], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
url = args[-1] if args else ""
if "/ledger/resolve" in url and os.environ.get("CURL_LEDGER_JSON"):
    print(os.environ["CURL_LEDGER_JSON"], end="")
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
        if "-d" not in args:
            continue
        body = args[args.index("-d") + 1]
        bodies.append(json.loads(body))
    return bodies


def ledger_json(instance_id: str = "inst-123", pane: str = "%20") -> str:
    return json.dumps(
        {
            "ok": True,
            "result": {
                "found": True,
                "row": {
                    "wrapper_id": "wrap-123",
                    "instance_id": instance_id,
                    "pane_positional_id": pane,
                    "state": "OPEN",
                },
            },
        }
    )


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


def test_coderabbit_wait_is_unbounded_by_default(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
parse_args
printf 'timeout=<%s> seconds=%s timed_out=' "$TIMEOUT_MINS" "$(timeout_mins_to_seconds "$TIMEOUT_MINS")"
if wait_timed_out 999999 "$(timeout_mins_to_seconds "$TIMEOUT_MINS")"; then
  printf 'yes\\n'
else
  printf 'no\\n'
fi
""",
        repo,
    )

    assert "timeout=<> seconds=0 timed_out=no" in result.stdout


def test_explicit_timeout_remains_available_as_operator_cap(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
parse_args --timeout 2
printf 'timeout=<%s> seconds=%s timed_out=' "$TIMEOUT_MINS" "$(timeout_mins_to_seconds "$TIMEOUT_MINS")"
if wait_timed_out 120 "$(timeout_mins_to_seconds "$TIMEOUT_MINS")"; then
  printf 'yes\\n'
else
  printf 'no\\n'
fi
""",
        repo,
    )

    assert "timeout=<2> seconds=120 timed_out=yes" in result.stdout


def test_rate_limit_reset_parser_uses_real_signal_not_magic_sleep(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
date() { if [[ "${1:-}" == "+%s" ]]; then echo 1000; else command date "$@"; fi; }
printf 'retry_after=%s\\n' "$(rate_limit_reset_epoch_from_text 1000 <<<'HTTP 403
Retry-After: 9')"
printf 'retry_after_http_date=%s\\n' "$(rate_limit_reset_epoch_from_text 1000 <<<'Retry-After: Thu, 01 Jan 1970 00:16:49 GMT')"
printf 'inline_retry_after=%s\\n' "$(rate_limit_reset_epoch_from_text 1000 <<<'CodeRabbit rate limit exceeded. Retry-After: 9')"
printf 'reset_header=%s\\n' "$(rate_limit_reset_epoch_from_text 1000 <<<'x-ratelimit-reset: 1700001017')"
printf 'prose_wait=%s\\n' "$(rate_limit_reset_epoch_from_text 1000 <<<'Please wait 2 minutes and 3 seconds before requesting another review.')"
printf 'coderabbit_available=%s\\n' "$(rate_limit_reset_epoch_from_text 1000 <<<'**Next review available in:** **9 seconds**')"
printf 'coderabbit_reply_available=%s\\n' "$(rate_limit_reset_epoch_from_text 1000 <<<'Your next review will be available in 4 minutes.')"
printf 'absent=<%s>\\n' "$(rate_limit_reset_epoch_from_text 1000 <<<'rate limit exceeded' || true)"
printf 'seconds=%s\\n' "$(coderabbit_ratelimit_wait_seconds 'Retry-After: 9')"
""",
        repo,
    )

    assert "retry_after=1009" in result.stdout
    assert "retry_after_http_date=1009" in result.stdout
    assert "inline_retry_after=1009" in result.stdout
    assert "reset_header=1700001017" in result.stdout
    assert "prose_wait=1123" in result.stdout
    assert "coderabbit_available=1009" in result.stdout
    assert "coderabbit_reply_available=1240" in result.stdout
    assert "absent=<>" in result.stdout
    assert "seconds=9" in result.stdout


def test_already_reviewed_skip_is_not_rate_limit_deferral(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
if coderabbit_body_is_ratelimit 'Result: Skipped - Already reviewed'; then
  echo bad
else
  echo ok
fi
if coderabbit_body_is_ratelimit 'Review limit reached. Next review available in: 12 minutes'; then
  echo limited
fi
""",
        repo,
    )

    assert "ok" in result.stdout
    assert "bad" not in result.stdout
    assert "limited" in result.stdout


def test_normal_review_does_not_inject_empty_timeout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    arg_log = tmp_path / "args.log"

    bash_with_pr_step(
        f"""
run_internal_capture() {{
  shift 2
  printf '%s\\n' "$@" > {str(arg_log)!r}
}}
review_pr_normal 7 "check latest head"
""",
        repo,
    )

    args = arg_log.read_text().splitlines()
    assert args == ["pr_review_main", "7", "--message", "check latest head"]
    assert "--timeout" not in args


def test_normal_create_does_not_inject_empty_timeout(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    arg_log = tmp_path / "args.log"

    bash_with_pr_step(
        f"""
run_internal_capture() {{
  shift 2
  printf '%s\\n' "$@" > {str(arg_log)!r}
}}
current_pr_number() {{ echo 7; }}
current_pr_url() {{ echo https://example.invalid/pr/7; }}
mark_pr_flag() {{ :; }}
create_pr_normal >/dev/null
""",
        repo,
    )

    args = arg_log.read_text().splitlines()
    assert "--wait" in args
    assert "--timeout" not in args


def prepare_dual_remote_upstream_repo(tmp_path: Path) -> Path:
    repo = init_repo(tmp_path)
    bare = tmp_path / "upstream.git"
    run(["git", "init", "--bare", str(bare)], repo)
    run(["git", "remote", "add", "origin", str(bare)], repo)
    run(["git", "push", "-u", "origin", "misleading-branch"], repo)
    run(["git", "remote", "set-url", "origin", "git@github.com:TokenAmby-Code/Token-OS.git"], repo)
    run(["git", "remote", "add", "github", "git@github.com:TokenAmby-Code/Token-OS.git"], repo)
    return repo


def install_fake_gh_for_create(tmp_path: Path) -> tuple[Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    arg_log = tmp_path / "gh-pr-create.args0"
    gh = fake_bin / "gh"
    gh.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "repo" && "$2" == "view" ]]; then
  echo owner/repo
  exit 0
fi
if [[ "$1" == "pr" && "$2" == "create" ]]; then
  printf '%s\n' "$@" > {str(arg_log)!r}
  echo https://github.com/owner/repo/pull/123
  exit 0
fi
echo "unexpected gh call: $*" >&2
exit 1
"""
    )
    gh.chmod(0o755)
    return fake_bin, arg_log


def read_logged_args(path: Path) -> list[str]:
    return path.read_text().splitlines()


def test_pr_create_main_injects_current_branch_head_for_dual_remote_create(
    tmp_path: Path,
) -> None:
    repo = prepare_dual_remote_upstream_repo(tmp_path)
    fake_bin, arg_log = install_fake_gh_for_create(tmp_path)

    bash_with_pr_step(
        'pr_create_main --title "test title" --body "test body" --no-wait',
        repo,
        {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    args = read_logged_args(arg_log)
    assert args[:2] == ["pr", "create"]
    assert "--head" in args
    assert args[args.index("--head") + 1] == "misleading-branch"


def test_pr_create_main_preserves_explicit_head(tmp_path: Path) -> None:
    repo = prepare_dual_remote_upstream_repo(tmp_path)
    fake_bin, arg_log = install_fake_gh_for_create(tmp_path)

    bash_with_pr_step(
        'pr_create_main --title "test title" --body "test body" --head explicit-head --no-wait',
        repo,
        {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    args = read_logged_args(arg_log)
    assert args.count("--head") == 1
    assert args[args.index("--head") + 1] == "explicit-head"


def test_plain_pr_step_create_failure_reemits_captured_create_error(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
set +e
assert_repo() { :; }
mark_instance_status() { :; }
current_pr_number() { :; }
commit_if_needed() { return 1; }
push_branch() { :; }
create_pr_normal() {
  printf '%s\n' 'captured create output before failure'
  printf '%s\n' '[pr-create] Failed to create PR: you must first push the current branch to a remote, or use the --head flag'
  return 42
}
main --no-merge
rc=$?
set -e
printf 'rc=%s\n' "$rc"
exit 0
""",
        repo,
    )

    assert "rc=42" in result.stdout
    assert "you must first push the current branch" in result.stderr
    assert "Could not determine created PR number" not in result.stderr


def test_review_loop_waits_for_coderabbit_rate_limit_reset_then_returns_review(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.write_text("initial")
    calls = tmp_path / "gh.calls"
    sleeps = tmp_path / "sleep.calls"
    gh = fake_bin / "gh"
    gh.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {str(calls)!r}
state="$(cat {str(state)!r})"
if [[ "$1" == "repo" && "$2" == "view" ]]; then
  echo owner/repo
  exit 0
fi
if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  if [[ "$state" == "initial" ]]; then
    echo rate_limited > {str(state)!r}
  elif [[ "$state" == "rate_limited" ]]; then
    echo reviewed > {str(state)!r}
  fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7" ]]; then
  if [[ "${{@: -2:1}}" == "--jq" ]]; then echo headsha; else echo '{{"head":{{"sha":"headsha"}}}}'; fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/comments" ]]; then
  echo '[]'
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/issues/7/comments" ]]; then
  if [[ "$state" == "rate_limited" ]]; then
    cat <<'JSON'
[
  {{"user":{{"login":"coderabbitai[bot]"}},"created_at":"1970-01-01T00:16:40Z","updated_at":"1970-01-01T00:16:40Z","body":"CodeRabbit rate limit exceeded. Retry-After: 9"}}
]
JSON
  elif [[ "$state" == "reviewed" ]]; then
    cat <<'JSON'
[
  {{"user":{{"login":"coderabbitai[bot]"}},"created_at":"1970-01-01T00:16:40Z","updated_at":"1970-01-01T00:16:40Z","body":"CodeRabbit rate limit exceeded. Retry-After: 9"}},
  {{"user":{{"login":"coderabbitai[bot]"}},"created_at":"1970-01-01T00:16:50Z","updated_at":"1970-01-01T00:16:50Z","body":"## Summary by CodeRabbit\\nAll clear."}}
]
JSON
  else
    echo '[]'
  fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/commits/headsha/statuses" ]]; then
  if [[ "$state" == "reviewed" ]]; then
    echo '[{{"context":"coderabbit","state":"success","updated_at":"1970-01-01T00:16:50Z"}}]'
  else
    echo '[]'
  fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/reviews" ]]; then
  if [[ "$state" == "reviewed" ]]; then
    echo '[{{"user":{{"login":"coderabbitai[bot]"}},"state":"APPROVED","submitted_at":"1970-01-01T00:16:51Z"}}]'
  else
    echo '[]'
  fi
  exit 0
fi
echo "unexpected gh call: $*" >&2
exit 1
"""
    )
    gh.chmod(0o755)

    result = bash_with_pr_step(
        f"""
_FAKE_NOW=1000
date() {{ if [[ "${{1:-}}" == "+%s" ]]; then echo "$_FAKE_NOW"; else command date "$@"; fi; }}
sleep() {{ printf '%s\\n' "$1" >> {str(sleeps)!r}; printf 'sleep %s\\n' "$1" >> {str(calls)!r}; _FAKE_NOW=$((_FAKE_NOW + $1)); }}
pr_review_main 7 --message "fix" --no-push
""",
        repo,
        {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert "Review State:    APPROVED" in result.stdout
    assert "## Summary by CodeRabbit" in result.stdout
    sleep_values = sleeps.read_text().splitlines()
    assert "69" in sleep_values
    assert "9" not in sleep_values
    assert "60" not in sleep_values
    assert "120" not in sleep_values
    assert "150" not in sleep_values
    call_lines = calls.read_text().splitlines()
    comment_indexes = [i for i, line in enumerate(call_lines) if line.startswith("pr comment 7")]
    assert len(comment_indexes) == 2
    sleep_index = call_lines.index("sleep 69")
    # No blind duplicate re-request: the second CodeRabbit command happens only
    # after pr-step sleeps to the reset carried by the rate-limit response.
    assert comment_indexes[0] < sleep_index < comment_indexes[1]


def test_review_loop_handles_preexisting_rate_limit_deferral_after_reset(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.write_text("rate_limited")
    requests = tmp_path / "requests"
    requests.write_text("0")
    calls = tmp_path / "gh.calls"
    sleeps = tmp_path / "sleep.calls"
    gh = fake_bin / "gh"
    gh.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {str(calls)!r}
state="$(cat {str(state)!r})"
if [[ "$1" == "repo" && "$2" == "view" ]]; then echo owner/repo; exit 0; fi
if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  n="$(cat {str(requests)!r})"
  n=$((n + 1))
  echo "$n" > {str(requests)!r}
  if [[ "$n" -ge 2 ]]; then echo reviewed > {str(state)!r}; fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7" ]]; then
  if [[ "${{@: -2:1}}" == "--jq" ]]; then echo headsha; else echo '{{"head":{{"sha":"headsha"}}}}'; fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/comments" ]]; then echo '[]'; exit 0; fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/issues/7/comments" ]]; then
  if [[ "$state" == "reviewed" ]]; then
    cat <<'JSON'
[
  {{"user":{{"login":"coderabbitai[bot]"}},"created_at":"1970-01-01T00:16:40Z","updated_at":"1970-01-01T00:16:40Z","body":"You're currently rate limited. Your next review will be available in 9 seconds."}},
  {{"user":{{"login":"coderabbitai[bot]"}},"created_at":"1970-01-01T00:16:50Z","updated_at":"1970-01-01T00:16:50Z","body":"## Summary by CodeRabbit\\nAll clear."}}
]
JSON
  else
    cat <<'JSON'
[
  {{"user":{{"login":"coderabbitai[bot]"}},"created_at":"1970-01-01T00:16:40Z","updated_at":"1970-01-01T00:16:40Z","body":"You're currently rate limited. Your next review will be available in 9 seconds."}}
]
JSON
  fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/commits/headsha/statuses" ]]; then
  echo '[{{"context":"coderabbit","state":"success","updated_at":"1970-01-01T00:16:40Z"}}]'
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/reviews" ]]; then
  if [[ "$state" == "reviewed" ]]; then
    echo '[{{"user":{{"login":"coderabbitai[bot]"}},"state":"APPROVED","submitted_at":"1970-01-01T00:16:51Z"}}]'
  else
    echo '[]'
  fi
  exit 0
fi
echo "unexpected gh call: $*" >&2
exit 1
"""
    )
    gh.chmod(0o755)

    result = bash_with_pr_step(
        f"""
_FAKE_NOW=1000
date() {{ if [[ "${{1:-}}" == "+%s" ]]; then echo "$_FAKE_NOW"; else command date "$@"; fi; }}
sleep() {{ printf '%s\\n' "$1" >> {str(sleeps)!r}; printf 'sleep %s\\n' "$1" >> {str(calls)!r}; _FAKE_NOW=$((_FAKE_NOW + $1)); }}
pr_review_main 7 --message "fix" --no-push
""",
        repo,
        {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert "Review State:    APPROVED" in result.stdout
    assert "## Summary by CodeRabbit" in result.stdout
    sleep_values = sleeps.read_text().splitlines()
    assert sleep_values[0] == "69"
    assert "9" not in sleep_values
    assert "60" not in sleep_values
    assert "120" not in sleep_values
    assert "150" not in sleep_values
    call_lines = calls.read_text().splitlines()
    comment_indexes = [i for i, line in enumerate(call_lines) if line.startswith("pr comment 7")]
    assert len(comment_indexes) == 2
    assert comment_indexes[0] < call_lines.index("sleep 69") < comment_indexes[1]


def test_review_loop_blocks_until_pending_review_lands(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.write_text("pending")
    sleeps = tmp_path / "sleep.calls"
    gh = fake_bin / "gh"
    gh.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
state="$(cat {str(state)!r})"
if [[ "$1" == "repo" && "$2" == "view" ]]; then echo owner/repo; exit 0; fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7" ]]; then
  if [[ "${{@: -2:1}}" == "--jq" ]]; then echo headsha; else echo '{{"head":{{"sha":"headsha"}}}}'; fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/comments" ]]; then echo '[]'; exit 0; fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/issues/7/comments" ]]; then
  echo '[]'
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/commits/headsha/statuses" ]]; then
  echo '[]'
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/reviews" ]]; then
  if [[ "$state" == "reviewed" ]]; then echo '[{{"user":{{"login":"coderabbitai[bot]"}},"state":"APPROVED","submitted_at":"1970-01-01T00:01:01Z","body":"review body all clear"}}]'; else echo '[]'; fi
  exit 0
fi
exit 1
"""
    )
    gh.chmod(0o755)

    result = bash_with_pr_step(
        f"""
_sleeps=0
sleep() {{
  printf '%s\\n' "$1" >> {str(sleeps)!r}
  _sleeps=$((_sleeps + 1))
  if [[ "$_sleeps" -ge 2 ]]; then echo reviewed > {str(state)!r}; fi
}}
pr_review_main 7 --read
""",
        repo,
        {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert sleeps.read_text().splitlines()[:2] == ["30", "30"]
    assert "No new review comments found before the explicit timeout" not in result.stderr
    assert "Review State:    APPROVED" in result.stdout
    assert "review body all clear" in result.stdout


def test_review_loop_returns_findings_review_body_and_failed_check_logs_inline(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "state"
    state.write_text("initial")
    gh = fake_bin / "gh"
    gh.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
state="$(cat {str(state)!r})"
if [[ "$1" == "repo" && "$2" == "view" ]]; then echo owner/repo; exit 0; fi
if [[ "$1" == "pr" && "$2" == "comment" ]]; then echo reviewed > {str(state)!r}; exit 0; fi
if [[ "$1" == "pr" && "$2" == "checks" ]]; then
  cat <<'JSON'
[
  {{"name":"quality","workflow":"CI","state":"failure","bucket":"fail","link":"https://github.com/owner/repo/actions/runs/123/job/456","description":"quality gate failed"}}
]
JSON
  exit 0
fi
if [[ "$1" == "run" && "$2" == "view" && "$3" == "123" ]]; then
  echo 'quality failed line from log'
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7" ]]; then
  if [[ "${{@: -2:1}}" == "--jq" ]]; then echo headsha; else echo '{{"head":{{"sha":"headsha"}}}}'; fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/comments" ]]; then
  if [[ "$state" == "reviewed" ]]; then
    cat <<'JSON'
[
  {{"user":{{"login":"coderabbitai[bot]"}},"path":"cli-tools/bin/pr-step","line":42,"commit_id":"headsha","body":"specific inline finding body"}}
]
JSON
  else echo '[]'; fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/issues/7/comments" ]]; then
  if [[ "$state" == "reviewed" ]]; then
    echo '[{{"user":{{"login":"coderabbitai[bot]"}},"created_at":"1970-01-01T00:01:00Z","updated_at":"1970-01-01T00:01:00Z","body":"## Summary by CodeRabbit\\nPlease fix the finding."}}]'
  else echo '[]'; fi
  exit 0
fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/commits/headsha/statuses" ]]; then echo '[{{"context":"coderabbit","state":"failure"}}]'; exit 0; fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7/reviews" ]]; then
  if [[ "$state" == "reviewed" ]]; then echo '[{{"user":{{"login":"coderabbitai[bot]"}},"state":"CHANGES_REQUESTED","submitted_at":"1970-01-01T00:01:01Z","body":"review body says change this"}}]'; else echo '[]'; fi
  exit 0
fi
exit 1
"""
    )
    gh.chmod(0o755)

    result = bash_with_pr_step(
        """
set +e
pr_review_main 7 --message "fix" --no-push
rc=$?
set -e
printf 'rc=%s\n' "$rc"
""",
        repo,
        {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert "rc=1" in result.stdout
    assert "Review State:    CHANGES_REQUESTED" in result.stdout
    assert "## Summary by CodeRabbit" in result.stdout
    assert "specific inline finding body" in result.stdout
    assert "review body says change this" in result.stdout
    assert "quality gate failed" in result.stdout
    assert "quality failed line from log" in result.stdout


def test_review_loop_plain_timeout_path_does_not_rate_limit_rerequest(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "gh.calls"
    sleeps = tmp_path / "sleep.calls"
    gh = fake_bin / "gh"
    gh.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {str(calls)!r}
if [[ "$1" == "repo" && "$2" == "view" ]]; then echo owner/repo; exit 0; fi
if [[ "$1" == "pr" && "$2" == "comment" ]]; then exit 0; fi
if [[ "$1" == "api" && "$2" == "repos/owner/repo/pulls/7" ]]; then
  if [[ "${{@: -2:1}}" == "--jq" ]]; then echo headsha; else echo '{{"head":{{"sha":"headsha"}}}}'; fi
  exit 0
fi
case "$2" in
  repos/owner/repo/pulls/7/comments|repos/owner/repo/issues/7/comments|repos/owner/repo/pulls/7/reviews)
    echo '[]'; exit 0 ;;
  repos/owner/repo/commits/headsha/statuses)
    echo '[]'; exit 0 ;;
esac
echo "unexpected gh call: $*" >&2
exit 1
"""
    )
    gh.chmod(0o755)

    result = bash_with_pr_step(
        f"""
_FAKE_NOW=1000
date() {{ if [[ "${{1:-}}" == "+%s" ]]; then echo "$_FAKE_NOW"; else command date "$@"; fi; }}
sleep() {{ printf '%s\\n' "$1" >> {str(sleeps)!r}; _FAKE_NOW=$((_FAKE_NOW + $1)); }}
set +e
pr_review_main 7 --message "fix" --no-push --timeout 1
rc=$?
set -e
printf 'rc=%s\\n' "$rc"
""",
        repo,
        {"PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )

    assert "rc=1" in result.stdout
    assert "cr_wait_exceeded" in result.stderr
    assert "Check manually" not in result.stderr
    call_lines = calls.read_text().splitlines()
    assert sum(1 for line in call_lines if line.startswith("pr comment 7")) == 3


def test_mark_instance_status_reviewing_sends_workflow_payload(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        "mark_instance_status reviewing",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "CURL_LEDGER_JSON": ledger_json("inst-ledger", "%20"),
            "TOKEN_API_WRAPPER_ID": "wrap-123",
            "TOKEN_API_URL": "http://token-api.test",
        },
    )

    calls = curl_calls(curl_log)
    assert any("/ledger/resolve?wrapper_id=wrap-123" in call[-1] for call in calls)
    status_calls = [call for call in calls if "/api/instances/" in call[-1]]
    assert len(status_calls) == 1
    assert "PATCH" in status_calls[0]
    assert status_calls[0][-1] == "http://token-api.test/api/instances/inst-ledger/status"
    body = curl_json_bodies(curl_log, "/api/instances/")[0]
    assert body == {
        "status": "reviewing",
        "workflow_state": "review_mode",
        "next_required_action": "review",
        "next_action_owner": "human",
    }


def test_pr_flag_uses_explicit_instance_env_when_no_wrapper(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        "mark_pr_flag https://github.com/owner/repo/pull/621 open",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "TOKEN_API_INSTANCE_ID": "inst-env-owner",
        },
    )

    pr_calls = [call for call in curl_calls(curl_log) if "/api/instances/" in call[-1]]
    assert len(pr_calls) == 1
    assert pr_calls[0][-1].endswith("/api/instances/inst-env-owner/pr")


def test_pr_flag_prefers_explicit_instance_env_over_stale_pane(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        "unset TOKEN_API_WRAPPER_ID TOKEN_API_WRAPPER_LAUNCH_ID; mark_pr_flag https://github.com/owner/repo/pull/622 merged",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "CURL_LEDGER_JSON": ledger_json("inst-stale-pane", "%stale"),
            "TOKEN_API_INSTANCE_ID": "inst-true-owner",
            "TMUX_PANE": "%stale",
        },
    )

    pr_calls = [call for call in curl_calls(curl_log) if "/api/instances/" in call[-1]]
    assert len(pr_calls) == 1
    assert pr_calls[0][-1].endswith("/api/instances/inst-true-owner/pr")


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
            "CURL_LEDGER_JSON": ledger_json("inst-123", "%ledger"),
            "TOKEN_API_WRAPPER_ID": "wrap-123",
        },
    )

    hooks = curl_json_bodies(curl_log, "/api/hooks/subscribe")
    assert len(hooks) == 1
    assert hooks[0]["purpose"] == "pr_step_plan"
    assert hooks[0]["event"] == "stop"
    assert hooks[0]["delivery"] == "prompt"
    assert hooks[0]["oneshot"] is True
    assert hooks[0]["target_instance_id"] == "inst-123"
    assert hooks[0]["subscriber_instance_id"] == "inst-123"
    assert hooks[0]["target_pane"] == "%ledger"
    assert hooks[0]["subscriber_pane"] == "%ledger"
    assert hooks[0]["payload"].startswith(
        "/plan PR #17 review returned "
        "(https://github.com/owner/repo/pull/17); plan fixes or next review action."
    )
    assert "CodeRabbit verdict:" in hooks[0]["payload"]


def test_merge_completion_does_not_arm_terminal_plan_followup(tmp_path: Path) -> None:
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
            "CURL_LEDGER_JSON": ledger_json("inst-123", "%ledger"),
            "TOKEN_API_WRAPPER_ID": "wrap-123",
        },
    )

    hooks = curl_json_bodies(curl_log, "/api/hooks/subscribe")
    assert hooks == []
    unsubs = curl_json_bodies(curl_log, "/api/hooks/unsubscribe")
    assert len(unsubs) == 1
    assert unsubs[0]["target_instance_id"] == "inst-123"
    assert unsubs[0]["subscriber_instance_id"] == "inst-123"
    assert unsubs[0]["purpose"] == "pr_step_plan"


def test_marker_strand_repro_requires_rearm_to_reuse_merge_instance_id(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        """
mark_pr_flag https://github.com/owner/repo/pull/17 merged
arm_pr_plan_followup review 17 https://github.com/owner/repo/pull/17 "plan fixes or next review action."
""",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "CURL_LEDGER_JSON": ledger_json("inst-merge-marker", "%ledger"),
            "TOKEN_API_WRAPPER_ID": "wrap-123",
        },
    )

    pr_calls = [call for call in curl_calls(curl_log) if "/api/instances/" in call[-1]]
    assert any(call[-1].endswith("/api/instances/inst-merge-marker/pr") for call in pr_calls)
    pr_body = curl_json_bodies(curl_log, "/api/instances/inst-merge-marker/pr")[0]
    assert pr_body["pr_state"] == "merged"

    hooks = curl_json_bodies(curl_log, "/api/hooks/subscribe")
    assert len(hooks) == 1
    assert hooks[0]["target_instance_id"] == "inst-merge-marker"
    assert hooks[0]["subscriber_instance_id"] == "inst-merge-marker"
    assert hooks[0]["target_pane"] == "%ledger"


def test_merged_clean_pr_does_not_arm_terminal_plan_followup(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    result = bash_with_pr_step(
        """
assert_repo() { :; }
current_pr_number() { echo 17; }
current_pr_state() { echo MERGED; }
current_pr_url() { echo https://github.com/owner/repo/pull/17; }
mark_instance_status() { printf 'status:%s\\n' "$1"; }
commit_if_needed() { echo "unexpected commit" >&2; return 99; }
push_branch() { echo "unexpected push" >&2; return 99; }
review_pr_normal() { echo "unexpected review" >&2; return 99; }
summarize_pr() { printf 'summarized:%s\\n' "$1"; }
main --no-merge
""",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "CURL_LEDGER_JSON": ledger_json("inst-terminal", "%ledger"),
            "TOKEN_API_WRAPPER_ID": "wrap-123",
        },
    )

    assert "summarized:17" in result.stdout
    hooks = curl_json_bodies(curl_log, "/api/hooks/subscribe")
    assert hooks == []
    unsubs = curl_json_bodies(curl_log, "/api/hooks/unsubscribe")
    assert len(unsubs) == 1
    assert unsubs[0]["target_instance_id"] == "inst-terminal"
    assert unsubs[0]["subscriber_instance_id"] == "inst-terminal"
    assert unsubs[0]["purpose"] == "pr_step_plan"
    pr_body = curl_json_bodies(curl_log, "/api/instances/inst-terminal/pr")[0]
    assert pr_body["pr_state"] == "merged"


def test_force_review_with_empty_args_does_not_trip_nounset(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
parse_args --force review
pr_review_main() { printf 'argc=%s\\n' "$#"; }
run_force_mode
""",
        repo,
    )

    assert "argc=0" in result.stdout


def test_plan_followup_resolves_pane_from_ledger_without_instance_env(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        """
unset TOKEN_API_INSTANCE_ID TMUX_PANE TOKEN_API_DISPATCH_RESOLVED_PANE
export TOKEN_API_WRAPPER_ID=wrap-123
arm_pr_plan_followup review 17 https://github.com/owner/repo/pull/17 "plan fixes or next review action."
""",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "CURL_LEDGER_JSON": ledger_json("inst-123", "%ledger"),
        },
    )

    hooks = curl_json_bodies(curl_log, "/api/hooks/subscribe")
    assert len(hooks) == 1
    assert hooks[0]["target_pane"] == "%ledger"
    assert hooks[0]["payload"].startswith(
        "/plan PR #17 review returned "
        "(https://github.com/owner/repo/pull/17); plan fixes or next review action."
    )
    assert "CodeRabbit verdict:" in hooks[0]["payload"]


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


def test_plan_followup_payload_embeds_coderabbit_context(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    fake_bin, curl_log = install_fake_curl(tmp_path)

    bash_with_pr_step(
        """
repo_slug() { echo owner/repo; }
pr_head_sha() { echo headsha123456; }
coderabbit_state_for_head() { echo success; }
latest_coderabbit_review_state() { echo CHANGES_REQUESTED; }
changes_requested_count() { echo 1; }
checks_summary() { echo "  - unit / pytest: passing"; }
summarize_actionable_findings() { echo "  - cli-tools/bin/pr-step:42 — missing CR context"; }
arm_pr_plan_followup review 17 https://github.com/owner/repo/pull/17 "plan fixes or next review action."
""",
        repo,
        {
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "CURL_LOG": str(curl_log),
            "CURL_LEDGER_JSON": ledger_json("inst-123", "%ledger"),
            "TOKEN_API_WRAPPER_ID": "wrap-123",
        },
    )

    payload = curl_json_bodies(curl_log, "/api/hooks/subscribe")[0]["payload"]
    assert (
        "CodeRabbit verdict: commit=success; review=CHANGES_REQUESTED; changes_requested=1"
        in payload
    )
    assert "Checks:" in payload
    assert "unit / pytest: passing" in payload
    assert "Actionable CodeRabbit findings:" in payload
    assert "cli-tools/bin/pr-step:42" in payload


def test_review_timeout_rerequests_coderabbit_without_agent_visible_bounce(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
rerequests=0
coderabbit_rerequest_review() { rerequests=$((rerequests + 1)); printf 'body:%s\n' "$2"; return 0; }
TIMEOUT_REREQUESTS_DONE=0
if coderabbit_maybe_rerequest_on_timeout 17 TIMEOUT_REREQUESTS_DONE 1; then
  printf 'first=yes done=%s rerequests=%s\n' "$TIMEOUT_REREQUESTS_DONE" "$rerequests"
fi
if ! coderabbit_maybe_rerequest_on_timeout 17 TIMEOUT_REREQUESTS_DONE 1; then
  printf 'second=no done=%s rerequests=%s\n' "$TIMEOUT_REREQUESTS_DONE" "$rerequests"
fi
""",
        repo,
    )

    assert "first=yes done=1 rerequests=1" in result.stdout
    assert "second=no done=1 rerequests=1" in result.stdout
    combined = result.stdout + result.stderr
    assert "timed out with no fresh verdict; re-requesting" in combined
    assert "Check manually" not in combined
    assert "Possible reasons" not in combined


def test_timeout_prefers_rate_limit_reset_signal_before_rerequest(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
slept=0
rerequested=0
order=
coderabbit_latest_issue_comment_body() { echo 'Review limit reached. Your next review will be available in 18 minutes.'; }
coderabbit_latest_issue_comment_timestamp() { echo '1970-01-01T00:00:00Z'; }
sleep_until_rate_limit_reset() { slept=$1; order="${order}sleep "; return 0; }
rate_limit_reset_reached() { return 0; }
coderabbit_rerequest_review() { rerequested=$((rerequested + 1)); order="${order}rerequest"; return 0; }
if coderabbit_maybe_wait_rate_limit_on_timeout 17; then
  printf 'waited=%s rerequested=%s order=%s\n' "$slept" "$rerequested" "$order"
fi
""",
        repo,
    )

    assert "waited=1140 rerequested=1 order=sleep rerequest" in result.stdout


def test_rate_limit_reset_rerequest_consumes_retry_budget(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
rerequested=0
LAST_RATELIMIT_SIGNATURE=""
sleep_until_rate_limit_reset() { return 0; }
rate_limit_reset_reached() { return 0; }
coderabbit_rerequest_review() { rerequested=$((rerequested + 1)); return 0; }
done_count=0
body_one='Review limit reached. Your next review will be available in 1 second.'
body_two='Review limit reached. Your next review will be available in 2 seconds.'
coderabbit_handle_ratelimit_comment 17 "$body_one" '1970-01-01T00:00:00Z' done_count 1
if ! coderabbit_handle_ratelimit_comment 17 "$body_two" '1970-01-01T00:00:01Z' done_count 1; then
  printf 'blocked done=%s rerequested=%s\\n' "$done_count" "$rerequested"
fi
""",
        repo,
    )

    assert "blocked done=1 rerequested=1" in result.stdout


def test_coderabbit_heartbeat_is_opaque_when_requested(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    visible_heartbeat = tmp_path / "visible heartbeat.log"
    opaque_heartbeat = tmp_path / "opaque heartbeat.log"

    visible = bash_with_pr_step(
        f"""
PR_STEP_HEARTBEAT_FILE={shlex.quote(str(visible_heartbeat))}
emit_coderabbit_heartbeat 'CodeRabbit poll: visible'
echo visible-done
""",
        repo,
    )
    result = bash_with_pr_step(
        f"""
PR_STEP_OPAQUE_WAIT=true
PR_STEP_HEARTBEAT_FILE={shlex.quote(str(opaque_heartbeat))}
emit_coderabbit_heartbeat 'CodeRabbit poll: hidden'
echo done
""",
        repo,
    )

    assert "visible-done" in visible.stdout
    assert visible_heartbeat.exists()
    assert visible_heartbeat.read_text() != ""
    assert "done" in result.stdout
    assert not opaque_heartbeat.exists() or opaque_heartbeat.read_text() == ""


def test_fresh_current_head_verdict_skips_rerequest(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
current_pr_number() { echo 17; }
current_pr_state() { echo OPEN; }
current_pr_url() { echo https://github.com/owner/repo/pull/17; }
assert_repo() { :; }
mark_instance_status() { :; }
mark_pr_flag() { :; }
commit_if_needed() { return 1; }
push_branch() { :; }
checks_green() { return 0; }
review_pr_normal() { echo unexpected-rerequest; return 99; }
summarize_pr() { :; }
merge_pr_normal() { :; }
main --no-merge
""",
        repo,
    )

    assert "unexpected-rerequest" not in result.stdout
    assert "already green; skipping re-review" in result.stderr + result.stdout


def test_coderabbit_state_for_head_accepts_check_run_when_commit_status_absent(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
repo_slug() { echo owner/repo; }
gh() {
  if [[ "$*" == *"commits/headsha/statuses"* ]]; then
    printf '[]\n'
    return 0
  fi
  if [[ "$*" == *"commits/headsha/check-runs"* ]]; then
    cat <<'JSON'
{"check_runs":[{"name":"CodeRabbit / Review","status":"completed","conclusion":"success","completed_at":"2026-07-10T02:47:00Z","app":{"slug":"coderabbitai"}}]}
JSON
    return 0
  fi
  return 2
}
coderabbit_state_for_head headsha
""",
        repo,
    )

    assert result.stdout.strip() == "success"


def test_latest_coderabbit_issue_comment_helpers_paginate(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)

    result = bash_with_pr_step(
        """
repo_slug() { echo owner/repo; }
gh() {
  if [[ "$*" == *"issues/17/comments"* ]]; then
    [[ " $* " == *" --paginate "* ]] || {
      printf '%s\n' '[{"user":{"login":"coderabbitai[bot]"},"created_at":"2026-07-10T00:00:00Z","updated_at":"2026-07-10T00:00:00Z","body":"old"}]'
      return 0
    }
    cat <<'JSON'
[{"user":{"login":"coderabbitai[bot]"},"created_at":"2026-07-10T00:00:00Z","updated_at":"2026-07-10T00:00:00Z","body":"old"}]
[{"user":{"login":"coderabbitai[bot]"},"created_at":"2026-07-10T01:00:00Z","updated_at":"2026-07-10T01:30:00Z","body":"new"}]
JSON
    return 0
  fi
  return 2
}
printf 'body=%s\\n' "$(coderabbit_latest_issue_comment_body 17)"
printf 'stamp=%s\\n' "$(coderabbit_latest_issue_comment_timestamp 17)"
""",
        repo,
    )

    assert "body=new" in result.stdout
    assert "stamp=2026-07-10T01:30:00Z" in result.stdout
