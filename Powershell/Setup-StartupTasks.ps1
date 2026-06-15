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
        Source = Join-Path $PSScriptRoot "Invoke-DeskflowBoot.ps1"
        Target = Join-Path $StartupRoot "Invoke-DeskflowBoot.ps1"
    },
    @{
        Source = Join-Path $PSScriptRoot "Start-BluetoothAudioReceiver.ps1"
        Target = Join-Path $StartupRoot "Start-BluetoothAudioReceiver.ps1"
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

# Belt-and-suspenders fallback for the non-elevated bootstrap. If the scheduled
# task is stale, disabled, or blocked by permissions, HKCU Run still starts the
# local startup.ahk copy at interactive logon. #SingleInstance in startup.ahk
# makes this safe even when ahk_boot also succeeds.
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "`"$AhkExe`" `"$StartupAhk`""
New-Item -Path $RunKey -Force | Out-Null
Set-ItemProperty -Path $RunKey -Name "ImperiumStartupAhk" -Value $RunValue
Write-Host "Registered HKCU Run fallback: ImperiumStartupAhk"

Register-ImperiumLogonTask `
    -TaskName "ahk_boot" `
    -Description "Windows logon bootstrap: monitor TUI, Deskflow phased restart, Bluetooth Audio Receiver, startup hotkeys" `
    -Execute $AhkExe `
    -Arguments "`"$StartupAhk`"" `
    -Principal $LimitedPrincipal

Register-ImperiumLogonTask `
    -TaskName "ahk_init" `
    -Description "Start the main NAS-backed AutoHotkey suite" `
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
Write-Host "  ahk_init   - NAS-backed main AutoHotkey suite"
Write-Host "  ahk_admin  - elevated ring remap"
