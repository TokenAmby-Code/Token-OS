#!/bin/bash
# heartbeat-watchdog.sh — External watchdog for OpenClaw cron worker system
# Runs every 30 minutes via launchd. Detects stalling and intervenes.
# Monitors cron_worker_log.md for task-worker activity.

set -euo pipefail

WORKSPACE="/Users/tokenclaw/.openclaw/workspace"
WORKER_LOG="$WORKSPACE/memory/cron_worker_log.md"
WATCHDOG_LOG="$WORKSPACE/memory/watchdog_log.md"
TIMESTAMP=$(date "+%Y-%m-%d %H:%M:%S")

log() {
    echo "- [$TIMESTAMP] $1" >> "$WATCHDOG_LOG"
}

# Ensure log files exist
touch "$WORKER_LOG" "$WATCHDOG_LOG"

# Find the last worker log entry timestamp and compute minutes since
LAST_ENTRY_LINE=$(grep '^- \[' "$WORKER_LOG" | tail -1 || true)
MINUTES_SINCE_LAST=999  # default: treat as very stale

if [ -n "$LAST_ENTRY_LINE" ]; then
    # Extract timestamp from "- [YYYY-MM-DD HH:MM:SS] ..."
    LAST_TS=$(echo "$LAST_ENTRY_LINE" | sed 's/^- \[\(.*\)\].*/\1/')
    if [ -n "$LAST_TS" ]; then
        LAST_EPOCH=$(date -j -f "%Y-%m-%d %H:%M:%S" "$LAST_TS" "+%s" 2>/dev/null || echo 0)
        NOW_EPOCH=$(date "+%s")
        if [ "$LAST_EPOCH" -gt 0 ]; then
            MINUTES_SINCE_LAST=$(( (NOW_EPOCH - LAST_EPOCH) / 60 ))
        fi
    fi
fi

# Count consecutive IDLE entries from end
CONSECUTIVE_IDLE=0
while IFS= read -r line; do
    [[ -z "$line" || "$line" =~ ^#  || "$line" =~ ^\<\!-- ]] && continue
    [[ ! "$line" =~ ^-\ \[ ]] && continue

    if echo "$line" | grep -qiE '(ACTION:|TASK:|COMPLETED:|PROGRESS:)'; then
        break
    fi

    if echo "$line" | grep -qiE 'IDLE:'; then
        CONSECUTIVE_IDLE=$((CONSECUTIVE_IDLE + 1))
    else
        break
    fi
done < <(grep '^- \[' "$WORKER_LOG" | tail -r)

log "WATCHDOG CHECK: ${MINUTES_SINCE_LAST}m since last worker entry, $CONSECUTIVE_IDLE consecutive idle"

# Also check if the task-worker cron job still exists
WORKER_EXISTS=$(openclaw cron list --json 2>/dev/null | python3 -c "
import json, sys
jobs = json.load(sys.stdin)
print('yes' if any(j.get('name') == 'task-worker' for j in jobs) else 'no')
" 2>/dev/null || echo "unknown")

if [ "$WORKER_EXISTS" = "no" ]; then
    log "ALERT: task-worker cron job is MISSING! It may have been deleted."
    # Send Discord alert
    openclaw message send --channel discord --target 1472043387535495323 \
        --message "WATCHDOG ALERT: task-worker cron job is missing! Check cron jobs." 2>/dev/null || true
fi

if [ "$MINUTES_SINCE_LAST" -ge 60 ]; then
    # TIER 2: Heavy escalation — no worker output for 60+ minutes
    log "TIER 2 ESCALATION: No worker output for ${MINUTES_SINCE_LAST}m. Invoking Claude for assessment."

    CLAUDE_RESPONSE=$(claude -p "You are the OpenClaw watchdog. The task-worker cron agent has not produced any output for $MINUTES_SINCE_LAST minutes.

Check:
1. Are cron jobs still running? Run: openclaw cron list
2. Is the gateway healthy? Run: openclaw health
3. Read the task list at $WORKSPACE/Claw-ENV/0-Admin/TASK_LIST.md
4. Read the worker log at $WORKER_LOG

If cron jobs are missing, recreate them. If the gateway is down, report it.
If everything looks fine, manually trigger a worker run:
openclaw cron run --name task-worker

Report your findings." 2>&1 || true)

    log "TIER 2 RESULT: Claude response received ($(echo "$CLAUDE_RESPONSE" | wc -c | tr -d ' ') bytes)"

elif [ "$MINUTES_SINCE_LAST" -ge 30 ]; then
    # TIER 1: Nudge — manually trigger the worker
    log "TIER 1 NUDGE: No worker output for ${MINUTES_SINCE_LAST}m. Manually triggering task-worker."

    TRIGGER_RESULT=$(openclaw cron run --name task-worker 2>&1 || true)
    log "TIER 1 RESULT: Manual trigger sent ($(echo "$TRIGGER_RESULT" | wc -c | tr -d ' ') bytes)"

else
    log "STATUS OK: Last worker entry ${MINUTES_SINCE_LAST}m ago (threshold: 30m)"
fi
