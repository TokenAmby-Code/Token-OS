#!/usr/bin/env bash
# pane-id.sh — Human-readable tmux pane ID system
# Sourced by tx, vault-dispatch, tmuxctl, and other tmux tools.
#
# Pane IDs use the format window:position (e.g., palace:NE, mechanicus:1).
# Stored as @PANE_ID tmux pane option. Resolves to tmux pane target (%N).
#
# Palace positions:  WW NW SW NE SE EE  (6-pane: side columns flank a 2x2 grid)
# Somnium positions: NW NE SW SE EE     (5-pane: 2x2 grid + right TUI column)
# Mechanicus/Legion: incrementing integers (1, 2, 3...)
# TUI:               1

_TMUX_STATE_LIB_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# shellcheck source=./tmux-state.sh
source "${_TMUX_STATE_LIB_DIR}/tmux-state.sh" 2>/dev/null || true

pane_canonical_id() {
    local pane_id="${1:-}" window pos
    window="${pane_id%:*}"
    pos="${pane_id#*:}"
    case "$pos" in
        TL) pos="NW" ;;
        TR) pos="NE" ;;
        BL) pos="SW" ;;
        BR) pos="SE" ;;
        SL) pos="WW" ;;
        SR) pos="EE" ;;
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
        "$TMUX_PANE_TYPE_MECHANICUS"|"$TMUX_PANE_TYPE_LEGION"|"$TMUX_PANE_TYPE_TUI")
            tmux set-option -p -t "$target" @PANE_TYPE "$pane_type"
            ;;
    esac
    # somnium:EE gets @PANE_TYPE "tui" — set by caller
}

# Resolve a pane ID to a tmux pane target (e.g., palace:NE → %17).
# Usage: tmux send-keys -t "$(pane_resolve palace:NE)" "echo hi" Enter
pane_resolve() {
    local id="$1"
    local resolved
    id="$(pane_canonical_id "$id")"
    resolved=$(PYTHONPATH="${_TMUX_STATE_LIB_DIR}${PYTHONPATH:+:$PYTHONPATH}" \
        python3 -m tmuxctl.cli resolve-pane "$id" 2>/dev/null \
        | awk -F': ' '$1 == "pane_id" { print $2; exit }' || true)
    if [[ -n "$resolved" ]]; then
        echo "$resolved"
        return 0
    fi
    tmux list-panes -a -F '#{pane_id} #{@PANE_ID}' 2>/dev/null \
        | awk -v id="$id" '$2 == id { print $1; exit }'
}

# List all pane IDs and their tmux targets.
# Output: %0 palace:NW\n%1 palace:SW\n...
pane_list() {
    tmux list-panes -a -F '#{pane_id} #{@PANE_ID}' 2>/dev/null \
        | awk '$2 != "" && $2 != "(null)" { print }'
}

# Get the @PANE_ID for a given tmux pane target.
# Usage: pane_id_of %5  →  palace:NE
pane_id_of() {
    local target="$1"
    tmux show-options -pv -t "$target" @PANE_ID 2>/dev/null || echo ""
}
