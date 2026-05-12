#Requires AutoHotkey v2.0
#SingleInstance Force

; Raise hotkey rate-limit ceiling — see script-compiler.ahk for rationale
A_MaxHotkeysPerInterval := 200
A_HotkeyInterval := 1000

; Voice chat executor. Called via local_exec from generic-hook.sh when voice chat is active.
; Uses explicit on/off for Wispr control via Token-API dictation state (no toggle guessing).

; CLI args: instance_id
INSTANCE_ID := A_Args.Has(1) ? A_Args[1] : ""
API_BASE := "http://100.95.109.23:7777"

; --- Token-API HTTP helpers (synchronous WinHttp) ---
PostToApi(endpoint, body := "") {
    global API_BASE
    try {
        http := ComObject("WinHttp.WinHttpRequest.5.1")
        http.Open("POST", API_BASE . endpoint, false)
        http.SetRequestHeader("Content-Type", "application/json")
        http.Send(body)
        return {success: true, status: http.Status, body: http.ResponseText}
    } catch as err {
        return {success: false, status: 0, body: err.Message}
    }
}

GetFromApi(endpoint) {
    global API_BASE
    try {
        http := ComObject("WinHttp.WinHttpRequest.5.1")
        http.Open("GET", API_BASE . endpoint, false)
        http.Send()
        return {success: true, status: http.Status, body: http.ResponseText}
    } catch as err {
        return {success: false, status: 0, body: err.Message}
    }
}

; --- Check current dictation state from Token-API ---
IsDictationActive() {
    resp := GetFromApi("/api/dictation")
    if (!resp.success)
        return false
    ; Parse: {"active": true, ...}
    return InStr(resp.body, '"active": true')
}

; --- Explicit Wispr control: only acts if state needs to change ---
SendWisprToggle() {
    Send("{LCtrl down}{LWin down}")
    Sleep(250)
    Send("{Space}{LWin up}{LCtrl up}")
}

WisprOff() {
    if (!IsDictationActive())
        return  ; Already off, no-op
    SendWisprToggle()
    PostToApi("/api/dictation?active=false")
}

WisprOn() {
    if (IsDictationActive())
        return  ; Already on, no-op
    SendWisprToggle()
    PostToApi("/api/dictation?active=true")
}

; --- Intercept manual Wispr toggle (passthrough, report to Token-API) ---
WisprToggle(ThisHotkey) {
    ; The keystroke passes through (~), so Wispr toggles. We report the new state.
    ; Read current state and report the opposite (since the toggle just happened).
    wasActive := IsDictationActive()
    PostToApi("/api/dictation?active=" . (wasActive ? "false" : "true"))
}
Hotkey "~^#Space", WisprToggle

; --- Ctrl+Alt+R: Full voice mode restart ---
VoiceRestart(ThisHotkey) {
    global INSTANCE_ID, API_BASE
    if (INSTANCE_ID) {
        PostToApi("/api/instances/" INSTANCE_ID "/voice-chat?active=false")
    }
    ExitApp
}
Hotkey "^!r", VoiceRestart

; --- Enter handler (starts disabled) ---
VoiceSubmit(ThisHotkey) {
    WisprOff()
    Sleep(1500)
    Send("{Enter}")
    Hotkey "$Enter", "Off"
    Hotkey "$+Enter", "Off"
    Sleep(1000)
    WisprOn()
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
