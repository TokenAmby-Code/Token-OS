#!/bin/bash
# system-dashboard.sh - Unified system dashboard
# Combines: daily-status, network-test, cron-dashboard, vault-progress
# Run: ./system-dashboard.sh

echo "╔══════════════════════════════════════════════════════════╗"
echo "║           SYSTEM DASHBOARD - $(date '+%Y-%m-%d %H:%M')            ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# =====================
# TOKEN API
# =====================
echo "━━━ TOKEN API ━━━"
TOKEN_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:7777/health 2>/dev/null)
if [ "$TOKEN_HEALTH" = "200" ]; then
    echo "✅ Running (HTTP $TOKEN_HEALTH)"
else
    echo "❌ Down (HTTP $TOKEN_HEALTH)"
fi
echo ""

# =====================
# TAILSCALE
# =====================
echo "━━━ TAILSCALE ━━━"
TAILSCALE_STATE=$(tailscale status --json 2>/dev/null | jq -r '.BackendState // "unknown"')
if [ "$TAILSCALE_STATE" = "Running" ]; then
    echo "✅ Status: Running"
    TAILSCALE_IPV4=$(tailscale ip -4 2>/dev/null | head -1)
    TAILSCALE_IPV6=$(tailscale ip -6 2>/dev/null | head -1)
    [ -n "$TAILSCALE_IPV4" ] && echo "📍 IPv4: $TAILSCALE_IPV4"
    [ -n "$TAILSCALE_IPV6" ] && echo "📍 IPv6: $TAILSCALE_IPV6"
    
    PEER_COUNT=$(tailscale status --json 2>/dev/null | jq '.Peer | length // 0')
    echo "👥 $PEER_COUNT peer(s)"
else
    echo "❌ Status: $TAILSCALE_STATE"
fi
echo ""

# =====================
# OPENCLAW GATEWAY
# =====================
echo "━━━ OPENCLAW GATEWAY ━━━"
GATEWAY_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18789/ --connect-timeout 2 2>/dev/null)
if echo "$GATEWAY_STATUS" | grep -q "200\|401\|403"; then
    echo "✅ Running (HTTP $GATEWAY_STATUS)"
else
    echo "❌ Not running (HTTP $GATEWAY_STATUS)"
fi

# Cron jobs
JOBS_FILE="/Users/tokenclaw/.openclaw/cron/jobs.json"
if [ -f "$JOBS_FILE" ]; then
    TOTAL_JOBS=$(jq '.jobs | length' "$JOBS_FILE" 2>/dev/null || echo "0")
    ENABLED_JOBS=$(jq '[.jobs[] | select(.enabled == true)] | length' "$JOBS_FILE" 2>/dev/null || echo "0")
    ERROR_JOBS=$(jq '[.jobs[] | select(.state.lastStatus == "error")] | length' "$JOBS_FILE" 2>/dev/null || echo "0")
    echo "📋 Cron: $ENABLED_JOBS/$TOTAL_JOBS enabled, $ERROR_JOBS error(s)"
fi
echo ""

# =====================
# SYSTEM RESOURCES
# =====================
echo "━━━ SYSTEM RESOURCES ━━━"
# Memory
MEM=$(vm_stat 2>/dev/null | grep "Pages free" | awk '{print $3}' | tr -d '.')
MEM_MB=$((MEM / 256))
echo "💾 Memory: ~${MEM_MB}MB free"

# Load
LOAD=$(sysctl -n vm.loadavg 2>/dev/null | tr -d '{}')
echo "📊 Load: $LOAD"

# Disk
DISK_PCT=$(df -h . | tail -1 | awk '{print $5}')
DISK_FREE=$(df -h . | tail -1 | awk '{print $4}')
echo "💿 Disk: $DISK_PCT used ($DISK_FREE free)"
echo ""

# =====================
# PULPER PROGRESS
# =====================
echo "━━━ PULPER PROGRESS ━━━"
CLAW_ENV="/Users/tokenclaw/Claw-ENV"
PROCESSED_FILE="/Users/tokenclaw/.openclaw/workspace/memory/processed_files.md"

TOTAL=$(find "$CLAW_ENV" -name "*.md" -type f 2>/dev/null | wc -l | tr -d ' ')
PROCESSED=$(grep -c "^\- \[20" "$PROCESSED_FILE" 2>/dev/null || echo "0")
REMAINING=$((TOTAL - PROCESSED))
if [ "$TOTAL" -gt 0 ]; then
    PERCENT=$(awk "BEGIN {printf \"%.1f\", ($PROCESSED/$TOTAL)*100}")
else
    PERCENT="0.0"
fi

echo "📚 Files: $PROCESSED/$TOTAL processed ($PERCENT% complete)"
echo "⏳ Remaining: $REMAINING files"
echo ""

# =====================
# SSH HOSTS
# =====================
echo "━━━ SSH HOSTS ━━━"
SSH_HOSTS=("desktop" "wsl" "phone")
for host in "${SSH_HOSTS[@]}"; do
    HOST_INFO=$(grep -A2 "^Host $host$" ~/.ssh/config 2>/dev/null | grep "HostName" | awk '{print $2}')
    if [ -z "$HOST_INFO" ]; then
        continue
    fi
    
    if timeout 2 ssh -o ConnectTimeout=1 -o BatchMode=yes "$host" "echo ok" 2>/dev/null > /dev/null; then
        echo "✅ $host ($HOST_INFO)"
    else
        echo "❌ $host ($HOST_INFO) - unreachable"
    fi
done
echo ""

echo "══════════════════════════════════════════════════════════"
echo "💡 Run individual scripts for details:"
echo "   Scripts/Shell/daily-status.sh    (Token, Tailscale, Gateway)"
echo "   Scripts/Shell/network-test.sh    (Peers, SSH)"  
echo "   Scripts/Shell/cron-dashboard.sh  (Cron jobs)"
echo "   Scripts/Shell/vault-progress.sh  (Pulper)"
echo "══════════════════════════════════════════════════════════"
