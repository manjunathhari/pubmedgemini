"""Reading a structured reply: strictly, then one answer-shaped value in prose.

Moved unchanged from `researcher/studylist.py` (F2) so the writer can share it (F3 plan
Task 2). Ports of Incisive (commit 12630f7, `crates/incisive-app/src/agent/llm.rs`):

| Here | Incisive |
|---|---|
| `read_structured` | `read_structured` |
| `_parse_strict` | `parse_strict` |
| `_answer_shaped` | `answer_shaped` |
| `_parse_reply` | `parse_reply` |
| `_fault` | `text_fault` (categories as F2: `syntax` or `shape`) |

`typed(value)` stands in for serde's typed reading of `T`: it returns the typed value, or
None when serde would refuse it. JSON objects reach it as dicts that remember keys seen twice
(`dups`), because serde refuses a duplicate field.
"""

import json
from collections.abc import Callable
from typing import Any, Literal

from pubmedgemini.gemini.client import Reply

UnreadableCategory = Literal["truncated", "syntax", "shape"]

# Rust's `trim` and `\s`: Unicode White_Space (Python's strip() also removes U+001C..U+001F).
_WHITE_SPACE = "\t\n\x0b\x0c\r \x85\xa0        " \
    "        　"


def rust_trim(s: str) -> str:
    return s.strip(_WHITE_SPACE)


class Unreadable(Exception):
    """The reply holds no usable value. `category` only: never the reply's text."""

    def __init__(self, category: UnreadableCategory) -> None:
        super().__init__(category)
        self.category = category


class _Obj(dict):
    """A JSON object that remembers keys it saw twice (serde refuses a duplicate field)."""

    dups: frozenset = frozenset()


def _pairs(pairs: list[tuple[str, Any]]) -> _Obj:
    out = _Obj()
    dups = set()
    for k, v in pairs:
        if k in out:
            dups.add(k)
        out[k] = v
    out.dups = frozenset(dups)
    return out


def _no_constant(name: str) -> Any:
    raise ValueError("not JSON")  # NaN and Infinity are not JSON (serde refuses them)


_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_no_constant)


def _has_lone_surrogate(v: Any) -> bool:
    """serde_json refuses an unpaired surrogate escape; Python decodes it to a lone one."""
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            if any("\ud800" <= ch <= "\udfff" for ch in x):
                return True
        elif isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return False


def _loads(s: str) -> Any:
    """One whole JSON value, read as serde_json would; ValueError otherwise."""
    v = _DECODER.decode(s)
    if _has_lone_surrogate(v):
        raise ValueError("lone surrogate")
    return v


def _parse_strict[T](text: str, typed: Callable[[Any], T | None]) -> T | None:
    """The whole reply is one value, optionally inside a single code fence."""
    body = rust_trim(text)
    if body.startswith("```"):
        rest = rust_trim(body[3:].removeprefix("json"))
        if not rest.endswith("```"):
            return None
        body = rust_trim(rest[:-3])
    try:
        return typed(_loads(body))
    except (ValueError, RecursionError):
        return None


def _answer_shaped(v: Any) -> bool:
    """A non-empty object, or an array holding at least one object."""
    if isinstance(v, dict):
        return bool(v)
    if isinstance(v, list):
        return any(isinstance(x, dict) for x in v)
    return False


def _cut_off(e: json.JSONDecodeError) -> bool:
    """serde's `is_eof`: the value ran into the end of the text."""
    return e.pos >= len(e.doc) or e.msg.startswith("Unterminated string")


def _parse_reply[T](text: str, typed: Callable[[Any], T | None]) -> T | None:
    """Strict first; else exactly one answer-shaped, typed value among the complete top-level
    values found in the text (more than one, or none, is None)."""
    strict = _parse_strict(text, typed)
    if strict is not None:
        return strict
    found: list[T] = []
    skip_until = 0
    for i, ch in enumerate(text):
        if i < skip_until or ch not in "{[":
            continue
        try:
            v, end = _DECODER.raw_decode(text, i)
        except json.JSONDecodeError as e:
            if _cut_off(e):
                break
            continue
        except (ValueError, RecursionError):
            continue
        skip_until = end
        if _has_lone_surrogate(v):
            continue
        if _answer_shaped(v):
            t = typed(v)
            if t is not None:
                found.append(t)
    return found[0] if len(found) == 1 else None


def _fault(text: str) -> UnreadableCategory:
    """Incisive `text_fault`: not JSON at all is `syntax`; JSON of the wrong shape, or no
    single typed value in the text, is `shape`."""
    try:
        _loads(rust_trim(text))
    except (ValueError, RecursionError):
        return "syntax"
    return "shape"


def read_structured[T](reply: Reply, typed: Callable[[Any], T | None]) -> T:
    """The typed value in `reply`. A reply cut off by the token limit is never read.
    Raises Unreadable (outside any `except` block, so it carries no context)."""
    if reply.finish == "MAX_TOKENS":
        raise Unreadable("truncated")
    got = _parse_reply(reply.text, typed)
    if got is None:
        raise Unreadable(_fault(reply.text))
    return got
