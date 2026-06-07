#!/usr/bin/env bash

set -euo pipefail

API_URL="${TOKEN_API_URL:-http://100.95.109.23:7777}"
LAUNCHER="${TOKEN_API_LAUNCHER:-claude-wrapper}"
ENGINE="${TOKEN_API_ENGINE:-claude}"
WRAPPER_LAUNCH_ID="${TOKEN_API_WRAPPER_LAUNCH_ID:-$(uuidgen | tr '[:upper:]' '[:lower:]')}"
WORKING_DIR="$(pwd)"
TMUX_PANE_VALUE="${TOKEN_API_DISPATCH_RESOLVED_PANE:-${TMUX_PANE:-}}"
DISPATCH_TARGET_WINDOW="${TOKEN_API_PRINT_REDIRECT_WINDOW:-main:legion}"

post_hook() {
  local action_type="$1"
  local payload="$2"
  curl -s --connect-timeout 2 --max-time 5 \
    -X POST "${API_URL}/api/hooks/${action_type}" \
    -H "Content-Type: application/json" \
    -d "$payload" >/dev/null 2>&1 || true
}

build_payload() {
  local action_type="$1"
  local exit_code="${2:-}"
  jq -nc \
    --arg action "$action_type" \
    --arg wrapper_launch_id "$WRAPPER_LAUNCH_ID" \
    --arg launcher "$LAUNCHER" \
    --arg engine "$ENGINE" \
    --arg cwd "$WORKING_DIR" \
    --arg tmux_pane "$TMUX_PANE_VALUE" \
    --arg ssh_client "${SSH_CLIENT:-}" \
    --arg tmux "${TMUX:-}" \
    --arg token_api_launcher "${TOKEN_API_LAUNCHER:-}" \
    --arg token_api_engine "${TOKEN_API_ENGINE:-}" \
    --arg token_api_dispatch_target "${TOKEN_API_DISPATCH_TARGET:-}" \
    --arg token_api_dispatch_window "${TOKEN_API_DISPATCH_WINDOW:-}" \
    --arg token_api_dispatch_mode "${TOKEN_API_DISPATCH_MODE:-}" \
    --arg token_api_dispatch_slot "${TOKEN_API_DISPATCH_SLOT:-}" \
    --arg token_api_parent_instance_id "${TOKEN_API_PARENT_INSTANCE_ID:-}" \
    --arg token_api_dispatch_session_doc_path "${TOKEN_API_DISPATCH_SESSION_DOC_PATH:-}" \
    --arg token_api_target_working_dir "${TOKEN_API_TARGET_WORKING_DIR:-}" \
    --arg token_api_launch_mode "${TOKEN_API_LAUNCH_MODE:-}" \
    --arg token_api_transplant_expected "${TOKEN_API_TRANSPLANT_EXPECTED:-}" \
    --arg token_api_instance_type "${TOKEN_API_INSTANCE_TYPE:-}" \
    --arg token_api_zealotry "${TOKEN_API_ZEALOTRY:-}" \
    --arg token_api_dispatch_mcp "${TOKEN_API_DISPATCH_MCP:-}" \
    --arg token_api_dispatch_with_browser "${TOKEN_API_DISPATCH_WITH_BROWSER:-}" \
    --arg token_api_dispatch_with_desktop "${TOKEN_API_DISPATCH_WITH_DESKTOP:-}" \
    --arg token_api_dispatch_mcp_list "${TOKEN_API_DISPATCH_MCP_LIST:-}" \
    --arg token_api_discord_hosted "${TOKEN_API_DISCORD_HOSTED:-}" \
    --arg token_api_discord_channel "${TOKEN_API_DISCORD_CHANNEL:-}" \
    --arg token_api_discord_bot "${TOKEN_API_DISCORD_BOT:-}" \
    --arg token_api_wrapper_launch_id "$WRAPPER_LAUNCH_ID" \
    --argjson pid "$$" \
    --argjson exit_code "${exit_code:-null}" \
    '{
      action: $action,
      wrapper_launch_id: $wrapper_launch_id,
      launcher: $launcher,
      engine: $engine,
      cwd: $cwd,
      tmux_pane: (if $tmux_pane == "" then null else $tmux_pane end),
      pid: $pid,
      exit_code: $exit_code,
      env: {
        SSH_CLIENT: $ssh_client,
        TMUX: $tmux,
        TMUX_PANE: $tmux_pane,
        TOKEN_API_LAUNCHER: $token_api_launcher,
        TOKEN_API_ENGINE: $token_api_engine,
        TOKEN_API_DISPATCH_TARGET: $token_api_dispatch_target,
        TOKEN_API_DISPATCH_WINDOW: $token_api_dispatch_window,
        TOKEN_API_DISPATCH_MODE: $token_api_dispatch_mode,
        TOKEN_API_DISPATCH_SLOT: $token_api_dispatch_slot,
        TOKEN_API_PARENT_INSTANCE_ID: $token_api_parent_instance_id,
        TOKEN_API_DISPATCH_SESSION_DOC_PATH: $token_api_dispatch_session_doc_path,
        TOKEN_API_TARGET_WORKING_DIR: $token_api_target_working_dir,
        TOKEN_API_LAUNCH_MODE: $token_api_launch_mode,
        TOKEN_API_TRANSPLANT_EXPECTED: $token_api_transplant_expected,
        TOKEN_API_INSTANCE_TYPE: $token_api_instance_type,
        TOKEN_API_ZEALOTRY: $token_api_zealotry,
        TOKEN_API_DISPATCH_MCP: $token_api_dispatch_mcp,
        TOKEN_API_DISPATCH_WITH_BROWSER: $token_api_dispatch_with_browser,
        TOKEN_API_DISPATCH_WITH_DESKTOP: $token_api_dispatch_with_desktop,
        TOKEN_API_DISPATCH_MCP_LIST: $token_api_dispatch_mcp_list,
        TOKEN_API_DISCORD_HOSTED: $token_api_discord_hosted,
        TOKEN_API_DISCORD_CHANNEL: $token_api_discord_channel,
        TOKEN_API_DISCORD_BOT: $token_api_discord_bot,
        TOKEN_API_WRAPPER_LAUNCH_ID: $token_api_wrapper_launch_id
      }
    }'
}

cleanup() {
  local exit_code=$?
  local end_payload
  end_payload="$(build_payload "WrapperEnd" "$exit_code")"
  post_hook "WrapperEnd" "$end_payload"
  exit "$exit_code"
}

PRINT_MODE=false
redirect_args=()
skip_next=0
for arg in "$@"; do
  if [[ "$skip_next" -eq 1 ]]; then
    skip_next=0
    continue
  fi
  case "$arg" in
    -p|--print)
      PRINT_MODE=true
      ;;
    --output-format|--input-format|--json-schema|--max-budget-usd|--include-partial-messages|--replay-user-messages|--no-session-persistence)
      # Print-mode-only flags are dropped when redirecting into an interactive pane.
      if [[ "$arg" == "--output-format" || "$arg" == "--input-format" || "$arg" == "--json-schema" || "$arg" == "--max-budget-usd" ]]; then
        skip_next=1
      fi
      ;;
    *)
      redirect_args+=("$arg")
      ;;
  esac
done

if $PRINT_MODE; then
  if ! command -v tmux >/dev/null 2>&1; then
    echo "claude -p redirect requires tmux" >&2
    exit 1
  fi

  quoted_wrapper="$(printf '%q' "$0")"
  quoted_workdir="$(printf '%q' "$WORKING_DIR")"
  quoted_launcher="$(printf '%q' "$LAUNCHER")"
  quoted_engine="$(printf '%q' "$ENGINE")"
  quoted_wrapper_id="$(printf '%q' "$WRAPPER_LAUNCH_ID")"

  quoted_discord_hosted="$(printf '%q' "${TOKEN_API_DISCORD_HOSTED:-}")"
  quoted_discord_channel="$(printf '%q' "${TOKEN_API_DISCORD_CHANNEL:-}")"
  quoted_discord_bot="$(printf '%q' "${TOKEN_API_DISCORD_BOT:-}")"
  cmd="cd $quoted_workdir && TOKEN_API_LAUNCHER=$quoted_launcher TOKEN_API_ENGINE=$quoted_engine TOKEN_API_WRAPPER_LAUNCH_ID=$quoted_wrapper_id TOKEN_API_DISCORD_HOSTED=$quoted_discord_hosted TOKEN_API_DISCORD_CHANNEL=$quoted_discord_channel TOKEN_API_DISCORD_BOT=$quoted_discord_bot $quoted_wrapper --dangerously-skip-permissions"
  for arg in "${redirect_args[@]}"; do
    cmd+=" $(printf '%q' "$arg")"
  done

  dispatch_session="main"
  dispatch_base="$DISPATCH_TARGET_WINDOW"
  if [[ "$dispatch_base" == *:* ]]; then
    dispatch_session="${dispatch_base%%:*}"
    dispatch_base="${dispatch_base#*:}"
  fi
  dispatch_base="${dispatch_base%%(*}"
  case "$dispatch_base" in
    legion|mechanicus|mars|kreig) ;;
    *)
      echo "claude -p redirect target must be a managed stack window, got: $DISPATCH_TARGET_WINDOW" >&2
      exit 1
      ;;
  esac

  tmuxctl_bin="$(cd "$(dirname "$0")/../bin" && pwd)/tmuxctl"
  pane_id="$(
    IMPERIUM_TMUX_AUTOMATION=1 "$tmuxctl_bin" stack dispatch "$dispatch_base" \
      --session "$dispatch_session" \
      --cwd "$WORKING_DIR" \
      --no-focus \
      --command "$cmd" 2>/dev/null
  )" || {
    echo "failed to dispatch print-mode agent to $DISPATCH_TARGET_WINDOW" >&2
    exit 1
  }
  echo "redirected claude -p to $pane_id"
  exit 0
fi

trap cleanup EXIT INT TERM HUP

start_payload="$(build_payload "WrapperStart")"
post_hook "WrapperStart" "$start_payload"

export TOKEN_API_WRAPPER_LAUNCH_ID="$WRAPPER_LAUNCH_ID"

CLAUDE_BIN="${CLAUDE_WRAPPER_TARGET:-$HOME/.local/bin/claude}"
if [[ ! -x "$CLAUDE_BIN" ]]; then
  CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
fi
if [[ -z "$CLAUDE_BIN" ]]; then
  echo "claude binary not found" >&2
  exit 127
fi

"$CLAUDE_BIN" "$@" 2> >(grep -v 'Overriding existing handler for signal' >&2)
