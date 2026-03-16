# Setup-WorktreeSync.ps1 - Create scheduled task for worktree export at logoff
# RUN THIS SCRIPT ONCE AS ADMINISTRATOR to set up the task
# After setup, worktrees are automatically exported when logging off Windows

# Requires Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Must run as Administrator to create scheduled tasks"
    Write-Host "Right-click PowerShell and select 'Run as administrator'"
    exit 1
}

$ScriptDir = $PSScriptRoot
$ExportScript = Join-Path $ScriptDir "Worktree-SyncExport.ps1"

# Verify the export script exists
if (-not (Test-Path $ExportScript)) {
    Write-Host "Error: Worktree-SyncExport.ps1 not found at $ExportScript"
    exit 1
}

Write-Host "Setting up Worktree Sync scheduled task..."
Write-Host "Script location: $ExportScript"
Write-Host ""

$TaskName = "WorktreeSyncExport"

# Remove existing task if present
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task: $TaskName"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Task settings
$TaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden

# Principal - run as current user with highest privileges
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest -LogonType Interactive

# Action - run the export script
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ExportScript`""

# Trigger - at logoff
$Trigger = New-ScheduledTaskTrigger -AtLogOff

# Register the task
Register-ScheduledTask -TaskName $TaskName -Description "Export git worktrees to NAS staging at logoff" `
    -Action $Action -Principal $Principal -Settings $TaskSettings -Trigger $Trigger | Out-Null

Write-Host "Created task: $TaskName"
Write-Host ""
Write-Host "Setup complete!"
Write-Host ""
Write-Host "The task will run automatically when you log off Windows."
Write-Host "This exports all non-main worktrees to NAS staging so they"
Write-Host "can be imported on another machine."
Write-Host ""
Write-Host "Manual trigger:"
Write-Host "  schtasks /run /tn $TaskName"
Write-Host ""
Write-Host "From WSL:"
Write-Host "  schtasks.exe /run /tn $TaskName"
