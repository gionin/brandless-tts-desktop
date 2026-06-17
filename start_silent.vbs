' Launches Speak Selection with no console window (windowless pyw).
' Double-click this to start the app silently.
Set sh = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "pyw """ & dir & "speak_selection.py""", 0, False
