#!/bin/bash
# plan-gatekeeper.sh — yield ExitPlanMode to native UI and approve clear-context modal
#
# No bounce state machine. /preplan is the explicit session-doc update step.
# This hook only starts a short-lived screen watcher that presses the native
# clear-context approval choice when that specific modal appears.

set -euo pipefail

INPUT=$(cat 2>/dev/null || echo "{}")
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
LOG="${HOME}/.claude/logs/plan-gatekeeper.log"
mkdir -p "${HOME}/.claude/logs"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LIB="${SCRIPT_DIR}/../../cli-tools/lib/plan-approver-launch.sh"
if [[ -f "$LIB" ]]; then
  # shellcheck source=../../cli-tools/lib/plan-approver-launch.sh
  source "$LIB"
else
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] plan-approver-skip engine=claude trigger=precise_permission reason=ExitPlanMode:${SESSION_ID:-unknown} error=missing-lib" >> "$LOG" 2>/dev/null || true
  exit 0
fi

plan_approver_launch \
  --agent claude \
  --trigger-class precise_permission \
  --hook-input "$INPUT" \
  --reason "ExitPlanMode:${SESSION_ID:-unknown}" \
  --log-file "$LOG"

# No JSON output = no hook decision = native dialog appears.
exit 0
