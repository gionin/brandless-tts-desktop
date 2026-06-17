@echo off
setlocal
cd /d "%~dp0"
set "SCRIPT=%~dp0speak_selection.py"
set "PYW="
for /f "delims=" %%i in ('where pyw 2^>nul') do (
    set "PYW=%%i"
    goto :found
)
:found
if "%PYW%"=="" (
    echo Could not find pyw.exe ^(the windowless Python launcher^).
    echo Make sure Python from python.org is installed, then try again.
    pause
    exit /b 1
)

echo Registering Speak Selection to start with Windows...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v SpeakSelection /t REG_SZ /d "\"%PYW%\" \"%SCRIPT%\"" /f
if errorlevel 1 (
    echo Failed to write the startup entry.
    pause
    exit /b 1
)

echo Done. It will launch automatically at every login.
echo Starting it now ^(no window will appear; look for the tray icon^)...
start "" "%PYW%" "%SCRIPT%"
echo.
echo You can close this window.
pause
