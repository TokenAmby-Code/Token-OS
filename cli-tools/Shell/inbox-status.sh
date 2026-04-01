#!/bin/bash
# inbox-status.sh - Show what's waiting in Claw-ENV/Inbox/Staging
# Run: ./inbox-status.sh

INBOX_DIR="/Users/tokenclaw/Claw-ENV/Inbox/Staging"
PROCESSED_FILE="/Users/tokenclaw/.openclaw/workspace/memory/processed_files.md"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║              INBOX STATUS - $(date '+%Y-%m-%d %H:%M')              ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# Check if inbox exists
if [ ! -d "$INBOX_DIR" ]; then
    echo "❌ Inbox directory not found: $INBOX_DIR"
    exit 1
fi

# Count total files
TOTAL=$(ls "$INBOX_DIR" | wc -l | tr -d ' ')
echo "📥 Total files waiting: $TOTAL"
echo ""

# Files by category (based on filename prefixes)
echo "━━━ BY CATEGORY ━━━"
CATEGORIES=("AI-" "API-" "Advisory-" "Agent-" "Claude-" "Code-" "Cursor-" "Civic-" "CLI-" "Development-" "Documentation-" "Obsidian-" "Project-" "Work-" "Personal-" "Maintenance-" "Shopping-" "Health-" "Entertainment-" "Staging-")

for cat in "${CATEGORIES[@]}"; do
    count=$(ls "$INBOX_DIR"/"$cat"* 2>/dev/null | wc -l | tr -d ' ')
    if [ "$count" -gt 0 ]; then
        echo "  $cat*: $count file(s)"
    fi
done

# Show uncategorized (no prefix match)
UNCATEGORIZED=$(for f in "$INBOX_DIR"/*; do
    basename "$f" | grep -v "^-" | head -1
done | grep -v "AI-\|API-\|Advisory-\|Agent-\|Claude-\|Code-\|Cursor-\|Civic-\|CLI-\|Development-\|Documentation-\|Obsidian-\|Project-\|Work-\|Personal-\|Maintenance-\|Shopping-\|Health-\|Entertainment-\|Staging-" | wc -l | tr -d ' ')

if [ "$UNCATEGORIZED" -gt 0 ]; then
    echo "  Other: $UNCATEGORIZED file(s)"
fi
echo ""

# Show oldest files (first in queue)
echo "━━━ OLDEST FILES (first to process) ━━━"
ls -t "$INBOX_DIR" | tail -5 | while read f; do
    # Get file age
    FILE_PATH="$INBOX_DIR/$f"
    AGE_DAYS=$(( $(date +%s) - $(stat -f %m "$FILE_PATH") ))
    AGE_DAYS=$((AGE_DAYS / 86400))
    echo "  📄 $f ($AGE_DAYS days old)"
done
echo ""

# Show newest files (just added)
echo "━━━ NEWEST FILES (recently added) ━━━"
ls -t "$INBOX_DIR" | head -5 | while read f; do
    FILE_PATH="$INBOX_DIR/$f"
    AGE_HOURS=$(( $(date +%s) - $(stat -f %m "$FILE_PATH") ))
    AGE_HOURS=$((AGE_HOURS / 3600))
    if [ "$AGE_HOURS" -lt 1 ]; then
        AGE="<1hr"
    else
        AGE="${AGE_HOURS}hrs"
    fi
    echo "  📄 $f ($AGE)"
done
echo ""

# Estimate time to process (based on pulper rate)
echo "━━━ PROCESSING ESTIMATE ━━━"
# Pulper typically processes ~5-10 files per run
RATE_PER_RUN=7
echo "  At ~$RATE_PER_RUN files/run, ~$(( (TOTAL + RATE_PER_RUN - 1) / RATE_PER_RUN )) runs remaining"
echo ""

# Show sample of file sizes
echo "━━━ SIZE DISTRIBUTION ━━━"
SMALL=$(find "$INBOX_DIR" -type f -size -2k | wc -l | tr -d ' ')
MEDIUM=$(find "$INBOX_DIR" -type f -size +2k -size -5k | wc -l | tr -d ' ')
LARGE=$(find "$INBOX_DIR" -type f -size +5k | wc -l | tr -d ' ')
echo "  Small (<2KB): $SMALL"
echo "  Medium (2-5KB): $MEDIUM"
echo "  Large (>5KB): $LARGE"
echo ""

echo "══════════════════════════════════════════════════════════"
echo "💡 Run pulper to process: openclaw cron run <pulser-job-id>"
echo "   Or wait for scheduled run"
echo "══════════════════════════════════════════════════════════"
