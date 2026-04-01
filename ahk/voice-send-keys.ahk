#Requires AutoHotkey v2.0
#SingleInstance Force

; voice-send-keys.ahk — Voice dictation via tmux send-keys
;
; Simple: on each Wispr dictation stop, grab transcript via !+z
; and write it directly to the target tmux pane. The terminal
; prompt bar is the buffer — user sees text as it arrives,
; normal Enter submits.
;
; Usage:
;   voice-send-keys.ahk <tmux_pane>

; --- Logging ---
LOG_FILE := A_Temp . "\voice-send-keys.log"

Log(msg) {
    global LOG_FILE
    try {
        timestamp := FormatTime(, "HH:mm:ss")
        FileAppend(timestamp . " " . msg . "`n", LOG_FILE)
    }
}

try FileDelete(LOG_FILE)

; --- Args ---
PANE_TARGET := A_Args.Has(1) ? A_Args[1] : "%0"

API_BASE := "http://100.95.109.23:7777"

LAST_UPDATED_AT := ""
LAST_GRABBED_TRANSCRIPT := ""

Log("=== voice-send-keys started ===")
Log("PANE_TARGET: " . PANE_TARGET)

; --- WSL command helpers ---
RunWSL(cmd) {
    Log("RunWSL: " . cmd)
    try {
        Run('wsl ' . cmd,, "Hide")
        return true
    } catch as err {
        Log("RunWSL ERROR: " . err.Message)
        return false
    }
}

RunWSLWait(cmd) {
    Log("RunWSLWait: " . cmd)
    try {
        RunWait('wsl ' . cmd,, "Hide")
        return true
    } catch as err {
        Log("RunWSLWait ERROR: " . err.Message)
        return false
    }
}

; --- Token-API helper ---
GetFromApi(endpoint) {
    global API_BASE
    try {
        http := ComObject("WinHttp.WinHttpRequest.5.1")
        http.Open("GET", API_BASE . endpoint, false)
        http.SetRequestHeader("Content-Type", "application/json")
        http.Send()
        return {success: true, body: http.ResponseText}
    } catch as err {
        return {success: false, body: err.Message}
    }
}

; --- Send text to tmux pane ---
TmuxSendLiteral(text) {
    global PANE_TARGET
    escaped := StrReplace(text, "'", "'\''" )
    Log("TmuxSendLiteral: [" . SubStr(text, 1, 80) . "]")
    RunWSLWait('tmux send-keys -t "' . PANE_TARGET . '" -l ' . "'" . escaped . "'")
}

; --- Grab Wispr's last transcript via Alt+Shift+Z ---
GrabWisprTranscript() {
    Log("GrabWisprTranscript: starting")

    grabGui := Gui("+AlwaysOnTop -Caption +ToolWindow", "WisprGrab")
    grabEdit := grabGui.Add("Edit", "w600 h200 vGrabText")
    grabGui.Show("w620 h210 x-9999 y-9999")  ; Off-screen
    grabEdit.Focus()
    Sleep(150)

    Log("GrabWisprTranscript: sending !+z")
    Send("!+z")
    Sleep(1500)  ; Wait for Wispr to finish typing

    transcript := grabEdit.Value
    Log("GrabWisprTranscript: got [" . SubStr(transcript, 1, 80) . "] (" . StrLen(transcript) . " chars)")

    grabGui.Destroy()
    return Trim(transcript)
}

; --- Poll dictation state, write to pane on each stop ---
CheckDictationState() {
    global API_BASE, LAST_UPDATED_AT, LAST_GRABBED_TRANSCRIPT

    resp := GetFromApi("/api/dictation")
    if (!resp.success)
        return

    body := resp.body
    currentlyActive := InStr(body, '"active": true') ? true : false

    ; Extract updated_at
    updatedAt := ""
    pos := InStr(body, '"updated_at"')
    if (pos) {
        startQuote := InStr(body, '"',, pos + 13)
        endQuote := InStr(body, '"',, startQuote + 1)
        if (startQuote && endQuote)
            updatedAt := SubStr(body, startQuote + 1, endQuote - startQuote - 1)
    }

    static pollCount := 0
    pollCount++
    if (Mod(pollCount, 60) = 0)  ; Log every 30 seconds
        Log("Poll #" . pollCount . ": active=" . (currentlyActive ? "true" : "false"))

    ; Dictation just stopped — grab and send to pane immediately
    if (!currentlyActive && updatedAt != LAST_UPDATED_AT && LAST_UPDATED_AT != "") {
        Log("Poll: dictation stopped, grabbing transcript")
        LAST_UPDATED_AT := updatedAt
        Sleep(300)
        transcript := GrabWisprTranscript()
        if (StrLen(transcript) > 0) {
            if (transcript = LAST_GRABBED_TRANSCRIPT) {
                Log("Poll: duplicate, skipping")
            } else {
                LAST_GRABBED_TRANSCRIPT := transcript
                ; Add a space before appending if the pane likely has text already
                TmuxSendLiteral(transcript)
                Log("Poll: sent to pane")
            }
        } else {
            Log("Poll: empty transcript")
        }
    } else if (updatedAt != LAST_UPDATED_AT) {
        LAST_UPDATED_AT := updatedAt
    }
}

SetTimer(CheckDictationState, 500)

; Seed initial state
initResp := GetFromApi("/api/dictation")
if (initResp.success) {
    pos := InStr(initResp.body, '"updated_at"')
    if (pos) {
        sq := InStr(initResp.body, '"',, pos + 13)
        eq := InStr(initResp.body, '"',, sq + 1)
        if (sq && eq)
            LAST_UPDATED_AT := SubStr(initResp.body, sq + 1, eq - sq - 1)
    }
}
Log("Initial updated_at: " . LAST_UPDATED_AT)

; --- Ctrl+Alt+R: Exit ---
VoiceExit(ThisHotkey) {
    Log("VoiceExit: Ctrl+Alt+R")
    ExitApp
}
Hotkey "^!r", VoiceExit

Log("=== Ready ===")
Persistent
