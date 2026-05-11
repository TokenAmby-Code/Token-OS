#!/usr/bin/env bash
# base-staleness.sh — Detect stale-base conditions before dispatch.
#
# Exposes:
#   check_base_staleness <working_dir> [strict=true|false]
#
# Returns 0 if the dispatch is safe (or only warned), non-zero if strict mode
# refuses the dispatch.
#
# Two checks:
#   1. Source vs parent HEAD: count commits in the parent repo's HEAD not
#      reachable from the worktree's source branch. If > 0, the worktree may
#      be missing recent changes (e.g., a target function added on main).
#   2. Dirty parent: count uncommitted files in the parent repo. The worktree
#      won't see them, so any dispatch that depends on those files will operate
#      on stale state.
#
# Both checks fire warnings by default. When the second argument is "true",
# either condition triggers a non-zero return.

# Fallback logging stubs — vault-dispatch defines its own colorful versions;
# tests / library users get plain stderr lines if those aren't loaded.
if ! declare -F log_info >/dev/null 2>&1; then
    log_info()    { echo "> $*" >&2; }
fi
if ! declare -F log_warn >/dev/null 2>&1; then
    log_warn()    { echo "! $*" >&2; }
fi
if ! declare -F log_error >/dev/null 2>&1; then
    log_error()   { echo "x $*" >&2; }
fi

check_base_staleness() {
    local working_dir="$1"
    local strict="${2:-false}"

    [[ -d "$working_dir" ]] || return 0

    # Skip if working_dir isn't a git repo
    if ! git -C "$working_dir" rev-parse --git-dir >/dev/null 2>&1; then
        return 0
    fi

    local source_branch
    source_branch=$(git -C "$working_dir" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "HEAD")

    # Resolve the main worktree path via git-common-dir
    local common_dir main_repo
    common_dir=$(git -C "$working_dir" rev-parse --git-common-dir 2>/dev/null || echo "")
    if [[ -z "$common_dir" ]]; then
        return 0
    fi
    if [[ "$common_dir" != /* ]]; then
        common_dir=$(cd "$working_dir" && cd "$common_dir" && pwd)
    fi
    if [[ "$common_dir" == */.git ]]; then
        main_repo="${common_dir%/.git}"
    else
        main_repo="$working_dir"
    fi
    [[ -d "$main_repo" ]] || return 0

    # 1. Commits behind: HEAD of main_repo not reachable from source_branch
    local commits_behind=0
    if [[ "$source_branch" != "HEAD" ]] \
       && git -C "$main_repo" rev-parse --verify "$source_branch" >/dev/null 2>&1; then
        commits_behind=$(git -C "$main_repo" rev-list --count "${source_branch}..HEAD" 2>/dev/null || echo 0)
    fi

    # 2. Uncommitted changes in main_repo
    local porcelain dirty_count=0 dirty_summary="none"
    porcelain=$(git -C "$main_repo" status --porcelain 2>/dev/null || true)
    if [[ -n "$porcelain" ]]; then
        dirty_count=$(printf '%s\n' "$porcelain" | wc -l | tr -d ' ')
        # porcelain format: "XY path" — strip the 2-char status + space
        dirty_summary=$(printf '%s\n' "$porcelain" \
            | awk '{ print substr($0, 4) }' \
            | head -5 \
            | paste -sd ',' -)
        if (( dirty_count > 5 )); then
            dirty_summary+=",...(+$((dirty_count - 5)))"
        fi
    fi

    log_info "vault-dispatch: base check — base_branch=${source_branch}, commits_behind=${commits_behind}, dirty_files=${dirty_count}, dirty_target_files=${dirty_summary}"

    local stale=false
    if (( commits_behind > 0 )); then
        log_warn "Source branch '${source_branch}' is ${commits_behind} commit(s) behind ${main_repo} HEAD — worktree may be missing recent changes"
        stale=true
    fi
    if (( dirty_count > 0 )); then
        log_warn "Parent repo ${main_repo} has ${dirty_count} uncommitted file(s); worktree at ${working_dir} won't see them"
        stale=true
    fi

    if $stale && [[ "$strict" == "true" ]]; then
        log_error "--strict-base: refusing to dispatch with stale base"
        return 2
    fi

    return 0
}
