#!/bin/bash
# uv-python-policy.sh — PreToolUse(Bash) companion to the bin/python shim.
#
# The bin/python shim is a pure interpreter delegate: it no longer rewrites
# `python ...` into `uv run -- python ...`, because uv probes the configured
# interpreter by executing it, so a python->uv shim caused uv->python->uv
# recursion (the bug this whole salvage fixes). uv-backed-python *policy* lives
# here instead.
#
# Behaviour: detect a bare `python`/`python3` invocation in a Bash command and
# surface a NON-BLOCKING advisory steering toward `uv run`. It ALWAYS allows the
# call and ALWAYS exits 0 — advisory, never blocking. The recursion outage was
# caused by hard enforcement; this companion deliberately does not repeat that.
# Promote to permissionDecision "deny"/"ask" only by deliberate FG/Emperor
# decision, not by default.
#
# Wire via settings.json PreToolUse matcher "Bash".
set -uo pipefail

INPUT=$(cat 2>/dev/null || echo "{}")

# Extract the command; bail silently if jq is missing or input is unparseable.
CMD=$(printf '%s' "$INPUT" | jq -r '.tool_input.command // empty' 2>/dev/null || true)
[[ -n "$CMD" ]] || exit 0

# Already uv-backed — nothing to nag about.
printf '%s' "$CMD" | grep -Eq 'uv[[:space:]]+run' && exit 0

# Explicit, deliberate raw bypasses — respect them silently.
printf '%s' "$CMD" | grep -Eq '(IMPERIUM_PYTHON_RAW=1|--no-uv)' && exit 0

# Detect a *bare* python command word: `python`, `python3`, or `python3.NN`,
# at the start of the command or after a shell separator/whitespace, and NOT
# part of a path or a longer word (so `/usr/bin/python`, `ipython`,
# `python3-config`, `realpython` do NOT trigger).
BARE_PY='(^|[^[:alnum:]_./-])(python|python3|python3\.[0-9]+)([[:space:]]|$)'
if ! printf '%s' "$CMD" | grep -Eq "$BARE_PY"; then
  exit 0
fi

ADVISORY="Advisory (uv-policy): a bare \`python\`/\`python3\` invocation was detected. Imperium policy prefers uv-backed Python — run it as \`uv run python ...\` (or \`uv run -- python ...\`) so dependencies resolve against the project venv. This is advisory only; the command was allowed. Use \`--no-uv\` or \`IMPERIUM_PYTHON_RAW=1\` for deliberate raw bootstrap/debug cases."

jq -n --arg ctx "$ADVISORY" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "allow",
    permissionDecisionReason: "uv-python-policy: advisory only",
    additionalContext: $ctx
  }
}'

exit 0
