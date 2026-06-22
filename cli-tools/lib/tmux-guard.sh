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
#   tmux_wait_for_clear PANE [TIMEOUT]  — returns 0 if send may proceed, 1 if blocked
#   tmux_send_guarded [send-keys args]  — guards, then sends
#
# Guard model:
#   * A pane is stamped once when pending prompt input is first observed.
#   * The stamp is per-pane and never refreshed by further typing.
#   * The stamp clears when the prompt becomes empty/submitted, or expires after
#     TMUX_GUARD_TTL seconds (default 300). An expired dirty pane stays allowed
#     until it becomes empty again; this prevents stale panes from re-blocking
#     forever.
#   * TMUX_GUARD_TIMEOUT is only how long a caller is willing to wait before
#     failing loud. Default 0 means immediate fail-loud, not indefinite wait.

_tmux_guard_tmux() {
    local bin="${TMUX_GUARD_REAL_TMUX:-}"
    if [[ -n "$bin" && -x "$bin" ]]; then
        "$bin" "$@"
    else
        command tmux "$@"
    fi
}

tmux_guard_now() {
    if [[ -n "${TMUX_GUARD_NOW:-}" ]]; then
        printf '%s\n' "$TMUX_GUARD_NOW"
    else
        date +%s
    fi
}

tmux_guard_ttl() {
    local ttl="${TMUX_GUARD_TTL:-300}"
    [[ "$ttl" =~ ^[0-9]+$ ]] || ttl=300
    (( ttl > 0 )) || ttl=300
    printf '%s\n' "$ttl"
}

tmux_guard_state_dir() {
    local dir="${TMUX_GUARD_STATE_DIR:-${XDG_RUNTIME_DIR:-/tmp}/tmux-typing-guard-${UID:-$(id -u 2>/dev/null || echo 0)}}"
    mkdir -p "$dir" 2>/dev/null || true
    printf '%s\n' "$dir"
}

tmux_guard_pane_key() {
    printf '%s' "$1" | sed 's/[^A-Za-z0-9_.:%-]/_/g'
}

tmux_guard_stamp_file() {
    local dir key
    dir="$(tmux_guard_state_dir)"
    key="$(tmux_guard_pane_key "$1")"
    printf '%s/%s.stamp\n' "$dir" "$key"
}

tmux_guard_write_stamp() {
    local pane="$1" started_at="$2" state="${3:-active}" file
    file="$(tmux_guard_stamp_file "$pane")"
    {
        printf 'started_at=%s\n' "$started_at"
        printf 'state=%s\n' "$state"
    } > "${file}.$$" 2>/dev/null && mv "${file}.$$" "$file" 2>/dev/null
}

tmux_guard_clear_stamp() {
    local file
    file="$(tmux_guard_stamp_file "$1")"
    rm -f "$file" "${file}.$$" 2>/dev/null || true
}

tmux_guard_log() {
    local event="$1" pane="$2" reason="${3:-}" timeout="${4:-}" waited="${5:-}" now log
    now="$(tmux_guard_now)"
    log="${TMUX_GUARD_LOG:-/tmp/tmux-typing-guard.jsonl}"
    TMUX_GUARD_LOG_PATH="$log" \
    TMUX_GUARD_EVENT="$event" \
    TMUX_GUARD_PANE="$pane" \
    TMUX_GUARD_REASON="$reason" \
    TMUX_GUARD_TIMEOUT_VALUE="$timeout" \
    TMUX_GUARD_WAITED_VALUE="$waited" \
    TMUX_GUARD_TTL_VALUE="$(tmux_guard_ttl)" \
    TMUX_GUARD_TS_VALUE="$now" \
    python3 - <<'PY' 2>/dev/null || true
import json
import os

path = os.environ["TMUX_GUARD_LOG_PATH"]
record = {
    "ts": int(float(os.environ.get("TMUX_GUARD_TS_VALUE") or "0")),
    "event": os.environ.get("TMUX_GUARD_EVENT") or "",
    "pane": os.environ.get("TMUX_GUARD_PANE") or "",
    "reason": os.environ.get("TMUX_GUARD_REASON") or "",
    "ttl": int(float(os.environ.get("TMUX_GUARD_TTL_VALUE") or "0")),
}
timeout = os.environ.get("TMUX_GUARD_TIMEOUT_VALUE")
if timeout not in (None, ""):
    record["timeout"] = timeout
waited = os.environ.get("TMUX_GUARD_WAITED_VALUE")
if waited not in (None, ""):
    record["waited"] = waited
os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, sort_keys=True) + "\n")
PY
}

tmux_guard_emit_blocked() {
    local pane="$1" timeout="${2:-0}" waited="${3:-0}"
    echo "tmux-guard: BLOCKED send-keys to $pane — user has pending input (waited ${waited}s, timeout ${timeout}s, ttl $(tmux_guard_ttl)s; bypass with TMUX_GUARD_SKIP=1)" >&2
    tmux_guard_log "blocked" "$pane" "user_input_pending" "$timeout" "$waited"
}

# Check if a pane has pending user input on the current prompt line.
# Returns 0 (true) if input detected, 1 (false) if clear.
#
# Detection strategy:
#   1. Capture the last non-chrome line of the pane (the prompt/input line)
#   2. Normalize Claude Code's non-breaking prompt space to ordinary space
#   3. Check if the line has content after common prompt markers:
#      - Shell prompts: ends with $ % # > followed by user text
#      - Claude Code: the input line has a > or ❯ prefix with text after it
#   4. If the stripped last line is empty or is just a prompt marker, it's clear
#
# Edge cases handled:
#   - Claude Code processing (spinner visible) → cursor line is output, not prompt → clear
#   - Empty shell prompt (just $) → clear
#   - User mid-type ($ git comm) → has input
tmux_pane_has_input() {
    local pane="$1"

    # Capture the pane and inspect the last real prompt/input line, filtering out
    # Claude Code / Codex status chrome that can sit below the prompt and confuse
    # prompt-marker heuristics:
    #   "  4% 38k/1.0M $0.19"             — Claude Code context % footer (the `%` was a false-positive prompt marker)
    #   "  ... 0/200k $0.00"              — Claude Code context footer after /clear
    #   "  ⏵⏵ bypass permissions ..."      — Claude Code hint line
    #   "  esc again to edit previous ..." — Codex CLI hint line
    local capture
    capture="$(_tmux_guard_tmux capture-pane -t "$pane" -p 2>/dev/null)"

    TMUX_GUARD_CAPTURE="$capture" python3 - <<'PY'
import os
import re
import sys

capture = os.environ.get("TMUX_GUARD_CAPTURE", "")


def normalize(line: str) -> str:
    return line.replace("\u00a0", " ")


def is_chrome(line: str) -> bool:
    normalized = normalize(line)
    stripped = normalized.strip()
    if not stripped:
        return True
    if stripped.startswith("⏵"):
        return True
    if stripped.startswith("esc again"):
        return True
    if stripped and all(ch in "─━-" for ch in stripped):
        return True
    if re.match(r"^\s*(?:\.\.\.|[0-9]+%)\s+.*\$[0-9]", normalized):
        return True
    return False


current = ""
for raw in reversed(capture.splitlines()):
    if is_chrome(raw):
        continue
    current = normalize(raw)
    break

if not current:
    sys.exit(1)  # empty pane or only UI chrome: clear

# Claude Code prompt. It can be rendered as "❯\u00a0" (NBSP after the marker)
# and can have box/border chars before it. This must win over stale submitted
# command echoes above it, such as "❯ /clear".
lead = current.lstrip(" \t\r\n│░▒▓▐▌")
if lead.startswith((">", "❯")):
    rest = lead[1:]
    sys.exit(0 if rest.strip() else 1)

# Shell-ish fallback for common prompts: marker with text after it blocks;
# bare marker at end is clear. If there is no marker, treat it as output.
marker_pos = max(current.rfind(marker) for marker in ("$", "%", "#", ">", "❯"))
if marker_pos < 0:
    sys.exit(1)

rest = current[marker_pos + 1 :]
sys.exit(0 if rest.strip() else 1)
PY
}

tmux_guard_pane_blocked() {
    local pane="$1" file now ttl started_at="" state="" age
    file="$(tmux_guard_stamp_file "$pane")"
    now="$(tmux_guard_now)"
    ttl="$(tmux_guard_ttl)"

    if ! tmux_pane_has_input "$pane"; then
        # Empty/just-submitted prompt: clear any prior stamp immediately.
        if [[ -e "$file" ]]; then
            tmux_guard_clear_stamp "$pane"
            tmux_guard_log "cleared" "$pane" "prompt_empty_or_submitted"
        fi
        return 1
    fi

    if [[ -f "$file" ]]; then
        # shellcheck disable=SC1090
        source "$file" 2>/dev/null || true
    fi

    if [[ "$state" == "expired" ]]; then
        return 1
    fi

    if [[ -z "$started_at" || ! "$started_at" =~ ^[0-9]+$ ]]; then
        started_at="$now"
        state="active"
        tmux_guard_write_stamp "$pane" "$started_at" "$state"
        tmux_guard_log "stamped" "$pane" "first_pending_input_observed"
    fi

    age=$(( now - started_at ))
    if (( age >= ttl )); then
        # Hard self-heal. Do not re-stamp until the pane becomes empty again.
        tmux_guard_write_stamp "$pane" "$started_at" "expired"
        tmux_guard_log "expired" "$pane" "hard_ttl_elapsed"
        return 1
    fi

    return 0
}

# Wait for a pane to be clear of user input.
# Args: PANE [TIMEOUT_SECONDS]
# Returns 0 if sending may proceed, 1 if blocked. Timeout 0 means no wait.
tmux_wait_for_clear() {
    local pane="$1"
    local timeout="${2:-0}"
    local start now elapsed=0
    local interval="${TMUX_GUARD_POLL_INTERVAL:-0.5}"
    local marked_work=0

    if [[ ! "$timeout" =~ ^[0-9]+$ ]]; then
        timeout="${timeout%%.*}"
        [[ "$timeout" =~ ^[0-9]+$ ]] || timeout=0
    fi

    start="$(tmux_guard_now)"

    while tmux_guard_pane_blocked "$pane"; do
        now="$(tmux_guard_now)"
        elapsed=$(( now - start ))
        if [[ "$timeout" == "0" ]] || (( elapsed >= timeout )); then
            tmux_guard_emit_blocked "$pane" "$timeout" "$elapsed"
            return 1
        fi
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
    done
    return 0
}

# Canonical typing-guard predicate — thin reader of the ONE implementation in
# tmuxctl.send_gate.typing_guard_active (which reads the per-pane keystroke lock
# @TYPING_LOCK_UNTIL stamped by the tmux any-key binding). No second source of
# truth: the status segment, this guard, and the universal send gate all consult
# the same predicate. Returns 0 if the target pane is keystroke-locked (the
# Emperor typed into it within the last 5 min and has not pressed Enter),
# non-zero otherwise (or on error → fail-open).
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
#   TMUX_GUARD_TIMEOUT   — max seconds to wait (default: 0 = wait indefinitely)
#   TMUX_GUARD_SKIP      — set to "1" to bypass the pane-line guard entirely
#   TMUX_SEND_GATE_ALLOW — sanctioned-send reason (default "tmux-guard")
tmux_send_guarded() {
    local allow="${TMUX_SEND_GATE_ALLOW:-tmux-guard}"
    # Bypass if guard is disabled
    [[ "${TMUX_GUARD_SKIP:-}" == "1" ]] && { TMUX_SEND_GATE_ALLOW="$allow" _tmux_guard_tmux send-keys "$@"; return; }

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
        TMUX_SEND_GATE_ALLOW="$allow" _tmux_guard_tmux send-keys "$@"
        return
    fi

    local timeout="${TMUX_GUARD_TIMEOUT:-0}"

    if ! tmux_wait_for_clear "$pane" "$timeout"; then
        return 1
    fi

    # Use real binary directly to avoid double-guarding through the wrapper function
    TMUX_SEND_GATE_ALLOW="$allow" _tmux_guard_tmux send-keys "$@"
}
