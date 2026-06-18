#Requires AutoHotkey v2.0
#SingleInstance Force

; Launch Phone Link while aggressively placing any windows it creates on the
; smallest monitor. This is intentionally separate from startup until the
; no-main-monitor-flash behavior has been proven.

SetWinDelay(0)

global MaxPlacementAttempts := 450
global PlacementAttempts := MaxPlacementAttempts
global InitialHiddenAttempts := 25
global MiniMonitor := GetMiniMonitor()
global RevealedWindows := Map()

GetMiniMonitor() {
    monitorCount := MonitorGetCount()
    mini := {index: 1, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, area: 0}

    Loop monitorCount {
        MonitorGet(A_Index, &left, &top, &right, &bottom)
        width := right - left
        height := bottom - top
        area := width * height

        if (A_Index == 1 || area < mini.area) {
            mini := {index: A_Index, left: left, top: top, right: right, bottom: bottom, width: width, height: height, area: area}
        }
    }

    return mini
}

IsHwndOnMini(hwnd) {
    global MiniMonitor

    try {
        WinGetPos(&x, &y, &width, &height, "ahk_id " hwnd)
        if (x < -30000 || y < -30000 || width <= 0 || height <= 0) {
            return false
        }

        centerX := x + Floor(width / 2)
        centerY := y + Floor(height / 2)
        return centerX >= MiniMonitor.left
            && centerX < MiniMonitor.right
            && centerY >= MiniMonitor.top
            && centerY < MiniMonitor.bottom
    } catch {
        return false
    }
}

PlaceHwndOnMini(hwnd, reveal := false) {
    global MiniMonitor, RevealedWindows

    hwndKey := String(hwnd)
    try {
        alreadyOnMini := IsHwndOnMini(hwnd)
        alreadyRevealed := RevealedWindows.Has(hwndKey)
        minMax := WinGetMinMax("ahk_id " hwnd)

        if (alreadyOnMini && ((reveal && (alreadyRevealed || minMax != -1)) || (!reveal && minMax == -1))) {
            return true
        }

        if (minMax == -1 || reveal) {
            WinRestore("ahk_id " hwnd)
        }

        width := Floor(MiniMonitor.width * 0.96)
        height := Floor(MiniMonitor.height * 0.92)
        x := MiniMonitor.left + Floor((MiniMonitor.width - width) / 2)
        y := MiniMonitor.top + Floor((MiniMonitor.height - height) / 2)
        WinMove(x, y, width, height, "ahk_id " hwnd)

        if reveal {
            RevealedWindows[hwndKey] := true
        } else {
            WinMinimize("ahk_id " hwnd)
        }

        return true
    } catch {
        return false
    }
}

SweepPhoneLinkWindows() {
    global MaxPlacementAttempts, PlacementAttempts, InitialHiddenAttempts

    selectors := [
        "ahk_exe PhoneExperienceHost.exe",
        "Phone Link"
    ]

    reveal := PlacementAttempts <= (MaxPlacementAttempts - InitialHiddenAttempts)
    for selector in selectors {
        try {
            for hwnd in WinGetList(selector) {
                PlaceHwndOnMini(hwnd, reveal)
            }
        } catch {
            continue
        }
    }

    PlacementAttempts -= 1
    if (PlacementAttempts <= 0) {
        SetTimer(SweepPhoneLinkWindows, 0)
        ExitApp()
    }
}

SetTimer(SweepPhoneLinkWindows, 100)
SweepPhoneLinkWindows()
Run('explorer.exe "shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"', , "Min")
