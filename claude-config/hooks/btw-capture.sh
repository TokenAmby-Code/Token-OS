#!/bin/bash
# btw-capture.sh — Brain dump reprompt via /btw sidecar (prefix+B)
#
# Called by: bind B run-shell "bash ~/.claude/hooks/btw-capture.sh >/dev/null 2>&1"
#
# Immediately backgrounds the work so run-shell returns instantly
# (prevents tmux freeze during the 120s poll).
#
# Flow:
#   1. Inject /btw reformat prefix into existing brain dump
#   2. Submit
#   3. Poll for /btw dismiss panel (background)
#   4. Extract output → clipboard (pbcopy)
#   5. Dismiss panel (Escape)
#   6. Clear prompt bar (Ctrl+C)
#   7. Paste into prompt bar (user reviews: Enter to send, Ctrl+C to discard)

set -uo pipefail

LOG_FILE="${HOME}/.claude/logs/btw-capture.log"
mkdir -p "${HOME}/.claude/logs"
log() { echo "[$(date '+%H:%M:%S')] $*" >> "$LOG_FILE"; }

# --- Resolve target pane ---
# run-shell context: display-message resolves to the active pane.
# #{pane_id} strips the % prefix inside run-shell — re-add it.
_id=$(tmux display-message -p '#{pane_id}' 2>/dev/null) || exit 1
if [[ "$_id" == %* ]]; then
    PANE="$_id"
else
    PANE="%${_id}"
fi

# --- Background the entire operation so run-shell returns immediately ---
(
    log "Reprompt on pane $PANE — injecting /btw prefix"

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

    # --- Step 3: Navigate to end, append delimiter, submit ---
    for _ in $(seq 1 50); do
        tmux send-keys -t "$PANE" PgDn
    done
    tmux send-keys -t "$PANE" End
    sleep 0.1
    tmux send-keys -t "$PANE" -l " <<<END>>>"
    sleep 0.1
    tmux send-keys -t "$PANE" Enter
    log "Submitted /btw reprompt, polling for response"

    # --- Step 4: Poll for btw completion ---
    # Wait for "Answering..." to appear (btw started), then wait for it
    # to disappear (btw finished). Simpler and more robust than matching
    # the dismiss dialog text which changes across versions.
    MAX_WAIT=120
    ELAPSED=0
    SAW_ANSWERING=false
    while (( ELAPSED < MAX_WAIT )); do
        sleep 1
        ELAPSED=$((ELAPSED + 1))

        CONTENT=$(tmux capture-pane -p -t "$PANE" -S -200 2>/dev/null) || continue

        if tmux capture-pane -p -t "$PANE" -S -3 2>/dev/null | grep -q "Answering"; then
            SAW_ANSWERING=true
            continue
        fi

        # Answering disappeared — btw is done (or never started yet)
        if [[ "$SAW_ANSWERING" == true ]]; then
            log "btw complete after ${ELAPSED}s"
            echo "$CONTENT" > "${HOME}/.claude/logs/btw-pane-dump.txt"

            # --- Step 5: Extract /btw output → clipboard ---
            CLEANED=$(echo "$CONTENT" | python3 -c '
import sys

lines = sys.stdin.read().split("\n")

# Find dismiss line (scan from bottom)
end = None
for i in range(len(lines) - 1, -1, -1):
    if "dismiss" in lines[i].lower() and "escape" in lines[i].lower():
        end = i
        break
if end is None:
    sys.exit(1)

# Find where the btw response starts.
# Scan bottom-up from dismiss for <<<END>>> delimiter. The btw panel
# echoes the submitted command (indented), so <<<END>>> appears twice:
#   ❯ /btw ... <<<END>>>        ← original prompt (starts with ❯)
#     /btw... <<<END>>>          ← indented echo
#     response text...
#   dismiss line
# We need the SECOND hit scanning up (skip the indented echo).
btw_echo_end = None
hits = 0

for i in range(end - 1, -1, -1):
    if "<<<END>>>" in lines[i]:
        hits += 1
        if hits == 2:
            btw_echo_end = i
            break
        # First hit is the echo — record it as fallback
        if hits == 1:
            btw_echo_end = i

if btw_echo_end is None:
    btw_echo_end = max(0, end - 20)

start = btw_echo_end + 1
while start < end and not lines[start].strip():
    start += 1

if start >= end:
    sys.exit(1)

box_strip = "\u2502\u250c\u2510\u2514\u2518\u251c\u2524\u252c\u2534\u253c\u2500\u256d\u256e\u256f\u2570"
result = []
for line in lines[start:end]:
    cleaned = line
    for ch in box_strip:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    if cleaned:
        result.append(cleaned)

if not result:
    sys.exit(1)
print("\n".join(result))
' 2>/dev/null)

            if [[ -z "$CLEANED" ]]; then
                log "Failed to parse btw output — check btw-pane-dump.txt"
                tmux send-keys -t "$PANE" Escape
                exit 1
            fi

            echo "$CLEANED" | pbcopy
            log "Captured ${#CLEANED} chars to clipboard"

            # --- Step 6: Dismiss panel ---
            tmux send-keys -t "$PANE" Escape
            sleep 0.5

            # --- Step 7: Clear prompt bar ---
            tmux send-keys -t "$PANE" C-c
            sleep 0.5

            # --- Step 8: Paste into prompt bar (bracketed paste) ---
            echo "$CLEANED" | tmux load-buffer -
            tmux paste-buffer -p -t "$PANE"

            log "Done — pasted into prompt bar"
            exit 0
        fi
    done

    log "Timed out after ${MAX_WAIT}s"
) &
disown

exit 0
