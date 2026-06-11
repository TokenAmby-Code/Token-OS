; Antikater dial → unified scroll curve + momentum coast
; Dial rotation mapped to F13 (up/right) and F14 (down/left) via Antikater software.
; twin: hammerspoon/init.lua — same algorithm + constants.
;
; THIS engine is the live one for BOTH surfaces: deskflow ignores the dial's
; injected F13/F14 but forwards our injected wheel events, so when KVM focus is
; on the Mac the Mac feels this curve too (the Hammerspoon twin is a dormant
; fallback for if the dial ever talks to the Mac directly).
;
; One wheel event per dial tick, scaled by a multiplier that ramps while ticks
; arrive faster than FAST_WINDOW_MS and resets to base on any slow tick.
; A ripped dial (multiplier past COAST_MIN_MULT) coasts after release: a pulse
; timer keeps emitting decaying scroll until the multiplier runs out.
; Brake: a reverse tick while ripping OR coasting is swallowed — freezes output
; instead of scrolling backward. The next reverse tick, at rest, scrolls normally.
;
; Shared constants (keep in sync with twin):
;   BASE_LINES=1  FAST_WINDOW_MS=120  ACCEL_RATE=1.2  ACCEL_MAX=12.0  BRAKE_WINDOW_MS=300
;   COAST_DECAY=0.85  COAST_TICK_MS=30  COAST_MIN_MULT=3.0  RELEASE_MS=60
;   MIN_INPUT_GAP_MS=3 is AHK-only (BT phantom guard); the deskflow path doesn't need it.

; Buffer rather than warn when dial spams (BT noise can fire F13/F14 while AFK)
#MaxThreadsPerHotkey 2
#MaxThreadsBuffer true

; ============== CONFIGURATION ==============
DIAL_BASE_LINES := 1                 ; Scroll lines for a single deliberate tick
DIAL_FAST_WINDOW_MS := 120           ; Gaps below this ramp acceleration
DIAL_ACCEL_RATE := 1.2               ; Multiplier growth per fast tick
DIAL_ACCEL_MAX := 12.0               ; Cap on the multiplier
DIAL_BRAKE_WINDOW_MS := 300          ; Reversal within this of last tick while ramped = brake
DIAL_COAST_DECAY := 0.85             ; Multiplier decay per coast pulse
DIAL_COAST_TICK_MS := 30             ; Coast pulse interval
DIAL_COAST_MIN_MULT := 3.0           ; Only rips coast; gentle ticking still stops dead
DIAL_RELEASE_MS := 60                ; No tick for this long = dial released, coast may pulse
DIAL_MIN_INPUT_GAP_MS := 3           ; Drop inputs faster than this (BT phantom guard)
DIAL_DEBUG := false                  ; Log gap:lines per tick to dial-debug.log (tuning only)
; ===========================================

; State
global dialLastTickMs := 0
global dialLastDir := 0
global dialMult := 1.0
global dialCoastOn := false
global dialDbgBuf := ""

; Wildcard (*) so the dial fires even while Ctrl/Shift/Alt/Win are held —
; a bare F13:: would be bypassed as a distinct Ctrl+F13 combo. Blind-mode
; Send in DialTick then carries the held modifier into the wheel event,
; so Ctrl+dial → Ctrl+scroll (zoom), etc.
*F13::DialTick(1)
*F14::DialTick(-1)

DialTick(dir) {
    global dialLastTickMs, dialLastDir, dialMult, dialCoastOn
    global DIAL_BASE_LINES, DIAL_FAST_WINDOW_MS, DIAL_ACCEL_RATE, DIAL_ACCEL_MAX
    global DIAL_BRAKE_WINDOW_MS, DIAL_COAST_MIN_MULT, DIAL_COAST_TICK_MS
    global DIAL_MIN_INPUT_GAP_MS, DIAL_DEBUG

    now := A_TickCount
    gap := now - dialLastTickMs

    ; Drop inputs faster than human-possible — guards against BT/HID phantom spam
    if (gap > 0 && gap < DIAL_MIN_INPUT_GAP_MS)
        return
    dialLastTickMs := now

    coasting := dialCoastOn

    if (dir != dialLastDir && dialLastDir != 0) {
        wasMoving := coasting || (dialMult > 1.0 && gap < DIAL_BRAKE_WINDOW_MS)
        DialCoastStop()
        dialMult := 1.0
        dialLastDir := dir
        if (wasMoving)
            return  ; BRAKE: swallow the stop-tick, scroll nothing
        ; slow-gap reversal = deliberate turnaround → fall through, scroll at base
    }
    dialLastDir := dir

    ; A same-direction tick during coast re-engages the flywheel: keep the
    ; decayed multiplier and ramp from there instead of resetting to base.
    if (gap < DIAL_FAST_WINDOW_MS || coasting)
        dialMult := Min(dialMult * DIAL_ACCEL_RATE, DIAL_ACCEL_MAX)
    else
        dialMult := 1.0

    lines := Max(1, Round(DIAL_BASE_LINES * dialMult))
    Send("{Blind}{Wheel" (dir > 0 ? "Up" : "Down") " " lines "}")

    if (DIAL_DEBUG)
        DialDebugLog(gap, lines)

    if (dialMult >= DIAL_COAST_MIN_MULT && !dialCoastOn) {
        dialCoastOn := true
        SetTimer(DialCoastPulse, DIAL_COAST_TICK_MS)
    }
}

; Coast pulse: while the dial is still actively ticking it no-ops; once released
; it pays out decaying momentum until the multiplier runs dry.
DialCoastPulse() {
    global dialLastTickMs, dialLastDir, dialMult
    global DIAL_BASE_LINES, DIAL_COAST_DECAY, DIAL_RELEASE_MS

    if (A_TickCount - dialLastTickMs < DIAL_RELEASE_MS)
        return
    dialMult := dialMult * DIAL_COAST_DECAY
    lines := Round(DIAL_BASE_LINES * dialMult)
    if (lines < 1) {
        DialCoastStop()
        dialMult := 1.0
        return
    }
    Send("{Blind}{Wheel" (dialLastDir > 0 ? "Up" : "Down") " " lines "}")
}

DialCoastStop() {
    global dialCoastOn
    if (dialCoastOn) {
        dialCoastOn := false
        SetTimer(DialCoastPulse, 0)
    }
}

; Tuning telemetry: buffers "gap:lines" per tick, flushes to dial-debug.log next
; to the script only on a burst boundary so no file I/O happens mid-rip.
DialDebugLog(gap, lines) {
    global dialDbgBuf
    if (gap > 1000 && dialDbgBuf != "") {
        try FileAppend(dialDbgBuf, A_ScriptDir "\dial-debug.log")
        dialDbgBuf := ""
    }
    dialDbgBuf .= gap ":" lines "`n"
}
