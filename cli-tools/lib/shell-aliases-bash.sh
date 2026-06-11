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


# --- Agent exit cleanup + clean-pane stamp lifecycle (bash parity) ----------
# Mirrors shell-aliases-zsh.sh: the resume sentinel and the @PANE_CLEAN stamp
# share one consume primitive so the late-landing sentinel can never wipe a
# command the user has already run. @PANE_CLEAN is set by clear()
# (shell-aliases.sh) and dropped on the first command (DEBUG trap) or ^C
# (INT trap).

# Read + consume (delete) the pane-scoped resume sentinel. Echoes the resume
# command (possibly empty); returns 0 if a sentinel was present, 1 if none.
_agent_resume_consume() {
    local pane="${TMUX_PANE:-}"
    [[ -z "$pane" ]] && return 1

    local f="/tmp/agent-resume-${pane}"
    local legacy="/tmp/claude-resume-${pane}"
    local cmd=""

    if [[ -f "$f" ]]; then
        cmd="$(sed -n '3p' "$f" 2>/dev/null)"
        [[ -z "$cmd" ]] && cmd="$(sed -n '2p' "$f" 2>/dev/null)"
        rm -f "$f"
    elif [[ -f "$legacy" ]]; then
        cmd="$(cat "$legacy" 2>/dev/null)"
        rm -f "$legacy"
    else
        return 1
    fi

    printf '%s\n' "$cmd"
    return 0
}

# Drop the clean stamp AND cancel any pending post-agent auto-reset. Cancelling
# means the late `cd ~; clear` can never wipe the user's first command; the
# resume command is still recorded in history.
_agent_pane_dirty() {
    _pane_drop_clean
    local cmd
    cmd="$(_agent_resume_consume)" || return
    [[ -z "$cmd" ]] && return
    history -s "$cmd"
}

# PROMPT_COMMAND piece: auto-reset ONLY when a sentinel is present AND untouched,
# then arm the DEBUG-trap preexec for the next user command.
_pane_clean_preexec_armed=""
_agent_resume_prompt_command() {
    local cmd
    if cmd="$(_agent_resume_consume)"; then
        cd ~ 2>/dev/null || true
        clear
        [[ -n "$cmd" ]] && history -s "$cmd"
    fi
    _pane_clean_preexec_armed=1
}

# DEBUG trap = bash preexec: fires before each command. When armed (first command
# since the prompt was drawn) and the command isn't the prompt machinery, dirty
# + cancel, then disarm until the next prompt.
_agent_clean_debug_trap() {
    [[ -n "$_pane_clean_preexec_armed" ]] || return
    case "$BASH_COMMAND" in
        _agent_resume_prompt_command*|_agent_clean_debug_trap*|_agent_clean_int_trap*) return ;;
    esac
    _pane_clean_preexec_armed=""
    _agent_pane_dirty
}
trap '_agent_clean_debug_trap' DEBUG

# ^C dirties a partial line too (likelier than backspacing the whole line).
_agent_clean_int_trap() {
    _pane_clean_preexec_armed=""
    _agent_pane_dirty
}
trap '_agent_clean_int_trap' INT

case ";${PROMPT_COMMAND:-};" in
    *";_agent_resume_prompt_command;"*) ;;
    *) PROMPT_COMMAND="_agent_resume_prompt_command${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
esac
