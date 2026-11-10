"""Cleaning of upstream text: one rule for every string that comes back from a source.

A lone UTF-16 surrogate (JSON can spell one, as `\\ud800`) cannot be encoded as UTF-8, so
it would fail the cache write and the page; it becomes U+FFFD. A NUL cannot be stored in a
Postgres text column; it is removed. Nothing here logs.
"""

import re
from typing import Any

# A valid surrogate pair in JSON decodes to one character, so any surrogate left is lone.
_SURROGATE = re.compile("[\ud800-\udfff]")


def clean_text(s: str) -> str:
    """`s` with lone surrogates as U+FFFD and NUL characters removed."""
    if "\x00" in s:
        s = s.replace("\x00", "")
    return _SURROGATE.sub("�", s)


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
