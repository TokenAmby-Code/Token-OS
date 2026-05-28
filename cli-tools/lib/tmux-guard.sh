#!/usr/bin/env bash
# tmux-guard.sh — Typing guard for tmux send-keys injection
# TAGS: tmux, guard, send-keys, safety
#
# Prevents send-keys injection when the user has pending input in a pane.
# Source this file and use tmux_send_guarded instead of raw tmux send-keys.
#
# Usage:
#   source "$(dirname "$(readlink -f "$0")")/../lib/tmux-guard.sh"
#   tmux_send_guarded -t "$pane" -l "some text"
#   tmux_send_guarded -t "$pane" Enter
#
# Functions:
#   tmux_pane_has_input PANE  — returns 0 if user has pending input, 1 if clear
#   tmux_wait_for_clear PANE [TIMEOUT]  — blocks until pane is clear or timeout
#   tmux_send_guarded [send-keys args]  — waits for clear, then sends

# Check if a pane has pending user input on the current prompt line.
# Returns 0 (true) if input detected, 1 (false) if clear.
#
# Detection strategy:
#   1. Capture the last line of the pane (the prompt/input line)
#   2. Strip trailing whitespace
#   3. Check if the line has content after common prompt markers:
#      - Shell prompts: ends with $ % # > followed by user text
#      - Claude Code: the input line has a > prefix with text after it
#   4. If the stripped last line is empty or is just a prompt marker, it's clear
#
# Edge cases handled:
#   - Claude Code processing (spinner visible) → cursor line is output, not prompt → clear
#   - Empty shell prompt (just $) → clear
#   - User mid-type ($ git comm) → has input
tmux_pane_has_input() {
    local pane="$1"

    # Capture just the cursor line (last line with content), filtering out
    # Claude Code / Codex status chrome that confuses the prompt-marker heuristic:
    #   "  4% 38k/1.0M $0.19"             — Claude Code context % footer (the `%` was a false-positive prompt marker)
    #   "  ⏵⏵ bypass permissions ..."      — Claude Code hint line
    #   "  esc again to edit previous ..." — Codex CLI hint line
    local last_line
    last_line=$(tmux capture-pane -t "$pane" -p 2>/dev/null \
        | sed '/^[[:space:]]*$/d' \
        | grep -vE '^[[:space:]]*[0-9]+%[[:space:]]+[0-9]' \
        | grep -vE '^[[:space:]]*⏵' \
        | grep -vE '^[[:space:]]*esc again' \
        | tail -1)

    # Empty pane or no content
    [[ -z "$last_line" ]] && return 1

    # Strip the line down to check for input after prompt markers.
    # Common prompt endings: $ % # > (with optional space)
    # If after removing the prompt marker there's still content, user is typing.

    # Match: line ends with a bare prompt (no user text after it)
    # These patterns match a prompt with nothing after it:
    #   "user@host:~/dir$ "
    #   "❯ "
    #   "> "
    #   "% "
    #   "# "
    # The key insight: if the line ends with [$%#>❯] followed by only whitespace,
    # the prompt is empty. If there's more content after, user is typing.

    # Check for Claude Code idle prompt: line is just ">" or "> " (with possible leading space/box chars)
    if echo "$last_line" | grep -qE '^[[:space:]│░▒▓]*>[[:space:]]*$'; then
        return 1  # Empty Claude Code prompt — clear
    fi

    # Check for shell prompt ending with common markers, nothing after
    if echo "$last_line" | grep -qE '[$%#>❯][[:space:]]*$'; then
        # Line ends with prompt marker + optional whitespace — no user input
        return 1
    fi

    # If we got here, there's content after the prompt marker (or no recognized prompt).
    # Check if the pane is showing Claude Code output (not a prompt at all).
    # Claude Code output lines don't start with > and aren't shell prompts.
    # If there's no prompt marker anywhere on the line, it's probably output → clear.
    if ! echo "$last_line" | grep -qE '[$%#>❯]'; then
        return 1  # No prompt marker found — likely output, not an input line
    fi

    # Content exists after a prompt marker — user is typing
    return 0
}

# Wait for a pane to be clear of user input.
# Args: PANE [TIMEOUT_SECONDS]
# Returns 0 if cleared, 1 if timed out.
tmux_wait_for_clear() {
    local pane="$1"
    local timeout="${2:-10}"
    local elapsed=0
    local interval=0.5
    local marked_work=0

    while tmux_pane_has_input "$pane"; do
        if [[ "$marked_work" == "0" ]]; then
            # Pending human input is a short-lived work signal. This bridges the
            # gap between typing a prompt and submitting it, but Token-API's
            # productivity layer will decay after its normal 3-minute work
            # activity grace if the draft is abandoned.
            if command -v work-action >/dev/null 2>&1; then
                work-action --source tmux-typing-guard --note "pane=${pane}" >/dev/null 2>&1 || true
            elif [[ -n "${TOKEN_API_URL:-}" ]]; then
                curl -fsS -m 1 \
                    -H 'Content-Type: application/json' \
                    -d "{\"source\":\"tmux-typing-guard\",\"note\":\"pane=${pane}\"}" \
                    "${TOKEN_API_URL%/}/api/work-action" >/dev/null 2>&1 || true
            fi
            marked_work=1
        fi
        sleep "$interval"
        elapsed=$(echo "$elapsed + $interval" | bc)
        if (( $(echo "$elapsed >= $timeout" | bc -l) )); then
            echo "tmux-guard: timed out waiting for clear input in $pane (${timeout}s)" >&2
            return 1
        fi
    done
    return 0
}

# Canonical typing-guard predicate — thin reader of the ONE implementation in
# tmuxctl.send_gate.typing_guard_active (which reads tmux #{client_activity}).
# No second source of truth: the status segment, this guard, and the universal
# send gate all consult the same predicate. Returns 0 if the human typed within
# the client_activity window, non-zero otherwise (or on error → fail-open).
tmux_typing_guard_active() {
    local lib_dir
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
    PYTHONPATH="${lib_dir}${PYTHONPATH:+:$PYTHONPATH}" python3 -m tmuxctl.send_gate typing >/dev/null 2>&1
}

# Guarded send-keys: waits for the pane to be clear, then sends.
# Accepts the same arguments as `tmux send-keys`.
# Extracts -t TARGET from args to know which pane to guard.
#
# These sends are human-initiated (dictation, pedal-enter, resume), so they are
# marked sanctioned via TMUX_SEND_GATE_ALLOW: the universal send gate then lets
# them through and audits them as send_gate_override rather than suppressing
# them as automated traffic. The pane-clear wait below is a complementary
# protection (don't inject into a half-typed prompt) and stays.
#
# Special env vars:
#   TMUX_GUARD_TIMEOUT   — max seconds to wait (default: 10)
#   TMUX_GUARD_SKIP      — set to "1" to bypass guard entirely
#   TMUX_SEND_GATE_ALLOW — sanctioned-send reason (default "tmux-guard")
tmux_send_guarded() {
    local allow="${TMUX_SEND_GATE_ALLOW:-tmux-guard}"
    # Bypass if guard is disabled
    [[ "${TMUX_GUARD_SKIP:-}" == "1" ]] && { TMUX_SEND_GATE_ALLOW="$allow" "${TMUX_GUARD_REAL_TMUX:-tmux}" send-keys "$@"; return; }

    # Extract pane target from args
    local pane=""
    local args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
        if [[ "${args[$i]}" == "-t" ]] && (( i + 1 < ${#args[@]} )); then
            pane="${args[$((i + 1))]}"
            break
        fi
    done

    # If no -t specified, guard the current pane
    if [[ -z "$pane" ]]; then
        pane="${TMUX_PANE:-}"
    fi

    # If we still can't identify the pane, send without guarding
    if [[ -z "$pane" ]]; then
        TMUX_SEND_GATE_ALLOW="$allow" "${TMUX_GUARD_REAL_TMUX:-tmux}" send-keys "$@"
        return
    fi

    local timeout="${TMUX_GUARD_TIMEOUT:-10}"

    if ! tmux_wait_for_clear "$pane" "$timeout"; then
        echo "tmux-guard: ABORTED send-keys to $pane — user input not cleared after ${timeout}s" >&2
        return 1
    fi

    # Use real binary directly to avoid double-guarding through the wrapper function
    TMUX_SEND_GATE_ALLOW="$allow" "${TMUX_GUARD_REAL_TMUX:-tmux}" send-keys "$@"
}
