#!/bin/bash
# vault-progress.sh - Shows pulper progress (files remaining, % complete)
# Tracks extraction progress from Claw-ENV vault to memory

CLAW_ENV="/Users/tokenclaw/Claw-ENV"
PROCESSED_FILE="/Users/tokenclaw/.openclaw/workspace/memory/processed_files.md"

# Count total markdown files in Claw-ENV
TOTAL=$(find "$CLAW_ENV" -name "*.md" -type f 2>/dev/null | wc -l | tr -d ' ')

# Count processed files (entries in processed_files.md)
PROCESSED=$(grep -c "^\- \[20" "$PROCESSED_FILE" 2>/dev/null || echo "0")

# Calculate remaining and percentage
REMAINING=$((TOTAL - PROCESSED))
if [ "$TOTAL" -gt 0 ]; then
    PERCENT=$(awk "BEGIN {printf \"%.1f\", ($PROCESSED/$TOTAL)*100}")
else
    PERCENT="0.0"
fi

# Output
echo "📚 Pulper Progress"
echo "=================="
echo "Total files:     $TOTAL"
echo "Processed:       $PROCESSED"
echo "Remaining:       $REMAINING"
echo "Complete:        ${PERCENT}%"
