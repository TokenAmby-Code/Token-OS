#!/usr/bin/env bash
# origin.sh — I-side primitive: resolve the invoking actor
# TAGS: shell, origin, attribution, shared
#
# Contract: see cli-tools/lib/ORIGIN.md
#
# Answers "who is invoking this?" — dynamic, per-invocation.
# Counterpart to nas-path.sh (static "what is machine X?").
#
# Every resolver follows the same three-step hierarchy:
#   1. Env override   — IMPERIUM_ORIGIN_<SLOT>
#   2. Cache file     — ${TMPDIR:-/tmp}/imperium-origin-<client_pid>.<slot>
#   3. Live resolution
#
# Resolvers echo a single line on stdout. No other output. No exit on failure.
#
# Requires: nas-path.sh already sourced (for imperium_cfg + IMPERIUM_MACHINE).

[[ -n "${_IMPERIUM_ORIGIN_LOADED:-}" ]] && return 0 2>/dev/null
_IMPERIUM_ORIGIN_LOADED=1

# ============================================================
# Internal helpers
# ============================================================

# Get the tmux client PID for the invoking client, or empty if not in tmux.
_origin_client_pid() {
    [[ -n "${1:-}" ]] && { echo "$1"; return; }
    # No tmux context at all → no client_pid
    [[ -z "${TMUX:-}" && -z "${TMUX_PANE:-}" ]] && return
    tmux display-message -p '#{client_pid}' 2>/dev/null || true
}

# Cache file path for a given client_pid + slot.
_origin_cache_path() {
    local client_pid="$1" slot="$2"
    [[ -z "$client_pid" ]] && return 1
    echo "${TMPDIR:-/tmp}/imperium-origin-${client_pid}.${slot}"
}

# Walk up the process tree from $1 looking for an sshd ancestor.
# Echoes the sshd PID, or nothing if no sshd ancestor found.
_origin_find_sshd_ancestor() {
    local pid="$1"
    local depth=0
    while (( depth++ < 20 )) && [[ -n "$pid" && "$pid" != "1" && "$pid" != "0" ]]; do
        local comm
        comm=$(ps -o comm= -p "$pid" 2>/dev/null | tr -d ' \t\n')
        # sshd may appear as "sshd", "sshd:", or a full path ending in /sshd
        case "$comm" in
            *sshd|*sshd:*) echo "$pid"; return 0 ;;
        esac
        pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' \t\n')
    done
    return 1
}

# Get the remote peer IP for a given sshd PID.
# Mac: lsof -p <pid> -i -n -P (no /proc).
# Linux: /proc/<pid>/environ SSH_CONNECTION.
_origin_ssh_peer_ip() {
    local pid="$1"
    if [[ "$(uname)" == "Darwin" ]]; then
        lsof -p "$pid" -i -n -P 2>/dev/null \
            | awk '/ESTABLISHED/ {
                n = split($9, a, "->")
                if (n == 2) {
                    split(a[2], b, ":")
                    print b[1]
                    exit
                }
            }'
    elif [[ -r "/proc/$pid/environ" ]]; then
        tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
            | awk -F= '/^SSH_CONNECTION=/ { split($2, a, " "); print a[1]; exit }'
    fi
}

# Pure mapping: peer IP → machine id via imperium_cfg tailscale_ip.
# Exposed as an internal so tests can exercise it without process mocking.
_origin_machine_from_ip() {
    local ip="$1"
    [[ -z "$ip" ]] && return 1
    local candidate
    for candidate in mac wsl phone linux; do
        if [[ "$(imperium_cfg tailscale_ip "$candidate" 2>/dev/null)" == "$ip" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    echo "unknown"
}

# ============================================================
# Resolvers
# ============================================================

# Resolve the machine that invoked this action.
# Usage: origin_machine [client_pid]
#
# Three transport paths:
#   1. tmux — walk client_pid up to sshd ancestor, read remote peer IP
#   2. bare SSH (no tmux) — read SSH_CONNECTION from current env
#   3. local shell — fall back to $IMPERIUM_MACHINE (self)
origin_machine() {
    # Env override wins over everything
    if [[ -n "${IMPERIUM_ORIGIN_MACHINE:-}" ]]; then
        echo "$IMPERIUM_ORIGIN_MACHINE"
        return 0
    fi

    local client_pid cache_file=""
    client_pid=$(_origin_client_pid "${1:-}")

    # Cache only applies when we have a stable client_pid
    if [[ -n "$client_pid" ]]; then
        cache_file=$(_origin_cache_path "$client_pid" machine)
        if [[ -r "$cache_file" ]]; then
            cat "$cache_file"
            return 0
        fi
    fi

    local resolved
    if [[ -n "$client_pid" ]]; then
        # tmux context: walk process tree for sshd ancestor
        local sshd_pid peer_ip
        if sshd_pid=$(_origin_find_sshd_ancestor "$client_pid"); then
            peer_ip=$(_origin_ssh_peer_ip "$sshd_pid")
            if [[ -n "$peer_ip" ]]; then
                resolved=$(_origin_machine_from_ip "$peer_ip")
            else
                resolved="unknown"
            fi
        else
            resolved="${IMPERIUM_MACHINE:-unknown}"
        fi
    elif [[ -n "${SSH_CONNECTION:-}" ]]; then
        # Bare SSH shell (no tmux): SSH_CONNECTION="<client_ip> <client_port> <server_ip> <server_port>"
        local peer_ip="${SSH_CONNECTION%% *}"
        resolved=$(_origin_machine_from_ip "$peer_ip")
    else
        # Local shell on this machine
        resolved="${IMPERIUM_MACHINE:-unknown}"
    fi

    [[ -n "$cache_file" ]] && echo "$resolved" > "$cache_file" 2>/dev/null
    echo "$resolved"
}

# Resolve the device_id (canonical device name) of the invoker.
origin_device_id() {
    if [[ -n "${IMPERIUM_ORIGIN_DEVICE_ID:-}" ]]; then
        echo "$IMPERIUM_ORIGIN_DEVICE_ID"
        return 0
    fi
    local m
    m=$(origin_machine)
    [[ -z "$m" || "$m" == "unknown" ]] && { echo ""; return 0; }
    imperium_cfg device_name "$m" 2>/dev/null
}

# Resolve the tmux pane the invocation is running in.
origin_pane() {
    if [[ -n "${IMPERIUM_ORIGIN_PANE:-}" ]]; then
        echo "$IMPERIUM_ORIGIN_PANE"
        return 0
    fi
    if [[ -n "${TMUX_PANE:-}" ]]; then
        echo "$TMUX_PANE"
        return 0
    fi
    tmux display-message -p '#{pane_id}' 2>/dev/null || true
}

# STUB: resolve the Claude instance the invocation belongs to.
# TODO: call GET /api/instances/resolve with pid + pane + cwd.
origin_instance() {
    if [[ -n "${IMPERIUM_ORIGIN_INSTANCE:-}" ]]; then
        echo "$IMPERIUM_ORIGIN_INSTANCE"
        return 0
    fi
    echo ""
}

# STUB: resolve the invoker's geofence (home|away|unknown).
# TODO: call Token-API geofence endpoint; for phone originators, consult phone heartbeat.
origin_geofence() {
    if [[ -n "${IMPERIUM_ORIGIN_GEOFENCE:-}" ]]; then
        echo "$IMPERIUM_ORIGIN_GEOFENCE"
        return 0
    fi
    echo "unknown"
}

# Describe which transport delivered the invocation.
origin_transport() {
    if [[ -n "${IMPERIUM_ORIGIN_TRANSPORT:-}" ]]; then
        echo "$IMPERIUM_ORIGIN_TRANSPORT"
        return 0
    fi
    if [[ -n "${TMUX:-}" || -n "${TMUX_PANE:-}" ]]; then
        echo "tmux"
    elif [[ -n "${SSH_CONNECTION:-}" ]]; then
        echo "ssh"
    else
        echo "local"
    fi
}

# Emit a JSON record of every resolved slot.
# Minimal JSON — no jq dependency. Values are shell-quoted-free so the slots
# must not contain quotes or backslashes (they don't, by construction).
origin_record() {
    local machine device pane instance geofence transport
    machine=$(origin_machine)
    device=$(origin_device_id)
    pane=$(origin_pane)
    instance=$(origin_instance)
    geofence=$(origin_geofence)
    transport=$(origin_transport)
    cat <<JSON
{"machine":"$machine","device_id":"$device","pane":"$pane","instance_id":"$instance","geofence":"$geofence","transport":"$transport"}
JSON
}

# Clear all cache files for a given client_pid (on detach, for example).
origin_cache_clear() {
    local client_pid="${1:-$(_origin_client_pid)}"
    [[ -z "$client_pid" ]] && return 0
    rm -f "${TMPDIR:-/tmp}/imperium-origin-${client_pid}".* 2>/dev/null || true
}
