# Invoke-DeskflowBoot.ps1
# Wait for token-satellite to come up on localhost, then trigger the
# DeskFlow watchdog's phased recovery path.

param(
    [int]$DelaySeconds = 20,
    [int]$HealthTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$HealthUri = "http://localhost:7777/health"
$ControlUri = "http://localhost:7777/kvm/control"
$Body = @{ action = "reload" } | ConvertTo-Json -Compress

if ($DelaySeconds -gt 0) {
    Start-Sleep -Seconds $DelaySeconds
}

$deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    try {
        Invoke-RestMethod -Uri $HealthUri -TimeoutSec 3 | Out-Null
        Invoke-RestMethod `
            -Uri $ControlUri `
            -Method Post `
            -ContentType "application/json" `
            -Body $Body `
            -TimeoutSec 10 | Out-Null
        exit 0
    } catch {
        Start-Sleep -Seconds 3
    }
}

exit 0
