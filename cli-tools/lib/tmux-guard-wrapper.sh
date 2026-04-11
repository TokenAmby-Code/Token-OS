#!/usr/bin/env bash
# tmux-guard-wrapper.sh — Shell function override that guards ALL tmux send-keys calls
# TAGS: tmux, guard, send-keys, safety, wrapper
#
# Source this file from your shell profile (.bashrc / .zshrc) AFTER nas-path.sh.
# It defines a tmux() function that intercepts `send-keys` (and `send`) subcommands,
# checks for pending user input in the target pane, and aborts if the user is typing.
#
# All other tmux subcommands pass through to the real binary unmodified.
#
# Opt-out: TMUX_GUARD_SKIP=1 bypasses the guard for a single invocation.
# Timeout: TMUX_GUARD_TIMEOUT=N seconds to wait for clear (default: 10).
#
# Usage:
#   source /path/to/cli-tools/lib/tmux-guard-wrapper.sh
#   tmux send-keys -t %5 "hello" Enter    # guarded automatically
#   TMUX_GUARD_SKIP=1 tmux send-keys ...  # bypass guard
#   tmux list-sessions                     # passes through, no guard

# Resolve the real tmux binary once at source time.
# Use `command` builtin to bypass our function and find the actual binary.
# Idempotent — skip if already loaded with a valid path
if [[ -n "${TMUX_GUARD_REAL_TMUX:-}" && "${TMUX_GUARD_REAL_TMUX}" == /* ]]; then
    return 0 2>/dev/null || true
fi

# Resolve real binary. `command -v` in zsh may return just the name,
# so try whence -p (zsh) first, then command -v (bash), then known paths.
TMUX_GUARD_REAL_TMUX="$(whence -p tmux 2>/dev/null || command -v tmux 2>/dev/null)"
# Validate we got an absolute path, not just "tmux"
if [[ "$TMUX_GUARD_REAL_TMUX" != /* ]]; then
    # Fall back to known locations
    for _p in /opt/homebrew/bin/tmux /usr/local/bin/tmux /usr/bin/tmux; do
        [[ -x "$_p" ]] && TMUX_GUARD_REAL_TMUX="$_p" && break
    done
    unset _p
fi

# Bail out if tmux isn't installed
if [[ -z "$TMUX_GUARD_REAL_TMUX" || "$TMUX_GUARD_REAL_TMUX" != /* ]]; then
    return 0 2>/dev/null || true
fi

# Source the detection logic from the existing tmux-guard.sh.
# We need tmux_pane_has_input() and tmux_wait_for_clear().
# Resolve the lib path relative to this file.
_tmux_guard_wrapper_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
if [[ -f "$_tmux_guard_wrapper_dir/tmux-guard.sh" ]]; then
    source "$_tmux_guard_wrapper_dir/tmux-guard.sh"
fi
unset _tmux_guard_wrapper_dir

# The wrapper function. Overrides `tmux` in shell scope.
tmux() {
    local real="$TMUX_GUARD_REAL_TMUX"

    # Fast path: no args or not send-keys — pass through immediately
    if [[ $# -eq 0 ]]; then
        "$real" "$@"
        return
    fi

    # Detect if the subcommand is send-keys or send
    local subcmd="$1"
    case "$subcmd" in
        send-keys|send-key|send)
            ;;  # Fall through to guard logic
        *)
            # Not send-keys — pass through unmodified
            "$real" "$@"
            return
            ;;
    esac

    # --- Guard logic for send-keys ---

    # Opt-out check
    if [[ "${TMUX_GUARD_SKIP:-}" == "1" ]]; then
        "$real" "$@"
        return
    fi

    # Extract -t TARGET from the arguments
    local pane=""
    local args=("$@")
    for ((i = 1; i < ${#args[@]}; i++)); do
        if [[ "${args[$i]}" == "-t" ]] && (( i + 1 < ${#args[@]} )); then
            pane="${args[$((i + 1))]}"
            break
        fi
        # Handle -tTARGET (no space)
        if [[ "${args[$i]}" == -t* ]] && [[ "${args[$i]}" != "-t" ]]; then
            pane="${args[$i]#-t}"
            break
        fi
    done

    # If no -t specified, use current pane
    if [[ -z "$pane" ]]; then
        pane="${TMUX_PANE:-}"
    fi

    # If we can't identify the pane, pass through without guarding
    if [[ -z "$pane" ]]; then
        "$real" "$@"
        return
    fi

    # Check if tmux_wait_for_clear is available (from tmux-guard.sh)
    if ! type tmux_wait_for_clear &>/dev/null; then
        # Guard functions not available — pass through
        "$real" "$@"
        return
    fi

    local timeout="${TMUX_GUARD_TIMEOUT:-10}"

    if ! tmux_wait_for_clear "$pane" "$timeout"; then
        echo "tmux-guard: BLOCKED send-keys to $pane — user has pending input (waited ${timeout}s)" >&2
        return 1
    fi

    "$real" "$@"
}
