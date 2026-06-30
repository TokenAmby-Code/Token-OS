#!/usr/bin/env bash
# 410 GONE tombstone: tmux CLI exterminatus 2026-06-30.
{
    cat >&2 <<'__TMUX_410_TOMBSTONE__'
410 GONE: cli-tools/lib/tmux-runtime-cleanup.sh (tmux-runtime-cleanup.sh) is tombstoned by the 2026-06-30 tmux CLI exterminatus.
This cold tmux feature surface must not be used as an active runtime/control path.
Daemon-native replacement: tmuxctld POST /hooks/wrapperend or POST /clear-runtime.
Original body is retained below this early-return as the emergency restore lever; lift only this tombstone block to prove an active blocker, build/cut over the daemon-native replacement, then restore the 410.
__TMUX_410_TOMBSTONE__
}
return 410 2>/dev/null || exit 410

# --- ORIGINAL BODY BELOW: emergency restore lever, intentionally dead under the 410. ---
# Shared pane-scoped runtime cleanup for agent wrapper exit and operator closeout.

# Usage: tmux_runtime_cleanup_pane <pane> [--preserve-persona-guard]
tmux_runtime_cleanup_pane() {
    local pane="${1:-}"
    local preserve_persona_guard="${2:-}"
    [[ -n "$pane" ]] || return 0
    command -v tmux >/dev/null 2>&1 || return 0

    # Clear visible title/style chrome that belongs to the departed runtime.
    tmux select-pane -t "$pane" -T "" >/dev/null 2>&1 || true
    tmux set-option -pu -t "$pane" window-style >/dev/null 2>&1 || true
    tmux set-option -pu -t "$pane" window-active-style >/dev/null 2>&1 || true

    local opt
    for opt in \
        @INSTANCE_ID \
        @PERSONA \
        @SESSION_DOC \
        @CWD \
        @CC_STATE \
        @PANE_LABEL \
        @PANE_CLEAN \
        @PANE_BORN \
        @ACTIVE_TITLE \
        @PROGRESS_TITLE \
        @PANE_PROGRESS \
        @TTS_STATE \
        @OPS_SELECTED \
        @CONTEXT_INFO \
        @STACK_PENDING \
        @GT_FIRE \
        @PLANNING_STATE \
        @PLANNING_AGENT \
        @TYPING_LOCK_UNTIL \
        @TYPING_PENDING_UNTIL \
        @TYPING_AGENT_UNTIL \
        @GUARD \
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
