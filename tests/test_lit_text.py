"""The text cleaning rules in `lit/text.py` (F3b plan Task 1)."""

from pubmedgemini.lit.text import INVISIBLE, clean_json, clean_text, drop_invisible

# Every code point the class holds: bidi controls and zero-width characters.
CODE_POINTS = (
    [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + [chr(0x200E), chr(0x200F), chr(0x061C)]
    + [chr(c) for c in range(0x200B, 0x200E)]
    + [chr(0x2060), chr(0xFEFF)]
)


def test_drop_invisible_removes_bidi_and_zero_width():
    assert len(CODE_POINTS) == 17
    for ch in CODE_POINTS:
        assert INVISIBLE.fullmatch(ch), hex(ord(ch))
        assert drop_invisible(f"a{ch}b") == "ab", hex(ord(ch))
    assert drop_invisible("TEP\u200b") == "TEP"
    assert drop_invisible("\u202eabc") == "abc"
    assert drop_invisible("T" + "".join(CODE_POINTS) + "EP") == "TEP"


def test_drop_invisible_keeps_everything_else():
    kept = "a\u00a0b \U0001F600 \u00e9\nx\ty \u2212 \u2013 \u00b7 \ufffd"
    assert drop_invisible(kept) == kept
    # the neighbours of each range are not in the class
    for c in (0x2029, 0x202F, 0x2065, 0x206A, 0x200A, 0x2010, 0x205F, 0x2061, 0xFEFE, 0x061B):
        assert drop_invisible(chr(c)) == chr(c), hex(c)


def test_clean_text_is_unchanged():
    assert clean_text("a\x00b") == "ab"
    assert clean_text("a\ud800b") == "a\ufffdb"
    # clean_text does not drop invisible characters: that is the page's rule only (N10)
    assert clean_text("a\u200bb") == "a\u200bb"
    assert clean_json({"k\x00": ["x\ud800", {"y": "z\x00"}]}) == {"k": ["x\ufffd", {"y": "z"}]}
    assert clean_json({"k": "a\u202eb"}) == {"k": "a\u202eb"}


def test_scrub_json_cleans_every_string_and_leaves_the_input_alone():
    from pubmedgemini.lit.text import scrub_json

    dirty = "a\x00b\u202ec\u200bd\ud800"
    value = {"k\u200b": [dirty, (dirty, 2), {"n": None}], 7: True}
    assert scrub_json(value) == {"k": ["abcd\ufffd", ["abcd\ufffd", 2], {"n": None}], 7: True}
    assert value["k\u200b"][0] == dirty
    assert scrub_json(dirty) == "abcd\ufffd" and scrub_json(3.5) == 3.5
