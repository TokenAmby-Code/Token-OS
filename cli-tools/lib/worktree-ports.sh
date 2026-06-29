#!/usr/bin/env bash
# worktree-ports.sh — live-derived port allocation for per-worktree dev servers.
#
# Ownership doctrine: the old JSON allocation registry is NOT source of truth.
# Occupied ports are derived at allocation time from live runtime truth:
#   - non-stopped/non-archived Token-API instance rows whose working_dir has a
#     .worktree.env PORT in the dev pool;
#   - active short-lived setup leases (race prevention only, not ownership);
#   - OS listeners already bound in the dev pool.
#
# A stopped/retired instance naturally drops out of the query. The legacy
# $HOME/.local/state/imperium/worktree-ports.json file may still exist on old
# machines, but this library never reads it as an allocation registry.
#
# Public functions:
#   assign_port <worktree-dir>          → echoes assigned port (idempotent while live/leased)
#   free_port <worktree-dir>            → kills the worktree's derived port listener, drops lease
#   stop_port_process <worktree-dir>    → kills assigned port listener, keeps lease/env
#   lookup_port <worktree-dir>          → echoes live/leased/.worktree.env port or empty
#   list_ports                          → diagnostic live owners + free candidates
#   prune_ports                         → removes stale short-lived leases; registry no-op compat

WORKTREE_PORT_POOL_START=${WORKTREE_PORT_POOL_START:-7100}
WORKTREE_PORT_POOL_END=${WORKTREE_PORT_POOL_END:-7199}
WORKTREE_PORT_STATE_DIR="${WORKTREE_PORT_STATE_DIR:-${HOME}/.local/state/imperium}"
WORKTREE_PORT_LEASE_DIR="${WORKTREE_PORT_LEASE_DIR:-${WORKTREE_PORT_STATE_DIR}/worktree-port-leases}"
WORKTREE_PORT_LOCK="${WORKTREE_PORT_LOCK:-${WORKTREE_PORT_STATE_DIR}/worktree-ports.lock}"
WORKTREE_PORT_LEASE_TTL_SECONDS=${WORKTREE_PORT_LEASE_TTL_SECONDS:-900}
# Legacy path retained only for operator diagnostics/migration messaging.
WORKTREE_PORT_REGISTRY="${WORKTREE_PORT_REGISTRY:-${WORKTREE_PORT_STATE_DIR}/worktree-ports.json}"

_wp_state_init() {
    mkdir -p "$WORKTREE_PORT_STATE_DIR" "$WORKTREE_PORT_LEASE_DIR"
}

_wp_normalize_dir() {
    local dir="$1"
    if command -v python3 &>/dev/null; then
        python3 - "$dir" <<'PY' 2>/dev/null || printf '%s\n' "$dir"
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
    else
        (cd "$dir" 2>/dev/null && pwd -P) || printf '%s\n' "$dir"
    fi
}

_wp_safe_key() {
    if command -v shasum &>/dev/null; then
        printf '%s' "$1" | shasum -a 256 | awk '{print $1}'
    elif command -v sha256sum &>/dev/null; then
        printf '%s' "$1" | sha256sum | awk '{print $1}'
    else
        printf '%s' "$1" | tr -c '[:alnum:]' '_'
    fi
}

_wp_lease_file() {
    local worktree_dir key
    worktree_dir="$(_wp_normalize_dir "$1")"
    key="$(_wp_safe_key "$worktree_dir")"
    printf '%s/%s.lease\n' "$WORKTREE_PORT_LEASE_DIR" "$key"
}

# Short-lived lock so concurrent worktree-setup invocations cannot both choose
# the same candidate before either one has a live instance row or OS listener.
_wp_with_lock() {
    _wp_state_init
    if [[ -z "${WORKTREE_PORTS_NO_FLOCK:-}" ]] && command -v flock &>/dev/null; then
        exec 9>"$WORKTREE_PORT_LOCK"
        flock -w 10 9 || { echo "worktree-ports: could not acquire lock" >&2; return 1; }
        "$@"
        local rc=$?
        exec 9>&-
        return "$rc"
    fi

    local lockdir="${WORKTREE_PORT_LOCK}.d"
    local waited=0 owner
    while ! mkdir "$lockdir" 2>/dev/null; do
        owner="$(cat "$lockdir/pid" 2>/dev/null || true)"
        if [[ -n "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then
            rm -rf "$lockdir" 2>/dev/null || true
            continue
        fi
        if (( waited >= 100 )); then
            echo "worktree-ports: could not acquire lock" >&2
            return 1
        fi
        sleep 0.1
        waited=$(( waited + 1 ))
    done
    printf '%s' "$$" > "$lockdir/pid"
    "$@"
    local rc=$?
    rm -rf "$lockdir" 2>/dev/null || true
    return "$rc"
}

_wp_agents_db() {
    # Source centralized machine config when available, but do not require it in
    # tests or minimal shells.
    local lib_dir
    lib_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    # shellcheck source=nas-path.sh
    source "$lib_dir/nas-path.sh" 2>/dev/null || true

    local legacy_agents_db="${HOME}/.claude/agents.db"
    local token_api_db="${TOKEN_API_DB:-}"
    if [[ -n "$token_api_db" ]]; then
        local resolved legacy_resolved
        resolved="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$token_api_db" 2>/dev/null || printf '%s' "$token_api_db")"
        legacy_resolved="$(python3 -c 'import os,sys; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$legacy_agents_db" 2>/dev/null || printf '%s' "$legacy_agents_db")"
        [[ "$resolved" != "$legacy_resolved" ]] && { printf '%s\n' "$token_api_db"; return 0; }
    fi
    printf '%s\n' "${TOKEN_API_AGENTS_DB:-${TOKEN_API_DATABASE_DIR:-${HOME}/runtimes/database}/agents.db}"
}

_wp_port_from_env_file() {
    local env_file="$1/.worktree.env"
    [[ -f "$env_file" ]] || return 0
    awk -F= '/^PORT=[0-9]+$/ {print $2; exit}' "$env_file" 2>/dev/null || true
}

_wp_in_pool() {
    local port="$1"
    [[ "$port" =~ ^[0-9]+$ ]] || return 1
    (( port >= WORKTREE_PORT_POOL_START && port <= WORKTREE_PORT_POOL_END ))
}

_wp_live_instance_dirs() {
    local db
    db="$(_wp_agents_db)"
    [[ -f "$db" ]] || return 0
    command -v sqlite3 &>/dev/null || return 0
    sqlite3 -noheader -batch "$db" \
        "SELECT COALESCE(working_dir,'') || char(9) || id || char(9) || COALESCE(status,'')
         FROM instances
         WHERE COALESCE(working_dir,'') <> ''
           AND COALESCE(status,'') NOT IN ('stopped','archived','retired','closed')
           AND stopped_at IS NULL
           AND archived_at IS NULL;" 2>/dev/null || true
}

_wp_live_instance_port_rows() {
    local row dir instance_id status port
    while IFS=$'\t' read -r dir instance_id status; do
        [[ -n "$dir" ]] || continue
        port="$(_wp_port_from_env_file "$dir")"
        _wp_in_pool "$port" || continue
        printf '%s\tinstance:%s\t%s\t%s\n' "$port" "$instance_id" "$dir" "$status"
    done < <(_wp_live_instance_dirs)
}

_wp_prune_leases_inner() {
    _wp_state_init
    local now lease created dir port pruned=0
    now="$(date +%s)"
    shopt -s nullglob
    for lease in "$WORKTREE_PORT_LEASE_DIR"/*.lease; do
        created="" dir="" port=""
        # shellcheck disable=SC1090
        source "$lease" 2>/dev/null || true
        if [[ -z "$created" || -z "$dir" || -z "$port" ]] \
            || (( now - created > WORKTREE_PORT_LEASE_TTL_SECONDS )) \
            || [[ ! -d "$dir" ]]; then
            rm -f "$lease" 2>/dev/null || true
            pruned=$(( pruned + 1 ))
        fi
    done
    shopt -u nullglob
    return 0
}

_wp_lease_port_rows() {
    _wp_prune_leases_inner
    local lease created dir port
    shopt -s nullglob
    for lease in "$WORKTREE_PORT_LEASE_DIR"/*.lease; do
        created="" dir="" port=""
        # shellcheck disable=SC1090
        source "$lease" 2>/dev/null || true
        _wp_in_pool "$port" || continue
        [[ -n "$dir" ]] || continue
        printf '%s\tlease\t%s\tcreated=%s\n' "$port" "$dir" "$created"
    done
    shopt -u nullglob
}

_wp_listener_pids() {
    local port="$1"
    command -v lsof &>/dev/null || return 0
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
}

_wp_listener_rows() {
    command -v lsof &>/dev/null || return 0
    local port pids
    for ((port = WORKTREE_PORT_POOL_START; port <= WORKTREE_PORT_POOL_END; port++)); do
        pids="$(_wp_listener_pids "$port" | paste -sd, -)"
        [[ -n "$pids" ]] || continue
        printf '%s\tlistener:pids=%s\t-\tOS\n' "$port" "$pids"
    done
}

_wp_used_ports() {
    { _wp_live_instance_port_rows; _wp_lease_port_rows; } | awk -F'\t' '{print $1}' | sort -n -u
}

_wp_lookup_live_or_lease() {
    local worktree_dir="$(_wp_normalize_dir "$1")"
    local port kind dir rest
    while IFS=$'\t' read -r port kind dir rest; do
        [[ "$(_wp_normalize_dir "$dir")" == "$worktree_dir" ]] || continue
        echo "$port"
        return 0
    done < <({ _wp_live_instance_port_rows; _wp_lease_port_rows; })
}

_wp_write_lease() {
    local worktree_dir="$(_wp_normalize_dir "$1")" port="$2" lease
    lease="$(_wp_lease_file "$worktree_dir")"
    cat > "$lease" <<EOF_LEASE
created=$(date +%s)
dir=$(printf '%q' "$worktree_dir")
port=$port
EOF_LEASE
}

_wp_drop_lease() {
    rm -f "$(_wp_lease_file "$1")" 2>/dev/null || true
}

_wp_assign_inner() {
    local worktree_dir="$(_wp_normalize_dir "$1")"

    local existing
    existing="$(_wp_lookup_live_or_lease "$worktree_dir")"
    if _wp_in_pool "$existing"; then
        echo "$existing"
        return 0
    fi

    local used port
    used="$(_wp_used_ports | tr '\n' ' ')"
    for ((port = WORKTREE_PORT_POOL_START; port <= WORKTREE_PORT_POOL_END; port++)); do
        if grep -qw "$port" <<<"$used"; then
            continue
        fi
        # Also check the OS — port may be held by something outside Token-OS.
        if [[ -n "$(_wp_listener_pids "$port")" ]]; then
            continue
        fi
        _wp_write_lease "$worktree_dir" "$port"
        echo "$port"
        return 0
    done

    echo "worktree-ports: pool exhausted (${WORKTREE_PORT_POOL_START}-${WORKTREE_PORT_POOL_END})" >&2
    return 1
}

_wp_kill_port_process() {
    local port="$1"

    # Hard safety rails: never touch live Token-API, and only reap the worktree
    # dev pool. Corrupt env/lease state must fail safe, not kill arbitrary listeners.
    [[ "$port" == "7777" ]] && return 0
    _wp_in_pool "$port" || return 0

    local pids
    pids="$(_wp_listener_pids "$port")"
    if [[ -n "$pids" ]]; then
        # shellcheck disable=SC2086
        kill -INT $pids 2>/dev/null || true
        sleep 1
        # shellcheck disable=SC2086
        kill -KILL $pids 2>/dev/null || true
    fi
}

_wp_lookup_port_any() {
    local worktree_dir="$(_wp_normalize_dir "$1")" port
    port="$(_wp_lookup_live_or_lease "$worktree_dir")"
    if _wp_in_pool "$port"; then
        echo "$port"
        return 0
    fi
    # Specific-worktree fallback for stop/delete ergonomics only. This is not an
    # allocation source and cannot exhaust the pool.
    port="$(_wp_port_from_env_file "$worktree_dir")"
    if _wp_in_pool "$port"; then
        echo "$port"
    fi
    return 0
}

_wp_stop_port_process_inner() {
    local port
    port="$(_wp_lookup_port_any "$1")"
    [[ -n "$port" ]] || return 0
    _wp_kill_port_process "$port"
}

_wp_free_inner() {
    local port
    port="$(_wp_lookup_port_any "$1")"
    _wp_drop_lease "$1"
    [[ -n "$port" ]] || return 0
    _wp_kill_port_process "$port"
    echo "$port"
}

assign_port() {
    [[ $# -eq 1 ]] || { echo "assign_port <worktree-dir>" >&2; return 1; }
    _wp_with_lock _wp_assign_inner "$1"
}

prune_ports() {
    _wp_with_lock _wp_prune_leases_inner
}

free_port() {
    [[ $# -eq 1 ]] || { echo "free_port <worktree-dir>" >&2; return 1; }
    _wp_with_lock _wp_free_inner "$1"
}

stop_port_process() {
    [[ $# -eq 1 ]] || { echo "stop_port_process <worktree-dir>" >&2; return 1; }
    _wp_with_lock _wp_stop_port_process_inner "$1"
}

lookup_port() {
    [[ $# -eq 1 ]] || { echo "lookup_port <worktree-dir>" >&2; return 1; }
    _wp_lookup_port_any "$1"
}

list_ports() {
    _wp_state_init
    _wp_prune_leases_inner
    if [[ -f "$WORKTREE_PORT_REGISTRY" ]]; then
        echo "# legacy registry ignored: $WORKTREE_PORT_REGISTRY" >&2
    fi
    printf 'PORT\tOWNER\tWORKTREE\tSTATUS\n'
    { _wp_live_instance_port_rows; _wp_lease_port_rows; _wp_listener_rows; } | sort -n -u
    local used port first=1
    used="$({ _wp_used_ports; _wp_listener_rows | awk -F'\t' '{print $1}'; } | sort -n -u | tr '\n' ' ')"
    printf 'FREE\t'
    for ((port = WORKTREE_PORT_POOL_START; port <= WORKTREE_PORT_POOL_END; port++)); do
        grep -qw "$port" <<<"$used" && continue
        if (( first )); then first=0; else printf ','; fi
        printf '%s' "$port"
    done
    printf '\n'
}
