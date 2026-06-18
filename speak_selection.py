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
import difflib
import json
import logging
import math
import os
import queue
import re
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
    "auto_switch_voice": False,  # pick a voice matching the detected language
    "per_sentence_switch": False,  # when auto on: detect per sentence vs per read
    "preferred_voices": [],     # voice ids; the one per language to prefer
    "highlight_while_reading": False,  # OCR-based underline of the spoken word
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

# Clipboard formats whose data is a GDI/special handle rather than an HGLOBAL
# memory block, so we can't snapshot/restore them by copying bytes. Skipped when
# preserving the clipboard during capture. CF_BITMAP, CF_METAFILEPICT,
# CF_PALETTE, CF_ENHMETAFILE, CF_OWNERDISPLAY, and the CF_DSP* display variants.
SKIP_CLIPBOARD_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}


def is_copyable_clipboard_format(fmt):
    """True if a clipboard format's data is an HGLOBAL we can copy as bytes.
    Apps that publish a bitmap/metafile almost always also publish a CF_DIB, so
    images still survive even though the GDI handle formats are skipped."""
    return fmt not in SKIP_CLIPBOARD_FORMATS

BTN_FORWARD = 2
BTN_BACK = 1
BTN_LABELS = {BTN_FORWARD: "Forward (front side button)",
            BTN_BACK: "Back (rear side button)"}

# Reading highlighter: a thin underline bar under the spoken word.
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
HL_COLOR = "#ff9500"   # underline color
HL_THICKNESS = 3       # px
HL_ALPHA = 0.9

# TEMPORARY SAFETY SWITCH. The Tk click-through overlay blocked mouse input on
# the user's machine (twice), and we can't verify click-through remotely. While
# disabled, no overlay window is ever created, so the app cannot block clicks.
# The rest of the highlight pipeline stays intact for when we ship a verified
# rendering. Flip to True only once click-through is proven safe.
HIGHLIGHT_OVERLAY_ENABLED = False


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


# ----------------------------------------------------------------------------
# Sentence chunking
# ----------------------------------------------------------------------------
# We split captured text into sentence-sized chunks and feed them to SAPI one
# at a time (it queues async Speak calls internally and plays them in order).
# This isn't about pause/skip — it's a cleaner foundation plus a clarity win:
# collapsing newlines/whitespace (common in selections from PDFs or wrapped
# text) stops SAPI from pausing oddly at line breaks, and very long run-ons get
# hard-wrapped into bounded units. Over-splitting is harmless: SAPI just speaks
# one more short chunk.

MAX_CHUNK_CHARS = 240

# Split at whitespace that follows sentence-final punctuation and precedes the
# likely start of a new sentence (an optional opening quote/bracket then an
# uppercase letter or digit). Avoids breaking "3.14" (no space) while tolerating
# the odd over-split like "Mr. Smith".
_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+(?=["\'(\[]?[A-Z0-9])')


def split_sentences(text):
    """Normalize whitespace and break text into bounded, sentence-ish chunks.

    Returns a list of non-empty strings in reading order. Empty/whitespace-only
    input yields an empty list.
    """
    if not text:
        return []
    norm = re.sub(r'\s+', ' ', text).strip()
    if not norm:
        return []

    chunks = []
    for part in _SENT_SPLIT.split(norm):
        part = part.strip()
        if not part:
            continue
        # Hard-wrap an over-long run-on (no sentence punctuation) at the last
        # space before the cap so no single chunk is unbounded.
        while len(part) > MAX_CHUNK_CHARS:
            cut = part.rfind(' ', 0, MAX_CHUNK_CHARS)
            if cut <= 0:
                cut = MAX_CHUNK_CHARS
            head = part[:cut].strip()
            if head:
                chunks.append(head)
            part = part[cut:].strip()
        if part:
            chunks.append(part)
    return chunks


# ----------------------------------------------------------------------------
# Offset-preserving chunking + word mapping (for the reading highlighter)
# ----------------------------------------------------------------------------
# Normal reading normalizes whitespace (nicer pacing) but that loses the link
# between a SAPI character offset and the original text. In highlight mode we
# instead speak exact substrings of the captured text, so SAPI's reported
# offset maps straight back to a word we can locate on screen.

def _emit_chunk_spans(text, start, end, out):
    """Append (substring, base_offset) pieces of text[start:end] to out, after
    trimming surrounding whitespace and hard-wrapping over-long runs. Every
    emitted chunk is an exact substring of text (text[base:base+len] == chunk)."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    while end - start > MAX_CHUNK_CHARS:
        cut = text.rfind(" ", start, start + MAX_CHUNK_CHARS)
        if cut <= start:
            cut = start + MAX_CHUNK_CHARS
        piece_end = cut
        while piece_end > start and text[piece_end - 1].isspace():
            piece_end -= 1
        if piece_end > start:
            out.append((text[start:piece_end], start))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if end > start:
        out.append((text[start:end], start))


def split_sentences_spans(text):
    """Like split_sentences, but returns (chunk, base_offset) pairs where each
    chunk is an exact substring of `text` at `base_offset` (no whitespace
    normalization), so SAPI offsets stay aligned to the original text."""
    if not text:
        return []
    spans = []
    last = 0
    for m in _SENT_SPLIT.finditer(text):
        _emit_chunk_spans(text, last, m.start(), spans)
        last = m.end()
    _emit_chunk_spans(text, last, len(text), spans)
    return spans


def word_spans(text):
    """Character spans (start, end) of each whitespace-delimited word."""
    return [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]


def word_at_offset(spans, offset):
    """Index of the word span containing `offset`; if `offset` lands in
    whitespace, the next word; None if past the end."""
    for i, (s, e) in enumerate(spans):
        if offset < e:
            return i
    return None


def normalize_token(s):
    """Lowercase, keep only alphanumerics — for fuzzy word matching."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


def align_words(spoken_tokens, ocr_tokens):
    """Map spoken-word index -> OCR-word index via fuzzy sequence alignment.

    Tolerates OCR misses/misreads and ignores extra on-screen words that aren't
    part of the selection. Tokens that normalize to empty (pure punctuation) are
    made unique so they never match across the two lists.
    """
    def prep(tokens):
        out = []
        for i, t in enumerate(tokens):
            n = normalize_token(t)
            out.append(n if n else "\x00%d" % i)  # unmatchable sentinel
        return out

    sp = prep(spoken_tokens)
    oc = prep(ocr_tokens)
    sm = difflib.SequenceMatcher(a=sp, b=oc, autojunk=False)
    mapping = {}
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            mapping[a + k] = b + k
    return mapping


def ocr_words(image, origin=(0, 0)):
    """OCR a PIL image with Windows' built-in engine. Returns a list of
    (text, (x, y, w, h)) in reading order, with boxes offset by `origin` to
    screen coordinates. Returns None if winocr is unavailable or OCR fails."""
    try:
        import winocr
    except Exception:
        log.warning("winocr not installed; word highlighting unavailable")
        return None
    try:
        result = winocr.recognize_pil_sync(image, "en-US")
    except Exception:
        log.exception("OCR failed")
        return None
    ox, oy = origin
    words = []
    try:
        for line in result["lines"]:
            for w in line["words"]:
                r = w["bounding_rect"]
                words.append((w["text"],
                              (int(r["x"]) + ox, int(r["y"]) + oy,
                               int(r["width"]), int(r["height"]))))
    except Exception:
        log.exception("could not parse OCR result")
        return None
    return words


# ----------------------------------------------------------------------------
# Language detection -> voice matching
# ----------------------------------------------------------------------------
# Each SAPI voice token reports a "Language" attribute as one or more hex LCIDs
# (e.g. "409" = en-US, "416" = pt-BR, "409;9"). The low 10 bits are the primary
# language id, which is what we match against the detected language of the text.

# Map langdetect's ISO 639-1 codes to Windows primary language ids. Only the
# common ones; anything unlisted simply won't auto-match (we fall back).
ISO_TO_PRIMARY_LANG = {
    "ar": 0x01, "bg": 0x02, "ca": 0x03, "zh-cn": 0x04, "zh-tw": 0x04,
    "cs": 0x05, "da": 0x06, "de": 0x07, "el": 0x08, "en": 0x09, "es": 0x0A,
    "fi": 0x0B, "fr": 0x0C, "he": 0x0D, "hu": 0x0E, "is": 0x0F, "it": 0x10,
    "ja": 0x11, "ko": 0x12, "nl": 0x13, "no": 0x14, "pl": 0x15, "pt": 0x16,
    "ro": 0x18, "ru": 0x19, "hr": 0x1A, "sk": 0x1B, "sq": 0x1C, "sv": 0x1D,
    "th": 0x1E, "tr": 0x1F, "ur": 0x20, "id": 0x21, "uk": 0x22, "vi": 0x2A,
}

# Friendly names for primary language ids, for the Settings UI.
PRIMARY_LANG_NAMES = {
    0x01: "Arabic", 0x02: "Bulgarian", 0x03: "Catalan", 0x04: "Chinese",
    0x05: "Czech", 0x06: "Danish", 0x07: "German", 0x08: "Greek",
    0x09: "English", 0x0A: "Spanish", 0x0B: "Finnish", 0x0C: "French",
    0x0D: "Hebrew", 0x0E: "Hungarian", 0x0F: "Icelandic", 0x10: "Italian",
    0x11: "Japanese", 0x12: "Korean", 0x13: "Dutch", 0x14: "Norwegian",
    0x15: "Polish", 0x16: "Portuguese", 0x18: "Romanian", 0x19: "Russian",
    0x1A: "Croatian", 0x1B: "Slovak", 0x1C: "Albanian", 0x1D: "Swedish",
    0x1E: "Thai", 0x1F: "Turkish", 0x20: "Urdu", 0x21: "Indonesian",
    0x22: "Ukrainian", 0x2A: "Vietnamese",
}


def primary_lang_name(prim):
    """Human-readable name for a primary language id, for display."""
    if prim is None:
        return "Unknown"
    return PRIMARY_LANG_NAMES.get(prim, "0x%X" % prim)


def lcid_to_primary_lang(hexstr):
    """Parse a SAPI 'Language' attribute (hex LCID, possibly ';'-separated) into
    a primary language id. Returns the first parseable id, or None."""
    if not hexstr:
        return None
    for part in str(hexstr).split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            return int(part, 16) & 0x3FF
        except ValueError:
            continue
    return None


def iso_to_primary_lang(iso):
    """Map a langdetect ISO code (e.g. 'en', 'pt', 'zh-cn') to a primary
    language id, or None if we don't have a mapping."""
    if not iso:
        return None
    return ISO_TO_PRIMARY_LANG.get(iso.lower())


# Detect on a prefix only: the first few hundred characters settle the language
# and it keeps detection fast on very long selections.
MAX_DETECT_CHARS = 1000


def detect_language(text):
    """Best-effort offline language detection. Returns an ISO 639-1 code (e.g.
    'en', 'pt') or None. langdetect is imported lazily and seeded so results are
    deterministic; any failure (empty text, no model, ambiguous) yields None.

    The first call is slow (langdetect loads its language profiles); warm it up
    off the playback path with warm_up_langdetect()."""
    if not text or not text.strip():
        return None
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return detect(text[:MAX_DETECT_CHARS])
    except Exception:
        return None


def warm_up_langdetect():
    """Force langdetect to load its profiles (the ~300 ms one-time cost) so the
    first real detection during playback is fast. Safe to call repeatedly."""
    detect_language("warming up the language detector")


def voice_for_language(iso, overrides, lang_index, fallback):
    """Resolve a voice token for a detected ISO language code.

    Precedence: a user `overrides` choice for that language wins; else the first
    installed voice for the language (`lang_index`); else `fallback`. Both
    `overrides` and `lang_index` are keyed by primary language id (int).
    """
    prim = iso_to_primary_lang(iso)
    if prim is not None:
        if prim in overrides:
            return overrides[prim]
        if prim in lang_index:
            return lang_index[prim]
    return fallback


def plan_voices(auto, per_sentence, chunks, full_text, overrides, lang_index,
                fallback, detect=detect_language):
    """Decide which voice token speaks each chunk.

    Returns a list of (token, chunk) pairs in order. When auto is off every
    chunk uses `fallback`. `overrides` (user preferred voice per language) and
    `lang_index` (first installed voice per language) are keyed by primary
    language id. `detect` is injectable so the logic can be tested without
    langdetect.
    """
    if not auto:
        return [(fallback, c) for c in chunks]

    def pick(text):
        return voice_for_language(detect(text), overrides, lang_index, fallback)

    if per_sentence:
        return [(pick(c), c) for c in chunks]
    tok = pick(full_text)
    return [(tok, c) for c in chunks]


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
        u32.EnumClipboardFormats.argtypes = [wintypes.UINT]
        u32.EnumClipboardFormats.restype = wintypes.UINT
        u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        u32.RegisterClipboardFormatW.restype = wintypes.UINT

        k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k32.GlobalAlloc.restype = wintypes.HGLOBAL
        k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalLock.restype = wintypes.LPVOID
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.restype = wintypes.BOOL
        k32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        k32.GlobalSize.restype = ctypes.c_size_t

        # Registered formats that opt a clipboard write out of clipboard history
        # and cloud sync. We add these when *restoring* the original clipboard so
        # the restore isn't recorded as a new (duplicate) history entry. The
        # user's original copy stays in history; only our restore is hidden.
        self._exclude_formats = []
        for _name in ("ExcludeClipboardContentFromMonitorProcessing",
                      "CanIncludeInClipboardHistory",
                      "CanUploadToCloudClipboard"):
            fid = u32.RegisterClipboardFormatW(_name)
            if fid:
                self._exclude_formats.append(fid)

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

    def snapshot(self):
        """Copy every memory-backed clipboard format out as bytes so the whole
        clipboard (text, images, file lists, rich formats) can be restored after
        a capture. Returns a list of (format, bytes), or None if the clipboard
        couldn't be opened."""
        if not self._open():
            return None
        try:
            items = []
            ctypes = self.ctypes
            fmt = self.u32.EnumClipboardFormats(0)
            while fmt:
                if is_copyable_clipboard_format(fmt):
                    handle = self.u32.GetClipboardData(fmt)
                    if handle:
                        size = self.k32.GlobalSize(handle)
                        if size:
                            ptr = self.k32.GlobalLock(handle)
                            if ptr:
                                try:
                                    items.append(
                                        (fmt, ctypes.string_at(ptr, size)))
                                finally:
                                    self.k32.GlobalUnlock(handle)
                fmt = self.u32.EnumClipboardFormats(fmt)
            return items
        finally:
            self.u32.CloseClipboard()

    def restore(self, items):
        """Replace the clipboard contents with a snapshot from snapshot(). A
        None snapshot (open failed earlier) is left untouched."""
        if items is None:
            return False
        if not self._open():
            return False
        try:
            self.u32.EmptyClipboard()
            ctypes = self.ctypes
            self._mark_excluded_from_history()
            for fmt, data in items:
                size = len(data)
                handle = self.k32.GlobalAlloc(GMEM_MOVEABLE, size)
                if not handle:
                    continue
                ptr = self.k32.GlobalLock(handle)
                if not ptr:
                    continue
                ctypes.memmove(ptr, data, size)
                self.k32.GlobalUnlock(handle)
                # System takes ownership of the handle after SetClipboardData.
                self.u32.SetClipboardData(fmt, handle)
            return True
        finally:
            self.u32.CloseClipboard()

    def _mark_excluded_from_history(self):
        """Within an open clipboard session, add the opt-out marker formats so
        this clipboard write is skipped by clipboard history and cloud sync.
        Each carries a serialized DWORD 0."""
        ctypes = self.ctypes
        for fid in self._exclude_formats:
            handle = self.k32.GlobalAlloc(GMEM_MOVEABLE, 4)
            if not handle:
                continue
            ptr = self.k32.GlobalLock(handle)
            if not ptr:
                continue
            ctypes.memmove(ptr, ctypes.byref(ctypes.c_uint32(0)), 4)
            self.k32.GlobalUnlock(handle)
            self.u32.SetClipboardData(fid, handle)

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
# Reading highlighter: a single thin underline bar that follows the spoken word
# ============================================================================

class HighlightBar:
    """One reused borderless, click-through, topmost window drawn as a thin bar
    under the current word. Lives on the Tk main thread."""

    def __init__(self, root):
        self.root = root
        self.win = None
        self._visible = False
        self._geom = None

    def _ensure(self):
        if self.win is not None:
            return
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=HL_COLOR)
        w.geometry("1x1+0+0")
        w.withdraw()
        w.update_idletasks()
        # Make it click-through. Critically, apply the styles to the *real*
        # top-level HWND (Tk wraps toplevels, so winfo_id() can be a child), and
        # manage visibility with LWA_ALPHA on that same window so transparency
        # (click-through) and alpha live together. This is what was broken
        # before — the styles landed on the wrong window and clicks were eaten.
        try:
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            u32.GetAncestor.restype = wintypes.HWND
            u32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
            u32.SetLayeredWindowAttributes.argtypes = [
                wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
            hwnd = u32.GetAncestor(w.winfo_id(), 2)  # GA_ROOT
            ex = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                               ex | WS_EX_LAYERED | WS_EX_TRANSPARENT
                               | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            u32.SetLayeredWindowAttributes(
                hwnd, 0, int(255 * HL_ALPHA), 0x2)  # LWA_ALPHA
        except Exception:
            log.exception("could not set highlight-bar styles")
        self.win = w

    def show(self, x, y, w, h):
        if not HIGHLIGHT_OVERLAY_ENABLED:   # safety: never create a window
            return
        try:
            self._ensure()
            width = max(1, int(w))
            by = int(y) + int(h)            # underline at the word's baseline
            geom = "%dx%d+%d+%d" % (width, HL_THICKNESS, int(x), by)
            if geom != self._geom:          # skip redundant moves
                self.win.geometry(geom)
                self._geom = geom
            if not self._visible:           # restack only when first shown,
                self.win.deiconify()        # not on every word (that was the lag)
                self.win.lift()
                self._visible = True
        except Exception:
            log.exception("highlight show failed")

    def hide(self):
        if self.win is not None and self._visible:
            try:
                self.win.withdraw()
            except Exception:
                pass
            self._visible = False

    def destroy(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
            self._visible = False
            self._geom = None


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
        self.auto_switch = bool(cfg["auto_switch_voice"])
        self.per_sentence = bool(cfg["per_sentence_switch"])
        pv = cfg["preferred_voices"]
        self.preferred_voices = list(pv) if isinstance(pv, list) else []
        self._lang_warmed = False
        self.highlight = bool(cfg["highlight_while_reading"])
        self.swallow = threading.Event()
        if cfg["swallow_side_buttons"]:
            self.swallow.set()

        self.speech_q = queue.Queue()
        self.capture_q = queue.Queue()
        self.ui_q = queue.Queue()
        self._hl_gen_counter = 0          # bumped per highlight read (capture thread)

        # Reading-highlight UI state (main thread only)
        self._hl_bar = None
        self._hl_cur_gen = None
        self._hl_spans = None
        self._hl_index_to_box = None
        self._hl_last_idx = None

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
        self._auto_switch_var = None
        self._per_sentence_var = None
        self._highlight_var = None
        self._pref_listbox = None
        self._pref_add_combo = None
        self._pref_view = []
        self._all_voices = []
        self._voice_by_id = {}
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
            "auto_switch_voice": bool(self.auto_switch),
            "per_sentence_switch": bool(self.per_sentence),
            "preferred_voices": list(self.preferred_voices),
            "highlight_while_reading": bool(self.highlight),
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
        old_clipboard = win.snapshot()

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
        win.restore(old_clipboard)

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
                gen = None
                if self.highlight and HIGHLIGHT_OVERLAY_ENABLED:
                    # Build the word->box map BEFORE speaking so the very first
                    # word can be highlighted (OCR takes ~250 ms; doing it up
                    # front adds that to the read's start, only when highlight
                    # is on). The map reaches the UI before any HL offset.
                    self._hl_gen_counter += 1
                    gen = self._hl_gen_counter
                    spans, index_to_box = self._prepare_highlight(text)
                    self.ui_q.put(("HL_MAP", gen, spans, index_to_box))
                self.speech_q.put(("READ", text, gen))
            else:
                log.info("no selection captured")

    @staticmethod
    def _grab_foreground():
        """Screenshot the foreground window. Returns (PIL image, (x, y) origin)
        or (None, (0, 0))."""
        try:
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            hwnd = u32.GetForegroundWindow()
            if not hwnd:
                return None, (0, 0)
            rect = wintypes.RECT()
            u32.GetWindowRect(hwnd, ctypes.byref(rect))
            l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
            if r <= l or b <= t:
                return None, (0, 0)
            from PIL import ImageGrab
            img = ImageGrab.grab(bbox=(l, t, r, b))
            return img, (l, t)
        except Exception:
            log.exception("foreground grab failed")
            return None, (0, 0)

    def _prepare_highlight(self, text):
        """Screenshot the foreground window, OCR it, and fuzzy-align the detected
        words to `text`. Returns (word_spans, {word_index: screen_box}). Runs on
        the capture thread before speaking so the map is ready for word one."""
        spans = word_spans(text)
        index_to_box = {}
        try:
            image, origin = self._grab_foreground()
            if image is not None:
                ocr = ocr_words(image, origin)
                if ocr:
                    spoken_tokens = [text[s:e] for s, e in spans]
                    ocr_tokens = [t for t, _box in ocr]
                    mapping = align_words(spoken_tokens, ocr_tokens)
                    for sp_idx, ocr_idx in mapping.items():
                        index_to_box[sp_idx] = ocr[ocr_idx][1]
        except Exception:
            log.exception("highlight prep failed")
        return spans, index_to_box

    # ----- speech -------------------------------------------------------

    @staticmethod
    def _is_speaking(voice):
        try:
            return int(voice.Status.RunningState) == SRSE_IS_SPEAKING
        except Exception:
            return False

    @staticmethod
    def _build_voice_index(voice):
        """Read each voice token's SAPI 'Language' attribute and return three
        maps used for auto voice switching:
          by_lang   primary language id -> token (first installed match)
          by_id     voice id            -> token
          id_to_prim voice id           -> primary language id
        """
        by_lang, by_id, id_to_prim = {}, {}, {}
        try:
            voices = voice.GetVoices()
            for i in range(voices.Count):
                tok = voices.Item(i)
                try:
                    vid = tok.Id
                except Exception:
                    vid = None
                try:
                    lang_attr = tok.GetAttribute("Language")
                except Exception:
                    lang_attr = None
                prim = lcid_to_primary_lang(lang_attr)
                if vid is not None:
                    by_id[vid] = tok
                    if prim is not None:
                        id_to_prim[vid] = prim
                if prim is not None and prim not in by_lang:
                    by_lang[prim] = tok
        except Exception:
            log.exception("could not build voice language index")
        return by_lang, by_id, id_to_prim

    def _preferred_overrides(self, by_id, id_to_prim):
        """Build a primary-language-id -> token map from the user's preferred
        voice list. First preferred voice for a language wins (the add flow
        keeps one per language, but we guard here too)."""
        overrides = {}
        for vid in self.preferred_voices:
            prim = id_to_prim.get(vid)
            tok = by_id.get(vid)
            if prim is not None and tok is not None and prim not in overrides:
                overrides[prim] = tok
        return overrides

    def _plan_voices(self, chunks, full_text, overrides, lang_index, fallback):
        return plan_voices(self.auto_switch, self.per_sentence, chunks,
                           full_text, overrides, lang_index, fallback)

    def _speak_plan(self, voice, plan, current_id):
        """Speak each (token, chunk[, base]) item. SAPI queues async Speak calls
        and plays them in order; a STOP/READ purge clears the whole queue at once
        so interruption stays instant. Switching voice resets the rate, so we
        re-apply it. Returns (current_id, stream_base) where stream_base maps the
        SAPI stream number of each chunk to its base character offset in the
        original text (used by the reading highlighter; base is None otherwise)."""
        stream_base = {}
        for item in plan:
            tok, chunk = item[0], item[1]
            base = item[2] if len(item) > 2 else None
            if tok is not None:
                try:
                    tid = tok.Id
                except Exception:
                    tid = None
                if tid is not None and tid != current_id:
                    try:
                        voice.Voice = tok
                        voice.Rate = speed_to_rate(self.speed)
                        current_id = tid
                    except Exception:
                        log.exception("could not switch voice")
            try:
                sid = voice.Speak(chunk, SVSF_ASYNC)
                stream_base[int(sid)] = base
            except Exception:
                log.exception("speak chunk failed")
        return current_id, stream_base

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

            # For auto voice switching: language/id token maps, and the
            # currently-selected token used as the fallback when auto is off or
            # no installed voice matches the detected language.
            lang_index, by_id, id_to_prim = self._build_voice_index(voice)

            def current_token_id():
                try:
                    return voice.Voice.Id
                except Exception:
                    return None

            try:
                fallback_token = voice.Voice
            except Exception:
                fallback_token = default_token
            current_id = current_token_id()

            pending_plan = None
            pending_gen = None
            deadline = None

            # Reading-highlight state. While a highlight read is speaking we poll
            # SAPI's word position and emit offsets to the UI thread.
            POLL_S = 0.03
            hl_gen = None
            hl_stream_base = {}
            hl_last_off = -1
            hl_started = False
            hl_wait = 0  # polls spent waiting for speech to actually start

            def emit_hl():
                nonlocal hl_last_off
                if hl_gen is None:
                    return
                try:
                    st = voice.Status
                    stream = int(st.CurrentStreamNumber)
                    pos = int(st.InputWordPosition)
                except Exception:
                    return
                base = hl_stream_base.get(stream)
                if base is None:
                    return
                off = base + pos
                if off != hl_last_off:
                    hl_last_off = off
                    try:
                        self.ui_q.put_nowait(("HL", hl_gen, off))
                    except Exception:
                        pass

            def end_hl():
                nonlocal hl_gen, hl_stream_base, hl_last_off, hl_started, hl_wait
                if hl_gen is not None:
                    try:
                        self.ui_q.put_nowait(("HL_END", hl_gen))
                    except Exception:
                        pass
                hl_gen = None
                hl_stream_base = {}
                hl_last_off = -1
                hl_started = False
                hl_wait = 0

            while True:
                timeouts = []
                if deadline is not None:
                    timeouts.append(max(0.0, deadline - time.monotonic()))
                if hl_gen is not None:
                    timeouts.append(POLL_S)
                timeout = min(timeouts) if timeouts else None
                try:
                    cmd = self.speech_q.get(timeout=timeout)
                except queue.Empty:
                    cmd = None

                if cmd is None:
                    if (pending_plan is not None and deadline is not None
                            and time.monotonic() >= deadline):
                        current_id, sb = self._speak_plan(voice, pending_plan,
                                                          current_id)
                        if pending_gen is not None:
                            hl_gen = pending_gen
                            hl_stream_base = sb
                            hl_last_off = -1
                            hl_started = False
                            hl_wait = 0
                        pending_plan = None
                        pending_gen = None
                        deadline = None
                    if hl_gen is not None:
                        if self._is_speaking(voice):
                            hl_started = True
                            emit_hl()
                        elif hl_started:
                            end_hl()           # finished playing
                        else:
                            hl_wait += 1
                            if hl_wait > 50:   # ~2s and never started; give up
                                end_hl()
                    continue

                kind = cmd[0]

                if kind == "QUIT":
                    try:
                        voice.Speak("", SVSF_PURGE | SVSF_ASYNC)
                    except Exception:
                        pass
                    end_hl()
                    break
                elif kind == "STOP":
                    try:
                        voice.Speak("", SVSF_PURGE | SVSF_ASYNC)
                    except Exception:
                        pass
                    pending_plan = None
                    pending_gen = None
                    deadline = None
                    end_hl()
                elif kind == "SET_VOICE":
                    self._apply_voice(voice, cmd[1], default_token)
                    # The manual pick becomes the new fallback/default voice.
                    try:
                        fallback_token = voice.Voice
                    except Exception:
                        pass
                    current_id = current_token_id()
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
                            try:
                                prim = lcid_to_primary_lang(
                                    tok.GetAttribute("Language"))
                            except Exception:
                                prim = None
                            out.append((tok.Id, tok.GetDescription(), prim))
                    except Exception:
                        log.exception("could not list voices")
                    resp.put(out)
                elif kind == "SAY":
                    # Speak specific text in a specific voice, bypassing
                    # auto-switch (used for Settings samples so the sample always
                    # matches the voice you picked).
                    say_chunks = split_sentences(cmd[1])
                    vid = cmd[2]
                    tok = by_id.get(vid) if vid else fallback_token
                    try:
                        voice.Speak("", SVSF_PURGE | SVSF_ASYNC)
                    except Exception:
                        pass
                    end_hl()
                    current_id, _ = self._speak_plan(
                        voice, [(tok, c, None) for c in say_chunks], current_id)
                    pending_plan = None
                    pending_gen = None
                    deadline = None
                elif kind == "READ":
                    text = cmd[1]
                    gen = cmd[2] if len(cmd) > 2 else None
                    highlight = gen is not None and self.highlight
                    if highlight:
                        chunk_bases = split_sentences_spans(text)
                        chunks = [c for c, _b in chunk_bases]
                    else:
                        chunks = split_sentences(text)
                    if not chunks:
                        continue
                    was_playing = (self._is_speaking(voice)
                                   or pending_plan is not None)
                    try:
                        voice.Speak("", SVSF_PURGE | SVSF_ASYNC)
                    except Exception:
                        pass
                    end_hl()  # drop any in-progress highlight before the new read
                    overrides = self._preferred_overrides(by_id, id_to_prim)
                    plan = self._plan_voices(chunks, text, overrides,
                                             lang_index, fallback_token)
                    if highlight:
                        items = [(plan[i][0], chunks[i], chunk_bases[i][1])
                                 for i in range(len(chunks))]
                    else:
                        items = [(tok, chunk, None) for tok, chunk in plan]
                    if was_playing:
                        pending_plan = items
                        pending_gen = gen if highlight else None
                        deadline = time.monotonic() + (self.breathing_room_ms / 1000.0)
                    else:
                        current_id, sb = self._speak_plan(voice, items,
                                                          current_id)
                        if highlight:
                            hl_gen = gen
                            hl_stream_base = sb
                            hl_last_off = -1
                            hl_started = False
                            hl_wait = 0
                        pending_plan = None
                        pending_gen = None
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

    def _speak_sample(self, voice_id=None):
        # SAY bypasses auto-switch so the sample is always heard in the chosen
        # voice (defaults to the manually selected one).
        if voice_id is None:
            voice_id = self.voice_id
        self.speech_q.put(("SAY", SAMPLE_TEXT, voice_id))

    def _warm_langdetect(self):
        # Load langdetect's profiles off the playback path (once) so the first
        # auto-switch read doesn't stall ~300 ms loading the model.
        if self._lang_warmed:
            return
        self._lang_warmed = True
        threading.Thread(target=warm_up_langdetect, daemon=True,
                        name="langwarm").start()

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

    def _on_toggle_highlight(self, icon, item):
        self.highlight = not self.highlight
        if not self.highlight:
            self.ui_q.put(("HL_OFF",))   # remove the bar on the UI thread
        self.save_config()

    def _highlight_checked(self, item):
        return self.highlight

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
            Item("Highlight words while reading (disabled)",
                 self._on_toggle_highlight, checked=self._highlight_checked),
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
        if self._pref_listbox is not None:
            try:
                self._pref_listbox.configure(bg=p["field"], fg=p["fg"],
                                            selectbackground=p["sel"],
                                            selectforeground=p["fg"])
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
        win.resizable(True, True)
        win.protocol("WM_DELETE_WINDOW", self._hide_settings)
        # Let the content frame fill (and grow with) the window.
        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        frm = ttk.Frame(win, padding=16)
        frm.grid(row=0, column=0, sticky="nsew")
        frm.columnconfigure(1, weight=1)

        # voices: list of (id, description, primary_lang_id)
        self._all_voices = self._request_voices()
        self._voice_by_id = {vid: (desc, prim)
                             for vid, desc, prim in self._all_voices}
        self._voice_items = ([("__default__", "(system default)")]
                             + [(vid, desc) for vid, desc, _ in self._all_voices])
        descs = [d for _, d in self._voice_items]

        # Size selection widgets to the longest voice name so it fits on open
        # (bounded so a very long name can't make the window huge; the user can
        # still resize freely and the field grows with the window).
        self._name_w = max(28, min(64, max((len(d) for d in descs), default=28)))

        r = 0
        ttk.Label(frm, text="Voice").grid(row=r, column=0, sticky="w", pady=6)
        self._voice_combo = ttk.Combobox(frm, values=descs, state="readonly",
                                        width=self._name_w)
        cur = 0
        for i, (vid, _) in enumerate(self._voice_items):
            if vid == self.voice_id or (vid == "__default__" and self.voice_id is None):
                cur = i
                break
        self._voice_combo.current(cur)
        self._voice_combo.grid(row=r, column=1, columnspan=2, sticky="ew", pady=6)
        self._voice_combo.bind("<<ComboboxSelected>>", self._on_voice_select)
        r += 1

        self._auto_switch_var = tk.BooleanVar(value=self.auto_switch)
        ttk.Checkbutton(frm, text="Auto-switch voice by detected language",
                        variable=self._auto_switch_var,
                        command=self._on_auto_switch_toggle).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
        r += 1

        self._per_sentence_var = tk.BooleanVar(value=self.per_sentence)
        ttk.Checkbutton(
            frm, text="    └ detect per sentence (else per selection)",
            variable=self._per_sentence_var,
            command=self._on_per_sentence_toggle).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 6))
        r += 1

        r = self._build_preferred_voices(frm, r)

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

        self._highlight_var = tk.BooleanVar(value=self.highlight)
        ttk.Checkbutton(
            frm,
            text="Highlight (underline) each word while reading "
                 "(temporarily disabled)",
            variable=self._highlight_var,
            command=self._on_highlight_toggle).grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(0, 6))
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
        # Open at least as large as the content needs (nothing clipped), and
        # don't let the user shrink below that. They can enlarge freely.
        win.minsize(win.winfo_reqwidth(), win.winfo_reqheight())
        win.lift()
        win.focus_force()

    # ----- preferred voices (per-language overrides) --------------------

    def _build_preferred_voices(self, frm, r):
        ttk.Label(frm, text="Preferred voices").grid(row=r, column=0,
                                                     sticky="nw", pady=(8, 0))
        ttk.Label(frm, foreground="gray",
                  text="One per language. Used when auto-switch is on.").grid(
            row=r, column=1, columnspan=2, sticky="w", pady=(8, 0))
        r += 1

        box = ttk.Frame(frm)
        box.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(2, 6))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        # This is the row that should absorb extra height when the window grows.
        frm.rowconfigure(r, weight=1)
        yscroll = ttk.Scrollbar(box, orient="vertical")
        lb = tk.Listbox(box, height=4, activestyle="none",
                        width=getattr(self, "_name_w", 36) + 14,
                        yscrollcommand=yscroll.set, exportselection=False)
        yscroll.config(command=lb.yview)
        lb.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        self._pref_listbox = lb
        r += 1

        ctrl = ttk.Frame(frm)
        ctrl.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        ctrl.columnconfigure(0, weight=1)
        self._pref_add_combo = ttk.Combobox(
            ctrl, values=[d for _, d, _ in self._all_voices],
            state="readonly", width=getattr(self, "_name_w", 36))
        self._pref_add_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(ctrl, text="Add / replace", command=self._on_pref_add).grid(
            row=0, column=1, padx=(0, 4))
        ttk.Button(ctrl, text="Remove", command=self._on_pref_remove).grid(
            row=0, column=2)
        r += 1

        self._refresh_pref_listbox()
        return r

    def _refresh_pref_listbox(self):
        lb = self._pref_listbox
        if lb is None:
            return
        self._pref_view = []
        try:
            lb.delete(0, "end")
            for vid in self.preferred_voices:
                info = self._voice_by_id.get(vid)
                if not info:
                    continue  # a previously-preferred voice no longer installed
                desc, prim = info
                lb.insert("end", f"{primary_lang_name(prim)}  —  {desc}")
                self._pref_view.append(vid)
        except Exception:
            log.exception("could not refresh preferred voices list")

    def _on_pref_add(self):
        idx = self._pref_add_combo.current()
        if idx < 0 or idx >= len(self._all_voices):
            return
        vid, _desc, prim = self._all_voices[idx]
        # Keep one preferred voice per language: drop any existing same-language
        # pick, then add this one.
        new_list = []
        for existing in self.preferred_voices:
            ex = self._voice_by_id.get(existing)
            if ex and prim is not None and ex[1] == prim:
                continue
            if existing != vid:
                new_list.append(existing)
        new_list.append(vid)
        self.preferred_voices = new_list
        self.save_config()
        self._refresh_pref_listbox()
        self._speak_sample(vid)  # preview the voice you just assigned

    def _on_pref_remove(self):
        if self._pref_listbox is None:
            return
        sel = self._pref_listbox.curselection()
        if not sel:
            return
        i = sel[0]
        if i < 0 or i >= len(self._pref_view):
            return
        vid = self._pref_view[i]
        self.preferred_voices = [v for v in self.preferred_voices if v != vid]
        self.save_config()
        self._refresh_pref_listbox()

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

    def _on_auto_switch_toggle(self):
        self.auto_switch = bool(self._auto_switch_var.get())
        if self.auto_switch:
            self._warm_langdetect()
        self.save_config()

    def _on_per_sentence_toggle(self):
        self.per_sentence = bool(self._per_sentence_var.get())
        self.save_config()

    def _on_highlight_toggle(self):
        self.highlight = bool(self._highlight_var.get())
        if not self.highlight and self._hl_bar is not None:
            self._hl_bar.destroy()       # this runs on the Tk thread
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

    def _highlight_bar(self):
        if self._hl_bar is None:
            self._hl_bar = HighlightBar(self.root)
        return self._hl_bar

    def _handle_hl(self, msg):
        kind = msg[0]
        if kind == "HL_MAP":
            _, gen, spans, index_to_box = msg
            self._hl_cur_gen = gen
            self._hl_spans = spans
            self._hl_index_to_box = index_to_box
            self._hl_last_idx = None
        elif kind == "HL_OFF":
            if self._hl_bar is not None:
                self._hl_bar.destroy()
        elif kind == "HL":
            _, gen, off = msg
            if not self.highlight:        # toggled off mid-read; stop drawing
                if self._hl_bar is not None:
                    self._hl_bar.hide()
                return
            if gen != self._hl_cur_gen or not self._hl_spans:
                return
            idx = word_at_offset(self._hl_spans, off)
            if idx is None or idx == self._hl_last_idx:
                return                       # same word -> nothing to do
            self._hl_last_idx = idx
            box = self._hl_index_to_box.get(idx) if self._hl_index_to_box else None
            if box:  # missing box (OCR miss) -> hold on the previous word
                x, y, w, h = box
                self._highlight_bar().show(x, y, w, h)
        elif kind == "HL_END":
            _, gen = msg
            if gen == self._hl_cur_gen and self._hl_bar is not None:
                self._hl_bar.hide()

    def _tick(self):
        try:
            while True:
                msg = self.ui_q.get_nowait()
                if isinstance(msg, tuple):
                    self._handle_hl(msg)
                elif msg == "OPEN":
                    self._open_settings()
                elif msg == "LOG":
                    self._open_log()
                elif msg == "QUIT":
                    self._do_quit()
                    return
        except queue.Empty:
            pass

        self._tick_count += 1

        # The heavier upkeep (log refresh, theme follow) runs ~every 240 ms; the
        # fast 30 ms cadence is so the highlight bar tracks the spoken word.
        if self.log_win is not None and self._tick_count % 8 == 0:
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

        self.root.after(20, self._tick)

    # ----- lifecycle -----------------------------------------------------

    def run(self):
        threading.Thread(target=self._speech_worker, daemon=True,
                        name="speech").start()
        threading.Thread(target=self._capture_worker, daemon=True,
                        name="capture").start()
        self._start_hook()
        if self.auto_switch:
            self._warm_langdetect()

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


def _set_dpi_aware():
    # Per-monitor DPI aware so screenshot pixels, window rectangles and the
    # highlight overlay all share one coordinate space (the reading highlighter
    # needs this; it's a no-op at 100% display scaling).
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main():
    _setup_logging()
    _set_dpi_aware()
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
