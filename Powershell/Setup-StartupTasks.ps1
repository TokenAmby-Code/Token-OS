# Setup-StartupTasks.ps1
# Create the canonical Windows logon tasks for Token-PC startup automation.
# RUN THIS SCRIPT ONCE AS ADMINISTRATOR on Windows.

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Must run as Administrator to create or update startup tasks"
    Write-Host "Right-click PowerShell and select 'Run as administrator'"
    exit 1
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$AhkDir = Join-Path $RepoRoot "ahk"
$StartupRoot = Join-Path $env:USERPROFILE "Imperium-Startup"
$AhkExe = "C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"
$UserProfile = [Environment]::GetFolderPath("UserProfile")

$CopyMap = @(
    @{
        Source = Join-Path $AhkDir "startup.ahk"
        Target = Join-Path $UserProfile "startup.ahk"
    },
    @{
        Source = Join-Path $AhkDir "ahk-nas-wait.bat"
        Target = Join-Path $UserProfile "ahk-nas-wait.bat"
    },
    @{
        Source = Join-Path $AhkDir "ring-remap.ahk"
        Target = Join-Path $StartupRoot "ring-remap.ahk"
    },
    @{
        Source = Join-Path $PSScriptRoot "Invoke-DeskflowBoot.ps1"
        Target = Join-Path $StartupRoot "Invoke-DeskflowBoot.ps1"
    },
    @{
        Source = Join-Path $PSScriptRoot "Start-BluetoothAudioReceiver.ps1"
        Target = Join-Path $StartupRoot "Start-BluetoothAudioReceiver.ps1"
    },
    @{
        Source = Join-Path $PSScriptRoot "Open-BluetoothAudioReceiverConnection.ps1"
        Target = Join-Path $StartupRoot "Open-BluetoothAudioReceiverConnection.ps1"
    },
    @{
        Source = Join-Path $PSScriptRoot "Repair-BonjourLsaBlock.ps1"
        Target = Join-Path $StartupRoot "Repair-BonjourLsaBlock.ps1"
    }
)

foreach ($item in $CopyMap) {
    if (-not (Test-Path $item.Source)) {
        Write-Host "Missing required source file: $($item.Source)"
        exit 1
    }
}

if (-not (Test-Path $AhkExe)) {
    Write-Host "AutoHotkey not found at $AhkExe"
    exit 1
}

New-Item -ItemType Directory -Force -Path $StartupRoot | Out-Null
foreach ($item in $CopyMap) {
    $targetDir = Split-Path -Parent $item.Target
    if ($targetDir) {
        New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    }
    Copy-Item -Force $item.Source $item.Target
}

$DisabledStartupRoot = Join-Path $StartupRoot "DisabledStartupShortcuts"
New-Item -ItemType Directory -Force -Path $DisabledStartupRoot | Out-Null

# These are now owned by startup.ahk so it can place/minimize them consistently.
$WisprStartupShortcuts = @(
    (Join-Path ([Environment]::GetFolderPath("Startup")) "Wispr Flow.lnk"),
    (Join-Path ([Environment]::GetFolderPath("CommonStartup")) "Wispr Flow.lnk"),
    (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup\Wispr Flow.lnk"),
    (Join-Path $env:ProgramData "Microsoft\Windows\Start Menu\Programs\Startup\Wispr Flow.lnk")
) | Select-Object -Unique
foreach ($WisprStartupShortcut in $WisprStartupShortcuts) {
    if (Test-Path $WisprStartupShortcut) {
        Move-Item -Force $WisprStartupShortcut (Join-Path $DisabledStartupRoot "Wispr Flow.lnk")
        Write-Host "Disabled Startup folder shortcut: Wispr Flow ($WisprStartupShortcut)"
    }
}

# Phone Link is a packaged app startup task, not a classic Run/Startup entry.
# Disable its login startup task. Phone Link remains manual until we have a
# no-main-monitor-flash launch wrapper.
$PhoneLinkStartupTaskKey = "HKCU:\Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\SystemAppData\Microsoft.YourPhone_8wekyb3d8bbwe\YourPhone.Start"
if (Test-Path $PhoneLinkStartupTaskKey) {
    Set-ItemProperty -Path $PhoneLinkStartupTaskKey -Name "State" -Type DWord -Value 1
    Set-ItemProperty -Path $PhoneLinkStartupTaskKey -Name "UserEnabledStartupOnce" -Type DWord -Value 0
    Write-Host "Disabled packaged startup task: Phone Link / YourPhone.Start"
}

$TaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$LimitedPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$HighestPrincipal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest

function Register-ImperiumLogonTask {
    param(
        [string]$TaskName,
        [string]$Description,
        [string]$Execute,
        [string]$Arguments,
        $Principal,
        [int]$DelaySeconds = 0
    )

    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }

    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    if ($DelaySeconds -gt 0) {
        $trigger.Delay = "PT${DelaySeconds}S"
    }

    $action = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Description $Description `
        -Action $action `
        -Trigger $trigger `
        -Principal $Principal `
        -Settings $TaskSettings | Out-Null

    Write-Host "Registered task: $TaskName"
}

$StartupAhk = Join-Path $UserProfile "startup.ahk"
$AhkNasWait = Join-Path $UserProfile "ahk-nas-wait.bat"

# The Task Scheduler entry is the single owner for startup. HKCU Run caused a
# duplicate startup.ahk execution and duplicate ops cockpit windows.
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (Test-Path $RunKey) {
    Remove-ItemProperty -Path $RunKey -Name "ImperiumStartupAhk" -ErrorAction SilentlyContinue
    Write-Host "Removed HKCU Run fallback: ImperiumStartupAhk"
}

Register-ImperiumLogonTask `
    -TaskName "ahk_boot" `
    -Description "Windows logon bootstrap: WSL boot, Deskflow phased restart, Bluetooth Audio Receiver, startup hotkeys, app placement" `
    -Execute $AhkExe `
    -Arguments "`"$StartupAhk`"" `
    -Principal $LimitedPrincipal

Register-ImperiumLogonTask `
    -TaskName "ahk_init" `
    -Description "Start the main local-cache AutoHotkey suite" `
    -Execute $AhkNasWait `
    -Arguments "script-compiler" `
    -Principal $LimitedPrincipal

Register-ImperiumLogonTask `
    -TaskName "ahk_admin" `
    -Description "Start the elevated ring remap AutoHotkey script" `
    -Execute $AhkNasWait `
    -Arguments "ring-remap" `
    -Principal $HighestPrincipal `
    -DelaySeconds 60

foreach ($obsoleteTask in @("Deskflow", "AHK startup mode", "MonitorLauncher")) {
    $task = Get-ScheduledTask -TaskName $obsoleteTask -ErrorAction SilentlyContinue
    if ($task) {
        Disable-ScheduledTask -TaskName $obsoleteTask | Out-Null
        Write-Host "Disabled obsolete task: $obsoleteTask"
    }
}

Write-Host ""
Write-Host "Startup bootstrap synced to:"
Write-Host "  $StartupAhk"
Write-Host "  $AhkNasWait"
Write-Host "  $StartupRoot"
Write-Host ""
Write-Host "Tasks now managed by this script:"
Write-Host "  ahk_boot   - local startup bootstrap"
Write-Host "  ahk_init   - local-cache main AutoHotkey suite"
Write-Host "  ahk_admin  - elevated ring remap"
