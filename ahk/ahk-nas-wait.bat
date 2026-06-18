@echo off
setlocal EnableExtensions EnableDelayedExpansion
:: Generic AHK launcher for cached startup scripts.
:: Usage: ahk-nas-wait.bat <script>
::   script-compiler       -> C:\TokenOS\ahk\script-compiler.ahk
::   ring-remap            -> C:\TokenOS\ahk\ring-remap.ahk
::   C:\full\path\foo.ahk  -> used as-is
::   Civic\foo.ahk         -> \\Token-NAS\Civic\foo.ahk
::   \\full\path\bar.ahk   -> used as-is

set "AHK=C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"
set "NAS=\\Token-NAS"
set "AHK_DIR=C:\TokenOS\ahk"
set "RETRIES=60"
set "DELAY=5"
set "INPUT=%~1"
set "LOG_DIR=%USERPROFILE%\Imperium-Startup\logs"
set "LOG=%LOG_DIR%\ahk-nas-wait.log"

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" >nul 2>&1

if "%INPUT%"=="" (
    echo Usage: ahk-nas-wait.bat ^<script^>
    exit /b 1
)

if /i not "%INPUT:~-4%"==".ahk" set "INPUT=%INPUT%.ahk"

if /i "%INPUT%"=="ring-remap.ahk" (
    if exist "%USERPROFILE%\Imperium-Startup\ring-remap.ahk" (
        set "SCRIPT=%USERPROFILE%\Imperium-Startup\ring-remap.ahk"
    )
)

if not defined SCRIPT (
    if "%INPUT:~1,1%"==":" (
        set "SCRIPT=%INPUT%"
    )
)

if not defined SCRIPT (
    if "%INPUT:~0,2%"=="\\" (
        set "SCRIPT=%INPUT%"
    )
)

if not defined SCRIPT (
    if not "%INPUT:\=%"=="%INPUT%" (
        set "SCRIPT=%NAS%\%INPUT%"
    ) else (
        set "SCRIPT=%AHK_DIR%\%INPUT%"
    )
)

for /L %%i in (1,1,%RETRIES%) do (
    if exist "%SCRIPT%" (
        echo [!date! !time!] launching "%SCRIPT%" >> "%LOG%"
        start "" "%AHK%" "%SCRIPT%"
        exit /b 0
    )
    echo [!date! !time!] waiting %%i/%RETRIES% for "%SCRIPT%" >> "%LOG%"
    timeout /t %DELAY% /nobreak >nul
)

echo [!date! !time!] missing after wait; not launching "%SCRIPT%" >> "%LOG%"
exit /b 1
