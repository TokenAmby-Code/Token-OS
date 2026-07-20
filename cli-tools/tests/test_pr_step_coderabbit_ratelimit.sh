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

# --- Rate-limit-only blockage takes the force-merge path automatically ------
# Fixture shape: Token-Fleet PR #96 (2026-07-20). Repo has no required checks,
# so `gh pr checks --required` fails and checks_green cannot go green even
# though the deferral was detected. The only blocker is the rate-limit, so
# pr-step must auto-merge via its own force-merge route instead of refusing.
: >"$log_file"
coderabbit_has_actionable_findings() { return 1; }
non_coderabbit_required_checks_green() { return 1; }
non_coderabbit_checks_green() { return 0; }
output="$(main 2>&1)"
grep -qx merged "$log_file"
grep -q 'AUTO-FORCE-MERGE' <<<"$output"

# Fixture shape: Terminus-OS PR #9 (2026-07-20). The latest CodeRabbit comment
# is a "starting full review" reply — not a rate-limit deferral — but the
# CodeRabbit commit status itself went red with "Review rate limited". The
# head-signal detection must classify this as a rate-limit, not a real failure.
: >"$log_file"
coderabbit_review_is_deferred() { return 1; }
coderabbit_head_signal_is_ratelimit() { [[ "$1" == "head123" ]]; }
output="$(main 2>&1)"
grep -qx merged "$log_file"
grep -q 'AUTO-FORCE-MERGE' <<<"$output"

# A red CodeRabbit signal that is NOT a rate-limit stays blocking.
: >"$log_file"
coderabbit_head_signal_is_ratelimit() { return 1; }
output="$(main 2>&1)"
[[ ! -s "$log_file" ]]
grep -q 'not green yet' <<<"$output"

# A failing non-CodeRabbit check stays blocking even during a rate-limit.
: >"$log_file"
coderabbit_review_is_deferred() { return 0; }
non_coderabbit_checks_green() { return 1; }
output="$(main 2>&1)"
[[ ! -s "$log_file" ]]
grep -q 'not green yet' <<<"$output"

# A CHANGES_REQUESTED review stays blocking even during a rate-limit.
: >"$log_file"
non_coderabbit_checks_green() { return 0; }
changes_requested_count() { echo 1; }
output="$(main 2>&1)"
[[ ! -s "$log_file" ]]
grep -q 'not green yet' <<<"$output"
changes_requested_count() { echo 0; }

# --- Detection helpers against real API fixtures ----------------------------
# coderabbit_head_signal_is_ratelimit: commit status red with rate-limit text
# (Token-Fleet PR #96 / Terminus-OS PR #9 statuses) is a rate-limit; red with
# other text is not; a newest pending status is not.
unset -f coderabbit_head_signal_is_ratelimit
PR_STEP_SOURCE_ONLY=1 source "$ROOT/cli-tools/bin/pr-step"
repo_slug() { echo owner/repo; }
gh() {
    case "$*" in
        'api --paginate repos/owner/repo/commits/head123/statuses')
            printf '%s' "$STATUSES_JSON" ;;
        'api --paginate repos/owner/repo/commits/head123/check-runs')
            printf '%s' '{"check_runs":[]}' ;;
        *) return 1 ;;
    esac
}
STATUSES_JSON='[{"context":"CodeRabbit","state":"failure","description":"Review rate limited","updated_at":"2026-07-20T22:11:06Z"},{"context":"CodeRabbit","state":"pending","description":"Review in progress","updated_at":"2026-07-20T22:05:39Z"}]'
coderabbit_head_signal_is_ratelimit head123
STATUSES_JSON='[{"context":"CodeRabbit","state":"failure","description":"Review failed","updated_at":"2026-07-20T22:11:06Z"}]'
! coderabbit_head_signal_is_ratelimit head123
STATUSES_JSON='[{"context":"CodeRabbit","state":"pending","description":"Review in progress","updated_at":"2026-07-20T22:11:06Z"},{"context":"CodeRabbit","state":"failure","description":"Review rate limited","updated_at":"2026-07-20T22:05:39Z"}]'
! coderabbit_head_signal_is_ratelimit head123

# non_coderabbit_checks_green: "no checks" is green (the --required variant
# fails outright on repos without required checks — the PR #96 refusal); a
# CodeRabbit-only red check list is green; a failing non-CodeRabbit check is not.
gh() {
    case "$*" in
        'pr checks 42 --json name,bucket,workflow')
            printf '%s\n' "$CHECKS_OUT"; return "${CHECKS_RC:-0}" ;;
        *) return 1 ;;
    esac
}
CHECKS_OUT="no checks reported on the 'fix/branch' branch" CHECKS_RC=1
non_coderabbit_checks_green 42
CHECKS_OUT='[{"name":"CodeRabbit","bucket":"fail","workflow":""}]' CHECKS_RC=1
non_coderabbit_checks_green 42
CHECKS_OUT='[{"name":"CodeRabbit","bucket":"fail","workflow":""},{"name":"tests","bucket":"fail","workflow":"CI"}]' CHECKS_RC=1
! non_coderabbit_checks_green 42
CHECKS_OUT='[{"name":"CodeRabbit","bucket":"fail","workflow":""},{"name":"tests","bucket":"pass","workflow":"CI"}]' CHECKS_RC=1
non_coderabbit_checks_green 42

echo 'PASS: rate-limit skip merges; current-head findings still block; rate-limit-only blockage force-merges'
