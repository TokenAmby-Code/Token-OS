#!/usr/bin/env bash
# post-tool-format.sh — targeted Python formatting for agent file edits.
#
# Intended for Claude/Codex PostToolUse hooks. Reads the hook JSON payload from
# stdin, extracts touched file paths from mutating tool invocations, and formats
# only existing Python files that live under a project with pyproject.toml.
#
# This hook is best-effort by design: formatter errors are logged but never
# returned to the agent as hook failures.

set -uo pipefail

LOG_DIR="${AGENT_HOOK_LOG_DIR:-${HOME}/.codex/log}"
if [[ "${HOOK_AGENT:-}" == "claude" ]]; then
    LOG_DIR="${AGENT_HOOK_LOG_DIR:-${HOME}/.claude/logs}"
fi
mkdir -p "$LOG_DIR" 2>/dev/null || true
LOG_FILE="${LOG_DIR}/post-tool-format.log"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE" 2>/dev/null || true
}

finish() {
    exit 0
}
trap finish EXIT

HOOK_INPUT="$(cat 2>/dev/null || true)"
[[ -z "$HOOK_INPUT" ]] && HOOK_INPUT="{}"

if ! command -v jq >/dev/null 2>&1; then
    log "jq not found; skipping formatter"
    exit 0
fi

TOOL_NAME="$(
    printf '%s' "$HOOK_INPUT" | jq -r '
        .tool_name // .toolName // .tool // .name //
        .event.tool_name // .event.toolName // .event.tool // .event.name //
        .tool_call.name // .toolCall.name //
        ""
    ' 2>/dev/null || true
)"

case "$TOOL_NAME" in
    Write|Edit|MultiEdit|apply_patch|functions.apply_patch|mcp__functions__apply_patch|codex.apply_patch) ;;
    "")
        log "unsupported payload: missing tool name"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac

collect_paths() {
    printf '%s' "$HOOK_INPUT" | jq -r '
        def strings:
            if type == "string" then .
            elif type == "array" then .[]? | strings
            elif type == "object" then .[]? | strings
            else empty end;

        [
            .file_path?, .filepath?, .path?,
            .tool_input.file_path?, .tool_input.filepath?, .tool_input.path?,
            .tool_input.files[]?,
            .tool_input.edits[]?.file_path?, .tool_input.edits[]?.path?,
            .tool_input.patches[]?.file_path?, .tool_input.patches[]?.path?,
            .input.file_path?, .input.filepath?, .input.path?,
            .input.files[]?,
            .input.edits[]?.file_path?, .input.edits[]?.path?,
            .arguments.file_path?, .arguments.filepath?, .arguments.path?,
            .arguments.files[]?,
            .arguments.edits[]?.file_path?, .arguments.edits[]?.path?,
            .params.file_path?, .params.filepath?, .params.path?,
            .params.files[]?,
            .params.edits[]?.file_path?, .params.edits[]?.path?
        ] | .[]? | strings
    ' 2>/dev/null

    # apply_patch payloads commonly contain raw patch text rather than file args.
    printf '%s' "$HOOK_INPUT" | jq -r '.. | strings' 2>/dev/null | awk '
        /^\*\*\* (Add|Update|Delete) File: / {
            sub(/^\*\*\* (Add|Update|Delete) File: /, "")
            print
        }
    '
}

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
CWD="$(pwd)"

find_project_root() {
    local dir="$1"
    while [[ "$dir" == "$REPO_ROOT"* || "$dir" == "$CWD"* ]]; do
        if [[ -f "$dir/pyproject.toml" ]]; then
            printf '%s\n' "$dir"
            return 0
        fi
        [[ "$dir" == "/" ]] && break
        dir="$(dirname "$dir")"
    done
    return 1
}

normalize_path() {
    local raw="$1"
    [[ -z "$raw" ]] && return 1
    [[ "$raw" == *$'\n'* ]] && return 1
    [[ "$raw" != *.py ]] && return 1

    local candidate
    if [[ "$raw" = /* ]]; then
        candidate="$raw"
    else
        candidate="$CWD/$raw"
    fi

    if [[ ! -e "$candidate" ]]; then
        # Some hooks report paths relative to the git root even when invoked
        # from a subdirectory.
        candidate="$REPO_ROOT/$raw"
    fi

    [[ -f "$candidate" ]] || return 1

    local real
    real="$(cd "$(dirname "$candidate")" 2>/dev/null && pwd -P)/$(basename "$candidate")" || return 1
    [[ "$real" == "$REPO_ROOT"/* || "$real" == "$CWD"/* ]] || return 1
    printf '%s\n' "$real"
}

FILES=()
while IFS= read -r file; do
    FILES+=("$file")
done < <(collect_paths | while IFS= read -r p; do normalize_path "$p"; done | awk '!seen[$0]++')

if [[ "${#FILES[@]}" -eq 0 ]]; then
    exit 0
fi

for file in "${FILES[@]}"; do
    project_root="$(find_project_root "$(dirname "$file")" || true)"
    if [[ -z "$project_root" ]]; then
        log "skip ${file}: no pyproject.toml parent"
        continue
    fi

    rel="${file#$project_root/}"
    log "format ${file}"
    if ! (cd "$project_root" && uvx --python 3.11 ruff format "$rel" >> "$LOG_FILE" 2>&1); then
        log "ruff format failed for ${file}"
        continue
    fi
    if ! (cd "$project_root" && uvx --python 3.11 ruff check --fix "$rel" >> "$LOG_FILE" 2>&1); then
        log "ruff check --fix failed for ${file}"
        continue
    fi
done

exit 0
