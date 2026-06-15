#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../lib/tmux-runtime-cleanup.sh
source "${SCRIPT_DIR}/../lib/tmux-runtime-cleanup.sh" 2>/dev/null || true

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
WRAPPER_LAUNCH_ID="${TOKEN_API_WRAPPER_LAUNCH_ID:-$(uuidgen 2>/dev/null | tr '[:upper:]' '[:lower:]' || date +%s%N)}"
if declare -F tmux_runtime_stamp_wrapper >/dev/null 2>&1; then
  tmux_runtime_stamp_wrapper "${TMUX_PANE:-}" "$WRAPPER_LAUNCH_ID" "${TOKEN_API_ENGINE:-codex}" "${TOKEN_API_LAUNCHER:-codex-wrapper}" "$(pwd)"
fi

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
  rm -f "$TEMP_LOG"
  # Clear the instance->pane stamp on agent death so tmuxctl resolve-instance
  # fails closed the instant codex exits (mirrors claude-wrapper cleanup).
  if declare -F tmux_runtime_cleanup_pane >/dev/null 2>&1; then
    tmux_runtime_cleanup_pane "${TMUX_PANE:-}"
  elif [[ -n "${TMUX_PANE:-}" ]] && command -v tmux >/dev/null 2>&1; then
    tmux set-option -p -u -t "$TMUX_PANE" @INSTANCE_ID >/dev/null 2>&1 || true
  fi
}
trap codex_cleanup EXIT

script -a -f -e -c "$codex_path $(printf '%q' "$command_str")" "$TEMP_LOG"
status=$?

strip_ansi <"$TEMP_LOG" >>"$LOG_FILE"

end_timestamp="$(date -Iseconds)"
{
  echo "Finished: ${end_timestamp}"
  echo "Exit code: ${status}"
  echo ""
} >>"$LOG_FILE"

exit $status

