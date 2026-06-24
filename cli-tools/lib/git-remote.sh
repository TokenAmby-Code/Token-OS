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

_GIT_REMOTE_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${IMPERIUM:-}" && -f "$_GIT_REMOTE_LIB_DIR/nas-path.sh" ]]; then
    # shellcheck disable=SC1091
    source "$_GIT_REMOTE_LIB_DIR/nas-path.sh" 2>/dev/null || true
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

if [[ -z "${GIT_GITHUB_REMOTE:-}" ]]; then
    export GIT_GITHUB_REMOTE="$(_detect_github_remote)"
fi

token_os_github_remote() {
    local url="${1%.git}"
    case "$url" in
        git@github.com:TokenAmby-Code/Token-OS|\
        ssh://git@github.com/TokenAmby-Code/Token-OS|\
        https://github.com/TokenAmby-Code/Token-OS)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

dead_or_quarantined_remote() {
    local url="${1%/}" imperium_root="${IMPERIUM:-}" token_os_bare=""
    if [[ -z "$imperium_root" ]] && type imperium_cfg >/dev/null 2>&1; then
        imperium_root="$(imperium_cfg nas_imperium 2>/dev/null || true)"
    fi
    if [[ -n "$imperium_root" ]]; then
        token_os_bare="${imperium_root%/}/token-os.git"
    fi
    case "$url" in
        ""|*'#recycle'*)
            return 0
            ;;
    esac
    if [[ -n "$token_os_bare" ]]; then
        [[ "$url" == "$token_os_bare" || "$url" == "file://$token_os_bare" ]] && return 0
    fi
    return 1
}
