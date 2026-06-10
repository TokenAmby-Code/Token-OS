#Requires AutoHotkey v2.0
#SingleInstance Force

; ==================== STARTUP.AHK ====================
; Canonical Windows logon bootstrap.
; Copied locally by Powershell/Setup-StartupTasks.ps1 so the task does not
; depend on the NAS being mounted during early login.

global StartupRoot := EnvGet("USERPROFILE") "\Imperium-Startup"
global StartupTimerSeconds := 10

LaunchLocalPowerShell(scriptName, arguments := "") {
    global StartupRoot
    scriptPath := StartupRoot "\" scriptName
    if !FileExist(scriptPath) {
        return false
    }

    cmd := 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' scriptPath '"'
    if (arguments != "") {
        cmd .= " " arguments
    }
    Run(cmd,, "Hide")
    return true
}

BootWslHeadless() {
    ; Kick WSL awake headlessly so systemd boots and starts token-satellite.
    ; The deprecated `wt.exe ... monitor` TUI surface is gone; booting WSL was
    ; the only function it served. `-e true` runs a trivial command purely to
    ; trigger distro boot. Invoke-DeskflowBoot.ps1 (below) absorbs systemd
    ; warm-up via its 20s delay / 180s health-timeout before kicking the Mac.
    Run('wsl.exe -d Ubuntu -e true', , "Hide")
    return true
}

ExitStartup() {
    TrayTip "Startup Launcher Exiting", "Normal key behavior restored", 1
    Sleep 1500
    ExitApp
}

; 1. Boot WSL headlessly so systemd brings up token-satellite.
BootWslHeadless()

; 2. Kick the Deskflow phased restart through token-satellite once WSL is up.
LaunchLocalPowerShell("Invoke-DeskflowBoot.ps1", "-DelaySeconds 20 -HealthTimeoutSeconds 180")

; 3. Launch Bluetooth Audio Receiver after login settles.
LaunchLocalPowerShell("Start-BluetoothAudioReceiver.ps1", "-DelaySeconds 35")

; 4. Keep the short-lived startup hotkeys.
TrayTip "Startup Mode Active", "V=Vivaldi S=Spotify O=Obsidian`nC=Cursor U=Ubuntu B=Brave`n`nAuto-exits in " StartupTimerSeconds "s", 1
SetTimer ExitStartup, StartupTimerSeconds * 1000 * -1

Escape::ExitApp

v:: Run "C:\Users\colby\AppData\Local\Vivaldi\Application\vivaldi.exe"
s:: Run "C:\Users\colby\AppData\Roaming\Spotify\Spotify.exe"
o:: Run "C:\Users\colby\AppData\Local\Programs\Obsidian\Obsidian.exe"
c:: Run "C:\Users\colby\AppData\Local\Programs\cursor\Cursor.exe"
u:: Run "wt.exe"
b:: Run "C:\Users\colby\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
