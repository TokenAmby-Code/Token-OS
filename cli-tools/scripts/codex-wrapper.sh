#!/usr/bin/env bash

set -euo pipefail

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

API_URL="${TOKEN_API_URL:-http://100.95.109.23:7777}"
LAUNCHER="${TOKEN_API_LAUNCHER:-codex-wrapper}"
ENGINE="${TOKEN_API_ENGINE:-codex}"
WRAPPER_LAUNCH_ID="${TOKEN_API_WRAPPER_LAUNCH_ID:-$(token_wrapper_uuid)}"
WORKING_DIR="$(pwd)"
TMUX_PANE_VALUE="${TOKEN_API_DISPATCH_RESOLVED_PANE:-${TMUX_PANE:-}}"
export TOKEN_API_WRAPPER_LAUNCH_ID="$WRAPPER_LAUNCH_ID"

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <agent_id> <log_file> <command...>" >&2
  exit 64
fi

AGENT_ID="$1"
LOG_FILE="$2"
shift 2

if ! command -v script >/dev/null 2>&1; then
  echo "codex-wrapper.sh requires the 'script' utility for TTY-preserving logging." >&2
  exit 65
fi

LOG_DIR="$(dirname -- "$LOG_FILE")"
mkdir -p "$LOG_DIR"

codex_path="$1"
shift
prompt_arg="$*"

if [[ "$prompt_arg" =~ ^@FILE:(.+)$ ]]; then
  prompt_file="${BASH_REMATCH[1]}"
  if [[ ! -f "$prompt_file" ]]; then
    echo "Error: Prompt file not found: $prompt_file" >&2
    exit 66
  fi
  command_str=$(cat "$prompt_file")
  command_display="@FILE:${prompt_file}"
else
  command_str="$prompt_arg"
  command_display="$prompt_arg"
fi

start_timestamp="$(date -Iseconds)"
{
  echo "=== Codex Agent ${AGENT_ID} ==================================="
  echo "Command: ${command_display}"
  if [[ "$prompt_arg" =~ ^@FILE:(.+)$ ]]; then
    echo "Prompt source: file ($prompt_file)"
    echo "Prompt length: $(wc -c < "$prompt_file") bytes"
  fi
  echo "Started: ${start_timestamp}"
  echo "==============================================================="
} >>"$LOG_FILE"

strip_ansi() {
  sed -E \
    -e 's/\x1b\[[0-9;?]*[a-zA-Z]//g' \
    -e 's/\x1b\[[<>][0-9;]*[a-zA-Z]//g' \
    -e 's/\x1b\][0-9]*;[^[:cntrl:]]*(\x07|\x1b\\)//g' \
    -e 's/\x1b\?[0-9;]*[a-zA-Z]//g' \
    -e 's/\x1b[=><]//g' \
    -e 's/\x1b[()][AB012]//g' \
    -e 's/\x1b\[[0-9]*[;rHJ]//g'
}

TEMP_LOG=$(mktemp)
codex_cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM HUP
  rm -f "$TEMP_LOG"
  # Clear the instance->pane stamp on agent death so tmuxctl resolve-instance
  # fails closed the instant codex exits (mirrors claude-wrapper cleanup).
  token_wrapper_cleanup_pane "$TMUX_PANE_VALUE"
  token_wrapper_end "$exit_code"
  exit "$exit_code"
}
trap codex_cleanup EXIT INT TERM HUP

token_wrapper_start

set +e
script -a -f -e -c "$codex_path $(printf '%q' "$command_str")" "$TEMP_LOG"
status=$?
set -e

strip_ansi <"$TEMP_LOG" >>"$LOG_FILE"

end_timestamp="$(date -Iseconds)"
{
  echo "Finished: ${end_timestamp}"
  echo "Exit code: ${status}"
  echo ""
} >>"$LOG_FILE"

exit "$status"
