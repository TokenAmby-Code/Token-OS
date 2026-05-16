#!/usr/bin/env bash
# shell-aliases-bash.sh — Bash-specific interactive additions
# TAGS: shell, bash, aliases
# Sourced by shell-init.sh after shell-aliases.sh (interactive only)

# History search with arrow keys (type partial, then Up/Down)
bind '"\e[A": history-search-backward' 2>/dev/null
bind '"\e[B": history-search-forward' 2>/dev/null

# Convenience: `reload` re-sources ~/.bashrc.
# Why not override `source`? A function-wrapped source forces every sourced
# file's `declare`/`local`/`typeset` into the function's scope, so any
# `declare -A FOO=(...)` becomes function-local and vanishes on return —
# silently breaking config files like ~/.bash_cd. Keep `source` as the builtin.
alias reload='builtin source ~/.bashrc'


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
