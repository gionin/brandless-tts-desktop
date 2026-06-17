@echo off
echo Removing Speak Selection from Windows startup...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v SpeakSelection /f
echo Done. (This does not close the app if it is currently running;
echo use Quit in the tray menu for that.)
pause
