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
#   3. Poll for /btw completion (Answering appears → disappears)
#   4. Extract output → clipboard (panel "c to copy", scrape fallback)
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
    # a dialog string which changes across versions.
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
            # Claude Code v2.1.176+ renders the response in an expandable panel
            # whose footer reads "↑/↓ to scroll · c to copy · f to fork · Esc to
            # close". Primary path: drive the panel's own "c to copy" — under tmux
            # that copy lands in a tmux paste buffer (OSC52), read via show-buffer,
            # not pbpaste. Fallback: scrape the pane, using "esc to close" as the
            # response end boundary.

            # --- Step 5.0: Confirm the response panel rendered ---
            # The footer line is the completion/panel marker (replaces the old
            # "dismiss"+"escape" detection, which no longer exists). Poll briefly;
            # if not seen, nudge focus to the last message with one Up and re-check.
            PANEL_OK=false
            for _ in $(seq 1 5); do
                if tmux capture-pane -p -t "$PANE" -S -80 2>/dev/null | grep -qi "esc to close"; then
                    PANEL_OK=true
                    break
                fi
                sleep 1
            done
            if [[ "$PANEL_OK" != true ]]; then
                tmux send-keys -t "$PANE" Up
                sleep 0.5
                if tmux capture-pane -p -t "$PANE" -S -80 2>/dev/null | grep -qi "esc to close"; then
                    PANEL_OK=true
                fi
            fi
            if [[ "$PANEL_OK" != true ]]; then
                log "Panel footer 'esc to close' not seen — attempting extraction anyway"
            fi

            CLEANED=""

            # --- Step 5a: Primary extraction — panel "c to copy" ---
            # Claude Code's "c to copy" emits an OSC52 clipboard escape. Under
            # tmux (set-clipboard on|external) that copy is captured into a tmux
            # paste BUFFER, not the macOS system clipboard — so we read it back
            # with `tmux show-buffer`, NOT pbpaste. Seed a sentinel buffer first;
            # if pressing "c" pushes a new top buffer, that is the clean response
            # (no box chrome, no echoed prompt). The footer renders whether or
            # not the panel holds keyboard focus, so try "c" directly; if the top
            # buffer is untouched, attempt 1's "c" was typed into the prompt
            # instead — backspace it, send Up to focus the last message, retry.
            BTW_SENTINEL="__BTW_SENTINEL_${RANDOM}_${RANDOM}__"
            tmux set-buffer -- "$BTW_SENTINEL"
            CLIP=""

            tmux send-keys -t "$PANE" c
            sleep 0.4
            _clip=$(tmux show-buffer 2>/dev/null)
            if [[ -n "$_clip" && "$_clip" != "$BTW_SENTINEL" ]]; then
                CLIP="$_clip"
            else
                # Attempt 1 likely typed a literal 'c' into the prompt — remove
                # it, focus the last message, and retry the copy.
                tmux send-keys -t "$PANE" BSpace
                sleep 0.1
                tmux send-keys -t "$PANE" Up
                sleep 0.4
                tmux send-keys -t "$PANE" c
                sleep 0.4
                _clip=$(tmux show-buffer 2>/dev/null)
                if [[ -n "$_clip" && "$_clip" != "$BTW_SENTINEL" ]]; then
                    CLIP="$_clip"
                fi
            fi

            if [[ -n "$CLIP" ]]; then
                if [[ "$CLIP" == *"<<<END>>>"* ]]; then
                    # Clipboard still carries the echoed prompt — keep only what
                    # follows the last <<<END>>> delimiter.
                    CLEANED=$(printf '%s' "$CLIP" | python3 -c '
import sys

data = sys.stdin.read()
idx = data.rfind("<<<END>>>")
if idx != -1:
    data = data[idx + len("<<<END>>>"):]
print(data.strip())
' 2>/dev/null)
                else
                    CLEANED="$CLIP"
                fi
                if [[ -n "$CLEANED" ]]; then
                    log "c-to-copy captured ${#CLEANED} chars (via tmux buffer)"
                fi
            fi

            # --- Step 5b: Fallback extraction — pane scrape ---
            if [[ -z "$CLEANED" ]]; then
                log "c-to-copy yielded nothing — falling back to pane scrape"
                CONTENT=$(tmux capture-pane -p -t "$PANE" -S -200 2>/dev/null)
                CLEANED=$(echo "$CONTENT" | python3 -c '
import re
import sys

lines = sys.stdin.read().split("\n")

# Find the panel footer line (scan from bottom). v2.1.176+ footer reads
# "... Esc to close"; use it as the response end boundary.
end = None
for i in range(len(lines) - 1, -1, -1):
    if "esc to close" in lines[i].lower():
        end = i
        break
if end is None:
    sys.exit(1)

# Find where the btw response starts.
# Scan bottom-up from the footer for the <<<END>>> delimiter. The btw panel
# echoes the submitted command (indented), so <<<END>>> appears twice:
#   ❯ /btw ... <<<END>>>        ← original prompt (starts with ❯)
#     /btw... <<<END>>>          ← indented echo
#     response text...
#   footer line
# The response follows the LAST <<<END>>> in the captured region, so scan
# bottom-up and start right after the first delimiter we hit (the echo
# closest to the response).
btw_echo_end = None
for i in range(end - 1, -1, -1):
    if "<<<END>>>" in lines[i]:
        btw_echo_end = i
        break

if btw_echo_end is None:
    btw_echo_end = max(0, end - 20)

start = btw_echo_end + 1
while start < end and not lines[start].strip():
    start += 1

if start >= end:
    sys.exit(1)

box_strip = "│┌┐└┘├┤┬┴┼─╭╮╯╰"
# Strip panel chrome at the LEFT/RIGHT margins only. Do NOT global-replace box
# glyphs or .strip() every line — that flattens tables, nested bullets, and code
# blocks and drops the blank lines between paragraphs. Trim a leading
# "<spaces><border glyphs><one pad space>" and the symmetric trailing run, keep
# interior indentation and blank lines, then drop only the outermost
# blank/border lines of the block.
box_class = "[" + re.escape(box_strip) + "]"
lead = re.compile(r"^\s*" + box_class + r"+\s?")
trail = re.compile(r"\s?" + box_class + r"+\s*$")

result = [trail.sub("", lead.sub("", line)).rstrip() for line in lines[start:end]]

while result and not result[0].strip():
    result.pop(0)
while result and not result[-1].strip():
    result.pop()

if not result:
    sys.exit(1)
print("\n".join(result))
' 2>/dev/null)
            fi

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
