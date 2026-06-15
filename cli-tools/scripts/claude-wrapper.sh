#!/usr/bin/env bash

set -euo pipefail

API_URL="${TOKEN_API_URL:-http://100.95.109.23:7777}"
LAUNCHER="${TOKEN_API_LAUNCHER:-claude-wrapper}"
ENGINE="${TOKEN_API_ENGINE:-claude}"
WORKING_DIR="$(pwd)"
TMUX_PANE_VALUE="${TOKEN_API_DISPATCH_RESOLVED_PANE:-${TMUX_PANE:-}}"
DISPATCH_TARGET_WINDOW="${TOKEN_API_PRINT_REDIRECT_WINDOW:-main:legion}"

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
while [[ -L "$SCRIPT_PATH" ]]; do
  SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  [[ "$SCRIPT_PATH" == /* ]] || SCRIPT_PATH="${SCRIPT_DIR}/${SCRIPT_PATH}"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
COMMON_LIB="${SCRIPT_DIR}/../lib/agent-wrapper-common.sh"
if [[ ! -r "$COMMON_LIB" ]]; then
  echo "agent wrapper common library not found: $COMMON_LIB" >&2
  exit 127
fi
# shellcheck source=../lib/agent-wrapper-common.sh
source "$COMMON_LIB"
WRAPPER_LAUNCH_ID="${TOKEN_API_WRAPPER_LAUNCH_ID:-$(token_wrapper_uuid)}"

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

cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM HUP
  # Clear the instance->pane stamp the instant the agent dies. Unset by name
  # (no value needed); tmuxctl resolve-instance returns not-found immediately,
  # so no consumer sends to — or speaks the position of — a vanished agent.
  token_wrapper_cleanup_pane "$TMUX_PANE_VALUE"
  token_wrapper_end "$exit_code"
  exit "$exit_code"
}

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

  tmuxctl_bin="$(cd "${SCRIPT_DIR}/../bin" && pwd)/tmuxctl"
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

token_wrapper_start

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
