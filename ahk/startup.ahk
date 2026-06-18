#Requires AutoHotkey v2.0
#SingleInstance Force

; ==================== STARTUP.AHK ====================
; Canonical Windows logon bootstrap.
; Copied locally by Powershell/Setup-StartupTasks.ps1 so the task does not
; depend on the NAS being mounted during early login.

global StartupRoot := EnvGet("USERPROFILE") "\Imperium-Startup"
global StartupTimerSeconds := 10
global StartupAppDelaySeconds := 2
global OpsCockpitUrl := "http://100.95.109.23:7777/ui/ops"

SetTitleMatchMode(2)
SetWinDelay(0)

LaunchLocalPowerShell(scriptName, arguments := "") {
    global StartupRoot
    scriptPath := StartupRoot "\" scriptName
    if !FileExist(scriptPath) {
        return false
    }

    cmd := 'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' scriptPath '"'
    if (arguments != "") {
        cmd .= " " arguments
    }
    Run(cmd,, "Hide")
    return true
}

BootWslHeadless() {
    ; Kick WSL awake headlessly so systemd boots and starts token-satellite.
    ; The deprecated `wt.exe ... monitor` TUI surface is gone; booting WSL was
    ; the only function it served. `-e true` runs a trivial command purely to
    ; trigger distro boot. Invoke-DeskflowBoot.ps1 (below) absorbs systemd
    ; warm-up via its 20s delay / 180s health-timeout before kicking the Mac.
    Run('wsl.exe -d Ubuntu -e true', , "Hide")
    return true
}

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

GetLeftVerticalMonitor() {
    monitorCount := MonitorGetCount()
    selected := ""

    Loop monitorCount {
        MonitorGet(A_Index, &left, &top, &right, &bottom)
        width := right - left
        height := bottom - top

        if (height > width && right <= 0) {
            candidate := {index: A_Index, left: left, top: top, right: right, bottom: bottom, width: width, height: height, area: width * height}
            if !IsObject(selected) || candidate.left < selected.left {
                selected := candidate
            }
        }
    }

    if IsObject(selected) {
        return selected
    }

    return GetMiniMonitor()
}

MoveWindowHwndToMonitor(hwnd, monitor, widthRatio := 0.92, heightRatio := 0.88) {
    WinRestore("ahk_id " hwnd)
    width := Floor(monitor.width * widthRatio)
    height := Floor(monitor.height * heightRatio)
    x := monitor.left + Floor((monitor.width - width) / 2)
    y := monitor.top + Floor((monitor.height - height) / 2)
    WinMove(x, y, width, height, "ahk_id " hwnd)
    return true
}

MoveWindowToMonitor(winTitle, monitor, widthRatio := 0.92, heightRatio := 0.88) {
    hwnd := WinExist(winTitle)
    if !hwnd {
        return false
    }

    return MoveWindowHwndToMonitor(hwnd, monitor, widthRatio, heightRatio)
}

StopWindowAttention(hwnd) {
    try {
        size := A_PtrSize == 8 ? 32 : 20
        hwndOffset := A_PtrSize == 8 ? 8 : 4
        flagsOffset := A_PtrSize == 8 ? 16 : 8
        countOffset := flagsOffset + 4
        timeoutOffset := countOffset + 4
        info := Buffer(size, 0)
        NumPut("UInt", size, info, 0)
        NumPut("Ptr", hwnd, info, hwndOffset)
        NumPut("UInt", 0, info, flagsOffset) ; FLASHW_STOP
        NumPut("UInt", 0, info, countOffset)
        NumPut("UInt", 0, info, timeoutOffset)
        DllCall("FlashWindowEx", "Ptr", info)
    } catch {
        return false
    }
    return true
}

IsHwndOnMonitor(hwnd, monitor) {
    try {
        WinGetPos(&x, &y, &width, &height, "ahk_id " hwnd)
        if (x < -30000 || y < -30000 || width <= 0 || height <= 0) {
            return false
        }

        centerX := x + Floor(width / 2)
        centerY := y + Floor(height / 2)
        return centerX >= monitor.left
            && centerX < monitor.right
            && centerY >= monitor.top
            && centerY < monitor.bottom
    } catch {
        return false
    }
}

PlaceWindowAttempt(state) {
    matched := false
    windows := []
    try {
        windows := WinGetList(state.title)
    } catch {
        windows := []
    }

    for hwnd in windows {
        try {
            minMax := WinGetMinMax("ahk_id " hwnd)
            StopWindowAttention(hwnd)

            if (state.minimize && minMax == -1) {
                WinRestore("ahk_id " hwnd)
                minMax := 0
            }

            if (state.maximize && minMax == 1 && IsHwndOnMonitor(hwnd, state.monitor)) {
                matched := true
                continue
            }

            MoveWindowHwndToMonitor(hwnd, state.monitor, state.widthRatio, state.heightRatio)

            if state.maximize {
                WinMaximize("ahk_id " hwnd)
            }
            if state.minimize {
                WinMinimize("ahk_id " hwnd)
            }
            if state.activate {
                WinActivate("ahk_id " hwnd)
            }
            matched := true
        } catch {
            continue
        }
    }

    state.attempts -= 1
    if (matched && !state.continueAfterFound) {
        SetTimer(state.timer, 0)
        return
    }

    if (state.attempts <= 0) {
        SetTimer(state.timer, 0)
        return
    }

    SetTimer(state.timer, state.intervalMs)
}

ScheduleWindowPlacement(winTitle, monitor, widthRatio := 0.92, heightRatio := 0.88, maximize := false, minimize := false, activate := false, attempts := 30, intervalMs := 1000, continueAfterFound := false) {
    state := {
        title: winTitle,
        monitor: monitor,
        widthRatio: widthRatio,
        heightRatio: heightRatio,
        maximize: maximize,
        minimize: minimize,
        activate: activate,
        attempts: attempts,
        intervalMs: intervalMs,
        continueAfterFound: continueAfterFound
    }
    state.timer := PlaceWindowAttempt.Bind(state)

    PlaceWindowAttempt(state)
}

LaunchVivaldi() {
    Run "C:\Users\colby\AppData\Local\Vivaldi\Application\vivaldi.exe"
}

LaunchSpotify() {
    spotifyExe := EnvGet("APPDATA") "\Spotify\Spotify.exe"
    if FileExist(spotifyExe) {
        Run(Format('"{1}"', spotifyExe), , "Min")
    } else {
        Run('explorer.exe "shell:AppsFolder\Spotify"', , "Min")
    }
}

LaunchObsidian() {
    Run "C:\Users\colby\AppData\Local\Programs\Obsidian\Obsidian.exe"
}

LaunchCursor() {
    Run "C:\Users\colby\AppData\Local\Programs\cursor\Cursor.exe"
}

LaunchUbuntuTerminal() {
    Run "wt.exe"
}

LaunchBrave() {
    Run "C:\Users\colby\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
}

LaunchDiscordMinimized() {
    discordUpdate := EnvGet("LOCALAPPDATA") "\Discord\Update.exe"
    if FileExist(discordUpdate) {
        Run(Format('"{1}" --processStart Discord.exe --process-start-args "--start-minimized"', discordUpdate), , "Min")
        return true
    }

    Run('explorer.exe "shell:AppsFolder\com.squirrel.Discord.Discord"', , "Min")
    return true
}

LaunchDiscordOnVerticalMinimized(monitor) {
    ScheduleWindowPlacement("ahk_exe Discord.exe", monitor, 0.96, 0.96, true, true, false, 45, 1000)
    LaunchDiscordMinimized()
}

LaunchSteamMinimized(monitor) {
    steamExe := "C:\Program Files (x86)\Steam\steam.exe"
    ScheduleWindowPlacement("ahk_exe steamwebhelper.exe", monitor, 0.96, 0.92, false, true, false, 90, 500, true)
    if FileExist(steamExe) {
        Run(Format('"{1}" -silent', steamExe), , "Min")
        return true
    }

    Run('explorer.exe "shell:AppsFolder\{7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E}\Steam\steam.exe"', , "Min")
    return true
}

LaunchSpotifyOnMini(monitor) {
    ScheduleWindowPlacement("ahk_exe Spotify.exe", monitor, 0.96, 0.92, true, false, false, 90, 500, false)
    LaunchSpotify()
}

LaunchPhoneLinkPrivate(monitor) {
    ScheduleWindowPlacement("ahk_exe PhoneExperienceHost.exe", monitor, 0.96, 0.92, false, true, false, 90, 500, true)
    Run('explorer.exe "shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"', , "Min")
}

LaunchWisprFlowMinimized(monitor) {
    wisprExe := EnvGet("LOCALAPPDATA") "\WisprFlow\Wispr Flow.exe"
    ScheduleWindowPlacement("ahk_exe Wispr Flow.exe", monitor, 0.96, 0.92, false, true, false, 90, 500, true)
    if FileExist(wisprExe) {
        Run(Format('"{1}"', wisprExe), , "Hide")
    } else {
        Run('explorer.exe "shell:AppsFolder\Wispr Flow"', , "Min")
    }
    SetTimer RemoveWisprStartupShortcut, -15000
}

RemoveWisprStartupShortcut() {
    shortcuts := [
        A_Startup "\Wispr Flow.lnk",
        A_StartupCommon "\Wispr Flow.lnk",
        EnvGet("APPDATA") "\Microsoft\Windows\Start Menu\Programs\Startup\Wispr Flow.lnk",
        EnvGet("ProgramData") "\Microsoft\Windows\Start Menu\Programs\Startup\Wispr Flow.lnk"
    ]

    disabledRoot := StartupRoot "\DisabledStartupShortcuts"
    DirCreate(disabledRoot)
    for shortcut in shortcuts {
        if FileExist(shortcut) {
            try FileMove(shortcut, disabledRoot "\Wispr Flow.lnk", true)
        }
    }
}

LaunchBluetoothAudioReceiverOnMini(monitor) {
    ScheduleWindowPlacement("Bluetooth Audio Receiver", monitor, 0.96, 0.92, true, false, false, 90, 500, false)
    Run('explorer.exe "shell:AppsFolder\55746MarkSmirnov.BluetoothAudioReveicer_xwrbx6997tsfc!App"')
    LaunchLocalPowerShell("Open-BluetoothAudioReceiverConnection.ps1", "-TimeoutSeconds 45")
}

LaunchOpsCockpitOnLeftVertical(monitor) {
    global OpsCockpitUrl, StartupRoot
    braveExe := "C:\Users\colby\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe"
    opsProfile := StartupRoot "\Brave-OpsCockpit"
    width := monitor.width
    height := monitor.height
    x := monitor.left
    y := monitor.top
    ScheduleWindowPlacement("Ops Cockpit", monitor, 1.0, 1.0, true, false, true, 90, 500, false)
    if FileExist(braveExe) {
        Run(Format('"{1}" --new-window --app="{2}" --user-data-dir="{3}" --no-first-run --start-fullscreen --window-position={4},{5} --window-size={6},{7}', braveExe, OpsCockpitUrl, opsProfile, x, y, width, height))
    } else {
        Run('explorer.exe "' OpsCockpitUrl '"')
    }
}

LaunchStartupApps() {
    miniMonitor := GetMiniMonitor()
    leftVerticalMonitor := GetLeftVerticalMonitor()

    SetTimer LaunchSteamMinimized.Bind(miniMonitor), -1
    SetTimer LaunchDiscordOnVerticalMinimized.Bind(leftVerticalMonitor), -500
    SetTimer LaunchWisprFlowMinimized.Bind(miniMonitor), -1000
    SetTimer LaunchBluetoothAudioReceiverOnMini.Bind(miniMonitor), -1500
    ; Phone Link's own startup is disabled, and AHK launching it still flashes
    ; texts on the main monitor before placement catches up. Keep it off
    ; startup until there is a no-flash wrapper.
    ; SetTimer LaunchPhoneLinkPrivate.Bind(miniMonitor), -8000
    SetTimer LaunchSpotifyOnMini.Bind(miniMonitor), -2000
    SetTimer LaunchOpsCockpitOnLeftVertical.Bind(leftVerticalMonitor), -2500

    SetTimer ExitAfterStartupApps, -60000
}

DisableStartupHotkeys() {
    for key in ["Escape", "v", "o", "c", "u", "b"] {
        Hotkey key, "Off"
    }

    TrayTip "Startup Launcher Exiting", "Normal key behavior restored", 1
    Sleep 1500
}

ExitAfterStartupApps() {
    ExitApp
}

; 1. Boot WSL headlessly so systemd brings up token-satellite.
BootWslHeadless()

; 2. Kick the Deskflow phased restart through token-satellite once WSL is up.
LaunchLocalPowerShell("Invoke-DeskflowBoot.ps1", "-DelaySeconds 0 -HealthTimeoutSeconds 180")

; 3. Bluetooth Audio Receiver is now launched by the managed app policy below
; so it can be placed on the mini monitor.

; 4. Launch personal startup apps after the desktop and monitor layout settle.
SetTimer LaunchStartupApps, StartupAppDelaySeconds * 1000 * -1

; 5. Keep the short-lived startup hotkeys.
TrayTip "Startup Mode Active", "V=Vivaldi O=Obsidian C=Cursor`nU=Ubuntu B=Brave`n`nAuto-exits in " StartupTimerSeconds "s", 1
SetTimer DisableStartupHotkeys, StartupTimerSeconds * 1000 * -1

Escape::ExitApp

v:: LaunchVivaldi()
o:: LaunchObsidian()
c:: LaunchCursor()
u:: LaunchUbuntuTerminal()
b:: LaunchBrave()
