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

LaunchMonitorWindow() {
    SetTitleMatchMode 2  ; Partial title match

    ; Launch Windows Terminal minimized to avoid flash on the main monitor.
    ; IMPERIUM_NO_TMUX=1 exported before sourcing .bashrc prevents auto-attach.
    Run('wt.exe --title "token-monitor" -p "Ubuntu" -- wsl.exe -d Ubuntu -e env IMPERIUM_NO_TMUX=1 bash -lic monitor', , "Min")

    hwnd := 0
    Loop 3 {
        if !WinWait("token-monitor ahk_exe WindowsTerminal.exe",, 15) {
            TrayTip "Startup Bootstrap", "Monitor window did not appear within 15s", 3
            Sleep 2000
            return false
        }

        hwnd := WinExist()
        Sleep 1000

        ; Windows Terminal may recycle the hwnd during startup.
        try {
            WinGetTitle("ahk_id " hwnd)
            break
        } catch {
            hwnd := 0
            continue
        }
    }

    if (!hwnd) {
        TrayTip "Startup Bootstrap", "Monitor window handle kept going stale", 3
        Sleep 2000
        return false
    }

    leftMon := 1
    leftX := 99999
    Loop MonitorGetCount() {
        MonitorGetWorkArea(A_Index, &l)
        if (l < leftX) {
            leftX := l
            leftMon := A_Index
        }
    }

    try {
        MonitorGetWorkArea(leftMon, &mL, &mT, &mR, &mB)
        WinRestore("ahk_id " hwnd)
        Sleep 50
        WinMove(mL, mT, mR - mL, mB - mT, "ahk_id " hwnd)
        Sleep 50
        WinMaximize("ahk_id " hwnd)
    }

    return true
}

ExitStartup() {
    TrayTip "Startup Launcher Exiting", "Normal key behavior restored", 1
    Sleep 1500
    ExitApp
}

; 1. Start the WSL monitor surface immediately.
LaunchMonitorWindow()

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
