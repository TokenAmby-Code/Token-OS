#!/bin/bash
# pr-gh-guard.sh — PreToolUse(Bash) guard that DENIES direct `gh pr ...`.
#
# Agents must invoke the PR skill and let `pr-step` own PR creation, review,
# merge, checks, CodeRabbit handling, and cleanup. This guard only blocks direct
# command-position `gh pr ...`; it does not block `pr-step`, non-PR `gh`
# commands, or text searches that mention "gh pr".
#
# Best-effort & fail-open: any internal error allows the call. Missing jq allows
# the call because jq is required to parse the hook payload.

set -uo pipefail

LOG_DIR="${AGENT_HOOK_LOG_DIR:-${HOME}/.claude/logs}"
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="${LOG_DIR}/pr-gh-guard.log"
log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

INPUT=$(cat 2>/dev/null || echo "{}")
[[ -n "$INPUT" ]] || INPUT="{}"

# Fast path: no command can be `gh pr` unless the raw payload mentions both.
case "$INPUT" in
    *gh*pr*|*pr*gh*) ;;
    *) exit 0 ;;
esac

# Need jq to read the command out of the payload. No jq -> fail open (logged).
if ! command -v jq >/dev/null 2>&1; then
    log "jq not found; allowing (fail-open)"
    exit 0
fi

CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // .tool_input.cmd // empty' 2>/dev/null || true)
[[ -n "$CMD" ]] || exit 0

# Re-check on the actual command (the fast path matched the whole JSON envelope).
case "$CMD" in
    *gh*pr*|*pr*gh*) ;;
    *) exit 0 ;;
esac

# Lex enough shell to distinguish command-position `gh pr` from text arguments
# like: grep -R "gh pr" docs. This intentionally handles the common direct
# forms agents use, including after ; / && / || / newline and simple env prefixes.
if command -v python3 >/dev/null 2>&1; then
    DETECTION=$(python3 - "$CMD" <<'PY' 2>/dev/null || true
import re
import shlex
import sys

cmd = sys.argv[1]

# Turn unquoted newlines into command separators so `x\ngh pr view` is caught.
out = []
quote = None
escaped = False
for ch in cmd:
    if escaped:
        out.append(ch)
        escaped = False
        continue
    if ch == "\\":
        out.append(ch)
        escaped = True
        continue
    if quote:
        out.append(ch)
        if ch == quote:
            quote = None
        continue
    if ch in ("'", '"'):
        out.append(ch)
        quote = ch
    elif ch == "\n":
        out.append(" ; ")
    else:
        out.append(ch)
cmd = "".join(out)

try:
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
except Exception:
    sys.exit(0)

assign_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*", re.S)
separators = {";", "&&", "||", "|", "(" , ")"}

segments = []
current = []
for tok in tokens:
    if tok in separators or set(tok) <= {";", "&", "|", "(", ")"}:
        if current:
            segments.append(current)
            current = []
    else:
        current.append(tok)
if current:
    segments.append(current)

for seg in segments:
    i = 0
    while i < len(seg) and assign_re.match(seg[i]):
        i += 1

    # Also handle the common external env-prefix form:
    #   env GH_TOKEN=x gh pr view
    if i < len(seg) and seg[i] == "env":
        i += 1
        while i < len(seg):
            tok = seg[i]
            if tok == "--":
                i += 1
                break
            if tok.startswith("-"):
                i += 1
                continue
            if assign_re.match(tok):
                i += 1
                continue
            break

    if i + 1 < len(seg) and seg[i] == "gh" and seg[i + 1] == "pr":
        print("direct-gh-pr")
        sys.exit(0)
PY
)
else
    log "python3 not found; allowing (fail-open)"
    exit 0
fi

[[ "$DETECTION" == "direct-gh-pr" ]] || exit 0

log "DENY direct-gh-pr: ${CMD}"

REASON='Direct `gh pr ...` is not an agent workflow. Invoke the `pr` skill and use `pr-step`.'

jq -n --arg r "$REASON" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: $r
  }
}'

exit 0
