#!/usr/bin/env bash
# tmux-guard.sh — shell reader for the canonical tmux typing guard
# TAGS: tmux, guard, send-keys, safety
#
# The guard state has one source of truth: the tmuxctld /typing-guard-state
# endpoint backed by @TYPING_GUARD_JSON. This shell library intentionally does
# not inspect pane contents, maintain sidecar files, or import cold tmuxctl state
# logic.

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
    echo "tmux-guard: BLOCKED send-keys to $pane — typing guard active (waited ${waited}s, timeout ${timeout}s; bypass with TMUX_GUARD_SKIP=1)" >&2
    tmux_guard_log "blocked" "$pane" "typing_guard" "$timeout" "$waited"
}

# Return 0 when the canonical typing guard is active for PANE, else non-zero.
tmux_typing_guard_active() {
    local pane="${1:-${TMUX_PANE:-}}"
    [[ -n "$pane" ]] || return 1
    local url body
    url="${TMUXCTLD_URL:-http://127.0.0.1:7778}"
    body="$(python3 - "$pane" <<'PY'
import json, sys
print(json.dumps({"cmd": "status", "pane": sys.argv[1]}, separators=(",", ":")))
PY
)" || return 1
    curl -fsS --connect-timeout "${TMUXCTLD_CONNECT_TIMEOUT:-1}" --max-time "${TMUXCTLD_MAX_TIME:-3}" \
        -H 'Content-Type: application/json' \
        -d "$body" \
        "${url%/}/typing-guard-state" 2>/dev/null |
        python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("active") else 1)' 2>/dev/null
}

# Historical shell name retained inside this library; it delegates to daemon state.
tmux_pane_has_input() {
    tmux_typing_guard_active "${1:-}"
}

# Wait for the canonical guard to clear. Timeout 0 means check once and fail loud.
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
    while tmux_typing_guard_active "$pane"; do
        now="$(tmux_guard_now)"
        elapsed=$(( now - start ))
        if [[ "$timeout" == "0" ]] || (( elapsed >= timeout )); then
            tmux_guard_emit_blocked "$pane" "$timeout" "$elapsed"
            return 1
        fi
        if [[ "$marked_work" == "0" ]]; then
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

# Guarded send-keys: waits for the canonical guard, then sends.
tmux_send_guarded() {
    local allow="${TMUX_SEND_GATE_ALLOW:-tmux-guard}"
    [[ "${TMUX_GUARD_SKIP:-}" == "1" ]] && { TMUX_SEND_GATE_ALLOW="$allow" _tmux_guard_tmux send-keys "$@"; return; }

    local pane=""
    local args=("$@")
    for ((i = 0; i < ${#args[@]}; i++)); do
        if [[ "${args[$i]}" == "-t" ]] && (( i + 1 < ${#args[@]} )); then
            pane="${args[$((i + 1))]}"
            break
        fi
    done
    [[ -n "$pane" ]] || pane="${TMUX_PANE:-}"
    if [[ -z "$pane" ]]; then
        TMUX_SEND_GATE_ALLOW="$allow" _tmux_guard_tmux send-keys "$@"
        return
    fi

    if ! tmux_wait_for_clear "$pane" "${TMUX_GUARD_TIMEOUT:-0}"; then
        return 1
    fi
    TMUX_SEND_GATE_ALLOW="$allow" _tmux_guard_tmux send-keys "$@"
}
