#Requires AutoHotkey v2.0
#SingleInstance Force

; Voice chat mode: navigates to "Other" on each AskUserQuestion,
; enables Enter remap for dictation submit, disables after submit.
; Called via local_exec from generic-hook.sh when voice chat is active.

; --- Define the Enter handler (starts disabled) ---
VoiceSubmit(ThisHotkey) {
    ; Stop Wispr dictation
    Send("{LCtrl down}{LWin down}")
    Sleep(250)
    Send("{Space}{LWin up}{LCtrl up}")
    Sleep(1500)         ; Wait for Wispr to process and paste text
    Send("{Enter}")     ; Submit the form
    ; Disable ourselves — no longer in AskUserQuestion
    Hotkey "$Enter", "Off"
    Sleep(1000)         ; Wait for Claude to start processing
    ; Restart dictation
    Send("{LCtrl down}{LWin down}")
    Sleep(250)
    Send("{Space}{LWin up}{LCtrl up}")
}

; Register hotkey disabled, then enable after navigation
Hotkey "$Enter", VoiceSubmit, "Off"

; --- Navigate to "Other" box ---
if WinExist("ahk_exe WindowsTerminal.exe")
    WinActivate

Sleep(500)
Send("{Down 6}")    ; Overshoot to bottom ("Chat about this")
Sleep(50)
Send("{Up 1}")      ; Back up one to "Other" (type-something input)

; Enable Enter remap now that we're in AskUserQuestion
Hotkey "$Enter", "On"
