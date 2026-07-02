#!/usr/bin/env bash

set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]:-$0}"
while [[ -L "$SCRIPT_PATH" ]]; do
  SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  [[ "$SCRIPT_PATH" == /* ]] || SCRIPT_PATH="${SCRIPT_DIR}/${SCRIPT_PATH}"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SCRIPT_PATH")" && pwd)"
COMMON_LIB="${SCRIPT_DIR}/../lib/agent-wrapper-common.sh"
if [[ ! -r "$COMMON_LIB" ]]; then
  echo "agent wrapper common library not found: $COMMON_LIB" >&2
  exit 127
fi
# shellcheck source=../lib/agent-wrapper-common.sh
source "$COMMON_LIB"
NAS_PATH_LIB="${SCRIPT_DIR}/../lib/nas-path.sh"
if [[ -f "$NAS_PATH_LIB" ]]; then
  # shellcheck source=../lib/nas-path.sh
  source "$NAS_PATH_LIB" 2>/dev/null || true
fi

ENGINE_ARG="${1:-}"
case "$ENGINE_ARG" in
  claude|codex) shift ;;
  *)
    echo "Usage: $0 claude|codex [engine-args...]" >&2
    exit 64
    ;;
esac

API_URL="${TOKEN_API_URL:-http://localhost:7777}"
LAUNCHER="${TOKEN_API_LAUNCHER:-$ENGINE_ARG}"
ENGINE="${TOKEN_API_ENGINE:-$ENGINE_ARG}"
WORKING_DIR="$(pwd)"
TMUX_PANE_VALUE="${TOKEN_API_DISPATCH_RESOLVED_PANE:-${TMUX_PANE:-}}"
DISPATCH_TARGET_WINDOW="${TOKEN_API_PRINT_REDIRECT_WINDOW:-main:mechanicus}"
WRAPPER_ID="${TOKEN_API_WRAPPER_ID:-${TOKEN_API_WRAPPER_LAUNCH_ID:-$(token_wrapper_uuid)}}"
WRAPPER_CHILD_PID=""
WRAPPER_CLEANUP_DONE=0

wrapper_mode_cleanup() {
  :
}

wrapper_cleanup() {
  local exit_code="${1:-$?}"
  trap - EXIT
  trap '' INT TERM HUP
  if [[ "${WRAPPER_CLEANUP_DONE:-0}" -eq 1 ]]; then
    exit "$exit_code"
  fi
  WRAPPER_CLEANUP_DONE=1
  set +e
  wrapper_mode_cleanup
  token_wrapper_end "$exit_code"
  exit "$exit_code"
}

wrapper_forward_signal() {
  local sig="$1"
  local child="${WRAPPER_CHILD_PID:-}"
  if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
    kill "-$sig" "$child" 2>/dev/null || true
  fi
}

wrapper_wait_child() {
  local status final_status child shell_opts="$-"
  set +e
  while true; do
    child="$WRAPPER_CHILD_PID"
    wait "$child"
    status=$?
    if kill -0 "$child" 2>/dev/null; then
      continue
    fi
    if [[ "$status" -ge 128 ]]; then
      wait "$child"
      final_status=$?
      [[ "$final_status" -ne 127 ]] && status=$final_status
    fi
    WRAPPER_CHILD_PID=""
    [[ "$shell_opts" == *e* ]] && set -e
    return "$status"
  done
}

wrapper_run_child() {
  # Hand the engine child the real controlling TTY on stdin. POSIX redirects an
  # async list's stdin to /dev/null UNLESS it is explicitly redirected, so a bare
  # `"$@" &` gives the child stdin=/dev/null=non-TTY: codex aborts with "stdin is
  # not a terminal" and claude silently tolerates it (masking the bug). The
  # explicit `<&0` dups the wrapper's own stdin (the pane pty) into the child,
  # overriding that default while preserving the background + wait machinery so
  # the INT/TERM/HUP forwarding traps keep working for signals sent to the
  # wrapper alone (e.g. tmuxctld kills) — not just to the foreground group.
  "$@" <&0 &
  WRAPPER_CHILD_PID=$!
  wrapper_wait_child
}

is_token_wrapper_file() {
  local path="$1"
  [[ -f "$path" ]] || return 1
  grep -q 'agent-wrapper.sh' "$path" 2>/dev/null
}

wrapper_real_candidates() {
  local engine="$1"
  case "$engine" in
    claude)
      [[ -n "${CLAUDE_BIN:-}" ]] && printf '%s\n' "$CLAUDE_BIN"
      printf '%s\n' \
        "$HOME/.local/bin/claude.token-os-real" \
        "$HOME/.local/bin/claude"
      ;;
    codex)
      [[ -n "${CODEX_BIN:-}" ]] && printf '%s\n' "$CODEX_BIN"
      printf '%s\n' \
        "/opt/homebrew/bin/codex.token-os-real" \
        "/opt/homebrew/bin/codex"
      ;;
  esac
}

resolve_engine_binary() {
  local engine="$1" candidate found=""
  while IFS= read -r candidate; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if [[ "$candidate" != *.token-os-real ]] && is_token_wrapper_file "$candidate"; then
      continue
    fi
    printf '%s' "$candidate"
    return 0
  done < <(wrapper_real_candidates "$engine")

  found="$(command -v "$engine" 2>/dev/null || true)"
  if [[ -n "$found" && -x "$found" ]]; then
    printf '%s' "$found"
    return 0
  fi
  return 1
}

run_engine_binary() {
  local engine="$1" bin
  shift
  bin="$(resolve_engine_binary "$engine")" || {
    echo "$engine binary not found" >&2
    exit 127
  }
  wrapper_run_child env TOKEN_API_AGENT_WRAPPER_BYPASS=1 "$bin" "$@"
}

cleanup_common() {
  wrapper_cleanup "$?"
}

run_claude() {
  local print_mode=false skip_next=0 arg
  local -a redirect_args=()
  for arg in "$@"; do
    if [[ "$skip_next" -eq 1 ]]; then
      skip_next=0
      continue
    fi
    case "$arg" in
      -p|--print)
        print_mode=true
        ;;
      --output-format|--input-format|--json-schema|--max-budget-usd|--include-partial-messages|--replay-user-messages|--no-session-persistence)
        # Print-mode-only flags are dropped when redirecting into an interactive pane.
        if [[ "$arg" == "--output-format" || "$arg" == "--input-format" || "$arg" == "--json-schema" || "$arg" == "--max-budget-usd" ]]; then
          skip_next=1
        fi
        ;;
      *)
        redirect_args+=("$arg")
        ;;
    esac
  done

  if $print_mode; then
    if ! command -v tmux >/dev/null 2>&1; then
      echo "claude -p redirect requires tmux" >&2
      exit 1
    fi

    local quoted_agent_wrapper quoted_workdir quoted_launcher quoted_engine quoted_wrapper_id
    local quoted_discord_hosted quoted_discord_channel quoted_discord_bot cmd
    quoted_agent_wrapper="$(printf '%q' "${SCRIPT_DIR}/agent-wrapper.sh")"
    quoted_workdir="$(printf '%q' "$WORKING_DIR")"
    quoted_launcher="$(printf '%q' "$LAUNCHER")"
    quoted_engine="$(printf '%q' "$ENGINE")"
    quoted_wrapper_id="$(printf '%q' "$WRAPPER_ID")"
    quoted_discord_hosted="$(printf '%q' "${TOKEN_API_DISCORD_HOSTED:-}")"
    quoted_discord_channel="$(printf '%q' "${TOKEN_API_DISCORD_CHANNEL:-}")"
    quoted_discord_bot="$(printf '%q' "${TOKEN_API_DISCORD_BOT:-}")"
    cmd="cd $quoted_workdir && TOKEN_API_LAUNCHER=$quoted_launcher TOKEN_API_ENGINE=$quoted_engine TOKEN_API_WRAPPER_ID=$quoted_wrapper_id TOKEN_API_DISCORD_HOSTED=$quoted_discord_hosted TOKEN_API_DISCORD_CHANNEL=$quoted_discord_channel TOKEN_API_DISCORD_BOT=$quoted_discord_bot $quoted_agent_wrapper claude --dangerously-skip-permissions"
    for arg in "${redirect_args[@]}"; do
      cmd+=" $(printf '%q' "$arg")"
    done

    local dispatch_session="main" dispatch_base="$DISPATCH_TARGET_WINDOW" tmuxctl_bin pane_id
    if [[ "$dispatch_base" == *:* ]]; then
      dispatch_session="${dispatch_base%%:*}"
      dispatch_base="${dispatch_base#*:}"
    fi
    dispatch_base="${dispatch_base%%(*}"
    case "$dispatch_base" in
      mechanicus|mars|kreig|reservists) ;;
      *)
        echo "claude -p redirect target must be a managed stack window, got: $DISPATCH_TARGET_WINDOW" >&2
        exit 1
        ;;
    esac

    tmuxctl_bin="$(cd "${SCRIPT_DIR}/../bin" && pwd)/tmuxctl"
    pane_id="$(
      IMPERIUM_TMUX_AUTOMATION=1 "$tmuxctl_bin" stack dispatch "$dispatch_base" \
        --session "$dispatch_session" \
        --cwd "$WORKING_DIR" \
        --no-focus \
        --command "$cmd" 2>/dev/null
    )" || {
      echo "failed to dispatch print-mode agent to $DISPATCH_TARGET_WINDOW" >&2
      exit 1
    }
    echo "redirected claude -p to $pane_id"
    exit 0
  fi

  trap cleanup_common EXIT
  trap 'wrapper_forward_signal INT' INT
  trap 'wrapper_forward_signal TERM' TERM
  trap 'wrapper_forward_signal HUP' HUP
  sync_shared_skills
  token_wrapper_start
  export TOKEN_API_WRAPPER_ID="$WRAPPER_ID"

  # Bake the rank+persona system-doc staple into claude's system prompt. This is
  # the single injection point for every Claude launch surface (workers +
  # singletons). We MERGE with any --append-system-prompt the caller already
  # passed (collect their values, re-emit one combined flag) and leave
  # --system-prompt untouched, so the staple layers under both rather than
  # dropping a caller-supplied prompt.
  local wrapper_system_text caller_append="" expect_append=0 carg
  local -a claude_argv=()
  wrapper_system_text="$(token_wrapper_compose_system_text || true)"
  for carg in "$@"; do
    if [[ "$expect_append" -eq 1 ]]; then
      expect_append=0
      caller_append+="${caller_append:+$'\n\n'}$carg"
      continue
    fi
    case "$carg" in
      --append-system-prompt)
        expect_append=1
        ;;
      --append-system-prompt=*)
        caller_append+="${caller_append:+$'\n\n'}${carg#--append-system-prompt=}"
        ;;
      *)
        claude_argv+=("$carg")
        ;;
    esac
  done
  local final_append=""
  [[ -n "$wrapper_system_text" ]] && final_append="$wrapper_system_text"
  if [[ -n "$caller_append" ]]; then
    final_append="${final_append:+$final_append$'\n\n'}$caller_append"
  fi
  [[ -n "$final_append" ]] && claude_argv+=(--append-system-prompt "$final_append")

  run_engine_binary claude ${claude_argv[@]+"${claude_argv[@]}"} 2> >(grep -v 'Overriding existing handler for signal' >&2)
}

strip_ansi() {
  sed -E \
    -e 's/\x1b\[[0-9;?]*[a-zA-Z]//g' \
    -e 's/\x1b\[[<>][0-9;]*[a-zA-Z]//g' \
    -e 's/\x1b\][0-9]*;[^[:cntrl:]]*(\x07|\x1b\\)//g' \
    -e 's/\x1b\?[0-9;]*[a-zA-Z]//g' \
    -e 's/\x1b[=><]//g' \
    -e 's/\x1b[()][AB012]//g' \
    -e 's/\x1b\[[0-9]*[;rHJ]//g'
}

codex_legacy_subagent_mode() {
  [[ $# -ge 3 ]] || return 1
  [[ "$1" != -* ]] || return 1
  [[ "$2" == */* || "$2" == *.log ]] || return 1
  [[ -x "$3" || "$3" == */* ]] || return 1
  return 0
}

run_codex_legacy_subagent() {
  local agent_id="$1" log_file="$2" codex_path prompt_arg command_str command_display prompt_file status end_timestamp start_timestamp
  shift 2
  if ! command -v script >/dev/null 2>&1; then
    echo "agent-wrapper.sh codex requires the 'script' utility for TTY-preserving logging." >&2
    exit 65
  fi

  mkdir -p "$(dirname -- "$log_file")"
  codex_path="$1"
  shift
  prompt_arg="$*"

  if [[ "$prompt_arg" =~ ^@FILE:(.+)$ ]]; then
    prompt_file="${BASH_REMATCH[1]}"
    if [[ ! -f "$prompt_file" ]]; then
      echo "Error: Prompt file not found: $prompt_file" >&2
      exit 66
    fi
    command_str="$(cat "$prompt_file")"
    command_display="@FILE:${prompt_file}"
  else
    command_str="$prompt_arg"
    command_display="$prompt_arg"
  fi

  start_timestamp="$(date -Iseconds)"
  {
    echo "=== Codex Agent ${agent_id} ==================================="
    echo "Command: ${command_display}"
    if [[ "$prompt_arg" =~ ^@FILE:(.+)$ ]]; then
      echo "Prompt source: file ($prompt_file)"
      echo "Prompt length: $(wc -c < "$prompt_file") bytes"
    fi
    echo "Started: ${start_timestamp}"
    echo "==============================================================="
  } >>"$log_file"

  local temp_log
  temp_log="$(mktemp)"
  wrapper_mode_cleanup() {
    rm -f "$temp_log"
  }
  trap cleanup_common EXIT
  trap 'wrapper_forward_signal INT' INT
  trap 'wrapper_forward_signal TERM' TERM
  trap 'wrapper_forward_signal HUP' HUP
  token_wrapper_start
  export TOKEN_API_WRAPPER_ID="$WRAPPER_ID"

  set +e
  wrapper_run_child env TOKEN_API_AGENT_WRAPPER_BYPASS=1 script -a -f -e -c "$codex_path $(printf '%q' "$command_str")" "$temp_log"
  status=$?
  set -e

  strip_ansi <"$temp_log" >>"$log_file"
  end_timestamp="$(date -Iseconds)"
  {
    echo "Finished: ${end_timestamp}"
    echo "Exit code: ${status}"
    echo ""
  } >>"$log_file"

  exit "$status"
}

sync_shared_skills() {
  # Keep shared skill roots repaired before any managed agent launch. The
  # engines expose them differently (`/skill` in Claude, `$skill` in Codex),
  # but the source of truth and root sync are intentionally unified. Launch-time
  # sync skips command shims so a worker cannot mutate slash-command plumbing.
  [[ "${TOKEN_WRAPPER_SYNC_SHARED_SKILLS:-1}" == "1" ]] || return 0
  local skills_sync="${SCRIPT_DIR}/../bin/skills-sync"
  [[ -x "$skills_sync" ]] || return 0
  local sync_stderr sync_rc=0
  sync_stderr="$(mktemp "${TMPDIR:-/tmp}/codex-skills-sync.XXXXXX")" || return 0
  "$skills_sync" --install --skip-commands >/dev/null 2>"$sync_stderr" || sync_rc=$?
  if [[ "$sync_rc" -ne 0 ]]; then
    printf 'token-wrapper: WARNING skills-sync --install --skip-commands failed before agent launch (rc=%s); skill autocomplete may be stale. Run `%s --check`.
' \
      "$sync_rc" "$skills_sync" >&2
    if [[ -s "$sync_stderr" ]]; then
      sed 's/^/token-wrapper skills-sync: /' "$sync_stderr" >&2 || true
    fi
  fi
  rm -f "$sync_stderr" 2>/dev/null || true
  return 0
}

run_codex() {
  if codex_legacy_subagent_mode "$@"; then
    run_codex_legacy_subagent "$@"
  fi

  sync_shared_skills

  local session_id bridge_id bridge_dir working_dir prompt resume_id bypass_flag output_file status=0
  local -a codex_args
  session_id="${TOKEN_API_SESSION_ID:-$(token_wrapper_uuid)}"
  bridge_id="${TOKEN_API_CODEX_BRIDGE_ID:-$WRAPPER_ID}"
  bridge_dir="${HOME}/.codex/session-bridges"
  working_dir="${TOKEN_API_TARGET_WORKING_DIR:-$WORKING_DIR}"
  prompt="$*"
  resume_id="${TOKEN_API_CODEX_RESUME_ID:-${TOKEN_API_RESUME_INSTANCE_ID:-}}"

  mkdir -p "$bridge_dir" 2>/dev/null || true
  [[ -n "${bridge_dir:-}" && -n "${bridge_id:-}" ]] && rm -f "${bridge_dir}/${bridge_id}.session_id" 2>/dev/null || true

  export TOKEN_API_SESSION_ID="$session_id"
  export TOKEN_API_CODEX_BRIDGE_ID="$bridge_id"
  export TOKEN_API_WRAPPER_ID="$WRAPPER_ID"
  export TOKEN_API_LAUNCHER="$LAUNCHER"
  export TOKEN_API_ENGINE="codex"

  wrapper_mode_cleanup() {
    [[ -n "${bridge_dir:-}" && -n "${bridge_id:-}" ]] && rm -f "${bridge_dir}/${bridge_id}.session_id" 2>/dev/null || true
  }
  trap cleanup_common EXIT
  trap 'wrapper_forward_signal INT' INT
  trap 'wrapper_forward_signal TERM' TERM
  trap 'wrapper_forward_signal HUP' HUP
  token_wrapper_start

  # Fold the rank+persona staple into codex's initial prompt as a delimited
  # <SYSTEM IDENTITY> preamble (codex has no system-prompt flag; its config
  # `instructions` key is ignored by current codex). Resume is intentionally
  # skipped — first launch already seeded the identity turn into the thread.
  local codex_preamble augmented_prompt
  codex_preamble="$(token_wrapper_codex_system_preamble || true)"
  augmented_prompt="$prompt"
  if [[ -n "$codex_preamble" ]]; then
    if [[ -n "$prompt" ]]; then
      augmented_prompt="${codex_preamble}"$'\n\n'"${prompt}"
    else
      augmented_prompt="${codex_preamble}"
    fi
  fi

  if [[ "${CODEX_DANGEROUS_BYPASS:-1}" == "1" ]]; then
    bypass_flag="--dangerously-bypass-approvals-and-sandbox"
  else
    bypass_flag="--full-auto"
  fi

  set +e
  if [[ "${CODEX_HEADLESS:-0}" == "1" ]]; then
    output_file="/tmp/codex-${session_id}.md"
    codex_args=(exec)
    [[ -n "${TOKEN_API_CODEX_PROFILE:-}" ]] && codex_args+=(--profile "$TOKEN_API_CODEX_PROFILE")
    codex_args+=("$augmented_prompt" -C "$working_dir" "$bypass_flag" --json -o "$output_file")
    run_engine_binary codex "${codex_args[@]}"
    status=$?
  elif [[ -n "$resume_id" ]]; then
    codex_args=(resume -C "$working_dir" "$bypass_flag")
    [[ -n "${TOKEN_API_CODEX_PROFILE:-}" ]] && codex_args+=(--profile "$TOKEN_API_CODEX_PROFILE")
    codex_args+=("$resume_id")
    [[ -n "$prompt" ]] && codex_args+=("$prompt")
    run_engine_binary codex "${codex_args[@]}"
    status=$?
  elif [[ "${TOKEN_API_INTERNAL_DISPATCH:-0}" == "1" || "$LAUNCHER" == "dispatch" ]]; then
    codex_args=()
    [[ -n "${TOKEN_API_CODEX_PROFILE:-}" ]] && codex_args+=(--profile "$TOKEN_API_CODEX_PROFILE")
    [[ -n "$augmented_prompt" ]] && codex_args+=("$augmented_prompt")
    codex_args+=(-C "$working_dir" "$bypass_flag")
    run_engine_binary codex "${codex_args[@]}"
    status=$?
  else
    # Bare interactive codex: pass the staple preamble as the initial prompt so a
    # managed persona pane still receives its identity (empty for unmanaged).
    if [[ -n "$codex_preamble" ]]; then
      run_engine_binary codex "$@" "$codex_preamble"
    else
      run_engine_binary codex "$@"
    fi
    status=$?
  fi
  set -e
  exit "$status"
}

case "$ENGINE_ARG" in
  claude) run_claude "$@" ;;
  codex) run_codex "$@" ;;
esac
