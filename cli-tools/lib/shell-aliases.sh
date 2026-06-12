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
# Smart resume: queries token-api if session not found locally

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
        "${IMPERIUM:-}/runtimes/token-os/live/cli-tools/bin/dispatch" \
        "/Volumes/Imperium/runtimes/token-os/live/cli-tools/bin/dispatch" \
        "/mnt/imperium/runtimes/token-os/live/cli-tools/bin/dispatch"
    do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        echo "$candidate"
        return 0
    done

    return 1
}

_resolve_claude_wrapper_bin() {
    local candidate=""

    candidate="$(command -v claude-wrapper.sh 2>/dev/null || true)"
    if [[ -n "$candidate" && -x "$candidate" ]]; then
        echo "$candidate"
        return 0
    fi

    for candidate in \
        "${CLI_TOOLS:-}/scripts/claude-wrapper.sh" \
        "${TOKEN_OS:-}/cli-tools/scripts/claude-wrapper.sh" \
        "$HOME/runtimes/Token-OS/live/cli-tools/scripts/claude-wrapper.sh" \
        "${IMPERIUM:-}/runtimes/token-os/live/cli-tools/scripts/claude-wrapper.sh" \
        "/Volumes/Imperium/runtimes/token-os/live/cli-tools/scripts/claude-wrapper.sh" \
        "/mnt/imperium/runtimes/token-os/live/cli-tools/scripts/claude-wrapper.sh"
    do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        echo "$candidate"
        return 0
    done

    return 1
}

# =============================================================================
# Clean-pane stamp (@PANE_CLEAN)
# =============================================================================
# @PANE_CLEAN=1 is a pure *descriptive* per-pane tmux option meaning "this pane
# is a clean, idle shell". It is set by clear() and dropped on the first command
# or ^C (see shell-aliases-{zsh,bash}.sh). It NEVER intercepts a command — it is
# semantics only, never control flow. The stamps ARE the source of truth;
# `tmuxctl freelist` derives a live view over them (no second registry).
#
# The stamp does NOT drive pane-border rendering — hostname nametags are killed
# globally in tmux-base.conf regardless of clean state.
_pane_stamp_clean() {
    [[ -n "${TMUX_PANE:-}" ]] || return 0
    command -v tmux >/dev/null 2>&1 || return 0
    tmux set-option -p -t "${TMUX_PANE}" @PANE_CLEAN 1 2>/dev/null || true
}

_pane_drop_clean() {
    [[ -n "${TMUX_PANE:-}" ]] || return 0
    command -v tmux >/dev/null 2>&1 || return 0
    tmux set-option -p -u -t "${TMUX_PANE}" @PANE_CLEAN 2>/dev/null || true
}

# Wrap `clear` so every clear stamps the pane clean. `command clear` runs the
# real binary; the stamp follows. c() calls this, so it inherits the stamp; the
# post-agent reset (precmd / _agent_post_exit_reset) does too.
clear() {
    command clear "$@"
    _pane_stamp_clean
}

# Post-agent reset, run synchronously in-shell the instant the agent wrapper
# returns (typed-`claude`/`codex` path). Clears + stamps clean immediately so a
# freshly-closed pane is visually reset without waiting on the racy resume
# sentinel. The resume command is dropped into history by the prompt hook
# (_agent_resume_precmd fires before the user can type; if the sentinel lands
# late, the preexec/^C cancel path prints it on the user's first action). Either
# way the pending auto-reset can never wipe a command the user has already run.
_agent_post_exit_reset() {
    clear
}

_codex_launch() {
    local dispatch_bin=""
    dispatch_bin="$(_resolve_dispatch_bin)" || {
        echo "dispatch not found" >&2
        return 1
    }

    local rc=0
    clear
    if [[ $# -gt 0 ]]; then
        "$dispatch_bin" --engine codex --dir "$PWD" --prompt "$*"
    else
        "$dispatch_bin" --engine codex --dir "$PWD"
    fi
    rc=$?
    # Only reset on a clean exit — a failed launch must keep its error output
    # on screen and propagate its exit code, not be cleared into a fresh prompt.
    [[ $rc -eq 0 ]] && _agent_post_exit_reset
    return "$rc"
}

_claude_launch() {
    local claude_wrapper_bin=""
    claude_wrapper_bin="$(_resolve_claude_wrapper_bin)" || {
        echo "claude-wrapper.sh not found" >&2
        return 1
    }

    local rc=0
    clear
    TOKEN_API_LAUNCHER="${TOKEN_API_LAUNCHER:-shell-aliases}" \
    TOKEN_API_ENGINE="${TOKEN_API_ENGINE:-claude}" \
    "$claude_wrapper_bin" --dangerously-skip-permissions "$@"
    rc=$?
    # Only reset on a clean exit — preserve a failed wrapper's error + exit code.
    [[ $rc -eq 0 ]] && _agent_post_exit_reset
    return "$rc"
}

claude() {
    local primarch=""
    local args=()
    local resume_id=""
    local use_codex=false

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --codex)
                use_codex=true
                shift
                ;;
            --primarch|-P)
                echo "claude --primarch is deprecated; use dispatch --persona <name>" >&2
                return 64
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

    if $use_codex; then
        _codex_launch "${args[@]}"
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

    _claude_launch "${args[@]}"
}

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
# Replaces cc as the human dispatch namespace.
d() {
    _dispatch_human_surface d false "$@"
}

# c — clear only. Dispatch routing lives on d/cdc.
c() {
    clear
}
