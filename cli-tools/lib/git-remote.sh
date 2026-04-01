#!/usr/bin/env bash
# git-remote.sh — Auto-detect the GitHub remote name
#
# In standard clones, the GitHub remote is "origin". In NAS-backed worktree
# setups, "origin" points to the bare repo on the NAS and "github" is the
# actual GitHub remote. This lib detects which remote points to GitHub and
# exports it as GIT_GITHUB_REMOTE.
#
# Usage in shell scripts:
#   source "$(dirname "$(readlink -f "$0")")/../lib/git-remote.sh"
#   git push "$GIT_GITHUB_REMOTE" "$branch"
#
# Exports:
#   GIT_GITHUB_REMOTE — Name of the remote that points to GitHub (e.g. "origin" or "github")

# Skip if already resolved (idempotent sourcing)
if [[ -n "${GIT_GITHUB_REMOTE:-}" ]]; then
    return 0 2>/dev/null || true
fi

# Detect GitHub remote by scanning `git remote -v` for github.com URLs.
# Falls back to "origin" if no GitHub remote found (safe default).
_detect_github_remote() {
    local remote_name=""

    # Look for a remote whose fetch URL contains github.com
    while IFS=$'\t' read -r name url _type; do
        if [[ "$url" == *github.com* ]]; then
            remote_name="$name"
            break
        fi
    done < <(git remote -v 2>/dev/null)

    echo "${remote_name:-origin}"
}

export GIT_GITHUB_REMOTE="$(_detect_github_remote)"
