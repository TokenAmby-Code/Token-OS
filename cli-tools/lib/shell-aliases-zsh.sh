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
# prompt returns to ~, clears the terminal, and records a "cd back && resume"
# command in zsh history.
_agent_resume_precmd() {
    local pane="${TMUX_PANE:-}"
    [[ -z "$pane" ]] && return

    local f="/tmp/agent-resume-${pane}"
    local legacy="/tmp/claude-resume-${pane}"
    local cmd=""
    local old_pwd=""

    if [[ -f "$f" ]]; then
        old_pwd="$(sed -n '2p' "$f" 2>/dev/null)"
        cmd="$(sed -n '3p' "$f" 2>/dev/null)"
        if [[ -z "$cmd" ]]; then
            cmd="$old_pwd"
            old_pwd=""
        fi
        rm -f "$f"
    elif [[ -f "$legacy" ]]; then
        cmd="$(cat "$legacy" 2>/dev/null)"
        rm -f "$legacy"
    else
        return
    fi

    [[ -z "$old_pwd" ]] && old_pwd="$PWD"
    cd ~ 2>/dev/null || true
    clear
    [[ -z "$cmd" ]] && return
    local staged_cmd
    staged_cmd="cd $(printf '%q' "$old_pwd") && $cmd"
    print -s "$staged_cmd"
}
add-zsh-hook precmd _agent_resume_precmd
