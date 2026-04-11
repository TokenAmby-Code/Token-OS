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

# c() reset hook — any non-c command resets the clear/claude toggle
_reset_c_cleared() { [[ "$1" != "c" ]] && _c_cleared=false; }
autoload -Uz add-zsh-hook
add-zsh-hook preexec _reset_c_cleared
