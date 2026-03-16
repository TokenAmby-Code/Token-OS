#Requires AutoHotkey v2.0
#Include ..\..\helper.ahk
;  #include ..\..\runjs.ahk
;  #include ..\numroot.ahk
;  #SingleInstance Force
;  global laptopState := true
obs_create(input) {
    switch (input) {
        case 7: Obs_Note()
    }
}

Obs_Note(input := false) {
    NoteType := ""
    NoteLevel := ""
    Relativity := ""
    Tags := []
    
    ; Create a more detailed tooltip function
    ; UpdateTooltip(NoteLevel, NoteType, Relativity, Tags) {
    ;     tagString := ""
    ;     for tag in Tags {
    ;         tagString .= tag " "
    ;     }
        
    ;     levelDesc := NoteLevel == "COT" ? "Chain of Thought" : 
    ;                 NoteLevel == "IMP" ? "Implementation" : 
    ;                 NoteLevel == "ABST" ? "Abstraction" : "Undefined"
                    
    ;     typeDesc := NoteType == "NOTE" ? "Standard Note" : 
    ;                NoteType == "GOAL" ? "Goal" : 
    ;                NoteType == "WILD" ? "Wild Idea" : 
    ;                NoteType == "SOURCE" ? "Source" : "Undefined"
                   
    ;     relDesc := Relativity == "CHILD" ? "Child of Current Note" : 
    ;               Relativity == "WKSP" ? "In Current Workspace" : 
    ;               Relativity == "ABSO" ? "In Absolute Location" : "Undefined"
        
    ;     ToolTip("Creating: " levelDesc " | " typeDesc " | " relDesc "`nTags: " tagString)
    ; }
    
    ; Initial tooltip
    if (!input) {
        ToolTip("Obs Note - Select options with numpad")
        input := KeyWaitNum()
    }
    while (input != "Enter") {
        switch (input) {
            ; Note Levels
            case 7:
                NoteLevel := "COT"
            case 8:
                NoteLevel := "IMP"
            case 9:
                NoteLevel := "ABST"
    
            ; Note Types
            case 4:
                NoteType := "NOTE"
            case 5:
                NoteType := "GOAL"
            case 6:
                NoteType := "WILD"
            case "Dot":
                NoteType := "SOURCE"
    
            ; Relativity
            case 1:
                Relativity := "CHILD"
            case 2:
                Relativity := "WKSP"
            case 3:
                Relativity := "ABSO:FREMEN"
                ToolTip("Workspace?")
                input := KeyWaitNum()
                if IsInteger(input) {
                    wksp := wkspmap[input]
                    Relativity := "ABSO:" wksp
                }
                
    
            ; Tags
            case "Add":
                ToggleValue(Tags, "QUICK")
            ; case "Sub":
            ;     if !HasValue(Tags, "WEB")
            ;         Tags.Push("WEB")
            ; case 0:
            ;     if !HasValue(Tags, "REFERENCE")
                    ; Tags.Push("REFERENCE")
            default: 
                ToolTip("Unrecognized Input " input)
                Sleep 500
        }
        
        ; UpdateTooltip(NoteLevel, NoteType, Relativity, Tags)
        tagStr := ''
        for tag in Tags {
            tagStr := tagStr ' #' tag
        }

        ToolTip(NoteLevel '|' NoteType '|' Relativity tagStr)
        input := KeyWaitNum()
    }
    
    ; Clear tooltip
    ToolTip()
    
    ; Ensure required fields are set
    if (NoteLevel == "" || NoteType == "" || Relativity == "") {
        if (!NoteLevel) {
            NoteLevel := "COT"
        }
        if (!NoteType) {
            NoteType := "NOTE"
        }
        if (!Relativity) {
            if (NoteLevel == "COT") {
                Relativity := "CHILD"
            } else {
                Relativity := "WKSP"
            }
        }
    }
    
    ; Prepare arguments for JavaScript function
    args := [NoteLevel, NoteType, Relativity]
    for tag in Tags {
        args.Push(tag)
    }
    
    ; Call JavaScript function
    RunTP('user.note', args)
}

; Helper function to check if array contains value
ToggleValue(arr, val) {
    i := 1
    for item in arr {
        if (item == val) {
            arr.RemoveAt(i)
            return
        }
        i++
    }
    arr.Push(val)
}
; Obs_Note(7)
; obs_create("7")