@echo off
setlocal
cd /d "%~dp0"

REM Pick the Python launcher (py preferred, else python).
set "PY=py"
where py >nul 2>nul || set "PY=python"

echo Building SpeakSelection.exe with %PY% ...
echo.

echo [1/4] Ensuring PyInstaller and runtime deps are installed...
%PY% -m pip install --quiet --upgrade pyinstaller -r requirements.txt
if errorlevel 1 (
    echo Failed to install build dependencies. See the errors above.
    pause
    exit /b 1
)

echo [2/4] Rendering the app icon from the tray drawing code...
%PY% -c "import speak_selection as s; s.make_icon_image().resize((256,256)).save('build_icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if errorlevel 1 (
    echo Could not render the icon. Building without a custom icon.
    set "ICON_ARG="
) else (
    set "ICON_ARG=--icon build_icon.ico"
)

echo [3/4] Running PyInstaller (one-file, windowless)...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name SpeakSelection %ICON_ARG% speak_selection.py
if errorlevel 1 (
    echo Build failed. See the errors above.
    if exist build_icon.ico del build_icon.ico
    pause
    exit /b 1
)

echo [4/4] Copying the exe to the folder and cleaning up...
copy /y "dist\SpeakSelection.exe" "SpeakSelection.exe" >nul
if errorlevel 1 (
    echo Could not copy the exe out of dist\. Leaving the build folders in
    echo place so nothing is lost; the exe is still at dist\SpeakSelection.exe.
    if exist build_icon.ico del build_icon.ico
    pause
    exit /b 1
)

REM Copy succeeded, so the scratch is safe to remove.
if exist build rd /s /q build
if exist dist rd /s /q dist
if exist SpeakSelection.spec del SpeakSelection.spec
if exist build_icon.ico del build_icon.ico
if exist __pycache__ rd /s /q __pycache__

echo.
echo ---- Done. The standalone app is: SpeakSelection.exe (in this folder) ----
echo It needs no Python install on the target machine. Config and the log
echo live in %%APPDATA%%\SpeakSelection.
pause
