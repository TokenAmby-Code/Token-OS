#!/usr/bin/env zsh
# shell-aliases-zsh.sh — Zsh-specific interactive additions
# TAGS: shell, zsh, aliases
# Sourced by shell-init.sh after shell-aliases.sh (interactive only)

# History search with arrow keys (type partial, then Up/Down)
bindkey '^[[A' history-search-backward
bindkey '^[[B' history-search-forward

# source with no args = reload profile
source() {
    if [[ $# -eq 0 ]]; then
        builtin source ~/.zshrc
    else
        builtin source "$@"
    fi
}

autoload -Uz add-zsh-hook

# Agent exit cleanup: hooks stage /tmp/agent-resume-${TMUX_PANE}; the next shell
# prompt returns to ~, clears the terminal, and records the resume command
# directly; dispatch resolves cwd from Token-API.
_agent_resume_precmd() {
    local pane="${TMUX_PANE:-}"
    [[ -z "$pane" ]] && return

    local f="/tmp/agent-resume-${pane}"
    local legacy="/tmp/claude-resume-${pane}"
    local cmd=""

    if [[ -f "$f" ]]; then
        cmd="$(sed -n '3p' "$f" 2>/dev/null)"
        if [[ -z "$cmd" ]]; then
            cmd="$(sed -n '2p' "$f" 2>/dev/null)"
        fi
        rm -f "$f"
    elif [[ -f "$legacy" ]]; then
        cmd="$(cat "$legacy" 2>/dev/null)"
        rm -f "$legacy"
    else
        return
    fi

    cd ~ 2>/dev/null || true
    clear
    [[ -z "$cmd" ]] && return
    print -s "$cmd"
}
add-zsh-hook precmd _agent_resume_precmd
