#!/usr/bin/env bash
# Shared, lossless preflight for deploy-owned Git runtimes.
# Source this immediately before any checkout/reset that can replace a runtime.

runtime_shunt_dirty() {
    local runtime="$1" bare="$2" repository="$3" target_sha="$4" machine="${5:-$(hostname -s)}"
    local receipt_dir="${RUNTIME_RECOVERY_RECEIPT_DIR:-$bare/runtime-recovery-receipts}"
    local old_sha timestamp base branch suffix=0 recovery_sha receipt tmp

    [[ -n "$(git -C "$runtime" status --porcelain --untracked-files=all 2>/dev/null)" ]] || return 0
    old_sha="$(git -C "$runtime" rev-parse HEAD)" || return 1
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    base="wip/live-dirty-${timestamp}-$$"
    branch="$base"
    while git --git-dir="$bare" show-ref --verify --quiet "refs/heads/$branch"; do
        suffix=$((suffix + 1)); branch="${base}-${suffix}"
    done

    echo "runtime dirty; preserving lossless recovery branch $branch" >&2
    # Creating a branch at the runtime HEAD retains both index and worktree.
    git -C "$runtime" checkout -b "$branch" >/dev/null || return 1
    git -C "$runtime" add -A || return 1
    git -C "$runtime" -c user.name=token-cd -c user.email=cd@token-os.local \
        commit -m "wip(runtime): preserve dirty deploy checkout $timestamp" >/dev/null || return 1
    recovery_sha="$(git -C "$runtime" rev-parse HEAD)" || return 1
    git -C "$runtime" push "$bare" "refs/heads/$branch:refs/heads/$branch" >/dev/null || return 1
    [[ "$(git --git-dir="$bare" rev-parse "refs/heads/$branch")" == "$recovery_sha" ]] || return 1

    # A receipt is outside the runtime and is written atomically only after the
    # branch has been durably verified in the local CD bare cache.
    mkdir -p "$receipt_dir" || return 1
    receipt="$receipt_dir/${repository}-${timestamp}-$$.json"
    tmp="${receipt}.tmp"
    cat >"$tmp" <<EOF
{"machine":"$machine","repository":"$repository","old_sha":"$old_sha","target_sha":"$target_sha","recovery_branch":"$branch","recovery_commit":"$recovery_sha","timestamp":"$timestamp"}
EOF
    mv "$tmp" "$receipt" || return 1
    [[ -s "$receipt" ]] || return 1
    # Keep the recovery branch anchored while callers detach/reset to target.
    git -C "$runtime" checkout --detach "$recovery_sha" >/dev/null || return 1
    RUNTIME_DIRTY_SHUNT_RECEIPT="$receipt"
    export RUNTIME_DIRTY_SHUNT_RECEIPT
    echo "runtime preserved: branch=$branch commit=$recovery_sha receipt=$receipt" >&2
}
