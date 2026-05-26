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
TMUX_GUARD_RESOLVER_BIN="$_tmux_guard_wrapper_dir/../bin/tmux-resolve-pane"
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

    _tmux_guard_focus_log() {
        local event="$1" target="${2:-}" command_name="${3:-}" previous client ts
        ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
        previous="$("$real" display-message -p '#{pane_id}' 2>/dev/null || true)"
        client="$("$real" display-message -p '#{client_tty}' 2>/dev/null || true)"
        local line
        line="$(printf '%s event=%s action=%s attempted_target=%q previous_pane=%q current_client=%q command_surface=tmux-function command=%q' \
            "$ts" "$event" "$event" "$target" "$previous" "$client" "$command_name")"
        printf '%s\n' "$line" >> /tmp/mechanicus-focus-guard.log 2>/dev/null || true
        printf '%s\n' "$line" >> /tmp/tmux-focus-guard.log 2>/dev/null || true
    }

    _tmux_guard_override_active() {
        local raw now
        raw="$("$real" show-options -gqv @IMPERIUM_ALLOW_MECHANICUS_FOCUS_UNTIL 2>/dev/null || true)"
        [[ -n "$raw" ]] || return 1
        now="$(python3 - <<'PY'
import time
print(time.time())
PY
)"
        python3 - "$raw" "$now" <<'PY'
import sys
try:
    sys.exit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)
except Exception:
    sys.exit(1)
PY
    }

    _tmux_guard_automation_focus_active() {
        [[ "${IMPERIUM_TMUX_FOCUS_RESTORE:-}" != "1" ]] || return 1
        [[ "${IMPERIUM_ALLOW_TMUX_FOCUS:-}" != "1" ]] || return 1
        [[ "${IMPERIUM_ALLOW_MECHANICUS_FOCUS:-}" != "1" ]] || return 1
        ! _tmux_guard_override_active
    }

    _tmux_guard_automation_env_active() {
        [[ -n "${IMPERIUM_TMUX_AUTOMATION:-}" || -n "${TOKEN_API_INTERNAL_DISPATCH:-}" ]]
    }

    _tmux_guard_open_override_from_env() {
        local target="$1" command_name="$2" until
        [[ "${IMPERIUM_ALLOW_MECHANICUS_FOCUS:-}" == "1" ]] || return 1
        _tmux_guard_target_is_mechanicus "$target" || return 1
        until="$(python3 - <<'PY'
import time
print(f"{time.time() + 4:.3f}")
PY
)"
        "$real" set-option -g @IMPERIUM_ALLOW_MECHANICUS_FOCUS_UNTIL "$until" 2>/dev/null || true
        "$real" set-option -g @IMPERIUM_ALLOW_MECHANICUS_FOCUS_REASON "env:tmux-function:${command_name}" 2>/dev/null || true
        _tmux_guard_focus_log "allowed" "$target" "$command_name"
        return 0
    }

    _tmux_guard_extract_target() {
        local flag="$1"; shift
        local arg
        while [[ $# -gt 0 ]]; do
            arg="$1"
            if [[ "$arg" == "$flag" && $# -gt 1 ]]; then
                printf '%s' "$2"
                return 0
            fi
            if [[ "$arg" == "$flag"* && "$arg" != "$flag" ]]; then
                printf '%s' "${arg#"$flag"}"
                return 0
            fi
            shift
        done
        return 1
    }

    _tmux_guard_target_is_mechanicus() {
        local target="$1" name
        [[ -n "$target" ]] || return 1
        [[ "$target" == mechanicus:* ]] && return 0
        name="${target##*:}"
        name="${name%%.*}"
        [[ "$name" == mechanicus* ]] && return 0
        name="$("$real" display-message -t "$target" -p '#{window_name}' 2>/dev/null || true)"
        name="${name%%\(*}"
        [[ "$name" == mechanicus* ]]
    }

    _tmux_guard_blocks_focus() {
        _tmux_guard_automation_focus_active || return 1
        case "${1:-}" in
            select-window|switch-client)
                local target
                target="$(_tmux_guard_extract_target -t "$@" 2>/dev/null || true)"
                _tmux_guard_automation_env_active && return 0
                [[ -n "$target" ]] || return 1
                _tmux_guard_target_is_mechanicus "$target"
                ;;
            select-pane)
                local arg target
                for arg in "${@:2}"; do
                    [[ "$arg" == "-P" || "$arg" == "-T" ]] && return 1
                done
                target="$(_tmux_guard_extract_target -t "$@" 2>/dev/null || true)"
                _tmux_guard_automation_env_active && return 0
                [[ -n "$target" ]] || return 1
                _tmux_guard_target_is_mechanicus "$target"
                ;;
            split-window|new-window)
                local arg target
                for arg in "${@:2}"; do
                    [[ "$arg" == "-d" || "$arg" == -*d* ]] && return 1
                done
                target="$(_tmux_guard_extract_target -t "$@" 2>/dev/null || true)"
                _tmux_guard_automation_env_active && return 0
                [[ -n "$target" ]] || return 1
                _tmux_guard_target_is_mechanicus "$target"
                ;;
            *) return 1 ;;
        esac
    }

    _tmux_guard_maybe_open_override() {
        case "${1:-}" in
            select-window|switch-client)
                local target
                target="$(_tmux_guard_extract_target -t "$@")" || return 1
                _tmux_guard_open_override_from_env "$target" "$1"
                ;;
            select-pane)
                local arg target
                for arg in "${@:2}"; do
                    [[ "$arg" == "-P" || "$arg" == "-T" ]] && return 1
                done
                target="$(_tmux_guard_extract_target -t "$@")" || return 1
                _tmux_guard_open_override_from_env "$target" "$1"
                ;;
            split-window|new-window)
                local arg target
                for arg in "${@:2}"; do
                    [[ "$arg" == "-d" || "$arg" == -*d* ]] && return 1
                done
                target="$(_tmux_guard_extract_target -t "$@")" || return 1
                _tmux_guard_open_override_from_env "$target" "$1"
                ;;
            *) return 1 ;;
        esac
    }

    _tmux_guard_maybe_open_override "$@" || true

    if _tmux_guard_blocks_focus "$@"; then
        local target
        target="$(_tmux_guard_extract_target -t "$@" 2>/dev/null || true)"
        _tmux_guard_focus_log "wrapper-blocked" "$target" "${1:-}"
        return 0
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

    # Resolve Imperium stable pane ids (1:N, palace:N, legion:custodes) once,
    # then guard and execute against the live %pane id. This keeps the shell
    # function override compatible with the bin/tmux shim.
    local resolver_bin="${TMUX_GUARD_RESOLVER_BIN:-}"
    if [[ -x "$resolver_bin" ]]; then
        local resolved_pane
        if resolved_pane=$("$resolver_bin" --format physical "$pane" 2>/dev/null) && [[ -n "$resolved_pane" ]]; then
            pane="$resolved_pane"
            for ((i = 1; i < ${#args[@]}; i++)); do
                if [[ "${args[$i]}" == "-t" ]] && (( i + 1 < ${#args[@]} )); then
                    args[$((i + 1))]="$pane"
                    set -- "${args[@]}"
                    break
                fi
                if [[ "${args[$i]}" == -t* ]] && [[ "${args[$i]}" != "-t" ]]; then
                    args[$i]="-t${pane}"
                    set -- "${args[@]}"
                    break
                fi
            done
        fi
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
