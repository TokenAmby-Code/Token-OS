# Setup-HeadlessTask.ps1 - Create scheduled tasks for headless mode control
# RUN THIS SCRIPT ONCE AS ADMINISTRATOR to set up the tasks
# After setup, tasks can be triggered from WSL without elevation

# Requires Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Must run as Administrator to create scheduled tasks"
    Write-Host "Right-click PowerShell and select 'Run as administrator'"
    exit 1
}

$ScriptDir = $PSScriptRoot
$ToggleScript = Join-Path $ScriptDir "Toggle-Headless.ps1"

# Verify the toggle script exists
if (-not (Test-Path $ToggleScript)) {
    Write-Host "Error: Toggle-Headless.ps1 not found at $ToggleScript"
    exit 1
}

Write-Host "Setting up Headless Mode scheduled tasks..."
Write-Host "Script location: $ToggleScript"
Write-Host ""

# Task settings - run with highest privileges, hidden window
$TaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden

# Principal - run as current user with highest privileges
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive

# Create three tasks: Toggle, Enable, Disable
$Tasks = @(
    @{
        Name = "HeadlessToggle"
        Description = "Toggle headless mode on/off"
        Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ToggleScript`""
    },
    @{
        Name = "HeadlessEnable"
        Description = "Enable headless mode"
        Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ToggleScript`" -Enable"
    },
    @{
        Name = "HeadlessDisable"
        Description = "Disable headless mode"
        Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ToggleScript`" -Disable"
    }
)

foreach ($Task in $Tasks) {
    # Remove existing task if present
    $existing = Get-ScheduledTask -TaskName $Task.Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing existing task: $($Task.Name)"
        Unregister-ScheduledTask -TaskName $Task.Name -Confirm:$false
    }

    # Create action
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $Task.Arguments

    # Register the task (no trigger - manual run only)
    Register-ScheduledTask -TaskName $Task.Name -Description $Task.Description -Action $Action -Principal $Principal -Settings $TaskSettings | Out-Null

    Write-Host "Created task: $($Task.Name)"
}

Write-Host ""
Write-Host "Setup complete! Tasks created:"
Write-Host "  - HeadlessToggle  : Toggle between enabled/disabled"
Write-Host "  - HeadlessEnable  : Force enable headless mode"
Write-Host "  - HeadlessDisable : Force disable headless mode"
Write-Host ""
Write-Host "Run from Windows:"
Write-Host "  schtasks /run /tn HeadlessToggle"
Write-Host "  schtasks /run /tn HeadlessEnable"
Write-Host "  schtasks /run /tn HeadlessDisable"
Write-Host ""
Write-Host "Run from WSL:"
Write-Host "  schtasks.exe /run /tn HeadlessToggle"
Write-Host "  schtasks.exe /run /tn HeadlessEnable"
Write-Host "  schtasks.exe /run /tn HeadlessDisable"
Write-Host ""
Write-Host "Check status:"
Write-Host "  cat /mnt/c/Users/colby/Documents/Obsidian/Imperium-ENV/Scripts/Powershell/headless-state.json"
