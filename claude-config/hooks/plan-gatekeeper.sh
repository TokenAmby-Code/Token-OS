#!/bin/bash
# plan-gatekeeper.sh — Bounce-then-approve plan gatekeeper
#
# Pass 1: REJECT with session doc hygiene reminder
# Pass 2: No decision (native dialog appears), then background
#          send-keys picks "clear context and auto-accept edits"
#
# State: /tmp/claude-plan-bounced-<session_id> exists = second pass
#
# The native dialog defaults to option 1:
#   > 1. Yes, clear context and auto-accept edits (shift+tab)
#     2. Yes, and manually approve edits
#     3. Yes, auto-accept edits
#     4. Yes, manually approve
#     5. Type here to tell Claude what to change
#
# We capture-pane to verify "clear context" is visible before pressing Enter,
# for safety.

set -euo pipefail

INPUT=$(cat 2>/dev/null || echo "{}")
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)

if [[ -z "$SESSION_ID" ]]; then
  exit 0
fi

GATE="/tmp/claude-plan-bounced-${SESSION_ID}"
LOG="${HOME}/.claude/logs/plan-gatekeeper.log"
mkdir -p "${HOME}/.claude/logs"

log() {
  echo "[$(date '+%H:%M:%S')] $*" >> "$LOG"
}

if [[ -f "$GATE" ]]; then
  rm -f "$GATE"

  # --- Second pass: let native dialog appear, then send Enter ---
  PANE="${TMUX_PANE:-}"
  if [[ -n "$PANE" ]]; then
    (
      # Wait for the dialog to render
      MAX_WAIT=10
      ELAPSED=0
      FOUND=false

      while (( ELAPSED < MAX_WAIT )); do
        sleep 0.5
        ELAPSED=$((ELAPSED + 1))

        CONTENT=$(tmux capture-pane -p -t "$PANE" -S -50 2>/dev/null) || continue

        if echo "$CONTENT" | grep -qi "clear context"; then
          FOUND=true
          log "Dialog detected with 'clear context' — pressing Enter"
          # Small delay to ensure dialog is fully interactive
          sleep 0.3
          tmux send-keys -t "$PANE" Enter
          break
        fi
      done

      if [[ "$FOUND" == "false" ]]; then
        log "WARNING: Dialog appeared but 'clear context' not found after ${MAX_WAIT}s — not pressing anything"
      fi

      # Post-approve: enforce bypass mode
      sleep 1
      MODE=$(tmux-mode-toggle --pane "$PANE" --status 2>/dev/null || echo "unknown")
      if [[ "$MODE" == "accept" || "$MODE" == "none" ]]; then
        tmux-mode-toggle --pane "$PANE" 2>/dev/null || true
      fi
    ) </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
  fi

  # No JSON output = no hook decision = native dialog appears
  log "Second pass — yielding to native dialog"
  exit 0
else
  touch "$GATE"
  MSG="We will clear context with this plan file. Have we updated the session doc with assumptions made, unvalidated code, completed tasks, and design decisions? Your plan file should orbit the session document — they should not duplicate concerns and the session doc is authoritative. The plan file is for the specific next task and context management. Your next plan will be automatically approved."
  log "First pass — bouncing with session doc reminder"
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PermissionRequest\",\"decision\":{\"behavior\":\"deny\",\"message\":\"$MSG\"}}}"
fi
