# Remove the legacy Bonjour Winsock namespace provider that Windows LSA
# protection blocks from loading into LSASS.
# RUN THIS SCRIPT AS ADMINISTRATOR on Windows.

if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Must run as Administrator to repair Bonjour/LSA block"
    Write-Host "Right-click PowerShell and select 'Run as administrator', then run this script again."
    exit 1
}

$ErrorActionPreference = "Stop"

$LogRoot = Join-Path $env:USERPROFILE "Imperium-Startup\logs"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$LogPath = Join-Path $LogRoot "repair-bonjour-lsa-block.log"
Start-Transcript -Path $LogPath -Append | Out-Null
try {

function Get-BonjourInstall {
    Get-ItemProperty `
        HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*, `
        HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\* `
        -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -eq "Bonjour" } |
        Select-Object -First 1
}

function Get-MdnsNamespaceProviders {
    netsh winsock show catalog | Select-String -Pattern "mdnsNSP|Bonjour|mDNS" -Context 4,6
}

Write-Host "Before:"
Get-Service -Name "Bonjour Service","mDNSResponder" -ErrorAction SilentlyContinue |
    Format-Table Name,Status,StartType -AutoSize
Get-MdnsNamespaceProviders

$bonjour = Get-BonjourInstall
if (-not $bonjour) {
    Write-Host "Bonjour is not registered as an installed product."
} else {
    if ($bonjour.UninstallString -notmatch "\{[0-9A-Fa-f-]{36}\}") {
        throw "Could not extract Bonjour MSI product code from: $($bonjour.UninstallString)"
    }

    $productCode = $Matches[0]
    Write-Host "Uninstalling Bonjour $($bonjour.DisplayVersion) ($productCode)..."

    $service = Get-Service -Name "Bonjour Service","mDNSResponder" -ErrorAction SilentlyContinue
    if ($service) {
        $service | Where-Object { $_.Status -ne "Stopped" } | Stop-Service -Force -ErrorAction SilentlyContinue
    }

    $process = Start-Process -FilePath "msiexec.exe" -ArgumentList @("/x", $productCode, "/qn", "/norestart") -Wait -PassThru
    if ($process.ExitCode -ne 0 -and $process.ExitCode -ne 3010) {
        throw "Bonjour uninstall failed with msiexec exit code $($process.ExitCode)"
    }
}

Write-Host ""
Write-Host "After:"
Get-Service -Name "Bonjour Service","mDNSResponder" -ErrorAction SilentlyContinue |
    Format-Table Name,Status,StartType -AutoSize
$remainingProviders = Get-MdnsNamespaceProviders
if ($remainingProviders) {
    Write-Host "mdnsNSP is still present in the Winsock catalog. Reboot first; if it remains, run 'netsh winsock reset' as Administrator and reboot again."
    $remainingProviders
    exit 2
}

Write-Host "Bonjour mdnsNSP Winsock providers are gone. Reboot once to clear any already-loaded state."
} finally {
    Stop-Transcript | Out-Null
}
