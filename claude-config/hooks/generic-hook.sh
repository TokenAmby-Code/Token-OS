#!/bin/bash
set -euo pipefail
# Best-effort hook: must never block Claude Code. Keep strict mode for real bugs,
# but force a clean exit 0 even if errexit aborts mid-script on a transient
# subprocess-spawn failure (EMFILE / token-api-restart race). Block decisions are
# relayed via stdout JSON, never via the exit code, so forcing exit 0 is safe.
#
# EXCEPTION — the registration-critical SessionStart path. A bare claude/codex
# launch (no dispatch warming) registers its DB row, @INSTANCE_ID stamp, and
# session-doc binding ONLY through this hook, with no other re-registration leg
# until the next full `tx restart`. A blanket `trap 'exit 0'` there masks a
# dropped/failed registration as success, permanently stranding the pane with no
# row, no @INSTANCE_ID, and no error — the silent-swallow wound that took fleet
# registration down ~2.4h and killed cold-start workers. So the trap is GATED:
# a SessionStart that never confirms a bound row exits NON-ZERO and writes a
# visible failure record; every other hook stays best-effort exit-0. A non-zero
# SessionStart hook exit does not block session startup, so this is still safe.
#
# SELF-SUFFICIENCY (the reg-root): a bare/bypass launch must REACH the
# registration curl. Every bare external command and filesystem write in the
# SessionStart pre-POST path is guarded so a transient subprocess-spawn /
# resource failure (EMFILE, fork-exhaustion, a token-api-restart fd-pressure
# race, a stale-NAS-mounted $HOME) cannot trip errexit and abort the hook BEFORE
# the curl — the sole registration leg of a bare launch. Pinned root: an
# unguarded `mkdir`/`echo >` (the logs dir, the session-pid cache) aborted
# pre-POST under `set -euo pipefail`, so no row / no @INSTANCE_ID / no
# persona/tint were ever created. The curl's OWN failure is still surfaced loud.
# The critical marker is set FROM THE ENV IMMEDIATELY (before any subprocess
# spawn) so even an abort during early setup trips the gated trap (visible)
# rather than the blanket exit-0 (silent strand).
SESSIONSTART_CRITICAL=0   # 1 once this invocation is known to be SessionStart
[[ "${HOOK_ACTION_TYPE:-}" == "SessionStart" ]] && SESSIONSTART_CRITICAL=1
REGISTRATION_OK=0         # 1 only after a confirmed-successful registration reply
REGISTRATION_QUEUED=0     # 1 when SessionStart was durably queued for replay
FAILURE_LOGGED=0          # de-dupe: the inline failure logger already fired
HTTP_CODE=""              # last SessionStart POST status (for the failure record)
RESPONSE=""               # last SessionStart POST body (for the failure record)
RESOLVED_PANE=""          # set during pane resolution; referenced by the logger
SESSIONSTART_FAILURE_LOG="${SESSIONSTART_FAILURE_LOG:-${HOME}/.claude/logs/sessionstart-failures.log}"

_log_sessionstart_failure() {
  # Surface a registration failure VISIBLY and durably. token-api may itself be
  # the thing that is down, so this must NOT depend on it: append to a dedicated,
  # greppable failure log AND emit to stderr (shown in-transcript for an
  # interactive pane; captured by the launching wrapper for an autonomous one).
  local reason="$1" sid
  FAILURE_LOGGED=1
  sid=$(echo "${HOOK_INPUT:-}" | jq -r '.session_id // "?"' 2>/dev/null || echo "?")
  mkdir -p "$(dirname "$SESSIONSTART_FAILURE_LOG")" 2>/dev/null || true
  printf '[%s] SessionStart registration FAILED: %s | session=%s pane=%s | http=%s resp=%s\n' \
    "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$reason" "$sid" \
    "${TMUX_PANE:-${RESOLVED_PANE:-?}}" "${HTTP_CODE:-?}" "${RESPONSE:0:300}" \
    >> "$SESSIONSTART_FAILURE_LOG" 2>/dev/null || true
  echo "token-api SessionStart registration FAILED: ${reason} (see ${SESSIONSTART_FAILURE_LOG})" >&2
}

_hook_exit() {
  local rc=$?
  # Registration-critical SessionStart that never confirmed a bound row: do NOT
  # mask it with exit 0. Surface it (visible record + non-zero) so a stranded
  # pane is never silent again. SessionStart's exit code cannot block startup.
  if [[ "$SESSIONSTART_CRITICAL" == "1" && "$REGISTRATION_OK" != "1" ]]; then
    if [[ "$REGISTRATION_QUEUED" == "1" ]]; then
      [[ "$FAILURE_LOGGED" == "1" ]] || _log_sessionstart_failure "registration queued for replay but not yet confirmed (rc=${rc})"
    else
      [[ "$FAILURE_LOGGED" == "1" ]] || _log_sessionstart_failure "hook exited without confirmed registration (rc=${rc})"
    fi
    exit 1
  fi
  # Every other hook stays best-effort: never block Claude Code.
  exit 0
}
trap _hook_exit EXIT
# generic-hook.sh - Unified hook dispatcher for Claude Code
# Forwards hook JSON + action_type to token-api server for centralized handling
#
# Usage: HOOK_ACTION_TYPE=<type> bash ~/.claude/hooks/generic-hook.sh
# Types: SessionStart, SessionEnd, UserPromptSubmit, PostToolUse, Stop, PreToolUse, Notification
#
# Always exits 0 to never block Claude Code

LOG_FILE="${HOME}/.claude/logs/hook-debug.log"
# Guarded: a transient mkdir spawn/resource failure must NOT abort the hook
# before the SessionStart registration curl (the reg-root pre-POST abort).
mkdir -p "${HOME}/.claude/logs" 2>/dev/null || true

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
# (SESSIONSTART_CRITICAL was already marked from this same env value at the top,
# before any subprocess spawn, so an early abort is never a silent strand.)
ACTION_TYPE="${HOOK_ACTION_TYPE:-Unknown}"

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

# Durable cross-restart retry outbox. This is only invoked after an actual
# Token-API-unreachable POST result (curl http=000 / connection refused class);
# it is NOT a workaround for pre-POST shell aborts (http=?), which must stay
# loud under the SessionStart critical trap.
GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX=$(_resolve_token_os_bin generic-token-api-durable-retry-outbox) || true
: "${GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX:=false}"

_enqueue_hook_token_api_post() {
  local action_type="$1" payload="$2" url="$3" cause="${4:-http-000}"
  [[ "$GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX" != "false" ]] || return 1
  printf '%s' "$payload" | "$GENERIC_TOKEN_API_DURABLE_RETRY_OUTBOX" enqueue \
    --action-type "$action_type" \
    --url "$url" \
    --cause "$cause" >/dev/null 2>&1
}

# Inject shell environment variables for device detection, primarch identity,
# and structured dispatch metadata from launcher wrappers.
if [[ -n "${SSH_CLIENT:-}" || -n "${TMUX:-}" || -n "${TMUX_PANE:-}" || -n "${TOKEN_API_PANE_LABEL:-}" || -n "${TOKEN_API_PERSONA:-}" || -n "${TOKEN_API_LAUNCHER:-}" || -n "${TOKEN_API_ENGINE:-}" || -n "${TOKEN_API_DISPATCH_TARGET:-}" || -n "${TOKEN_API_DISPATCH_WINDOW:-}" || -n "${TOKEN_API_DISPATCH_MODE:-}" || -n "${TOKEN_API_DISPATCH_SLOT:-}" || -n "${TOKEN_API_PARENT_INSTANCE_ID:-}" || -n "${TOKEN_API_DISPATCH_SESSION_DOC_PATH:-}" || -n "${TOKEN_API_TARGET_WORKING_DIR:-}" || -n "${TOKEN_API_LAUNCH_MODE:-}" || -n "${TOKEN_API_TRANSPLANT_EXPECTED:-}" || -n "${TOKEN_API_DISPATCH_RESOLVED_PANE:-}" || -n "${TOKEN_API_WRAPPER_ID:-}" || -n "${TOKEN_API_INSTANCE_TYPE:-}" || -n "${TOKEN_API_ZEALOTRY:-}" || -n "${TOKEN_API_DISCORD_HOSTED:-}" || -n "${TOKEN_API_DISCORD_CHANNEL:-}" || -n "${TOKEN_API_DISCORD_BOT:-}" || -n "${TOKEN_API_DISPATCH_MCP:-}" || -n "${TOKEN_API_DISPATCH_WITH_BROWSER:-}" || -n "${TOKEN_API_DISPATCH_WITH_DESKTOP:-}" || -n "${TOKEN_API_DISPATCH_MCP_LIST:-}" ]]; then
  JQ_FILTER=".env //= {} | .env"
  [[ -n "${SSH_CLIENT:-}" ]] && JQ_FILTER="$JQ_FILTER + {SSH_CLIENT: \$ssh}"
  [[ -n "${TMUX:-}" ]] && JQ_FILTER="$JQ_FILTER + {TMUX: \$tmux}"
  [[ -n "${TMUX_PANE:-}" ]] && JQ_FILTER="$JQ_FILTER + {TMUX_PANE: \$tmux_pane}"
  [[ -n "${TOKEN_API_PANE_LABEL:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_PANE_LABEL: \$pane_label}"
  [[ -n "${TOKEN_API_PERSONA:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_PERSONA: \$persona}"
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
  [[ -n "${TOKEN_API_WRAPPER_ID:-}" ]] && JQ_FILTER="$JQ_FILTER + {TOKEN_API_WRAPPER_ID: \$wrapper_id}"
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
    --arg pane_label "${TOKEN_API_PANE_LABEL:-}" \
    --arg persona "${TOKEN_API_PERSONA:-}" \
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
    --arg wrapper_id "${TOKEN_API_WRAPPER_ID:-}" \
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
  CURRENT=$(ps -o ppid= -p "$CURRENT" 2>/dev/null | tr -d ' ') || true
done

# Inject PID into payload
if [ -n "$CLAUDE_PID" ]; then
  HOOK_INPUT=$(echo "$HOOK_INPUT" | jq -c --arg pid "$CLAUDE_PID" '.pid = ($pid | tonumber)') || true
fi

# Resolve tmux pane via PID walk when env var is stripped (Claude Code strips $TMUX_PANE)
# Inject into payload so Token-API can store it for cross-machine dispatch
if [[ -z "${TMUX_PANE:-}" ]] && [[ -n "$CLAUDE_PID" ]] && [[ "$ACTION_TYPE" == "SessionStart" ]]; then
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
if [[ -n "${TMUX_PANE:-}" ]] && [[ "$ACTION_TYPE" == "SessionStart" ]]; then
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
  SESSION_ID=$(echo "$HOOK_INPUT" | jq -r '.session_id // empty' 2>/dev/null) || true
  if [ -n "$SESSION_ID" ]; then
    if [ "$ACTION_TYPE" = "SessionStart" ]; then
      # Guarded: this PID cache is a best-effort worktree-setup transplant aid.
      # An unguarded mkdir/write here aborted a bare SessionStart pre-POST under
      # errexit (the visible http=? reg-root) — it must never strand the row.
      mkdir -p "${HOME}/.claude/session-pids" 2>/dev/null || true
      echo "$SESSION_ID" > "${HOME}/.claude/session-pids/${CLAUDE_PID}" 2>/dev/null || true
    elif [ "$ACTION_TYPE" = "SessionEnd" ]; then
      rm -f "${HOME}/.claude/session-pids/${CLAUDE_PID}" 2>/dev/null || true
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
        TRANSPLANT_FROM=$(cat "$HANDOFF_TMP" 2>/dev/null) || true
        rm -f "$HANDOFF_TMP" 2>/dev/null || true
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
  TRANSCRIPT_PATH=$(echo "$HOOK_INPUT" | jq -r '.transcript_path // ""' 2>/dev/null) || true
  IS_LOCAL=$([[ "$API_URL" == *"localhost"* || "$API_URL" == *"127.0.0.1"* ]] && echo 1 || echo 0)
  if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ] && [ "$IS_LOCAL" = "0" ]; then
    TRANSCRIPT_TAIL=""
    for _ in 1 2 3 4 5 6 7 8; do
      TAIL=$(tail -n 60 "$TRANSCRIPT_PATH") || true
      if echo "$TAIL" | grep -q '"type":"text"'; then
        TRANSCRIPT_TAIL="$TAIL"
        break
      fi
      sleep 0.25
    done
    # Fallback: use raw tail even without "type":"text" (short sessions, tool-only output)
    if [ -z "$TRANSCRIPT_TAIL" ]; then
      TRANSCRIPT_TAIL=$(tail -n 60 "$TRANSCRIPT_PATH" 2>/dev/null) || true
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
  RESP_FILE="${HOME}/.claude/logs/.pretooluse-resp.$$"
  HTTP_CODE=$(echo "${HOOK_INPUT}" | \
    curl -s -o "$RESP_FILE" -w '%{http_code}' --connect-timeout 2 --max-time 3 \
      -X POST "${API_URL}/api/hooks/${ACTION_TYPE}" \
      -H "Content-Type: application/json" \
      -d @- 2>/dev/null) || true
  RESPONSE=$(cat "$RESP_FILE" 2>/dev/null || echo "")
  rm -f "$RESP_FILE" 2>/dev/null || true

  if [[ "$HTTP_CODE" == "000" ]]; then
    _enqueue_hook_token_api_post "$ACTION_TYPE" "$HOOK_INPUT" "${API_URL}/api/hooks/${ACTION_TYPE}" "http-000" || true
  fi

  if echo "$RESPONSE" | grep -q '"permissionDecision"'; then
    echo "$RESPONSE"
  fi

  # Execute local_exec side-effect if present (e.g., AHK for voice chat)
  LOCAL_EXEC=$(echo "$RESPONSE" | jq -r '.local_exec // empty' 2>/dev/null) || true
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
  if [[ -n "${TMUX_PANE:-}" ]]; then
    SAFE_PANE="${TMUX_PANE#%}"
    rm -f "/tmp/claude-panes/flush-${SAFE_PANE}.ts" 2>/dev/null || true
  fi

  # SessionStart carries the pane's registration. It is the ONLY registration
  # path a bare launch has until the next full `tx restart`, so a single dropped
  # POST (a momentary token-api hiccup — restart race, brief EMFILE fd
  # exhaustion) strands the pane with no registry row. Retry with a bounded
  # backoff so a transient server blip self-recovers in-band. --retry-max-time
  # caps the total retry window so a genuinely-down server still can't block
  # Claude's startup for long.
  #
  # Capture BOTH the body and the HTTP status: `curl -s` does not fail on a 503
  # (only -f does), so the server's bounded fail-loud (503 on a write that could
  # not self-heal) would otherwise be swallowed exactly like a 200. We validate
  # the reply below and only a 2xx + {"success": true} counts as a bound row.
  RESP_FILE="${HOME}/.claude/logs/.sessionstart-resp.$$"
  HTTP_CODE=$(echo "${HOOK_INPUT}" | \
    curl -s -o "$RESP_FILE" -w '%{http_code}' \
      --connect-timeout 2 --max-time 5 \
      --retry 3 --retry-connrefused --retry-delay 1 --retry-max-time 12 \
      -X POST "${API_URL}/api/hooks/${ACTION_TYPE}" \
      -H "Content-Type: application/json" \
      -d @- 2>/dev/null) || true
  RESPONSE=$(cat "$RESP_FILE" 2>/dev/null || echo "")
  rm -f "$RESP_FILE" 2>/dev/null || true

  # Validate the registration actually bound a row. A confirmed reply is a 2xx
  # whose body is {"success": true} (action registered / already_registered /
  # reregistered / supplanted / transplant_refreshed). Anything else — a 503
  # fail-loud, a connection-refused empty body, a read timeout — is a real
  # failure on the sole registration path of a bare launch, so surface it
  # (the gated trap then exits non-zero); never let it pass as a silent strand.
  REG_SUCCESS=$(echo "$RESPONSE" | jq -r '.success // false' 2>/dev/null || echo false)
  if [[ "$HTTP_CODE" == 2* && "$REG_SUCCESS" == "true" ]]; then
    REGISTRATION_OK=1
  elif [[ "$HTTP_CODE" == "000" ]] && _enqueue_hook_token_api_post "$ACTION_TYPE" "$HOOK_INPUT" "${API_URL}/api/hooks/${ACTION_TYPE}" "http-000"; then
    # Durable recovery path accepted: the hook's own SessionStart intent will be
    # replayed by the recovery-triggered drainer. This is intentionally NOT a
    # confirmed registration; keep the #436 loud nonzero path while making the
    # survivable queue state visible and durable.
    REGISTRATION_QUEUED=1
    echo "token-api SessionStart registration queued for replay: token-api unreachable (http=000)" >&2
  else
    _log_sessionstart_failure "token-api POST ${API_URL}/api/hooks/SessionStart did not confirm a bound row"
  fi

  # Defer auto-rename to next UserPromptSubmit via pending-ui-cmds. Pane tint is
  # DB/persona-resolved and applied by tmux style only; no slash color path.
  TAB_NAME=$(echo "$RESPONSE" | jq -r '.tab_name // empty' 2>/dev/null) || true
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
    http_code=$(echo "${HOOK_INPUT}" | \
      curl -s -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 10 \
        -X POST "${API_URL}/api/hooks/${ACTION_TYPE}" \
        -H "Content-Type: application/json" \
        -d @- 2>/dev/null) || true
    if [[ "$http_code" == "000" ]]; then
      _enqueue_hook_token_api_post "$ACTION_TYPE" "$HOOK_INPUT" "${API_URL}/api/hooks/${ACTION_TYPE}" "http-000" || true
    fi
  ) </dev/null >/dev/null 2>&1 &
  disown 2>/dev/null || true
fi

# Hand off to the gated EXIT trap (_hook_exit): non-critical hooks exit 0 (never
# block Claude Code); a registration-critical SessionStart that never confirmed a
# bound row exits non-zero with a visible failure record instead of silently
# stranding the pane.
exit 0
