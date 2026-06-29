#!/usr/bin/env bash
# command-boundary-guard.sh — generic PreToolUse(Bash) command-boundary guard.
#
# Evaluates command-boundary-rules.json and denies commands that cross a
# user-defined architecture boundary, redirecting to the sanctioned workflow.
# Best-effort/fail-open: internal errors never wedge the agent.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES_CONFIG="${COMMAND_BOUNDARY_RULES_CONFIG:-${SCRIPT_DIR}/command-boundary-rules.json}"
ENGINE="${SCRIPT_DIR}/command_boundary_guard.py"
RULE_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --rule)
            [[ $# -ge 2 ]] || break
            RULE_ARGS+=(--rule "$2")
            shift 2
            ;;
        --config)
            [[ $# -ge 2 ]] || break
            RULES_CONFIG="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

if [[ "${HOOK_AGENT:-}" == "codex" ]]; then
    LOG_DIR="${AGENT_HOOK_LOG_DIR:-${HOME}/.codex/log}"
else
    LOG_DIR="${AGENT_HOOK_LOG_DIR:-${HOME}/.claude/logs}"
fi
mkdir -p "$LOG_DIR" 2>/dev/null || true
export COMMAND_BOUNDARY_LOG_FILE="${COMMAND_BOUNDARY_LOG_FILE:-${LOG_DIR}/command-boundary-guard.log}"

INPUT="$(cat 2>/dev/null || true)"
[[ -n "$INPUT" ]] || INPUT="{}"

# Cheap global fast path for the current boundary rule set. This preserves the
# old hot-path behavior: ordinary Bash calls exit without jq/python startup.
case "$INPUT" in
    *gh*pr*|*pr*gh*|*chmod*|*chflags*|*runtime-write-protect.sh*|*tmux*|*find*|*bfs*|*rg*|*ugrep*|*grep*) ;;
    *) exit 0 ;;
esac

# Missing config or engine means fail open.
if [[ ! -f "$RULES_CONFIG" || ! -f "$ENGINE" ]]; then
    printf '[%s] missing config/engine; allowing fail-open config=%s engine=%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$RULES_CONFIG" "$ENGINE" >> "$COMMAND_BOUNDARY_LOG_FILE" 2>/dev/null || true
    exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
    printf '[%s] python3 not found; allowing fail-open\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$COMMAND_BOUNDARY_LOG_FILE" 2>/dev/null || true
    exit 0
fi

if [[ ${#RULE_ARGS[@]} -gt 0 ]]; then
    printf '%s' "$INPUT" | python3 "$ENGINE" --config "$RULES_CONFIG" "${RULE_ARGS[@]}" 2>/dev/null || true
else
    printf '%s' "$INPUT" | python3 "$ENGINE" --config "$RULES_CONFIG" 2>/dev/null || true
fi
exit 0
