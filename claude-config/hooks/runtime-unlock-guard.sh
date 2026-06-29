#!/bin/bash
# runtime-unlock-guard.sh — PreToolUse(Bash) guard that DENIES commands which
# try to UNLOCK the deploy-owned Token-OS live runtime checkout.
#
# WHY (read this before "fixing" it): ~/runtimes/Token-OS/live is intentionally
# write-locked — write bits cleared + the BSD user-immutable flag (chflags uchg)
# set by runtime-write-protect.sh (PR #242/#298). That lock is NOT agent-proof:
# agents run as the same uid that owns the tree, so an owner can always clear the
# flag (`chflags nouchg`) and re-grant write (`chmod u+w`). Within hours of #298
# an agent PROACTIVELY did exactly that — nouchg + chmod u+w + edit + relock —
# and reported it. The deliberate-bypass case is real.
#
# This hook does NOT try to be a wall (a same-uid actor can't be walled by code —
# see memory runtime-write-lock-no-privilege-boundary). It is a SPEED BUMP with
# TEETH: it keys on the actual shell command being invoked (chmod/chflags or
# the sanctioned runtime-write-protect helper), so it fires ONLY when an agent
# reaches for an unlock operation against a runtime path — not on every tool
# call and not when prompt text merely DESCRIBES such a command. (The old
# general per-cwd dir-watch guard,
# #235/395ed4e, was deleted for per-call cost and read over-blocking; do NOT
# reintroduce that shape here.) When it fires it DENIES with a verbose,
# educational message so the refusal teaches instead of reading as "one more
# error to route around".
#
# Escape hatches:
#   1. legitimate DEPLOY path ONLY: set IMPERIUM_ALLOW_RUNTIME_WRITE=1 in the
#      environment (deploy/token-restart unlocks via runtime-write-protect.sh,
#      which the agent PreToolUse path never sees anyway).
#   2. deliberate sanctioned admin/runtime maintenance: use the visible helper
#      marker `runtime-write-protect.sh unlock --force <runtime-root>` (or
#      `--admin-force`). This is intentionally explicit and loud; it is not for
#      quick code edits, which must still go worktree -> PR -> deploy.
#
# Best-effort & fail-open: any internal error allows the call — never wedge the
# fleet on a guard bug. Every deny and every escape-hatch bypass is logged.
#
# Wire via settings.json PreToolUse matcher "Bash".
set -uo pipefail

LOG_DIR="${AGENT_HOOK_LOG_DIR:-${HOME}/.claude/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="${LOG_DIR}/runtime-unlock-guard.log"
log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

INPUT=$(cat 2>/dev/null || echo "{}")
[[ -n "$INPUT" ]] || INPUT="{}"

# ---------------------------------------------------------------------------
# FAST PATH (near-zero overhead): if the raw payload mentions none of the
# unlock-capable command names, this cannot be an unlock command. No subprocess,
# no jq — just one bash case and exit. This is the overwhelmingly common case
# for every Bash call.
# ---------------------------------------------------------------------------
case "$INPUT" in
    *chmod*|*chflags*|*runtime-write-protect.sh*) ;;
    *) exit 0 ;;
esac

# Env escape hatch for the deploy path — honor before doing any parsing.
if [[ "${IMPERIUM_ALLOW_RUNTIME_WRITE:-}" == "1" ]]; then
    exit 0
fi

# Need jq to read the command out of the payload. No jq -> fail open (logged).
if ! command -v jq >/dev/null 2>&1; then
    log "jq not found; allowing (fail-open)"
    exit 0
fi

CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .tool_input.cmd // empty' 2>/dev/null || true)
[[ -n "$CMD" ]] || exit 0

# Re-check on the actual command (the fast path matched the whole JSON envelope).
case "$CMD" in
    *chmod*|*chflags*|*runtime-write-protect.sh*) ;;
    *) exit 0 ;;
esac

CWD=$(printf '%s' "$INPUT" | jq -r '.cwd // .tool_input.cwd // .env.PWD // empty' 2>/dev/null || true)

# ---------------------------------------------------------------------------
# Dangerous UNLOCK classifier. This intentionally parses the shell into command
# words and inspects only actual command invocations. A dispatch/prompt string
# that merely DESCRIBES `chmod +w` or `runtime-write-protect.sh unlock` must not
# be blocked. Conversely, the sanctioned helper's `unlock` action against the
# live runtime is as guarded as raw chmod/chflags for ad-hoc agent use.
# ---------------------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    log "python3 not found; allowing (fail-open)"
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLASSIFIER="${SCRIPT_DIR}/runtime_unlock_guard_classifier.py"
if [[ ! -f "$CLASSIFIER" ]]; then
    log "classifier missing; allowing (fail-open): $CLASSIFIER"
    exit 0
fi

UNLOCK=$(RUNTIME_UNLOCK_GUARD_CMD="$CMD" RUNTIME_UNLOCK_GUARD_CWD="$CWD" python3 "$CLASSIFIER") || {
    log "classifier failed; allowing (fail-open): ${CMD}"
    exit 0
}

[[ -n "$UNLOCK" ]] || exit 0

# ---------------------------------------------------------------------------
# DENY — verbose & educational. The refusal must TEACH, not read as a bare error.
# ---------------------------------------------------------------------------
log "DENY ${UNLOCK}: ${CMD}"

REASON="🛑 The Token-OS live runtime (~/runtimes/Token-OS/live) is intentionally write-locked. This is an ARCHITECTURE CONSTRAINT, not a bug to work around — workers making changes must not unlock it. The runtime is deploy-owned: it advances only via CD (token-restart, ff-only pull) when a PR merges. A '${UNLOCK}' here (chflags nouchg / chmod +w, or normal runtime-write-protect.sh unlock on the runtime) is the exact reflexive bypass this guard exists to stop; an unlocked edit is drift the next deploy silently clobbers, and the lock is a deliberate boundary, not an obstacle.

What to do instead: make a worktree and ship your change through a PR — CD deploys it on merge.
  worktree-setup <branch> --project Token-OS   # lands in ~/worktrees/Token-OS/wt-<branch>
  # edit there, then: push && pr-create

There is a final override for deliberate sanctioned admin/runtime maintenance only: runtime-write-protect.sh unlock --force <runtime-root> (or --admin-force). Do not use force to make a quick code change. If you are changing worker/runtime code, always use worktree → PR → deploy.

Do not retry this command, do not look for another path to the same write, and do not 'work around' this refusal — it is the intended behavior. If you believe the runtime genuinely needs editing in place but you are not acting under an explicit sanctioned-admin maintenance decision, escalate to a human rather than unlocking it yourself."

jq -n --arg r "$REASON" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: $r
  }
}'

exit 0
