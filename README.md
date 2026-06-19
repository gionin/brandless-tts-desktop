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
  a short sample. With auto-switch on, this is the fallback voice.
- **Auto-switch voice by detected language** — when on, the app detects the
  language of the captured text and speaks it with an installed voice whose
  language matches (e.g. an English selection uses an English voice, a
  Portuguese one a Portuguese voice). If no installed voice matches, it falls
  back to the **Voice** picked above. Detection is fully offline (langdetect).
  - **detect per sentence** — sub-option: when on, each sentence is detected and
    voiced independently (good for mixed-language text, but short sentences
    detect less reliably). When off (default), the whole selection is detected
    once and read with a single voice.
- **Preferred voices** — a list of voices to use for auto-switching, one per
  language. Add a voice (it previews, and replaces any existing pick for the
  same language) or remove one. When auto-switch detects a language you have a
  preferred voice for, it uses that voice; otherwise it falls back to the first
  installed voice for the language, then to the **Voice** chosen above.
- **Speed** — 0.5x to 2.0x. Samples on release. (SAPI's rate is a coarse
  integer scale, so the slider snaps to the nearest step.)
- **Breathing room** — the gap (ms) before a new read starts when it interrupts
  playing audio.
- **Swallow side buttons** — when on, the side buttons only control speech and
  don't also do browser back/forward.
- **Highlight (underline) each word while reading** — see "Word highlighting"
  below. Off by default.
- **Read / Stop button** — assign which physical side button does which. If you
  pick the same button for both, the other one auto-swaps so they never clash.

Everything saves immediately. No Save button.

## Word highlighting while reading

With this on (Settings checkbox or the tray menu), a thin underline bar follows
the word being spoken — in **any** app, including browsers and PDF readers.

**Appearance (Settings):** colour, underline thickness, opacity, and a vertical
**offset** for fine-tuning where the underline sits. Each word's underline is
snapped to its text line's average top/height, so the bar stays straight even
though OCR reports slightly different boxes per word.

The overlay is click-through (it never intercepts your mouse): the transparent
style is applied to the real top-level window via `GetAncestor(GA_ROOT)`, which
an earlier version got wrong. A watchdog also auto-hides the bar if updates stop,
so it can't get stuck on screen. (`HIGHLIGHT_OVERLAY_ENABLED` in
`speak_selection.py` is the master switch.)

It works by screenshotting the source window when you start a read, using
Windows' built-in OCR (`winocr`) to find where each word is on screen, and
matching that to the text being spoken. Because it reads pixels, it isn't
limited to apps that expose their text — but it inherits OCR's limits:

- **Occasional skips.** If OCR misses a word, the bar holds on the previous word
  until the next word it did detect.
- **On-screen text only.** Only what's visible when the read starts is tracked;
  scrolling mid-read isn't followed (yet).
- **Best at 100% display scaling.** Alignment is calibrated for physical pixels;
  other scales may drift slightly.
- Needs `winocr` (in `requirements.txt`). Without it, highlighting simply does
  nothing and reading is unaffected.

## Where settings (and the log) live

`%APPDATA%\SpeakSelection\`. `config.json` holds your preferences — delete it to
reset to defaults; it's seeded from defaults on first run, then the file wins.
`speak_selection.log` sits alongside it, rewritten on each launch. Keeping both
out of the program folder means the repo (and a packaged build) stay clean.

## Theme

The window reads your current Windows light/dark setting and matches it, and
re-checks while open so flipping the OS theme updates it within a couple
seconds.

## Getting more (and more natural) voices

The app speaks through any voice registered with **SAPI5**, so the way to get
better voices is to make more of them visible to SAPI — no app changes needed.

- **Windows natural (neural) voices** sound far better than the classic ones but
  are normally locked to Narrator. Install them under **Settings → Accessibility
  → Narrator → Add natural voices** (or **Time & Language → Speech**). They're
  local and run offline.
- To expose those (and the older OneCore voices) to SAPI apps like this one,
  install **[NaturalVoiceSAPIAdapter](https://github.com/gexgd0419/NaturalVoiceSAPIAdapter)**:
  download a release, run `Installer.exe` as administrator, and on 64-bit Windows
  install **both** the 32-bit and 64-bit versions. It can also enable free
  Microsoft Edge online voices (these need internet).

Once installed, the new voices show up in the **Voice** dropdown and are
eligible for auto language switching automatically. Restart the app after
installing voices so it re-reads the list.

## Known limits (by design, for now)

- Capture preserves the **whole** clipboard — text, images, and copied files
  are all snapshotted before the synthetic copy and restored after. The restore
  is marked to opt out of **clipboard history** and cloud sync, so restoring
  your image/file doesn't create a duplicate entry in Win+V. The only exceptions
  are formats published solely as a GDI handle (a bitmap/metafile with no
  accompanying `CF_DIB`) or delayed-render data that fails to materialize — both
  rare. Note: the synthetic copy itself still adds the *selected text* to
  clipboard history (that copy is done by the source app, outside our control).
- Text is now split into sentence-sized chunks before speaking (cleaner pacing
  and steadier playback of long selections), but pause/resume/skip aren't
  wired up — chunks are fed straight to SAPI's own queue.

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

## Running the tests

The Windows-independent logic (sentence chunking, speed mapping, config
loading) has unit tests that run on any platform — no SAPI, mouse hook, or
display needed.

```
py -m pip install -r requirements-dev.txt
py -m pytest
```

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
