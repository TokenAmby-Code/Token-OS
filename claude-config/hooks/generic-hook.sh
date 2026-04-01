#!/bin/bash
# generic-hook.sh - Unified hook dispatcher for Claude Code
# Forwards hook JSON + action_type to token-api server for centralized handling
#
# Usage: HOOK_ACTION_TYPE=<type> bash ~/.claude/hooks/generic-hook.sh
# Types: SessionStart, SessionEnd, UserPromptSubmit, PostToolUse, Stop, PreToolUse, Notification
#
# Always exits 0 to never block Claude Code

LOG_FILE="${HOME}/.claude/logs/hook-debug.log"
mkdir -p "${HOME}/.claude/logs"

# Read hook input from stdin
HOOK_INPUT=$(cat 2>/dev/null || echo "{}")

# Debug logging (enable with HOOK_DEBUG=1)
if [[ "${HOOK_DEBUG:-0}" == "1" ]]; then
  echo "[$(date '+%H:%M:%S')] ${HOOK_ACTION_TYPE:-Unknown}: ${HOOK_INPUT:0:200}" >> "$LOG_FILE"
fi

# Default to empty JSON if no input
if [[ -z "$HOOK_INPUT" ]]; then
  HOOK_INPUT="{}"
fi

# Get action type from environment (set by settings.json hook command)
ACTION_TYPE="${HOOK_ACTION_TYPE:-Unknown}"

# Resolve token-api URL from environment (set in nas-path.sh)
API_URL="${TOKEN_API_URL:-http://100.95.109.23:7777}"

# Inject shell environment variables for device detection and primarch identity
if [[ -n "$SSH_CLIENT" || -n "$TMUX" || -n "$TMUX_PANE" || -n "$TOKEN_API_PRIMARCH" ]]; then
  JQ_FILTER=".env //= {} | .env"
  [[ -n "$SSH_CLIENT" ]] && JQ_FILTER="$JQ_FILTER + {SSH_CLIENT: \$ssh}"
  [[ -n "$TMUX" ]] && JQ_FILTER="$JQ_FILTER + {TMUX: \$tmux}"
  [[ -n "$TMUX_PANE" ]] && JQ_FILTER="$JQ_FILTER + {TMUX_PANE: \$tmux_pane}"
  [[ -n "$TOKEN_API_PRIMARCH" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_PRIMARCH: \$primarch}"
  JQ_FILTER=".env = ($JQ_FILTER)"
  HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c \
    --arg ssh "${SSH_CLIENT:-}" \
    --arg tmux "${TMUX:-}" \
    --arg tmux_pane "${TMUX_PANE:-}" \
    --arg primarch "${TOKEN_API_PRIMARCH:-}" \
    "$JQ_FILTER" 2>/dev/null) || true
fi

# Find the Claude ancestor process PID
# Uses ps(1) which works on both macOS and Linux
CLAUDE_PID=""
CURRENT="$PPID"
for _ in 1 2 3; do
  if [ -z "$CURRENT" ] || [ "$CURRENT" = "1" ]; then break; fi
  COMM=$(basename "$(ps -o comm= -p "$CURRENT" 2>/dev/null)" 2>/dev/null)
  if [ "$COMM" = "claude" ]; then
    CLAUDE_PID="$CURRENT"
    break
  fi
  CURRENT=$(ps -o ppid= -p "$CURRENT" 2>/dev/null | tr -d ' ')
done

# Inject PID into payload
if [ -n "$CLAUDE_PID" ]; then
  HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg pid "$CLAUDE_PID" '.pid = ($pid | tonumber)') || true
fi

# Resolve tmux pane via PID walk when env var is stripped (Claude Code strips $TMUX_PANE)
# Inject into payload so Token-API can store it for cross-machine dispatch
if [[ -z "$TMUX_PANE" ]] && [[ -n "$CLAUDE_PID" ]] && [[ "$ACTION_TYPE" == "SessionStart" ]]; then
  RESOLVED_PANE=$(claude-cmd --self --resolve-only 2>/dev/null || true)
  if [[ -n "$RESOLVED_PANE" ]]; then
    HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg p "$RESOLVED_PANE" '.tmux_pane = $p') || true
  fi
fi

# Write session PID cache for worktree-setup transplant
if [ -n "$CLAUDE_PID" ]; then
  SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null)
  if [ -n "$SESSION_ID" ]; then
    if [ "$ACTION_TYPE" = "SessionStart" ]; then
      mkdir -p "${HOME}/.claude/session-pids"
      echo "$SESSION_ID" > "${HOME}/.claude/session-pids/${CLAUDE_PID}"
    elif [ "$ACTION_TYPE" = "SessionEnd" ]; then
      rm -f "${HOME}/.claude/session-pids/${CLAUDE_PID}"
    fi
  fi
fi

# For SessionStart: check for transplant handoff file (written by transplant tool)
# Atomically consume (mv then read) to prevent double-reads and act as lock release.
if [[ "$ACTION_TYPE" == "SessionStart" ]]; then
  TRANSPLANT_PANE=""
  if [[ -n "$CLAUDE_PID" ]]; then
    TRANSPLANT_PANE=$(claude-cmd --self --resolve-only 2>/dev/null || true)
  fi

  if [[ -n "$TRANSPLANT_PANE" ]]; then
    HANDOFF_FILE="${HOME}/.claude/transplant-pending/${TRANSPLANT_PANE}"
    if [[ -f "$HANDOFF_FILE" ]]; then
      HANDOFF_TMP="${HANDOFF_FILE}.consumed.$$"
      if mv "$HANDOFF_FILE" "$HANDOFF_TMP" 2>/dev/null; then
        TRANSPLANT_FROM=$(cat "$HANDOFF_TMP" 2>/dev/null)
        rm -f "$HANDOFF_TMP"
        if [[ -n "$TRANSPLANT_FROM" ]]; then
          HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg tf "$TRANSPLANT_FROM" '.transplant_from = $tf') || true
          [[ "${HOOK_DEBUG:-0}" == "1" ]] && echo "[$(date '+%H:%M:%S')] Transplant handoff: $TRANSPLANT_FROM -> pane $TRANSPLANT_PANE" >> "$LOG_FILE"
        fi
      fi
    fi
  fi
fi

# For Stop hooks: embed transcript tail so the remote server can read it.
# Skip embedding if server is local (it can read transcript_path directly).
# Embedding large transcripts (18KB/line × 60 lines) creates 740KB+ payloads
# that timeout in the background curl.
if [[ "$ACTION_TYPE" == "Stop" ]]; then
  TRANSCRIPT_PATH=$(echo "$HOOK_INPUT" | jq -r '.transcript_path // ""' 2>/dev/null)
  IS_LOCAL=$([[ "$API_URL" == *"localhost"* || "$API_URL" == *"127.0.0.1"* ]] && echo 1 || echo 0)
  if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ] && [ "$IS_LOCAL" = "0" ]; then
    TRANSCRIPT_TAIL=""
    for _ in 1 2 3 4 5 6 7 8; do
      TAIL=$(tail -n 60 "$TRANSCRIPT_PATH")
      if echo "$TAIL" | grep -q '"type":"text"'; then
        TRANSCRIPT_TAIL="$TAIL"
        break
      fi
      sleep 0.25
    done
    # Fallback: use raw tail even without "type":"text" (short sessions, tool-only output)
    if [ -z "$TRANSCRIPT_TAIL" ]; then
      TRANSCRIPT_TAIL=$(tail -n 60 "$TRANSCRIPT_PATH" 2>/dev/null)
    fi
    if [ -n "$TRANSCRIPT_TAIL" ]; then
      HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg t "$TRANSCRIPT_TAIL" '.transcript_tail = $t') || true
    fi
  fi
fi

# Drain pending UI commands on UserPromptSubmit (user just pressed Enter, prompt bar is empty)
if [[ "$ACTION_TYPE" == "UserPromptSubmit" ]]; then
  PANE=$(claude-cmd --self --resolve-only 2>/dev/null || true)
  if [[ -n "$PANE" ]]; then
    PANE_SAFE=$(echo "$PANE" | tr -d '%')
    PENDING_FILE="${HOME}/.claude/pending-ui-cmds/${PANE_SAFE}"
    if [[ -f "$PENDING_FILE" ]]; then
      DRAIN_FILE="${PENDING_FILE}.drain.$$"
      mv "$PENDING_FILE" "$DRAIN_FILE" 2>/dev/null || true
      if [[ -f "$DRAIN_FILE" ]]; then
        (
          exec 0</dev/null 1>/dev/null 2>/dev/null
          first=true
          last_pane=""
          while IFS=' ' read -r cmd_pane cmd_rest; do
            last_pane="$cmd_pane"
            if [[ "$first" == true ]]; then
              claude-cmd --no-escape --pane "$cmd_pane" "$cmd_rest" 2>/dev/null || true
              first=false
            else
              claude-cmd --pane "$cmd_pane" "$cmd_rest" 2>/dev/null || true
            fi
          done < "$DRAIN_FILE"
          [[ -n "$last_pane" ]] && tmux send-keys -t "$last_pane" C-u 2>/dev/null || true
          rm -f "$DRAIN_FILE"
        ) </dev/null >/dev/null 2>&1 &
        disown 2>/dev/null || true
      fi
    fi
  fi
fi

# Forward to token-api server
# PreToolUse needs synchronous response for permission decisions
# All other hooks fire-and-forget in background to never block Claude
if [[ "$ACTION_TYPE" == "PreToolUse" ]]; then
  RESPONSE=$(echo "${HOOK_INPUT}" | \
    curl -s --connect-timeout 2 --max-time 3 \
      -X POST "${API_URL}/api/hooks/${ACTION_TYPE}" \
      -H "Content-Type: application/json" \
      -d @- 2>/dev/null) || true

  if echo "$RESPONSE" | grep -q '"permissionDecision"'; then
    echo "$RESPONSE"
  fi

  # Execute local_exec side-effect if present (e.g., AHK for voice chat)
  LOCAL_EXEC=$(echo "$RESPONSE" | jq -r '.local_exec // empty' 2>/dev/null)
  if [[ -n "$LOCAL_EXEC" ]]; then
    (
      exec 0</dev/null 1>/dev/null 2>/dev/null
      eval "$LOCAL_EXEC"
    ) </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
    [[ "${HOOK_DEBUG:-0}" == "1" ]] && echo "[$(date '+%H:%M:%S')] local_exec: $LOCAL_EXEC" >> "$LOG_FILE"
  fi
elif [[ "$ACTION_TYPE" == "SessionStart" ]]; then
  RESPONSE=$(echo "${HOOK_INPUT}" | \
    curl -s --connect-timeout 2 --max-time 3 \
      -X POST "${API_URL}/api/hooks/${ACTION_TYPE}" \
      -H "Content-Type: application/json" \
      -d @- 2>/dev/null) || true

  # Defer auto-color to next UserPromptSubmit via pending-ui-cmds
  COLOR=$(echo "$RESPONSE" | jq -r '.cc_color // empty' 2>/dev/null)
  PANE=$(claude-cmd --self --resolve-only 2>/dev/null || true)
  if [[ -n "$COLOR" && -n "$PANE" ]]; then
    PENDING_DIR="${HOME}/.claude/pending-ui-cmds"
    mkdir -p "$PENDING_DIR"
    PANE_SAFE=$(echo "$PANE" | tr -d '%')
    echo "$PANE /color $COLOR" >> "$PENDING_DIR/${PANE_SAFE}"
  fi
else
  (
    exec 0</dev/null 1>/dev/null 2>/dev/null
    echo "${HOOK_INPUT}" | \
      curl -s --connect-timeout 2 --max-time 10 \
        -X POST "${API_URL}/api/hooks/${ACTION_TYPE}" \
        -H "Content-Type: application/json" \
        -d @-
  ) </dev/null >/dev/null 2>&1 &
  disown 2>/dev/null || true
fi

# Always exit successfully - hooks must not block Claude Code
exit 0
