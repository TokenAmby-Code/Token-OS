# Shizuku Reliability Plan

**Status**: RESOLVED (2026-03-04) — ADB over Tailscale
**Last updated**: 2026-03-05

---

## Resolution

Shizuku now runs in "Connected to a computer" mode via persistent ADB over Tailscale (port 5555), bypassing wireless debugging entirely. This solved the root cause: wireless debugging was unstable (Android auto-disables it on network/SSID changes, killing the ADB daemon and Shizuku with it).

### How It Works

| Component | Detail |
|-----------|--------|
| ADB target | `100.102.92.24:5555` (phone Tailscale IP, stable across networks) |
| CLI | `shizuku-connect [status|connect|start|bootstrap|keepalive|disconnect]` |
| LaunchAgent | `ai.tokenclaw.shizuku-keepalive` (every 5 min, reconnects + restarts if needed) |
| Recovery | MacroDroid "Shizuku Died" macro POSTs to Mac token-api, which calls `shizuku-connect start` |
| Bootstrap | `shizuku-connect bootstrap` (one-time per phone reboot, needs brief wireless debugging to set `adb tcpip 5555`) |

### What Changed

- `main.py`: `attempt_shizuku_restart()` now calls `shizuku-connect start` instead of the old 4-step SSH+ADB flow
- Removed `/phone/shizuku/config` endpoint (no wireless debug port to configure)
- MacroDroid macros simplified: no app launch, just POST to Mac for ADB restart
- Deleted `shizuku-death-logger.yaml` (superseded by Died/Restored macros)

### Remaining Limitation

Phone reboot kills the TCP ADB listener, requiring `shizuku-connect bootstrap` (brief wireless debugging + pairing). This is rare enough to be acceptable.

---

## Original Problem (Historical)

Shizuku died hours after being started. Root cause: wireless debugging dependency. Android auto-disabled wireless debugging on network changes, killing the ADB daemon and Shizuku. The ADB-over-Tailscale approach eliminates this dependency since the Tailscale IP is stable across all network transitions.
