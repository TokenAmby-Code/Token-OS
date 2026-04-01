#Requires AutoHotkey v2.0

; Simplified workspace map for Imperium-ENV
wkspmap := Map()
wkspmap.CaseSense := false
wkspmap.Set(
    1, "Work",
    2, "Personal",
    3, "Meta",
    4, "Inbox"
)

; RunWaitOne(command) {
;     DetectHiddenWindows(1)
;     Run(A_ComSpec,, "Hide", &pid)
;     WinWait("ahk_pid" pid)
;     DllCall("AttachConsole", "UInt", pid)
  
;     shell := ComObject("WScript.Shell")
;     exec := shell.Exec(A_ComSpec " /C " command)
  
;     DllCall("FreeConsole")
;     return exec.StdOut.ReadAll() 
; } 