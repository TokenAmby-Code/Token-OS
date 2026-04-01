; Antikater dial → smooth scroll with exponential acceleration
; Dial mapped to F13 (up/right) and F14 (down/left) via Antikater software
; Slow ticks = precise sub-line scroll. Fast swipe = exponential ramp.

; ============== CONFIGURATION ==============
DIAL_BASE_AMOUNT := 0.4              ; Scroll lines per tick at minimum speed
DIAL_ACCEL_RATE := 1.15              ; Exponential multiplier per rapid input
DIAL_ACCEL_MAX := 12.0               ; Cap on acceleration multiplier
DIAL_ACCEL_DECAY := 0.88             ; Multiplier decay per tick (back to base)
DIAL_ACCEL_WINDOW_MS := 120          ; Inputs faster than this increase accel
DIAL_TICK_MS := 8                    ; Output timer interval
DIAL_MIN_ACCUM := 0.05              ; Stop threshold for accumulator
; ===========================================

; State
global dialAccum := 0.0
global dialAccelMultiplier := 1.0
global dialTimerRunning := false
global dialLastInputTime := 0
global dialLastDir := 0

F13::DialInput(1)
F14::DialInput(-1)

DialInput(dir) {
    global dialAccum, dialAccelMultiplier, dialTimerRunning, dialLastInputTime, dialLastDir
    global DIAL_BASE_AMOUNT, DIAL_ACCEL_RATE, DIAL_ACCEL_MAX, DIAL_ACCEL_WINDOW_MS, DIAL_TICK_MS

    now := A_TickCount
    timeSinceLast := now - dialLastInputTime
    dialLastInputTime := now

    ; Direction change resets acceleration
    if (dir != dialLastDir && dialLastDir != 0) {
        dialAccelMultiplier := 1.0
        dialAccum := 0.0
    }
    dialLastDir := dir

    ; Ramp acceleration if inputs arrive faster than the window
    if (timeSinceLast < DIAL_ACCEL_WINDOW_MS && timeSinceLast > 0)
        dialAccelMultiplier := Min(dialAccelMultiplier * DIAL_ACCEL_RATE, DIAL_ACCEL_MAX)

    ; Add to accumulator: base amount scaled by current acceleration
    dialAccum += dir * DIAL_BASE_AMOUNT * dialAccelMultiplier

    ; Start timer if not running
    if (!dialTimerRunning) {
        dialTimerRunning := true
        SetTimer(DialScrollTick, DIAL_TICK_MS)
    }
}

DialScrollTick() {
    global dialAccum, dialAccelMultiplier, dialTimerRunning
    global DIAL_ACCEL_DECAY, DIAL_MIN_ACCUM

    ; Decay acceleration toward 1.0
    dialAccelMultiplier := Max(1.0, dialAccelMultiplier * DIAL_ACCEL_DECAY)

    ; Output accumulated scroll
    if (dialAccum >= 1) {
        outputAmount := Min(8, Max(1, Integer(dialAccum)))
        dialAccum -= outputAmount
        MouseClick("WheelUp",,, outputAmount)
    } else if (dialAccum <= -1) {
        outputAmount := Min(8, Max(1, Integer(-dialAccum)))
        dialAccum += outputAmount
        MouseClick("WheelDown",,, outputAmount)
    }

    ; Stop when nothing left
    if (dialAccum > -DIAL_MIN_ACCUM && dialAccum < DIAL_MIN_ACCUM && dialAccelMultiplier <= 1.0) {
        dialAccum := 0.0
        dialTimerRunning := false
        SetTimer(DialScrollTick, 0)
    }
}
