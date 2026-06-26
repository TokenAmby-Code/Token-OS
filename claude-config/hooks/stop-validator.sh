#!/bin/bash
set -euo pipefail
# Best-effort hook: must never block Claude Code. Keep strict mode for real bugs,
# but force a clean exit 0 even if errexit aborts mid-script on a transient
# subprocess-spawn failure (EMFILE / token-api-restart race). Block decisions are
# relayed via stdout JSON, never via the exit code, so forcing exit 0 is safe.
trap 'exit 0' EXIT
# stop-validator.sh - Thin shim: forwards stop validation to token-api.
# All validation logic lives in Python at /api/hooks/StopValidate.

INPUT=$(cat 2>/dev/null || echo "{}")

# Resolve token-api URL from centralized machine config.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Drop NAS mount roots that are not live so the `-f` probe below can't hang on a
# stale SMB mount; resolution falls through to script-relative / localhost.
if [[ -n "${IMPERIUM:-}" ]] && ! ls "${IMPERIUM}" >/dev/null 2>&1; then
  IMPERIUM=""
fi
if [[ -n "${CIVIC:-}" ]] && ! ls "${CIVIC}" >/dev/null 2>&1; then
  CIVIC=""
fi
for _nas_lib in \
  "${TOKEN_OS:-}/cli-tools/lib/nas-path.sh" \
  "${IMPERIUM:-}/runtimes/token-os/live/cli-tools/lib/nas-path.sh" \
  "${CIVIC:-}/runtimes/token-os/live/cli-tools/lib/nas-path.sh" \
  "${SCRIPT_DIR}/../../cli-tools/lib/nas-path.sh" \
  "${HOME}/runtimes/Token-OS/live/cli-tools/lib/nas-path.sh" \
  "${HOME}/runtimes/token-os/live/cli-tools/lib/nas-path.sh"; do
  if [[ -n "$_nas_lib" && -f "$_nas_lib" ]]; then
    # shellcheck source=/dev/null
    source "$_nas_lib" 2>/dev/null || true
    break
  fi
done
API_URL="${TOKEN_API_URL:-http://localhost:7777}"

# Walk process tree to inject the claude PID (portable: uses ps)
CLAUDE_PID=""
CURRENT="$PPID"
for _ in 1 2 3; do
  [ -z "$CURRENT" ] || [ "$CURRENT" = "1" ] && break
  COMM=$(basename "$(ps -o comm= -p "$CURRENT" 2>/dev/null)" 2>/dev/null)
  if [ "$COMM" = "claude" ]; then
    CLAUDE_PID="$CURRENT"
    break
  fi
  CURRENT=$(ps -o ppid= -p "$CURRENT" 2>/dev/null | tr -d ' ') || true
done

if [ -n "$CLAUDE_PID" ]; then
  INPUT=$(echo "$INPUT" | jq -c --arg pid "$CLAUDE_PID" '.pid = ($pid | tonumber)') || true
fi

# Inject TOKEN_API_SUBAGENT if set (marks instances spawned by the subagent CLI)
if [ -n "${TOKEN_API_SUBAGENT:-}" ]; then
  INPUT=$(echo "$INPUT" | jq -c --arg sub "$TOKEN_API_SUBAGENT" '.env.TOKEN_API_SUBAGENT = $sub') || true
fi

# Embed last 60 lines of transcript. Poll briefly for the final text block to flush.
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path // ""' 2>/dev/null) || true
if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  TRANSCRIPT_TAIL=""
  for _ in 1 2 3 4 5 6 7 8; do
    TAIL=$(tail -n 60 "$TRANSCRIPT_PATH") || true
    if echo "$TAIL" | grep -q '"type":"text"'; then
      TRANSCRIPT_TAIL="$TAIL"
      break
    fi
    sleep 0.25
  done
  # Fallback: use raw tail even without "type":"text"
  if [ -z "$TRANSCRIPT_TAIL" ]; then
    TRANSCRIPT_TAIL=$(tail -n 60 "$TRANSCRIPT_PATH" 2>/dev/null) || true
  fi
  if [ -n "$TRANSCRIPT_TAIL" ]; then
    INPUT=$(echo "$INPUT" | jq -c --arg t "$TRANSCRIPT_TAIL" '.transcript_tail = $t') || true
  fi
fi

# Forward to token-api synchronously
RESPONSE=$(echo "$INPUT" | curl -s --connect-timeout 2 --max-time 5 \
  -X POST "${API_URL}/api/hooks/StopValidate" \
  -H "Content-Type: application/json" \
  -d @- 2>/dev/null) || true

# Pass through block decision if present; exit 0 otherwise (allow on server unreachable)
if echo "$RESPONSE" | grep -q '"decision"'; then
  echo "$RESPONSE"
fi

exit 0
