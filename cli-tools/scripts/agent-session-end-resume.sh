#!/usr/bin/env bash
# agent-session-end-resume.sh - stage terminal cleanup + resume history on agent exit.
#
# Hook stdin is expected to be JSON. The script writes a pane-scoped sentinel
# consumed by the shared interactive shell prompt hook. Natural agent exits
# should stage the same resume surface as tmux-instance-exit: dispatch resolves
# cwd/engine from Token-API by instance id, and --pane self resumes in-place.

set -euo pipefail

AGENT="${1:-${HOOK_AGENT:-agent}}"
INPUT="$(cat 2>/dev/null || printf '{}')"
[[ -z "$INPUT" ]] && INPUT="{}"

_json() {
    local expr="$1"
    command -v jq >/dev/null 2>&1 || return 0
    printf '%s' "$INPUT" | jq -r "$expr // empty" 2>/dev/null || true
}

_sql_quote() {
    printf "%s" "$1" | sed "s/'/''/g"
}

_lookup_instance_by_token() {
    local token="$1" db="$2" qtoken
    [[ -n "$token" && -r "$db" ]] || return 0
    command -v sqlite3 >/dev/null 2>&1 || return 0
    qtoken="$(_sql_quote "$token")"
    sqlite3 -noheader "$db" "
        SELECT id
          FROM instances
         WHERE id = '$qtoken'
         ORDER BY last_activity DESC
         LIMIT 1;
    " 2>/dev/null | head -n 1 || true
}

_lookup_instance_by_pane() {
    local pane="$1" label="$2" db="$3" qpane qlabel label_pred=""
    [[ -n "$pane" && -r "$db" ]] || return 0
    command -v sqlite3 >/dev/null 2>&1 || return 0
    qpane="$(_sql_quote "$pane")"
    if [[ -n "$label" ]]; then
        qlabel="$(_sql_quote "$label")"
        label_pred="OR pane_label = '$qlabel'"
    fi
    sqlite3 -noheader "$db" "
        SELECT id
          FROM instances
         WHERE tmux_pane = '$qpane' $label_pred
         ORDER BY CASE status
                    WHEN 'working' THEN 0
                    WHEN 'idle' THEN 1
                    ELSE 2
                  END,
                  last_activity DESC
         LIMIT 1;
    " 2>/dev/null | head -n 1 || true
}

PANE="${TMUX_PANE:-}"
[[ -z "$PANE" ]] && PANE="$(_json '.tmux_pane')"
[[ -z "$PANE" ]] && PANE="$(_json '.env.TMUX_PANE')"
[[ -z "$PANE" ]] && PANE="$(_json '.env.TOKEN_API_DISPATCH_RESOLVED_PANE')"
[[ -z "$PANE" ]] && exit 0

SESSION_ID="$(_json '.session_id')"
[[ -z "$SESSION_ID" ]] && SESSION_ID="$(_json '.conversation_id')"
TOKEN_SESSION_ID="$(_json '.env.TOKEN_API_SESSION_ID')"
BRIDGE_ID="$(_json '.env.TOKEN_API_CODEX_BRIDGE_ID')"
[[ -z "$BRIDGE_ID" ]] && BRIDGE_ID="$(_json '.env.TOKEN_API_WRAPPER_LAUNCH_ID')"

PANE_LABEL=""
INSTANCE_ID=""
if command -v tmux >/dev/null 2>&1; then
    INSTANCE_ID="$(tmux show-options -pv -t "$PANE" @INSTANCE_ID 2>/dev/null || true)"
    PANE_LABEL="$(tmux show-options -pv -t "$PANE" @PANE_ID 2>/dev/null || true)"
fi

DB_PATH="${TOKEN_API_DB:-${HOME}/.claude/agents.db}"
RESUME_ID=""
if [[ -r "$DB_PATH" ]]; then
    for candidate in "$INSTANCE_ID" "$TOKEN_SESSION_ID" "$BRIDGE_ID" "$SESSION_ID"; do
        [[ -n "$RESUME_ID" ]] && break
        RESUME_ID="$(_lookup_instance_by_token "$candidate" "$DB_PATH")"
    done
    if [[ -z "$RESUME_ID" ]]; then
        RESUME_ID="$(_lookup_instance_by_pane "$PANE" "$PANE_LABEL" "$DB_PATH")"
    fi
else
    # Last-ditch fallback for machines without the Token-API DB mounted. Normal
    # managed panes must validate through the DB so stale @INSTANCE_ID cannot win.
    RESUME_ID="$INSTANCE_ID"
fi

RESUME_CMD=""
if [[ -n "$RESUME_ID" ]]; then
    printf -v RESUME_CMD 'dispatch --id %q --pane self' "$RESUME_ID"
fi
{
    printf '[%s] agent=%s pane=%s label=%s instance_opt=%s token_session=%s bridge=%s hook_session=%s db=%s resume_id=%s cmd=%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$AGENT" "$PANE" "$PANE_LABEL" "$INSTANCE_ID" \
        "$TOKEN_SESSION_ID" "$BRIDGE_ID" "$SESSION_ID" "$DB_PATH" "$RESUME_ID" "$RESUME_CMD"
} >> /tmp/agent-session-end-resume.log 2>/dev/null || true

SENTINEL="/tmp/agent-resume-${PANE}"
TMP="${SENTINEL}.tmp.$$"
printf '%s\n\n%s\n' "$AGENT" "$RESUME_CMD" > "$TMP"
mv "$TMP" "$SENTINEL"

# Compatibility for already-open shells that only know about the old Claude
# sentinel name. Payload is still the generic dispatch resume command.
LEGACY="/tmp/claude-resume-${PANE}"
LEGACY_TMP="${LEGACY}.tmp.$$"
printf '%s' "$RESUME_CMD" > "$LEGACY_TMP"
mv "$LEGACY_TMP" "$LEGACY"

exit 0
