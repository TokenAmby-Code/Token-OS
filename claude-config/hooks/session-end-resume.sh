#!/bin/bash
# session-end-resume.sh — SessionEnd hook: stage resume command for shell precmd pickup.
# Writes /tmp/claude-resume-${TMUX_PANE} so _claude_resume_precmd can clear the
# terminal and optionally stage a --resume command in shell history.
# Always writes the sentinel (even empty) so the clear fires on every Claude exit.

INPUT=$(cat 2>/dev/null || echo "{}")
PANE="${TMUX_PANE:-}"

[[ -z "$PANE" ]] && exit 0

SENTINEL="/tmp/claude-resume-${PANE}"
SESSION_ID=$(echo "$INPUT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || true)

TMP="${SENTINEL}.tmp"
if [[ -n "$SESSION_ID" ]]; then
    printf 'claude --resume %s' "$SESSION_ID" > "$TMP"
else
    : > "$TMP"
fi
mv "$TMP" "$SENTINEL"

exit 0
