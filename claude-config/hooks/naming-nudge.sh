#!/bin/bash
# naming-nudge.sh - thin Stop hook shim for active tab-name enforcement.

INPUT=$(cat 2>/dev/null || echo "{}")
API_URL="${TOKEN_API_URL:-http://100.95.109.23:7777}"

# Best-effort only: never block or fail the harness if token-api is unreachable.
echo "$INPUT" | curl -s --connect-timeout 2 --max-time 5 \
  -X POST "${API_URL}/api/orchestrator/naming_nudge" \
  -H "Content-Type: application/json" \
  -d @- >/dev/null 2>&1 || true

exit 0
