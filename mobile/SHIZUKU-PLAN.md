# Shizuku Reliability Plan

**Status**: ARCHIVED / UNVIABLE (reconfirmed 2026-07-05) — do not use in the active phone enforcement stack.
**Last updated**: 2026-07-05

---

## Current Decision

The Shizuku path remains parked. MacroDroid can detect Shizuku loss and attempt a restart, but that only stress-tests the failure mode: when Android turns Wireless debugging off, Shizuku loses the ADB-backed runtime it depends on and MacroDroid re-enters an indefinite restart/relogin loop.

Active mobile automation must stay on the stock Android + MacroDroid path with no Shizuku, root, or ADB dependency.

## July 2026 Recheck

A proposed MacroDroid enhancement attempted to make Shizuku viable again:

- Macro: `Shizuku Bootstrap`
- Triggers: HTTP endpoint `shizuku-bootstrap`, floating button, and `ShizukuStoppedTrigger`
- Actions: request `adb_wifi_enabled`, launch Shizuku, UI-click the start prompt, then log `ShizukuStateConstraint` result.

Observed deployed state and logs showed the macro repeatedly starting after Shizuku stopped:

```text
[SHIZUKU_BOOTSTRAP] adb_wifi_enabled requested
[SHIZUKU_BOOTSTRAP] attempting UI click id:android:id/button1
[SHIZUKU_BOOTSTRAP] result=running
...
[SHIZUKU_BOOTSTRAP] result=not-running-or-no-permission
```

This proves the MacroDroid trigger/recovery loop works, but it does not prove Shizuku is reliable. The practical failure remains below the macro layer: Wireless debugging is not merely a launch-time dependency in this stack. When Android disables it seconds later, Shizuku goes down with it and the `ShizukuStoppedTrigger` restarts the cycle.

Follow-up burst: `rish` became usable during the brief Shizuku-up window. From `rish`, direct `settings` commands work and confirmed shell UID access. `settings put global cw_disable_wifimediator 1` succeeded and persisted (`cw_disable_wifimediator=1`, `adb_wifi_enabled=1`, `adb_allowed_connection_time=0`). That did not prove stability: the deployed bootstrap macro continued to fire repeatedly and restart Shizuku. Current suspicion is that keeping `ShizukuStoppedTrigger` in the same bootstrap macro may be self-inducing churn because `start.sh` kills the old Shizuku process before starting a new one, which can retrigger the stopped trigger. A test variant has been staged locally by removing `ShizukuStoppedTrigger`; the next useful experiment is to import that replacement and test whether manual/HTTP bootstrap plus `cw_disable_wifimediator=1` stays up without the self-loop.

## Rish / ADB Notes From Recheck

Do not confuse these execution contexts:

| Context | Finding |
|---|---|
| Normal Termux shell | Cannot read target global developer settings here; `settings get global ...` failed with `SecurityException` requiring privileged cross-user access. |
| Termux `adb` | `adb shell ...` requires a connected/paired device. From the phone itself it reported no connected devices unless a separate wireless/self-pairing setup exists. |
| Shizuku `rish` shell | `rish` is already the privileged shell context. Commands should be run directly, not prefixed with `adb shell`. Example shape if Shizuku is running: `RISH_APPLICATION_ID=com.termux sh ~/rish settings put global <key> <value>`. |
| Current rish status during recheck | `RISH_APPLICATION_ID=com.termux sh ~/rish ...` returned `Server is not running`, matching the Shizuku-down failure. |

The attempted command shape was therefore wrong for `rish`:

```sh
sh rish adb shell settings put global cw_disable_wifimediator 1
```

Inside `rish`, use `settings put ...` directly. But that only matters if Shizuku is already alive long enough to run it, and this recheck did not establish that any setting keeps Wireless debugging alive.

## Reopen Criteria

Only reopen the Shizuku path if one of these is demonstrated on the phone without an infinite MacroDroid loop:

1. Wireless debugging can be kept enabled across screen/app/network transitions long enough for Shizuku to remain stable.
2. Shizuku can be started once and then survive after Wireless debugging is disabled.
3. ADB-over-TCP/Tailscale can be bootstrapped after reboot without recurring manual pairing or a self-triggered Shizuku restart loop.
4. A rooted or OEM-supported path changes the privilege model. Until then, this stack is out of scope.

## References

- Android developer docs: Wireless debugging on Android 11+ requires enabling Wireless debugging and pairing with a workstation; ADB Wi-Fi 2.0 improvements are Android 17+ only.
- Android developer docs: Developer options/debugging are communication settings for development tools, not a stable application runtime contract.
- XDA thread noted by operator: Android 12 Wireless debugging auto-turnoff behavior (`https://xdaforums.com/t/android-12-developer-options-adb-wireless-debugging-option-keeps-turning-off.4461375/`). Treat as anecdotal support, not system truth.

---

## Historical March 2026 Attempt

Earlier notes claimed Shizuku was resolved by "Connected to a computer" mode via persistent ADB over Tailscale on port 5555, bypassing Wireless debugging after setup. That approach still had a hard reboot/bootstrap dependency: the phone reboot kills the TCP ADB listener, requiring a fresh bootstrap using Wireless debugging and pairing.

That dependency is now considered too fragile for the active stack. The old `shizuku-connect` CLI may remain in the tree for archaeology, but it is inactive unless a future decision explicitly reopens this path.
