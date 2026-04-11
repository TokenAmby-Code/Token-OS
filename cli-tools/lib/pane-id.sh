#!/usr/bin/env bash
# pane-id.sh — Human-readable tmux pane ID system
# Sourced by tmux-workspace, tx, and other tmux tools.
#
# Pane IDs use the format window:position (e.g., palace:TR, warp:MON, kreig:1).
# Stored as @PANE_ID tmux pane option. Resolves to tmux pane target (%N).
#
# Palace positions:
#   Bridge (Mac):  TL TR BL BR SR
#   Grid (WSL):    SL TL BL TR BR SR
# Warp positions:  MON T B
# Kreig/Legion:    incrementing integers (1, 2, 3...)
# TUI:             1

# Set @PANE_ID on a pane and derive @GRID_STATE / @PANE_TYPE for backward compat.
pane_tag() {
    local target="$1" pane_id="$2"
    tmux set-option -p -t "$target" @PANE_ID "$pane_id"

    # Derive @GRID_STATE from position
    local pos="${pane_id#*:}"
    case "$pos" in
        SL|SR)  tmux set-option -p -t "$target" @GRID_STATE "side" ;;
        MON)    tmux set-option -p -t "$target" @GRID_STATE "mini" ;;
        TL|TR|BL|BR|T|B|[0-9]*) tmux set-option -p -t "$target" @GRID_STATE "small" ;;
    esac

    # Derive @PANE_TYPE from window prefix
    local win="${pane_id%%:*}"
    case "$win" in
        kreig)  tmux set-option -p -t "$target" @PANE_TYPE "kreig" ;;
        legion) tmux set-option -p -t "$target" @PANE_TYPE "legion" ;;
        tui)    tmux set-option -p -t "$target" @PANE_TYPE "tui" ;;
    esac
    # warp:MON and palace:SR (mac-palace) get @PANE_TYPE "tui" — set by caller
}

# Resolve a pane ID to a tmux pane target (e.g., palace:TR → %17).
# Usage: tmux send-keys -t "$(pane_resolve palace:TR)" "echo hi" Enter
pane_resolve() {
    local id="$1"
    tmux list-panes -a -F '#{pane_id} #{@PANE_ID}' 2>/dev/null \
        | awk -v id="$id" '$2 == id { print $1; exit }'
}

# List all pane IDs and their tmux targets.
# Output: %0 palace:TL\n%1 palace:BL\n...
pane_list() {
    tmux list-panes -a -F '#{pane_id} #{@PANE_ID}' 2>/dev/null \
        | awk '$2 != "" && $2 != "(null)" { print }'
}

# Get the @PANE_ID for a given tmux pane target.
# Usage: pane_id_of %5  →  palace:TR
pane_id_of() {
    local target="$1"
    tmux show-options -pv -t "$target" @PANE_ID 2>/dev/null || echo ""
}
