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

  # Resolve session doc ID and path from instance via Token-API
  SESSION_DOC_PATH=""
  DOC_ID=""
  API_URL="${TOKEN_API_URL:-http://localhost:7777}"
  if [[ -n "$SESSION_ID" ]]; then
    INSTANCE=$(curl -sf --max-time 2 "${API_URL}/api/instances/${SESSION_ID}" 2>/dev/null || echo "{}")
    DOC_ID=$(echo "$INSTANCE" | jq -r '.session_doc_id // empty' 2>/dev/null)
    if [[ -n "$DOC_ID" ]]; then
      DOC=$(curl -sf --max-time 2 "${API_URL}/api/session-docs/${DOC_ID}" 2>/dev/null || echo "{}")
      SESSION_DOC_PATH=$(echo "$DOC" | jq -r '.file_path // .title // empty' 2>/dev/null)
    fi
  fi

  # Build the rejection message — this IS the session-update mechanism.
  # Agents enter plan mode → gatekeeper bounces with merge instructions →
  # agent updates doc → resubmits → auto-approved. No /session-update skill needed.
  if [[ -n "$SESSION_DOC_PATH" && -n "$DOC_ID" ]]; then
    DOC_INSTRUCTION="Merge your update to session doc ${SESSION_DOC_PATH} (ID: ${DOC_ID}) using: curl -s -X POST ${API_URL}/api/session-docs/${DOC_ID}/merge -H 'Content-Type: application/json' -d '{\"content\": \"<your update>\", \"source\": \"agent\"}'"
  elif [[ -n "$DOC_ID" ]]; then
    DOC_INSTRUCTION="Merge your update to session doc ID ${DOC_ID} using: curl -s -X POST ${API_URL}/api/session-docs/${DOC_ID}/merge -H 'Content-Type: application/json' -d '{\"content\": \"<your update>\", \"source\": \"agent\"}'"
  else
    DOC_INSTRUCTION="No session doc linked. Create one with: instance-name \"<name>\" --session"
  fi

  MSG="Before clearing context, update your session document. ${DOC_INSTRUCTION} — Include: completed work, assumptions made, unvalidated code, design decisions, and remaining tasks. The session doc is authoritative — your plan file should orbit it, not duplicate it. The plan file is for the specific next task and context management. Your next ExitPlanMode will be automatically approved."
  log "First pass — bouncing with merge instructions (doc: ${DOC_ID:-none}, path: ${SESSION_DOC_PATH:-unknown})"
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PermissionRequest\",\"decision\":{\"behavior\":\"deny\",\"message\":\"$MSG\"}}}"
fi
