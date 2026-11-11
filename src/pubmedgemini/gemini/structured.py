"""Reading a model's structured reply as Incisive does, and serde's typed reading of it.

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/agent/llm.rs`), with serde_json
1.0.154 (features `std`, `raw_value`; no `float_roundtrip`, no `arbitrary_precision`: Fact,
`cargo tree -e features`) and serde's derive:

| Here | Incisive |
|---|---|
| `read_structured` | `read_structured` (`:974-980`) |
| `_parse_strict` | `parse_strict` (`:822-829`): `serde_json::from_str::<T>` of the trimmed, unfenced text |
| `_answer_shaped` | `answer_shaped` (`:834-840`) |
| `_parse_reply` | `parse_reply` (`:848-873`): each value in prose is read as `serde_json::Value`, then `from_value::<T>` |
| `_fault` | `text_fault` (`:990-1004`), categories `syntax` (also `eof`) and `shape` (also `data`) |
| `number` | serde_json `de.rs` `parse_integer` .. `f64_from_parts` (the non-`float_roundtrip` code) |
| `value_string` | `Value::to_string` (keys in byte order, floats as serde_json writes them) |

Two rule sets (`rules=`):

* `"serde"` (the writer): what Incisive does. The whole reply is read typed first: a duplicate
  key of a typed struct, a lone surrogate in any key of one, or a bad value in a field it reads
  refuses it; values under unknown keys are skipped unread (as serde's `IgnoredAny`). Failing
  that, each top-level value in the text is read as a `Value` first (a duplicate key: the last
  wins; a lone surrogate, a number out of `f64` range or nesting past 127 refuses that value and
  the scan goes on at the next character), then typed. Numbers are parsed with serde_json's own
  algorithm, which is not always correctly rounded (a 17-digit decimal can differ from Python's
  `float` in the last place), so a value reads exactly as Incisive reads it.
* `"f2"` (the study list): F2's reading, unchanged (plan Task 4: "behaviour unchanged"). It
  differs from Incisive only on duplicate keys, lone surrogates and out-of-range numbers (see
  the Task 4 report).
"""

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from pubmedgemini.gemini.client import Reply

# Rust's `trim` and `\s`: Unicode White_Space (Python's strip() also removes U+001C..U+001F).
WHITE_SPACE = "\t\n\x0b\x0c\r \x85\xa0        " \
    "        　"


def rust_trim(s: str) -> str:
    return s.strip(WHITE_SPACE)


def rust_trim_start(s: str) -> str:
    return s.lstrip(WHITE_SPACE)


UnreadableCategory = Literal["truncated", "syntax", "shape"]
Rules = Literal["serde", "f2"]


class Unreadable(Exception):
    """The reply holds no usable answer. `category` only: never the reply's text."""

    def __init__(self, category: UnreadableCategory) -> None:
        super().__init__(category)
        self.category = category


# ---------- decoding ----------


class _Obj(dict):
    """A JSON object that remembers keys it saw twice (serde refuses a duplicate field) and
    the values the later ones replaced (serde_json parsed those too, when building a
    `Value`)."""

    dups: frozenset = frozenset()
    dropped: tuple = ()


def _pairs(pairs: list[tuple[str, Any]]) -> _Obj:
    out = _Obj()
    dups = set()
    dropped = []
    for k, v in pairs:
        if k in out:
            dups.add(k)
            dropped.append(out[k])
        out[k] = v
    out.dups = frozenset(dups)
    out.dropped = tuple(dropped)
    return out


def _no_constant(name: str) -> Any:
    raise ValueError("not JSON")  # NaN and Infinity are not JSON (serde refuses them)


class _Overflow:
    """A number serde_json refuses (out of `f64` range). Kept in place so that only a read of
    it fails, as serde skips an unknown key's value unparsed."""

    def __repr__(self) -> str:
        return "OVERFLOW"


OVERFLOW = _Overflow()

_U64_MAX = 2**64 - 1
_I32_MAX = 2**31 - 1
_POW10 = tuple(float(f"1e{i}") for i in range(309))  # de.rs POW10, 1e0 .. 1e308
_NUMBER = re.compile(r"(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?)([0-9]+))?")


def _overflows(a: int, b: int, c: int) -> bool:
    """de.rs `overflow!(a * 10 + b, c)`."""
    return a >= c // 10 and (a > c // 10 or b > c % 10)


def _f64_from_parts(positive: bool, significand: int, exponent: int) -> float | _Overflow:
    """de.rs `f64_from_parts` (without `float_roundtrip`)."""
    f = float(significand)
    while True:
        a = abs(exponent)
        if a <= 308:
            if exponent >= 0:
                f *= _POW10[a]
                if math.isinf(f):
                    return OVERFLOW
            else:
                f /= _POW10[a]
            break
        if f == 0.0:
            break
        if exponent >= 0:
            return OVERFLOW
        f /= 1e308
        exponent += 308
    return f if positive else -f


def number(s: str) -> int | float | _Overflow:
    """A JSON number literal as serde_json parses it: an integer in `u64` or `i64` range stays
    an integer (`-0` is the float -0.0); anything else is an `f64` built from at most 19-20
    significant digits (the rest are dropped, not rounded) and a power of ten."""
    neg, ip, fp, esign, ed = _NUMBER.fullmatch(s).groups()
    positive = not neg
    sig = exp = 0
    long = False
    for ch in ip:  # parse_integer, then parse_long_integer counting the digits left
        d = ord(ch) - 48
        if long or _overflows(sig, d, _U64_MAX):
            long = True
            exp += 1
            continue
        sig = sig * 10 + d
    if fp is None and ed is None:
        if long:
            return _f64_from_parts(positive, sig, exp)
        if positive:
            return sig
        return -sig if 1 <= sig <= 2**63 else -float(sig)
    if fp is not None:  # parse_decimal, then parse_decimal_overflow dropping the rest
        for ch in fp:
            d = ord(ch) - 48
            if _overflows(sig, d, _U64_MAX):
                break
            sig = sig * 10 + d
            exp -= 1
    if ed is not None:  # parse_exponent
        positive_exp = esign != "-"
        x = ord(ed[0]) - 48
        for ch in ed[1:]:
            d = ord(ch) - 48
            if _overflows(x, d, _I32_MAX):  # parse_exponent_overflow
                if sig != 0 and positive_exp:
                    return OVERFLOW
                return 0.0 if positive else -0.0
            x = x * 10 + d
        exp = max(-(2**31), min(_I32_MAX, exp + x if positive_exp else exp - x))
    return _f64_from_parts(positive, sig, exp)


_F2_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_no_constant)
_SERDE_DECODER = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_no_constant,
                                  parse_float=number, parse_int=number)


def is_lone(s: str) -> bool:
    """`s` holds an unpaired surrogate (serde_json refuses one where it reads the string)."""
    return any("\ud800" <= ch <= "\udfff" for ch in s)


def _has_lone_surrogate(v: Any) -> bool:
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, str):
            if is_lone(x):
                return True
        elif isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return False


def _loads(s: str) -> Any:
    """F2: one whole JSON value; ValueError on a lone surrogate anywhere."""
    v = _F2_DECODER.decode(s)
    if _has_lone_surrogate(v):
        raise ValueError("lone surrogate")
    return v


MAX_DEPTH = 127  # serde_json's recursion limit of 128 lets 127 nested arrays or objects in


def depth(v: Any) -> int:
    """Nesting of arrays and objects (a scalar is 0), replaced duplicates included."""
    best = 0
    stack = [(v, 1)]
    while stack:
        x, d = stack.pop()
        if isinstance(x, dict):
            best = max(best, d)
            stack.extend((y, d + 1) for y in x.values())
            stack.extend((y, d + 1) for y in getattr(x, "dropped", ()))
        elif isinstance(x, list):
            best = max(best, d)
            stack.extend((y, d + 1) for y in x)
    return best


def is_value(v: Any) -> bool:
    """`v` is a `serde_json::Value` serde would have built: no lone surrogate in a string or
    key, no number out of range, at most MAX_DEPTH deep (a duplicate key's replaced value
    counts: serde_json parsed it before the later one replaced it)."""
    stack = [v]
    while stack:
        x = stack.pop()
        if x is OVERFLOW or (isinstance(x, str) and is_lone(x)):
            return False
        if isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
            stack.extend(getattr(x, "dropped", ()))
        elif isinstance(x, list):
            stack.extend(x)
    return depth(v) <= MAX_DEPTH


def plain(v: Any) -> Any:
    """`v` as a `Value`: objects lose their duplicate-key record (the last value is kept)."""
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [plain(x) for x in v]
    return v


# ---------- serde's typed reading ----------


class Miss(Exception):
    """serde would refuse the value (no content: never shown, never logged)."""


def string(v: Any) -> str:
    """`String`."""
    if isinstance(v, str) and not is_lone(v):
        return v
    raise Miss


def f64(v: Any) -> float:
    """`f64`: any JSON number (an integer converts), never a bool or a string."""
    if isinstance(v, bool):
        raise Miss
    if isinstance(v, int):
        return float(v)
    if isinstance(v, float):
        return v
    raise Miss


def vec(item: Callable[[Any], Any]) -> Callable[[Any], list]:
    """`Vec<T>`."""
    def read(v: Any) -> list:
        if not isinstance(v, list):
            raise Miss
        return [item(x) for x in v]
    return read


def option(item: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """`Option<T>`: JSON null is None."""
    def read(v: Any) -> Any:
        return None if v is None else item(v)
    return read


def value(v: Any) -> Any:
    """`Option<Value>`'s inner value (null is None): anything serde_json can build."""
    if v is None:
        return None
    if not is_value(v):
        raise Miss
    return plain(v)


@dataclass(frozen=True)
class Field:
    name: str
    read: Callable[[Any], Any]
    default: Callable[[], Any] | None = None  # `#[serde(default)]`


def struct(*fields: Field) -> Callable[[Any], dict]:
    """A `#[derive(Deserialize)]` struct, read into a dict of its fields: from an object (a
    missing field takes its default or refuses; unknown keys are ignored; a known key twice
    refuses; any key with a lone surrogate refuses), or from an array, field by field in order
    (as serde's `visit_seq`: fewer items take defaults, more refuse)."""
    def read(v: Any) -> dict:
        out = {}
        if isinstance(v, dict):
            if any(is_lone(k) for k in v):
                raise Miss
            dups = getattr(v, "dups", frozenset())
            for f in fields:
                if f.name in dups:
                    raise Miss
                if f.name in v:
                    out[f.name] = f.read(v[f.name])
                elif f.default is not None:
                    out[f.name] = f.default()
                else:
                    raise Miss
            return out
        if isinstance(v, list):
            if len(v) > len(fields):
                raise Miss
            for i, f in enumerate(fields):
                if i < len(v):
                    out[f.name] = f.read(v[i])
                elif f.default is not None:
                    out[f.name] = f.default()
                else:
                    raise Miss
            return out
        raise Miss
    return read


def typed[T](reader: Callable[[Any], T]) -> Callable[[Any], T | None]:
    """A typed reader for `read_structured`: the value as serde reads it, or None. What it
    returns nests exactly as the text it read did, so serde's depth limit is checked on it."""
    def read(v: Any) -> T | None:
        try:
            got = reader(v)
        except Miss:
            return None
        return got if depth(got) <= MAX_DEPTH else None
    return read


def from_value[T](reader: Callable[[Any], T], v: Any) -> T | None:
    """`serde_json::from_value::<T>(v)` for a `Value` `v` (no duplicates, no depth limit)."""
    try:
        return reader(v)
    except Miss:
        return None


# ---------- Value::to_string ----------


def f64_string(x: float) -> str:
    """An `f64` as serde_json writes it: the shortest digits that read back, as a decimal when
    the point falls within -5 < k <= 16 places (always with a fraction, `60.0`), else
    `d.ddde+N` / `de-N`."""
    if x == 0:
        return "-0.0" if math.copysign(1.0, x) < 0 else "0.0"
    sign = "-" if x < 0 else ""
    # The shortest round-trip digits (Python's repr; the same digits serde_json writes,
    # pinned by a fuzz run against Rust) and the point's place: x = 0.digits × 10^point.
    t = Decimal(repr(abs(x))).normalize().as_tuple()
    digits = "".join(map(str, t.digits))
    point = len(digits) + t.exponent
    if -5 < point <= 16:
        if point >= len(digits):
            body = digits + "0" * (point - len(digits)) + ".0"
        elif point > 0:
            body = digits[:point] + "." + digits[point:]
        else:
            body = "0." + "0" * (-point) + digits
    else:
        e10 = point - 1
        body = (digits[0] + ("." + digits[1:] if len(digits) > 1 else "") + "e"
                + ("-" if e10 < 0 else "+") + str(abs(e10)))
    return sign + body


def value_string(v: Any) -> str:
    """`serde_json::Value::to_string`: compact, object keys in byte order (serde_json without
    `preserve_order` keeps a BTreeMap), non-ASCII written as is."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f64_string(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ",".join(value_string(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{value_string(v[k])}"
                              for k in sorted(v)) + "}"
    raise TypeError("not a JSON value")


# ---------- reading the reply ----------


def _parse_strict[T](text: str, typed_: Callable[[Any], T | None], rules: Rules) -> T | None:
    """The whole reply is one value, optionally inside a single code fence."""
    body = rust_trim(text)
    if body.startswith("```"):
        rest = rust_trim(body[3:].removeprefix("json"))
        if not rest.endswith("```"):
            return None
        body = rust_trim(rest[:-3])
    try:
        v = _loads(body) if rules == "f2" else _SERDE_DECODER.decode(body)
    except (ValueError, RecursionError):
        return None
    return typed_(v)


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


def _parse_reply[T](text: str, typed_: Callable[[Any], T | None], rules: Rules) -> T | None:
    """Strict first; else exactly one answer-shaped, typed value among the complete top-level
    values found in the text (more than one, or none, is None)."""
    strict = _parse_strict(text, typed_, rules)
    if strict is not None:
        return strict
    decoder = _F2_DECODER if rules == "f2" else _SERDE_DECODER
    found: list = []
    skip_until = 0
    for i, ch in enumerate(text):
        if i < skip_until or ch not in "{[":
            continue
        try:
            v, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError as e:
            if _cut_off(e):
                break
            continue
        except (ValueError, RecursionError):
            continue
        if rules == "f2":
            skip_until = end
            if _has_lone_surrogate(v):
                continue
        else:
            if not is_value(v):
                continue  # serde_json's error, not at the end: the scan goes on
            skip_until = end
            v = plain(v)
        if _answer_shaped(v):
            t = typed_(v)
            if t is not None:
                found.append(t)
    return found[0] if len(found) == 1 else None


def _fault(text: str, rules: Rules) -> UnreadableCategory:
    """Incisive `text_fault`: not JSON at all is `syntax`; JSON of the wrong shape, or no
    single answer in the text, is `shape`."""
    try:
        if rules == "f2":
            _loads(rust_trim(text))
        elif not is_value(_SERDE_DECODER.decode(rust_trim(text))):
            return "syntax"
    except (ValueError, RecursionError):
        return "syntax"
    return "shape"


def read_structured[T](reply: Reply, typed_: Callable[[Any], T | None], *,
                    rules: Rules = "serde") -> T:
    """The answer in `reply`, read by `typed_` (None: not that type). A reply cut off by the
    token limit is never read. Raises Unreadable."""
    if reply.finish == "MAX_TOKENS":
        raise Unreadable("truncated")
    got = _parse_reply(reply.text, typed_, rules)
    if got is None:
        raise Unreadable(_fault(reply.text, rules))
    return got
