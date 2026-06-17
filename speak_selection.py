"""
Speak Selection (Desktop) - v1.0.1
A brandless, fully offline text-to-speech utility for Windows 10.

Core loop:
  - Lives in the system tray.
  - Mouse FORWARD side button reads the current selection; BACK stops.
  - Captures the selection via synthetic Ctrl+C, then restores the prior
    clipboard text.
  - Breathing room: a new read while audio plays stops it, waits a short
    delay, then speaks. Explicit stop is always instant.
  - Side buttons swallowed by default so they don't also do browser nav.

Settings (v1.0.0): voice picker, speed, breathing-room delay, swallow toggle,
read/stop button assignment. Persisted to %APPDATA%\\SpeakSelection\\config.json.
Window follows Windows light/dark.

New in v1.0.1:
  - The side-button "swallow" now uses our own low-level WH_MOUSE_LL hook
    instead of pynput's suppression, which was unreliable for the X buttons.
    Swallowed buttons no longer trigger browser back/forward.
  - Built to run windowless under pyw (no console). Logging still works:
    a "Show log..." tray item opens a window with the live log inside it.

Engine: SAPI5 via comtypes. Mouse hook + clipboard via ctypes. pynput is used
only to synthesize Ctrl+C.

Run normally (no console):  double-click start_silent.vbs, or install_startup.bat
Run with a console (debug): run.bat
"""

import collections
import json
import logging
import math
import os
import queue
import sys
import threading
import time

import tkinter as tk
from tkinter import ttk

# Third-party (see requirements.txt): comtypes, pynput, pystray, Pillow

# ----------------------------------------------------------------------------
# Defaults (used on first launch; the config file wins thereafter)
# ----------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "voice_id": None,
    "speed": 1.0,
    "breathing_room_ms": 350,
    "read_button": 2,           # 2 = forward (XBUTTON2), 1 = back (XBUTTON1)
    "stop_button": 1,
    "swallow_side_buttons": True,
}

CAPTURE_TIMEOUT_MS = 400
CAPTURE_POLL_MS = 10
SAMPLE_TEXT = "This is the selected voice."

# ----------------------------------------------------------------------------

log = logging.getLogger("speak_selection")

# In-memory log buffer shown in the in-app log window.
LOG_BUFFER = collections.deque(maxlen=1000)


class BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


# Windows constants
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_XBUTTONDBLCLK = 0x020D
WH_MOUSE_LL = 14
WM_QUIT = 0x0012

SVSF_ASYNC = 1
SVSF_PURGE = 2
SRSE_IS_SPEAKING = 2

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

BTN_FORWARD = 2
BTN_BACK = 1
BTN_LABELS = {BTN_FORWARD: "Forward (front side button)",
            BTN_BACK: "Back (rear side button)"}


# ============================================================================
# Config
# ============================================================================

def _config_dir():
    base = os.getenv("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "SpeakSelection")


def _config_path():
    return os.path.join(_config_dir(), "config.json")


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULT_CONFIG:
                if k in data:
                    cfg[k] = data[k]
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("config unreadable; using defaults")
    return cfg


def speed_to_rate(speed):
    speed = max(0.5, min(2.0, float(speed)))
    rate = round(10.0 * math.log(speed, 3.0))
    return int(max(-10, min(10, rate)))


# ============================================================================
# Win32 clipboard via ctypes
# ============================================================================

class _Win32:
    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = _Win32()
        return cls._instance

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.k32 = ctypes.windll.kernel32
        self.u32 = ctypes.windll.user32
        u32, k32 = self.u32, self.k32

        u32.OpenClipboard.argtypes = [wintypes.HWND]
        u32.OpenClipboard.restype = wintypes.BOOL
        u32.CloseClipboard.argtypes = []
        u32.CloseClipboard.restype = wintypes.BOOL
        u32.EmptyClipboard.argtypes = []
        u32.EmptyClipboard.restype = wintypes.BOOL
        u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        u32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        u32.GetClipboardData.argtypes = [wintypes.UINT]
        u32.GetClipboardData.restype = wintypes.HANDLE
        u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u32.SetClipboardData.restype = wintypes.HANDLE
        u32.GetClipboardSequenceNumber.argtypes = []
        u32.GetClipboardSequenceNumber.restype = wintypes.DWORD

        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.restype = wintypes.BOOL

    def _open(self, attempts=8, delay=0.01):
        for _ in range(attempts):
            if self.u32.OpenClipboard(None):
                return True
            time.sleep(delay)
        return False

    def get_text(self):
        if not self._open():
            return None
        try:
            if not self.u32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return None
            handle = self.u32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = self.k32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return self.ctypes.wstring_at(ptr)
            finally:
                self.k32.GlobalUnlock(handle)
        finally:
            self.u32.CloseClipboard()

    def set_text(self, text):
        if text is None:
            text = ""
        if not self._open():
            return False
        try:
            self.u32.EmptyClipboard()
            buf = self.ctypes.create_unicode_buffer(text)
            size = self.ctypes.sizeof(buf)
            handle = self.k32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            ptr = self.k32.GlobalLock(handle)
            if not ptr:
                return False
            self.ctypes.memmove(ptr, buf, size)
            self.k32.GlobalUnlock(handle)
            self.u32.SetClipboardData(CF_UNICODETEXT, handle)
            return True
        finally:
            self.u32.CloseClipboard()

    def empty(self):
        if not self._open():
            return
        try:
            self.u32.EmptyClipboard()
        finally:
            self.u32.CloseClipboard()

    def seq(self):
        return self.u32.GetClipboardSequenceNumber()


# ============================================================================
# Low-level global mouse hook (our own WH_MOUSE_LL), so we can reliably
# swallow the side buttons. Runs on a dedicated thread with a message loop,
# which low-level hooks require.
# ============================================================================

class MouseHook:
    def __init__(self, on_button_down, should_suppress):
        # on_button_down(xbtn:int), should_suppress(xbtn:int)->bool
        self.on_button_down = on_button_down
        self.should_suppress = should_suppress
        self._thread = None
        self._thread_id = None
        self._hook = None
        self._proc = None  # keep the CFUNCTYPE alive

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mousehook")
        self._thread.start()

    def _run(self):
        import ctypes
        from ctypes import wintypes

        LRESULT = ctypes.c_ssize_t
        WPARAM = ctypes.c_size_t
        LPARAM = ctypes.c_ssize_t

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", POINT),
                        ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32

        u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                        ctypes.c_void_p, wintypes.DWORD]
        u32.SetWindowsHookExW.restype = ctypes.c_void_p
        u32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                    WPARAM, LPARAM]
        u32.CallNextHookEx.restype = LRESULT
        u32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u32.UnhookWindowsHookEx.restype = wintypes.BOOL
        u32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    wintypes.UINT, wintypes.UINT]
        u32.GetMessageW.restype = ctypes.c_int
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        k32.GetModuleHandleW.restype = ctypes.c_void_p

        self._thread_id = k32.GetCurrentThreadId()

        def proc(nCode, wParam, lParam):
            try:
                if nCode == 0 and int(wParam) in (WM_XBUTTONDOWN, WM_XBUTTONUP,
                                                WM_XBUTTONDBLCLK):
                    ms = MSLLHOOKSTRUCT.from_address(lParam)
                    xbtn = (ms.mouseData >> 16) & 0xFFFF
                    if int(wParam) == WM_XBUTTONDOWN:
                        try:
                            self.on_button_down(xbtn)
                        except Exception:
                            log.exception("button handler error")
                    if self.should_suppress(xbtn):
                        return 1  # eat the event; app/browser never sees it
            except Exception:
                log.exception("hook proc error")
            return u32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc = HOOKPROC(proc)
        self._hook = u32.SetWindowsHookExW(WH_MOUSE_LL, self._proc,
                                        k32.GetModuleHandleW(None), 0)
        if not self._hook:
            log.error("failed to install mouse hook (GetLastError=%s)",
                    ctypes.get_last_error())
            return

        log.info("mouse hook installed")
        msg = wintypes.MSG()
        while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        u32.UnhookWindowsHookEx(self._hook)
        log.info("mouse hook removed")

    def stop(self):
        if self._thread_id:
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                            ctypes.c_size_t, ctypes.c_ssize_t]
            u32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


# ============================================================================
# Single instance: a named mutex detects an already-running copy, and a named
# event lets a second launch poke the first one to open its window, then exit.
# Pure local OS objects, no sockets, no network.
# ============================================================================

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF


class SingleInstance:
    MUTEX_NAME = "SpeakSelectionSingleInstanceMutex"
    EVENT_NAME = "SpeakSelectionShowWindowEvent"

    def __init__(self):
        import ctypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._mutex = None
        self._event = None
        self.already_running = False

    def acquire(self):
        """Try to become the primary instance. Returns True if we are it."""
        import ctypes
        k = self.k32
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.CreateMutexW.restype = ctypes.c_void_p
        self._mutex = k.CreateMutexW(None, False, self.MUTEX_NAME)
        err = ctypes.get_last_error()
        self.already_running = (err == ERROR_ALREADY_EXISTS)
        return not self.already_running

    def _open_event(self):
        import ctypes
        k = self.k32
        k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_wchar_p]
        k.CreateEventW.restype = ctypes.c_void_p
        return k.CreateEventW(None, False, False, self.EVENT_NAME)

    def signal_existing(self):
        """Second instance: wake the primary one, then we can exit."""
        import ctypes
        k = self.k32
        h = self._open_event()
        if h:
            k.SetEvent.argtypes = [ctypes.c_void_p]
            k.SetEvent.restype = ctypes.c_int
            k.SetEvent(h)
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            k.CloseHandle(h)

    def start_listener(self, on_signal):
        """Primary instance: wait for pokes from later launches."""
        import ctypes
        self._event = self._open_event()
        if not self._event:
            log.warning("could not create show-window event")
            return
        k = self.k32
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k.WaitForSingleObject.restype = ctypes.c_uint

        def loop():
            while True:
                r = k.WaitForSingleObject(self._event, INFINITE)
                if r == WAIT_OBJECT_0:
                    try:
                        on_signal()
                    except Exception:
                        log.exception("show-window signal handler failed")
                else:
                    time.sleep(0.5)

        threading.Thread(target=loop, daemon=True,
                        name="singleinstance").start()


# ============================================================================
# Theme
# ============================================================================

def read_dark_mode():
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        finally:
            winreg.CloseKey(key)
        return val == 0
    except Exception:
        return False


def make_icon_image():
    """The tray glyph: a rounded square with a play triangle. Module-level so
    the build script can reuse it to render the packaged .exe icon."""
    from PIL import Image, ImageDraw
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    gray = (136, 136, 136, 255)
    d.rounded_rectangle([6, 6, size - 6, size - 6], radius=12,
                        outline=gray, width=4)
    d.polygon([(26, 22), (26, 42), (44, 32)], fill=gray)
    return img


def palette(dark):
    if dark:
        return dict(bg="#2b2b2b", fg="#e8e8e8", field="#3c3c3c",
                    sel="#3d5a80", trough="#1f1f1f", border="#555555")
    return dict(bg="#f0f0f0", fg="#1a1a1a", field="#ffffff",
                sel="#cce4ff", trough="#d9d9d9", border="#bcbcbc")


# ============================================================================
# The app
# ============================================================================

class SpeakSelectionApp:
    def __init__(self):
        cfg = load_config()
        self.voice_id = cfg["voice_id"]
        self.speed = float(cfg["speed"])
        self.breathing_room_ms = int(cfg["breathing_room_ms"])
        self.read_button = int(cfg["read_button"])
        self.stop_button = int(cfg["stop_button"])
        self.swallow = threading.Event()
        if cfg["swallow_side_buttons"]:
            self.swallow.set()

        self.speech_q = queue.Queue()
        self.capture_q = queue.Queue()
        self.ui_q = queue.Queue()

        self.hook = None
        self.icon = None
        self.root = None
        self.style = None
        self.settings_win = None
        self.log_win = None
        self._log_text = None
        self._log_len = -1
        self._cur_dark = None
        self._tick_count = 0

        self._voice_combo = None
        self._voice_items = []
        self._speed_var = None
        self._speed_label = None
        self._breath_var = None
        self._breath_label = None
        self._swallow_var = None
        self._read_combo = None
        self._stop_combo = None

        from pynput.keyboard import Controller as KbController
        self.kb = KbController()

    # ----- config -------------------------------------------------------

    def save_config(self):
        data = {
            "voice_id": self.voice_id,
            "speed": round(self.speed, 3),
            "breathing_room_ms": int(self.breathing_room_ms),
            "read_button": int(self.read_button),
            "stop_button": int(self.stop_button),
            "swallow_side_buttons": self.swallow.is_set(),
        }
        try:
            os.makedirs(_config_dir(), exist_ok=True)
            tmp = _config_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, _config_path())
        except Exception:
            log.exception("could not save config")

    # ----- capture ------------------------------------------------------

    def _capture_selection(self):
        from pynput.keyboard import Key

        win = _Win32.get()
        old_seq = win.seq()
        old_text = win.get_text()

        self.kb.press(Key.ctrl)
        self.kb.press('c')
        time.sleep(0.02)
        self.kb.release('c')
        self.kb.release(Key.ctrl)

        deadline = time.monotonic() + (CAPTURE_TIMEOUT_MS / 1000.0)
        changed = False
        while time.monotonic() < deadline:
            if win.seq() != old_seq:
                changed = True
                break
            time.sleep(CAPTURE_POLL_MS / 1000.0)

        if not changed:
            return None

        new_text = win.get_text()
        if old_text is not None:
            win.set_text(old_text)
        else:
            win.empty()

        if not new_text or not new_text.strip():
            return None
        return new_text

    def _capture_worker(self):
        while True:
            item = self.capture_q.get()
            if item == "QUIT":
                break
            try:
                text = self._capture_selection()
            except Exception:
                log.exception("capture failed")
                text = None
            if text:
                log.info("captured %d chars", len(text))
                self.speech_q.put(("READ", text))
            else:
                log.info("no selection captured")

    # ----- speech -------------------------------------------------------

    @staticmethod
    def _is_speaking(voice):
        try:
            return int(voice.Status.RunningState) == SRSE_IS_SPEAKING
        except Exception:
            return False

    @staticmethod
    def _apply_voice(voice, vid, default_token):
        try:
            if not vid:
                if default_token is not None:
                    voice.Voice = default_token
                return
            voices = voice.GetVoices()
            for i in range(voices.Count):
                tok = voices.Item(i)
                if tok.Id == vid:
                    voice.Voice = tok
                    return
        except Exception:
            log.exception("could not set voice")

    def _speech_worker(self):
        import comtypes
        from comtypes.client import CreateObject

        comtypes.CoInitialize()
        try:
            try:
                voice = CreateObject("SAPI.SpVoice")
            except Exception:
                log.exception("could not create SAPI voice; speech disabled")
                return

            try:
                default_token = voice.Voice
            except Exception:
                default_token = None

            self._apply_voice(voice, self.voice_id, default_token)
            try:
                voice.Rate = speed_to_rate(self.speed)
            except Exception:
                pass

            pending_text = None
            deadline = None

            while True:
                timeout = None
                if deadline is not None:
                    timeout = max(0.0, deadline - time.monotonic())
                try:
                    cmd = self.speech_q.get(timeout=timeout)
                except queue.Empty:
                    cmd = None

                if cmd is None:
                    if pending_text is not None:
                        try:
                            voice.Speak(pending_text, SVSF_ASYNC)
                        except Exception:
                            log.exception("speak (pending) failed")
                        pending_text = None
                        deadline = None
                    continue

                kind = cmd[0]

                if kind == "QUIT":
                    try:
                        voice.Speak("", SVSF_PURGE | SVSF_ASYNC)
                    except Exception:
                        pass
                    break
                elif kind == "STOP":
                    try:
                        voice.Speak("", SVSF_PURGE | SVSF_ASYNC)
                    except Exception:
                        pass
                    pending_text = None
                    deadline = None
                elif kind == "SET_VOICE":
                    self._apply_voice(voice, cmd[1], default_token)
                elif kind == "SET_RATE":
                    try:
                        voice.Rate = speed_to_rate(cmd[1])
                    except Exception:
                        log.exception("could not set rate")
                elif kind == "LIST_VOICES":
                    resp = cmd[1]
                    out = []
                    try:
                        voices = voice.GetVoices()
                        for i in range(voices.Count):
                            tok = voices.Item(i)
                            out.append((tok.Id, tok.GetDescription()))
                    except Exception:
                        log.exception("could not list voices")
                    resp.put(out)
                elif kind == "READ":
                    text = cmd[1]
                    was_playing = self._is_speaking(voice) or (pending_text is not None)
                    try:
                        voice.Speak("", SVSF_PURGE | SVSF_ASYNC)
                    except Exception:
                        pass
                    if was_playing:
                        pending_text = text
                        deadline = time.monotonic() + (self.breathing_room_ms / 1000.0)
                    else:
                        try:
                            voice.Speak(text, SVSF_ASYNC)
                        except Exception:
                            log.exception("speak failed")
                        pending_text = None
                        deadline = None
        finally:
            comtypes.CoUninitialize()

    def _request_voices(self, timeout=3.0):
        resp = queue.Queue()
        self.speech_q.put(("LIST_VOICES", resp))
        try:
            return resp.get(timeout=timeout)
        except queue.Empty:
            log.warning("voice list timed out")
            return []

    def _speak_sample(self):
        self.speech_q.put(("READ", SAMPLE_TEXT))

    # ----- mouse hook wiring --------------------------------------------

    def _on_button_down(self, xbtn):
        if xbtn == self.read_button:
            try:
                self.capture_q.put_nowait("READ_REQUEST")
            except queue.Full:
                pass
        elif xbtn == self.stop_button:
            self.speech_q.put(("STOP",))

    def _should_suppress(self, xbtn):
        return self.swallow.is_set() and xbtn in (self.read_button,
                                                self.stop_button)

    def _start_hook(self):
        self.hook = MouseHook(self._on_button_down, self._should_suppress)
        self.hook.start()

    # ----- tray ----------------------------------------------------------

    def _on_speak_clipboard(self, icon, item):
        text = _Win32.get().get_text()
        if text and text.strip():
            self.speech_q.put(("READ", text))
        else:
            log.info("clipboard has no text to speak")

    def _on_stop(self, icon, item):
        self.speech_q.put(("STOP",))

    def _on_toggle_swallow(self, icon, item):
        if self.swallow.is_set():
            self.swallow.clear()
        else:
            self.swallow.set()
        self.save_config()

    def _swallow_checked(self, item):
        return self.swallow.is_set()

    def _on_open_settings(self, icon, item):
        self.ui_q.put("OPEN")

    def _on_show_log(self, icon, item):
        self.ui_q.put("LOG")

    def _on_quit(self, icon, item):
        self.ui_q.put("QUIT")

    def _build_tray(self):
        import pystray
        from pystray import MenuItem as Item, Menu
        menu = Menu(
            Item("Settings...", self._on_open_settings, default=True),
            Item("Speak clipboard (test)", self._on_speak_clipboard),
            Item("Stop", self._on_stop),
            Menu.SEPARATOR,
            Item("Swallow side buttons", self._on_toggle_swallow,
                 checked=self._swallow_checked),
            Item("Show log...", self._on_show_log),
            Menu.SEPARATOR,
            Item("Quit", self._on_quit),
        )
        self.icon = pystray.Icon("speak_selection", make_icon_image(),
                                "Speak Selection", menu=menu)

    # ----- theming -------------------------------------------------------

    def _apply_theme(self, dark):
        self._cur_dark = dark
        p = palette(dark)
        st = self.style
        st.theme_use("clam")
        st.configure(".", background=p["bg"], foreground=p["fg"],
                    fieldbackground=p["field"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"])
        st.configure("TFrame", background=p["bg"])
        st.configure("TLabel", background=p["bg"], foreground=p["fg"])
        st.configure("TButton", background=p["field"], foreground=p["fg"])
        st.map("TButton", background=[("active", p["sel"])])
        st.configure("TCheckbutton", background=p["bg"], foreground=p["fg"])
        st.map("TCheckbutton", background=[("active", p["bg"])])
        st.configure("TCombobox", fieldbackground=p["field"],
                    background=p["field"], foreground=p["fg"],
                    arrowcolor=p["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", p["field"])],
            foreground=[("readonly", p["fg"])])
        st.configure("Horizontal.TScale", background=p["bg"],
                    troughcolor=p["trough"])

        if self.root is not None:
            self.root.option_add("*TCombobox*Listbox.background", p["field"])
            self.root.option_add("*TCombobox*Listbox.foreground", p["fg"])
            self.root.option_add("*TCombobox*Listbox.selectBackground", p["sel"])
            self.root.option_add("*TCombobox*Listbox.selectForeground", p["fg"])
            self.root.configure(bg=p["bg"])
        if self.settings_win is not None:
            try:
                self.settings_win.configure(bg=p["bg"])
            except Exception:
                pass
        if self._log_text is not None:
            try:
                self._log_text.configure(bg=p["field"], fg=p["fg"],
                                        insertbackground=p["fg"])
            except Exception:
                pass
        if self.log_win is not None:
            try:
                self.log_win.configure(bg=p["bg"])
            except Exception:
                pass

    # ----- settings window ----------------------------------------------

    @staticmethod
    def _btn_label_to_value(label):
        return BTN_FORWARD if "Forward" in label else BTN_BACK

    def _open_settings(self):
        if self.settings_win is not None:
            try:
                self.settings_win.deiconify()
                self.settings_win.lift()
                self.settings_win.focus_force()
                return
            except Exception:
                self.settings_win = None
        self._build_settings()

    def _hide_settings(self):
        if self.settings_win is not None:
            try:
                self.settings_win.withdraw()
            except Exception:
                pass

    def _build_settings(self):
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("Speak Selection \u2014 Settings")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._hide_settings)

        frm = ttk.Frame(win, padding=16)
        frm.grid(row=0, column=0, sticky="nsew")
        frm.columnconfigure(1, weight=1)

        voices = self._request_voices()
        self._voice_items = [("__default__", "(system default)")] + voices
        descs = [d for _, d in self._voice_items]

        r = 0
        ttk.Label(frm, text="Voice").grid(row=r, column=0, sticky="w", pady=6)
        self._voice_combo = ttk.Combobox(frm, values=descs, state="readonly",
                                        width=36)
        cur = 0
        for i, (vid, _) in enumerate(self._voice_items):
            if vid == self.voice_id or (vid == "__default__" and self.voice_id is None):
                cur = i
                break
        self._voice_combo.current(cur)
        self._voice_combo.grid(row=r, column=1, columnspan=2, sticky="ew", pady=6)
        self._voice_combo.bind("<<ComboboxSelected>>", self._on_voice_select)
        r += 1

        ttk.Label(frm, text="Speed").grid(row=r, column=0, sticky="w", pady=6)
        self._speed_var = tk.DoubleVar(value=self.speed)
        sc = ttk.Scale(frm, from_=0.5, to=2.0, orient="horizontal",
                    variable=self._speed_var, command=self._on_speed_move)
        sc.grid(row=r, column=1, sticky="ew", pady=6, padx=(0, 8))
        sc.bind("<ButtonRelease-1>", self._on_speed_commit)
        self._speed_label = ttk.Label(frm, text=f"{self.speed:.2f}x", width=6)
        self._speed_label.grid(row=r, column=2, sticky="e")
        r += 1

        ttk.Label(frm, text="Breathing room").grid(row=r, column=0, sticky="w",
                                                pady=6)
        self._breath_var = tk.DoubleVar(value=self.breathing_room_ms)
        bsc = ttk.Scale(frm, from_=0, to=1000, orient="horizontal",
                        variable=self._breath_var, command=self._on_breath_move)
        bsc.grid(row=r, column=1, sticky="ew", pady=6, padx=(0, 8))
        bsc.bind("<ButtonRelease-1>", self._on_breath_commit)
        self._breath_label = ttk.Label(frm, text=f"{self.breathing_room_ms} ms",
                                    width=6)
        self._breath_label.grid(row=r, column=2, sticky="e")
        r += 1

        self._swallow_var = tk.BooleanVar(value=self.swallow.is_set())
        ttk.Checkbutton(frm, text="Swallow side buttons (don't also navigate)",
                        variable=self._swallow_var,
                        command=self._on_swallow_toggle).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(10, 6))
        r += 1

        ttk.Label(frm, text="Read button").grid(row=r, column=0, sticky="w",
                                                pady=6)
        self._read_combo = ttk.Combobox(
            frm, values=[BTN_LABELS[BTN_FORWARD], BTN_LABELS[BTN_BACK]],
            state="readonly", width=24)
        self._read_combo.set(BTN_LABELS[self.read_button])
        self._read_combo.grid(row=r, column=1, columnspan=2, sticky="ew", pady=6)
        self._read_combo.bind("<<ComboboxSelected>>", self._on_read_button)
        r += 1

        ttk.Label(frm, text="Stop button").grid(row=r, column=0, sticky="w",
                                                pady=6)
        self._stop_combo = ttk.Combobox(
            frm, values=[BTN_LABELS[BTN_FORWARD], BTN_LABELS[BTN_BACK]],
            state="readonly", width=24)
        self._stop_combo.set(BTN_LABELS[self.stop_button])
        self._stop_combo.grid(row=r, column=1, columnspan=2, sticky="ew", pady=6)
        self._stop_combo.bind("<<ComboboxSelected>>", self._on_stop_button)
        r += 1

        ttk.Button(frm, text="Close", command=self._hide_settings).grid(
            row=r, column=2, sticky="e", pady=(12, 0))

        self._apply_theme(read_dark_mode())
        win.update_idletasks()
        win.lift()
        win.focus_force()

    def _on_voice_select(self, _evt):
        idx = self._voice_combo.current()
        vid, _ = self._voice_items[idx]
        self.voice_id = None if vid == "__default__" else vid
        self.speech_q.put(("SET_VOICE", self.voice_id))
        self.save_config()
        self._speak_sample()

    def _on_speed_move(self, val):
        v = float(val)
        self.speed = round(v, 2)
        if self._speed_label is not None:
            self._speed_label.config(text=f"{self.speed:.2f}x")

    def _on_speed_commit(self, _evt):
        self.speech_q.put(("SET_RATE", self.speed))
        self.save_config()
        self._speak_sample()

    def _on_breath_move(self, val):
        v = int(round(float(val) / 50.0) * 50)
        self.breathing_room_ms = v
        if self._breath_var is not None:
            self._breath_var.set(v)
        if self._breath_label is not None:
            self._breath_label.config(text=f"{v} ms")

    def _on_breath_commit(self, _evt):
        self.save_config()

    def _on_swallow_toggle(self):
        if self._swallow_var.get():
            self.swallow.set()
        else:
            self.swallow.clear()
        self.save_config()

    def _on_read_button(self, _evt):
        rb = self._btn_label_to_value(self._read_combo.get())
        if rb == self.stop_button:
            self.stop_button = BTN_BACK if rb == BTN_FORWARD else BTN_FORWARD
            self._stop_combo.set(BTN_LABELS[self.stop_button])
        self.read_button = rb
        self.save_config()

    def _on_stop_button(self, _evt):
        sb = self._btn_label_to_value(self._stop_combo.get())
        if sb == self.read_button:
            self.read_button = BTN_BACK if sb == BTN_FORWARD else BTN_FORWARD
            self._read_combo.set(BTN_LABELS[self.read_button])
        self.stop_button = sb
        self.save_config()

    # ----- log window ----------------------------------------------------

    def _open_log(self):
        if self.log_win is not None:
            try:
                self.log_win.deiconify()
                self.log_win.lift()
                self.log_win.focus_force()
                self._refresh_log(force=True)
                return
            except Exception:
                self.log_win = None
        self._build_log_window()

    def _hide_log(self):
        if self.log_win is not None:
            try:
                self.log_win.withdraw()
            except Exception:
                pass

    def _build_log_window(self):
        win = tk.Toplevel(self.root)
        self.log_win = win
        win.title("Speak Selection \u2014 Log")
        win.geometry("680x380")
        win.protocol("WM_DELETE_WINDOW", self._hide_log)

        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        yscroll = ttk.Scrollbar(frame, orient="vertical")
        yscroll.pack(side="right", fill="y")
        txt = tk.Text(frame, wrap="none", height=20, undo=False,
                    yscrollcommand=yscroll.set)
        txt.pack(side="left", fill="both", expand=True)
        yscroll.config(command=txt.yview)
        self._log_text = txt

        self._apply_theme(read_dark_mode())
        self._refresh_log(force=True)

    def _refresh_log(self, force=False):
        if self._log_text is None:
            return
        n = len(LOG_BUFFER)
        if not force and n == self._log_len:
            return
        try:
            self._log_text.config(state="normal")
            self._log_text.delete("1.0", "end")
            self._log_text.insert("end", "\n".join(LOG_BUFFER))
            self._log_text.see("end")
            self._log_text.config(state="disabled")
            self._log_len = n
        except Exception:
            pass

    # ----- main-thread tick ---------------------------------------------

    def _tick(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                if msg == "OPEN":
                    self._open_settings()
                elif msg == "LOG":
                    self._open_log()
                elif msg == "QUIT":
                    self._do_quit()
                    return
        except queue.Empty:
            pass

        self._tick_count += 1

        # keep the log window live
        if self.log_win is not None:
            try:
                if self.log_win.winfo_viewable():
                    self._refresh_log()
            except Exception:
                pass

        # follow the system theme while a window is open
        if self._tick_count % 8 == 0:
            open_win = None
            try:
                if self.settings_win is not None and self.settings_win.winfo_viewable():
                    open_win = self.settings_win
                elif self.log_win is not None and self.log_win.winfo_viewable():
                    open_win = self.log_win
            except Exception:
                open_win = None
            if open_win is not None:
                dark = read_dark_mode()
                if dark != self._cur_dark:
                    self._apply_theme(dark)
                if self._swallow_var is not None:
                    try:
                        self._swallow_var.set(self.swallow.is_set())
                    except Exception:
                        pass

        self.root.after(250, self._tick)

    # ----- lifecycle -----------------------------------------------------

    def run(self):
        threading.Thread(target=self._speech_worker, daemon=True,
                        name="speech").start()
        threading.Thread(target=self._capture_worker, daemon=True,
                        name="capture").start()
        self._start_hook()

        self.root = tk.Tk()
        self.root.withdraw()
        self.style = ttk.Style(self.root)
        self._apply_theme(read_dark_mode())

        self._build_tray()
        threading.Thread(target=self.icon.run, daemon=True, name="tray").start()

        log.info("ready. read=%s stop=%s swallow=%s speed=%.2f",
                self.read_button, self.stop_button, self.swallow.is_set(),
                self.speed)

        self.root.after(250, self._tick)
        self.root.mainloop()

    def _do_quit(self):
        self.shutdown()
        try:
            self.root.quit()
        except Exception:
            pass

    def shutdown(self):
        try:
            if self.icon is not None:
                self.icon.stop()
        except Exception:
            pass
        try:
            if self.hook is not None:
                self.hook.stop()
        except Exception:
            pass
        self.capture_q.put("QUIT")
        self.speech_q.put(("QUIT",))


# ============================================================================

def _log_path():
    # Kept next to config.json under %APPDATA%\SpeakSelection so the program
    # folder (and the repo) stays free of runtime artifacts. Works the same
    # whether running from source or as a packaged .exe.
    return os.path.join(_config_dir(), "speak_selection.log")


def _setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    try:
        os.makedirs(_config_dir(), exist_ok=True)
        fh = logging.FileHandler(_log_path(), mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass

    bh = BufferHandler()
    bh.setFormatter(fmt)
    root.addHandler(bh)

    # Only attach a console handler when there actually is a console
    # (there isn't under pyw / pythonw).
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _pause_if_console():
    try:
        if sys.stdin and sys.stdin.isatty():
            input("\n--- a fatal error occurred (see speak_selection.log). "
                "Press Enter to close. ---\n")
    except Exception:
        pass


def main():
    _setup_logging()
    log.info("starting. python=%s platform=%s", sys.version.split()[0],
            sys.platform)
    log.info("config: %s", _config_path())

    single = SingleInstance()
    if not single.acquire():
        log.info("already running; asking the existing instance to open")
        single.signal_existing()
        return

    try:
        app = SpeakSelectionApp()
        # When a later launch pokes us, open the Settings window.
        single.start_listener(lambda: app.ui_q.put("OPEN"))
        app.run()
    except Exception:
        log.exception("FATAL startup error")
        _pause_if_console()
        sys.exit(1)


if __name__ == "__main__":
    main()
