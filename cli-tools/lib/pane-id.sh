#!/usr/bin/env bash
# 410 GONE tombstone: tmux CLI exterminatus 2026-06-30.
{
    cat >&2 <<'__TMUX_410_TOMBSTONE__'
410 GONE: cli-tools/lib/pane-id.sh (pane-id.sh) is tombstoned by the 2026-06-30 tmux CLI exterminatus.
This cold tmux feature surface must not be used as an active runtime/control path.
Daemon-native replacement: tmuxctld GET /translate-ids or GET /resolve-pane.
Original body is retained below this early-return as the emergency restore lever; lift only this tombstone block to prove an active blocker, build/cut over the daemon-native replacement, then restore the 410.
__TMUX_410_TOMBSTONE__
}
return 410 2>/dev/null || exit 410

# --- ORIGINAL BODY BELOW: emergency restore lever, intentionally dead under the 410. ---
# pane-id.sh — Human-readable tmux pane ID system
# Sourced by tx, dispatch, tmuxctl, and other tmux tools.
#
# Pane IDs use the format window:position (e.g., palace:N, mechanicus:1).
# Stored as @PANE_ID tmux pane option. Resolves to tmux pane target (%N).
#
# Palace positions:  W N S E          (4-pane H layout)
# Somnium positions: W N NE S SE      (left side rail + right 2x2)
# Council seats:     custodes/pax/malcador/administratum/true-terminal
# Mechanicus:        named persona anchors + numeric/worker roles
# TUI: legacy compatibility only; no default workspace TUI window

_TMUX_STATE_LIB_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
_TOKEN_OS_ROOT="${TOKEN_OS:-$(cd "${_TMUX_STATE_LIB_DIR}/../.." && pwd)}"
_TMUXCTLD_LIB_DIR="${TMUXCTLD_LIB:-${_TOKEN_OS_ROOT}/tmuxctld/lib}"
_CLI_LIB_DIR="${_TOKEN_OS_ROOT}/cli-tools/lib"
# shellcheck source=./tmux-state.sh
source "${_TMUX_STATE_LIB_DIR}/tmux-state.sh" 2>/dev/null || true

pane_canonical_id() {
    local pane_id="${1:-}" window pos
    window="${pane_id%:*}"
    pos="${pane_id#*:}"
    case "${window}:${pos}" in
        palace:WW|palace:SL) pos="W" ;;
        palace:EE|palace:SR) pos="E" ;;
        palace:NW|palace:NE|palace:TL|palace:TR) pos="N" ;;
        palace:SW|palace:SE|palace:BL|palace:BR) pos="S" ;;
        somnium:NW|somnium:SW|somnium:TL|somnium:BL) pos="W" ;;
        somnium:TR) pos="NE" ;;
        somnium:BR) pos="SE" ;;
    esac
    echo "${window}:${pos}"
}

# Set @PANE_ID on a pane and derive @GRID_STATE / @PANE_TYPE for backward compat.
pane_tag() {
    local target="$1" pane_id="$2"
    local grid_state pane_type

    pane_id="$(pane_canonical_id "$pane_id")"

    if ! tmux_is_valid_pane_slot "$pane_id"; then
        echo "pane_tag: invalid pane id '${pane_id}'" >&2
        return 1
    fi

    tmux set-option -p -t "$target" @PANE_ID "$pane_id"

    grid_state=$(tmux_grid_state_from_pane_id "$pane_id" 2>/dev/null || true)
    if tmux_is_valid_grid_state "$grid_state"; then
        tmux set-option -p -t "$target" @GRID_STATE "$grid_state"
    fi

    pane_type=$(tmux_pane_type_from_pane_id "$pane_id" 2>/dev/null || true)
    case "$pane_type" in
        "$TMUX_PANE_TYPE_MECHANICUS"|"$TMUX_PANE_TYPE_LEGION")
            tmux set-option -p -t "$target" @PANE_TYPE "$pane_type"
            ;;
    esac
}

# Resolve a pane ID to a tmux pane target (e.g., palace:N → %17).
# Usage: tmux send-keys -t "$(pane_resolve palace:N)" "echo hi" Enter
pane_resolve() {
    local id="$1"
    local resolved
    id="$(pane_canonical_id "$id")"
    resolved=$(PYTHONPATH="${_TMUXCTLD_LIB_DIR}:${_CLI_LIB_DIR}${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m tmuxctl.cli resolve-pane --format physical "$id" 2>/dev/null || true)
    if [[ -n "$resolved" ]]; then
        echo "$resolved"
        return 0
    fi
    return 1
}

# List all pane IDs and their tmux targets.
# Output: palace:N\npalace:S\n...
pane_list() {
    tmux list-panes -a -F '#{@PANE_ID}' 2>/dev/null \
        | awk '$1 != "" && $1 != "(null)" { print }'
}

# Get the @PANE_ID for a given tmux pane target.
# Usage: pane_id_of %5  →  palace:N
pane_id_of() {
    local target="$1"
    tmux show-options -pv -t "$target" @PANE_ID 2>/dev/null || echo ""
}
