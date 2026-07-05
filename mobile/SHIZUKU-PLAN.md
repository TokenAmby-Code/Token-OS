# Shizuku Reliability Plan

**Status**: REOPENED / SCOPED PHONE BOOTSTRAP (2026-07-04)
**Last updated**: 2026-07-04

---

## Current Decision

Shizuku/ADB remains forbidden for MacroDroid macro delivery. The only supported delivery path is official `.macro` JSON validated by `macrodroid-validate` and imported through `MACRODROID_AUTO_IMPORT=1 macrodroid-import`, followed by a fresh phone pull/export. The live phone export is canonical because the import harness can currently false-success.

A narrow phone-side exception now exists: MacroDroid may use its own `SystemSettingAction` and `UIInteractionAction` inside `Shizuku Bootstrap` to start Shizuku. This exception does not revive desktop ADB keepalives or direct phone mutation flows.

## Current Macro

`Shizuku Bootstrap` is the only active Shizuku/ADB-lane macro after 2026-07-04 cleanup. The legacy macros `Shizuku Auto Start` and `Automatically activates wireless ADB` were removed from the active phone inventory.

Snapshot file: `macros/shizuku-bootstrap.macro`

Behavior:

1. Trigger via HTTP `/shizuku-bootstrap` or floating button `shizuku-bootstrap` (`SZK`).
2. Disable legacy macros by name as a safety first step.
3. Enable wireless debugging using MacroDroid Helper:
   - `SystemSettingAction`
   - `tableOption=2` / Global
   - `settingString=adb_wifi_enabled`
   - `valueString=1`
   - `useHelper=true`
4. Launch package `moe.shizuku.privileged.api`.
5. Pause, then click the Shizuku UI start button.
6. Check `ShizukuStateConstraint option=3` and log result to `/storage/emulated/0/MacroDroid/logs/debug.log`.

The initial hand-authored UI target `id:android:id/button1` did not work. The operator used MacroDroid Identify-in-app to sniff the real target. Current deployed click config is:

```json
{
  "clickOption": 3,
  "textContent": "Start",
  "textMatchOption": 1,
  "viewId": "android:id/button1",
  "xyPoint": {"x": 243, "y": 1394}
}
```

Checkpoint: the macro opens Shizuku and the sniffed UI interaction appears to click the `Start` button. Full Shizuku-running validation is still the next live gate.

## Harness Caveat

Do not rely on import CLI success alone. During phone TTS MacroDroid work, `macrodroid-import --replace` was observed to false-success even when the live MacroDroid app rejected or duplicated an import. `macrodroid-validate` also misses some MacroDroid UI-level rejections. Rejected shapes include `SpeakTextAction` values using dictionary/global magic text directly instead of scalar locals and HTTP/dictionary variable forms MacroDroid accepts in text fields but rejects in TTS speak fields.

Required verification remains: pull/export after import, inspect the deployed macro, and treat the live phone export as truth.

## Historical ADB-over-Tailscale Plan

The previous 2026-03 plan used persistent ADB over Tailscale and `shizuku-connect`. That design is historical only for this lane. Do not revive it without an explicit new decision.

---

## Archived 2026-03 Notes

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
