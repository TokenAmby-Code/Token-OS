#Requires AutoHotkey v2.0


RunTP(cmdName, args := []) {
    ; Construct the full path to the JS file
    tpFunc := "app.plugins.plugins['templater-obsidian'].templater.current_functions_object." cmdName
    argBuf := ''
    i := 0
    for arg in args {
        argBuf := argBuf (i==0 ? '' : ', ')
        i++
        if IsInteger(arg) {
            argBuf := argBuf arg
        } else if IsObject(arg) {
            argBuf := argBuf '{'
            j := 0
            for key, value in arg {
                argBuf := argBuf (j==0 ? '' : ', ')
                j++
                argBuf := argBuf '%22' key '%22: %22' value '%22'
            }
            argBuf := argBuf '}'
        } else if RegExMatch(arg, "^\{.*:.*\}$") {
            content := RegExReplace(SubStr(arg, 2, StrLen(arg)-2), "[\s'`"]", "") ; remove whitespace, quotes and brackets
            pairs := StrSplit(content, ',')
            argBuf := argBuf '{'
            j := 0
            for pair in pairs
                pairbuf := pair
                argBuf := argBuf (j==0 ? '' : ', ')
                j++
                kv := StrSplit(pairbuf, ':')
                argBuf := argBuf '%22' kv[1] '%22: %22' kv[2] '%22'
            argBuf := argBuf '}'
        } else {
            arg := RegExReplace(arg, "[\s'`"]", "")
            argBuf := argBuf '%22' arg '%22'
        }  
    }
    try {
        Run('obsidian://advanced-uri?vault=Imperium-ENV&eval=' tpFunc '(' argBuf ');',, "Hide")
    } catch as err {
        MsgBox "Error running JavaScript: " err.Message
    }
}