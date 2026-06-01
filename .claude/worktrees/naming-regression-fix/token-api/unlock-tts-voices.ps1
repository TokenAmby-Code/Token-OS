# Unlock Windows OneCore TTS Voices for SAPI
# Run this script as Administrator
#
# This copies voice registry entries from Speech_OneCore to Speech,
# making them available to any SAPI-compatible application.
#
# Sources:
# - https://www.ghacks.net/2018/08/11/unlock-all-windows-10-tts-voices-system-wide-to-get-more-of-them/
# - https://github.com/jonelo/unlock-win-tts-voices

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

$OneCorePath = "HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens"
$SAPIPath = "HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens"
$WOW64Path = "HKLM:\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens"

Write-Host "=== Windows TTS Voice Unlocker ===" -ForegroundColor Cyan
Write-Host ""

# Check if running as admin
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: This script must be run as Administrator!" -ForegroundColor Red
    Write-Host "Right-click PowerShell and select 'Run as Administrator'" -ForegroundColor Yellow
    exit 1
}

# Get current SAPI voices
Write-Host "Current SAPI voices:" -ForegroundColor Yellow
$existingVoices = Get-ChildItem $SAPIPath | Select-Object -ExpandProperty PSChildName
$existingVoices | ForEach-Object { Write-Host "  - $_" -ForegroundColor Gray }
Write-Host ""

# Get OneCore voices
Write-Host "Available OneCore voices:" -ForegroundColor Yellow
$oneCoreVoices = Get-ChildItem $OneCorePath | Select-Object -ExpandProperty PSChildName
$oneCoreVoices | ForEach-Object { Write-Host "  - $_" -ForegroundColor Gray }
Write-Host ""

# Find voices to copy (not already in SAPI)
$voicesToCopy = $oneCoreVoices | Where-Object { $existingVoices -notcontains $_ }

if ($voicesToCopy.Count -eq 0) {
    Write-Host "All OneCore voices are already available in SAPI!" -ForegroundColor Green
    exit 0
}

Write-Host "Voices to unlock: $($voicesToCopy.Count)" -ForegroundColor Cyan
$voicesToCopy | ForEach-Object { Write-Host "  + $_" -ForegroundColor Green }
Write-Host ""

# Copy each voice
$copied = 0
$failed = 0

foreach ($voice in $voicesToCopy) {
    $sourcePath = Join-Path $OneCorePath $voice

    try {
        # Copy to SAPI path
        $destPath = Join-Path $SAPIPath $voice
        if (-not (Test-Path $destPath)) {
            Copy-Item -Path $sourcePath -Destination $SAPIPath -Recurse -Force
            Write-Host "  [SAPI] Copied: $voice" -ForegroundColor Green
        }

        # Copy to WOW64 path (for 32-bit apps)
        $destPath64 = Join-Path $WOW64Path $voice
        if (-not (Test-Path $destPath64)) {
            Copy-Item -Path $sourcePath -Destination $WOW64Path -Recurse -Force
            Write-Host "  [WOW64] Copied: $voice" -ForegroundColor Green
        }

        $copied++
    }
    catch {
        Write-Host "  [FAIL] $voice : $($_.Exception.Message)" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
Write-Host "Copied: $copied voices" -ForegroundColor Green
if ($failed -gt 0) {
    Write-Host "Failed: $failed voices" -ForegroundColor Red
}

Write-Host ""
Write-Host "Verifying new SAPI voices:" -ForegroundColor Yellow
$newVoices = Get-ChildItem $SAPIPath | Select-Object -ExpandProperty PSChildName
$newVoices | ForEach-Object { Write-Host "  - $_" -ForegroundColor Gray }

Write-Host ""
Write-Host "Done! Restart any applications using TTS to see new voices." -ForegroundColor Cyan
