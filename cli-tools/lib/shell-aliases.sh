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
# Claude Code — unified launcher
# =============================================================================
# Primarch dispatch: uses _primarch_launch (mac .zsh_aliases) or primarch binary (WSL cli-tools)
# Smart resume: queries token-api if session not found locally

claude() {
    local primarch=""
    local args=()
    local resume_id=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --primarch|-P)
                primarch="$2"
                shift 2
                ;;
            -r|--resume)
                args+=("$1")
                shift
                # Capture session ID if next arg isn't a flag
                if [[ $# -gt 0 && "$1" != -* ]]; then
                    resume_id="$1"
                    args+=("$1")
                    shift
                fi
                ;;
            *)
                args+=("$1")
                shift
                ;;
        esac
    done

    # Primarch dispatch — try inline launcher (mac), then CLI binary (WSL)
    if [[ -n "$primarch" ]]; then
        if type _primarch_launch &>/dev/null; then
            _primarch_launch "$primarch" "${args[@]}"
        elif command -v primarch &>/dev/null; then
            command primarch "$primarch" "${args[@]}"
        else
            echo "Primarch system not available" >&2
            return 1
        fi
        return
    fi

    # Smart resume: if session ID given but not found locally, query token-api
    if [[ -n "$resume_id" ]]; then
        local encoded_dir
        encoded_dir="$(pwd | sed 's|/|-|g')"
        local session_file="$HOME/.claude/projects/${encoded_dir}/${resume_id}.jsonl"
        if [[ ! -f "$session_file" ]]; then
            local api_url="${TOKEN_API_URL:-http://localhost:7777}"
            local instance
            instance=$(curl -sf "$api_url/api/instances/$resume_id" 2>/dev/null)
            if [[ -n "$instance" && "$instance" != *"not found"* && "$instance" != *"Not Found"* ]]; then
                local target_dir
                target_dir=$(echo "$instance" | python3 -c "import sys,json; print(json.load(sys.stdin).get('working_dir',''))" 2>/dev/null)
                if [[ -n "$target_dir" && -d "$target_dir" ]]; then
                    echo "Session not in $(pwd) — found in token-api"
                    echo "  cd $target_dir"
                    cd "$target_dir" || return 1
                fi
            fi
        fi
    fi

    # At $HOME with no meaningful args: open launcher if available
    if [[ "$PWD" == "$HOME" && ${#args[@]} -eq 0 ]] && command -v claude-launcher &>/dev/null; then
        claude-launcher
        return
    fi

    # Auto-cd to vault if launched from $HOME and NAS is available
    if [[ "$PWD" == "$HOME" && -n "${IMPERIUM:-}" && -d "$IMPERIUM/Imperium-ENV" ]]; then
        cd "$IMPERIUM/Imperium-ENV"
    fi

    clear && ~/.local/bin/claude --dangerously-skip-permissions "${args[@]}" 2> >(grep -v 'Overriding existing handler for signal' >&2)
}

# cdc — cd + clear + claude
cdc() {
    local dir="" primarch="" claude_args=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -r|--resume|--continue|--haiku)
                claude_args+=("$1"); shift ;;
            -p|--primarch)
                primarch="$2"; shift 2 ;;
            *)
                if [[ -z "$dir" ]]; then dir="$1"; else claude_args+=("$1"); fi
                shift ;;
        esac
    done
    [[ -n "$dir" ]] && { cd "$dir" || return 1; }
    clear
    if [[ -n "$primarch" ]]; then
        claude --primarch "$primarch" "${claude_args[@]}"
    else
        claude "${claude_args[@]}"
    fi
}

# cc — clear + claude (always routes args to claude)
cc() {
    clear
    claude "$@"
}

# c — smart toggle: clear if dirty, claude if already clear
# Args always passthrough to claude. From ~, opens launcher.
_c_cleared=true
c() {
    if [[ $# -gt 0 ]]; then
        claude "$@"
        return
    fi
    # At $HOME with no args: open interactive launcher
    if [[ "$PWD" == "$HOME" ]] && command -v claude-launcher &>/dev/null; then
        claude-launcher
        return
    fi
    if $_c_cleared; then
        _c_cleared=false
        claude
    else
        _c_cleared=true
        clear
    fi
}
