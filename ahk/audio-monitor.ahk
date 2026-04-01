#Requires AutoHotkey v2.0

; ========================================
; Audio Monitor for Timer Automation
; ========================================
; Monitors audio playback state and automatically triggers
; Obsidian timer modes via Advanced URI
;
; Detection Logic:
; - Spotify playing → Music mode (timer-work-music)
; - YouTube in browser → Video mode (timer-work-video)
; - Neither → Silence mode (timer-work-silence)
;
; Phase 1: Simple window detection
; Phase 2: COM-based audio session detection (future)

; ========================================
; Configuration
; ========================================

global AM_CONFIG := {
    vaultName: "Imperium-ENV",
    pollingInterval: 5000,          ; Check every 5 seconds
    debounceCount: 2,                ; Require 2 consistent readings before switching
    manualLockDuration: 30,          ; Minutes to lock mode when manually set
    showNotifications: true,         ; Show notifications on mode change
    logToFile: true,                 ; Enable file logging for debugging
    logPath: A_ScriptDir . "\audio-monitor.log",

    ; token-api integration (authoritative for all decisions)
    ; token-api handles: mode changes, window enforcement, productivity tracking
    ; Enforcement is PUSH-BASED: token-api closes windows directly when needed
    tokenApiUrl: "http://100.95.109.23:7777"
}

; ========================================
; State Management
; ========================================

global AM_STATE := {
    currentMode: "silence",          ; Current active mode
    detectedMode: "silence",         ; Last detected mode
    pendingMode: "",                 ; Mode waiting for confirmation
    confirmCount: 0,                 ; Consecutive detections of pending mode
    isLocked: false,                 ; Manual lock status
    lockUntil: 0,                    ; Timestamp when lock expires
    lastTrigger: 0,                  ; Last time we triggered a mode change
    lastWindowTitle: "",             ; Last detected window title (for token-api)
    lastBlockNotify: 0,              ; Timestamp of last block notification
    blockNotifyCooldown: 60000,      ; 60 second cooldown for block notifications
    pollCount: 0                     ; Poll counter for heartbeat interval
}

; ========================================
; Mode Definitions
; ========================================

global AM_MODES := Map(
    "silence", {
        name: "Silence",
        icon: "🔇",
        command: "timer-auto-work-silence",
        description: "No audio detected"
    },
    "music", {
        name: "Music",
        icon: "🎵",
        command: "timer-auto-work-music",
        description: "Spotify playing"
    },
    "video", {
        name: "Video",
        icon: "📺",
        command: "timer-auto-work-video",
        description: "YouTube playing"
    },
    "gaming", {
        name: "Gaming",
        icon: "🎮",
        command: "timer-auto-work-gaming",
        description: "Minecraft (Lucky World Invasion) detected"
    },
    "meeting", {
        name: "Meeting",
        icon: "📞",
        command: "timer-auto-work-meeting",
        description: "Zoom or Google Meet active"
    }
)

; ========================================
; Main Detection Logic
; ========================================

DetectAudioState() {
    ; Check if we're in manual lock mode
    if (AM_STATE.isLocked) {
        if (A_TickCount < AM_STATE.lockUntil) {
            return AM_STATE.currentMode  ; Stay in locked mode
        } else {
            AM_STATE.isLocked := false
            LogMessage("Manual lock expired, resuming auto-detection")
        }
    }

    ; Detection Priority:
    ; 1. Meeting (Zoom process or Google Meet in Brave) → Meeting
    ; 2. Minecraft window title (Lucky World Invasion) → Gaming
    ; 3. Spotify (if running) → Music
    ; 4. YouTube in browser → Video
    ; 5. Nothing detected → Silence

    ; Check for Zoom meeting
    ; Zoom creates dozens of internal windows (ConfPopupTop, ActiveMovie Window,
    ; more menu, video_preview_fit_panel, etc.) — blacklisting is whack-a-mole.
    ; Whitelist: only match titles containing "Zoom Meeting" (default) or
    ; "'s Zoom Meeting" (personalized). Custom topics without "Meeting" are missed
    ; but that's better than false positives from internal UI.
    try {
        if (WinExist("ahk_exe Zoom.exe")) {
            windows := WinGetList("ahk_exe Zoom.exe")
            for hwnd in windows {
                try {
                    title := WinGetTitle("ahk_id " . hwnd)
                    if (InStr(title, "Zoom Meeting")) {
                        LogMessage("Detected: Zoom meeting (" . title . ")")
                        AM_STATE.lastWindowTitle := title
                        return "meeting"
                    }
                } catch {
                    continue
                }
            }
        }
    } catch {
        ; ignore
    }

    ; Check for Google Meet in Brave
    try {
        if (WinExist("ahk_exe brave.exe")) {
            windows := WinGetList("ahk_exe brave.exe")
            for hwnd in windows {
                try {
                    title := WinGetTitle("ahk_id " . hwnd)
                    ; Active call: "Meet - abc-defg-hij" (meeting code)
                    ; Named meeting: "Meeting Name | Google Meet" or "Meeting Name - Google Meet"
                    ; Skip landing page: just "Google Meet" with no separator
                    if (RegExMatch(title, "^Meet - [a-z]") || RegExMatch(title, ".+[\|\-] Google Meet")) {
                        LogMessage("Detected: Google Meet in Brave (" . title . ")")
                        AM_STATE.lastWindowTitle := title
                        return "meeting"
                    }
                } catch {
                    continue
                }
            }
        }
    } catch {
        ; ignore
    }

    ; Check for Minecraft (Java) - window title contains "Lucky World Invasion"
    try {
        if (WinExist("Lucky World Invasion")) {
            title := WinGetTitle("Lucky World Invasion")
            if (InStr(title, "Lucky World Invasion")) {
                LogMessage("Detected: Minecraft (" . title . ")")
                AM_STATE.lastWindowTitle := title
                return "gaming"
            }
        }
    } catch {
        ; ignore
    }

    ; Check for Spotify
    if (WinExist("ahk_exe Spotify.exe")) {
        ; Additional check: Spotify window title changes when playing
        ; Format: "Artist - Song Title" vs just "Spotify Free" or "Spotify Premium"
        spotifyTitle := WinGetTitle("ahk_exe Spotify.exe")

        ; If title contains " - ", it's likely playing a song
        ; This helps reduce false positives from idle Spotify
        if (InStr(spotifyTitle, " - ")) {
            LogMessage("Detected: Spotify playing (" . spotifyTitle . ")")
            AM_STATE.lastWindowTitle := spotifyTitle
            return "music"
        }
    }

    ; Check for YouTube in browsers
    ; Common browsers: Chrome, Edge, Firefox, Brave, Vivaldi
    ; browsers := [
    ;     {exe: "chrome.exe", class: "Chrome_WidgetWin_1"},
    ;     {exe: "msedge.exe", class: "Chrome_WidgetWin_1"},
    ;     {exe: "firefox.exe", class: "MozillaWindowClass"},
    ;     {exe: "brave.exe", class: "Chrome_WidgetWin_1"},
    ;     {exe: "vivaldi.exe", class: "Chrome_WidgetWin_1"}
    ; ]

    ; Only Brave for now to avoid false positives
    browsers := [{exe: "brave.exe", class: "Chrome_WidgetWin_1"}]

    for browser in browsers {
        if (WinExist("ahk_exe " . browser.exe)) {
            ; Get all windows for this browser
            windows := WinGetList("ahk_exe " . browser.exe)

            for hwnd in windows {
                try {
                    title := WinGetTitle("ahk_id " . hwnd)

                    ; Check if "YouTube" is in the title
                    if (InStr(title, "YouTube")) {
                        LogMessage("Detected: YouTube in " . browser.exe . " (" . title . ")")
                        AM_STATE.lastWindowTitle := title
                        return "video"
                    }
                } catch {
                    ; Window may have closed, skip it
                    continue
                }
            }
        }
    }

    ; No audio sources detected
    LogMessage("Detected: No audio sources (Silence)")
    AM_STATE.lastWindowTitle := ""
    return "silence"
}

; ========================================
; State Transition & Debouncing
; ========================================

ProcessDetection(detectedMode) {
    AM_STATE.detectedMode := detectedMode

    ; If detected mode matches current mode, reset pending state
    if (detectedMode == AM_STATE.currentMode) {
        AM_STATE.pendingMode := ""
        AM_STATE.confirmCount := 0
        return
    }

    ; If detected mode is new, start confirmation process
    if (detectedMode != AM_STATE.pendingMode) {
        AM_STATE.pendingMode := detectedMode
        AM_STATE.confirmCount := 1
        LogMessage("New mode detected: " . detectedMode . " (waiting for confirmation)")
        return
    }

    ; Same pending mode detected again - increment counter
    AM_STATE.confirmCount++
    LogMessage("Confirming mode: " . detectedMode . " (" . AM_STATE.confirmCount . "/" . AM_CONFIG.debounceCount . ")")

    ; If we've reached the debounce threshold, trigger mode change
    if (AM_STATE.confirmCount >= AM_CONFIG.debounceCount) {
        SwitchMode(detectedMode, AM_STATE.lastWindowTitle)
        AM_STATE.pendingMode := ""
        AM_STATE.confirmCount := 0
    }
}

; ========================================
; Mode Switching
; ========================================

SwitchMode(newMode, windowTitle := "") {
    if (!AM_MODES.Has(newMode)) {
        LogMessage("ERROR: Invalid mode: " . newMode)
        return
    }

    oldMode := AM_STATE.currentMode

    ; Music, silence, and meeting modes are always allowed without productivity check
    if (newMode == "music" || newMode == "silence" || newMode == "meeting") {
        LogMessage("🔄 " . newMode . " mode change (no productivity required): " . oldMode . " → " . newMode)

        ; Still notify token-api for tracking, but don't block on response
        NotifyTokenApiDetection(newMode, windowTitle)

        AM_STATE.currentMode := newMode
        AM_STATE.lastTrigger := A_TickCount

        LogMessage("Mode switched: " . AM_MODES[oldMode].icon . " " . AM_MODES[oldMode].name . " → " . AM_MODES[newMode].icon . " " . AM_MODES[newMode].name)

        if (AM_CONFIG.showNotifications) {
            TrayTip(AM_MODES[newMode].icon . " " . AM_MODES[newMode].name,
                    AM_MODES[newMode].description,
                    "Iconi Mute")
        }
        return
    }

    ; Route through token-api (authoritative for all decisions)
    LogMessage("🔄 Requesting mode change via token-api: " . oldMode . " → " . newMode)

    ; token-api decides if change is allowed and triggers Obsidian
    success := NotifyTokenApiDetection(newMode, windowTitle)

    if (success) {
        ; token-api approved and triggered Obsidian
        AM_STATE.currentMode := newMode
        AM_STATE.lastTrigger := A_TickCount

        LogMessage("Mode switched via token-api: " . AM_MODES[oldMode].icon . " " . AM_MODES[oldMode].name . " → " . AM_MODES[newMode].icon . " " . AM_MODES[newMode].name)

        if (AM_CONFIG.showNotifications) {
            TrayTip(AM_MODES[newMode].icon . " " . AM_MODES[newMode].name,
                    AM_MODES[newMode].description,
                    "Iconi Mute")
        }
    } else {
        ; token-api blocked the change (e.g., video without productivity/break time)
        LogMessage("Mode change blocked by token-api: " . newMode)

        ; Only show notification if cooldown has elapsed (prevents spam)
        if (AM_CONFIG.showNotifications) {
            if (A_TickCount - AM_STATE.lastBlockNotify >= AM_STATE.blockNotifyCooldown) {
                AM_STATE.lastBlockNotify := A_TickCount
                TrayTip("🚫 Productivity Required",
                        AM_MODES[newMode].name . " requires active work or earned break time",
                        "Iconx")
                LogMessage("Block notification shown (next in " . (AM_STATE.blockNotifyCooldown / 1000) . "s)")
            } else {
                LogMessage("Block notification suppressed (cooldown active)")
            }
        }
    }
}

; ========================================
; Manual Controls
; ========================================

LockMode(mode, duration := 0) {
    ; Lock to a specific mode for a duration (in minutes)
    ; If duration is 0, use default from config

    if (duration == 0) {
        duration := AM_CONFIG.manualLockDuration
    }

    AM_STATE.isLocked := true
    AM_STATE.lockUntil := A_TickCount + (duration * 60 * 1000)

    ; Immediately switch to locked mode if different
    if (mode != AM_STATE.currentMode) {
        SwitchMode(mode)
    }

    LogMessage("Manual lock enabled: " . AM_MODES[mode].name . " for " . duration . " minutes")

    if (AM_CONFIG.showNotifications) {
        TrayTip("🔒 Mode Locked",
                AM_MODES[mode].icon . " " . AM_MODES[mode].name . " for " . duration . " minutes",
                "Iconi")
    }
}

UnlockMode() {
    AM_STATE.isLocked := false
    AM_STATE.lockUntil := 0

    LogMessage("Manual lock disabled, resuming auto-detection")

    if (AM_CONFIG.showNotifications) {
        TrayTip("🔓 Auto Mode",
                "Automatic detection resumed",
                "Iconi")
    }
}

ToggleLock() {
    if (AM_STATE.isLocked) {
        UnlockMode()
    } else {
        LockMode(AM_STATE.currentMode)
    }
}

; ========================================
; Logging
; ========================================

LogMessage(message) {
    timestamp := FormatTime(, "yyyy-MM-dd HH:mm:ss")
    logLine := timestamp . " | " . message

    ; Always log to console for debugging
    OutputDebug(logLine)

    ; Optionally log to file
    if (AM_CONFIG.logToFile) {
        try {
            FileAppend(logLine . "`n", AM_CONFIG.logPath)
        } catch {
            ; Ignore file write errors
        }
    }
}

; ========================================
; token-api Integration
; ========================================

PostToTokenApi(endpoint, jsonBody) {
    ; POST JSON to token-api server
    ; Returns response object with: success, status, body

    url := AM_CONFIG.tokenApiUrl . endpoint

    try {
        http := ComObject("WinHttp.WinHttpRequest.5.1")
        http.Open("POST", url, false)
        http.SetRequestHeader("Content-Type", "application/json")
        http.Send(jsonBody)

        response := {
            success: true,
            status: http.Status,
            body: http.ResponseText
        }

        LogMessage("📡 token-api POST " . endpoint . " -> " . http.Status)
        return response
    } catch as err {
        LogMessage("❌ token-api POST failed: " . err.Message)
        return {
            success: false,
            status: 0,
            body: err.Message
        }
    }
}

NotifyTokenApiDetection(detectedMode, windowTitle := "") {
    ; Notify token-api of detected mode change
    ; token-api decides if mode change is allowed and triggers Obsidian

    jsonBody := '{"detected_mode": "' . detectedMode . '", "window_title": "' . windowTitle . '", "source": "ahk"}'

    response := PostToTokenApi("/desktop", jsonBody)

    if (response.success && response.status == 200) {
        LogMessage("✅ token-api approved mode: " . detectedMode)
        return true
    } else if (response.status == 403) {
        LogMessage("🚫 token-api blocked mode: " . detectedMode . " (productivity inactive)")
        return false
    } else {
        LogMessage("⚠️ token-api error: " . response.body)
        return false
    }
}

; ========================================
; Tray Menu
; ========================================

SetupTrayMenu() {
    A_TrayMenu.Delete()

    ; Add current status
    A_TrayMenu.Add("Audio Monitor - " . AM_MODES[AM_STATE.currentMode].icon . " " . AM_MODES[AM_STATE.currentMode].name, (*) => ShowStatus())
    A_TrayMenu.Disable("Audio Monitor - " . AM_MODES[AM_STATE.currentMode].icon . " " . AM_MODES[AM_STATE.currentMode].name)
    A_TrayMenu.Add()

    ; Manual mode selection
    A_TrayMenu.Add("🔇 Lock to Silence", (*) => LockMode("silence"))
    A_TrayMenu.Add("🎵 Lock to Music", (*) => LockMode("music"))
    A_TrayMenu.Add("📺 Lock to Video", (*) => LockMode("video"))
    A_TrayMenu.Add()

    ; Toggle auto mode
    lockLabel := AM_STATE.isLocked ? "🔓 Enable Auto Mode" : "🔒 Disable Auto Mode"
    A_TrayMenu.Add(lockLabel, (*) => ToggleLock())
    A_TrayMenu.Add()

    ; Show status
    A_TrayMenu.Add("📊 Show Status", (*) => ShowStatus())
    A_TrayMenu.Add()

    ; Standard items
    A_TrayMenu.Add("⚙️ Reload Script", (*) => Reload())
    A_TrayMenu.Add("❌ Exit", (*) => ExitApp())

    ; Set default action
    A_TrayMenu.Default := "📊 Show Status"
}

ShowStatus() {
    status := ""
    status .= "🎯 Current Mode: " . AM_MODES[AM_STATE.currentMode].icon . " " . AM_MODES[AM_STATE.currentMode].name . "`n"
    status .= "🔍 Detected Mode: " . AM_MODES[AM_STATE.detectedMode].icon . " " . AM_MODES[AM_STATE.detectedMode].name . "`n"

    if (AM_STATE.pendingMode != "") {
        status .= "⏳ Pending: " . AM_MODES[AM_STATE.pendingMode].icon . " " . AM_MODES[AM_STATE.pendingMode].name . " (" . AM_STATE.confirmCount . "/" . AM_CONFIG.debounceCount . ")`n"
    }

    if (AM_STATE.isLocked) {
        remaining := Round((AM_STATE.lockUntil - A_TickCount) / 1000 / 60, 1)
        status .= "🔒 Locked for: " . remaining . " minutes`n"
    } else {
        status .= "🔓 Auto mode: Active`n"
    }

    status .= "`nPolling interval: " . (AM_CONFIG.pollingInterval / 1000) . " seconds"
    status .= "`ntoken-api: " . AM_CONFIG.tokenApiUrl
    status .= "`nEnforcement: Push-based (server-side)"

    MsgBox(status, "Audio Monitor Status", "Iconi T10")
}

; ========================================
; Hotkeys (Optional)
; ========================================

; Ctrl+Alt+Shift+A - Toggle auto/manual mode
^!+a::ToggleLock()

; Ctrl+Alt+Shift+S - Show status
; ^!+s::ShowStatus()

; Ctrl+Alt+Shift+1 - Lock to Silence
^!+1::LockMode("silence")

; Ctrl+Alt+Shift+2 - Lock to Music
^!+2::LockMode("music")

; Ctrl+Alt+Shift+3 - Lock to Video
^!+3::LockMode("video")

; ========================================
; Polling Timer
; ========================================

MonitorLoop() {
    detectedMode := DetectAudioState()
    ProcessDetection(detectedMode)

    ; Send heartbeat every 6th poll (~30s at 5s interval)
    AM_STATE.pollCount++
    if (Mod(AM_STATE.pollCount, 6) == 0) {
        SendHeartbeat()
    }

    ; Update tray menu to show current status
    SetupTrayMenu()
}

SendHeartbeat() {
    jsonBody := '{"mode": "' . AM_STATE.currentMode . '", "source": "ahk"}'
    response := PostToTokenApi("/desktop/heartbeat", jsonBody)
    if (response.success) {
        LogMessage("💓 Heartbeat sent (mode=" . AM_STATE.currentMode . ")")
    }
}

; ========================================
; Initialization
; ========================================

LogMessage("=== Audio Monitor Started ===")
LogMessage("Vault: " . AM_CONFIG.vaultName)
LogMessage("Polling interval: " . (AM_CONFIG.pollingInterval / 1000) . " seconds")
LogMessage("Debounce count: " . AM_CONFIG.debounceCount)
LogMessage("token-api: ENABLED (" . AM_CONFIG.tokenApiUrl . ")")
LogMessage("  - Mode changes: POST /desktop")
LogMessage("  - Heartbeat: POST /desktop/heartbeat (every ~30s)")

SetupTrayMenu()

; Start polling
SetTimer(MonitorLoop, AM_CONFIG.pollingInterval)

; Run once immediately
MonitorLoop()
