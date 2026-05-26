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

log() {
  echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"
}

PANE="${TMUX_PANE:-}"
if [[ -n "$PANE" ]]; then
  (
    tmux-plan-approve-clear --pane "$PANE" --agent claude --timeout 10 >> "$LOG" 2>&1 || true
  ) </dev/null >/dev/null 2>&1 &
  disown 2>/dev/null || true
  log "ExitPlanMode ${SESSION_ID:-unknown}: launched clear-context approver for $PANE"
else
  log "ExitPlanMode ${SESSION_ID:-unknown}: no TMUX_PANE; yielding without approver"
fi

# No JSON output = no hook decision = native dialog appears.
exit 0
