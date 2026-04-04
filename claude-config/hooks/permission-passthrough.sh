#!/bin/bash
# permission-passthrough.sh — Let the native permission dialog appear
#
# Outputs nothing. When a PermissionRequest hook returns no JSON decision,
# Claude Code falls through to the native interactive prompt.
#
# Use this for tools that MUST reach the user (e.g., AskUserQuestion).
# Wire via specific matchers in settings.json PermissionRequest array,
# placed BEFORE the catch-all permission-auto-allow.sh.
#
# Expandable: add more matchers in settings.json pointing here.
# No code changes needed per new tool — just a new matcher entry.

# Debug: log that we fired
INPUT=$(cat 2>/dev/null || echo "{}")
mkdir -p "${HOME}/.claude/logs"
echo "[$(date '+%H:%M:%S')] permission-passthrough fired. Input: $INPUT" >> "${HOME}/.claude/logs/permission-passthrough.log"

# Intentionally empty — no stdout = no hook decision = native dialog
exit 0
