# Toggle-Headless.ps1 - Toggle between headless and normal mode
# Designed to be run via Scheduled Task with elevated privileges
# State is tracked in headless-state.json for external systems to read

param(
    [switch]$Enable,
    [switch]$Disable,
    [switch]$Status
)

$ScriptDir = $PSScriptRoot
$StateFile = Join-Path $ScriptDir "headless-state.json"
$LogFile = Join-Path $ScriptDir "headless.log"

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$timestamp - $Message" | Out-File -FilePath $LogFile -Append -Encoding UTF8
    Write-Host $Message
}

function Get-HeadlessState {
    if (Test-Path $StateFile) {
        try {
            $state = Get-Content $StateFile -Raw | ConvertFrom-Json
            return $state
        } catch {
            return @{ enabled = $false; lastChanged = $null }
        }
    }
    return @{ enabled = $false; lastChanged = $null }
}

function Set-HeadlessState {
    param([bool]$Enabled)
    $state = @{
        enabled = $Enabled
        lastChanged = (Get-Date -Format "o")
        hostname = $env:COMPUTERNAME
    }
    $state | ConvertTo-Json | Out-File -FilePath $StateFile -Encoding UTF8
    return $state
}

function Enable-HeadlessMode {
    Write-Log "Enabling Headless Mode..."

    # Disable all timeouts (0 = never)
    powercfg /change monitor-timeout-ac 0 | Out-Null
    powercfg /change disk-timeout-ac 0 | Out-Null
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null

    # Set performance mode
    powercfg /setactive 8c5e7fda-e8bf-45a6-a6cc-4b3c1234a2d8 2>&1 | Out-Null

    # Turn off monitors
    $monitor = @"
[DllImport("user32.dll", SetLastError = true)]
public static extern int SendMessage(int hWnd, int hMsg, int wParam, int lParam);
"@
    Add-Type -MemberDefinition $monitor -Name 'User32' -Namespace 'Win32' -PassThru -ErrorAction SilentlyContinue | Out-Null
    [Win32.User32]::SendMessage(-1, 0x0112, 0xf170, 2) | Out-Null

    $state = Set-HeadlessState -Enabled $true
    Write-Log "Headless mode ENABLED"
    return $state
}

function Disable-HeadlessMode {
    Write-Log "Disabling Headless Mode..."

    # Restore default timeouts
    powercfg /change monitor-timeout-ac 10 | Out-Null    # 10 min
    powercfg /change disk-timeout-ac 20 | Out-Null       # 20 min
    powercfg /change standby-timeout-ac 30 | Out-Null    # 30 min
    powercfg /change hibernate-timeout-ac 0 | Out-Null   # Never

    # Set balanced mode
    powercfg /setactive 381b4222-f694-41f0-9685-ff5bb260df2e 2>&1 | Out-Null

    # Turn monitors back on
    $monitor = @"
[DllImport("user32.dll", SetLastError = true)]
public static extern int SendMessage(int hWnd, int hMsg, int wParam, int lParam);
"@
    Add-Type -MemberDefinition $monitor -Name 'User32' -Namespace 'Win32' -PassThru -ErrorAction SilentlyContinue | Out-Null
    [Win32.User32]::SendMessage(-1, 0x0112, 0xf170, -1) | Out-Null

    $state = Set-HeadlessState -Enabled $false
    Write-Log "Headless mode DISABLED"
    return $state
}

# Main logic
$currentState = Get-HeadlessState

if ($Status) {
    # Just return current status
    Write-Host ($currentState | ConvertTo-Json)
    exit 0
}

if ($Enable) {
    $result = Enable-HeadlessMode
    Write-Host ($result | ConvertTo-Json)
    exit 0
}

if ($Disable) {
    $result = Disable-HeadlessMode
    Write-Host ($result | ConvertTo-Json)
    exit 0
}

# Default: Toggle based on current state
if ($currentState.enabled) {
    $result = Disable-HeadlessMode
} else {
    $result = Enable-HeadlessMode
}

Write-Host ($result | ConvertTo-Json)
