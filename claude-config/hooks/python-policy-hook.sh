#!/usr/bin/env bash
# Block direct agent Python execution; require uv at command boundary.
# Keep python/python3 shims as real interpreter delegates so uv can safely probe them.

set -euo pipefail
HOOK_INPUT=$(cat 2>/dev/null || echo '{}')

_direct_python_command() {
  local cmd="$1"
  [[ -n "$cmd" ]] || return 1
  [[ "$cmd" =~ (^|[\;\&\|\(][[:space:]]*)(env[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*=[^[:space:]]+[[:space:]]+)*(/[^[:space:]\;\&\|\(\)]+/)?python(3(\.[0-9]+)?)?([[:space:]\;\&\|\)]|$) ]]
}

TOOL_NAME=$(echo "$HOOK_INPUT" | jq -r '.tool_name // .tool // empty' 2>/dev/null || true)
TOOL_COMMAND=$(echo "$HOOK_INPUT" | jq -r '.tool_input.command // .tool_input.cmd // .command // empty' 2>/dev/null || true)

if [[ ( "$TOOL_NAME" == "Bash" || -n "$TOOL_COMMAND" ) ]] && _direct_python_command "$TOOL_COMMAND"; then
  cat <<'JSON'
{"permissionDecision":"deny","permissionDecisionReason":"Direct python/python3 execution is blocked by local policy. Use uv, e.g. `uv run python ...` or `uv run --python /opt/homebrew/bin/python3 -- python ...`. The python shim itself is a real-interpreter delegate to avoid uv recursion."}
JSON
fi
exit 0
