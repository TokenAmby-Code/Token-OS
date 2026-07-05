#!/usr/bin/env bash
# persona-seat.sh — the thin, exec-ing persona seat shim.
#
# `tmuxctl assertions.launch_persona_seat` respawns this into a vacated singleton
# persona pane — the daemon-native replacement for shelling `dispatch` (which
# `exit 73`s on the six protected singleton-seat labels). Usage:
#
#     persona-seat.sh <engine>        # engine = claude | codex
#
# with the seat's identity supplied in the environment by the respawn command
# (TOKEN_API_PERSONA / …CLAUDE_MODEL / …DISPATCH_SESSION_DOC_PATH /
# …TARGET_WORKING_DIR / …WRAPPER_ID / …INSTANCE_TYPE).
#
# What it does, and deliberately does NOT do:
#   1. Assemble persona-seat-specific argv/env (profile overlays, model, standby
#      prompt) for the tracked agent wrapper front-door.
#   2. Fire an async, fire-and-forget audit ping for seat-launch observability.
#   3. `exec` the tracked agent wrapper, never a raw engine binary. Emperor ruling
#      2026-07-05: the wrapper is the sole caller of claude/codex engine binaries
#      and the sole wrapper-ledger producer.
#
# Registration is NOT done here: the wrapper and the agent's own SessionStart hook
# create/reactivate ledger and registry rows. token-api owns the sqlite writer;
# this shim never touches agents.db.

set -euo pipefail

ENGINE="${1:-claude}"

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
while [[ -L "$SCRIPT_PATH" ]]; do
  SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  [[ "$SCRIPT_PATH" == /* ]] || SCRIPT_PATH="${SCRIPT_DIR}/${SCRIPT_PATH}"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
COMMON_LIB="${SCRIPT_DIR}/../lib/agent-wrapper-common.sh"
# shellcheck source=../lib/agent-wrapper-common.sh
[[ -r "$COMMON_LIB" ]] && source "$COMMON_LIB"
NAS_PATH_LIB="${SCRIPT_DIR}/../lib/nas-path.sh"
# shellcheck source=../lib/nas-path.sh
[[ -f "$NAS_PATH_LIB" ]] && source "$NAS_PATH_LIB" 2>/dev/null || true
RUNTIME_CLEANUP_LIB="${SCRIPT_DIR}/../lib/tmux-runtime-cleanup.sh"
# shellcheck source=../lib/tmux-runtime-cleanup.sh
[[ -f "$RUNTIME_CLEANUP_LIB" ]] && source "$RUNTIME_CLEANUP_LIB" 2>/dev/null || true

# Minimal lifecycle context the shared compose/warn helpers reference. Defined so
# any fail-open path inside the common lib (e.g. token_wrapper_warn_missing_system_doc)
# has the values it expects without us pulling in the full wrapper.
API_URL="${TOKEN_API_URL:-http://localhost:7777}"
LAUNCHER="${TOKEN_API_LAUNCHER:-persona-seat}"
ENGINE_LABEL="${TOKEN_API_ENGINE:-$ENGINE}"
WORKING_DIR="${TOKEN_API_TARGET_WORKING_DIR:-$(pwd)}"
TMUX_PANE_VALUE="${TMUX_PANE:-}"
WRAPPER_ID="${TOKEN_API_WRAPPER_ID:-${TOKEN_API_WRAPPER_LAUNCH_ID:-}}"
CLAUDE_MODEL="${TOKEN_API_CLAUDE_MODEL:-}"
PERSONA="${TOKEN_API_PERSONA:-}"

# --- local execution log (non-blocking backstop for the fire-and-forget ping) --
# The audit ping below discards its result, so token-api downtime would otherwise
# erase all launch telemetry. A best-effort local append keeps a breadcrumb on the
# seat host without ever blocking or failing the hot path.
PERSONA_SEAT_LOG="${PERSONA_SEAT_LOG:-${HOME}/.claude/logs/persona-seat.log}"
persona_seat_log() {
  local dir
  dir="$(dirname "$PERSONA_SEAT_LOG")"
  mkdir -p "$dir" 2>/dev/null || true
  printf '%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" \
    >> "$PERSONA_SEAT_LOG" 2>/dev/null || true
}
persona_seat_log "launch persona=${PERSONA:-?} engine=${ENGINE} pane=${TMUX_PANE_VALUE:-?} wrapper_id=${WRAPPER_ID:-?}"

# --- async, fire-and-forget audit ping (no retry belt on the hot path) ---------
persona_seat_audit_ping() {
  local payload http_code
  payload="$(printf '{"persona":"%s","engine":"%s","wrapper_id":"%s","tmux_pane":"%s","launcher":"persona-seat"}' \
    "$PERSONA" "$ENGINE" "$WRAPPER_ID" "$TMUX_PANE_VALUE")"
  http_code=$(curl -s -o /dev/null -w '%{http_code}' --connect-timeout 1 --max-time 3 \
    -X POST "${API_URL}/api/hooks/PersonaSeatLaunch" \
    -H 'Content-Type: application/json' -d "$payload" 2>/dev/null) || true
  if [[ "$http_code" == 2* ]]; then
    return 0
  fi
  persona_seat_log "audit-ping-failed persona=${PERSONA:-?} engine=${ENGINE} http=${http_code:-?}"
  if [[ "$http_code" == "000" ]]; then
    token_wrapper_enqueue_hook_post "PersonaSeatLaunch" "$payload" "http-000" || true
  fi
}
persona_seat_audit_ping & disown

# --- enter the tracked agent wrapper front-door ------------------------------
# Emperor ruling (2026-07-05): persona-seat may assemble seat-specific argv, but
# it must never invoke raw claude/codex binaries. The tracked agent wrapper is the
# sole caller of engine binaries and the authoritative wrapper-ledger producer.
AGENT_WRAPPER="${SCRIPT_DIR}/agent-wrapper.sh"
if [[ ! -x "$AGENT_WRAPPER" ]]; then
  echo "persona-seat: agent wrapper not executable: $AGENT_WRAPPER" >&2
  exec "${SHELL:-/bin/bash}" -l
fi

export TOKEN_API_WRAPPER_ID="$WRAPPER_ID"
export TOKEN_API_ENGINE="$ENGINE"
export TOKEN_API_LAUNCHER="$LAUNCHER"
# Force codex through the managed-launch branch in agent-wrapper.sh even though
# the launcher is persona-seat, not dispatch. That branch adds cwd/bypass flags
# and injects the wrapper-owned system preamble exactly once.
export TOKEN_API_INTERNAL_DISPATCH=1

if [[ "$ENGINE" == "codex" ]]; then
  exec "$AGENT_WRAPPER" codex
fi

# --- claude ---------------------------------------------------------------------
claude_argv=(--dangerously-skip-permissions)
[[ -n "$CLAUDE_MODEL" ]] && claude_argv+=(--model "$CLAUDE_MODEL")

# Persona profile overlays (MCP / settings / disallowed-tools), matching dispatch
# so a daemon-seated persona keeps the exact capability coat it had under dispatch.
if [[ -n "$PERSONA" ]]; then
  profile_dir="$HOME/.claude/profiles/$PERSONA"
  if [[ -d "$profile_dir" ]]; then
    [[ -f "$profile_dir/.mcp.json" ]] && claude_argv+=(--mcp-config "$profile_dir/.mcp.json" --strict-mcp-config)
    if [[ -f "$profile_dir/disallowed-tools.txt" ]]; then
      while IFS= read -r line; do
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        claude_argv+=(--disallowedTools "$line")
      done < "$profile_dir/disallowed-tools.txt"
    fi
    [[ -f "$profile_dir/settings.json" ]] && claude_argv+=(--settings "$profile_dir/settings.json")
  fi
fi

# Reservist standby prompt (the "keep the pulse" instruction) rides as the engine's
# first message. Set only for reservist seats; persona seats never pass it, so this
# is a no-op on the persona path (regression-safe).
[[ -n "${TOKEN_API_SEAT_INITIAL_PROMPT:-}" ]] && claude_argv+=("$TOKEN_API_SEAT_INITIAL_PROMPT")

exec "$AGENT_WRAPPER" claude "${claude_argv[@]}"
