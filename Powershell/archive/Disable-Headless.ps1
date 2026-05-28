# Requires Administrator
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Must run as Administrator"
    exit 1
}

Write-Host "Disabling Headless Mode..."

# Set back to standard timeouts
Write-Host "Restoring default power timeouts..."
powercfg /change monitor-timeout-ac 10 | Out-Null    # 10 min
powercfg /change disk-timeout-ac 20 | Out-Null       # 20 min
powercfg /change standby-timeout-ac 30 | Out-Null    # 30 min
powercfg /change hibernate-timeout-ac 0 | Out-Null   # Never

# Set balanced mode
Write-Host "Setting power mode to 'Balanced'..."
powercfg /setactive 381b4222-f694-41f0-9685-ff5bb260df2e 2>&1 | Out-Null

# Turn monitors back on
Write-Host "Turning monitors on..."
$monitor = @"
[DllImport("user32.dll", SetLastError = true)]
public static extern int SendMessage(int hWnd, int hMsg, int wParam, int lParam);
"@
Add-Type -MemberDefinition $monitor -Name 'User32' -Namespace 'Win32' -PassThru -ErrorAction SilentlyContinue | Out-Null
[Win32.User32]::SendMessage(-1, 0x0112, 0xf170, -1) | Out-Null

Write-Host "Headless mode disabled - normal operation restored!"
Write-Host ""
Write-Host "Status:"
Write-Host "   - Monitors: ON"
Write-Host "   - Monitor timeout: 10 minutes"
Write-Host "   - Sleep timeout: 30 minutes"
Write-Host "   - Hibernate: Never"
