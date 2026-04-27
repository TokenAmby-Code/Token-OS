#!/usr/bin/env bash
# codex-hook-bridge.sh — forward Codex hook events to token-api.
#
# The Codex hooks config calls this with the action type as argv[1]. Hook JSON
# is read from stdin and forwarded best-effort; the script never blocks Codex on
# API failures.

set -uo pipefail

ACTION_TYPE="${1:-Unknown}"
API_URL="${TOKEN_API_URL:-http://100.95.109.23:7777}"
LOG_DIR="${HOME}/.codex/log"
LOG_FILE="${LOG_DIR}/hook-bridge.log"
mkdir -p "$LOG_DIR" 2>/dev/null || true

HOOK_INPUT="$(cat 2>/dev/null || true)"
[[ -z "$HOOK_INPUT" ]] && HOOK_INPUT="{}"

if command -v jq >/dev/null 2>&1; then
    HOOK_INPUT="$(
        printf '%s' "$HOOK_INPUT" | jq -c \
            --arg action "$ACTION_TYPE" \
            --arg cwd "$(pwd)" \
            --arg tmux "${TMUX:-}" \
            --arg tmux_pane "${TMUX_PANE:-}" \
            --arg ssh_client "${SSH_CLIENT:-}" \
            '.action = $action
             | .cwd //= $cwd
             | .env //= {}
             | .env.TMUX = $tmux
             | .env.TMUX_PANE = $tmux_pane
             | .env.SSH_CLIENT = $ssh_client' 2>/dev/null || printf '%s' "$HOOK_INPUT"
    )"
fi

if [[ "${HOOK_DEBUG:-0}" == "1" ]]; then
    printf '[%s] %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$ACTION_TYPE" "${HOOK_INPUT:0:500}" >> "$LOG_FILE" 2>/dev/null || true
fi

(
    exec 0</dev/null 1>/dev/null 2>/dev/null
    printf '%s' "$HOOK_INPUT" | curl -s --connect-timeout 2 --max-time 5 \
        -X POST "${API_URL}/api/hooks/${ACTION_TYPE}" \
        -H "Content-Type: application/json" \
        -d @- >/dev/null 2>&1 || true
) &
disown 2>/dev/null || true

exit 0
