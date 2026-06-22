#!/usr/bin/env bash
# worktree-ports.sh — port allocation for per-worktree local dev servers.
#
# Sourced by worktree-setup / worktree-delete. Maintains a JSON registry at
# $HOME/.local/state/imperium/worktree-ports.json mapping
#   worktree-dir → assigned port (within pool 7100..7199).
#
# Pool deliberately avoids:
#   - 7777 (Token-API)
#   - 3000, 8000, 8080 (common dev defaults)
#
# Public functions:
#   assign_port <worktree-dir>    → echoes assigned port (idempotent)
#   free_port   <worktree-dir>    → frees the port and kills its process
#   lookup_port <worktree-dir>    → echoes assigned port or empty
#   list_ports                     → dumps the registry as text
#   prune_ports                    → frees entries whose worktree dir is gone

WORKTREE_PORT_POOL_START=7100
WORKTREE_PORT_POOL_END=7199
WORKTREE_PORT_REGISTRY="${HOME}/.local/state/imperium/worktree-ports.json"

_wp_ensure_registry() {
    local dir
    dir="$(dirname "$WORKTREE_PORT_REGISTRY")"
    [[ -d "$dir" ]] || mkdir -p "$dir"
    [[ -f "$WORKTREE_PORT_REGISTRY" ]] || echo '{}' > "$WORKTREE_PORT_REGISTRY"
}

# Lock file so concurrent worktree-setup invocations don't both grab 7101.
# Prefers flock(1) where present (Linux/CI); falls back to an atomic mkdir
# lock on macOS, where flock is not installed and skipping the lock entirely
# produced a real duplicate assignment (port 7159 handed to two worktrees).
# WORKTREE_PORTS_NO_FLOCK forces the portable path (used by tests).
_wp_with_lock() {
    _wp_ensure_registry
    local lock="${WORKTREE_PORT_REGISTRY}.lock"
    if [[ -z "${WORKTREE_PORTS_NO_FLOCK:-}" ]] && command -v flock &>/dev/null; then
        exec 9>"$lock"
        flock -w 10 9 || { echo "worktree-ports: could not acquire lock" >&2; return 1; }
        "$@"
        local rc=$?
        exec 9>&-
        return "$rc"
    fi
    # Portable fallback: atomic mkdir lock with bounded wait + stale-owner steal.
    local lockdir="${lock}d"
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

_wp_used_ports() {
    jq -r 'to_entries | map(.value) | .[]' "$WORKTREE_PORT_REGISTRY" 2>/dev/null
}

_wp_assign_inner() {
    local worktree_dir="$1"

    local existing
    existing=$(jq -r --arg k "$worktree_dir" '.[$k] // empty' "$WORKTREE_PORT_REGISTRY")
    if [[ -n "$existing" ]]; then
        echo "$existing"
        return 0
    fi

    local used port
    used=$(_wp_used_ports | sort -n | tr '\n' ' ')
    for ((port = WORKTREE_PORT_POOL_START; port <= WORKTREE_PORT_POOL_END; port++)); do
        if ! grep -qw "$port" <<<"$used"; then
            # Also check the OS — port may be held by something we don't track.
            if command -v lsof &>/dev/null && lsof -iTCP:"$port" -sTCP:LISTEN -n -P &>/dev/null; then
                continue
            fi
            local tmp
            tmp=$(mktemp)
            jq --arg k "$worktree_dir" --argjson v "$port" '. + {($k): $v}' \
                "$WORKTREE_PORT_REGISTRY" > "$tmp" && mv "$tmp" "$WORKTREE_PORT_REGISTRY"
            echo "$port"
            return 0
        fi
    done

    echo "worktree-ports: pool exhausted (${WORKTREE_PORT_POOL_START}-${WORKTREE_PORT_POOL_END})" >&2
    return 1
}

_wp_free_inner() {
    local worktree_dir="$1"
    _wp_ensure_registry

    local port
    port=$(jq -r --arg k "$worktree_dir" '.[$k] // empty' "$WORKTREE_PORT_REGISTRY")
    if [[ -z "$port" ]]; then
        return 0  # nothing to free
    fi

    # Kill whatever's bound to it (best-effort).
    if command -v lsof &>/dev/null; then
        local pids
        pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
        if [[ -n "$pids" ]]; then
            kill -INT $pids 2>/dev/null || true
            sleep 1
            kill -KILL $pids 2>/dev/null || true
        fi
    fi

    local tmp
    tmp=$(mktemp)
    jq --arg k "$worktree_dir" 'del(.[$k])' \
        "$WORKTREE_PORT_REGISTRY" > "$tmp" && mv "$tmp" "$WORKTREE_PORT_REGISTRY"
    echo "$port"
}

# Drop registry entries whose worktree directory no longer exists (deleted
# out-of-band, bypassing worktree-delete). Echoes each pruned "port dir" pair.
# Registry keys are always local worktree dirs, so a missing dir is authoritative.
_wp_prune_inner() {
    local key port tmp
    while IFS= read -r key; do
        [[ -n "$key" && ! -d "$key" ]] || continue
        port=$(jq -r --arg k "$key" '.[$k] // empty' "$WORKTREE_PORT_REGISTRY")
        tmp=$(mktemp)
        jq --arg k "$key" 'del(.[$k])' \
            "$WORKTREE_PORT_REGISTRY" > "$tmp" && mv "$tmp" "$WORKTREE_PORT_REGISTRY"
        echo "$port $key"
    done < <(jq -r 'keys[]' "$WORKTREE_PORT_REGISTRY" 2>/dev/null)
    return 0
}

assign_port() {
    [[ $# -eq 1 ]] || { echo "assign_port <worktree-dir>" >&2; return 1; }
    _wp_with_lock _wp_assign_inner "$1"
}

prune_ports() {
    _wp_with_lock _wp_prune_inner
}

free_port() {
    [[ $# -eq 1 ]] || { echo "free_port <worktree-dir>" >&2; return 1; }
    _wp_with_lock _wp_free_inner "$1"
}

lookup_port() {
    [[ $# -eq 1 ]] || { echo "lookup_port <worktree-dir>" >&2; return 1; }
    _wp_ensure_registry
    jq -r --arg k "$1" '.[$k] // empty' "$WORKTREE_PORT_REGISTRY"
}

list_ports() {
    _wp_ensure_registry
    jq -r 'to_entries | sort_by(.value) | .[] | "\(.value)\t\(.key)"' "$WORKTREE_PORT_REGISTRY"
}
