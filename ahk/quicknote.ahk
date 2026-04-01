#Requires AutoHotkey v2.0

; Simplified quick note creation for Imperium-ENV
; Pure URI triggers - no logic in AHK

; Quick note creation - invoke QuickAdd commands directly
^!p::Run('obsidian://advanced-uri?vault=Imperium-ENV&commandid=quickadd%3Achoice%3Aquick-prescriptive',, 'Hide')  ; Ctrl+Alt+P - Prescriptive
^!r::Run('obsidian://advanced-uri?vault=Imperium-ENV&commandid=quickadd%3Achoice%3Aquick-descriptive',, 'Hide')   ; Ctrl+Alt+R - Descriptive (Reference)

; Daily note
^!d::Run('obsidian://advanced-uri?vault=Imperium-ENV&commandid=daily-notes%3Aopen-today',, 'Hide')  ; Ctrl+Alt+D - Daily note

; Workspace switching (kept for utility)
^!1::Run('obsidian://advanced-uri?vault=Imperium-ENV&workspace=Work',, 'Hide')
^!2::Run('obsidian://advanced-uri?vault=Imperium-ENV&workspace=Personal',, 'Hide')
^!3::Run('obsidian://advanced-uri?vault=Imperium-ENV&workspace=Meta',, 'Hide')
^!4::Run('obsidian://advanced-uri?vault=Imperium-ENV&workspace=Inbox',, 'Hide')
