#Requires AutoHotkey v2.0
#Include ..\..\helper.ahk

obs_manage(input) {
    switch (input) {
        case 7: obs_workspace()
        case 8:
            ; SetTitleMatchMode "2"
            ; if WinExist("Obsidian") {
            ;     WinClose "Obsidian"
            ; }
            ; Sleep 500
            ; Send "!{Space}"
            ; Sleep 100
            ; Send ".Obsidian{Enter}"
            ; Run "C:\Users\colby\AppData\Local\Programs\obsidian\Obsidian.exe",, "Max"
        case 9:
            ToolTip("Delete?")
            if KeyWaitNum() == 9 {
                Run("obsidian://adv-uri?vault=Personal-ENV&commandid=app%3Adelete-file")
            }
            ToolTip()
    }
}

obs_workspace() {
    ToolTip("Select Workspace")
    input := KeyWaitNum()
    wksp := wkspmap[input]
    ToolTip(wksp)
    RunTP('user.wksp', [wksp])
    ToolTip()
}