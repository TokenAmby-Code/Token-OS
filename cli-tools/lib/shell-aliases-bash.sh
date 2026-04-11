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
