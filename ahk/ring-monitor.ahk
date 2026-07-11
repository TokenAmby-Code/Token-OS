#Requires AutoHotkey v2.0
#SingleInstance Force
Persistent

; Ring Monitor - Identifies device IDs for input remapping
; Run this script, then click with your D06 Pro ring to see its device ID
; Press Ctrl+Escape to exit

#Include <AutoHotInterception>

global AHI := AutoHotInterception()
global mouseIds := []
global RING_MONITOR_LOG := A_UserProfile "\Imperium-Startup\logs\ring-monitor.log"

AppendMonitorLog(message) {
    global RING_MONITOR_LOG
    try {
        SplitPath(RING_MONITOR_LOG, , &dir)
        if (dir != "" && !DirExist(dir))
            DirCreate(dir)
        FileAppend(FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss") A_Tab message "`n", RING_MONITOR_LOG)
    }
}

DeviceSummary(id, device) {
    return "id=" id " Handle=" device.Handle " VID=0x" Format("{:04X}", device.VID) " PID=0x" Format("{:04X}", device.PID)
}

; Get all devices (returns a Map keyed by device ID)
devices := AHI.GetDeviceList()

; Display all detected mice
output := "=== DETECTED MOUSE DEVICES ===`n`n"
for id, device in devices {
    if (device.IsMouse) {
        mouseIds.Push(id)
        summary := DeviceSummary(id, device)
        AppendMonitorLog("candidate " summary)
        output .= "ID: " id "`n"
        output .= "Handle: " device.Handle "`n"
        output .= "VID: 0x" Format("{:04X}", device.VID) " | PID: 0x" Format("{:04X}", device.PID) "`n"
        output .= "---`n"
    }
}

if (mouseIds.Length == 0) {
    MsgBox("No mouse devices detected! Make sure Interception driver is installed.", "Error")
    ExitApp
}

MsgBox(output, "Ring Monitor - Device List")

; Subscribe to all mouse buttons on all devices to identify which one is clicked
output2 := "Now monitoring all mouse clicks...`n"
output2 .= "Click with your RING to see which device ID it uses.`n"
output2 .= "A tooltip will appear showing the device.`n`n"
output2 .= "Press OK to start monitoring, then Ctrl+ESC to exit."

MsgBox(output2, "Ring Monitor")

; Subscribe to mouse buttons on all detected mice (using pattern from Monitor.ahk)
for id in mouseIds {
    AHI.SubscribeMouseButtons(id, false, MouseButtonCallback.Bind(id))
}

MouseButtonCallback(id, code, state) {
    buttonNames := Map(0, "Left", 1, "Right", 2, "Middle", 3, "XButton1", 4, "XButton2")
    buttonName := buttonNames.Has(code) ? buttonNames[code] : "Unknown(" code ")"
    stateText := state ? "DOWN" : "UP"

    AppendMonitorLog("event-source id=" id " button=" buttonName " state=" stateText)
    ToolTip("Device ID: " id "`nButton: " buttonName "`nState: " stateText, 100, 100)
    SetTimer(ClearTooltip, -2000)
}

ClearTooltip() {
    ToolTip
}

^Escape::ExitApp
