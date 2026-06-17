@echo off
cd /d "%~dp0"
echo Starting Speak Selection... (close this window to quit, or use the tray)
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py speak_selection.py
) else (
    python speak_selection.py
)
echo.
echo ---- the app exited. if it crashed, the error is above ----
echo ---- and also saved in speak_selection.log ----
pause
