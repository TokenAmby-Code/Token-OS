# Startup Audit (All Devices)

> Last updated: 2026-06-17

Central reference for all custom startup automations across devices.

---

# Mac Mini (100.95.109.23)

## LaunchAgents (`~/Library/LaunchAgents/`)

| Label | State | What It Does |
|-------|-------|-------------|
| **ai.openclaw.tokenapi** | enabled | Token API — FastAPI on `:7777` (uvicorn). KeepAlive on crash. Logs: `~/.claude/token-api-std{out,err}.log` |
| **ai.openclaw.caffeinate** | enabled | `caffeinate -dims` — prevents display, idle, system, and disk sleep. KeepAlive always. **Added 2026-02-22** (was previously relying on openclaw cron watchdog, which used wrong flags) |
| **ai.openclaw.gateway** | enabled | OpenClaw gateway |

### Disabled / Backed Up

| Label | Notes |
|-------|-------|
| **ai.openclaw.watchdog** | `.plist.disabled` — superseded by openclaw cron `overnight-watchdog` |

## Mac Boot Sequence

1. macOS login
2. launchd loads all `~/Library/LaunchAgents/*.plist` with `RunAtLoad`
3. **caffeinate** starts immediately — display stays on
4. **tokenapi** starts on `:7777`
5. **gateway** starts
6. Windows PC boots → Deskflow KVM connects → Mac Token-API runs keymap guard and starts/reloads Deskflow client

## Power Settings (`pmset`)

```
displaysleep    10      ← overridden by caffeinate -d
sleep           0       ← system sleep disabled
autorestart     1       ← restart after power loss
womp            1       ← wake on LAN
```

---

# Windows PC (Task Scheduler + WSL)

## Task Scheduler (All Custom Tasks)

| # | Task Name | RunLevel | Delay | What It Does |
|---|-----------|----------|-------|-------------|
| 1 | **ahk_boot** | Limited | — | Runs local `startup.ahk` bootstrap — boots WSL headlessly, kicks DeskFlow's phased recovery through local `token-satellite`, launches **Bluetooth Audio Receiver**, exposes 10-second startup hotkeys, then applies personal app launch/window-placement policy |
| 2 | **ahk_init** | Limited | — | Runs `script-compiler.ahk` through `ahk-nas-wait.bat` from the Windows-local `C:\TokenOS\ahk` cache (see AHK Architecture below) |
| 3 | **ahk_admin** | Highest | 60s | Runs `ring-remap.ahk` through `ahk-nas-wait.bat` — Bluetooth ring button remapping via AutoHotInterception (needs admin for driver access) |
| 4 | ~~**Deskflow**~~ | Limited | 3s | **DISABLED** — legacy direct DeskFlow launch task. Superseded by `token-satellite` watchdog plus `ahk_boot`'s phased `/kvm/control` kick |
| 5 | ~~**AHK startup mode**~~ | Limited | — | **DISABLED** — folded into `ahk_boot` |
| 6 | ~~**MonitorLauncher**~~ | Limited | 15s | **DISABLED** — folded into `ahk_boot` |
| 7 | **fast_task** | Highest | — | Starts Fast! app (`C:\Program Files (x86)\Fast!\fast!.exe`) |
| 8 | **Dual Monitor Tools** | Limited | — | Starts DMT.exe for multi-monitor management |
| 9 | **Autorun for colby** | Limited | 3s | Starts PowerToys |

### On-Demand Only (No Logon Trigger)

These have no triggers — invoked manually via `schtasks /Run /TN "<name>"` or from AHK/token-satellite.

| Task Name | RunLevel | What It Does |
|-----------|----------|-------------|
| **HeadlessDisable** | Highest | `Toggle-Headless.ps1 -Disable` — disables headless display |
| **HeadlessEnable** | Highest | `Toggle-Headless.ps1 -Enable` — enables headless display |
| **HeadlessToggle** | Highest | `Toggle-Headless.ps1` — toggles headless display state |

---

## WSL Systemd Services

### User Services (`systemctl --user`)

| Service | State | What It Does |
|---------|-------|-------------|
| **token-satellite.service** | enabled | FastAPI on `:7777` — TTS, enforcement, process control, headless toggle. Auto-starts when WSL is running (depends on `MonitorLauncher` keeping WSL alive) |
| **mem-watchdog.service** | static (timer disabled) | One-shot memory check via `mem-watchdog` CLI tool. Timer currently inactive |

### System Services (Stale / Disabled)

| Service | State | Notes |
|---------|-------|-------|
| **token-api.service** | disabled | Old version — superseded by token-satellite |
| **mesh-pipe.service** | disabled | No longer in use |

---

## AHK Script Architecture

Source-of-truth AHK scripts live in this repo at `Token-OS/ahk/`. The WSL runtime source is `/home/token/runtimes/token-os/live`, and every satellite runtime refresh verifies the WSL `/health.git_sha` against the Mac deploy target. `token-satellite-refresh` mirrors the repo AHK tree into the Windows-local cache at `C:\TokenOS\ahk` (`TOKEN_OS_AHK_DIR=/mnt/c/TokenOS/ahk`) and refreshes the startup-owned copies under `%USERPROFILE%` / `%USERPROFILE%\Imperium-Startup`. Windows startup must read AHK entry points from local Windows/WSL storage, not from the retired NAS runtime.

`Setup-StartupTasks.ps1` still copies the boot-critical launcher files into `%USERPROFILE%` / `%USERPROFILE%\Imperium-Startup` for Task Scheduler compatibility. `ahk-nas-wait.bat` keeps its historical name, but its default resolution is now local-cache-first:

- Bare script names such as `script-compiler` resolve to `C:\TokenOS\ahk\<script>.ahk`.
- `ring-remap` uses `%USERPROFILE%\Imperium-Startup\ring-remap.ahk` when that local elevated-task copy exists, otherwise it falls back to the local cache.
- `script-compiler.ahk` is launched from `C:\TokenOS\ahk`; the `Imperium-Startup\script-compiler.ahk` copy is maintained only as an explicit diagnostic/on-demand copy so manual placement cannot drift silently.
- Explicit drive-letter paths and UNC paths are used as-is.
- NAS-relative paths such as `Civic\foo.ahk` remain supported for intentional manual use, but the NAS runtime must not be a Windows startup dependency.

```
startup.ahk                  <- ahk_boot task (copied local at setup time)
ahk-nas-wait.bat            <- local wrapper used by ahk_init / ahk_admin
script-compiler.ahk          <- ahk_init task (main entry point)
  #Include audio-monitor.ahk    Audio device monitoring
  #Include discord-ipc-mute.ahk Discord mute via IPC
  #Include hotkeys.ahk           Global hotkeys

ring-remap.ahk               <- ahk_admin task (standalone, needs admin)
Imperium-Startup\script-compiler.ahk <- maintained diagnostic/on-demand copy; not ahk_init's launch path
startup-launcher.ahk          <- historical standalone startup hotkeys (folded into startup.ahk)
monitor-launcher.ahk          <- historical standalone monitor launcher (folded into startup.ahk)
```

> **Note**: Old copies still exist at `Documents/Obsidian/Personal-ENV/Scripts/ahk/` and under local ad-hoc Windows paths. Treat `Token-OS/ahk/` plus `Powershell/Setup-StartupTasks.ps1` as the canonical source.

---

## Third-Party Autostart (Registry / Installer-Managed)

Not managed by us unless explicitly overridden below. Includes: Docker Desktop, Figma Agent, Medal, Stream Deck, Epic Games, NVIDIA Broadcast, Wooting, Tailscale.

Explicit overrides:

- **Phone Link** packaged startup task is disabled at `HKCU:\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\SystemAppData\Microsoft.YourPhone_8wekyb3d8bbwe\YourPhone.Start` by setting `State=1` and `UserEnabledStartupOnce=0`. Phone Link remains manual until a no-main-monitor-flash wrapper is proven.
- **Wispr Flow** Startup-folder shortcut is moved to `C:\Users\colby\Imperium-Startup\DisabledStartupShortcuts\Wispr Flow.lnk`. `startup.ahk` owns the launch and minimizes it.

### Bonjour LSA Block Repair

Fixed on 2026-06-17. Windows was showing Program Compatibility Assistant errors because legacy Bonjour 3.0.0.10 registered `mdnsNSP.dll` as both 32-bit and 64-bit Winsock namespace providers:

- `C:\Program Files\Bonjour\mdnsNSP.dll`
- `C:\Program Files (x86)\Bonjour\mdnsNSP.dll`

With LSA protection enabled (`RunAsPPL=2`, `RunAsPPLBoot=2`), Windows blocked that old provider from loading into LSASS. No other Apple/iTunes/iCloud/AirPort product was installed, so the clean fix was to uninstall Bonjour rather than weaken LSA protection. Repair script:

- Repo: `Powershell/Repair-BonjourLsaBlock.ps1`
- Local copy used for elevation: `C:\Users\colby\Imperium-Startup\Repair-BonjourLsaBlock.ps1`
- Log: `C:\Users\colby\Imperium-Startup\logs\repair-bonjour-lsa-block.log`

Verification after elevated uninstall: `Bonjour Service` was removed, the Bonjour MSI product entry was removed, and `netsh winsock show catalog` no longer returned `mdnsNSP`. Reboot once to confirm the Program Compatibility Assistant popup is gone.

## Monitor Topology

Current Windows-side physical layout notes from 2026-06-16:

- Large central monitor: primary working display, physically switched between this WSL/Windows PC and the Mac.
- Vertical monitor: left of the main central monitor. Windows currently reports it at `X=-1080, Y=-483, W=1080, H=1920`; display number changed across restarts (`DISPLAY1` before restart, `DISPLAY4` after restart), so select by geometry, not display ID. This is the official Windows ops cockpit display.
- Mini 7-inch monitor: underneath the main monitor on the right side, aligned to the main monitor's east edge. Windows currently reports it as `\\.\DISPLAY2` at `X=1526, Y=1440, W=1024, H=600`.
- Right-side monitor: physically on the right of the main, vertically centered, usually owned by the Mac; can be toggled to this PC via monitor/HDMI controls. In Windows display settings it is placed above the main monitor so normal horizontal cursor travel reaches the Mac Deskflow edge instead of this occasional Windows display.
- Navigation convention: when the right-side monitor is toggled to Windows, reach it by moving off the top of the main display or by the AHK/Wootility `Caps 3` path.
- Wooting note: `Caps 3` triggers a Wootility layer swap; keys `1`, `2`, and `3` are rebound as dynamic keys that execute modifier macro keystrokes. A Wootility profile export may be needed before automating this; check whether the Wootility SDK is available.

## Personal Startup App Policy

Implemented in local `startup.ahk` after a 2-second desktop-settle delay:

- **Phone Link** does not launch on startup. The packaged app startup task is disabled, and the AHK launch timer is disabled because launching Phone Link through AHK still flashes messages on the main monitor before placement catches up. Keep it manual until there is a no-flash wrapper.
- **Wispr Flow** launches through `startup.ahk` and is minimized on the mini monitor; its Startup-folder shortcut is disabled.
- **Discord** launches minimized via Squirrel `Update.exe --processStart Discord.exe --process-start-args "--start-minimized"` when available, is placed on the left vertical monitor, and remains minimized behind the visible ops cockpit.
- **Steam** launches with `-silent`; this is also the prerequisite for Steam Input desktop/gamepad mouse movement support.
- **Bluetooth Audio Receiver** launches through `startup.ahk` and is moved/maximized on the mini 7-inch monitor.
- **Ops cockpit** launches at `http://100.95.109.23:7777/ui/ops` in an explicit Brave app window using a dedicated `C:\Users\colby\Imperium-Startup\Brave-OpsCockpit` profile, then is moved/maximized/activated on the left vertical monitor. Do not launch this through the default browser; Vivaldi is the main workspace and is too heavy for this surface. The TTS interface is currently the ops cockpit's TTS strip and `Voice / TTS queue` panel, not a separate Windows app.
- **Spotify** launches on startup and is moved/maximized on the smallest detected monitor, expected to be the mini 7-inch display.
- Startup dependencies are launched through the same `startup.ahk` app-launch functions that back the temporary startup hotkeys. These are no longer treated as menu decisions; the auto-launch path runs the dependency subset directly.

Follow-ups:

- Confirm whether Steam desktop/gamepad mouse movement needs a specific Steam shortcut/app id, GlosSI/GloSC, or only Steam's Desktop Layout once Steam is running.
- Confirm whether the mini monitor is always the smallest Windows display. If not, replace `GetMiniMonitor()` with a coordinate-based selector.
- Phone Link no-flash wrapper is still unsolved. Do not re-enable startup launch until it can open without exposing messages on the main monitor.
- Several apps still use "launch on default monitor, then move" because their launch APIs do not expose a monitor target. True launch on the intended monitor remains open; if AHK cannot do it, evaluate a stronger window manager/invariant layer such as AlomWare, DisplayFusion, PowerToys FancyZones hooks, or a dedicated wrapper process.
- If a standalone TTS Studio/TUI should launch separately from the ops cockpit, define its target surface and launch command. Current restart test scope treats the ops cockpit TTS panel as the TTS interface.

## Ops Cockpit Browser Rule

- Human/runtime surfaces should use the machine-appropriate Token-API URL. On this Windows/WSL PC, the startup browser opens the Mac-hosted Token-API over Tailscale at `http://100.95.109.23:7777/ui/ops`.
- Browser automation and Playwright validation should run on the Mac Token-API host and target its `http://localhost:7777/...` cockpit there. Do not run these browser tests from Windows against the vertical-monitor cockpit; this keeps tests on a stable same-host target.
- Phone-hosted ops cockpit is the fallback when the WSL/Windows computer is off. It is a separate user surface, not a browser-automation target.
- Brave is allowed for the Windows ops cockpit because `audio-monitor.ahk` only treats media titles as video distraction and explicitly skips Brave windows whose title contains `Ops Cockpit` or whose window center is on the left vertical monitor. If Brave remains the video-player browser too, keep detection title/profile-aware rather than making all Brave windows distractions.

---

## Boot Sequence

1. Windows logon
2. Task Scheduler fires all logon-triggered tasks (with respective delays)
3. **ahk_boot** starts immediately and boots WSL Ubuntu headlessly, which triggers systemd
4. WSL systemd starts **token-satellite** on `:7777`
5. `startup.ahk` waits for `token-satellite` health, then calls `POST /kvm/control {"action":"reload"}` to kick DeskFlow's phased recovery ladder instead of launching the old task directly. Mac KVM start/reload runs `Shell/deskflow-keymap-guard.sh` to preserve the Australian-input-source keymap fix.
6. `startup.ahk` waits 2 seconds for the desktop/monitor layout to settle, then schedules Steam, Discord, Wispr Flow, Bluetooth Audio Receiver, Spotify, and the ops cockpit with the app placement policy above. Phone Link is intentionally not launched at startup. Launches are staggered over 2.5 seconds, and placement retries are timer-driven so a slow window match does not block the remaining startup dependencies.
7. `ahk_init`, `ahk_admin`, PowerToys, DMT, and Fast! start on their respective schedules

### Ring Remap Startup Diagnostic

`ahk_admin` points at the local `%USERPROFILE%\ahk-nas-wait.bat` wrapper with `ring-remap` as its argument. For `ring-remap.ahk`, the wrapper now prefers `%USERPROFILE%\Imperium-Startup\ring-remap.ahk`; `Setup-StartupTasks.ps1` maintains that local copy. This avoids the elevated-task NAS credential problem where the same UNC path works in a normal shell but the admin task waits or throws "file not found". The wrapper still logs to `%USERPROFILE%\Imperium-Startup\logs\ahk-nas-wait.log` and exits without launching AutoHotkey if the selected script is unavailable. If a visible "file not found" error still appears, the live Windows scheduled task is probably stale or pointing directly at the NAS `.ahk`; rerun `Powershell/Setup-StartupTasks.ps1` as Administrator and inspect `Get-ScheduledTask -TaskName ahk_admin | Get-ScheduledTaskInfo`.

### Runtime Locality Rule

- WSL runtime: `/home/token/runtimes/token-os/live`.
- Windows AHK startup cache: `C:\TokenOS\ahk`.
- WSL view of that cache: `/mnt/c/TokenOS/ahk`.
- `%USERPROFILE%\startup.ahk`, `%USERPROFILE%\ahk-nas-wait.bat`, and `%USERPROFILE%\Imperium-Startup\{ring-remap.ahk,script-compiler.ahk}` are deploy-refreshed startup artifacts.
- `token-api/scripts/validate-windows-ahk-startup-drift` is the read-only drift validator. It fails on cache/startup hash drift, scheduled-task drift, or retired NAS runtime references; it does not self-heal.
- The retired NAS runtime mount is not a startup dependency and must not be used by Windows logon tasks or AHK hotkeys that recover local WSL services.

## Restart Test Preflight

State captured on 2026-06-16 before the first real restart test:

- Scheduled tasks are locked in:
  - `ahk_boot` -> `C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe "C:\Users\colby\startup.ahk"`
  - `ahk_init` -> `C:\Users\colby\ahk-nas-wait.bat script-compiler`
  - `ahk_admin` -> `C:\Users\colby\ahk-nas-wait.bat ring-remap`, delay `PT1M`
- Local boot-critical files exist:
  - `C:\Users\colby\startup.ahk`
  - `C:\Users\colby\ahk-nas-wait.bat`
  - `C:\Users\colby\Imperium-Startup\ring-remap.ahk`
- AutoHotkey v2 is installed at `C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe`; `/Validate` was run against `startup.ahk` and `ring-remap.ahk`.
- Monitor assertions before restart test 2:
  - Left vertical cockpit monitor: currently `\\.\DISPLAY4`, `X=-1080`, `Y=-483`, `W=1080`, `H=1920`; select by `height > width` and `right <= 0`, not by display number.
  - Mini monitor: currently `\\.\DISPLAY2`, `X=1526`, `Y=1440`, `W=1024`, `H=600`; selected as the smallest display by area.
  - Primary main monitor: currently `\\.\DISPLAY3`, `X=0`, `Y=0`, `W=2560`, `H=1440`.
  - Right/above optional monitor: currently `\\.\DISPLAY1`, `X=630`, `Y=-1080`, `W=1920`, `H=1080`.

Expected pass conditions after restart:

- No visible AutoHotkey "file not found" error for `ring-remap.ahk`.
- `ahk_admin` log contains `launching "C:\Users\colby\Imperium-Startup\ring-remap.ahk"` or the running AHK process list shows the local ring-remap script.
- `script-compiler.ahk` starts through the local-cache wrapper; main hotkeys and the `Ctrl+Alt+R` ring restart hotkey path still work.
- Ops cockpit opens at `http://100.95.109.23:7777/ui/ops` on the left vertical monitor.
- Ops cockpit uses Brave app mode with the dedicated `Brave-OpsCockpit` profile, not Vivaldi.
- Ops cockpit is maximized on the left vertical monitor.
- Spotify opens maximized on the mini 7-inch monitor.
- Phone Link launches but is moved to the mini monitor and minimized; it must not remain fullscreen on the main monitor.
- Discord and Steam launch minimized/silent.
- WSL boots headlessly and token-satellite comes up; Deskflow recovers through the existing phased restart path.

Manual post-restart checks:

```powershell
Get-ScheduledTask -TaskName ahk_boot,ahk_init,ahk_admin |
  Select TaskName,State,@{n="Execute";e={$_.Actions[0].Execute}},@{n="Arguments";e={$_.Actions[0].Arguments}},@{n="Delay";e={$_.Triggers[0].Delay}}

Get-Content "$env:USERPROFILE\Imperium-Startup\logs\ahk-nas-wait.log" -Tail 40

Get-Process AutoHotkey64 -ErrorAction SilentlyContinue |
  Select Id,ProcessName,Path,MainWindowTitle
```

## Future Forks

Loose threads from the 2026-06-16 startup/monitor brain dump, ready to split into focused implementors later:

- **Phone Link privacy:** If even a brief launch flash is visible, replace the current launch/move/minimize sequence with a stronger privacy strategy: delayed launch after monitor layout is stable, background/session-only launch if Phone Link exposes one, or a dedicated mini-monitor launch wrapper.
- **Phone Link mini wrapper:** `ahk/phone-link-mini.ahk` is the current manual test wrapper. First attempt caused rapid flicker because it repeatedly restored/minimized the same Phone Link window every 100ms; the patched wrapper now tracks whether a window is already centered on the mini monitor and should only act when it is minimized, off-monitor, or not yet revealed.
- **True monitor-targeted launch:** Current AHK policy mostly performs post-launch placement. The desired invariant is stronger: apps should create their first visible frame on the intended monitor, especially Phone Link and other private surfaces. Investigate whether this is possible through app-specific CLI flags, Windows APIs, virtual desktop staging, AlomWare/DisplayFusion-style window rules, or a custom launcher that prepositions/owns the first window.
- **Steam gamepad mouse movement:** Confirm whether Steam Input Desktop Layout alone is enough for mouse movement via gamepad, or whether a specific Steam shortcut/app id, GlosSI/GloSC-style target, or Steam Big Picture behavior is needed.
- **Wootility / Caps 3 path:** Export/check the current Wootility profile. `Caps 3` currently triggers a Wootility layer swap and `1`, `2`, `3` are dynamic keys that execute modifier macro keystrokes. Investigate whether the Wootility SDK can automate or introspect this.
- **Ring remapper device detection:** `ring-remap.ahk` bound to the regular mouse after restart. `MINIMUM_RING_ID` was lowered from `20` to `14` on 2026-07-09 because the D06 Pro can reappear below 20 on the WSL PC; the local elevated copy must be refreshed/restarted through the satellite refresh path. If this binds a regular mouse again, stop relying on "highest mouse above threshold" and identify the D06 Pro by hardware path / VID-PID / name from AutoHotInterception's device list.
- **Monitor selection hardening:** Current code selects left vertical by geometry and mini by smallest area. If Windows display IDs or geometry drift, replace with a small monitor registry/config file and assertion check.
- **Mini-monitor taskbar:** User would prefer no taskbar on the 7-inch monitor unless intentionally interacting there, but per-monitor hover/swipe taskbar behavior is out of active startup scope because it likely requires a persistent window-manager layer rather than the short-lived startup launcher.
- **Ops cockpit testing doctrine:** Formalize that browser automation uses localhost ops cockpits on the Mac Token-API host only. The physical Windows vertical-monitor cockpit is a human/runtime surface and should not be hijacked by Playwright or agent browser tests.
- **Phone-hosted cockpit:** When the WSL/Windows PC is off, phone hosts its own ops cockpit surface. Treat it as a separate operational surface, not the automation target.
- **Dedicated ops shell:** If Brave app mode is still too heavy or too entangled with video/distraction policy, evaluate a third browser, a WebView wrapper, or a local TypeScript/Electron/Tauri-style shell for the ops web client.
- **AlomWare invariants:** AlomWare Toolbox remains a candidate for system-level window invariants if AHK window forcing stays brittle. Current public docs emphasize GUI-authored actions, hotkeys, window events, scheduled tasks, DOS commands, app launch, website launch, and window automation; no clean external CLI/API for importing/managing actions has been confirmed yet.
- **Session-doc/process hygiene:** This note is the durable handoff because this Codex pane did not have a resolvable Token session-doc binding. Future agents should start from `[[Ultramar/Reference/Startup Audit]]` plus today's daily-note link if the chat thread is gone.
- **AHK cache/runtime locality:** Startup is local-cache-first. `Imperium/runtimes` is deprecated as a startup source; use `/home/token/runtimes/token-os/live` for the WSL runtime and `C:\TokenOS\ahk` for Windows AHK launch. NAS-relative launcher support is only for explicit manual paths.

## Restart Test 1 Result

Observed after the first real restart on 2026-06-16:

- Correct monitors were selected.
- Windows appeared after a long delay and then launched in a single wave. Root cause: the first implementation used blocking `WinWait` calls inside the startup sequence.
- Ops cockpit opened in Vivaldi because the launch used `explorer.exe` with the URL, which delegates to the default browser. This is wrong because Vivaldi is the main workspace.
- Windows were placed but not maximized.

Follow-up patch:

- Ops cockpit now launches through explicit Brave app mode with a dedicated profile.
- Window placement uses retry timers instead of blocking waits.
- Launches are staggered at 0s/3s/6s/9s/12s after the 45s desktop-settle timer.
- Ops cockpit and Spotify are maximized after being moved to their target monitors.

## Restart Test 2 Prep

Prepared on 2026-06-16 after patching the first restart findings.

What changed since restart test 1:

- `startup.ahk` no longer launches the ops cockpit through `explorer.exe`; it uses explicit Brave app mode with `--app=http://100.95.109.23:7777/ui/ops` and `--user-data-dir=C:\Users\colby\Imperium-Startup\Brave-OpsCockpit`.
- `startup.ahk` no longer blocks on `WinWait` during the dependency launch phase. It uses `ScheduleWindowPlacement()` retry timers and staggered launch timers.
- `startup.ahk` keeps the old startup hotkey menu semantics, but the auto-start dependencies call the same app-launch functions directly.
- `audio-monitor.ahk` explicitly ignores Brave windows whose title contains `Ops Cockpit` so the Brave ops shell does not count as the video/distraction Brave surface.
- `ahk_init` was restarted after the audio-monitor patch so the current long-lived AHK suite has the Brave ops exclusion loaded.

Live preflight state:

- `C:\Users\colby\startup.ahk` exists and was last synced at 2026-06-16 17:24, length 7885 bytes.
- `C:\Users\colby\ahk-nas-wait.bat` exists, length 1705 bytes.
- `C:\Users\colby\Imperium-Startup\ring-remap.ahk` exists, length 27599 bytes.
- Brave exists at `C:\Users\colby\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe`.
- `C:\Users\colby\Imperium-Startup\Brave-OpsCockpit` may not exist before first launch; Brave should create this dedicated profile directory during restart test 2.
- AutoHotkey validation passed for live `startup.ahk`, repo `script-compiler.ahk`, and repo/local ring remap before restart test 2.
- Scheduled tasks still point to the expected launchers:
  - `ahk_boot`: AutoHotkey -> `C:\Users\colby\startup.ahk`
  - `ahk_init`: `C:\Users\colby\ahk-nas-wait.bat script-compiler`
  - `ahk_admin`: `C:\Users\colby\ahk-nas-wait.bat ring-remap`, delay `PT1M`

Restart test 2 pass conditions:

- Before restarting, close the stale Vivaldi ops cockpit window from restart test 1 if it is still open. Vivaldi itself can remain open, but it should not own the ops cockpit surface.
- Launches should feel staggered rather than delayed and released in one wave.
- Ops cockpit should open in Brave app mode, not Vivaldi.
- Ops cockpit should be maximized on the left vertical monitor selected by geometry (`height > width`, `right <= 0`).
- Spotify should be maximized on the mini monitor selected by smallest area.
- Phone Link should end minimized on the mini monitor and should not remain fullscreen on the main monitor.
- Brave ops cockpit should not trip desktop audio/video distraction detection.
- `Brave-OpsCockpit` profile directory should exist after the test.

## Restart Test 2 Result

Observed after the second real restart on 2026-06-16:

- Ops cockpit looked good and landed on the correct left vertical monitor.
- Launch still happened too slowly. Phone Link had already popped up before our AHK placement/minimize path caught it.
- Wispr Flow also popped up on the main monitor. Root cause: it still had its own Startup-folder shortcut, so AHK was racing installer-managed startup.
- Bluetooth Audio Receiver needs to be explicitly launched to the mini 7-inch monitor.
- Discord was not visible. Desired invariant: Discord launches on the left vertical monitor but stays minimized; the ops cockpit is what is actually showing.
- Brave video/distraction detection needs belt-and-suspenders guards: ops cockpit title is safe, and anything on the left vertical ops monitor is safe.

## Restart Test 3 Prep

Prepared on 2026-06-16 after patching restart test 2 findings.

What changed since restart test 2:

- Phone Link packaged startup task is disabled live and in `Setup-StartupTasks.ps1`: `State=1`, `UserEnabledStartupOnce=0` under `YourPhone.Start`.
- Wispr Flow's user Startup-folder shortcut is disabled live and in `Setup-StartupTasks.ps1` by moving it to `C:\Users\colby\Imperium-Startup\DisabledStartupShortcuts\Wispr Flow.lnk`.
- `startup.ahk` now starts managed apps after 15 seconds instead of 45 seconds.
- `startup.ahk` owns the launch/placement for Steam, Discord, Wispr Flow, Bluetooth Audio Receiver, Phone Link, Spotify, and the ops cockpit.
- Discord launches minimized and is placed on the left vertical monitor, while the ops cockpit launches last and is activated/maximized on that same monitor.
- Bluetooth Audio Receiver launches through the UWP app id `55746MarkSmirnov.BluetoothAudioReveicer_xwrbx6997tsfc!App` and is moved/maximized on the mini monitor.
- Wispr Flow launches minimized and is placed on the mini monitor.
- Phone Link launches only through AHK, then is moved to the mini monitor and minimized.
- `audio-monitor.ahk` skips Brave windows whose title contains `Ops Cockpit` and skips Brave windows centered on the left vertical monitor.

Live preflight state:

- `C:\Users\colby\startup.ahk` has been synced from repo; length is 8996 bytes.
- Phone Link packaged startup task is disabled: `State=1`, `UserEnabledStartupOnce=0`.
- Wispr Flow Startup-folder shortcut is absent; disabled copy exists at `C:\Users\colby\Imperium-Startup\DisabledStartupShortcuts\Wispr Flow.lnk`.
- Scheduled tasks still point to expected launchers:
  - `ahk_boot`: `C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe "C:\Users\colby\startup.ahk"`
  - `ahk_init`: `C:\Users\colby\ahk-nas-wait.bat script-compiler`
  - `ahk_admin`: `C:\Users\colby\ahk-nas-wait.bat ring-remap`, delay `PT1M`
- Current monitor assertions:
  - Left vertical cockpit monitor: `\\.\DISPLAY4`, `X=-1080`, `Y=-483`, `W=1080`, `H=1920`; selected by `height > width` and `right <= 0`.
  - Mini monitor: `\\.\DISPLAY2`, `X=1526`, `Y=1440`, `W=1024`, `H=600`; selected as the smallest display by area.
  - Primary main monitor: `\\.\DISPLAY3`, `X=0`, `Y=0`, `W=2560`, `H=1440`.
  - Right/above optional monitor: `\\.\DISPLAY1`, `X=630`, `Y=-1080`, `W=1920`, `H=1080`.
- AutoHotkey validation returned clean exit codes for live `startup.ahk`, repo `script-compiler.ahk`, and local `ring-remap.ahk`.

Restart test 3 pass conditions:

- Phone Link must not appear by itself at login before AHK launches it. If it appears early, the packaged startup disable is incomplete or another startup source exists.
- Phone Link should end minimized on the mini monitor and must not remain fullscreen or visible on the main/living-room monitor.
- Wispr Flow should not appear by itself on the main monitor; it should launch through AHK and end minimized.
- Bluetooth Audio Receiver should be on the mini 7-inch monitor.
- Spotify should be maximized on the mini monitor.
- Ops cockpit should open in Brave app mode, not Vivaldi, on the left vertical monitor, maximized and active.
- Discord should be launched and placed on the left vertical monitor, but minimized; the visible foreground app on that monitor should be the ops cockpit.
- Steam should launch silently/minimized for Steam Input desktop/gamepad mouse movement support.
- Brave ops cockpit should not trip desktop audio/video distraction detection, by title and by left-vertical-monitor location.
- Ring remap should not throw a startup "file not found" error.

## Restart Test 3 Result

Observed after the third real restart on 2026-06-17:

- Wispr Flow still launched to the main monitor.
- Two ops cockpit windows appeared; one was on the correct left vertical monitor.
- Steam launched a visible window on the main monitor.
- Discord was not visually obvious, but Stream Deck Discord buttons loaded properly; treat Discord as acceptable if its integration surface is active and it is not visible.

Root causes found:

- `ImperiumStartupAhk` still existed in `HKCU:\Software\Microsoft\Windows\CurrentVersion\Run`, so `startup.ahk` could launch once via HKCU Run and once via the `ahk_boot` scheduled task. This explains duplicate ops cockpit windows.
- Wispr Flow recreated `Wispr Flow.lnk` in the user Startup folder after launch, so moving the shortcut once was not durable.
- Steam's visible UI window belongs to `steamwebhelper.exe`, not `steam.exe`; minimizing `steam.exe` did not catch the real window.

## Restart Test 4 Prep

Prepared on 2026-06-17 after patching restart test 3 findings.

What changed since restart test 3:

- Live `ImperiumStartupAhk` HKCU Run value has been removed.
- `Setup-StartupTasks.ps1` no longer creates the HKCU Run fallback. Task Scheduler `ahk_boot` is now the single bootstrap owner.
- Live Wispr Flow Startup-folder shortcut has been moved back to `C:\Users\colby\Imperium-Startup\DisabledStartupShortcuts\Wispr Flow.lnk`; no Wispr startup-command entry remained after cleanup.
- `Setup-StartupTasks.ps1` now checks all user/common Startup folder variants for `Wispr Flow.lnk`.
- `startup.ahk` removes any newly recreated `Wispr Flow.lnk` 15 seconds after the managed Wispr launch.
- `startup.ahk` now schedules minimization for `steamwebhelper.exe`, the actual visible Steam UI process.
- Discord remains acceptable if its Stream Deck integration loads and it stays visually unobtrusive.

Live preflight state:

- `C:\Users\colby\startup.ahk` has been synced from repo; length is 9851 bytes.
- `ImperiumStartupAhk` does not exist under HKCU Run.
- No Wispr Flow Startup-folder entries were found by `Win32_StartupCommand`.
- Scheduled tasks still point to expected launchers:
  - `ahk_boot`: `C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe "C:\Users\colby\startup.ahk"`
  - `ahk_init`: `C:\Users\colby\ahk-nas-wait.bat script-compiler`
  - `ahk_admin`: `C:\Users\colby\ahk-nas-wait.bat ring-remap`, delay `PT1M`
- AutoHotkey validation returned exit code `0` for live `startup.ahk`, repo `script-compiler.ahk`, and local `ring-remap.ahk`.

Restart test 4 pass conditions:

- Exactly one ops cockpit window should open.
- Ops cockpit should open in Brave app mode on the left vertical monitor, maximized and active.
- Wispr Flow should not self-launch to the main monitor. If Wispr creates a Startup shortcut after launch, AHK should remove it before the next restart.
- Steam should not leave a visible `Steam` / `steamwebhelper.exe` window on the main monitor.
- Discord is acceptable if Stream Deck Discord controls load and Discord remains visually unobtrusive.
- Phone Link should still not self-launch before AHK and should end minimized on the mini monitor.
- Bluetooth Audio Receiver and Spotify should still target the mini monitor.

## Restart Test 4 Result

Observed after the fourth real restart on 2026-06-17:

- Wispr Flow still launched to the main monitor.
- Phone Link still launched to the main monitor.
- User confirmed Phone Link settings also report startup disabled.
- User disabled Wispr Flow's own "launch on startup" setting before this restart, so Wispr should not have recreated its startup task.

Root causes / corrected assumptions:

- The startup-command audit was clean: no Wispr, Phone Link, or Imperium duplicate startup entries were present. The visible launches were therefore coming from the managed AHK launch path, not generic Windows startup entries.
- Phone Link was still being launched by `startup.ahk`; even with Windows/Phone Link startup disabled, our own launch caused the flash.
- The AHK placement helper stopped after the first matching process window. Apps with helper/invisible windows can satisfy the match before the real UI appears, leaving the later visible window on the main monitor.

## Restart Test 5 Prep

Prepared on 2026-06-17 after patching restart test 4 findings.

What changed since restart test 4:

- `startup.ahk` no longer schedules `LaunchPhoneLinkPrivate`; Phone Link should not launch at startup at all.
- `ScheduleWindowPlacement()` now sweeps all matching windows, not only the first `WinExist()` match.
- `ScheduleWindowPlacement()` supports `continueAfterFound`; Wispr and Phone Link placement callers use it so delayed real UI windows keep getting moved/minimized for the full retry window instead of stopping on an early helper window.
- Wispr Flow direct launch now uses AHK's hidden run mode, then a 90-attempt / 500ms repeated placement sweep on all `Wispr Flow.exe` windows.

Live preflight state:

- `C:\Users\colby\startup.ahk` has been synced from repo; length is 10597 bytes.
- No Wispr, Phone Link, YourPhone, Imperium, or `startup.ahk` startup-command entries were found by `Win32_StartupCommand`.
- Phone Link packaged startup task remains disabled: `State=1`, `UserEnabledStartupOnce=0`.
- AutoHotkey validation returned exit code `0` for live `startup.ahk`, repo `script-compiler.ahk`, and local `ring-remap.ahk`.

Restart test 5 pass conditions:

- Phone Link should not launch at startup at all.
- Wispr Flow may launch because AHK still owns it, but it should not remain visible on the main monitor; it should be hidden/minimized by the repeated all-window sweep.
- Exactly one ops cockpit window should open on the left vertical monitor.
- Steam should not leave a visible `Steam` / `steamwebhelper.exe` window on the main monitor.
- Bluetooth Audio Receiver and Spotify should still target the mini monitor.

## Restart Test 5 Result

Observed on 2026-06-17:

- Startup held: nothing popped up on the main monitor.
- Phone Link did not launch, matching the test constraint.
- Remaining user request: Phone Link should eventually launch automatically, but only if it can avoid main-monitor exposure.
- Remaining system-design gap: some apps still visibly flash on the main monitor before AHK moves them. Desired future behavior is true launch on the target monitor, not flash-launch-and-move.
- Startup launcher menu still included Spotify even though Spotify is now auto-launched.

Patch:

- `startup.ahk` startup hotkey tray/menu was de-duplicated: `S=Spotify` and the `s:: LaunchSpotify()` startup hotkey were removed because Spotify is now an auto-launched dependency.
- Normal Brave remains in the menu because it is distinct from the auto-launched Brave ops cockpit app profile.

## Restart Test 6 Prep

Prepared on 2026-06-17 for the speed and true-launch pass.

What changed since restart test 5:

- `StartupAppDelaySeconds` was reduced from `15` to `2`.
- Managed startup app stagger was compressed from roughly 12 seconds to 2.5 seconds:
  - Steam at app-phase start.
  - Discord at +0.5s.
  - Wispr Flow at +1.0s.
  - Bluetooth Audio Receiver at +1.5s.
  - Spotify at +2.0s.
  - Ops cockpit at +2.5s.
- Placement watchers now start before launching Discord, Steam UI helper, Wispr Flow, Bluetooth Audio Receiver, Spotify, and the ops cockpit.
- Placement retry cadence for the high-risk startup windows is now 500ms with longer retry windows.
- Brave ops cockpit now launches with `--window-position=<left-vertical-x>,<left-vertical-y>` and `--window-size=<left-vertical-w>,<left-vertical-h>` before the AHK maximize/activate sweep. This is the closest current true-launch candidate because Brave exposes first-window geometry flags.
- Deskflow bootstrap is more event-driven: `startup.ahk` now calls `Invoke-DeskflowBoot.ps1 -DelaySeconds 0`, so the script polls Token Satellite health immediately instead of sleeping 20 seconds before polling.
- Startup launcher menu remains de-duplicated: Spotify is not offered because it is auto-launched.

Live preflight state:

- `C:\Users\colby\startup.ahk` has been synced from repo; length is 10650 bytes.
- AutoHotkey validation returned exit code `0` for live `startup.ahk`.
- No Wispr, Phone Link, YourPhone, Imperium, or `startup.ahk` startup-command entries were found by `Win32_StartupCommand`.
- Scheduled tasks still point to expected launchers:
  - `ahk_boot`: `C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe "C:\Users\colby\startup.ahk"`
  - `ahk_init`: `C:\Users\colby\ahk-nas-wait.bat script-compiler`
  - `ahk_admin`: `C:\Users\colby\ahk-nas-wait.bat ring-remap`, delay `PT1M`

Restart test 6 pass conditions:

- Startup app phase should begin materially sooner than prior tests.
- No main-monitor popups should regress.
- Ops cockpit should still land on the left vertical monitor and should appear faster because Brave receives target geometry at launch.
- Spotify and Bluetooth Audio Receiver should still target the mini monitor.
- Wispr Flow should remain hidden/minimized and not appear on the main monitor.
- Phone Link should still not launch during startup.

## Restart Test 6 Result

Observed on 2026-06-17:

- Launches were much faster.
- Spotify, Bluetooth Audio Receiver, and ops cockpit rapidly fullscreened/unfullscreened on a ticking loop.
- Cause: the 500ms placement sweep with `continueAfterFound=true` kept calling `WinRestore`, `WinMove`, and `WinMaximize` after those windows were already correctly placed/maximized.

Patch:

- Fullscreen targets now stop after the first successful placement/maximize:
  - Spotify uses `continueAfterFound=false`.
  - Bluetooth Audio Receiver uses `continueAfterFound=false`.
  - Ops cockpit uses `continueAfterFound=false`.
- `startup.ahk` now detects when a window is already centered on the target monitor.
- Placement sweeps skip already-minimized windows, avoiding restore/minimize thrash for minimized targets.
- Placement sweeps skip already-maximized windows on the target monitor, avoiding repeated restore/maximize thrash if a retry loop is still active.

Live preflight state:

- `C:\Users\colby\startup.ahk` has been synced from repo; length is 11412 bytes.
- AutoHotkey validation returned exit code `0` for live and repo `startup.ahk`.

Restart test 7 pass conditions:

- Startup should remain fast.
- Spotify, Bluetooth Audio Receiver, and ops cockpit should maximize once and stop.
- No fullscreen/unfullscreen ticking loop.
- No main-monitor popup regression.

## Startup Polish Pass

Prepared on 2026-06-17 after restart test 7 feedback.

Observed / requested:

- Startup is mostly good.
- Wispr Flow was not fullscreened, but that is acceptable for now because it is intended to stay unobtrusive.
- User would like Bluetooth Audio Receiver to automatically open the audio connection.
- Startup-created taskbar attention flashes should be suppressed where possible.
- Ops cockpit should be browser-fullscreened on the left vertical monitor.
- Per-monitor taskbar behavior on the 7-inch monitor would be nice but is not worth a major complexity step right now.

Patch:

- Added `Powershell/Open-BluetoothAudioReceiverConnection.ps1`, which uses UI Automation to invoke Bluetooth Audio Receiver's `Open Connection` button (`OpenAudioPlaybackConnectionButtonButton`) when the UWP window is ready.
- `Setup-StartupTasks.ps1` now copies `Open-BluetoothAudioReceiverConnection.ps1` into `C:\Users\colby\Imperium-Startup`.
- `startup.ahk` calls `Open-BluetoothAudioReceiverConnection.ps1 -TimeoutSeconds 45` after launching Bluetooth Audio Receiver.
- `startup.ahk` calls `FlashWindowEx` with `FLASHW_STOP` on managed windows during placement to reduce orange taskbar attention flashing.
- Ops cockpit Brave launch now includes `--start-fullscreen` in addition to target `--window-position` and `--window-size`.
- Mini-monitor taskbar management is explicitly out of scope for this startup pass.

Live preflight state:

- `C:\Users\colby\startup.ahk` has been synced from repo; length is 12186 bytes.
- `C:\Users\colby\Imperium-Startup\Open-BluetoothAudioReceiverConnection.ps1` exists and parses.
- AutoHotkey validation returned exit code `0` for live and repo `startup.ahk`.
- The Bluetooth connection script ran manually with exit code `0`; success of the actual connection should be confirmed at the next startup because the script exits cleanly on timeout too.

## Startup Polish Result

Observed on 2026-06-17 after the polish pass:

- Overall startup behavior is mostly good and stable.
- Wispr Flow still does not fullscreen. It stayed unobtrusive, but if the desired behavior is fullscreen/maximized on the mini monitor, the next thread should explicitly change its policy from "minimized/hidden" to "visible fullscreen/maximized" and decide whether that conflicts with keeping Wispr out of the way.
- Bluetooth Audio Receiver did not automatically open the audio connection. The UI Automation script parses and runs, but its current "exit 0 on timeout" behavior makes it hard to distinguish "button invoked" from "button not found / not ready". Next thread should add logging and assert on the `ConnectionState` / button availability before claiming success.

Next-thread handoff:

- Keep the current fast startup and anti-thrash placement logic.
- Investigate Bluetooth Audio Receiver automation with evidence: log whether `OpenAudioPlaybackConnectionButtonButton` is found, whether InvokePattern succeeds, and whether `ConnectionState` changes after invocation.
- Decide Wispr's intended startup state: hidden/minimized vs visible fullscreen on the mini monitor.
- Phone Link remains disabled at startup until a true no-main-monitor-flash launch path exists.
- Per-monitor taskbar behavior remains out of active startup scope unless a persistent window-manager layer is introduced.

## Manual Phone Link Mini Test

Started on 2026-06-17 after restart test 5 held.

Observed:

- Launching `phone-link-mini.ahk` did not create a sustained visible Phone Link window on the main monitor, but the first wrapper caused rapid flicker.
- The flicker was caused by wrapper behavior, not by repeated launch/destroy: it swept every 100ms and repeatedly called `WinRestore`, `WinMove`, and `WinMinimize` on the same Phone Link window.

Patch:

- `phone-link-mini.ahk` now tracks whether each Phone Link window is already centered on the mini monitor.
- The wrapper only minimizes during the initial catch phase, then reveals/restores the window once on the mini monitor and leaves it alone unless it drifts off the mini monitor or is still minimized.
- No `phone-link-mini.ahk` process was left running after the interrupted flicker test.

Manual test expectation:

- Run `ahk/phone-link-mini.ahk` manually.
- Phone Link should appear on the mini monitor without rapid flicker and without a sustained main-monitor exposure.
- If the wrapper still flickers, reduce the loop to a short pre-launch catch phase followed by a one-shot restore/move, or move this to a stronger window manager/invariant layer.

## Ring Remapper Device Threshold

Observed on 2026-06-17:

- `ring-remap.ahk` bound to the regular mouse.
- Previous detector accepted the highest mouse device ID as the ring if it was at least `14`.

Patch:

- `MINIMUM_RING_ID` raised from `14` to `20`.
- Stale comment `Ring device ID: 14` replaced with `Ring device ID: auto-detected above MINIMUM_RING_ID`.
- Local elevated copy `C:\Users\colby\Imperium-Startup\ring-remap.ahk` was synced and validated.
- `ahk_admin` was restarted so the threshold is active immediately.

## Deskflow KVM Canonical Reference

Deskflow architecture, keymap fix, and incident history are consolidated in the vault at `Terra/Ultramar/Personal-Infra/Deskflow KVM.md`. Do not use old incident notes as source of truth.

---

## How to Add New Startup Items

**Preferred method: Task Scheduler** — all custom startup items should use Task Scheduler for consistency.

**Canonical setup script:** `Powershell/Setup-StartupTasks.ps1`
This script copies the boot-critical startup assets local (`%USERPROFILE%\startup.ahk`, `%USERPROFILE%\ahk-nas-wait.bat`, `%USERPROFILE%\Imperium-Startup\*.ps1`) and then re-registers the managed tasks. It also disables the obsolete standalone `Deskflow`, `AHK startup mode`, and `MonitorLauncher` tasks.

### Create via PowerShell
```powershell
$action = New-ScheduledTaskAction -Execute "program.exe" -Argument "args"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "colby"
$trigger.Delay = "PT5S"  # optional delay
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "colby" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "MyTask" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "What it does"
```

Use `-RunLevel Highest` only if the program needs admin privileges.

### Create On-Demand Task (No Trigger)
```powershell
# Same as above but omit the $trigger and -Trigger parameter
Register-ScheduledTask -TaskName "MyTask" -Action $action -Settings $settings -Principal $principal
# Invoke with: schtasks /Run /TN "MyTask"
```

### WSL Systemd Service
```bash
# Create service file
nano ~/.config/systemd/user/my-service.service
# Enable and start
systemctl --user enable --now my-service.service
```

---

## Removed Items

| Item | Date | Reason |
|------|------|--------|
| **TokenAPI** (Task Scheduler) | 2026-02-17 | token-api moved to Mac Mini; token-satellite replaced it on this machine |
| **Deskflow.bat** (Startup Folder) | 2026-02-17 | Migrated to Task Scheduler for consistency |
| **WSL Keep-Alive** (Task Scheduler) | 2026-02-19 | Disabled (not deleted) — opened visible cmd.exe/conhost window on boot. Redundant now that MonitorLauncher keeps WSL alive via Windows Terminal |
