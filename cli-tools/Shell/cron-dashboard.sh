#!/bin/bash
# Cron Job Health Dashboard
# Shows status of all cron jobs with detailed metrics

CRON_STATUS=$(openclaw cron status 2>/dev/null)
JOBS_FILE="/Users/tokenclaw/.openclaw/cron/jobs.json"

echo "=========================================="
echo "     CRON JOB HEALTH DASHBOARD"
echo "     $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
echo ""

# Overall status
echo "📊 OVERALL STATUS"
echo "-----------------"
ENABLED=$(echo "$CRON_STATUS" | grep -o '"enabled":[^,]*' | cut -d':' -f2 | tr -d ' ')
JOBS_COUNT=$(echo "$CRON_STATUS" | grep -o '"jobs":[^,]*' | cut -d':' -f2)
NEXT_WAKE=$(echo "$CRON_STATUS" | grep -o '"nextWakeAtMs":[^,]*' | cut -d':' -f2)

if [ "$ENABLED" = "true" ]; then
    echo "✅ Cron Scheduler: ENABLED"
else
    echo "❌ Cron Scheduler: DISABLED"
fi
echo "📋 Total Jobs: $JOBS_COUNT"

# Convert next wake to human time
if [ -n "$NEXT_WAKE" ]; then
    NEXT_DATE=$(date -r $((NEXT_WAKE/1000)) 2>/dev/null || echo "unknown")
    echo "⏰ Next Wake: $NEXT_DATE"
fi

echo ""
echo "📋 JOB DETAILS"
echo "-------------"

# Parse jobs and display details
if [ -f "$JOBS_FILE" ]; then
    # Count totals
    TOTAL_JOBS=$(jq '.jobs | length' "$JOBS_FILE" 2>/dev/null || echo "0")
    ENABLED_JOBS=$(jq '[.jobs[] | select(.enabled == true)] | length' "$JOBS_FILE" 2>/dev/null || echo "0")
    DISABLED_JOBS=$(jq '[.jobs[] | select(.enabled == false)] | length' "$JOBS_FILE" 2>/dev/null || echo "0")
    ERROR_JOBS=$(jq '[.jobs[] | select(.state.lastStatus == "error")] | length' "$JOBS_FILE" 2>/dev/null || echo "0")
    RUNNING_JOBS=$(jq '[.jobs[] | select(.state.runningAtMs != null)] | length' "$JOBS_FILE" 2>/dev/null || echo "0")
    
    echo "   Enabled: $ENABLED_JOBS | Disabled: $DISABLED_JOBS | Errors: $ERROR_JOBS | Running: $RUNNING_JOBS"
    echo ""
    
    # Header
    printf "   %-22s %-12s %-12s %-12s %s\n" "JOB" "STATUS" "LAST RUN" "NEXT RUN" "SCHEDULE"
    printf "   %-22s %-12s %-12s %-12s %s\n" "---" "------" "--------" "--------" "--------"
    
    # Process each job
    for job in $(jq -r '.jobs[] | @base64' "$JOBS_FILE" 2>/dev/null); do
        # Decode base64 JSON
        job_json=$(echo "$job" | base64 -d)
        
        name=$(echo "$job_json" | jq -r '.name')
        enabled=$(echo "$job_json" | jq -r '.enabled')
        scheduleKind=$(echo "$job_json" | jq -r '.schedule.kind')
        everyMs=$(echo "$job_json" | jq -r '.schedule.everyMs')
        nextRun=$(echo "$job_json" | jq -r '.state.nextRunAtMs')
        lastRun=$(echo "$job_json" | jq -r '.state.lastRunAtMs')
        lastStatus=$(echo "$job_json" | jq -r '.state.lastStatus')
        errors=$(echo "$job_json" | jq -r '.state.consecutiveErrors')
        
        # Format status
        if [ "$enabled" = "false" ]; then
            STATUS="[DISABLED]"
        elif [ "$lastStatus" = "error" ]; then
            STATUS="❌ ERROR"
        elif [ "$lastStatus" = "running" ]; then
            STATUS="🔄 RUNNING"
        elif [ "$lastStatus" = "idle" ]; then
            STATUS="💤 IDLE"
        else
            STATUS="✅ OK"
        fi
        
        # Format last run
        if [ "$lastRun" = "null" ] || [ -z "$lastRun" ]; then
            LAST_STR="Never"
        else
            LAST_SEC=$((lastRun/1000))
            SECONDS_AGO=$(( $(date +%s) - LAST_SEC ))
            if [ $SECONDS_AGO -lt 60 ]; then
                LAST_STR="${SECONDS_AGO}s ago"
            elif [ $SECONDS_AGO -lt 3600 ]; then
                LAST_STR="$((SECONDS_AGO/60))m ago"
            else
                LAST_STR="$((SECONDS_AGO/3600))h ago"
            fi
        fi
        
        # Format next run
        if [ "$nextRun" = "null" ] || [ -z "$nextRun" ] || [ "$nextRun" = "0" ]; then
            NEXT_STR="—"
        else
            NEXT_SEC=$((nextRun/1000))
            SECONDS_UNTIL=$((NEXT_SEC - $(date +%s)))
            if [ $SECONDS_UNTIL -lt 0 ]; then
                NEXT_STR="Overdue"
            elif [ $SECONDS_UNTIL -lt 60 ]; then
                NEXT_STR="in ${SECONDS_UNTIL}s"
            elif [ $SECONDS_UNTIL -lt 3600 ]; then
                NEXT_STR="in $((SECONDS_UNTIL/60))m"
            else
                NEXT_STR="in $((SECONDS_UNTIL/3600))h"
            fi
        fi
        
        # Format schedule - convert ms to minutes
        if [ "$scheduleKind" = "every" ] && [ -n "$everyMs" ] && [ "$everyMs" != "null" ]; then
            MINUTES=$((everyMs/60000))
            SCHEDULE="${MINUTES}m"
        else
            SCHEDULE="$scheduleKind"
        fi
        
        # Show errors if any
        ERR_STR=""
        if [ "$errors" != "0" ] && [ "$errors" != "null" ] && [ -n "$errors" ]; then
            ERR_STR=" ($errors err)"
        fi
        
        printf "   %-22s %-12s %-12s %-12s %s\n" "$name" "$STATUS$ERR_STR" "$LAST_STR" "$NEXT_STR" "$SCHEDULE"
    done
fi

echo ""
echo "=========================================="
echo "💡 'openclaw cron list' for full details"
echo "=========================================="
