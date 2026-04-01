# Startup Audit (All Devices)

> Last updated: 2026-02-22

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
6. Windows PC boots → Deskflow KVM connects → `POST /api/kvm/start` signals Mac

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
| 1 | ~~**WSL Keep-Alive**~~ | Limited | 5s | **DISABLED 2026-02-19** — `wsl.exe -d Ubuntu -- bash -lc "exec sleep infinity"` — kept WSL alive but opened a visible cmd.exe/conhost window. Redundant now that MonitorLauncher (task #9) keeps WSL running via Windows Terminal |
| 2 | **Deskflow** | Limited | 3s | Starts Deskflow KVM minimized, waits 10s, then curls Mac token-api (`POST http://100.95.109.23:7777/api/kvm/start`) to signal KVM is ready |
| 3 | **ahk_init** | Limited | — | Runs `script-compiler.ahk` — the main AHK suite (see AHK Architecture below) |
| 4 | **ahk_admin** | Highest | — | Runs `ring-remap.ahk` — Bluetooth ring button remapping via AutoHotInterception (needs admin for driver access) |
| 5 | **AHK startup mode** | Limited | — | Runs `startup-launcher.ahk` — 10-second quick app launcher (V=Vivaldi, S=Spotify, etc.) |
| 6 | **fast_task** | Highest | — | Starts Fast! app (`C:\Program Files (x86)\Fast!\fast!.exe`) |
| 7 | **Dual Monitor Tools** | Limited | — | Starts DMT.exe for multi-monitor management |
| 8 | **Autorun for colby** | Limited | 3s | Starts PowerToys |
| 9 | **MonitorLauncher** | Limited | 15s | Runs `monitor-launcher.ahk` — launches Windows Terminal with `monitor` TUI on leftmost monitor. Creates grouped session `monitor` for independent window viewing. TUI runs via `tui-pane-guard` with auto-restart lifecycle. |

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

All AHK scripts live at `/Volumes/Imperium/Scripts/ahk/` (accessed from Windows via `\\Token-NAS\Imperium\Scripts\ahk\`).

```
script-compiler.ahk          <- ahk_init task (main entry point)
  #Include audio-monitor.ahk    Audio device monitoring
  #Include discord-ipc-mute.ahk Discord mute via IPC
  #Include hotkeys.ahk           Global hotkeys

ring-remap.ahk               <- ahk_admin task (standalone, needs admin)
startup-launcher.ahk          <- AHK startup mode task (standalone)
monitor-launcher.ahk          <- MonitorLauncher task (standalone, 15s delay)
```

> **Note**: Old copies exist at `Documents/Obsidian/Personal-ENV/Scripts/ahk/` — these are superseded by `/Volumes/Imperium/Scripts/ahk/` but kept for historical reference in the Obsidian vault.

---

## Third-Party Autostart (Registry / Installer-Managed)

Not managed by us. Includes: Steam, Discord, Docker Desktop, Spotify, Figma Agent, Medal, Stream Deck, Wispr Flow, Epic Games, NVIDIA Broadcast, Wooting, Tailscale.

---

## Boot Sequence

1. Windows logon
2. Task Scheduler fires all logon-triggered tasks (with respective delays)
3. **Deskflow** (3s delay) starts KVM, then notifies Mac token-api after 10s
4. AHK scripts, PowerToys, DMT, Fast! all start in parallel
5. **MonitorLauncher** (15s delay) opens Windows Terminal with `monitor` TUI on leftmost screen — creates grouped session `monitor` on `main` for independent window viewing. TUI auto-restarts on crash via `tui-pane-guard`. This starts WSL Ubuntu and triggers systemd
6. systemd starts **token-satellite** on `:7777`

---

## How to Add New Startup Items

**Preferred method: Task Scheduler** — all custom startup items should use Task Scheduler for consistency.

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
