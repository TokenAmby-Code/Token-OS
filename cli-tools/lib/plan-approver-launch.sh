#!/usr/bin/env bash
# plan-approver-launch.sh — shared launcher for clear-context plan approval watchers.
#
# This library owns the cross-engine policy for launching tmux-plan-approve-clear:
#   * trigger_class -> timeout mapping
#   * universal --no-state policy (screen classifier is the gate)
#   * guarded Token-OS binary resolution
#   * shared pane recovery chain
#   * one structured launch line per spawned watcher
#
# Intentionally no launcher-level lock: tmux-plan-approve-clear owns the per-pane
# single-flight lock, and speculative Codex hooks rely on that dedupe point.

_PLAN_APPROVER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
source "${_PLAN_APPROVER_LIB_DIR}/nas-path.sh" >/dev/null 2>&1 || true

plan_approver_mount_live() {
    local root="$1"
    [[ -n "$root" && -d "$root" ]] || return 1
    ls "$root" >/dev/null 2>&1
}

plan_approver_resolve_token_os_bin() {
    local tool="$1" found root cand
    found="$(command -v "$tool" 2>/dev/null || true)"
    if [[ -n "$found" && -x "$found" ]]; then
        printf '%s\n' "$found"
        return 0
    fi

    for root in \
        "${TOKEN_OS:-}" \
        "${HOME%/}/runtimes/Token-OS/live" \
        "${HOME%/}/runtimes/token-os/live"; do
        [[ -n "$root" ]] || continue
        cand="${root%/}/cli-tools/bin/${tool}"
        if [[ -x "$cand" ]]; then
            printf '%s\n' "$cand"
            return 0
        fi
    done

    for root in "${IMPERIUM:-}" "${CIVIC:-}"; do
        [[ -n "$root" ]] || continue
        cand="${root%/}/runtimes/token-os/live/cli-tools/bin/${tool}"
        if plan_approver_mount_live "$root" && [[ -x "$cand" ]]; then
            printf '%s\n' "$cand"
            return 0
        fi
    done

    cand="${_PLAN_APPROVER_LIB_DIR}/../bin/${tool}"
    if [[ -x "$cand" ]]; then
        printf '%s\n' "$cand"
        return 0
    fi
    return 1
}

plan_approver_default_log_file() {
    case "${1:-}" in
        claude) printf '%s\n' "${HOME}/.claude/logs/plan-gatekeeper.log" ;;
        codex) printf '%s\n' "${HOME}/.codex/log/hook-bridge.log" ;;
        *) return 1 ;;
    esac
}

plan_approver_log() {
    local log_file="$1" message="$2"
    [[ -n "$log_file" ]] || return 0
    mkdir -p "$(dirname "$log_file")" 2>/dev/null || true
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$message" >> "$log_file" 2>/dev/null || true
}

plan_approver_json_value_from() {
    local input="$1" expr="$2"
    command -v jq >/dev/null 2>&1 || return 1
    printf '%s' "$input" | jq -r "$expr" 2>/dev/null || true
}

plan_approver_trigger_timeout() {
    case "${1:-}" in
        precise_permission) printf '10\n' ;;
        early_prompt) printf '90\n' ;;
        post_tool) printf '30\n' ;;
        late_stop) printf '10\n' ;;
        *) return 1 ;;
    esac
}

plan_approver_resolve_pane() {
    local explicit="${1:-}" hook_input="${2:-${HOOK_INPUT:-}}" pane_resolver="${3:-}"
    local pane cmd_name resolved_cmd

    if [[ -n "$explicit" ]]; then
        printf '%s\n' "$explicit"
        return 0
    fi

    pane="${TMUX_PANE:-}"
    if [[ -z "$pane" && -n "$hook_input" ]]; then
        pane="$(plan_approver_json_value_from "$hook_input" '.env.TMUX_PANE // empty' || true)"
    fi
    [[ -n "$pane" ]] || pane="${TOKEN_API_DISPATCH_RESOLVED_PANE:-}"
    if [[ -z "$pane" && -n "$hook_input" ]]; then
        pane="$(plan_approver_json_value_from "$hook_input" '.env.TOKEN_API_DISPATCH_RESOLVED_PANE // empty' || true)"
    fi
    if [[ -n "$pane" ]]; then
        printf '%s\n' "$pane"
        return 0
    fi

    if [[ -n "$pane_resolver" ]] && type "$pane_resolver" >/dev/null 2>&1; then
        pane="$($pane_resolver 2>/dev/null || true)"
        if [[ -n "$pane" ]]; then
            printf '%s\n' "$pane"
            return 0
        fi
    fi

    for cmd_name in agent-cmd claude-cmd; do
        resolved_cmd="$(plan_approver_resolve_token_os_bin "$cmd_name" 2>/dev/null || true)"
        [[ -n "$resolved_cmd" ]] || continue
        pane="$($resolved_cmd --self --resolve-only 2>/dev/null || true)"
        if [[ -n "$pane" ]]; then
            printf '%s\n' "$pane"
            return 0
        fi
    done

    return 1
}

plan_approver_resolve_approver() {
    local candidate
    for candidate in \
        "${TOKEN_API_PLAN_APPROVER:-}" \
        "$(plan_approver_resolve_token_os_bin tmux-plan-approve-clear 2>/dev/null || true)"; do
        [[ -n "$candidate" ]] || continue
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

plan_approver_get_planning_state() {
    local pane="$1" api_url="${2:-${TOKEN_API_URL:-}}"
    [[ -n "$pane" && -n "$api_url" ]] || return 1
    curl -fsS -G --connect-timeout 1 --max-time 2 \
        --data-urlencode "tmux_pane=${pane}" \
        "${api_url}/api/planning/state" 2>/dev/null \
        | jq -r '.planning_state // empty' 2>/dev/null || true
}

plan_approver_payload_has_plan() {
    [[ "${HOOK_INPUT:-}" == *"<proposed_plan>"* ]]
}

plan_approver_find_codex_transcript() {
    local transcript session_id found
    transcript="$(plan_approver_json_value_from "${HOOK_INPUT:-}" '.transcript_path // .transcriptPath // empty' || true)"
    if [[ -n "$transcript" && -f "$transcript" ]]; then
        printf '%s\n' "$transcript"
        return 0
    fi

    session_id="$(plan_approver_json_value_from "${HOOK_INPUT:-}" '.session_id // .conversation_id // empty' || true)"
    [[ -n "$session_id" ]] || return 1

    found="$(find "${HOME}/.codex/sessions" -type f -name "*${session_id}*.jsonl" -print 2>/dev/null | head -n 1 || true)"
    [[ -n "$found" ]] || return 1
    printf '%s\n' "$found"
}

plan_approver_latest_transcript_turn_has_plan() {
    local transcript
    transcript="$(plan_approver_find_codex_transcript)" || return 1
    python3 - "$transcript" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
current = []
latest_completed = []

try:
    lines = path.read_text(errors="replace").splitlines()
except OSError:
    sys.exit(1)

for line in lines:
    if not line.strip():
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    current.append(obj)
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if (
        isinstance(obj, dict)
        and obj.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "task_complete"
    ):
        latest_completed = current
        current = []

if not latest_completed:
    sys.exit(1)

def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)
    elif isinstance(value, str):
        yield value

for item in latest_completed:
    for value in walk(item):
        if isinstance(value, dict) and value.get("type") == "Plan":
            sys.exit(0)
        if isinstance(value, str) and "<proposed_plan>" in value:
            sys.exit(0)

sys.exit(1)
PY
}

plan_approver_current_transcript_turn_is_plan_mode() {
    local transcript
    transcript="$(plan_approver_find_codex_transcript)" || return 1
    python3 - "$transcript" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
current = []
try:
    lines = path.read_text(errors="replace").splitlines()
except OSError:
    sys.exit(1)

for line in lines:
    if not line.strip():
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    current.append(obj)
    payload = obj.get("payload") if isinstance(obj, dict) else None
    if (
        isinstance(obj, dict)
        and obj.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "task_complete"
    ):
        current = []

if not current:
    sys.exit(1)

def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)

for value in walk(current):
    if not isinstance(value, dict):
        continue
    if value.get("collaboration_mode_kind") == "plan":
        sys.exit(0)
    mode = value.get("collaboration_mode")
    if isinstance(mode, dict) and mode.get("mode") == "plan":
        sys.exit(0)

sys.exit(1)
PY
}

plan_approver_payload_prompt_starts_plan() {
    HOOK_PAYLOAD="${HOOK_INPUT:-}" python3 - <<'PY'
import json, os, sys
try:
    data=json.loads(os.environ.get("HOOK_PAYLOAD", ""))
except Exception:
    sys.exit(1)
texts=[]
def walk(v):
    if isinstance(v, dict):
        for k, child in v.items():
            if k in {"prompt", "message", "text", "user_prompt"} and isinstance(child, str):
                texts.append(child)
            walk(child)
    elif isinstance(v, list):
        for child in v:
            walk(child)
walk(data)
for text in texts:
    if text.lstrip().startswith("/plan"):
        sys.exit(0)
sys.exit(1)
PY
}

plan_approver_launch() {
    local agent="" trigger_class="" explicit_pane="" reason="" detector=""
    local hook_input="${HOOK_INPUT:-}" log_file="" approver="" pane_resolver=""
    local timeout pane state_policy="no-state"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --agent) agent="$2"; shift 2 ;;
            --trigger-class) trigger_class="$2"; shift 2 ;;
            --pane) explicit_pane="$2"; shift 2 ;;
            --reason) reason="$2"; shift 2 ;;
            --detector) detector="$2"; shift 2 ;;
            --hook-input) hook_input="$2"; shift 2 ;;
            --log-file) log_file="$2"; shift 2 ;;
            --approver) approver="$2"; shift 2 ;;
            --pane-resolver) pane_resolver="$2"; shift 2 ;;
            *) echo "plan_approver_launch: unknown arg: $1" >&2; return 2 ;;
        esac
    done

    case "$agent" in
        claude|codex) ;;
        *) echo "plan_approver_launch: --agent must be claude or codex" >&2; return 2 ;;
    esac
    timeout="$(plan_approver_trigger_timeout "$trigger_class")" || {
        echo "plan_approver_launch: invalid --trigger-class: $trigger_class" >&2
        return 2
    }
    [[ -n "$reason" ]] || reason="$trigger_class"
    if [[ -z "$log_file" ]]; then
        log_file="$(plan_approver_default_log_file "$agent")" || return 2
    fi

    if [[ -n "$detector" ]]; then
        if ! type "$detector" >/dev/null 2>&1; then
            plan_approver_log "$log_file" "plan-approver-skip engine=$agent trigger=$trigger_class reason=$reason detector=$detector error=missing-detector"
            return 0
        fi
        if ! "$detector"; then
            return 0
        fi
    fi

    pane="$(plan_approver_resolve_pane "$explicit_pane" "$hook_input" "$pane_resolver" 2>/dev/null || true)"
    if [[ -z "$pane" ]]; then
        plan_approver_log "$log_file" "plan-approver-skip engine=$agent trigger=$trigger_class reason=$reason error=no-pane"
        return 0
    fi

    if [[ -z "$approver" ]]; then
        approver="$(plan_approver_resolve_approver 2>/dev/null || true)"
    fi
    if [[ -z "$approver" || ! -x "$approver" ]]; then
        plan_approver_log "$log_file" "plan-approver-skip engine=$agent trigger=$trigger_class pane=$pane reason=$reason error=no-approver"
        return 0
    fi

    (
        "$approver" --pane "$pane" --agent "$agent" --timeout "$timeout" --no-state >> "$log_file" 2>&1 || true
    ) </dev/null >/dev/null 2>&1 &
    disown 2>/dev/null || true

    plan_approver_log "$log_file" "plan-approver-launch engine=$agent trigger=$trigger_class pane=$pane reason=$reason timeout=$timeout state_policy=$state_policy approver=$approver"
}
