#!/usr/bin/env bash
# tmux-state.sh — Runtime state model and invariant helpers for tmux workspace
#
# Bash has no compile-time type safety. This file provides the next best thing:
# centralized enum-like constants, parsers, and validation helpers so scripts
# can work from the architecture's state vocabulary instead of ad hoc strings.

TMUX_LAYOUT_WSL="wsl"
TMUX_LAYOUT_MAC="mac"

TMUX_GRID_STATE_SMALL="small"
TMUX_GRID_STATE_SIDE="side"
TMUX_GRID_STATE_MINI="mini"

TMUX_PANE_TYPE_TUI="tui"
TMUX_PANE_TYPE_LEGION="legion"
TMUX_PANE_TYPE_MECHANICUS="mechanicus"

tmux_is_valid_layout_origin() {
    case "${1:-}" in
        "$TMUX_LAYOUT_WSL"|"$TMUX_LAYOUT_MAC") return 0 ;;
        *) return 1 ;;
    esac
}

tmux_is_valid_grid_state() {
    case "${1:-}" in
        "$TMUX_GRID_STATE_SMALL"|"$TMUX_GRID_STATE_SIDE"|"$TMUX_GRID_STATE_MINI") return 0 ;;
        *) return 1 ;;
    esac
}

tmux_grid_state_from_pane_id() {
    local pane_id="${1:-}" pos
    pos="${pane_id#*:}"
    case "$pos" in
        SL|SR) echo "$TMUX_GRID_STATE_SIDE" ;;
        MON)   echo "$TMUX_GRID_STATE_MINI" ;;
        TL|TR|BL|BR|T|B|[0-9]*) echo "$TMUX_GRID_STATE_SMALL" ;;
        *) return 1 ;;
    esac
}

tmux_pane_type_from_pane_id() {
    local pane_id="${1:-}" win
    win="${pane_id%%:*}"
    case "$win" in
        mechanicus|mars|kreig) echo "$TMUX_PANE_TYPE_MECHANICUS" ;;
        legion) echo "$TMUX_PANE_TYPE_LEGION" ;;
        tui)    echo "$TMUX_PANE_TYPE_TUI" ;;
        *) return 1 ;;
    esac
}

tmux_is_valid_pane_slot() {
    case "${1:-}" in
        palace:SL|palace:TL|palace:BL|palace:TR|palace:BR|palace:SR) return 0 ;;
        somnium:TL|somnium:BL|somnium:TR|somnium:BR|somnium:SR) return 0 ;;
        bridge:TL|bridge:BL|bridge:TR|bridge:BR|bridge:SR) return 0 ;;
        warp:MON|warp:T|warp:B) return 0 ;;
        tui:1) return 0 ;;
        mechanicus:*|mars:*|kreig:*|legion:*) return 0 ;;
        *) return 1 ;;
    esac
}

tmux_count_panes_with_grid_state() {
    local target="$1" want="$2"
    tmux list-panes -t "$target" -F '#{@GRID_STATE}' 2>/dev/null | grep -c "^${want}$" || true
}

tmux_infer_layout_origin() {
    local target="$1" window_base="$2"
    local side_count origin=""
    side_count=$(tmux_count_panes_with_grid_state "$target" "$TMUX_GRID_STATE_SIDE")

    case "$window_base" in
        somnium|bridge)
            origin="$TMUX_LAYOUT_MAC"
            ;;
        palace)
            if (( side_count >= 2 )); then
                origin="$TMUX_LAYOUT_WSL"
            elif (( side_count == 1 )); then
                origin="$TMUX_LAYOUT_MAC"
            fi
            ;;
    esac

    [[ -n "$origin" ]] && printf '%s\n' "$origin"
}

tmux_require_layout_origin() {
    local target="$1" window_base="$2" origin
    origin=$(tmux show-options -wv -t "$target" @LAYOUT_ORIGIN 2>/dev/null || echo "")
    if tmux_is_valid_layout_origin "$origin"; then
        printf '%s\n' "$origin"
        return 0
    fi

    origin=$(tmux_infer_layout_origin "$target" "$window_base")
    if tmux_is_valid_layout_origin "$origin"; then
        tmux set-option -w -t "$target" @LAYOUT_ORIGIN "$origin" 2>/dev/null || true
        printf '%s\n' "$origin"
        return 0
    fi

    return 1
}
