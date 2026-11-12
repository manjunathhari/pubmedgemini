"""Cleaning of upstream text: one rule for every string that comes back from a source.

A lone UTF-16 surrogate (JSON can spell one, as `\\ud800`) cannot be encoded as UTF-8, so
it would fail the cache write and the page; it becomes U+FFFD. A NUL cannot be stored in a
Postgres text column; it is removed. The answer page also drops bidi controls and
zero-width characters from model text (`drop_invisible`); nothing stored changes. Nothing
here logs.
"""

import re
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
    if isinstance(value, str):
        return clean_text(value)
    if not isinstance(value, (dict, list)):
        return value
    root: dict | list = {} if isinstance(value, dict) else []
    stack: list[tuple[dict | list, dict | list]] = [(value, root)]
    while stack:
        src, dst = stack.pop()
        pairs = src.items() if isinstance(src, dict) else enumerate(src)
        for key, item in pairs:
            if isinstance(item, (dict, list)):
                new: Any = {} if isinstance(item, dict) else []
                stack.append((item, new))
            elif isinstance(item, str):
                new = clean_text(item)
            else:
                new = item
            if isinstance(dst, dict):
                dst[clean_text(key)] = new
            else:
                dst.append(new)
    return root
