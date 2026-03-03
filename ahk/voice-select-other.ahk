#Requires AutoHotkey v2.0
#SingleInstance Force

; One-shot: select "Other" in Claude Code AskUserQuestion prompt
; Called by token-satellite when voice chat is active
; AskUserQuestion shows 2 options + "Other" at bottom
; Down x2 -> Other, Enter -> select it

Sleep(300)          ; Wait for AskUserQuestion UI to render
Send("{Down 2}")    ; Move past 2 options to "Other"
Sleep(50)
Send("{Enter}")     ; Select "Other"
ExitApp
