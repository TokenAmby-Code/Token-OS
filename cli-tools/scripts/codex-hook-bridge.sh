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
RESUME_SCRIPT="${IMPERIUM:-/Volumes/Imperium}/Token-OS/cli-tools/scripts/agent-session-end-resume.sh"
[[ -f "$RESUME_SCRIPT" ]] || RESUME_SCRIPT="/Volumes/Imperium/Token-OS/cli-tools/scripts/agent-session-end-resume.sh"
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
            --arg token_session "${TOKEN_API_SESSION_ID:-}" \
            --arg bridge_id "${TOKEN_API_CODEX_BRIDGE_ID:-}" \
            '.action = $action
             | .cwd //= $cwd
             | .env //= {}
             | .env.TMUX = $tmux
             | .env.TMUX_PANE = $tmux_pane
             | .env.SSH_CLIENT = $ssh_client
             | .env.TOKEN_API_SESSION_ID = $token_session
             | .env.TOKEN_API_CODEX_BRIDGE_ID = $bridge_id' 2>/dev/null || printf '%s' "$HOOK_INPUT"
    )"
fi

if command -v jq >/dev/null 2>&1 && [[ -n "${TOKEN_API_CODEX_BRIDGE_ID:-}" ]]; then
    CODEX_SESSION_ID="$(printf '%s' "$HOOK_INPUT" | jq -r '.session_id // .conversation_id // empty' 2>/dev/null || true)"
    if [[ -n "$CODEX_SESSION_ID" && "$CODEX_SESSION_ID" != "${TOKEN_API_SESSION_ID:-}" ]]; then
        BRIDGE_DIR="${HOME}/.codex/session-bridges"
        mkdir -p "$BRIDGE_DIR" 2>/dev/null || true
        printf '%s' "$CODEX_SESSION_ID" > "${BRIDGE_DIR}/${TOKEN_API_CODEX_BRIDGE_ID}.session_id" 2>/dev/null || true
    fi
fi

if [[ "$ACTION_TYPE" == "Stop" ]]; then
    printf '%s' "$HOOK_INPUT" | bash "$RESUME_SCRIPT" codex 2>/dev/null || true
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
