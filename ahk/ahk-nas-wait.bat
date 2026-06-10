@echo off
:: Generic NAS-wait launcher for AHK scripts.
:: Usage: ahk-nas-wait.bat <script>
::   script-compiler       -> \\Token-NAS\Imperium\runtimes\token-os\live\ahk\script-compiler.ahk
::   ring-remap            -> \\Token-NAS\Imperium\runtimes\token-os\live\ahk\ring-remap.ahk
::   Civic\foo.ahk         -> \\Token-NAS\Civic\foo.ahk
::   \\full\path\bar.ahk   -> used as-is

set "AHK=C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"
set "NAS=\\Token-NAS"
set "AHK_DIR=%NAS%\Imperium\runtimes\token-os\live\ahk"
set "RETRIES=15"
set "DELAY=3"
set "INPUT=%~1"

if "%INPUT%"=="" (
    echo Usage: ahk-nas-wait.bat ^<script^>
    exit /b 1
)

echo %INPUT% | findstr /i "\.ahk$" >nul || set "INPUT=%INPUT%.ahk"

echo %INPUT% | findstr "\\" >nul
if errorlevel 1 (
    set "SCRIPT=%AHK_DIR%\%INPUT%"
) else (
    echo %INPUT% | findstr /b "\\\\" >nul
    if errorlevel 1 (
        set "SCRIPT=%NAS%\%INPUT%"
    ) else (
        set "SCRIPT=%INPUT%"
    )
)

for /L %%i in (1,1,%RETRIES%) do (
    if exist "%SCRIPT%" (
        start "" "%AHK%" "%SCRIPT%"
        exit /b 0
    )
    timeout /t %DELAY% /nobreak >nul
)

:: Final attempt even if the existence check failed.
start "" "%AHK%" "%SCRIPT%"
