@echo off
REM Stream Deck work-event button: clear newest pending enforcement ack.
REM Hits Token-API on Mac via Tailscale.
setlocal
set "API=http://100.95.109.23:7777"
set "LOG_DIR=%USERPROFILE%\.local\state\work-event"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG=%LOG_DIR%\%DATE:~10,4%-%DATE:~4,2%-%DATE:~7,2%.log"

for /f "tokens=*" %%i in ('curl -s "%API%/api/enforcement/status"') do set "STATUS=%%i"
echo %DATE% %TIME% ack: status=%STATUS% >> "%LOG%"

REM Extract first pending source+instance via PowerShell one-liner, then ack
powershell -NoProfile -Command ^
  "$s = Invoke-RestMethod -Uri '%API%/api/enforcement/status';" ^
  "if ($s.pending -and $s.pending.Count -gt 0) {" ^
  "  $p = $s.pending[0];" ^
  "  $body = @{ source = $p.source; instance_id = $p.instance_id } | ConvertTo-Json -Compress;" ^
  "  $r = Invoke-RestMethod -Uri '%API%/api/enforcement/ack' -Method POST -ContentType 'application/json' -Body $body;" ^
  "  Add-Content -Path '%LOG%' -Value (\"$(Get-Date -Format o) ack-resp: \" + ($r | ConvertTo-Json -Compress));" ^
  "} else {" ^
  "  Add-Content -Path '%LOG%' -Value \"$(Get-Date -Format o) ack: no pending\";" ^
  "}"
endlocal
