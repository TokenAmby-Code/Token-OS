#!/usr/bin/env bash
# worktree-resolve.sh — resolve the linked worktree that holds a given branch.
#
# pr-merge must remove the worktree belonging to the PR's *head branch*, not the
# caller's current working directory. The original code set
# `WORKTREE_PATH="$REPO_ROOT"` (cwd) and removed that, while deriving the branch
# to delete from the PR itself — so running `pr-merge <N>` from an unrelated
# worktree merged the right branch but removed the WRONG worktree (the caller's),
# orphaning the PR's real worktree.
#
# This helper closes that gap: given a branch name it walks
# `git worktree list --porcelain` and echoes the worktree path checked out on
# refs/heads/<branch>. The bare repo itself (`bare` line) and detached worktrees
# such as the deploy-owned live runtime (`detached` line) carry no `branch` line
# and are never matched — they cannot be a PR's head-branch worktree.

# resolve_worktree_for_branch <branch> [git_dir]
#   Echoes the worktree path checked out on <branch>, or nothing when the branch
#   is not checked out in any linked worktree. Matching is exact (refs/heads/
#   stripped), so 'feat' never matches 'feature-x'. Always returns 0 — absence is
#   communicated by empty output, not exit status. [git_dir] is optional and used
#   mainly for testing against a throwaway bare repo; omit it to resolve against
#   the repository containing the cwd.
resolve_worktree_for_branch() {
    local want_branch="${1:-}"
    local git_dir="${2:-}"
    [[ -n "$want_branch" ]] || return 0

    local -a git_cmd=(git)
    [[ -n "$git_dir" ]] && git_cmd=(git --git-dir="$git_dir")

    # `git worktree list --porcelain` emits, per worktree, a `worktree <path>`
    # line followed by exactly one of `branch <ref>`, `detached`, or `bare`.
    local line cur_path="" ref branch
    while IFS= read -r line; do
        case "$line" in
            "worktree "*)
                cur_path="${line#worktree }"
                ;;
            "branch "*)
                ref="${line#branch }"          # refs/heads/<name>
                branch="${ref#refs/heads/}"
                if [[ "$branch" == "$want_branch" ]]; then
                    printf '%s\n' "$cur_path"
                    return 0
                fi
                ;;
        esac
    done < <("${git_cmd[@]}" worktree list --porcelain 2>/dev/null)

    return 0
}
