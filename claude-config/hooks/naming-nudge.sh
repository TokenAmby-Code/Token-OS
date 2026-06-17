#!/bin/bash
# naming-nudge.sh - thin Stop hook shim for active tab-name enforcement.

INPUT=$(cat 2>/dev/null || echo "{}")
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
for _nas_lib in \
  "${TOKEN_OS:-}/cli-tools/lib/nas-path.sh" \
  "${IMPERIUM:-}/runtimes/token-os/live/cli-tools/lib/nas-path.sh" \
  "${SCRIPT_DIR}/../../cli-tools/lib/nas-path.sh" \
  "${HOME}/runtimes/Token-OS/live/cli-tools/lib/nas-path.sh"; do
  if [[ -n "$_nas_lib" && -f "$_nas_lib" ]]; then
    # shellcheck source=/dev/null
    source "$_nas_lib" 2>/dev/null || true
    break
  fi
done
API_URL="${TOKEN_API_URL:-http://localhost:7777}"

# Best-effort only: never block or fail the harness if token-api is unreachable.
echo "$INPUT" | curl -s --connect-timeout 2 --max-time 5 \
  -X POST "${API_URL}/api/orchestrator/naming_nudge" \
  -H "Content-Type: application/json" \
  -d @- >/dev/null 2>&1 || true

exit 0
