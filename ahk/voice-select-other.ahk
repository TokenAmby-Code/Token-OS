#Requires AutoHotkey v2.0
#SingleInstance Force

; Stateless voice chat executor. Receives state via CLI args from Token API.
; Called via local_exec from generic-hook.sh when voice chat is active.

; CLI args: instance_id, listening (1/0)
INSTANCE_ID := A_Args.Has(1) ? A_Args[1] : ""
listening := A_Args.Has(2) ? (A_Args[2] = "1") : true
API_BASE := "http://100.95.109.23:7777"

; --- Notify Token API of listening changes ---
NotifyListening(state) {
    global INSTANCE_ID, API_BASE
    if (!INSTANCE_ID)
        return
    active := state ? "true" : "false"
    Run('wsl.exe curl -s -X POST "' API_BASE '/api/instances/' INSTANCE_ID '/voice-chat/listening?active=' active '"', , "Hide")
}

; --- Wispr control (hold pattern) ---
WisprOff() {
    Send("{LCtrl down}{LWin down}")
    Sleep(250)
    Send("{Space}{LWin up}{LCtrl up}")
}

WisprOn() {
    Send("{LCtrl down}{LWin down}")
    Sleep(250)
    Send("{Space}{LWin up}{LCtrl up}")
}

EnsureListeningOff() {
    global listening
    if (!listening)
        return
    WisprOff()
    listening := false
    NotifyListening(false)
}

EnsureListeningOn() {
    global listening
    if (listening)
        return
    WisprOn()
    listening := true
    NotifyListening(true)
}

; --- Intercept manual Wispr toggle (passthrough, just track + notify) ---
WisprToggle(ThisHotkey) {
    global listening
    listening := !listening
    NotifyListening(listening)
    UpdateTooltip()
}
Hotkey "~^#Space", WisprToggle

; --- Ctrl+Alt+R: Full voice mode restart ---
VoiceRestart(ThisHotkey) {
    global INSTANCE_ID, API_BASE
    if (INSTANCE_ID) {
        Run('wsl.exe curl -s -X POST "' API_BASE '/api/instances/' INSTANCE_ID '/voice-chat?active=false"', , "Hide")
    }
    ExitApp
}
Hotkey "^!r", VoiceRestart

; --- Enter handler (starts disabled) ---
VoiceSubmit(ThisHotkey) {
    EnsureListeningOff()
    Sleep(1500)
    Send("{Enter}")
    Hotkey "$Enter", "Off"
    Hotkey "$+Enter", "Off"
    Sleep(1000)
    EnsureListeningOn()
}

NormalEnter(ThisHotkey) {
    Send("{Enter}")
}

Hotkey "$Enter", VoiceSubmit, "Off"
Hotkey "$+Enter", NormalEnter, "Off"

; --- Navigate to "Other" box ---
if WinExist("ahk_exe WindowsTerminal.exe")
    WinActivate

Sleep(500)
Send("{Down 6}")
Sleep(50)
Send("{Up 1}")

Hotkey "$Enter", "On"
Hotkey "$+Enter", "On"
