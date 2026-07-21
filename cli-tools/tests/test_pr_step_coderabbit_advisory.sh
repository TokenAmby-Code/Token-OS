#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PR_STEP_SOURCE_ONLY=1 source "$ROOT/cli-tools/bin/pr-step"

pr_head_sha() { echo deadbeef; }
current_pr_url() { echo https://example.test/pull/42; }
coderabbit_state_for_head() { echo "${CR_STATE:-pending}"; }
latest_coderabbit_review_state() { echo "${CR_REVIEW:-}"; }
changes_requested_count() { echo "${CR_CHANGES:-0}"; }
coderabbit_review_is_deferred() { [[ "${CR_DEFERRED:-false}" == true ]]; }
summarize_actionable_findings() { printf '%s' "${CR_FINDINGS:-}"; }
checks_summary() { echo "  - ci success"; }
non_coderabbit_checks_green() { [[ "${CI_GREEN:-true}" == true ]]; }
gh() {
    case "$*" in
        "pr view 42 --json state -q .state") echo OPEN ;;
        "pr view 42 --json mergeable -q .mergeable") echo MERGEABLE ;;
        "pr checks 42 --watch=false --fail-fast=false") [[ "${CI_GREEN:-true}" == true ]] ;;
        *) return 1 ;;
    esac
}

fail() { echo "FAIL: $*" >&2; exit 1; }

coderabbit_only_run_failure '{"jobs":[{"steps":[{"name":"Lint","conclusion":"success"},{"name":"CodeRabbit PR gate","conclusion":"failure"}]}]}' \
    || fail "aggregate check with only CodeRabbit failure must be advisory"
if coderabbit_only_run_failure '{"jobs":[{"steps":[{"name":"Lint","conclusion":"failure"},{"name":"CodeRabbit PR gate","conclusion":"failure"}]}]}'; then
    fail "real CI failure must remain blocking"
fi

CI_GREEN=true CR_STATE=pending CR_DEFERRED=true CR_CHANGES=1
checks_green 42 || fail "CI green + CodeRabbit rate-limited must be mergeable"
[[ "$(coderabbit_advisory_summary 42)" == *"rate-limited"* ]] || fail "rate-limit advisory missing"

CI_GREEN=false CR_STATE=success CR_DEFERRED=false CR_CHANGES=0
if checks_green 42; then fail "CI red must not be mergeable"; fi

CI_GREEN=true CR_STATE=success CR_REVIEW=CHANGES_REQUESTED CR_CHANGES=1 CR_FINDINGS='  - fix the edge case'
checks_green 42 || fail "completed CodeRabbit findings must not block green CI"
summary="$(coderabbit_advisory_summary 42)"
[[ "$summary" == *"completed with findings"* ]] || fail "completed findings advisory missing"
[[ "$summary" == *"fix the edge case"* ]] || fail "finding summary missing"

assert_repo() { :; }
mark_instance_status() { :; }
mark_pr_flag() { :; }
current_pr_number() { echo 42; }
current_pr_state() { echo OPEN; }
commit_if_needed() { return 1; }
push_branch() { :; }
summarize_pr() { coderabbit_advisory_summary "$1" >/dev/null; }
disarm_pr_plan_followup() { :; }
MERGES=0
merge_pr_normal() { MERGES=$((MERGES + 1)); }

CI_GREEN=true CR_STATE=pending CR_DEFERRED=true CR_CHANGES=1 CR_FINDINGS=
main
[[ "$MERGES" == 1 ]] || fail "main did not merge green CI while CodeRabbit was rate-limited"

MERGES=0 CI_GREEN=false CR_STATE=success CR_DEFERRED=false CR_CHANGES=0
main
[[ "$MERGES" == 0 ]] || fail "main merged while CI was red"

MERGES=0 CI_GREEN=true CR_STATE=success CR_REVIEW=CHANGES_REQUESTED CR_CHANGES=1 CR_FINDINGS='  - fix the edge case'
main
[[ "$MERGES" == 1 ]] || fail "main did not merge green CI with completed CodeRabbit findings"

echo "PASS: CodeRabbit is advisory and CI remains the merge gate"
