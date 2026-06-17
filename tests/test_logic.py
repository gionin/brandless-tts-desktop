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
