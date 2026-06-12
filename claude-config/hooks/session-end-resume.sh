#!/usr/bin/env bash
# session-end-resume.sh - Claude compatibility wrapper for generic agent exit handling.

SCRIPT="${CLI_TOOLS:-$HOME/runtimes/Token-OS/live/cli-tools}/scripts/agent-session-end-resume.sh"
if [[ ! -x "$SCRIPT" && -n "${IMPERIUM:-}" ]]; then
    SCRIPT="$IMPERIUM/runtimes/token-os/live/cli-tools/scripts/agent-session-end-resume.sh"
fi

exec bash "$SCRIPT" claude
