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

# Claude Code strips PATH during hook execution, so `command -v` may fail.
# Use environment-configured roots only; do not hardcode NAS paths.
_mount_live() {
  local root="$1"
  [[ -n "$root" && -d "$root" ]] || return 1
  ls "$root" >/dev/null 2>&1
}

_resolve_token_os_bin() {
  local tool="$1" found root cand
  found=$(command -v "$tool" 2>/dev/null) || true
  if [[ -n "$found" && -x "$found" ]]; then
    printf '%s\n' "$found"
    return 0
  fi
  for root in "${IMPERIUM:-}" "${CIVIC:-}"; do
    [[ -n "$root" ]] || continue
    cand="${root%/}/runtimes/token-os/live/cli-tools/bin/${tool}"
    if _mount_live "$root" && [[ -x "$cand" ]]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
  return 1
}

CLAUDE_CMD=$(_resolve_token_os_bin claude-cmd) || true
# Fallback to no-op if claude-cmd is not found anywhere
: "${CLAUDE_CMD:=false}"

# Resolve pending-ui-flush the same way — it owns the guarded
# drain/enqueue/sweep of the pane-branding queue.
PENDING_UI_FLUSH=$(_resolve_token_os_bin pending-ui-flush) || true
: "${PENDING_UI_FLUSH:=false}"

# Inject shell environment variables for device detection, primarch identity,
# and structured dispatch metadata from launcher wrappers.
if [[ -n "$SSH_CLIENT" || -n "$TMUX" || -n "$TMUX_PANE" || -n "${TOKEN_API_PERSONA:-}" || -n "$TOKEN_API_PRIMARCH" || -n "${TOKEN_API_LAUNCHER:-}" || -n "${TOKEN_API_ENGINE:-}" || -n "${TOKEN_API_DISPATCH_TARGET:-}" || -n "${TOKEN_API_DISPATCH_WINDOW:-}" || -n "${TOKEN_API_DISPATCH_MODE:-}" || -n "${TOKEN_API_DISPATCH_SLOT:-}" || -n "${TOKEN_API_PARENT_INSTANCE_ID:-}" || -n "${TOKEN_API_DISPATCH_SESSION_DOC_PATH:-}" || -n "${TOKEN_API_TARGET_WORKING_DIR:-}" || -n "${TOKEN_API_LAUNCH_MODE:-}" || -n "${TOKEN_API_TRANSPLANT_EXPECTED:-}" || -n "${TOKEN_API_DISPATCH_RESOLVED_PANE:-}" || -n "${TOKEN_API_WRAPPER_LAUNCH_ID:-}" || -n "${TOKEN_API_INSTANCE_TYPE:-}" || -n "${TOKEN_API_ZEALOTRY:-}" || -n "${TOKEN_API_DISCORD_HOSTED:-}" || -n "${TOKEN_API_DISCORD_CHANNEL:-}" || -n "${TOKEN_API_DISCORD_BOT:-}" || -n "${TOKEN_API_DISPATCH_MCP:-}" || -n "${TOKEN_API_DISPATCH_WITH_BROWSER:-}" || -n "${TOKEN_API_DISPATCH_WITH_DESKTOP:-}" || -n "${TOKEN_API_DISPATCH_MCP_LIST:-}" ]]; then
  JQ_FILTER=".env //= {} | .env"
  [[ -n "$SSH_CLIENT" ]] && JQ_FILTER="$JQ_FILTER + {SSH_CLIENT: \$ssh}"
  [[ -n "$TMUX" ]] && JQ_FILTER="$JQ_FILTER + {TMUX: \$tmux}"
  [[ -n "$TMUX_PANE" ]] && JQ_FILTER="$JQ_FILTER + {TMUX_PANE: \$tmux_pane}"
  [[ -n "${TOKEN_API_PERSONA:-${TOKEN_API_PRIMARCH:-}}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_PERSONA: \$persona}"
  [[ -n "${TOKEN_API_LAUNCHER:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_LAUNCHER: \$launcher}"
  [[ -n "${TOKEN_API_ENGINE:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_ENGINE: \$engine}"
  [[ -n "${TOKEN_API_DISPATCH_TARGET:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_TARGET: \$dispatch_target}"
  [[ -n "${TOKEN_API_DISPATCH_WINDOW:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_WINDOW: \$dispatch_window}"
  [[ -n "${TOKEN_API_DISPATCH_MODE:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_MODE: \$dispatch_mode}"
  [[ -n "${TOKEN_API_DISPATCH_SLOT:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_SLOT: \$dispatch_slot}"
  [[ -n "${TOKEN_API_PARENT_INSTANCE_ID:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_PARENT_INSTANCE_ID: \$parent_instance_id}"
  [[ -n "${TOKEN_API_DISPATCH_SESSION_DOC_PATH:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_SESSION_DOC_PATH: \$dispatch_session_doc_path}"
  [[ -n "${TOKEN_API_TARGET_WORKING_DIR:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_TARGET_WORKING_DIR: \$target_working_dir}"
  [[ -n "${TOKEN_API_LAUNCH_MODE:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_LAUNCH_MODE: \$launch_mode}"
  [[ -n "${TOKEN_API_TRANSPLANT_EXPECTED:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_TRANSPLANT_EXPECTED: \$transplant_expected}"
  [[ -n "${TOKEN_API_DISPATCH_RESOLVED_PANE:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_RESOLVED_PANE: \$dispatch_resolved_pane}"
  [[ -n "${TOKEN_API_WRAPPER_LAUNCH_ID:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_WRAPPER_LAUNCH_ID: \$wrapper_launch_id}"
  [[ -n "${TOKEN_API_INSTANCE_TYPE:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_INSTANCE_TYPE: \$instance_type}"
  [[ -n "${TOKEN_API_ZEALOTRY:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_ZEALOTRY: \$zealotry}"
  [[ -n "${TOKEN_API_DISCORD_HOSTED:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISCORD_HOSTED: \$discord_hosted}"
  [[ -n "${TOKEN_API_DISCORD_CHANNEL:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISCORD_CHANNEL: \$discord_channel}"
  [[ -n "${TOKEN_API_DISCORD_BOT:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISCORD_BOT: \$discord_bot}"
  [[ -n "${TOKEN_API_DISPATCH_MCP:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_MCP: \$dispatch_mcp}"
  [[ -n "${TOKEN_API_DISPATCH_WITH_BROWSER:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_WITH_BROWSER: \$dispatch_with_browser}"
  [[ -n "${TOKEN_API_DISPATCH_WITH_DESKTOP:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_WITH_DESKTOP: \$dispatch_with_desktop}"
  [[ -n "${TOKEN_API_DISPATCH_MCP_LIST:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_DISPATCH_MCP_LIST: \$dispatch_mcp_list}"
  JQ_FILTER=".env = ($JQ_FILTER)"
  HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c \
    --arg ssh "${SSH_CLIENT:-}" \
    --arg tmux "${TMUX:-}" \
    --arg tmux_pane "${TMUX_PANE:-}" \
    --arg persona "${TOKEN_API_PERSONA:-${TOKEN_API_PRIMARCH:-}}" \
    --arg launcher "${TOKEN_API_LAUNCHER:-}" \
    --arg engine "${TOKEN_API_ENGINE:-}" \
    --arg dispatch_target "${TOKEN_API_DISPATCH_TARGET:-}" \
    --arg dispatch_window "${TOKEN_API_DISPATCH_WINDOW:-}" \
    --arg dispatch_mode "${TOKEN_API_DISPATCH_MODE:-}" \
    --arg dispatch_slot "${TOKEN_API_DISPATCH_SLOT:-}" \
    --arg parent_instance_id "${TOKEN_API_PARENT_INSTANCE_ID:-}" \
    --arg dispatch_session_doc_path "${TOKEN_API_DISPATCH_SESSION_DOC_PATH:-}" \
    --arg target_working_dir "${TOKEN_API_TARGET_WORKING_DIR:-}" \
    --arg launch_mode "${TOKEN_API_LAUNCH_MODE:-}" \
    --arg transplant_expected "${TOKEN_API_TRANSPLANT_EXPECTED:-}" \
    --arg dispatch_resolved_pane "${TOKEN_API_DISPATCH_RESOLVED_PANE:-}" \
    --arg wrapper_launch_id "${TOKEN_API_WRAPPER_LAUNCH_ID:-}" \
    --arg instance_type "${TOKEN_API_INSTANCE_TYPE:-}" \
    --arg zealotry "${TOKEN_API_ZEALOTRY:-}" \
    --arg discord_hosted "${TOKEN_API_DISCORD_HOSTED:-}" \
    --arg discord_channel "${TOKEN_API_DISCORD_CHANNEL:-}" \
    --arg discord_bot "${TOKEN_API_DISCORD_BOT:-}" \
    --arg dispatch_mcp "${TOKEN_API_DISPATCH_MCP:-}" \
    --arg dispatch_with_browser "${TOKEN_API_DISPATCH_WITH_BROWSER:-}" \
    --arg dispatch_with_desktop "${TOKEN_API_DISPATCH_WITH_DESKTOP:-}" \
    --arg dispatch_mcp_list "${TOKEN_API_DISPATCH_MCP_LIST:-}" \
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
  RESOLVED_PANE=$($CLAUDE_CMD --self --resolve-only 2>/dev/null || true)
  if [[ -n "$RESOLVED_PANE" ]]; then
    HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg p "$RESOLVED_PANE" '.tmux_pane = $p') || true
    # Also resolve @PANE_ID (human-readable label like "palace:TR") for DB-driven resurrection
    RESOLVED_LABEL=$(tmux show-options -pv -t "$RESOLVED_PANE" @PANE_ID 2>/dev/null || true)
    if [[ -n "$RESOLVED_LABEL" ]]; then
      HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg l "$RESOLVED_LABEL" '.pane_label = $l') || true
    fi
    # Ship the pane's @INSTANCE_ID stamp so SessionStart can adopt the prior
    # occupant's row even when the server-side tmuxctl lookup misses
    # (a plan-approval context-clear would otherwise mint a duplicate row)
    PANE_STAMP=$(tmux show-options -pqv -t "$RESOLVED_PANE" @INSTANCE_ID 2>/dev/null || true)
    if [[ -n "$PANE_STAMP" ]]; then
      HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg s "$PANE_STAMP" '.pane_instance_id = $s') || true
    fi
  fi
fi

# When $TMUX_PANE IS available, still resolve @PANE_ID for DB persistence
if [[ -n "$TMUX_PANE" ]] && [[ "$ACTION_TYPE" == "SessionStart" ]]; then
  RESOLVED_LABEL=$(tmux show-options -pv -t "$TMUX_PANE" @PANE_ID 2>/dev/null || true)
  if [[ -n "$RESOLVED_LABEL" ]]; then
    HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg l "$RESOLVED_LABEL" '.pane_label = $l') || true
  fi
  PANE_STAMP=$(tmux show-options -pqv -t "$TMUX_PANE" @INSTANCE_ID 2>/dev/null || true)
  if [[ -n "$PANE_STAMP" ]]; then
    HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg s "$PANE_STAMP" '.pane_instance_id = $s') || true
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
    TRANSPLANT_PANE=$($CLAUDE_CMD --self --resolve-only 2>/dev/null || true)
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

# Drain pending UI commands on UserPromptSubmit (user just pressed Enter, prompt bar is empty).
# pending-ui-flush applies the typing-guard HOLD, epoch/TTL/dead-pane purge, and
# human-attached skip before any send-keys — see cli-tools/bin/pending-ui-flush.
# It never injects while the target pane is being typed in or actively viewed,
# never replays a stale or foreign-epoch entry, and never drops a held command.
if [[ "$ACTION_TYPE" == "UserPromptSubmit" ]]; then
  PANE=$($CLAUDE_CMD --self --resolve-only 2>/dev/null || true)
  if [[ -n "$PANE" ]]; then
    SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
    (
      exec 0</dev/null 1>/dev/null 2>/dev/null
      "$PENDING_UI_FLUSH" flush --pane "$PANE" --session "$SESSION_ID"
    ) </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
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
  # Clear stale tmux-context flush marker so fresh context window gets fresh threshold
  if [[ -n "$TMUX_PANE" ]]; then
    SAFE_PANE="${TMUX_PANE#%}"
    rm -f "/tmp/claude-panes/flush-${SAFE_PANE}.ts" 2>/dev/null
  fi

  RESPONSE=$(echo "${HOOK_INPUT}" | \
    curl -s --connect-timeout 2 --max-time 3 \
      -X POST "${API_URL}/api/hooks/${ACTION_TYPE}" \
      -H "Content-Type: application/json" \
      -d @- 2>/dev/null) || true

  # Defer auto-rename to next UserPromptSubmit via pending-ui-cmds. Pane tint is
  # DB/persona-resolved and applied by tmux style only; no slash color path.
  TAB_NAME=$(echo "$RESPONSE" | jq -r '.tab_name // empty' 2>/dev/null)
  PANE=$($CLAUDE_CMD --self --resolve-only 2>/dev/null || true)
  if [[ -n "$PANE" && -n "$TAB_NAME" ]]; then
    SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)
    ENQ_ARGS=(enqueue --pane "$PANE" --session "$SESSION_ID" --rename "$TAB_NAME")
    "$PENDING_UI_FLUSH" "${ENQ_ARGS[@]}" >/dev/null 2>&1 || true
  fi
  # Prune legacy/expired/dead-pane queue files every relaunch so the queue never
  # grows append-only (the 58-file, April-backlog failure mode).
  "$PENDING_UI_FLUSH" sweep >/dev/null 2>&1 || true
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
