@echo off
REM Stream Deck work-event button: log infra_work + opportunistic ack-clear.
setlocal
set "API=http://100.95.109.23:7777"
set "LOG_DIR=%USERPROFILE%\.local\state\work-event"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG=%LOG_DIR%\%DATE:~10,4%-%DATE:~4,2%-%DATE:~7,2%.log"

powershell -NoProfile -Command ^
  "$evt = @{ event_type = 'work_event'; details = @{ kind = 'infra_work'; source = 'stream_deck' } } | ConvertTo-Json -Compress;" ^
  "$r = Invoke-RestMethod -Uri '%API%/api/events/log' -Method POST -ContentType 'application/json' -Body $evt;" ^
  "Add-Content -Path '%LOG%' -Value (\"$(Get-Date -Format o) infra_work: \" + ($r | ConvertTo-Json -Compress));" ^
  "$s = Invoke-RestMethod -Uri '%API%/api/enforcement/status';" ^
  "if ($s.pending -and $s.pending.Count -gt 0) {" ^
  "  $p = $s.pending[0];" ^
  "  $body = @{ source = $p.source; instance_id = $p.instance_id } | ConvertTo-Json -Compress;" ^
  "  $a = Invoke-RestMethod -Uri '%API%/api/enforcement/ack' -Method POST -ContentType 'application/json' -Body $body;" ^
  "  Add-Content -Path '%LOG%' -Value (\"$(Get-Date -Format o) infra_work-ack: \" + ($a | ConvertTo-Json -Compress));" ^
  "}"
endlocal
