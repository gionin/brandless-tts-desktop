@echo off
cd /d "%~dp0"
echo Installing dependencies for Speak Selection...
echo.
where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install -r requirements.txt
) else (
    python -m pip install -r requirements.txt
)
echo.
echo ---- done. if you saw errors above, copy them. ----
pause
