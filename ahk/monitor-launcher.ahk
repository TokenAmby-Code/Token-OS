#Requires AutoHotkey v2.0
#SingleInstance Force

; ==================== MONITOR LAUNCHER ====================
; Launches Windows Terminal with `monitor` TUI on the leftmost monitor
; Trigger: Task Scheduler on logon, or manually: schtasks /Run /TN "MonitorLauncher"

SetTitleMatchMode 2  ; Partial title match

; Launch Windows Terminal minimized to avoid flash on main monitor
; IMPERIUM_NO_TMUX=1 exported before sourcing .bashrc prevents auto-attach
; monitor() handles its own tmux session creation + attachment to main:tui
Run('wt.exe --title "token-monitor" -p "Ubuntu" -- wsl.exe -d Ubuntu -e env IMPERIUM_NO_TMUX=1 bash -lic monitor', , "Min")

; Wait for window — Windows Terminal may recycle the hwnd during startup
; so we retry a few times if the handle goes stale
hwnd := 0
Loop 3 {
    if !WinWait("token-monitor ahk_exe WindowsTerminal.exe",, 15) {
        TrayTip "Monitor Launcher", "Window didn't appear within 15s", 3
        Sleep 2000
        ExitApp
    }
    hwnd := WinExist()
    Sleep 1000  ; Let WT finish its window setup

    ; Verify the hwnd is still valid
    try {
        WinGetTitle("ahk_id " hwnd)
        break  ; hwnd is good
    } catch {
        hwnd := 0
        continue  ; WT recycled the window, retry
    }
}

if (!hwnd) {
    TrayTip "Monitor Launcher", "Window handle kept going stale", 3
    Sleep 2000
    ExitApp
}

; Find the leftmost monitor
leftMon := 1
leftX := 99999
Loop MonitorGetCount() {
    MonitorGetWorkArea(A_Index, &l)
    if (l < leftX) {
        leftX := l
        leftMon := A_Index
    }
}

; Move to leftmost monitor while still minimized, then maximize in place
try {
    MonitorGetWorkArea(leftMon, &mL, &mT, &mR, &mB)
    WinRestore("ahk_id " hwnd)
    Sleep 50
    WinMove(mL, mT, mR - mL, mB - mT, "ahk_id " hwnd)
    Sleep 50
    WinMaximize("ahk_id " hwnd)
}

ExitApp
