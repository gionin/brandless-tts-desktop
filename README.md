# Speak Selection (Desktop) — v1.0.0 "Polished core"

Brandless, fully offline text-to-speech for Windows 10. Select text anywhere,
press a mouse side button, hear it. Now with a settings window and saved
preferences.

## Install & run

```
install.bat     (once, to install dependencies via the py launcher)
run.bat         (each time; keeps the console open and logs to the log file)
```

tkinter ships with the standard python.org install, so there's nothing extra
to set up for the UI. To confirm: `py -c "import tkinter; print(tkinter.TkVersion)"`.

## Using it

- **Forward** side button reads the current selection. **Back** stops.
- Tray menu: **Settings...**, Speak clipboard (test), Stop, Swallow toggle, Quit.
- Double-clicking the tray icon opens Settings.

## Settings window

- **Voice** — pick any SAPI5 voice installed on the machine. Changing it speaks
  a short sample.
- **Speed** — 0.5x to 2.0x. Samples on release. (SAPI's rate is a coarse
  integer scale, so the slider snaps to the nearest step.)
- **Breathing room** — the gap (ms) before a new read starts when it interrupts
  playing audio.
- **Swallow side buttons** — when on, the side buttons only control speech and
  don't also do browser back/forward.
- **Read / Stop button** — assign which physical side button does which. If you
  pick the same button for both, the other one auto-swaps so they never clash.

Everything saves immediately. No Save button.

## Where settings (and the log) live

`%APPDATA%\SpeakSelection\`. `config.json` holds your preferences — delete it to
reset to defaults; it's seeded from defaults on first run, then the file wins.
`speak_selection.log` sits alongside it, rewritten on each launch. Keeping both
out of the program folder means the repo (and a packaged build) stay clean.

## Theme

The window reads your current Windows light/dark setting and matches it, and
re-checks while open so flipping the OS theme updates it within a couple
seconds.

## Known limits (by design, for now)

- Only clipboard **text** is preserved/restored during capture; images or file
  lists on the clipboard aren't brought back yet.
- Single voice selection only. Per-language auto-switching is a later milestone.
- No sentence chunking yet, so pause/resume/skip aren't here either.

## If something breaks

The app logs to `%APPDATA%\SpeakSelection\speak_selection.log`, overwritten each
run. `run.bat` also keeps the console open. Send the bottom of either if you hit
a problem.

## Note on where this was built

Written in a Linux sandbox and not run there (it needs Windows: SAPI5, the
global mouse hook, Win32 clipboard, the registry theme read). Syntax is
verified and the pure-logic pieces (speed mapping, config loading) were tested.
You're the one running it on real Windows.

---

## v1.0.1 — runs windowless, starts with Windows, swallow fixed

### Start with Windows (no console)
- `install_startup.bat` — registers the app to launch at every login using
  `pyw` (the windowless Python launcher, so no console window), and starts it
  immediately. Look for the tray icon.
- `uninstall_startup.bat` — removes that startup entry.
- `start_silent.vbs` — double-click to start the app right now with no console
  window, without touching startup registration.

`run.bat` still exists for debugging: it runs with a visible console so you can
watch output live.

### Where did the console text go?
Nowhere — it's still logged. Open the tray menu and choose **Show log...** to
see the live log inside a window. It also still writes to the log file in
`%APPDATA%\SpeakSelection\`.

### Side buttons were still navigating — fixed
The button "swallow" now uses our own low-level mouse hook rather than the
input library's suppression, which wasn't reliably blocking the X buttons. With
"Swallow side buttons" on (the default), the side buttons no longer trigger
browser back/forward. Turn it off in Settings if you want them to do both.

### Single instance
Only one copy runs at a time. Launching it again (via the tray, the .vbs, or
at login) won't start a second copy — it pokes the running one to open its
Settings window and then exits, the way Steam re-focuses instead of opening
twice. This uses a named mutex plus a named event (local OS objects, no network
and no firewall prompts). If the app ever crashes, the lock frees itself when
the process ends, so there's no stale-lock problem.

---

## Building a standalone .exe

`build.bat` packages everything into a single windowless executable so it can
run on a machine **without Python installed**.

```
build.bat
```

It installs PyInstaller and the runtime deps, renders the tray glyph into a
proper multi-resolution `.ico` for the executable, then produces
`dist\SpeakSelection.exe` (one file, no console). The intermediate `build\`,
`dist\`, `*.spec`, and the temporary icon are all git-ignored.

Double-click `dist\SpeakSelection.exe` to run it like any other app — it behaves
exactly as running from source, with config and the log living in
`%APPDATA%\SpeakSelection\`. To start it at login, point a shortcut in your
Startup folder at the exe (the `install_startup.bat` route is for running from
source under `pyw`).
