#Requires AutoHotkey v2.0

#SingleInstance Off  ; Allow multiple scripts, but we handle uniqueness manually

SetCapsLockState "AlwaysOff"
SetScrollLockState "AlwaysOff"

PragmaOnce(scriptPath, hwnd) {
    DetectHiddenWindows True
    SetTitleMatchMode 3
    query := scriptPath " ahk_class AutoHotkey"
    ; Check if another instance of this specific script is already running
    try {
        if existingHwnd := WinExist(query) {
            ToolTip("Balls")
            ProcessClose(WinGetPID(existingHwnd))  ; Close the existing instance
            Sleep 100  ; Give it time to close
            PragmaOnce(scriptPath, hwnd)
            ToolTip()
        } else {
            WinSetTitle scriptPath, "ahk_id " hwnd
        }
    }
}
PragmaOnce(A_ScriptFullPath, A_ScriptHwnd)

#Include audio-monitor.ahk
#Include discord-ipc-mute.ahk
#Include scroll-lock.ahk
#Include *i dial-scroll.ahk  ; Antikater dial — enable when F13/F14 mapped
#Include *i private.ahk  ; Optional include - won't error if missing

^Up:: Send "{Up}{Up}{Up}"
^Down:: Send "{Down}{Down}{Down}"

^!r::Reload()
^!h::KeyHistory()

^!s::{
    Send("^+n")
    Sleep 1500
    Send("{F8}")
    Sleep 100
    Send("askcivic.com")
    Sleep 500
    Send("{Enter}")
    Sleep 500
    Send("^+i")
}

global tvConnected := false

^!t:: {  ; Ctrl+Alt+T - Toggle TV connection
    global tvConnected

    Send "#k"
    Sleep 800
    if (!tvConnected) {
	    Sleep 2000
        Send "{Tab}{Enter}"
        tvConnected := true
    } else {
        Send "{Tab}{Tab}{Enter}"
        tvConnected := false
    }
    Send "{esc}"
}

^!+s::{
    Send("^+n")
    Sleep 1500
    Send("{F8}")
    Sleep 100
    Send("dev.askcivic.com")
    Sleep 500
    Send("{Enter}")
    Sleep 500
    Send("^+i")
}

^!f::Send("Name three things in the Alabama administrative code that are abnormal in the nation. cite sources.")


^!o::{
    Send("!{f4}")
    Sleep 100
    Send("!{Space}")
    Sleep 100
    Send ".Obsidian{Enter}"
}


global mClickCount := 0
global mClickTimer := 0

MButton:: {
    global mClickCount, mClickTimer
    if (A_TickCount - mClickTimer > 2000) {
        mClickCount := 0
    }
    mClickCount++
    if (mClickCount == 1) {
        mClickTimer := A_TickCount
        Send("{MButton}")
    }
    ToolTip("click (" mClickCount "/3)")
    SetTimer(() => ToolTip(), -1500)
    if (mClickCount >= 3) {
        mClickCount := 0
        mClickTimer := 0
        ToolTip("ring!")
        SetTimer(() => ToolTip(), -1500)
        Run('schtasks /Run /TN "ahk_admin"',, "Hide")
    }
}

Media_Stop:: {  ; Pause/Play toggle TTS
    PostToTokenApi("/api/tts/control", '{"command":"toggle"}')
}

Media_Next:: {  ; Skip current TTS (play next in queue)
    PostToTokenApi("/api/tts/skip", "")
}

Media_Prev:: {  ; Restart current TTS message from beginning
    PostToTokenApi("/api/tts/control", '{"command":"restart"}')
}

^!w:: {                       ; Ctrl+Alt+W

    Run "ms-settings:mobile-devices"
    WinWaitActive "Settingt"

    Send "{Tab}"
    Sleep 100
    Send "{Tab}"
    Sleep 100
    Send "{Space}" ; focus the toggle, hit Space
}

^!+w:: {  ; Ctrl+Alt+Shift+W = Work Action (reset idle)
    PostToTokenApi("/api/work-action", "")
}

^!c:: {  ; Ctrl+Win+C = Copy clipboard → stash → Mac clipboard
    ToolTip("→ mac")
    try {
        RunWaitOutput('wsl.exe -d Ubuntu -e bash -lic "stash cp && ssh-mac \"stash paste\""')
        ToolTip("→ mac ✓")
    } catch {
        ToolTip("→ mac ✗")
    }
    SetTimer(() => ToolTip(), -1500)
}

^!v:: {  ; Ctrl+Alt+V = Fetch Mac clipboard → paste here
    ; Wootility sends #v to the same machine — swallow it before it hits Windows
    Hotkey "#v", (*) => "", "On"
    SetTimer(() => Hotkey("#v", "Off"), -500)
    ToolTip("← mac")
    try {
        RunWaitOutput('wsl.exe -d Ubuntu -e bash -lic "ssh-mac \"stash cp\" && stash paste"')
        ToolTip("← mac ✓")
        Sleep 100
        Send "^v"
    } catch {
        ToolTip("← mac ✗")
    }
    SetTimer(() => ToolTip(), -1500)
}

RunWaitOutput(cmd) {
    tmpFile := A_Temp "\stash_output.txt"
    RunWait(A_ComSpec ' /c ' cmd ' > "' tmpFile '" 2>&1',, "Hide")
    try {
        output := FileRead(tmpFile)
        FileDelete(tmpFile)
    } catch {
        output := "no output"
    }
    return Trim(output, "`n`r ")
}

; --- Focus-aware app switching ---
; Reads HWND from \\wsl.localhost\Ubuntu\tmp\.wt-focus-hwnd to identify the workspace terminal.
; Maximized terminal = unfocused (just activate). Snapped = focused (snap target left 50%).

GetWtHwnd() {
    try {
        hwnd := Trim(FileRead("\\wsl.localhost\Ubuntu\tmp\.wt-focus-hwnd"), "`n`r `t")
        return hwnd ? Integer(hwnd) : 0
    } catch
        return 0
}

GetFocusMode() {
    hwnd := GetWtHwnd()
    if !hwnd || !WinExist("ahk_id " hwnd)
        return "unfocused"
    return WinGetMinMax("ahk_id " hwnd) = 1 ? "unfocused" : "focused"
}

FocusApp(winTitle) {
    if !WinExist(winTitle)
        return
    if (GetFocusMode() = "focused") {
        WinRestore(winTitle)
        MonitorGetWorkArea(, &mL, &mT, &mR, &mB)
        WinMove(mL, mT, (mR - mL) // 2, mB - mT, winTitle)
    } else {
        WinMaximize(winTitle)
    }
    WinActivate(winTitle)
}

^!1:: FocusApp("ahk_exe Obsidian.exe")
^!2:: FocusApp("ahk_class Chrome_WidgetWin_1 ahk_exe vivaldi.exe")
^!3:: {  ; Terminal — always just activate, it stays snapped right
    hwnd := GetWtHwnd()
    if hwnd && WinExist("ahk_id " hwnd)
        WinActivate("ahk_id " hwnd)
    else
        WinActivate("ahk_class CASCADIA_HOSTING_WINDOW_CLASS")
}

; --- Wispr Flow dictation tracking ---
; Passthrough hotkey: Wispr still receives the keystroke, we just report state to Token-API
global dictationActive := false
~^#Space:: {
    global dictationActive
    dictationActive := !dictationActive
    active := dictationActive ? "true" : "false"
    PostToTokenApi("/api/dictation?active=" active, "")
}

