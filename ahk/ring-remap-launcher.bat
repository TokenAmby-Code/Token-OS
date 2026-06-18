@echo off
:: ring-remap-launcher.bat
:: Local launcher for ring-remap.ahk - waits for the Windows-local cache before launching.
:: Point Task Scheduler at a LOCAL COPY of this file (e.g. C:\Scripts\ring-remap-launcher.bat),
:: not a network copy, so it can always be found on startup.

set "AHK_EXE=C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"
set "SCRIPT_PATH=C:\TokenOS\ahk\ring-remap.ahk"
set "RETRY_SECONDS=5"
set "MAX_RETRIES=60"

set /a ATTEMPT=0

:wait_loop
if exist "%SCRIPT_PATH%" goto :launch

set /a ATTEMPT+=1
if %ATTEMPT% geq %MAX_RETRIES% (
    exit /b 1
)

timeout /t %RETRY_SECONDS% /nobreak >nul
goto :wait_loop

:launch
start "" "%AHK_EXE%" "%SCRIPT_PATH%"
