#!/usr/bin/env bash
# worktree-park.sh — reconciler invariant: no agent worktree stays on main.
# TAGS: git, worktree, cd, reconciler
# AUDIENCE: agent
#
# Sourced by the CD reconciler (token-restart sync_bare_main) and by pr-merge's
# bare-main fast-forward path. Defines one function, no new infra.
#
#   park_worktrees_off_main <bare_or_common_git_dir>
#
# Why this exists (incident 2026-06-22): a worktree checked out *on* branch
# `main` (or `master`) — typically an agent that ran `git checkout main` inside
# a feature worktree — jams the CD/pr-merge bare-main fast-forward. Git refuses
# to advance a branch ref that is checked out in a linked worktree:
#
#   fatal: refusing to fetch into branch 'refs/heads/main' checked out at '<wt>'
#
# The fix is to PARK the offending worktree off main: detach it in place to its
# current SHA. Detaching to the same commit changes no files, so any uncommitted
# edits are preserved (recoverable from the worktree, or shunted to wip/ by the
# one-time `worktree-hygiene` tool). With main no longer pinned by a worktree,
# the bare ref fast-forwards normally.
#
# Invariants:
#   - Only worktrees whose checked-out branch is exactly main/master are touched.
#   - Already-detached worktrees (the deploy-owned live runtime is detached HEAD
#     by design) emit a `detached` porcelain line, never a `branch` line, so they
#     are NEVER matched — the runtime is untouched.
#   - The bare repo itself emits `bare`, also never matched.
#   - Best-effort per worktree: one failed detach (gone dir, locked index) warns
#     and the sweep continues; the function always returns 0 so it can run inside
#     a deploy under `set +e`/`set -e` without aborting CD.

# Detach every linked worktree that is checked out on main/master, in place, at
# its current SHA. Echoes one "parked …" line per worktree parked (stdout).
park_worktrees_off_main() {
    local git_dir="${1:-}"
    [[ -n "$git_dir" ]] || return 0

    local line cur_path="" ref branch sha
    # `git worktree list --porcelain` emits, per worktree, a `worktree <path>`
    # line followed by exactly one of `branch <ref>`, `detached`, or `bare`.
    while IFS= read -r line; do
        case "$line" in
            "worktree "*)
                cur_path="${line#worktree }"
                ;;
            "branch "*)
                ref="${line#branch }"          # refs/heads/<name>
                branch="${ref#refs/heads/}"
                if [[ "$branch" == "main" || "$branch" == "master" ]]; then
                    sha="$(git -C "$cur_path" rev-parse --short HEAD 2>/dev/null || echo '?')"
                    if git -C "$cur_path" checkout --detach >/dev/null 2>&1; then
                        echo "parked $cur_path (was on $branch, detached at $sha)"
                    else
                        echo "worktree-park: WARN could not park $cur_path off $branch" >&2
                    fi
                fi
                ;;
        esac
    done < <(git --git-dir="$git_dir" worktree list --porcelain 2>/dev/null)

    return 0
}
