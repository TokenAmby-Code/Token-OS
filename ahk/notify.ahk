#Requires AutoHotkey v2.0
; notify.ahk — Show a Windows TrayTip notification then exit.
; Usage: AutoHotkey.exe notify.ahk "Title" "Message"

title := A_Args.Has(1) ? A_Args[1] : "Imperium"
msg   := A_Args.Has(2) ? A_Args[2] : ""

TrayTip(msg, title, 1)
Sleep(3000)
ExitApp
