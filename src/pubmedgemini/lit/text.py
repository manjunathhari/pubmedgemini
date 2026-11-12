"""Cleaning of upstream text: one rule for every string that comes back from a source.

A lone UTF-16 surrogate (JSON can spell one, as `\\ud800`) cannot be encoded as UTF-8, so
it would fail the cache write and the page; it becomes U+FFFD. A NUL cannot be stored in a
Postgres text column; it is removed. The answer page also drops bidi controls and
zero-width characters from model text (`drop_invisible`), and the stored model text (the
Introduction, the early summary and the answer) is cleaned of all three before its INSERT
(`scrub_json`), so Postgres never refuses it nor writes a piece of it into its server log.
Nothing here logs.
"""

import re
from collections.abc import Callable
from typing import Any

# A valid surrogate pair in JSON decodes to one character, so any surrogate left is lone.
_SURROGATE = re.compile("[\ud800-\udfff]")
# Bidi controls (U+202A–U+202E, U+2066–U+2069, U+200E, U+200F, U+061C) and zero-width
# characters (U+200B–U+200D, U+2060, U+FEFF): in model text they can reorder or hide what
# the page shows (F3 third check L3).
INVISIBLE = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u200b-\u200d\u2060\ufeff]")


def clean_text(s: str) -> str:
    """`s` with lone surrogates as U+FFFD and NUL characters removed."""
    if "\x00" in s:
        s = s.replace("\x00", "")
    return _SURROGATE.sub("�", s)


def drop_invisible(s: str) -> str:
    """`s` without bidi controls or zero-width characters (`INVISIBLE`); nothing else changed."""
    return INVISIBLE.sub("", s)


def clean_json(value: Any) -> Any:
    """A parsed JSON value with every string (keys too) passed through `clean_text`.
    Walks with a stack, not recursion, so any depth json.loads accepted is fine."""
    return _walk(value, clean_text)


def _scrub(s: str) -> str:
    return drop_invisible(clean_text(s))


def scrub_json(value: Any) -> Any:
    """As `clean_json`, and every string (keys too) also without bidi controls or zero-width
    characters (`drop_invisible`): the body stored for model text. A new value; `value` is
    not changed."""
    return _walk(value, _scrub)


def _walk(value: Any, fn: Callable[[str], str]) -> Any:
    """`value` with `fn` applied to every string in it, keys too (a new value). A tuple is
    walked as a list is (json.dumps writes both as an array); a key that is not a string is
    kept as it is."""
    if isinstance(value, str):
        return fn(value)
    if not isinstance(value, (dict, list, tuple)):
        return value
    root: dict | list = {} if isinstance(value, dict) else []
    stack: list[tuple[dict | list | tuple, dict | list]] = [(value, root)]
    while stack:
        src, dst = stack.pop()
        pairs = src.items() if isinstance(src, dict) else enumerate(src)
        for key, item in pairs:
            if isinstance(item, (dict, list, tuple)):
                new: Any = {} if isinstance(item, dict) else []
                stack.append((item, new))
            elif isinstance(item, str):
                new = fn(item)
            else:
                new = item
            if isinstance(dst, dict):
                dst[fn(key) if isinstance(key, str) else key] = new
            else:
                dst.append(new)
    return root
