#!/usr/bin/env bash
# tmux-state.sh — Runtime state model and invariant helpers for tmux workspace
#
# Bash has no compile-time type safety. This file provides the next best thing:
# centralized enum-like constants, parsers, and validation helpers so scripts
# can work from the architecture's state vocabulary instead of ad hoc strings.
#
# The Mac is the only host that runs tmux now, so layout-origin distinctions
# (mac vs wsl) are gone. Window archetypes are: palace (4-pane H layout),
# somnium (left side rail + right 2x2), legion, mechanicus / mars / kreig.
# TUI panes/windows are legacy compatibility surfaces, not default topology.

TMUX_GRID_STATE_SMALL="small"
TMUX_GRID_STATE_SIDE="side"
TMUX_GRID_STATE_MINI="mini"
# Deprecated compatibility state; canonical side rails use "side".
TMUX_GRID_STATE_TALL="tall-grid"

TMUX_PANE_TYPE_LEGION="legion"
TMUX_PANE_TYPE_MECHANICUS="mechanicus"

tmux_is_valid_grid_state() {
    case "${1:-}" in
        "$TMUX_GRID_STATE_SMALL"|"$TMUX_GRID_STATE_SIDE"|"$TMUX_GRID_STATE_MINI"|"$TMUX_GRID_STATE_TALL") return 0 ;;
        *) return 1 ;;
    esac
}

tmux_grid_state_from_pane_id() {
    local pane_id="${1:-}" pos
    pos="${pane_id#*:}"
    case "$pane_id" in
        palace:W|palace:E) echo "$TMUX_GRID_STATE_SIDE" ;;
        somnium:W) echo "$TMUX_GRID_STATE_SIDE" ;;
        palace:N|palace:S|somnium:N|somnium:NE|somnium:S|somnium:SE|*:[0-9]*) echo "$TMUX_GRID_STATE_SMALL" ;;
        *) return 1 ;;
    esac
}

tmux_pane_type_from_pane_id() {
    local pane_id="${1:-}" win
    win="${pane_id%%:*}"
    case "$win" in
        mechanicus|mars|kreig) echo "$TMUX_PANE_TYPE_MECHANICUS" ;;
        legion) echo "$TMUX_PANE_TYPE_LEGION" ;;
        *) return 1 ;;
    esac
}

tmux_is_valid_pane_slot() {
    case "${1:-}" in
        palace:W|palace:N|palace:S|palace:E) return 0 ;;
        somnium:W|somnium:N|somnium:NE|somnium:S|somnium:SE) return 0 ;;
        palace:WW|palace:NW|palace:SW|palace:NE|palace:SE|palace:EE) return 0 ;;
        somnium:NW|somnium:SW|somnium:NE|somnium:SE|somnium:EE) return 0 ;;
        palace:SL|palace:TL|palace:BL|palace:TR|palace:BR|palace:SR) return 0 ;;
        somnium:TL|somnium:BL|somnium:TR|somnium:BR|somnium:SR) return 0 ;;
        mechanicus:*|mars:*|kreig:*|legion:*) return 0 ;;
        *) return 1 ;;
    esac
}

tmux_count_panes_with_grid_state() {
    local target="$1" want="$2"
    tmux list-panes -t "$target" -F '#{@GRID_STATE}' 2>/dev/null | grep -c "^${want}$" || true
}
