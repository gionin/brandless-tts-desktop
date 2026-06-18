"""Unit tests for the pure-logic pieces of speak_selection.

These cover the Windows-independent functions (sentence chunking, speed->rate
mapping, config loading) so they can run on any platform without SAPI, the
mouse hook, or a display. The heavy Win32/COM imports in speak_selection are
lazy (inside functions/methods), so importing the module here is cheap.
"""

import json

import pytest

import speak_selection as ss


# ---------------------------------------------------------------------------
# split_sentences
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "\n\t  \n", None])
def test_split_empty_yields_empty_list(text):
    assert ss.split_sentences(text) == []


def test_split_basic_sentences():
    assert ss.split_sentences("Hello there. How are you? I am fine!") == [
        "Hello there.",
        "How are you?",
        "I am fine!",
    ]


def test_split_collapses_whitespace_and_newlines():
    # Wrapped/multi-paragraph text (e.g. copied from a PDF) becomes clean
    # single-spaced sentences -- the clarity win.
    out = ss.split_sentences("First line\n  wrapped   text.\n\nSecond para here.")
    assert out == ["First line wrapped text.", "Second para here."]


def test_split_does_not_break_decimals():
    assert ss.split_sentences("Pi is 3.14 and e is 2.71 roughly.") == [
        "Pi is 3.14 and e is 2.71 roughly."
    ]


def test_split_single_fragment_without_punctuation():
    assert ss.split_sentences("just a fragment") == ["just a fragment"]


def test_split_preserves_order():
    out = ss.split_sentences("One. Two. Three.")
    assert out == ["One.", "Two.", "Three."]


def test_split_chunks_are_stripped_and_nonempty():
    out = ss.split_sentences("  A.   B.   C.  ")
    assert out == ["A.", "B.", "C."]
    assert all(c == c.strip() and c for c in out)


def test_split_hard_wraps_long_runon_within_cap():
    runon = "word " * 200  # no sentence punctuation at all
    out = ss.split_sentences(runon)
    assert len(out) > 1
    assert all(len(c) <= ss.MAX_CHUNK_CHARS for c in out)
    # No content is lost: every word survives, order intact.
    assert " ".join(out).split() == runon.split()


def test_split_hard_wrap_handles_unbreakable_token():
    # A single token longer than the cap must still be emitted (cut at the cap)
    # rather than looping forever or being dropped.
    token = "x" * (ss.MAX_CHUNK_CHARS + 50)
    out = ss.split_sentences(token)
    assert "".join(out) == token
    assert all(len(c) <= ss.MAX_CHUNK_CHARS for c in out)


# ---------------------------------------------------------------------------
# speed_to_rate
# ---------------------------------------------------------------------------

def test_rate_at_normal_speed_is_zero():
    assert ss.speed_to_rate(1.0) == 0


def test_rate_clamps_speed_range():
    # Below 0.5 and above 2.0 clamp to the endpoints' rates.
    assert ss.speed_to_rate(0.1) == ss.speed_to_rate(0.5)
    assert ss.speed_to_rate(5.0) == ss.speed_to_rate(2.0)


def test_rate_is_monotonic_non_decreasing():
    speeds = [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]
    rates = [ss.speed_to_rate(s) for s in speeds]
    assert rates == sorted(rates)


def test_rate_stays_within_sapi_bounds():
    for s in (0.5, 1.0, 2.0):
        r = ss.speed_to_rate(s)
        assert isinstance(r, int)
        assert -10 <= r <= 10


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_missing_file_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_config_path",
                        lambda: str(tmp_path / "does_not_exist.json"))
    cfg = ss.load_config()
    assert cfg == ss.DEFAULT_CONFIG
    assert cfg is not ss.DEFAULT_CONFIG  # must be a copy, not the shared dict


def test_load_config_overrides_only_present_keys(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"speed": 1.5, "swallow_side_buttons": False}),
                    encoding="utf-8")
    monkeypatch.setattr(ss, "_config_path", lambda: str(path))
    cfg = ss.load_config()
    assert cfg["speed"] == 1.5
    assert cfg["swallow_side_buttons"] is False
    # Untouched keys keep their defaults.
    assert cfg["breathing_room_ms"] == ss.DEFAULT_CONFIG["breathing_room_ms"]


def test_load_config_ignores_unknown_keys(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"bogus": 123, "speed": 0.75}), encoding="utf-8")
    monkeypatch.setattr(ss, "_config_path", lambda: str(path))
    cfg = ss.load_config()
    assert "bogus" not in cfg
    assert cfg["speed"] == 0.75


def test_load_config_bad_json_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{ this is not valid json ", encoding="utf-8")
    monkeypatch.setattr(ss, "_config_path", lambda: str(path))
    cfg = ss.load_config()
    assert cfg == ss.DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# lcid_to_primary_lang / iso_to_primary_lang
# ---------------------------------------------------------------------------

def test_lcid_single_value():
    assert ss.lcid_to_primary_lang("409") == 0x09   # en-US
    assert ss.lcid_to_primary_lang("416") == 0x16   # pt-BR
    assert ss.lcid_to_primary_lang("40C") == 0x0C   # fr-FR


def test_lcid_strips_sublanguage_bits():
    # The primary id is the low 10 bits; sublanguage (region) is masked off.
    assert ss.lcid_to_primary_lang("816") == 0x16   # pt-PT -> still Portuguese


def test_lcid_multivalue_takes_first_parseable():
    assert ss.lcid_to_primary_lang("409;9") == 0x09
    assert ss.lcid_to_primary_lang(" ; 416 ; 409") == 0x16


@pytest.mark.parametrize("bad", [None, "", "   ", "zzz", ";", "xyz;qqq"])
def test_lcid_garbage_returns_none(bad):
    assert ss.lcid_to_primary_lang(bad) is None


def test_iso_maps_common_languages():
    assert ss.iso_to_primary_lang("en") == 0x09
    assert ss.iso_to_primary_lang("pt") == 0x16
    assert ss.iso_to_primary_lang("PT") == 0x16  # case-insensitive
    assert ss.iso_to_primary_lang("zh-cn") == 0x04


@pytest.mark.parametrize("bad", [None, "", "xx", "klingon"])
def test_iso_unknown_returns_none(bad):
    assert ss.iso_to_primary_lang(bad) is None


def test_lcid_and_iso_agree_round_trip():
    # A voice tagged en-US and text detected as 'en' must land on the same id.
    assert ss.lcid_to_primary_lang("409") == ss.iso_to_primary_lang("en")
    assert ss.lcid_to_primary_lang("416") == ss.iso_to_primary_lang("pt")


# ---------------------------------------------------------------------------
# voice_for_language / plan_voices  (detection injected; no langdetect / SAPI)
# ---------------------------------------------------------------------------

# Sentinel "tokens" -- the logic only ever passes them through.
EN = "EN_VOICE"
PT = "PT_VOICE"
EN_PREF = "EN_PREFERRED_VOICE"
FALLBACK = "FALLBACK_VOICE"
LANG_INDEX = {0x09: EN, 0x16: PT}     # first installed voice per language
NO_OVERRIDES = {}


def test_voice_for_language_uses_lang_index_when_no_override():
    assert ss.voice_for_language("pt", NO_OVERRIDES, LANG_INDEX, FALLBACK) == PT
    assert ss.voice_for_language("en", NO_OVERRIDES, LANG_INDEX, FALLBACK) == EN


def test_voice_for_language_override_takes_precedence():
    overrides = {0x09: EN_PREF}
    assert ss.voice_for_language("en", overrides, LANG_INDEX, FALLBACK) == EN_PREF
    # A language without an override still uses the first installed match.
    assert ss.voice_for_language("pt", overrides, LANG_INDEX, FALLBACK) == PT


def test_voice_for_language_unmatched_and_unknown_fall_back():
    assert ss.voice_for_language("fr", NO_OVERRIDES, LANG_INDEX, FALLBACK) == FALLBACK
    assert ss.voice_for_language(None, NO_OVERRIDES, LANG_INDEX, FALLBACK) == FALLBACK
    assert ss.voice_for_language("xx", NO_OVERRIDES, LANG_INDEX, FALLBACK) == FALLBACK


def test_plan_auto_off_uses_fallback_for_every_chunk():
    chunks = ["One.", "Two."]
    plan = ss.plan_voices(False, False, chunks, "One. Two.", NO_OVERRIDES,
                          LANG_INDEX, FALLBACK, detect=lambda t: "pt")
    assert plan == [(FALLBACK, "One."), (FALLBACK, "Two.")]


def test_plan_per_selection_picks_one_voice_from_full_text():
    chunks = ["Olá.", "Tudo bem?"]
    plan = ss.plan_voices(True, False, chunks, "Olá. Tudo bem?", NO_OVERRIDES,
                          LANG_INDEX, FALLBACK, detect=lambda t: "pt")
    assert plan == [(PT, "Olá."), (PT, "Tudo bem?")]


def test_plan_per_sentence_switches_each_chunk():
    chunks = ["Hello there.", "Olá pessoal."]

    def fake_detect(text):
        return "pt" if "Olá" in text else "en"

    plan = ss.plan_voices(True, True, chunks, "ignored", NO_OVERRIDES,
                          LANG_INDEX, FALLBACK, detect=fake_detect)
    assert plan == [(EN, "Hello there."), (PT, "Olá pessoal.")]


def test_plan_uses_preferred_override():
    chunks = ["Hello."]
    overrides = {0x09: EN_PREF}
    plan = ss.plan_voices(True, False, chunks, "Hello.", overrides,
                          LANG_INDEX, FALLBACK, detect=lambda t: "en")
    assert plan == [(EN_PREF, "Hello.")]


def test_plan_unmatched_language_falls_back():
    chunks = ["Bonjour."]
    plan = ss.plan_voices(True, False, chunks, "Bonjour.", NO_OVERRIDES,
                          LANG_INDEX, FALLBACK, detect=lambda t: "fr")
    assert plan == [(FALLBACK, "Bonjour.")]


def test_plan_detection_failure_falls_back():
    chunks = ["???"]
    plan = ss.plan_voices(True, False, chunks, "???", NO_OVERRIDES,
                          LANG_INDEX, FALLBACK, detect=lambda t: None)
    assert plan == [(FALLBACK, "???")]


# ---------------------------------------------------------------------------
# is_copyable_clipboard_format
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", [
    2,      # CF_BITMAP
    3,      # CF_METAFILEPICT
    9,      # CF_PALETTE
    14,     # CF_ENHMETAFILE
    0x80,   # CF_OWNERDISPLAY
    0x82,   # CF_DSPBITMAP
    0x83,   # CF_DSPMETAFILEPICT
    0x8E,   # CF_DSPENHMETAFILE
])
def test_gdi_handle_formats_are_skipped(fmt):
    assert ss.is_copyable_clipboard_format(fmt) is False


@pytest.mark.parametrize("fmt", [
    1,       # CF_TEXT
    7,       # CF_OEMTEXT
    8,       # CF_DIB
    13,      # CF_UNICODETEXT
    15,      # CF_HDROP (files)
    16,      # CF_LOCALE
    17,      # CF_DIBV5
    0xC001,  # a registered format (e.g. "HTML Format", "PNG")
    0xC0FF,
])
def test_memory_formats_are_copyable(fmt):
    assert ss.is_copyable_clipboard_format(fmt) is True


# ---------------------------------------------------------------------------
# Highlighter: word spans / offset mapping / chunk spans / alignment
# ---------------------------------------------------------------------------

def test_word_spans_basic():
    assert ss.word_spans("ab cd  ef") == [(0, 2), (3, 5), (7, 9)]


def test_word_spans_empty():
    assert ss.word_spans("   ") == []


def test_word_at_offset_inside_and_whitespace():
    spans = ss.word_spans("ab cd ef")  # (0,2)(3,5)(6,8)
    assert ss.word_at_offset(spans, 0) == 0   # start of "ab"
    assert ss.word_at_offset(spans, 1) == 0   # inside "ab"
    assert ss.word_at_offset(spans, 2) == 1   # whitespace -> next word
    assert ss.word_at_offset(spans, 4) == 1   # inside "cd"
    assert ss.word_at_offset(spans, 7) == 2   # inside "ef"
    assert ss.word_at_offset(spans, 99) is None


def test_split_sentences_spans_are_exact_substrings():
    text = "Hello there.  How are\nyou? I am fine."
    out = ss.split_sentences_spans(text)
    assert len(out) >= 2
    for chunk, base in out:
        assert text[base:base + len(chunk)] == chunk  # exact substring
        assert chunk == chunk.strip()                 # trimmed


def test_split_sentences_spans_offsets_map_back():
    text = "Alpha beta. Gamma delta."
    out = ss.split_sentences_spans(text)
    # The second chunk should start at the real index of "Gamma".
    assert out[0][0] == "Alpha beta."
    assert out[1][0] == "Gamma delta."
    assert out[1][1] == text.index("Gamma")


def test_split_sentences_spans_hard_wraps_long_runon():
    text = "word " * 200  # no sentence punctuation
    out = ss.split_sentences_spans(text)
    assert len(out) > 1
    for chunk, base in out:
        assert len(chunk) <= ss.MAX_CHUNK_CHARS
        assert text[base:base + len(chunk)] == chunk


def test_normalize_token_strips_punctuation_and_case():
    assert ss.normalize_token("Hello,") == "hello"
    assert ss.normalize_token("DON'T") == "dont"
    assert ss.normalize_token("...") == ""


def test_align_words_exact():
    spoken = ["The", "quick", "brown", "fox"]
    ocr = ["The", "quick", "brown", "fox"]
    assert ss.align_words(spoken, ocr) == {0: 0, 1: 1, 2: 2, 3: 3}


def test_align_words_ignores_leading_screen_noise():
    spoken = ["quick", "brown", "fox"]
    ocr = ["Menu", "File", "quick", "brown", "fox"]  # OCR caught UI chrome too
    m = ss.align_words(spoken, ocr)
    assert m == {0: 2, 1: 3, 2: 4}


def test_align_words_tolerates_a_missed_word():
    spoken = ["the", "quick", "brown", "fox"]
    ocr = ["the", "brown", "fox"]  # OCR missed "quick"
    m = ss.align_words(spoken, ocr)
    assert m[0] == 0           # the
    assert 1 not in m          # quick has no box -> highlight will hold/skip
    assert m[2] == 1 and m[3] == 2


def test_align_words_punctuation_tokens_do_not_cross_match():
    spoken = ["hi", "--", "there"]
    ocr = ["hi", "there"]
    m = ss.align_words(spoken, ocr)
    assert m[0] == 0
    assert m[2] == 1
    assert 1 not in m          # the "--" token must not match anything
