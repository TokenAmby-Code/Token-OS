# Startup Audit (All Devices)

> Last updated: 2026-05-08

Central reference for all custom startup automations across devices.

---

# Mac Mini (100.95.109.23)

## LaunchAgents (`~/Library/LaunchAgents/`)

| Label | State | What It Does |
|-------|-------|-------------|
| **ai.openclaw.tokenapi** | enabled | Token API — FastAPI on `:7777` (uvicorn). KeepAlive on crash. Logs: `~/.claude/token-api-std{out,err}.log` |
| **ai.openclaw.caffeinate** | enabled | `caffeinate -dims` — prevents display, idle, system, and disk sleep. KeepAlive always. **Added 2026-02-22** (was previously relying on openclaw cron watchdog, which used wrong flags) |
| **ai.openclaw.gateway** | enabled | OpenClaw gateway |
| **ai.openclaw.discord-context** | enabled | Discord context collector |

### Disabled / Backed Up

| Label | Notes |
|-------|-------|
| **ai.openclaw.watchdog** | `.plist.disabled` — superseded by openclaw cron `overnight-watchdog` |

## Mac Boot Sequence

1. macOS login
2. launchd loads all `~/Library/LaunchAgents/*.plist` with `RunAtLoad`
3. **caffeinate** starts immediately — display stays on
4. **tokenapi** starts on `:7777`
5. **gateway** + **discord-context** start
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
| 1 | **ahk_boot** | Limited | — | Runs local `startup.ahk` bootstrap — launches Windows Terminal `monitor`, kicks DeskFlow's phased recovery through local `token-satellite`, launches **Bluetooth Audio Receiver**, and exposes 10-second startup hotkeys |
| 2 | **ahk_init** | Limited | — | Runs `script-compiler.ahk` through `ahk-nas-wait.bat` — the main NAS-backed AHK suite (see AHK Architecture below) |
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

Source-of-truth AHK scripts live in this repo at `Token-OS/ahk/`. `Setup-StartupTasks.ps1` copies the boot-critical launcher locally so it still runs before the NAS is mounted. The main long-lived suite still launches from the NAS through `ahk-nas-wait.bat`.

```
startup.ahk                  <- ahk_boot task (copied local at setup time)
ahk-nas-wait.bat            <- local wrapper used by ahk_init / ahk_admin
script-compiler.ahk          <- ahk_init task (main entry point)
  #Include audio-monitor.ahk    Audio device monitoring
  #Include discord-ipc-mute.ahk Discord mute via IPC
  #Include hotkeys.ahk           Global hotkeys

ring-remap.ahk               <- ahk_admin task (standalone, needs admin)
startup-launcher.ahk          <- historical standalone startup hotkeys (folded into startup.ahk)
monitor-launcher.ahk          <- historical standalone monitor launcher (folded into startup.ahk)
```

> **Note**: Old copies still exist at `Documents/Obsidian/Personal-ENV/Scripts/ahk/` and under local ad-hoc Windows paths. Treat `Token-OS/ahk/` plus `Powershell/Setup-StartupTasks.ps1` as the canonical source.

---

## Third-Party Autostart (Registry / Installer-Managed)

Not managed by us. Includes: Steam, Discord, Docker Desktop, Spotify, Figma Agent, Medal, Stream Deck, Wispr Flow, Epic Games, NVIDIA Broadcast, Wooting, Tailscale.

---

## Boot Sequence

1. Windows logon
2. Task Scheduler fires all logon-triggered tasks (with respective delays)
3. **ahk_boot** starts immediately and opens Windows Terminal with `monitor` on the leftmost screen — this starts WSL Ubuntu and triggers systemd
4. WSL systemd starts **token-satellite** on `:7777`
5. `startup.ahk` waits for `token-satellite` health, then calls `POST /kvm/control {"action":"reload"}` to kick DeskFlow's phased recovery ladder instead of launching the old task directly. Mac KVM start/reload runs `Shell/deskflow-keymap-guard.sh` to preserve the Australian-input-source keymap fix.
6. `startup.ahk` launches **Bluetooth Audio Receiver** so the phone can route audio through the PC
7. `ahk_init`, `ahk_admin`, PowerToys, DMT, and Fast! start on their respective schedules


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
