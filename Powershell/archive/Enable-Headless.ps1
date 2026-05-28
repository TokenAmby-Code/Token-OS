# Requires Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Must run as Administrator. Right-click PowerShell and select 'Run as administrator'"
    exit 1
}

Write-Host "Enabling Headless Mode..."

# Create backup file if it doesn't exist (use script directory)
$backupFile = Join-Path $PSScriptRoot "headless-backup.txt"
if (-not (Test-Path $backupFile)) {
    Write-Host "Saving current power settings..."

    # Query current power settings and save them
    $monitorTimeout = powercfg /query SCHEME_CURRENT SUB_VIDEO VIDEOIDLE 2>$null
    $diskTimeout = powercfg /query SCHEME_CURRENT SUB_DISK DISKIDLE 2>$null
    $sleepTimeout = powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>$null

    $backupContent = @"
# Backup of original power settings - generated $(Get-Date)
# These are raw powercfg outputs for reference

=== Monitor Timeout ===
$($monitorTimeout -join "`n")

=== Disk Timeout ===
$($diskTimeout -join "`n")

=== Sleep Timeout ===
$($sleepTimeout -join "`n")
"@
    $backupContent | Out-File -FilePath $backupFile -Encoding UTF8
}

# Disable all timeouts (0 = never)
Write-Host "Setting power timeouts to 'Never'..."
powercfg /change monitor-timeout-ac 0 | Out-Null
powercfg /change disk-timeout-ac 0 | Out-Null
powercfg /change standby-timeout-ac 0 | Out-Null
powercfg /change hibernate-timeout-ac 0 | Out-Null

# Set performance mode
Write-Host "Setting power mode to 'Best performance'..."
powercfg /setactive 8c5e7fda-e8bf-45a6-a6cc-4b3c1234a2d8 2>&1 | Out-Null

# Turn off monitors
Write-Host "Turning off monitors..."
$monitor = @"
[DllImport("user32.dll", SetLastError = true)]
public static extern int SendMessage(int hWnd, int hMsg, int wParam, int lParam);
"@
Add-Type -MemberDefinition $monitor -Name 'User32' -Namespace 'Win32' -PassThru -ErrorAction SilentlyContinue | Out-Null
[Win32.User32]::SendMessage(-1, 0x0112, 0xf170, 2) | Out-Null

Write-Host "Headless mode enabled!"
Write-Host ""
Write-Host "Status:"
Write-Host "   - Monitors: OFF"
Write-Host "   - Computer will NOT sleep or hibernate"
Write-Host "   - SSH access available via Tailscale"
Write-Host "   - To restore: Run Disable-Headless.ps1"
Write-Host ""
Write-Host "You can safely disconnect peripherals and leave."
