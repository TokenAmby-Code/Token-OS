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
# TEETH: it keys on the dangerous UNLOCK COMMAND PATTERNS themselves, so it fires
# ONLY when an agent reaches for `chflags nouchg` / `chmod +w` against a runtime
# path — not on every tool call. (The old general per-cwd dir-watch guard,
# #235/395ed4e, was deleted for per-call cost and read over-blocking; do NOT
# reintroduce that shape here.) When it fires it DENIES with a verbose,
# educational message so the refusal teaches instead of reading as "one more
# error to route around".
#
# Escape hatch — legitimate DEPLOY path ONLY: set IMPERIUM_ALLOW_RUNTIME_WRITE=1
# in the environment (deploy/token-restart unlocks via runtime-write-protect.sh,
# which the agent PreToolUse path never sees anyway). Deliberately NOT advertised
# in the deny message: a reflexive agent reading the refusal must not learn the
# bypass as its next move.
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
# FAST PATH (near-zero overhead): if the raw payload mentions neither chmod nor
# chflags, this cannot be an unlock command. No subprocess, no jq — just one
# bash case and exit. This is the overwhelmingly common case for every Bash call.
# ---------------------------------------------------------------------------
case "$INPUT" in
    *chmod*|*chflags*) ;;
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
    *chmod*|*chflags*) ;;
    *) exit 0 ;;
esac

# Inline escape hatch (e.g. a deliberate `IMPERIUM_ALLOW_RUNTIME_WRITE=1 ...`).
printf '%s' "$CMD" | grep -Eq 'IMPERIUM_ALLOW_RUNTIME_WRITE=(1|true)' && exit 0

# ---------------------------------------------------------------------------
# Runtime-path gate. The deploy-owned runtime lives at
# ~/runtimes/Token-OS/live (Mac), /home/token/runtimes/token-os/live (satellite),
# and the NAS mirrors ($IMPERIUM/runtimes/token-os/live). Every spelling an agent
# emits — ~, $HOME, /Users/<name>, /Volumes/Imperium, /mnt/imperium — still
# carries the literal substring `runtimes/Token-OS` (case-insensitive on the
# project segment). $TOKEN_OS / ${TOKEN_OS} resolve to the live checkout without
# that substring, so match them explicitly. This substring test is the whole
# point: it keys on the target appearing in the command, with no path resolution
# and no filesystem walk.
# ---------------------------------------------------------------------------
RUNTIME_PATH='(runtimes/token-os)|(\$\{?TOKEN_OS\}?)'
if ! printf '%s' "$CMD" | grep -Eiq "$RUNTIME_PATH"; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Dangerous UNLOCK patterns. Match only commands that ADD write or CLEAR the
# immutable flag — re-locking (chmod u-w / go-w, chmod 0444, chflags uchg) must
# pass through untouched.
#
#   chflags ... nouchg|nouchange|noschg|noschange   -> clears the immutable flag
#   chmod   ... <symbolic +...w>                     -> u+w, +w, a+w, u+rwx, g+w
#   chmod   ... <octal with owner write>             -> 7xx/6xx/3xx/2xx, 0755, ...
# ---------------------------------------------------------------------------
UNLOCK=""

# chflags clearing an immutable flag.
if printf '%s' "$CMD" | grep -Eq 'chflags([[:space:]]+-[A-Za-z]+)*[[:space:]]+(no(uchg|uchange|schg|schange))\b'; then
    UNLOCK="chflags-clear-immutable"
fi

# chmod adding write via a symbolic mode: a '+' clause whose perm set contains w
# (covers +w, u+w, a+w, ug+w, u+rwx). Deliberately ignores '-' clauses (u-w).
if [[ -z "$UNLOCK" ]] && \
   printf '%s' "$CMD" | grep -Eq 'chmod([[:space:]]+-[A-Za-z]+)*[[:space:]]+[ugoa]*\+[rwxXst]*w[rwxXst]*'; then
    UNLOCK="chmod-add-write"
fi

# chmod adding write via an octal mode whose OWNER digit has the write bit (2).
# Owner digit is the first of a 3-digit mode or the second of a 4-digit mode.
# 0755/755/700/644/664/2/... grant owner write; 0444/0555/444 do not.
if [[ -z "$UNLOCK" ]] && \
   printf '%s' "$CMD" | grep -Eq 'chmod([[:space:]]+-[A-Za-z]+)*[[:space:]]+0?[2367][0-7]{0,2}\b'; then
    UNLOCK="chmod-octal-owner-write"
fi

[[ -n "$UNLOCK" ]] || exit 0

# ---------------------------------------------------------------------------
# DENY — verbose & educational. The refusal must TEACH, not read as a bare error.
# ---------------------------------------------------------------------------
log "DENY ${UNLOCK}: ${CMD}"

REASON="🛑 The Token-OS live runtime (~/runtimes/Token-OS/live) is intentionally write-locked. This is an ARCHITECTURE CONSTRAINT, not a bug to work around — do NOT unlock it. The runtime is deploy-owned: it advances only via CD (token-restart, ff-only pull) when a PR merges. A '${UNLOCK}' here (chflags nouchg / chmod +w on the runtime) is the exact reflexive bypass this guard exists to stop; an unlocked edit is drift the next deploy silently clobbers, and the lock is a deliberate boundary, not an obstacle.

What to do instead: make a worktree and ship your change through a PR — CD deploys it on merge.
  worktree-setup <branch> --project Token-OS   # lands in ~/worktrees/Token-OS/wt-<branch>
  # edit there, then: push && pr-create

Do not retry this command, do not look for another path to the same write, and do not 'work around' this refusal — it is the intended behavior. If you believe the runtime genuinely needs editing in place, escalate to a human rather than unlocking it yourself."

jq -n --arg r "$REASON" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: $r
  }
}'

exit 0
