#!/usr/bin/env bash
# Shared pane-scoped runtime cleanup for agent wrapper exit and operator closeout.

# Usage: tmux_runtime_cleanup_pane <pane> [--preserve-persona-guard]
tmux_runtime_cleanup_pane() {
    local pane="${1:-}"
    local preserve_persona_guard="${2:-}"
    [[ -n "$pane" ]] || return 0
    command -v tmux >/dev/null 2>&1 || return 0

    # Clear visible title/style chrome that belongs to the departed runtime.
    tmux select-pane -t "$pane" -T "" >/dev/null 2>&1 || true
    tmux select-pane -t "$pane" -P "bg=default,fg=default" >/dev/null 2>&1 || true

    local opt
    for opt in \
        @INSTANCE_ID \
        @CC_STATE \
        @PANE_LABEL \
        @ACTIVE_TITLE \
        @PROGRESS_TITLE \
        @PANE_PROGRESS \
        @PANE_TITLE_SUPPRESS \
        @TTS_STATE \
        @CONTEXT_INFO \
        @STACK_PENDING \
        @GT_FIRE \
        @PLANNING_STATE \
        @PLANNING_AGENT \
        @DISCORD_VOICE_LOCK \
        @DISCORD_VOICE_PROCESSING \
        @TOKEN_API_WRAPPER_LAUNCH_ID \
        @TOKEN_API_ENGINE \
        @TOKEN_API_LAUNCHER \
        @TOKEN_API_CWD \
        @TOKEN_API_SESSION_ID \
        @TOKEN_API_DISPATCH_TARGET \
        @TOKEN_API_DISPATCH_WINDOW \
        @TOKEN_API_DISPATCH_MODE \
        @TOKEN_API_DISPATCH_SLOT \
        @TOKEN_API_LAUNCH_MODE \
        @TOKEN_API_TARGET_WORKING_DIR
    do
        tmux set-option -pu -t "$pane" "$opt" >/dev/null 2>&1 || true
    done

    if [[ "$preserve_persona_guard" != "--preserve-persona-guard" ]]; then
        tmux set-option -pu -t "$pane" @PERSONA_ASSERT_GUARD >/dev/null 2>&1 || true
    fi
}

# Usage: tmux_runtime_stamp_wrapper <pane> <wrapper_launch_id> <engine> <launcher> <cwd>
tmux_runtime_stamp_wrapper() {
    local pane="${1:-}" wrapper_launch_id="${2:-}" engine="${3:-}" launcher="${4:-}" cwd="${5:-}"
    [[ -n "$pane" ]] || return 0
    command -v tmux >/dev/null 2>&1 || return 0
    [[ -n "$wrapper_launch_id" ]] && tmux set-option -p -t "$pane" @TOKEN_API_WRAPPER_LAUNCH_ID "$wrapper_launch_id" >/dev/null 2>&1 || true
    [[ -n "$engine" ]] && tmux set-option -p -t "$pane" @TOKEN_API_ENGINE "$engine" >/dev/null 2>&1 || true
    [[ -n "$launcher" ]] && tmux set-option -p -t "$pane" @TOKEN_API_LAUNCHER "$launcher" >/dev/null 2>&1 || true
    [[ -n "$cwd" ]] && tmux set-option -p -t "$pane" @TOKEN_API_CWD "$cwd" >/dev/null 2>&1 || true
}
