#!/usr/bin/env bash
# Shared lifecycle helpers for interactive agent wrappers.
#
# Wrappers keep engine-specific launch semantics, but share the operational
# contract: symlink-safe library loading, wrapper IDs, Token-API hook payloads,
# pane runtime stamps, and pane cleanup.

# shellcheck shell=bash

TOKEN_WRAPPER_LIB_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOKEN_WRAPPER_CLEANUP_LIB="${TOKEN_WRAPPER_LIB_DIR}/tmux-runtime-cleanup.sh"
if [[ -r "$TOKEN_WRAPPER_CLEANUP_LIB" ]]; then
  # shellcheck source=tmux-runtime-cleanup.sh
  source "$TOKEN_WRAPPER_CLEANUP_LIB"
fi

token_wrapper_uuid() {
  if command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr '[:upper:]' '[:lower:]'
  else
    date +%s%N
  fi
}

# Normalize any pane id reaching our ingest boundary to its canonical page:index
# form. tmux hands the child process a physical %NNN via its own $TMUX_PANE
# contract; rather than carry that physical id as the working identity, we
# canonicalize it the moment it enters our space (the inverse of the legacy
# "translate physical->public on the way out" layer).
#
#   already-canonical (page:index)  -> kept verbatim
#   raw %NNN / self / current       -> `tmuxctl resolve-pane --format id`
#   empty / no @PANE_ID role / error -> FAIL OPEN to the input (today's behavior)
#
# Fail-open is deliberate: a pane with no canonical cardinal (e.g. no @PANE_ID
# role stamped) keeps emitting its physical id exactly as before, so this is a
# no-op for unmanaged panes — no regression. Wired into call sites in PR-B.
normalize_pane_to_canonical() {
  local pane="$1" resolved
  [[ -n "$pane" ]] || return 0
  if [[ "$pane" == [a-z]*:* && "$pane" != %* ]]; then
    printf '%s' "$pane"
    return 0
  fi
  if command -v tmuxctl >/dev/null 2>&1; then
    resolved="$(tmuxctl resolve-pane --format id "$pane" 2>/dev/null || true)"
    [[ -n "$resolved" ]] && { printf '%s' "$resolved"; return 0; }
  fi
  printf '%s' "$pane"
}

# Where token_wrapper_post_hook tallies dropped hooks, one TSV line per failure
# (ts \t action \t cause). Cheap instrumentation to quantify how material the
# restart window (conn-refused) is vs the fd-burst path (server-side EMFILE,
# tagged in routes/hooks.py) — `sort -k3 | uniq -c` by cause. Not a suppressor:
# we never gate or dedup on it (see anti-blind-dedup / no-suppress-debounce).
TOKEN_WRAPPER_HOOK_FAILURE_LOG="${TOKEN_WRAPPER_HOOK_FAILURE_LOG:-${HOME}/.claude/logs/hook-post-failures.log}"

token_wrapper_record_hook_failure() {
  local action_type="$1" cause="$2"
  local dir
  dir="$(dirname "$TOKEN_WRAPPER_HOOK_FAILURE_LOG")"
  mkdir -p "$dir" 2>/dev/null || true
  printf '%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$action_type" "$cause" \
    >> "$TOKEN_WRAPPER_HOOK_FAILURE_LOG" 2>/dev/null || true
}

token_wrapper_post_hook() {
  local action_type="$1"
  local payload="$2"
  # Bounded retry belt. launchd socket activation (the primary fix) holds new
  # connections in the kernel accept backlog across a planned restart, so they
  # stall instead of being connection-refused; this belt covers the residuals it
  # can't (backlog overflow under burst, a request killed mid-flight). Flags
  # mirror the SessionStart sender in claude-config/hooks/generic-hook.sh:
  # --retry-connrefused retries the restart window; --retry-max-time caps the
  # total window so a genuinely-down server can't hang the wrapper. No dedup/gate
  # and no idempotency key — there is no replay (SessionStart dedups on
  # session_id at the row level).
  # `&& rc=0 || rc=$?` keeps this set -e safe: a non-zero curl (e.g. rc=7 when the
  # server is down) would otherwise abort a sourcing wrapper that runs under
  # `set -euo pipefail` (dispatch does) right at the assignment, before we can
  # tally the cause. The compound always succeeds, so errexit never fires here.
  local http_code rc
  http_code=$(curl -s -o /dev/null -w '%{http_code}' \
    --connect-timeout 2 --max-time 5 \
    --retry 3 --retry-connrefused --retry-delay 1 --retry-max-time 12 \
    -X POST "${API_URL}/api/hooks/${action_type}" \
    -H "Content-Type: application/json" \
    -d "$payload" 2>/dev/null) && rc=0 || rc=$?

  if [[ "$rc" -eq 0 && "$http_code" == 2* ]]; then
    return 0
  fi

  # Tag the failure cause for the tally above. The EMFILE fd-burst path is
  # tagged server-side (routes/hooks.py); from the client we distinguish the
  # restart window (conn-refused) from a slow/hung accept (timeout) and HTTP
  # errors. Stay fire-and-forget: always return 0 so a dropped hook never breaks
  # the wrapper.
  local cause
  case "$rc" in
    7)  cause="conn-refused" ;;            # ECONNREFUSED: restart window / backlog overflow
    28) cause="timeout" ;;                 # --connect-timeout / --max-time exceeded
    0)  cause="http-${http_code:-000}" ;;  # connected but non-2xx
    *)  cause="other-rc${rc}" ;;
  esac
  token_wrapper_record_hook_failure "$action_type" "$cause"
  return 0
}

token_wrapper_build_payload() {
  local action_type="$1"
  local exit_code="${2:-}"
  jq -nc \
    --arg action "$action_type" \
    --arg wrapper_launch_id "$WRAPPER_LAUNCH_ID" \
    --arg launcher "$LAUNCHER" \
    --arg engine "$ENGINE" \
    --arg cwd "$WORKING_DIR" \
    --arg tmux_pane "$TMUX_PANE_VALUE" \
    --arg ssh_client "${SSH_CLIENT:-}" \
    --arg tmux "${TMUX:-}" \
    --arg token_api_launcher "${TOKEN_API_LAUNCHER:-}" \
    --arg token_api_engine "${TOKEN_API_ENGINE:-}" \
    --arg token_api_dispatch_target "${TOKEN_API_DISPATCH_TARGET:-}" \
    --arg token_api_dispatch_window "${TOKEN_API_DISPATCH_WINDOW:-}" \
    --arg token_api_dispatch_mode "${TOKEN_API_DISPATCH_MODE:-}" \
    --arg token_api_dispatch_slot "${TOKEN_API_DISPATCH_SLOT:-}" \
    --arg token_api_dispatch_resolved_pane "${TOKEN_API_DISPATCH_RESOLVED_PANE:-}" \
    --arg token_api_parent_instance_id "${TOKEN_API_PARENT_INSTANCE_ID:-}" \
    --arg token_api_dispatch_session_doc_path "${TOKEN_API_DISPATCH_SESSION_DOC_PATH:-}" \
    --arg token_api_target_working_dir "${TOKEN_API_TARGET_WORKING_DIR:-}" \
    --arg token_api_launch_mode "${TOKEN_API_LAUNCH_MODE:-}" \
    --arg token_api_transplant_expected "${TOKEN_API_TRANSPLANT_EXPECTED:-}" \
    --arg token_api_instance_type "${TOKEN_API_INSTANCE_TYPE:-}" \
    --arg token_api_zealotry "${TOKEN_API_ZEALOTRY:-}" \
    --arg token_api_dispatch_mcp "${TOKEN_API_DISPATCH_MCP:-}" \
    --arg token_api_dispatch_with_browser "${TOKEN_API_DISPATCH_WITH_BROWSER:-}" \
    --arg token_api_dispatch_with_desktop "${TOKEN_API_DISPATCH_WITH_DESKTOP:-}" \
    --arg token_api_dispatch_mcp_list "${TOKEN_API_DISPATCH_MCP_LIST:-}" \
    --arg token_api_discord_hosted "${TOKEN_API_DISCORD_HOSTED:-}" \
    --arg token_api_discord_channel "${TOKEN_API_DISCORD_CHANNEL:-}" \
    --arg token_api_discord_bot "${TOKEN_API_DISCORD_BOT:-}" \
    --arg token_api_session_id "${TOKEN_API_SESSION_ID:-}" \
    --arg token_api_codex_bridge_id "${TOKEN_API_CODEX_BRIDGE_ID:-}" \
    --arg token_api_codex_profile "${TOKEN_API_CODEX_PROFILE:-}" \
    --arg token_api_claude_model "${TOKEN_API_CLAUDE_MODEL:-}" \
    --arg token_api_persona "${TOKEN_API_PERSONA:-}" \
    --arg token_api_legion "${TOKEN_API_LEGION:-}" \
    --arg token_api_wrapper_launch_id "$WRAPPER_LAUNCH_ID" \
    --argjson pid "$$" \
    --argjson exit_code "${exit_code:-null}" \
    '{
      action: $action,
      wrapper_launch_id: $wrapper_launch_id,
      launcher: $launcher,
      engine: $engine,
      cwd: $cwd,
      tmux_pane: (if $tmux_pane == "" then null else $tmux_pane end),
      pid: $pid,
      exit_code: $exit_code,
      env: {
        SSH_CLIENT: $ssh_client,
        TMUX: $tmux,
        TMUX_PANE: $tmux_pane,
        TOKEN_API_LAUNCHER: $token_api_launcher,
        TOKEN_API_ENGINE: $token_api_engine,
        TOKEN_API_DISPATCH_TARGET: $token_api_dispatch_target,
        TOKEN_API_DISPATCH_WINDOW: $token_api_dispatch_window,
        TOKEN_API_DISPATCH_MODE: $token_api_dispatch_mode,
        TOKEN_API_DISPATCH_SLOT: $token_api_dispatch_slot,
        TOKEN_API_DISPATCH_RESOLVED_PANE: $token_api_dispatch_resolved_pane,
        TOKEN_API_PARENT_INSTANCE_ID: $token_api_parent_instance_id,
        TOKEN_API_DISPATCH_SESSION_DOC_PATH: $token_api_dispatch_session_doc_path,
        TOKEN_API_TARGET_WORKING_DIR: $token_api_target_working_dir,
        TOKEN_API_LAUNCH_MODE: $token_api_launch_mode,
        TOKEN_API_TRANSPLANT_EXPECTED: $token_api_transplant_expected,
        TOKEN_API_INSTANCE_TYPE: $token_api_instance_type,
        TOKEN_API_ZEALOTRY: $token_api_zealotry,
        TOKEN_API_DISPATCH_MCP: $token_api_dispatch_mcp,
        TOKEN_API_DISPATCH_WITH_BROWSER: $token_api_dispatch_with_browser,
        TOKEN_API_DISPATCH_WITH_DESKTOP: $token_api_dispatch_with_desktop,
        TOKEN_API_DISPATCH_MCP_LIST: $token_api_dispatch_mcp_list,
        TOKEN_API_DISCORD_HOSTED: $token_api_discord_hosted,
        TOKEN_API_DISCORD_CHANNEL: $token_api_discord_channel,
        TOKEN_API_DISCORD_BOT: $token_api_discord_bot,
        TOKEN_API_SESSION_ID: $token_api_session_id,
        TOKEN_API_CODEX_BRIDGE_ID: $token_api_codex_bridge_id,
        TOKEN_API_CODEX_PROFILE: $token_api_codex_profile,
        TOKEN_API_CLAUDE_MODEL: $token_api_claude_model,
        TOKEN_API_PERSONA: $token_api_persona,
        TOKEN_API_LEGION: $token_api_legion,
        TOKEN_API_WRAPPER_LAUNCH_ID: $token_api_wrapper_launch_id
      }
    }'
}

token_wrapper_stamp_start() {
  if declare -F tmux_runtime_stamp_wrapper >/dev/null 2>&1; then
    tmux_runtime_stamp_wrapper "$TMUX_PANE_VALUE" "$WRAPPER_LAUNCH_ID" "$ENGINE" "$LAUNCHER" "$WORKING_DIR"
  fi
}

token_wrapper_cleanup_pane() {
  local pane="${1:-$TMUX_PANE_VALUE}"
  local tmuxctl_bin="${TOKEN_WRAPPER_LIB_DIR}/../bin/tmuxctl"
  if [[ -n "$pane" && -x "$tmuxctl_bin" ]]; then
    IMPERIUM_TMUX_AUTOMATION=1 "$tmuxctl_bin" clear-runtime --pane "$pane" >/dev/null 2>&1 && return 0
  elif [[ -n "$pane" ]] && command -v tmuxctl >/dev/null 2>&1; then
    IMPERIUM_TMUX_AUTOMATION=1 tmuxctl clear-runtime --pane "$pane" >/dev/null 2>&1 && return 0
  fi
  if declare -F tmux_runtime_cleanup_pane >/dev/null 2>&1; then
    tmux_runtime_cleanup_pane "$pane"
  elif [[ -n "$pane" ]] && command -v tmux >/dev/null 2>&1; then
    tmux set-option -p -u -t "$pane" @INSTANCE_ID >/dev/null 2>&1 || true
  fi
}

token_wrapper_enforce_stack_if_needed() {
  local pane="${1:-$TMUX_PANE_VALUE}"
  [[ -n "$pane" ]] || return 0
  command -v tmux >/dev/null 2>&1 || return 0
  local meta window_target pane_role pane_type
  meta="$(tmux display-message -p -t "$pane" '#{session_name}:#{window_index}	#{@PANE_ID}	#{@PANE_TYPE}' 2>/dev/null || true)"
  [[ -n "$meta" ]] || return 0
  IFS=$'\t' read -r window_target pane_role pane_type <<< "$meta"
  case "$pane_role" in
    council:custodes|mechanicus:fabricator-general|council:administratum)
      return 0
      ;;
  esac
  if [[ "$pane_type" != "stack-worker" && "$pane_role" != "mechanicus:worker" && ! "$pane_role" =~ ^mechanicus:[1-9][0-9]*$ ]]; then
    return 0
  fi
  (
    local tmuxctl_bin="${TOKEN_WRAPPER_LIB_DIR}/../bin/tmuxctl"
    if [[ -x "$tmuxctl_bin" ]]; then
      IMPERIUM_TMUX_AUTOMATION=1 "$tmuxctl_bin" stack enforce --window "$window_target" --kill-pending-clear
    else
      IMPERIUM_TMUX_AUTOMATION=1 tmuxctl stack enforce --window "$window_target" --kill-pending-clear
    fi
  ) >/dev/null 2>&1 &
}

token_wrapper_start() {
  local start_payload
  start_payload="$(token_wrapper_build_payload "WrapperStart")"
  token_wrapper_post_hook "WrapperStart" "$start_payload"
  token_wrapper_stamp_start
}

token_wrapper_end() {
  local exit_code="${1:-0}"
  local end_payload
  end_payload="$(token_wrapper_build_payload "WrapperEnd" "$exit_code")"
  token_wrapper_post_hook "WrapperEnd" "$end_payload"
  token_wrapper_enforce_stack_if_needed "$TMUX_PANE_VALUE"
}
