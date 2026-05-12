#!/usr/bin/env bash
# session-end-resume.sh - Claude compatibility wrapper for generic agent exit handling.

SCRIPT="/Volumes/Imperium/Token-OS/cli-tools/scripts/agent-session-end-resume.sh"
if [[ ! -x "$SCRIPT" && -n "${IMPERIUM:-}" ]]; then
    SCRIPT="$IMPERIUM/Token-OS/cli-tools/scripts/agent-session-end-resume.sh"
fi

exec bash "$SCRIPT" claude
