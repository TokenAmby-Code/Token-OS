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
#   1. Compose the rank+persona doctrine staple — ONE source of truth, the shared
#      agent-wrapper-common.sh:token_wrapper_compose_system_text.
#   2. Fire an async, fire-and-forget audit ping (no blocking POST / no retry belt
#      on the hot path — the blocking 12s close-POST in the heavy wrapper is the
#      pane-reap-slowness cause this shim is built to avoid).
#   3. `exec` the engine so the AGENT becomes the pane process. Agent-exit then
#      surfaces as `pane-died` immediately → the daemon reaps async. No cleanup
#      trap, no token_wrapper_end, no lingering wrapper parent.
#
# Registration is NOT done here: the agent's own SessionStart hook
# (POST /api/hooks/SessionStart) creates/reactivates the registry row, keyed on
# the stable pane label. token-api owns the single sqlite writer; this shim never
# touches agents.db.

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

# --- resolve the REAL engine binary (bypass the front-door launch wrapper) ------
# Mirrors agent-wrapper.sh:resolve_engine_binary — prefer the *.token-os-real
# binary, never re-enter a wrapper shim (which would stall the pane reap again).
resolve_real_engine() {
  local engine="$1" candidate found
  case "$engine" in
    claude)
      set -- "${CLAUDE_BIN:-}" \
        "${HOME}/.local/bin/claude.token-os-real"
      ;;
    codex)
      set -- "${CODEX_BIN:-}" \
        "/opt/homebrew/bin/co""dex.token-os-real"
      ;;
    *)
      set --
      ;;
  esac
  for candidate in "$@"; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if [[ "$candidate" != *.token-os-real ]] \
        && grep -q 'agent-wrapper.sh' "$candidate" 2>/dev/null; then
      continue
    fi
    printf '%s' "$candidate"
    return 0
  done
  found="$(command -v "$engine" 2>/dev/null || true)"
  [[ -n "$found" && -x "$found" ]] && printf '%s' "$found"
}

# `|| true` so a no-match return doesn't trip `set -e` before the debug-shell fallback.
ENGINE_BIN="$(resolve_real_engine "$ENGINE" || true)"
if [[ -z "$ENGINE_BIN" || ! -x "$ENGINE_BIN" ]]; then
  echo "persona-seat: $ENGINE binary not found (looked for *.token-os-real and PATH)" >&2
  # Park an interactive shell so the pane is debuggable rather than churn-respawning
  # against a genuinely-missing engine; an operator will see the seat is empty.
  exec "${SHELL:-/bin/bash}" -l
fi

export TOKEN_API_AGENT_WRAPPER_BYPASS=1
export TOKEN_API_WRAPPER_ID="$WRAPPER_ID"
export TOKEN_API_ENGINE="$ENGINE"
export TOKEN_API_LAUNCHER="$LAUNCHER"
if declare -F tmux_runtime_stamp_wrapper >/dev/null 2>&1; then
  tmux_runtime_stamp_wrapper "$TMUX_PANE_VALUE" "$WRAPPER_ID" "$ENGINE" "$LAUNCHER" "$WORKING_DIR"
fi

if [[ "$ENGINE" == "codex" ]]; then
  PREAMBLE=""
  if declare -F token_wrapper_codex_system_preamble >/dev/null 2>&1; then
    PREAMBLE="$(token_wrapper_codex_system_preamble 2>/dev/null || true)"
  fi
  bypass_flag="--dangerously-bypass-approvals-and-sandbox"
  [[ "${CODEX_DANGEROUS_BYPASS:-1}" == "1" ]] || bypass_flag="--full-auto"
  codex_argv=()
  [[ -n "${TOKEN_API_CODEX_PROFILE:-}" ]] && codex_argv+=(--profile "$TOKEN_API_CODEX_PROFILE")
  codex_argv+=("$bypass_flag")
  [[ -n "$PREAMBLE" ]] && codex_argv+=("$PREAMBLE")
  exec "$ENGINE_BIN" "${codex_argv[@]}"
fi

# --- claude ---------------------------------------------------------------------
# Compose the rank+persona staple (ONE source of truth) only on the claude path —
# the codex branch above builds its own preamble via token_wrapper_codex_system_preamble.
# Synchronous on purpose: identity must be in hand before exec. Fail-open — an empty
# staple launches the agent without the doctrine layer rather than bricking the seat.
STAPLE=""
if declare -F token_wrapper_compose_system_text >/dev/null 2>&1; then
  STAPLE="$(token_wrapper_compose_system_text 2>/dev/null || true)"
fi

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

[[ -n "$STAPLE" ]] && claude_argv+=(--append-system-prompt "$STAPLE")

exec "$ENGINE_BIN" "${claude_argv[@]}"
