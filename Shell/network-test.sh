#!/bin/bash
# network-test.sh - Network connectivity test script
# Tests Tailscale peers, SSH connectivity, and Token API
# Run: ./network-test.sh

echo "=== Network Connectivity Test ==="
echo ""

# --- Tailscale Status ---
echo "🔸 Tailscale:"
TAILSCALE_STATE=$(tailscale status --json 2>/dev/null | jq -r '.BackendState // "unknown"')
if [ "$TAILSCALE_STATE" = "Running" ]; then
    echo "  ✅ Status: Running"
else
    echo "  ❌ Status: $TAILSCALE_STATE"
    echo "  ⚠️  Skipping peer/SSH checks - Tailscale not running"
    exit 1
fi

# Get Tailscale IPs
TAILSCALE_IPV4=$(tailscale ip -4 2>/dev/null | head -1)
TAILSCALE_IPV6=$(tailscale ip -6 2>/dev/null | head -1)
[ -n "$TAILSCALE_IPV4" ] && echo "  📍 IPv4: $TAILSCALE_IPV4" || echo "  📍 IPv4: (none)"
[ -n "$TAILSCALE_IPV6" ] && echo "  📍 IPv6: $TAILSCALE_IPV6" || echo "  📍 IPv6: (none)"

# Tailscale peers
PEER_COUNT=$(tailscale status --json 2>/dev/null | jq '.Peer | length // 0')
echo "  $PEER_COUNT 👥 Peers:"

# Show peer details (hostname and IP)
if [ "$PEER_COUNT" -gt 0 ]; then
    tailscale status --json 2>/dev/null | jq -r '.Peer | to_entries[] | "    - \(.value.HostName): \(.value.TailscaleIPs[0] // "no IP")"' 2>/dev/null | while read line; do
        echo "  $line"
    done
fi

echo ""

# --- Token API ---
echo "🔸 Token API:"
TOKEN_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:7777/health 2>/dev/null)
if [ "$TOKEN_HEALTH" = "200" ]; then
    echo "  ✅ Running (HTTP $TOKEN_HEALTH)"
else
    echo "  ❌ Down (HTTP $TOKEN_HEALTH)"
fi

echo ""

# --- SSH Connectivity Test ---
echo "🔸 SSH Hosts:"

# Define SSH hosts to test (from ~/.ssh/config)
SSH_HOSTS=("mini" "desktop" "wsl" "phone")

for host in "${SSH_HOSTS[@]}"; do
    # Get host details from ssh config
    HOST_INFO=$(grep -A2 "^Host $host$" ~/.ssh/config 2>/dev/null | grep "HostName" | awk '{print $2}')
    if [ -z "$HOST_INFO" ]; then
        continue
    fi
    
    # Quick connection test (timeout 3s)
    if timeout 3 ssh -o ConnectTimeout=2 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$host" "echo ok" 2>/dev/null > /dev/null; then
        echo "  ✅ $host ($HOST_INFO) - reachable"
    else
        # Check if it's the current host (mini is this machine)
        if [ "$host" = "mini" ]; then
            echo "  ⚠️  $host ($HOST_INFO) - localhost (skip)"
        else
            echo "  ❌ $host ($HOST_INFO) - unreachable"
        fi
    fi
done

echo ""

# --- Summary ---
echo "=== Done ==="
