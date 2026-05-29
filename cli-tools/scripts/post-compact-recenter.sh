#!/usr/bin/env bash
# post-compact-recenter.sh — engine-neutral post-compaction auto-recenter.
#
# Wired into BOTH harnesses on their respective post-compaction hook events:
#   - Claude Code : SessionStart hook, matcher "compact"  (HOOK_AGENT=claude)
#   - Codex       : PostCompact hook                       (HOOK_AGENT=codex)
#
# After a compaction the harness leaves the pane idle at the prompt. Compaction
# cost is already paid, so rather than letting the instance sit dead until the
# Emperor wanders back hours later, we inject a recenter prompt and submit it —
# making compaction behave like plan mode (auto-continues the agent). The prompt
# is self-gating: the instance continues only if it has strong continuity, else
# it surfaces the error and ends inference.
#
# Injection goes through agent-cmd (engine-neutral --self resolution + the shared
# tmux typing-guard), so it never types over the Emperor mid-keystroke.
#
# The script reads the hook JSON on stdin (and discards it), then returns
# immediately; the actual send is detached so the harness is never blocked.

set -uo pipefail

HOOK_AGENT="${HOOK_AGENT:-unknown}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_CMD="${SCRIPT_DIR}/../bin/agent-cmd"
LOG_FILE="${HOME}/.${HOOK_AGENT}/log/post-compact-recenter.log"

# Drain stdin so the harness pipe closes cleanly even though we don't use it.
HOOK_INPUT="$(cat 2>/dev/null || true)"

# The recenter directive. Overridable via env for experimentation, but the
# default is the canonical wording shared across both harnesses.
RECENTER_MSG="${POST_COMPACT_RECENTER_MSG:-recenter yourself after compact, read your session doc. if you have a strong sense of continuity then continue, else highlight the error and end inference.}"

mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true
if [[ "${HOOK_DEBUG:-0}" == "1" ]]; then
    printf '[%s] agent=%s recenter fired; hook=%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$HOOK_AGENT" "${HOOK_INPUT:0:300}" \
        >> "$LOG_FILE" 2>/dev/null || true
fi

[[ -x "$AGENT_CMD" ]] || exit 0

# Detach the injection. agent-cmd --self resolves this hook's agent ancestor
# (claude/codex) to its pane, waits out the typing-guard, then submits. A
# generous guard window lets the post-compaction summary finish rendering before
# we send; if the Emperor is actively typing the guard still protects them.
(
    TMUX_GUARD_TIMEOUT="${POST_COMPACT_GUARD_TIMEOUT:-45}" \
        "$AGENT_CMD" --self --detach "$RECENTER_MSG" >/dev/null 2>&1
) &
disown 2>/dev/null || true

exit 0
