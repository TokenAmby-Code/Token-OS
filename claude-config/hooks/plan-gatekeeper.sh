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

# Resolve a token-os bin tolerantly: PATH first, then the live runtime under
# IMPERIUM/CIVIC. Mirrors generic-hook.sh so we don't trust a minimal hook PATH.
# The _mount_live guard skips stale/unmounted NAS roots deterministically before
# probing them, so a dead mount can't yield a phantom -x hit.
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
  for root in "${TOKEN_OS:-}" "${HOME%/}/runtimes/Token-OS/live"; do
    [[ -n "$root" ]] || continue
    cand="${root%/}/cli-tools/bin/${tool}"
    if [[ -x "$cand" ]]; then
      printf '%s\n' "$cand"
      return 0
    fi
  done
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

normalize_agent() {
  case "${1:-auto}" in
    codex|openai) echo codex ;;
    claude|claude-code|anthropic) echo claude ;;
    *) echo auto ;;
  esac
}

resolve_pane_agent() {
  local pane="$1" tmuxctl_bin agent
  tmuxctl_bin=$(_resolve_token_os_bin tmuxctl) || true
  [[ -n "${tmuxctl_bin:-}" ]] || { echo auto; return 0; }
  agent=$("$tmuxctl_bin" resolve-agent --pane "$pane" --agent auto --default auto 2>/dev/null || true)
  normalize_agent "$agent"
}

PANE="${TMUX_PANE:-}"
APPROVER=$(_resolve_token_os_bin tmux-plan-approve-clear) || true
# Claude Code strips $TMUX_PANE from the hook env (see generic-hook.sh's PID-walk
# recovery). Without recovery, ExitPlanMode here would silently yield with no
# approver — the preplan → /plan → approve chain loses its approve+context-clear
# leg for that turn. Recover the pane by PID walk before deciding to yield.
if [[ -z "$PANE" ]]; then
  CLAUDE_CMD=$(_resolve_token_os_bin claude-cmd) || true
  if [[ -n "${CLAUDE_CMD:-}" ]]; then
    PANE=$("$CLAUDE_CMD" --self --resolve-only 2>/dev/null || true)
    [[ -n "$PANE" ]] && log "ExitPlanMode ${SESSION_ID:-unknown}: recovered pane $PANE via PID walk (TMUX_PANE stripped)"
  fi
fi

if [[ -n "$PANE" ]]; then
  AGENT=$(resolve_pane_agent "$PANE")
  if [[ "$AGENT" == auto ]]; then
    log "ExitPlanMode ${SESSION_ID:-unknown}: could not resolve harness for $PANE; yielding without approver"
  elif [[ -z "${APPROVER:-}" ]]; then
    log "ExitPlanMode ${SESSION_ID:-unknown}: tmux-plan-approve-clear not found; yielding without approver"
  else
    (
      "$APPROVER" --pane "$PANE" --agent "$AGENT" --timeout 10 --no-state >> "$LOG" 2>&1 || true
    ) </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true
    log "ExitPlanMode ${SESSION_ID:-unknown}: launched clear-context approver for $PANE as $AGENT"
  fi
else
  log "ExitPlanMode ${SESSION_ID:-unknown}: no TMUX_PANE and pane recovery failed; yielding without approver"
fi

# No JSON output = no hook decision = native dialog appears.
exit 0
