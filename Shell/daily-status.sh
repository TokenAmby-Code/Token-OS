#!/bin/bash
# daily-status.sh - Quick system health summary
# Run: ./daily-status.sh

echo "=== Daily Status Check ==="
echo ""

# Token API health
echo "🔸 Token API:"
TOKEN_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:7777/health 2>/dev/null)
if [ "$TOKEN_HEALTH" = "200" ]; then
    echo "  ✅ Running (HTTP $TOKEN_HEALTH)"
else
    echo "  ❌ Down (HTTP $TOKEN_HEALTH)"
fi

# Tailscale status
echo ""
echo "🔸 Tailscale:"
TAILSCALE_OUT=$(tailscale status 2>/dev/null | head -1)
if echo "$TAILSCALE_OUT" | grep -q "100\."; then
    echo "  ✅ Connected"
    tailscale ip -4 2>/dev/null | while read ip; do
        echo "  📍 IPv4: $ip"
    done
    tailscale ip -6 2>/dev/null | while read ip; do
        echo "  📍 IPv6: $ip"
    done
else
    echo "  ❌ Not connected"
fi

# OpenClaw gateway - check if dashboard responds
echo ""
echo "🔸 OpenClaw Gateway:"
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:18789/ --connect-timeout 2 2>/dev/null | grep -q "200\|401\|403"; then
    echo "  ✅ Running"
    echo "  🌐 Dashboard: http://127.0.0.1:18789/"
else
    echo "  ❌ Not running or unreachable"
fi

# Basic system stats
echo ""
echo "🔸 System:"
MEM=$(vm_stat 2>/dev/null | grep "Pages free" | awk '{print $3}' | tr -d '.')
MEM_MB=$((MEM / 256))
echo "  💾 Free memory: ~${MEM_MB}MB"

# Load average
LOAD=$(sysctl -n vm.loadavg 2>/dev/null)
if [ -n "$LOAD" ]; then
    echo "  📊 Load: $LOAD"
fi

# Disk space
DISK_USAGE=$(df -h . | tail -1 | awk '{print $5 " used (" $3 " free)"}')
echo "  💿 Disk: $DISK_USAGE"

echo ""
echo "=== Done ==="
