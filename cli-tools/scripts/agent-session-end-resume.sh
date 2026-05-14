#!/usr/bin/env bash
# agent-session-end-resume.sh - stage terminal cleanup + resume history on agent exit.
#
# Hook stdin is expected to be JSON. The script writes a pane-scoped sentinel
# consumed by the shared interactive shell prompt hook.

set -uo pipefail

AGENT="${1:-${HOOK_AGENT:-agent}}"
INPUT="$(cat 2>/dev/null || printf '{}')"
[[ -z "$INPUT" ]] && INPUT="{}"

PANE="${TMUX_PANE:-}"
if [[ -z "$PANE" ]] && command -v jq >/dev/null 2>&1; then
    PANE="$(printf '%s' "$INPUT" | jq -r '.tmux_pane // .env.TMUX_PANE // empty' 2>/dev/null || true)"
fi
[[ -z "$PANE" ]] && exit 0

SESSION_ID=""
if command -v jq >/dev/null 2>&1; then
    SESSION_ID="$(printf '%s' "$INPUT" | jq -r '.session_id // .conversation_id // empty' 2>/dev/null || true)"
else
    SESSION_ID="$(printf '%s' "$INPUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("session_id") or d.get("conversation_id") or "")' 2>/dev/null || true)"
fi

case "$AGENT" in
    claude|codex)
        [[ -n "$SESSION_ID" ]] && RESUME_CMD="dispatch --id ${SESSION_ID} --pane ${PANE}" || RESUME_CMD=""
        ;;
    *)
        RESUME_CMD=""
        ;;
esac

SENTINEL="/tmp/agent-resume-${PANE}"
TMP="${SENTINEL}.tmp.$$"
printf '%s\n%s\n' "$AGENT" "$RESUME_CMD" > "$TMP"
mv "$TMP" "$SENTINEL"

# Compatibility for already-open shells that only know about the old Claude
# sentinel name. The sentinel payload is just a command, so it is agent-agnostic.
LEGACY="/tmp/claude-resume-${PANE}"
LEGACY_TMP="${LEGACY}.tmp.$$"
printf '%s' "$RESUME_CMD" > "$LEGACY_TMP"
mv "$LEGACY_TMP" "$LEGACY"

exit 0
