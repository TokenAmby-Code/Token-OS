#!/usr/bin/env bash
# Behavioral-pin regression: CodeRabbit rate-limit deferrals do not block a
# green PR, while actual findings on the current head still do.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PR_STEP_SOURCE_ONLY=1 source "$ROOT/cli-tools/bin/pr-step"

log_file="$(mktemp)"
trap 'rm -f "$log_file"' EXIT

pr_head_sha() { echo head123; }
coderabbit_state_for_head() { echo failure; }
changes_requested_count() { echo 0; }
coderabbit_review_is_deferred() { return 0; }
non_coderabbit_required_checks_green() { return 0; }
coderabbit_has_actionable_findings() { return 1; }
current_pr_number() { echo 42; }
current_pr_url() { echo https://example.test/pr/42; }
current_pr_state() { echo OPEN; }
commit_if_needed() { return 1; }
push_branch() { :; }
review_pr_normal() { :; }
summarize_pr() { :; }
mark_instance_status() { :; }
mark_pr_flag() { :; }
disarm_pr_plan_followup() { :; }
merge_pr_normal() { echo merged >>"$log_file"; }
gh() {
    case "$*" in
        'pr view 42 --json state -q .state') echo OPEN ;;
        'pr view 42 --json mergeable -q .mergeable') echo MERGEABLE ;;
        *) return 1 ;;
    esac
}

output="$(main 2>&1)"
grep -qx merged "$log_file"
grep -q 'Skipping CodeRabbit review because it is rate-limited/unavailable' <<<"$output"

: >"$log_file"
coderabbit_has_actionable_findings() { return 0; }
output="$(main 2>&1)"
[[ ! -s "$log_file" ]]
grep -q 'not green yet' <<<"$output"

echo 'PASS: rate-limit skip merges; current-head findings still block'
