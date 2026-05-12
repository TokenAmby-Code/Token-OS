#!/usr/bin/env bash
# shell-aliases-bash.sh — Bash-specific interactive additions
# TAGS: shell, bash, aliases
# Sourced by shell-init.sh after shell-aliases.sh (interactive only)

# History search with arrow keys (type partial, then Up/Down)
bind '"\e[A": history-search-backward' 2>/dev/null
bind '"\e[B": history-search-forward' 2>/dev/null

# source with no args = reload profile
source() {
    if [[ $# -eq 0 ]]; then
        builtin source ~/.bashrc
    else
        builtin source "$@"
    fi
}

# c() reset hook — any non-c command resets the clear/claude toggle
_reset_c_cleared() { [[ "$BASH_COMMAND" != "c" && "$BASH_COMMAND" != "c " ]] && _c_cleared=false; }
trap '_reset_c_cleared' DEBUG

# Agent exit cleanup: hooks stage /tmp/agent-resume-${TMUX_PANE}; the next shell
# prompt clears the terminal and records the resume command in bash history.
_agent_resume_prompt_command() {
    local pane="${TMUX_PANE:-}"
    [[ -z "$pane" ]] && return

    local f="/tmp/agent-resume-${pane}"
    local legacy="/tmp/claude-resume-${pane}"
    local cmd=""

    if [[ -f "$f" ]]; then
        cmd="$(sed -n '2p' "$f" 2>/dev/null)"
        rm -f "$f"
    elif [[ -f "$legacy" ]]; then
        cmd="$(cat "$legacy" 2>/dev/null)"
        rm -f "$legacy"
    else
        return
    fi

    clear
    [[ -z "$cmd" ]] && return
    history -s "$cmd"
}

case ";${PROMPT_COMMAND:-};" in
    *";_agent_resume_prompt_command;"*) ;;
    *) PROMPT_COMMAND="_agent_resume_prompt_command${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
