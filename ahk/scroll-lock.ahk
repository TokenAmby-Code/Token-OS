; Direction-locked smooth scroll for main mouse
; M5 = lock scroll UP, M4 = lock scroll DOWN
; Tap = toggle lock on/off, Hold = lock while held, M4+M5 = reset
; While locked, ALL wheel input scrolls in the locked direction.
;
; Safe with ring-remap.ahk: AHK v2 SendLevel 0 (default) synthetic events
; don't trigger InputLevel 0 hotkeys, so ring's MouseClick output bypasses these.

; Buffer instead of warning when WheelUp/Down arrives faster than the handler
; (e.g. mouse with phantom scroll events while AFK)
#MaxThreadsPerHotkey 2
#MaxThreadsBuffer true

; ============== CONFIGURATION ==============
SCROLL_LOCK_MULTIPLIER := 0.4     ; Velocity added per scroll event
SCROLL_LOCK_DECAY := 0.85         ; Velocity multiplier per tick (lower = faster stop)
SCROLL_LOCK_TICK_MS := 8          ; Timer interval for smooth output
SCROLL_LOCK_MIN_VELOCITY := 0.2   ; Stop threshold
SCROLL_LOCK_HOLD_MS := 400        ; Hold threshold — shorter = tap, longer = hold
; ===========================================

; State
global scrollLockDir := 0           ; 0=unlocked, 1=up, -1=down
global scrollLockVelocity := 0.0
global scrollLockAccum := 0.0
global scrollLockTimerRunning := false
global scrollLockPressTime := 0
global scrollLockWasLocked := false
global scrollLockM4Down := false
global scrollLockM5Down := false

WheelUp::HandleLockedScroll(1)
WheelDown::HandleLockedScroll(-1)

; M5 = lock UP (1), M4 = lock DOWN (-1)
XButton2::SideBtnDown(1)
XButton2 Up::SideBtnUp(1)
XButton1::SideBtnDown(-1)
XButton1 Up::SideBtnUp(-1)

SideBtnDown(dir) {
    global scrollLockDir, scrollLockVelocity, scrollLockAccum, scrollLockPressTime
    global scrollLockM4Down, scrollLockM5Down, scrollLockTimerRunning, scrollLockWasLocked

    if (dir == -1)
        scrollLockM4Down := true
    else
        scrollLockM5Down := true

    ; Both pressed = reset everything
    if (scrollLockM4Down && scrollLockM5Down) {
        scrollLockDir := 0
        scrollLockVelocity := 0
        scrollLockAccum := 0
        scrollLockTimerRunning := false
        SetTimer(ScrollLockTick, 0)
        return
    }

    scrollLockPressTime := A_TickCount
    scrollLockWasLocked := (scrollLockDir == dir)

    if (scrollLockDir != dir) {
        scrollLockDir := dir
        scrollLockVelocity := 0
        scrollLockAccum := 0
    }
}

SideBtnUp(dir) {
    global scrollLockDir, scrollLockVelocity, scrollLockAccum, scrollLockPressTime
    global scrollLockM4Down, scrollLockM5Down, scrollLockWasLocked

    if (dir == -1)
        scrollLockM4Down := false
    else
        scrollLockM5Down := false

    if (scrollLockDir == 0)
        return

    held := (A_TickCount - scrollLockPressTime) >= SCROLL_LOCK_HOLD_MS

    if (held) {
        scrollLockDir := 0
        scrollLockVelocity := 0
        scrollLockAccum := 0
    } else if (scrollLockWasLocked) {
        scrollLockDir := 0
        scrollLockVelocity := 0
        scrollLockAccum := 0
    }
}

HandleLockedScroll(direction) {
    global scrollLockDir, scrollLockVelocity, scrollLockAccum, scrollLockTimerRunning
    global SCROLL_LOCK_MULTIPLIER, SCROLL_LOCK_TICK_MS

    effectiveDir := (scrollLockDir != 0) ? scrollLockDir : direction

    ; Immediate output on fresh start
    if (scrollLockVelocity == 0) {
        if (effectiveDir > 0)
            MouseClick("WheelUp",,, 1)
        else
            MouseClick("WheelDown",,, 1)
    }

    scrollLockVelocity += effectiveDir * SCROLL_LOCK_MULTIPLIER

    if (!scrollLockTimerRunning) {
        scrollLockTimerRunning := true
        SetTimer(ScrollLockTick, SCROLL_LOCK_TICK_MS)
    }
}

ScrollLockTick() {
    global scrollLockVelocity, scrollLockAccum, scrollLockTimerRunning
    global SCROLL_LOCK_DECAY, SCROLL_LOCK_MIN_VELOCITY

    scrollLockVelocity *= SCROLL_LOCK_DECAY

    if (scrollLockVelocity > -SCROLL_LOCK_MIN_VELOCITY && scrollLockVelocity < SCROLL_LOCK_MIN_VELOCITY) {
        scrollLockVelocity := 0
        scrollLockAccum := 0
        scrollLockTimerRunning := false
        SetTimer(ScrollLockTick, 0)
        return
    }

    scrollLockAccum += scrollLockVelocity

    if (scrollLockAccum >= 1) {
        outputAmount := Min(5, Max(1, Integer(scrollLockAccum / 2)))
        scrollLockAccum -= outputAmount
        MouseClick("WheelUp",,, outputAmount)
    } else if (scrollLockAccum <= -1) {
        outputAmount := Min(5, Max(1, Integer(-scrollLockAccum / 2)))
        scrollLockAccum += outputAmount
        MouseClick("WheelDown",,, outputAmount)
    }
}
