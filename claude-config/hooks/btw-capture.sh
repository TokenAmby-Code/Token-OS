#!/bin/bash
# btw-capture.sh — Brain dump reprompt via /btw sidecar
#
# Bound to a tmux keybinding. Assumes user has typed a brain dump
# in the Claude Code prompt bar.
#
# Flow:
#   1. PgUp x50 + Home to reach (0,0) in the prompt
#   2. Type /btw reformat instruction prefix
#   3. Submit with Enter
#   4. Poll tmux capture-pane for "dismiss" sentinel
#   5. Escape to dismiss /btw panel
#   6. Ctrl+C to clear prompt bar
#   7. send-keys the cleaned text (no Enter — user reviews first)
#
# Usage: tmux bind-key B run-shell "bash ~/.claude/hooks/btw-capture.sh"

# Don't use set -e — we need to handle failures gracefully
set -uo pipefail

PANE="${TMUX_PANE:-}"
if [[ -z "$PANE" ]]; then
    PANE=$(tmux display-message -p '#{pane_id}' 2>/dev/null) || exit 1
fi

LOG_FILE="${HOME}/.claude/logs/btw-capture.log"
DUMP_FILE="${HOME}/.claude/logs/btw-pane-dump.txt"
mkdir -p "${HOME}/.claude/logs"

log() {
    echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"
}

log "Starting btw-capture on pane $PANE"

# --- Step 1: Navigate to (0,0) in prompt bar ---
for _ in $(seq 1 50); do
    tmux send-keys -t "$PANE" PgUp
done
tmux send-keys -t "$PANE" Home
sleep 0.1

# --- Step 2: Type /btw reformat prefix ---
BTW_PREFIX="/btw reformat this into a structured prompt. group like concepts and fix dictation artifacts. if something doesn't make sense consider homophones. do not change content, preserve all topics even strange stubs or random thoughts. just text organization, nothing else. here is the brain dump: "

tmux send-keys -t "$PANE" -l "$BTW_PREFIX"
sleep 0.1

# --- Step 2b: Navigate to end of prompt bar, append delimiter ---
for _ in $(seq 1 50); do
    tmux send-keys -t "$PANE" PgDn
done
tmux send-keys -t "$PANE" End
tmux send-keys -t "$PANE" -l " ~~BTWEND~~"
sleep 0.1

# --- Step 3: Submit ---
tmux send-keys -t "$PANE" Enter

log "Submitted /btw, polling for response"

# --- Step 4: Poll for dismiss sentinel ---
MAX_WAIT=120
ELAPSED=0

while (( ELAPSED < MAX_WAIT )); do
    sleep 1
    ELAPSED=$((ELAPSED + 1))

    CONTENT=$(tmux capture-pane -p -t "$PANE" -S -200 2>/dev/null) || continue

    if echo "$CONTENT" | grep -qE "Press (Space|Enter|Escape).*dismiss"; then
        log "Response complete after ${ELAPSED}s"

        # Dump raw pane content for debugging
        echo "$CONTENT" > "$DUMP_FILE"
        log "Raw pane dumped to $DUMP_FILE"

        # --- Step 5: Extract text between -<>- delimiter and dismiss sentinel ---
        CLEANED=$(echo "$CONTENT" | python3 -c "
import sys

lines = sys.stdin.read().split('\n')

# Find dismiss line (scan from bottom)
end = None
for i in range(len(lines) - 1, -1, -1):
    if 'dismiss' in lines[i].lower() and 'press' in lines[i].lower():
        end = i
        break

if end is None:
    print('')
    sys.exit(0)

# Find the -<>- delimiter (last occurrence before dismiss)
start = None
for i in range(end - 1, -1, -1):
    if '~~BTWEND~~' in lines[i]:
        start = i + 1
        break

if start is None or start >= end:
    print('')
    sys.exit(0)

result = []
for line in lines[start:end]:
    cleaned = line
    for ch in '\u2502\u250c\u2510\u2514\u2518\u251c\u2524\u252c\u2534\u253c\u2500\u256d\u256e\u256f\u2570':
        cleaned = cleaned.replace(ch, '')
    cleaned = cleaned.strip()
    if cleaned:
        result.append(cleaned)

print('\n'.join(result))
" 2>/dev/null) || CLEANED=""

        if [[ -z "$CLEANED" ]]; then
            log "Failed to parse cleaned text — check $DUMP_FILE"
            tmux send-keys -t "$PANE" Escape
            exit 1
        fi

        log "Captured ${#CLEANED} chars"

        # --- Step 6: Dismiss /btw panel ---
        tmux send-keys -t "$PANE" Escape
        sleep 1

        # --- Step 7: Clear prompt bar ---
        tmux send-keys -t "$PANE" C-c
        sleep 1

        # --- Step 8: Send cleaned text + Enter ---
        tmux send-keys -t "$PANE" -l "$CLEANED"
        sleep 0.3
        tmux send-keys -t "$PANE" Enter

        log "Done — sent cleaned text + Enter"
        exit 0
    fi
done

log "Timed out after ${MAX_WAIT}s"
exit 1
