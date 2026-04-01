# mobile-shell-init — Dirty bit tracking for mobile tmux panes
# Sourced automatically by tmux-workspace in mobile window panes
# Works in both zsh (Mac) and bash (WSL)
#
# Dirty bit: window name gets '*' appended when any command runs.
# Removed only when ALL panes in the window are cleared.

_mobile_just_cleared=true  # Skip the first precmd (from sourcing this file)

# Mark window dirty after each command
_mobile_mark_dirty() {
    if $_mobile_just_cleared; then
        _mobile_just_cleared=false
        return
    fi
    tmux set-option -p @PANE_CLEAN false 2>/dev/null
    local win
    win=$(tmux display-message -p '#{window_name}' 2>/dev/null) || return
    [[ "$win" == *'*' ]] || tmux rename-window "${win}*" 2>/dev/null
}

# Clear terminal, mark pane clean, check if all panes are clean
_mobile_clear() {
    command clear
    _mobile_just_cleared=true
    tmux set-option -p @PANE_CLEAN true 2>/dev/null
    local all_clean=true
    local target
    target="$(tmux display-message -p '#{session_name}:#{window_index}' 2>/dev/null)"
    while IFS= read -r state; do
        [[ "$state" == "true" ]] || { all_clean=false; break; }
    done < <(tmux list-panes -t "$target" -F '#{@PANE_CLEAN}' 2>/dev/null)
    if $all_clean; then
        local win
        win=$(tmux display-message -p '#{window_name}' 2>/dev/null) || return
        tmux rename-window "${win%\*}" 2>/dev/null
    fi
}

alias clear='_mobile_clear'

# Shell-specific hook registration (driven by machine config shell key)
_imperium_shell="${IMPERIUM_MACHINE_SHELL:-$(imperium_cfg shell 2>/dev/null)}"
if [[ "$_imperium_shell" == "zsh" ]]; then
    precmd_functions+=(_mobile_mark_dirty)
    _mobile_clear_widget() { _mobile_clear; zle reset-prompt; }
    zle -N _mobile_clear_widget
    bindkey '^L' _mobile_clear_widget
else
    # bash (WSL, phone, linux)
    PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND;}_mobile_mark_dirty"
    bind -x '"\C-l": _mobile_clear' 2>/dev/null
fi
unset _imperium_shell
