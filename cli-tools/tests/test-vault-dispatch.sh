#!/usr/bin/env bash
# test-vault-dispatch.sh — Regression tests for vault-dispatch hardening.
#
# Covers:
#   1. --context value with an embedded apostrophe survives the LAUNCH_CMD
#      quoting that drives the tempfile-staging step.
#   2. check_base_staleness fires warnings on a synthetic stale-base scenario
#      and refuses (non-zero exit) under --strict-base.
#
# Usage: bash cli-tools/tests/test-vault-dispatch.sh

set -u
TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$TESTS_DIR/../lib" && pwd)"

PASS=0
FAIL=0
FAILED_TESTS=()

_pass() { PASS=$((PASS + 1)); echo "PASS: $1"; }
_fail() { FAIL=$((FAIL + 1)); FAILED_TESTS+=("$1"); echo "FAIL: $1 — $2" >&2; }

# ---------------------------------------------------------------------------
# Test 1 — apostrophe in --context survives LAUNCH_CMD quoting
# ---------------------------------------------------------------------------
# Replicates the quoting strategy used in vault-dispatch after the chunk-#1
# fix: the dispatch prompt is %q-encoded before interpolation, instead of
# being wrapped in literal single quotes ('${DISPATCH_PROMPT}'). The legacy
# single-quote form breaks the moment the value contains an apostrophe.

test_apostrophe_quoting() {
    local context="John's bug — the apostrophe killed the round-1 cascade"
    local prompt="You are dispatched. Additional context: ${context}"

    # The fix: %q-encode the prompt before placing it in the launch line.
    local prompt_q
    prompt_q=$(printf '%q' "$prompt")
    local launch_cmd="claude --dangerously-skip-permissions ${prompt_q}"

    # Stage the way send_launch_cmd does (printf '%s\n' "$cmd" > tempfile).
    local stage
    stage=$(mktemp -t vault-dispatch-test.XXXXXX)
    # shellcheck disable=SC2064
    trap "rm -f '$stage'" RETURN
    printf '%s\n' "$launch_cmd" > "$stage"

    # bash -n must accept the staged file (no quoting error).
    if ! bash -n "$stage" 2>/dev/null; then
        _fail "apostrophe_quoting" "bash -n rejected staged LAUNCH_CMD"
        return
    fi

    # Demonstrate the bug-then-fix: the legacy form would have produced
    # syntactically broken output. Confirm by constructing it and asserting
    # bash -n rejects it.
    local broken_cmd="claude --dangerously-skip-permissions '${prompt}'"
    local broken_stage
    broken_stage=$(mktemp -t vault-dispatch-broken.XXXXXX)
    printf '%s\n' "$broken_cmd" > "$broken_stage"
    if bash -n "$broken_stage" 2>/dev/null; then
        rm -f "$broken_stage"
        _fail "apostrophe_quoting" "control: legacy single-quote form should have failed bash -n, but parsed"
        return
    fi
    rm -f "$broken_stage"

    # Round-trip: re-evaluate the fixed launch_cmd and confirm the final argv
    # equals the original prompt (verbatim, including the apostrophe).
    local -a argv=()
    eval "argv=($launch_cmd)"
    local last="${argv[${#argv[@]} - 1]}"
    if [[ "$last" != "$prompt" ]]; then
        _fail "apostrophe_quoting" "prompt mutated by quoting; got: $last"
        return
    fi

    _pass "apostrophe_quoting"
}

# ---------------------------------------------------------------------------
# Test 2 — check_base_staleness on a synthetic stale-base scenario
# ---------------------------------------------------------------------------
# Build a parent repo + worktree where the worktree's branch lags the parent
# HEAD by N commits, and the parent has uncommitted changes. Assert:
#   - warning mode returns 0 but logs the expected fields
#   - --strict-base returns 2

test_base_staleness() {
    # Silence the colorful log_info/log_warn/log_error from vault-dispatch
    # so test output stays parseable. We define stubs *before* sourcing the
    # lib, and the lib will only define fallbacks if these don't exist.
    local LOG_FILE
    LOG_FILE=$(mktemp -t base-staleness-log.XXXXXX)
    # shellcheck disable=SC2317
    log_info()  { echo "INFO: $*" >> "$LOG_FILE"; }
    # shellcheck disable=SC2317
    log_warn()  { echo "WARN: $*" >> "$LOG_FILE"; }
    # shellcheck disable=SC2317
    log_error() { echo "ERROR: $*" >> "$LOG_FILE"; }
    export -f log_info log_warn log_error

    # shellcheck source=../lib/base-staleness.sh
    if ! source "$LIB_DIR/base-staleness.sh"; then
        _fail "base_staleness" "could not source base-staleness.sh"
        rm -f "$LOG_FILE"
        return
    fi

    local sandbox parent_repo worktree_dir
    sandbox=$(mktemp -d -t base-staleness-test.XXXXXX)
    parent_repo="$sandbox/parent"
    worktree_dir="$sandbox/worktree"
    # shellcheck disable=SC2064
    trap "rm -rf '$sandbox'; rm -f '$LOG_FILE'" RETURN

    (
        set -e
        git init -q -b main "$parent_repo"
        cd "$parent_repo"
        git config user.email test@example.com
        git config user.name test
        echo "v0" > file.txt
        git add file.txt
        git commit -q -m "v0"

        # Create a stale branch at v0
        git branch stale-feature

        # Advance main with two more commits (these are what stale-feature lacks)
        echo "v1" > file.txt
        git commit -qam "v1 — add target function"
        echo "v2" > file.txt
        git commit -qam "v2 — more target work"

        # Worktree off stale-feature: this is what gets dispatched
        git worktree add -q "$worktree_dir" stale-feature

        # Add uncommitted noise to parent
        echo "wip" > dirty1.txt
        echo "wip" > dirty2.txt
    ) || { _fail "base_staleness" "scenario setup failed"; return; }

    # 1. Warning mode — should return 0
    : > "$LOG_FILE"
    if ! check_base_staleness "$worktree_dir" "false"; then
        _fail "base_staleness" "warning mode returned non-zero"
        return
    fi
    if ! grep -q "commits_behind=2" "$LOG_FILE"; then
        _fail "base_staleness" "expected commits_behind=2 in log; got: $(cat "$LOG_FILE")"
        return
    fi
    if ! grep -q "dirty_files=2" "$LOG_FILE"; then
        _fail "base_staleness" "expected dirty_files=2 in log; got: $(cat "$LOG_FILE")"
        return
    fi
    if ! grep -q "WARN.*stale-feature.*2 commit" "$LOG_FILE"; then
        _fail "base_staleness" "expected stale-branch warning; got: $(cat "$LOG_FILE")"
        return
    fi
    if ! grep -q "WARN.*uncommitted file" "$LOG_FILE"; then
        _fail "base_staleness" "expected dirty-parent warning; got: $(cat "$LOG_FILE")"
        return
    fi

    # 2. Strict mode — should refuse with rc=2
    : > "$LOG_FILE"
    local rc=0
    check_base_staleness "$worktree_dir" "true" || rc=$?
    if (( rc != 2 )); then
        _fail "base_staleness" "strict mode expected rc=2, got rc=$rc; log: $(cat "$LOG_FILE")"
        return
    fi
    if ! grep -q "refusing to dispatch" "$LOG_FILE"; then
        _fail "base_staleness" "expected refusal message; got: $(cat "$LOG_FILE")"
        return
    fi

    # 3. Clean scenario — fast-forward stale-feature to parent HEAD and drop the dirt
    (
        set -e
        cd "$parent_repo"
        rm -f dirty1.txt dirty2.txt
        # Fast-forward stale-feature (checked out in the worktree) to main HEAD.
        # Use update-ref so we don't fight the worktree's checked-out branch lock.
        local main_sha
        main_sha=$(git rev-parse main)
        cd "$worktree_dir"
        git reset -q --hard "$main_sha"
    ) || { _fail "base_staleness" "clean scenario setup failed"; return; }

    : > "$LOG_FILE"
    if ! check_base_staleness "$worktree_dir" "true"; then
        _fail "base_staleness" "strict mode on clean scenario should pass; log: $(cat "$LOG_FILE")"
        return
    fi
    if ! grep -q "commits_behind=0" "$LOG_FILE"; then
        _fail "base_staleness" "clean: expected commits_behind=0; got: $(cat "$LOG_FILE")"
        return
    fi

    _pass "base_staleness"
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
test_apostrophe_quoting
test_base_staleness

echo ""
echo "Results: $PASS passed, $FAIL failed"
if (( FAIL > 0 )); then
    printf '  - %s\n' "${FAILED_TESTS[@]}" >&2
    exit 1
fi
exit 0
