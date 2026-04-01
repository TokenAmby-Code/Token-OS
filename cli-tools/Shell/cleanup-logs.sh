#!/bin/bash
# Log Cleanup Script - Trims log files to last N entries
# Usage: ./cleanup-logs.sh [max_lines] [log_dir]
# Defaults: max_lines=50, log_dir=~/.openclaw/logs

MAX_LINES="${1:-50}"
LOG_DIR="${2:-$HOME/.openclaw/logs}"

echo "🧹 Log Cleanup Script"
echo "====================="
echo "Max lines per file: $MAX_LINES"
echo "Log directory: $LOG_DIR"
echo ""

total_saved=0
files_processed=0

# Find all .log files and .jsonl files in the log directory
for logfile in "$LOG_DIR"/*.log "$LOG_DIR"/*.jsonl; do
    # Skip if no matches
    [ -f "$logfile" ] || continue
    
    original_size=$(wc -c < "$logfile")
    line_count=$(wc -l < "$logfile")
    
    # Skip if under threshold
    if [ "$line_count" -le "$MAX_LINES" ]; then
        echo "✓ $(basename "$logfile"): $line_count lines (under threshold, skipping)"
        continue
    fi
    
    # Create temp file with only last N lines
    temp_file=$(mktemp)
    tail -n "$MAX_LINES" "$logfile" > "$temp_file"
    
    # Replace original
    mv "$temp_file" "$logfile"
    
    new_size=$(wc -c < "$logfile")
    saved=$((original_size - new_size))
    total_saved=$((total_saved + saved))
    files_processed=$((files_processed + 1))
    
    echo "✂  $(basename "$logfile"): $line_count → $MAX_LINES lines (saved $((saved/1024))KB)"
done

echo ""
if [ "$files_processed" -eq 0 ]; then
    echo "✅ No logs needed trimming"
else
    echo "✅ Trimmed $files_processed file(s), saved $((total_saved/1024))KB total"
fi

# Also check tmp logs if they exist and are large
TMP_LOG_DIR="/tmp/openclaw"
if [ -d "$TMP_LOG_DIR" ]; then
    echo ""
    echo "📁 Checking tmp logs: $TMP_LOG_DIR"
    for logfile in "$TMP_LOG_DIR"/openclaw-*.log; do
        [ -f "$logfile" ] || continue
        
        original_size=$(wc -c < "$logfile")
        line_count=$(wc -l < "$logfile")
        
        # Use higher threshold for tmp logs (they rotate daily)
        TMP_MAX=1000
        if [ "$line_count" -le "$TMP_MAX" ]; then
            echo "  ✓ $(basename "$logfile"): $line_count lines (under threshold)"
            continue
        fi
        
        temp_file=$(mktemp)
        tail -n "$TMP_MAX" "$logfile" > "$temp_file"
        mv "$temp_file" "$logfile"
        
        new_size=$(wc -c < "$logfile")
        saved=$((original_size - new_size))
        total_saved=$((total_saved + saved))
        
        echo "  ✂ $(basename "$logfile"): $line_count → $TMP_MAX lines (saved $((saved/1024))KB)"
    done
fi

echo ""
echo "🧹 Cleanup complete!"
