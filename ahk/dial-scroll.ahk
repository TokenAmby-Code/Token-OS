; Antikater dial → unified scroll curve (event-driven, zero timers)
; Dial rotation mapped to F13 (up/right) and F14 (down/left) via Antikater software.
; twin: hammerspoon/init.lua — same algorithm + constants drive the Mac via deskflow KVM.
;
; One wheel event per dial tick, scaled by a multiplier that ramps while ticks
; arrive faster than FAST_WINDOW_MS and resets to base on any slow tick.
; Output happens only inside the tick handler — releasing the dial is a dead stop.
; Brake: a reverse tick while ripping (multiplier ramped AND last tick within
; BRAKE_WINDOW_MS) is swallowed — freezes output instead of scrolling backward.
; The next reverse tick, now at rest, scrolls normally.
;
; Shared constants (keep in sync with twin):
;   BASE_LINES=1  FAST_WINDOW_MS=120  ACCEL_RATE=1.2  ACCEL_MAX=12.0  BRAKE_WINDOW_MS=300
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
DIAL_MIN_INPUT_GAP_MS := 3           ; Drop inputs faster than this (BT phantom guard)
; ===========================================

; State
global dialLastTickMs := 0
global dialLastDir := 0
global dialMult := 1.0

; Wildcard (*) so the dial fires even while Ctrl/Shift/Alt/Win are held —
; a bare F13:: would be bypassed as a distinct Ctrl+F13 combo. Blind-mode
; Send in DialTick then carries the held modifier into the wheel event,
; so Ctrl+dial → Ctrl+scroll (zoom), etc.
*F13::DialTick(1)
*F14::DialTick(-1)

DialTick(dir) {
    global dialLastTickMs, dialLastDir, dialMult
    global DIAL_BASE_LINES, DIAL_FAST_WINDOW_MS, DIAL_ACCEL_RATE, DIAL_ACCEL_MAX
    global DIAL_BRAKE_WINDOW_MS, DIAL_MIN_INPUT_GAP_MS

    now := A_TickCount
    gap := now - dialLastTickMs

    ; Drop inputs faster than human-possible — guards against BT/HID phantom spam
    if (gap > 0 && gap < DIAL_MIN_INPUT_GAP_MS)
        return
    dialLastTickMs := now

    if (dir != dialLastDir && dialLastDir != 0) {
        wasRipping := (dialMult > 1.0 && gap < DIAL_BRAKE_WINDOW_MS)
        dialMult := 1.0
        dialLastDir := dir
        if (wasRipping)
            return  ; BRAKE: swallow the stop-tick, scroll nothing
        ; slow-gap reversal = deliberate turnaround → fall through, scroll at base
    }
    dialLastDir := dir

    if (gap < DIAL_FAST_WINDOW_MS)
        dialMult := Min(dialMult * DIAL_ACCEL_RATE, DIAL_ACCEL_MAX)
    else
        dialMult := 1.0

    lines := Max(1, Round(DIAL_BASE_LINES * dialMult))
    if (dir > 0)
        Send("{Blind}{WheelUp " lines "}")
    else
        Send("{Blind}{WheelDown " lines "}")
}
