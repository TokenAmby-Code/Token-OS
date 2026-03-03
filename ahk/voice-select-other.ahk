#Requires AutoHotkey v2.0
#SingleInstance Force

; One-shot: select "Other" in Claude Code AskUserQuestion prompt
; Called via local_exec from generic-hook.sh when voice chat is active
; Strategy: Down x6 to ensure we're at the bottom (list doesn't wrap),
; then Up x1 to land on "Other" (always second from bottom, above "Chat about this")

Sleep(500)          ; Wait for AskUserQuestion UI to render
Send("{Down 6}")    ; Overshoot to bottom of list
Sleep(50)
Send("{Up 1}")      ; Back up one to "Other"
Sleep(50)
Send("{Enter}")     ; Select "Other"
ExitApp
