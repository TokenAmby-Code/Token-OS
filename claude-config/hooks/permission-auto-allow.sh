#!/bin/bash
# permission-auto-allow.sh — Auto-approve permission requests
#
# Auto-allows all PermissionRequest events EXCEPT those in the passthrough list.
# Passthrough tools get no decision output, so the native dialog appears.
#
# To add a new passthrough: add the tool name to PASSTHROUGH_TOOLS below.

set -euo pipefail

INPUT=$(cat 2>/dev/null || echo "{}")
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null)

# Tools that must reach the user — no auto-allow
# ExitPlanMode MUST stay here: plan-gatekeeper.sh's second pass outputs nothing
# (yielding to native dialog). Without passthrough, this hook races gatekeeper
# and auto-approves ExitPlanMode, bypassing the "clear context" dialog entirely.
PASSTHROUGH_TOOLS="AskUserQuestion ExitPlanMode"

for pt in $PASSTHROUGH_TOOLS; do
  if [[ "$TOOL_NAME" == "$pt" ]]; then
    # No output = no decision = native dialog appears
    exit 0
  fi
done

echo '{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}'
