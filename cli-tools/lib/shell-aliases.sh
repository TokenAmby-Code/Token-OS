#!/usr/bin/env bash
# shell-aliases.sh — Cross-platform aliases and functions for all Imperium machines
# TAGS: shell, aliases, shared, cross-platform
#
# Sourced by shell-init.sh for interactive shells only.
# Requires: $IMPERIUM, $TOKEN_OS, $CLI_TOOLS, $TOKEN_API_URL (from nas-path.sh)
#
# Machine-specific content stays in:
#   WSL: ~/.bash_aliases
#   Mac: ~/.zsh_aliases

# Agent binary front doors are PATH shims, not shell functions. Clear stale
# definitions before the double-source guard so reloading an existing shell makes
# `claude` and `codex` resolve directly to cli-tools/bin/{claude,codex}, which
# then exec agent-wrapper.sh.
# Transitional cleanup only: remove on sight after 2026-07-07 or after any tx
# restart, whichever happens first.
unalias claude codex 2>/dev/null || true
unset -f claude codex _claude_launch _codex_launch _resolve_claude_wrapper_bin 2>/dev/null || true

# Guard against double-sourcing
[[ -n "${_IMPERIUM_ALIASES_LOADED:-}" ]] && return 0 2>/dev/null
_IMPERIUM_ALIASES_LOADED=1

# =============================================================================
# Editor
# =============================================================================
export EDITOR=micro
export VISUAL=micro
alias e='micro'

# =============================================================================
# Python
# =============================================================================
alias python="python3"

# =============================================================================
# Git
# =============================================================================
alias gs="git status"
alias gcam="git commit -a -m"
alias gba="git branch -a"
alias gc="git checkout"
alias gpush="git push origin"
alias gpull="git pull origin"
alias gadd="git add ."
alias gcom="$TOKEN_OS/git/gcom-enhanced.sh"
alias gcom-t="$TOKEN_OS/git/gcom-enhanced.sh -t"
alias gcom-d="$TOKEN_OS/git/gcom-enhanced.sh -d"

# =============================================================================
# File & directory
# =============================================================================
alias lsg="ls | grep"
alias lsgr="ls -r | grep"
alias cp='cp -i'
alias mv='mv -i'
alias duh='du -h -d 1 | sort -h'

# =============================================================================
# Search helpers
# =============================================================================
grap() { grep -n -r "$1" "app/"; }
grdp() { grep -n -r "$1" "docs/"; }
fif() { grep -rn "$1" "${2:-.}"; }
psg() { ps aux | grep -v grep | grep -i "$1"; }

# =============================================================================
# Timestamps
# =============================================================================
alias now='date +"%Y-%m-%d %H:%M:%S"'
alias nowdate='date +"%Y-%m-%d"'
alias nowtime='date +"%H:%M:%S"'

# =============================================================================
# Colored man pages
# =============================================================================
export LESS_TERMCAP_mb=$'\e[1;32m'
export LESS_TERMCAP_md=$'\e[1;34m'
export LESS_TERMCAP_me=$'\e[0m'
export LESS_TERMCAP_so=$'\e[1;33;44m'
export LESS_TERMCAP_se=$'\e[0m'
export LESS_TERMCAP_us=$'\e[1;4;36m'
export LESS_TERMCAP_ue=$'\e[0m'
export GROFF_NO_SGR=1

# =============================================================================
# Utility functions
# =============================================================================

extract() {
    if [ -f "$1" ]; then
        case "$1" in
            *.tar.bz2)   tar xjf "$1"     ;;
            *.tar.gz)    tar xzf "$1"     ;;
            *.tar.xz)    tar xJf "$1"     ;;
            *.bz2)       bunzip2 "$1"     ;;
            *.rar)       unrar x "$1"     ;;
            *.gz)        gunzip "$1"      ;;
            *.tar)       tar xf "$1"      ;;
            *.tbz2)      tar xjf "$1"     ;;
            *.tgz)       tar xzf "$1"     ;;
            *.zip)       unzip "$1"       ;;
            *.Z)         uncompress "$1"  ;;
            *.7z)        7z x "$1"        ;;
            *)           echo "'$1' cannot be extracted via extract()" ;;
        esac
    else
        echo "'$1' is not a valid file"
    fi
}

peek() {
    local lines="${2:-10}"
    echo "=== HEAD ($lines lines) ==="
    head -n "$lines" "$1"
    echo ""
    echo "=== TAIL ($lines lines) ==="
    tail -n "$lines" "$1"
}

venv() {
    if [[ -d .venv ]]; then
        source .venv/bin/activate
    else
        echo "No .venv directory found in current directory"
    fi
}

# =============================================================================
# Token-API
# =============================================================================

headless() {
    local endpoint="${TOKEN_API_URL:-http://localhost:7777}/api/headless"
    case "$1" in
        -t) curl -sX POST "$endpoint" -H "Content-Type: application/json" -d '{"action":"toggle"}' | jq -r '"Toggled: " + (if .after.enabled then "enabled" else "disabled" end)' ;;
        -e) curl -sX POST "$endpoint" -H "Content-Type: application/json" -d '{"action":"enable"}' | jq -r '"Headless: " + .message' ;;
        -d) curl -sX POST "$endpoint" -H "Content-Type: application/json" -d '{"action":"disable"}' | jq -r '"Headless: " + .message' ;;
        *)  curl -s "$endpoint" | jq -r '"Headless mode: " + (if .enabled then "enabled" else "disabled" end) + " (since " + (.last_changed // "unknown") + ")"' ;;
    esac
}

api-ping() {
    local base="${TOKEN_API_URL:-http://localhost:7777}"
    if [ "$1" = "-g" ]; then
        if [ -n "$2" ]; then
            curl -X GET "$base/$2"
        else
            echo "Usage: api-ping -g <endpoint>"
            return 1
        fi
    elif [ -n "$1" ]; then
        curl -X POST "$base/$1"
    else
        echo "Usage: api-ping <endpoint> [-g <endpoint> for GET]"
        return 1
    fi
}

# =============================================================================
# Dispatch launcher helpers
# =============================================================================

_resolve_dispatch_bin() {
    local candidate=""

    candidate="$(command -v dispatch 2>/dev/null || true)"
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        echo "$candidate"
        return 0
    fi

    for candidate in \
        "${CLI_TOOLS:-}/bin/dispatch" \
        "${TOKEN_OS:-}/cli-tools/bin/dispatch" \
        "$HOME/runtimes/Token-OS/live/cli-tools/bin/dispatch" \
        "/home/token/runtimes/token-os/live/cli-tools/bin/dispatch"
    do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        echo "$candidate"
        return 0
    done

    return 1
}

# NOTE: The @PANE_CLEAN "clean-pane" stamp was tombstoned. It was built for a
# retired `c` command that branched on whether a pane was clean/dirty. After the
# pivot to `d` (always-launcher) and `c` (plain clear) it had no live consumer:
# its only reader was the `tmuxctl freelist` view, which now derives availability
# from the daemon occupancy ledger (instance/agent/singleton/boot-grace), not the
# stamp. `clear` is therefore left as the real binary — no wrapper, no stamp.

_dispatch_has_flag() {
    local flag="$1"
    shift
    local arg
    for arg in "$@"; do
        [[ "$arg" == "$flag" ]] && return 0
    done
    return 1
}

_dispatch_kind_is_aspirant() {
    local previous=""
    local arg
    for arg in "$@"; do
        if [[ "$previous" == "--kind" ]]; then
            [[ "$arg" == "aspirant" ]] && return 0
            previous=""
        fi
        case "$arg" in
            --kind=aspirant) return 0 ;;
            --kind) previous="--kind" ;;
        esac
    done
    return 1
}

_dispatch_has_target_spec() {
    local first=true
    local arg
    for arg in "$@"; do
        if $first; then
            case "$arg" in
                legion|mechanicus|civic) return 0 ;;
            esac
        fi
        first=false
        case "$arg" in
            --target|--pane|--target=*|--pane=*) return 0 ;;
            --) break ;;
        esac
    done
    return 1
}

_dispatch_human_surface() {
    local origin="$1"
    local do_clear="$2"
    shift 2

    local dispatch_bin=""
    dispatch_bin="$(_resolve_dispatch_bin)" || {
        echo "dispatch not found" >&2
        return 1
    }

    [[ "$do_clear" == "true" ]] && clear

    local -a args
    args=("$@")

    # Human launcher surfaces are canonical dispatch entrypoints. Always enter the
    # dispatch selector by default. Aspirant intake is explicit-only for now
    # because the Aspirant pipeline is not reliable enough to be the default.
    if ! _dispatch_has_target_spec "${args[@]}" && [[ -n "${TMUX_PANE:-${TMUX:-}}" ]]; then
        args=(--pane self "${args[@]}")
    fi
    if [[ -z "${TOKEN_API_DISPATCH_MENU_CONSUMED:-${DISPATCH_MENU_CONSUMED:-}}" ]] \
        && ! _dispatch_has_flag --interactive "${args[@]}"; then
        args=(--interactive "${args[@]}")
    fi
    if ! _dispatch_has_flag --aspirant "${args[@]}" \
        && { _dispatch_has_flag --aspirant-kind "${args[@]}" || _dispatch_kind_is_aspirant "${args[@]}"; }; then
        args=(--aspirant "${args[@]}")
    fi

    TOKEN_API_DISPATCH_ORIGIN="$origin" "$dispatch_bin" "${args[@]}"
}

# Clear any stale interactive launcher definitions from older sourced versions.
# This matters when reloading an existing shell after c/cc namespace changes.
unalias c cc d 2>/dev/null || true
unset -f cc d 2>/dev/null || true
# Drop the retired clean-pane `clear` wrapper if an older sourced version defined
# it, so `clear` resolves back to the real binary after a reload.
unset -f clear 2>/dev/null || true

# cdc — cd + clear + direct dispatch selector; bypasses directory selection
cdc() {
    if [[ $# -gt 0 && "$1" != -* ]]; then
        local dir="$1"
        shift
        cd "$dir" >/dev/null || return 1
    fi

    _dispatch_human_surface cdc true --dir "$PWD" "$@"
}

# d — direct dispatch selector
# Replaces cc as the human dispatch namespace. Clears the pane before opening the
# fzf selector so the menu always starts from a clean screen (matches cdc).
d() {
    _dispatch_human_surface d true "$@"
}

# c — clear only. Dispatch routing lives on d/cdc.
c() {
    clear
}
