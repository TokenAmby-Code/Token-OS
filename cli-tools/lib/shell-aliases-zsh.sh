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

# --- Agent exit cleanup + clean-pane stamp lifecycle ------------------------
# Agent exit hooks stage /tmp/agent-resume-${TMUX_PANE}. The clean stamp
# (@PANE_CLEAN) is set by clear() (shell-aliases.sh) and dropped here on the
# first command or ^C. The two concerns share a single consume primitive so the
# late-landing resume sentinel can NEVER wipe a command the user has already run.

# A sentinel is only trusted when it is a regular, non-symlinked file owned by us.
# The path lives in shared /tmp, so a symlink or another user's file at the same
# name would otherwise let a local attacker inject into our history or wedge a
# fake sentinel in place. Untrusted files are treated as absent (never read,
# never removed). (Full hardening — moving the namespace into a per-user runtime
# dir — also needs the writer in agent-session-end-resume.sh and is tracked
# separately; this is the consumer-side guard.)
_agent_resume_trusted() {
    local f="$1"
    [[ -f "$f" && ! -h "$f" && -O "$f" ]]
}

# Read + consume (delete) the pane-scoped resume sentinel. Echoes the resume
# command (possibly empty) on stdout; returns 0 if a sentinel was present, 1 if
# none. Deleting the file is what *cancels* a pending auto-reset.
_agent_resume_consume() {
    local pane="${TMUX_PANE:-}"
    [[ -z "$pane" ]] && return 1

    local f="/tmp/agent-resume-${pane}"
    local legacy="/tmp/claude-resume-${pane}"
    local cmd=""

    if _agent_resume_trusted "$f"; then
        cmd="$(sed -n '3p' "$f" 2>/dev/null)"
        [[ -z "$cmd" ]] && cmd="$(sed -n '2p' "$f" 2>/dev/null)"
        rm -f "$f"
    elif _agent_resume_trusted "$legacy"; then
        cmd="$(cat "$legacy" 2>/dev/null)"
        rm -f "$legacy"
    else
        return 1
    fi

    print -r -- "$cmd"
    return 0
}

# precmd: auto-reset ONLY when a sentinel is present AND untouched (the user has
# not acted yet). Returns to ~, clears (→ stamps clean), records the resume
# command in history. precmd fires before the user can type, so the common
# typed-`claude`/`codex`-then-exit path resets here race-free. If the sentinel
# lands late, the preexec/^C path below consumes it on the user's first action.
_agent_resume_precmd() {
    local cmd
    cmd="$(_agent_resume_consume)" || return
    cd ~ 2>/dev/null || true
    clear
    [[ -z "$cmd" ]] && return
    print -s "$cmd"
}
add-zsh-hook precmd _agent_resume_precmd

# Drop the clean stamp AND cancel any pending post-agent auto-reset. Called from
# preexec (first command) and the ^C widget (first interrupt). Consuming the
# sentinel here means the late `cd ~; clear` can never wipe what the user just
# did — `cd o` lands in the vault with `ls` visible. The resume command is still
# recorded in history so up-arrow still resumes.
_agent_pane_dirty() {
    _pane_drop_clean
    local cmd
    cmd="$(_agent_resume_consume)" || return
    [[ -z "$cmd" ]] && return
    print -s "$cmd"
}

# preexec runs on the first real command — dirty the pane + cancel the reset.
_agent_clean_preexec() {
    _agent_pane_dirty
}
add-zsh-hook preexec _agent_clean_preexec

# ^C on a partial line dirties too (the user is likelier to ^C than to backspace
# a whole line). A ZLE widget intercepts ^C at the prompt: dirty + cancel, then
# perform the standard interrupt (abort the line via send-break). Guarded so a
# ZLE quirk can never wedge the prompt — if anything fails, fall through to the
# break. The keystroke-then-backspace edge (visually clear but still stamped
# clean) is accepted.
_agent_clean_interrupt() {
    _agent_pane_dirty 2>/dev/null
    zle send-break
}
zle -N _agent_clean_interrupt
bindkey '^C' _agent_clean_interrupt
