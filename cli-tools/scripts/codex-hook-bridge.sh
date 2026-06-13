#!/usr/bin/env bash
# codex-hook-bridge.sh — forward Codex hook events to token-api.
#
# The Codex hooks config calls this with the action type as argv[1]. Hook JSON
# is read from stdin and forwarded best-effort; the script never blocks Codex on
# API failures.

set -uo pipefail

ACTION_TYPE="${1:-Unknown}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/../lib"
if [[ -f "$LIB_DIR/nas-path.sh" ]]; then
    # shellcheck source=../lib/nas-path.sh
    source "$LIB_DIR/nas-path.sh" 2>/dev/null || true
fi
API_URL="${TOKEN_API_URL:-http://localhost:7777}"
LOG_DIR="${HOME}/.codex/log"
LOG_FILE="${LOG_DIR}/hook-bridge.log"
TOKEN_API_CODEX_LAUNCHER="${TOKEN_API_LAUNCHER:-codex-hooks}"
TOKEN_API_CODEX_ENGINE="${TOKEN_API_ENGINE:-codex}"
RESUME_SCRIPT="${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/scripts/agent-session-end-resume.sh"
[[ -f "$RESUME_SCRIPT" ]] || RESUME_SCRIPT="${IMPERIUM:-/Volumes/Imperium}/runtimes/token-os/live/cli-tools/scripts/agent-session-end-resume.sh"
PLAN_APPROVER="${TOKEN_API_PLAN_APPROVER:-${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/bin/tmux-plan-approve-clear}"
[[ -x "$PLAN_APPROVER" ]] || PLAN_APPROVER="${IMPERIUM:-/Volumes/Imperium}/runtimes/token-os/live/cli-tools/bin/tmux-plan-approve-clear"
[[ -x "$PLAN_APPROVER" ]] || PLAN_APPROVER="${SCRIPT_DIR}/../bin/tmux-plan-approve-clear"
mkdir -p "$LOG_DIR" 2>/dev/null || true

HOOK_INPUT="$(cat 2>/dev/null || true)"
[[ -z "$HOOK_INPUT" ]] && HOOK_INPUT="{}"

if command -v jq >/dev/null 2>&1; then
    HOOK_INPUT="$(
        printf '%s' "$HOOK_INPUT" | jq -c \
            --arg action "$ACTION_TYPE" \
            --arg cwd "$(pwd)" \
            --arg pid "$$" \
            --arg tmux "${TMUX:-}" \
            --arg tmux_pane "${TMUX_PANE:-}" \
            --arg token_resolved_pane "${TOKEN_API_DISPATCH_RESOLVED_PANE:-}" \
            --arg ssh_client "${SSH_CLIENT:-}" \
            --arg token_session "${TOKEN_API_SESSION_ID:-}" \
            --arg bridge_id "${TOKEN_API_CODEX_BRIDGE_ID:-}" \
            --arg token_launcher "$TOKEN_API_CODEX_LAUNCHER" \
            --arg token_engine "$TOKEN_API_CODEX_ENGINE" \
            --arg token_dispatch_target "${TOKEN_API_DISPATCH_TARGET:-}" \
            --arg token_dispatch_window "${TOKEN_API_DISPATCH_WINDOW:-}" \
            --arg token_dispatch_mode "${TOKEN_API_DISPATCH_MODE:-}" \
            --arg token_dispatch_slot "${TOKEN_API_DISPATCH_SLOT:-}" \
            --arg token_parent_instance_id "${TOKEN_API_PARENT_INSTANCE_ID:-}" \
            --arg token_dispatch_session_doc_path "${TOKEN_API_DISPATCH_SESSION_DOC_PATH:-}" \
            --arg token_target_working_dir "${TOKEN_API_TARGET_WORKING_DIR:-}" \
            --arg token_launch_mode "${TOKEN_API_LAUNCH_MODE:-}" \
            --arg token_transplant_expected "${TOKEN_API_TRANSPLANT_EXPECTED:-}" \
            --arg token_instance_type "${TOKEN_API_INSTANCE_TYPE:-}" \
            --arg token_zealotry "${TOKEN_API_ZEALOTRY:-}" \
            --arg token_dispatch_mcp "${TOKEN_API_DISPATCH_MCP:-}" \
            --arg token_dispatch_with_browser "${TOKEN_API_DISPATCH_WITH_BROWSER:-}" \
            --arg token_dispatch_with_desktop "${TOKEN_API_DISPATCH_WITH_DESKTOP:-}" \
            --arg token_dispatch_mcp_list "${TOKEN_API_DISPATCH_MCP_LIST:-}" \
            --arg token_discord_hosted "${TOKEN_API_DISCORD_HOSTED:-}" \
            --arg token_discord_channel "${TOKEN_API_DISCORD_CHANNEL:-}" \
            --arg token_discord_bot "${TOKEN_API_DISCORD_BOT:-}" \
            '.action = $action
             | .launcher //= $token_launcher
             | .engine //= $token_engine
             | .cwd //= $cwd
             | .pid //= ($pid | tonumber)
             | .env //= {}
             | .env.TMUX = $tmux
             | .env.TMUX_PANE = (if $tmux_pane == "" then $token_resolved_pane else $tmux_pane end)
             | .env.SSH_CLIENT = $ssh_client
             | .env.TOKEN_API_SESSION_ID = $token_session
             | .env.TOKEN_API_CODEX_BRIDGE_ID = $bridge_id
             | .env.TOKEN_API_WRAPPER_LAUNCH_ID = $bridge_id
             | .env.TOKEN_API_LAUNCHER = $token_launcher
             | .env.TOKEN_API_ENGINE = $token_engine
             | .env.TOKEN_API_DISPATCH_TARGET = $token_dispatch_target
             | .env.TOKEN_API_DISPATCH_WINDOW = $token_dispatch_window
             | .env.TOKEN_API_DISPATCH_MODE = $token_dispatch_mode
             | .env.TOKEN_API_DISPATCH_SLOT = $token_dispatch_slot
             | .env.TOKEN_API_PARENT_INSTANCE_ID = $token_parent_instance_id
             | .env.TOKEN_API_DISPATCH_SESSION_DOC_PATH = $token_dispatch_session_doc_path
             | .env.TOKEN_API_TARGET_WORKING_DIR = $token_target_working_dir
             | .env.TOKEN_API_LAUNCH_MODE = $token_launch_mode
             | .env.TOKEN_API_DISPATCH_RESOLVED_PANE = $token_resolved_pane
             | .env.TOKEN_API_TRANSPLANT_EXPECTED = $token_transplant_expected
             | .env.TOKEN_API_INSTANCE_TYPE = $token_instance_type
             | .env.TOKEN_API_ZEALOTRY = $token_zealotry
             | .env.TOKEN_API_DISPATCH_MCP = $token_dispatch_mcp
             | .env.TOKEN_API_DISPATCH_WITH_BROWSER = $token_dispatch_with_browser
             | .env.TOKEN_API_DISPATCH_WITH_DESKTOP = $token_dispatch_with_desktop
             | .env.TOKEN_API_DISPATCH_MCP_LIST = $token_dispatch_mcp_list
             | .env.TOKEN_API_DISCORD_HOSTED = $token_discord_hosted
             | .env.TOKEN_API_DISCORD_CHANNEL = $token_discord_channel
             | .env.TOKEN_API_DISCORD_BOT = $token_discord_bot' 2>/dev/null || printf '%s' "$HOOK_INPUT"
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

json_value() {
    local expr="$1"
    if ! command -v jq >/dev/null 2>&1; then
        return 1
    fi
    printf '%s' "$HOOK_INPUT" | jq -r "$expr" 2>/dev/null || true
}

maybe_launch_plan_approver() {
    [[ "$ACTION_TYPE" == "Stop" ]] || return 0
    command -v jq >/dev/null 2>&1 || return 0
    [[ -x "$PLAN_APPROVER" ]] || return 0

    local pane state
    pane="${TMUX_PANE:-}"
    [[ -n "$pane" ]] || pane="$(json_value '.env.TMUX_PANE // empty')"
    [[ -n "$pane" ]] || return 0

    state="$(
        curl -fsS -G --connect-timeout 1 --max-time 2 \
            --data-urlencode "tmux_pane=${pane}" \
            "${API_URL}/api/planning/state" 2>/dev/null \
            | jq -r '.planning_state // empty' 2>/dev/null || true
    )"
    case "$state" in
        planning|approving)
            (
                exec 0</dev/null
                "$PLAN_APPROVER" --pane "$pane" --agent codex --timeout 10 >>"$LOG_FILE" 2>&1
            ) &
            disown 2>/dev/null || true
            [[ "${HOOK_DEBUG:-0}" == "1" ]] && printf '[%s] Stop launched clear-context approver pane=%s state=%s\n' \
                "$(date '+%Y-%m-%d %H:%M:%S')" "$pane" "$state" >> "$LOG_FILE" 2>/dev/null || true
            ;;
    esac
}

maybe_launch_plan_approver

if [[ "$ACTION_TYPE" == "Stop" && "${TOKEN_API_DISABLE_SESSION_RESUME:-0}" != "1" ]]; then
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
