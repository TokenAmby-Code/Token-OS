#Requires AutoHotkey v2.0
#SingleInstance Off  ; Allow multiple scripts, but we handle uniqueness manually
Persistent

; Instance management for admin-mode scripts (AutoHotInterception requires admin)
PragmaOnce(scriptPath, hwnd) {
    DetectHiddenWindows True
    SetTitleMatchMode 3
    query := scriptPath " ahk_class AutoHotkey"
    ; Check if another instance of this specific script is already running
    try {
        if existingHwnd := WinExist(query) {
            ProcessClose(WinGetPID(existingHwnd))  ; Close the existing instance
            Sleep 100  ; Give it time to close
            PragmaOnce(scriptPath, hwnd)
        } else {
            WinSetTitle scriptPath, "ahk_id " hwnd
        }
    }
}
PragmaOnce(A_ScriptFullPath, A_ScriptHwnd)

#Include <AutoHotInterception>

; ============== TOKEN-API INTEGRATION ==============
TOKENAPI_URL := "http://100.95.109.23:7777"

PostToTokenApi(endpoint, body := "") {
    global TOKENAPI_URL
    url := TOKENAPI_URL . endpoint
    try {
        http := ComObject("WinHttp.WinHttpRequest.5.1")
        http.Open("POST", url, false)
        http.SetRequestHeader("Content-Type", "application/json")
        http.Send(body)
        return {success: true, status: http.Status, body: http.ResponseText}
    } catch as err {
        return {success: false, status: 0, body: err.Message}
    }
}

GetFromTokenApi(endpoint) {
    global TOKENAPI_URL
    url := TOKENAPI_URL . endpoint
    try {
        http := ComObject("WinHttp.WinHttpRequest.5.1")
        http.Open("GET", url, false)
        http.Send()
        return {success: true, status: http.Status, body: http.ResponseText}
    } catch as err {
        return {success: false, status: 0, body: err.Message}
    }
}

; Report dictation state change to Token-API
NotifyDictation(active) {
    arg := active ? "true" : "false"
    PostToTokenApi("/api/dictation?active=" arg)
}

; Check if a voice chat session is active (point-in-time read, not polling)
IsVoiceChatActive() {
    resp := GetFromTokenApi("/api/dictation")
    if (!resp.success)
        return false
    ; Parse JSON - look for "voice_chat_instance": "<something>"  (not null)
    return resp.body != "" && InStr(resp.body, '"voice_chat_instance": null') == 0 && InStr(resp.body, '"voice_chat_instance"')
}

; ============== CONFIGURATION ==============
RING_DEVICE_ID := 0  ; Auto-detected below (set manually to override)
MINIMUM_RING_ID := 14  ; IDs below this are assumed to be built-in devices (trackpad, etc.)
TAP_THRESHOLD_MS := 200  ; Mod-tap threshold for right button
LEFT_TAP_THRESHOLD_MS := 200  ; Mod-tap threshold for left button
DOUBLE_TAP_MS := 500  ; Double-tap window for Enter
DICTATION_BUFFER_MS := 1000  ; Buffer after dictation ends before sending queued Enter
DOUBLE_TAP_BYPASS_MS := 10000  ; Time window after dictation ends where single tap bypasses double-tap requirement
DEVICE_POLL_INTERVAL_MS := 3000  ; How often to check for ring when not found
DEVICE_POLL_MAX_ATTEMPTS := 100  ; Stop polling after 100 attempts
INACTIVITY_CHECK_MS := 30000  ; Check connection after 30 seconds of no input (was 5 minutes)
TRAYTIP_DURATION_MS := 2000  ; Auto-dismiss tray notifications after 2 seconds

; ============== AUTO-DETECT RING DEVICE ==============
; The Bluetooth ring gets a floating device ID that changes on reconnect.
; Known stable devices are typically IDs 11-13 (built-in trackpad, etc.)
; The ring is always the highest mouse device ID.
; Set RING_DEVICE_ID above to a specific value to override auto-detection.

global ringConnected := false
global scriptEnabled := true

; Manual reconnect - unsubscribes, re-detects, and resubscribes
^#r::{
    global ringConnected, scriptEnabled
    if (!scriptEnabled)
        return
    if (ringConnected)
        UnsubscribeFromRing()
    WaitForRing()
    SubscribeToRing()
    ResetActivityTimer()
}

; Toggle script on/off (wake-on-lan style - this hotkey always works)
#SuspendExempt
^+!r::{
    global scriptEnabled, ringConnected
    scriptEnabled := !scriptEnabled

    if (scriptEnabled) {
        ; Wake up - re-enable everything
        Suspend(false)
        WaitForRing()
        SubscribeToRing()
        ResetActivityTimer()
        QuickTrayTip("Ring Script ON", "All hotkeys re-enabled", 1)
        ShowFeedback("Script ON")
    } else {
        ; Sleep - disable everything except this hotkey
        if (ringConnected)
            UnsubscribeFromRing()
        CleanupRingScript()
        Suspend(true)
        QuickTrayTip("Ring Script OFF", "Press Ctrl+Shift+Alt+R to wake", 2)
        ToolTip("Script OFF - Ctrl+Shift+Alt+R to wake", 100, 100)
        SetTimer(ClearTooltip, -3000)
    }
}
#SuspendExempt false

; Bluetooth reconnect - force disconnect/reconnect at Windows Bluetooth layer
; Use when device shows "connected" but is unresponsive
^#d::{
    global ringConnected, scriptEnabled
    if (!scriptEnabled)
        return

    if (ringConnected)
        UnsubscribeFromRing()

    QuickTrayTip("Resetting Bluetooth", "Disconnecting D06 Pro...", 1)

    ; PowerShell command to disable and re-enable Bluetooth device
    psScript := 'Get-PnpDevice -FriendlyName "*D06*" -Class Bluetooth | Disable-PnpDevice -Confirm:$false; Start-Sleep -Milliseconds 500; Get-PnpDevice -FriendlyName "*D06*" -Class Bluetooth | Enable-PnpDevice -Confirm:$false'

    try {
        RunWait('*RunAs powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "' psScript '"',, 'Hide')
        QuickTrayTip("Bluetooth Reset", "Waiting for device...", 1)
    } catch {
        QuickTrayTip("Bluetooth Reset Failed", "PowerShell command failed. Check admin privileges.", 2)
        return
    }

    Sleep(1000)  ; Give device time to reappear

    WaitForRing()
    SubscribeToRing()
    ResetActivityTimer()
    QuickTrayTip("Bluetooth Restored", "D06 Pro reconnected successfully", 1)
}

; Nuclear option - completely remove Bluetooth device (requires manual re-pairing)
; Use only if Bluetooth reconnect fails repeatedly
^#!d::{
    global ringConnected, scriptEnabled
    if (!scriptEnabled)
        return

    if (ringConnected)
        UnsubscribeFromRing()

    QuickTrayTip("Removing Bluetooth Device", "This is a nuclear option. Device will be deleted.", 2)
    Sleep(2000)

    ; PowerShell command to remove the Bluetooth device completely
    psScript := 'Get-PnpDevice -FriendlyName "*D06*" -Class Bluetooth | Remove-PnpDevice -Confirm:$false'

    try {
        RunWait('*RunAs powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "' psScript '"',, 'Hide')
        QuickTrayTip("Device Removed", "D06 Pro deleted. Manually re-pair in Windows Bluetooth settings.", 2)
        Sleep(2000)
        ExitApp
    } catch {
        QuickTrayTip("Removal Failed", "PowerShell command failed. Check admin privileges.", 2)
        return
    }
}

DetectRingDevice() {
    global AHI, MINIMUM_RING_ID
    try {
        devices := AHI.GetDeviceList()
    } catch {
        return 0
    }
    highestMouseId := 0

    for id, device in devices {
        if (device.IsMouse && id > highestMouseId) {
            highestMouseId := id
        }
    }

    ; Only return if above minimum threshold (avoids detecting built-in devices as ring)
    if (highestMouseId < MINIMUM_RING_ID) {
        return 0
    }
    return highestMouseId
}

IsDevicePresent(deviceId) {
    global AHI
    if (deviceId == 0)
        return false
    try {
        devices := AHI.GetDeviceList()
        return devices.Has(deviceId) && devices[deviceId].IsMouse
    } catch {
        return false
    }
}

; Ring Remapper - D06 Pro ring with gestures and Wispr Flow
; Ring device ID: 14
; Press Ctrl+Shift+Escape to exit


WaitForRing() {
    global RING_DEVICE_ID, DEVICE_POLL_INTERVAL_MS, DEVICE_POLL_MAX_ATTEMPTS

    RING_DEVICE_ID := DetectRingDevice()

    if (RING_DEVICE_ID == 0) {
        QuickTrayTip("Waiting for Ring", "Ring not detected. Polling every " (DEVICE_POLL_INTERVAL_MS / 1000) "s...", 1)
        pollAttempts := 0

        while (RING_DEVICE_ID == 0) {
            Sleep(DEVICE_POLL_INTERVAL_MS)
            pollAttempts++
            RING_DEVICE_ID := DetectRingDevice()

            if (RING_DEVICE_ID > 0) {
                QuickTrayTip("Ring Found!", "Device ID: " RING_DEVICE_ID " (after " pollAttempts " attempts)", 1)
                break
            }

            if (DEVICE_POLL_MAX_ATTEMPTS > 0 && pollAttempts >= DEVICE_POLL_MAX_ATTEMPTS) {
                QuickTrayTip("Ring Not Found", "Ring not detected after " pollAttempts " attempts. Exiting.", 2)
                Sleep(2000)
                ExitApp
            }

            if (pollAttempts == 10 || pollAttempts == 50) {
                QuickTrayTip("Still Waiting for Ring", "Attempt " pollAttempts "... Connect your ring.", 1)
            }
        }
    }
    return RING_DEVICE_ID
}

SubscribeToRing() {
    global AHI, RING_DEVICE_ID, ringConnected

    AHI.SubscribeMouseButton(RING_DEVICE_ID, 0, true, LeftButtonCallback)
    AHI.SubscribeMouseButton(RING_DEVICE_ID, 1, true, RightButtonCallback)
    AHI.SubscribeMouseButton(RING_DEVICE_ID, 2, true, MiddleButtonCallback)
    AHI.SubscribeMouseMoveRelative(RING_DEVICE_ID, true, RingMoveCallback)
    AHI.SubscribeMouseButton(RING_DEVICE_ID, 5, true, ScrollCallback)

    ringConnected := true
    QuickTrayTip("Ring Remapper Active", "Device ID: " RING_DEVICE_ID, 1)
}

UnsubscribeFromRing() {
    global AHI, RING_DEVICE_ID, ringConnected

    if (!ringConnected)
        return

    try {
        AHI.SubscribeMouseButton(RING_DEVICE_ID, 0, false, LeftButtonCallback)
        AHI.SubscribeMouseButton(RING_DEVICE_ID, 1, false, RightButtonCallback)
        AHI.SubscribeMouseButton(RING_DEVICE_ID, 2, false, MiddleButtonCallback)
        AHI.SubscribeMouseMoveRelative(RING_DEVICE_ID, false, RingMoveCallback)
        AHI.SubscribeMouseButton(RING_DEVICE_ID, 5, false, ScrollCallback)
    } catch {
        ; Ensure ringConnected is false even if unsubscribe fails
    }
    ringConnected := false
}

ResetActivityTimer() {
    global INACTIVITY_CHECK_MS
    SetTimer(CheckRingConnection, -INACTIVITY_CHECK_MS)
}

; Comprehensive cleanup - stop all timers and reset state
CleanupRingScript() {
    global leftButtonDownTime, leftHoldActionSent, lastLeftTapTime, enterQueued
    global rightButtonDownTime, rightButtonHeld, toggleActive, dictationEndTime
    global doubleTapBypassActive, doubleTapBypassStartTime
    global gestureX, gestureY, gestureActive, gestureSequence
    global scrollVelocity, scrollAccum, scrollTimerRunning

    ; Stop all timers
    SetTimer(LeftHoldCheck, 0)
    SetTimer(SendQueuedEnter, 0)
    SetTimer(CheckRingConnection, 0)
    SetTimer(SmoothScrollTick, 0)
    SetTimer(ProcessGesture, 0)
    SetTimer(ClearGestureSequence, 0)
    SetTimer(ClearTooltip, 0)
    SetTimer(ClearTrayTip, 0)

    ; Reset all global state to defaults
    leftButtonDownTime := 0
    leftHoldActionSent := false
    lastLeftTapTime := 0
    enterQueued := false

    rightButtonDownTime := 0
    rightButtonHeld := false
    toggleActive := false
    dictationEndTime := 0

    doubleTapBypassActive := false
    doubleTapBypassStartTime := 0

    gestureX := 0
    gestureY := 0
    gestureActive := false
    gestureSequence := ""

    scrollVelocity := 0.0
    scrollAccum := 0.0
    scrollTimerRunning := false
}

CheckRingConnection() {
    global RING_DEVICE_ID, ringConnected, INACTIVITY_CHECK_MS

    if (!ringConnected)
        return

    if (IsDevicePresent(RING_DEVICE_ID)) {
        ; Still connected - reset timer for another inactivity period
        SetTimer(CheckRingConnection, -INACTIVITY_CHECK_MS)
        return
    }

    ; Ring disconnected
    UnsubscribeFromRing()
    QuickTrayTip("Ring Disconnected", "Device ID " RING_DEVICE_ID " lost. Waiting for reconnection...", 2)

    ; Wait for reconnection
    oldId := RING_DEVICE_ID
    RING_DEVICE_ID := 0
    WaitForRing()

    if (RING_DEVICE_ID != oldId) {
        QuickTrayTip("Ring Reconnected", "New Device ID: " RING_DEVICE_ID " (was " oldId ")", 1)
    }

    SubscribeToRing()
    ResetActivityTimer()
}

; Create AHI instance before detection
global AHI := AutoHotInterception()

; Initial connection
WaitForRing()

; Left button actions
LEFT_TAP_ACTION := "{Enter}"  ; Sent on double-tap
LEFT_HOLD_ACTION := "+!z"  ; Shift+Alt+Z - sent when held past threshold

; ============== GESTURE CONFIGURATION ==============
; Base actions (used by double-swipe gestures)
ACTION_UP := "click"        ; Special: performs mouse click
ACTION_DOWN := "^!#2"       ; Center cursor on primary monitor
ACTION_LEFT := "^!#1"       ; Move cursor to left monitor
ACTION_RIGHT := "^!#3"      ; Move cursor to right monitor

; Double-swipe gesture map (16 combinations)
; Single swipes are ignored - only double-swipes execute
global GestureCombo := Map()

; Same-direction doubles (terminal tab navigation)
GestureCombo["left-left"] := "!{Left}"        ; Alt+Left - previous tab
GestureCombo["right-right"] := "!{Right}"     ; Alt+Right - next tab
GestureCombo["up-up"] := "!{Up}"              ; Alt+Up - move tab up
GestureCombo["down-down"] := "!{Down}"        ; Alt+Down - move tab down

; Compound gestures (execute multiple actions)
GestureCombo["left-up"] := "left+up"          ; Move left, then click
GestureCombo["right-up"] := "right+up"        ; Move right, then click
GestureCombo["down-left"] := "down+left"      ; Center, then move left
GestureCombo["down-right"] := "down+right"    ; Center, then move right

; Remaining combinations (undefined for now)
GestureCombo["left-down"] := "!+{-}"
GestureCombo["left-right"] := ""
GestureCombo["right-down"] := "!+{+}"
GestureCombo["right-left"] := ""
GestureCombo["up-down"] := ""
GestureCombo["up-left"] := ""
GestureCombo["up-right"] := ""
GestureCombo["down-up"] := "claude{Enter}"  ; FLASH KICK! Summon Claude

; Gesture tuning
GESTURE_THRESHOLD := 30
GESTURE_TIMEOUT_MS := 150
GESTURE_SEQUENCE_TIMEOUT_MS := 800  ; Clear first gesture if second doesn't arrive

; ============== SMOOTH SCROLL CONFIGURATION ==============
SCROLL_MULTIPLIER := 1.2         ; Velocity added per scroll event
SCROLL_DECAY := 0.9              ; Velocity multiplier per tick (lower = faster stop)
SCROLL_TICK_MS := 8              ; Timer interval
SCROLL_MIN_VELOCITY := 0.2       ; Zero out velocity below this (in timer only)
; ============================================

global rightButtonDownTime := 0

; Subscribe and start inactivity-based connection monitoring
SubscribeToRing()
ResetActivityTimer()

; Left button state tracking
global leftButtonDownTime := 0
global lastLeftTapTime := 0
global enterQueued := false
global leftHoldActionSent := false  ; Track if hold action already fired

; Dictation state tracking
global rightButtonHeld := false
global toggleActive := false
global dictationEndTime := 0
global doubleTapBypassActive := false
global doubleTapBypassStartTime := 0

; Gesture tracking
global gestureX := 0
global gestureY := 0
global gestureActive := false
global gestureSequence := ""

; Scroll tracking - single authoritative timer model
global scrollVelocity := 0.0
global scrollAccum := 0.0
global scrollTimerRunning := false

; ============== BUTTON HANDLING ==============

; Check if dictation is currently active (right button held or toggle on)
IsDictationActive() {
    global rightButtonHeld, toggleActive
    return rightButtonHeld || toggleActive
}

LeftButtonCallback(state) {
    global LEFT_TAP_THRESHOLD_MS
    global leftButtonDownTime, leftHoldActionSent, dictationEndTime

    ResetActivityTimer()

    if (state) {
        ; Button pressed - start hold timer
        leftButtonDownTime := A_TickCount
        leftHoldActionSent := false
        SetTimer(LeftHoldCheck, -LEFT_TAP_THRESHOLD_MS)
    } else {
        ; Button released - cancel hold timer
        SetTimer(LeftHoldCheck, 0)

        if (leftHoldActionSent) {
            ; Hold action was sent - set up buffer window for quick follow-up tap
            dictationEndTime := A_TickCount
        } else {
            ; Hold action wasn't sent, so this was a tap
            HandleLeftTap()
        }
        leftButtonDownTime := 0
    }
}

LeftHoldCheck() {
    global LEFT_HOLD_ACTION, leftButtonDownTime, leftHoldActionSent

    ; Only fire if button is still held (leftButtonDownTime > 0)
    if (leftButtonDownTime > 0 && LEFT_HOLD_ACTION != "") {
        leftHoldActionSent := true
        Send(LEFT_HOLD_ACTION)
        ShowFeedback("Ring Left Hold → " LEFT_HOLD_ACTION)
    }
}

HandleLeftTap() {
    global LEFT_TAP_ACTION, DOUBLE_TAP_MS, DICTATION_BUFFER_MS, DOUBLE_TAP_BYPASS_MS
    global lastLeftTapTime, enterQueued, dictationEndTime
    global doubleTapBypassActive, doubleTapBypassStartTime

    ; Voice chat mode: single tap sends Enter immediately (hook intercepts it)
    if (IsVoiceChatActive() && !IsDictationActive()) {
        if (LEFT_TAP_ACTION != "") {
            Send(LEFT_TAP_ACTION)
            ShowFeedback("Ring Left (voice chat) → " LEFT_TAP_ACTION)
        }
        return
    }

    if (IsDictationActive()) {
        ; Queue Enter - will be sent after dictation ends + buffer
        enterQueued := true
        ShowFeedback("Enter queued (dictation active)")
        return
    }

    ; Check if we're in the buffer window after dictation ended (for wait-until-send timer)
    if (dictationEndTime > 0 && (A_TickCount - dictationEndTime) < DICTATION_BUFFER_MS) {
        ; Single tap during buffer - wait out remaining buffer time before sending
        remainingMs := DICTATION_BUFFER_MS - (A_TickCount - dictationEndTime)
        SetTimer(SendQueuedEnter, 0)  ; Cancel any pending timer
        enterQueued := true
        SetTimer(SendQueuedEnter, -remainingMs)
        ShowFeedback("Enter in " remainingMs "ms...")
        return
    }

    ; Check if double-tap bypass is active (10-second window)
    if (doubleTapBypassActive && doubleTapBypassStartTime > 0) {
        elapsedMs := A_TickCount - doubleTapBypassStartTime
        if (elapsedMs < DOUBLE_TAP_BYPASS_MS) {
            ; Bypass is active - single tap sends Enter, then disables bypass
            if (LEFT_TAP_ACTION != "") {
                Send(LEFT_TAP_ACTION)
                ShowFeedback("Ring Left (bypass) → " LEFT_TAP_ACTION)
            }
            doubleTapBypassActive := false  ; Disable bypass after use
            doubleTapBypassStartTime := 0
            lastLeftTapTime := 0  ; Reset double-tap tracking
            return
        } else {
            ; Bypass window expired - disable it
            doubleTapBypassActive := false
            doubleTapBypassStartTime := 0
        }
    }

    ; Normal double-tap logic
    if ((A_TickCount - lastLeftTapTime) < DOUBLE_TAP_MS) {
        ; Second tap within window - send Enter
        if (LEFT_TAP_ACTION != "") {
            Send(LEFT_TAP_ACTION)
            ShowFeedback("Ring Left (double-tap) → " LEFT_TAP_ACTION)
        }
        lastLeftTapTime := 0  ; Reset to require new double-tap
    } else {
        ; First tap - just record time
        lastLeftTapTime := A_TickCount
        ShowFeedback("Ring Left (tap 1/2)")
    }
}

SendQueuedEnter() {
    global enterQueued, dictationEndTime, LEFT_TAP_ACTION
    global doubleTapBypassActive, doubleTapBypassStartTime, DOUBLE_TAP_BYPASS_MS

    if (enterQueued && LEFT_TAP_ACTION != "") {
        Send(LEFT_TAP_ACTION)
        ShowFeedback("Ring Left → " LEFT_TAP_ACTION " (queued)")
    }
    enterQueued := false
    dictationEndTime := 0
    
    ; Activate double-tap bypass for 10 seconds after sending queued Enter
    doubleTapBypassActive := true
    doubleTapBypassStartTime := A_TickCount
}

RightButtonCallback(state) {
    global TAP_THRESHOLD_MS, DICTATION_BUFFER_MS, rightButtonDownTime
    global rightButtonHeld, toggleActive, enterQueued, dictationEndTime

    ResetActivityTimer()

    if (state) {
        ; Button pressed
        rightButtonDownTime := A_TickCount
        rightButtonHeld := true
        Send("{LCtrl down}{LWin down}")
        NotifyDictation(true)
        ShowFeedback("Wispr: Holding...")
    } else {
        ; Button released
        rightButtonHeld := false
        holdDuration := A_TickCount - rightButtonDownTime

        if (holdDuration < TAP_THRESHOLD_MS) {
            ; Tap - toggle dictation on/off
            toggleActive := !toggleActive
            Send("{Space}{LWin up}{LCtrl up}")
            if (toggleActive) {
                NotifyDictation(true)
                ShowFeedback("Wispr: Toggle ON (" holdDuration "ms)")
            } else {
                ; Toggle turned OFF - dictation ended
                NotifyDictation(false)
                OnDictationEnd()
                ShowFeedback("Wispr: Toggle OFF (" holdDuration "ms)")
            }
        } else {
            ; Hold release - dictation ended
            Send("{LWin up}{LCtrl up}")
            NotifyDictation(false)
            OnDictationEnd()
            ShowFeedback("Wispr: Released (" holdDuration "ms)")
        }
        rightButtonDownTime := 0
    }
}

OnDictationEnd() {
    global enterQueued, dictationEndTime, DICTATION_BUFFER_MS
    global doubleTapBypassActive, doubleTapBypassStartTime, DOUBLE_TAP_BYPASS_MS

    dictationEndTime := A_TickCount

    ; Activate double-tap bypass for 10 seconds
    doubleTapBypassActive := true
    doubleTapBypassStartTime := A_TickCount

    if (enterQueued) {
        ; Schedule queued Enter after buffer delay (1000ms)
        SetTimer(SendQueuedEnter, -DICTATION_BUFFER_MS)
        ShowFeedback("Enter will send in " DICTATION_BUFFER_MS "ms...")
    }
}

MiddleButtonCallback(state) {
    ResetActivityTimer()

    if (state) {
        ; Button pressed - send period and space
        Send(".{Space}")
        ShowFeedback("Ring Middle → .{Space}")
    }
    ; No action on release
}

; ============== SCROLL HANDLING ==============
; Button 5: state=1 for up, state=-1 for down

ScrollCallback(state) {
    global scrollVelocity, scrollTimerRunning, SCROLL_MULTIPLIER, SCROLL_TICK_MS

    ; state: 1 = up, -1 = down, 0 = ignore
    if (state == 0)
        return

    ; Immediate output if fresh start (velocity is 0)
    if (scrollVelocity == 0) {
        if (state > 0)
            MouseClick("WheelUp",,, 1)
        else
            MouseClick("WheelDown",,, 1)
    }

    ; Add to velocity (state is +1 or -1)
    scrollVelocity += state * SCROLL_MULTIPLIER

    ; Ensure single global timer is running
    if (!scrollTimerRunning) {
        scrollTimerRunning := true
        SetTimer(SmoothScrollTick, SCROLL_TICK_MS)
    }
}

; Single authoritative timer - reads scrollVelocity, outputs scroll, applies decay
SmoothScrollTick() {
    global scrollVelocity, scrollAccum, scrollTimerRunning
    global SCROLL_DECAY, SCROLL_MIN_VELOCITY

    ; Apply decay
    scrollVelocity *= SCROLL_DECAY

    ; Zero out if below threshold and stop timer
    if (scrollVelocity > -SCROLL_MIN_VELOCITY && scrollVelocity < SCROLL_MIN_VELOCITY) {
        scrollVelocity := 0
        scrollAccum := 0
        scrollTimerRunning := false
        SetTimer(SmoothScrollTick, 0)
        ResetActivityTimer()  ; Scroll sequence ended
        return
    }

    ; Accumulate velocity
    scrollAccum += scrollVelocity

    ; Output half of accumulation, capped at 5 (log-n convergence, no loop)
    ; Only subtract what we actually output to preserve inputs
    if (scrollAccum >= 1) {
        outputAmount := Min(5, Max(1, Integer(scrollAccum / 2)))
        scrollAccum -= outputAmount
        MouseClick("WheelUp",,, outputAmount)
    } else if (scrollAccum <= -1) {
        outputAmount := Min(5, Max(1, Integer(-scrollAccum / 2)))
        scrollAccum += outputAmount
        MouseClick("WheelDown",,, outputAmount)
    }
}

; ============== GESTURE HANDLING ==============
RingMoveCallback(x, y) {
    global gestureX, gestureY, gestureActive, GESTURE_TIMEOUT_MS

    ResetActivityTimer()

    gestureX += x
    gestureY += y
    gestureActive := true

    SetTimer(ProcessGesture, -GESTURE_TIMEOUT_MS)
}

ProcessGesture() {
    global gestureX, gestureY, gestureActive
    global GESTURE_THRESHOLD, GESTURE_SEQUENCE_TIMEOUT_MS
    global gestureSequence

    if (!gestureActive) {
        return
    }

    absX := Abs(gestureX)
    absY := Abs(gestureY)

    if (absX >= GESTURE_THRESHOLD || absY >= GESTURE_THRESHOLD) {
        if (absY > absX) {
            direction := (gestureY < 0) ? "up" : "down"
        } else {
            direction := (gestureX < 0) ? "left" : "right"
        }

        if (gestureSequence != "") {
            ; Cancel the timeout timer
            SetTimer(ClearGestureSequence, 0)
            gestureSequence .= "-" direction
            ; Second gesture received - execute immediately
            ExecuteGestureAction()
        } else {
            ; First gesture - show feedback and wait for second
            gestureSequence := direction
            ShowFeedback("Gesture: " gestureSequence " ...")
            ; Start timeout to clear if no second gesture arrives
            SetTimer(ClearGestureSequence, -GESTURE_SEQUENCE_TIMEOUT_MS)
        }
    }

    gestureX := 0
    gestureY := 0
    gestureActive := false
}

ClearGestureSequence() {
    global gestureSequence
    if (gestureSequence != "") {
        ShowFeedback("Gesture: " gestureSequence " (timeout)")
        gestureSequence := ""
    }
}

ExecuteGestureAction() {
    global gestureSequence, GestureCombo
    global ACTION_UP, ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT

    if (gestureSequence == "") {
        return
    }

    ; Only double-swipe gestures are valid
    if (!InStr(gestureSequence, "-")) {
        ShowFeedback("Gesture: " gestureSequence " (single - ignored)")
        gestureSequence := ""
        return
    }

    ; Look up the gesture in the combo map
    if (!GestureCombo.Has(gestureSequence) || GestureCombo[gestureSequence] == "") {
        ShowFeedback("Gesture: " gestureSequence " (no action)")
        gestureSequence := ""
        return
    }

    actionStr := GestureCombo[gestureSequence]
    ShowFeedback("Gesture: " gestureSequence " → " actionStr)

    ; Check if it's a raw hotkey string (contains modifiers or braces)
    if (RegExMatch(actionStr, "[!^#\{\}]")) {
        ; Raw hotkey - send directly
        Send(actionStr)
    } else {
        ; Parse and execute actions (can be "left" or "left+up" for compound)
        actions := StrSplit(actionStr, "+")
        for action in actions {
            ExecuteSingleAction(action)
        }
    }

    gestureSequence := ""
}

ExecuteSingleAction(action) {
    global ACTION_UP, ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT

    switch action {
        case "up":
            MouseClick "left"
        case "down":
            Send(ACTION_DOWN)
        case "left":
            Send(ACTION_LEFT)
        case "right":
            Send(ACTION_RIGHT)
    }
}

; ============== UTILITIES ==============
ShowFeedback(msg) {
    ToolTip(msg, 100, 100)
    SetTimer(ClearTooltip, -1500)
}

; Quick tray notification that auto-dismisses and overwrites previous
QuickTrayTip(title, msg, icon := 1) {
    global TRAYTIP_DURATION_MS
    TrayTip()  ; Clear any existing notification first
    TrayTip(title, msg, icon)
    SetTimer(ClearTrayTip, -TRAYTIP_DURATION_MS)
}

ClearTrayTip() {
    TrayTip()
}

ClearTooltip() {
    ToolTip
}

^+Escape::ExitApp
