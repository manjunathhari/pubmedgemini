"""The study list: its schema, reading the model's reply strictly, and the checks in code.

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/`):

| Here | Incisive |
|---|---|
| `study_list_schema` | `agent/researcher.rs` `study_list_schema` (verbatim) |
| `read_study_list` | `agent/llm.rs` `read_structured` (in `gemini/structured.py`, F2's rules), plus serde's typed reading of `StudyList` |
| `validate` | `agent/researcher.rs` `validate_capped`, `clean_pmid`, `clip_chars` |
| `is_empty_list` | `agent/researcher.rs` `is_empty_list` |
| `seen_pmids` | `agent/researcher.rs` `seen_pmids`, `PMID_FIELD` |

The model's list is untrusted: every PMID must be 1 to 9 ASCII digits and seen in a tool result
this run; designs are an enum; texts are trimmed and cut to 500 characters; counts are capped.
Differences (plan, "Differences from Incisive"): `considered_not_selected` is validated and kept
(at most 30), where Incisive drops it after reading; the seen-PMID pattern has a space after
the colon because F1's JSON has one.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pubmedgemini.gemini.client import Reply
from pubmedgemini.gemini.structured import (  # Unreadable and its category: re-exported
    Unreadable,  # noqa: F401
    UnreadableCategory,  # noqa: F401
    read_structured,
    rust_trim,
)
from pubmedgemini.lit.text import clean_text

DESIGNS = ("RCT", "SR_MA", "other")
MAX_SELECTED = 10
MAX_NOTE = 500  # characters of a why, a reason or a contradiction note
MAX_CONTRADICTIONS = 10
MAX_CONSIDERED = 30  # ours (Estimate): Incisive does not keep this list

def study_list_schema() -> dict:
    """The StudyList JSON Schema, verbatim: every object `additionalProperties: false` with
    every property required; no `minimum` / `maximum`."""

    def obj(props: dict, req: list[str]) -> dict:
        return {"type": "object", "additionalProperties": False, "properties": props,
                "required": req}

    pmid = {"type": "string",
            "description": "PubMed id, digits only, exactly as a tool returned it."}
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "searches": {"type": "array", "items": obj({
                "query": {"type": "string"},
                "source": {"type": "string", "description": "pubmed, europepmc or related"},
                "hits": {"type": "integer"},
            }, ["query", "source", "hits"])},
            "selected": {"type": "array", "items": obj({
                "pmid": pmid,
                "design": {"type": "string", "enum": ["RCT", "SR_MA", "other"]},
                "why": {"type": "string",
                        "description": "One line: why this study matters for the question."},
            }, ["pmid", "design", "why"])},
            "considered_not_selected": {"type": "array", "items": obj({
                "pmid": pmid,
                "reason": {"type": "string"},
            }, ["pmid", "reason"])},
            "possible_contradictions": {"type": "array", "items": obj({
                "pmid": pmid,
                "note": {"type": "string",
                         "description": "How it may contradict the majority view."},
            }, ["pmid", "note"])},
        },
        "required": ["searches", "selected", "considered_not_selected",
                     "possible_contradictions"],
    }


# ---------- reading the reply ----------
# `Unreadable` and the reading itself are `gemini/structured.py`'s (re-exported here), with F2's
# rules (`rules="f2"`), unchanged.

_FIELDS = {
    "searches": (("query", str), ("source", str), ("hits", int)),
    "selected": (("pmid", str), ("design", str), ("why", str)),
    "considered_not_selected": (("pmid", str), ("reason", str)),
    "possible_contradictions": (("pmid", str), ("note", str)),
}


def _is_type(v: Any, t: type) -> bool:
    if t is int:
        return isinstance(v, int) and not isinstance(v, bool) and -(2**63) <= v < 2**63
    return isinstance(v, t)


def _as_study_list(v: Any) -> dict | None:
    """serde's typed reading of `StudyList`: the four keys are lists of objects with the
    typed fields; `design` is one of DESIGNS; unknown keys are ignored. None on any miss."""
    if not isinstance(v, dict):
        return None
    if any(k in getattr(v, "dups", ()) for k in _FIELDS):
        return None
    out: dict[str, list[dict]] = {}
    for key, fields in _FIELDS.items():
        items = v.get(key)
        if not isinstance(items, list):
            return None
        kept = []
        for it in items:
            if not isinstance(it, dict):
                return None
            if any(name in getattr(it, "dups", ()) for name, _ in fields):
                return None
            row = {}
            for name, t in fields:
                if name not in it or not _is_type(it[name], t):
                    return None
                row[name] = it[name]
            if key == "selected" and row["design"] not in DESIGNS:
                return None
            kept.append(row)
        out[key] = kept
    return out


def read_study_list(reply: Reply) -> dict:
    """The study list in `reply`, typed. A reply cut off by the token limit is never read.
    Raises Unreadable."""
    return read_structured(reply, _as_study_list, rules="f2")


# ---------- the checks ----------


@dataclass(frozen=True)
class Selected:
    pmid: str
    design: str  # one of DESIGNS
    why: str


@dataclass(frozen=True)
class Considered:
    pmid: str
    reason: str


@dataclass(frozen=True)
class Contradiction:
    pmid: str
    note: str


@dataclass(frozen=True)
class Validated:
    selected: tuple[Selected, ...]
    considered: tuple[Considered, ...]
    contradictions: tuple[Contradiction, ...]
    dropped_unseen: int  # selected PMIDs not seen this run, or not a PMID at all
    duplicates: int  # selected again after a first mention
    over_cap: int  # valid studies past the cap


def clean_pmid(pmid: str) -> str | None:
    """`pmid` trimmed, if it is 1 to 9 ASCII digits."""
    p = rust_trim(pmid)
    return p if 1 <= len(p) <= 9 and all(ch in "0123456789" for ch in p) else None


def clip_chars(s: str, max_chars: int) -> str:
    """Trimmed, then cut to `max_chars` characters. NUL and lone surrogates are cleaned too
    (F1's `clean_text`), so the note can be stored."""
    return clean_text(rust_trim(s)[:max_chars])


def validate(lst: dict, seen: set[str], cap: int = MAX_SELECTED) -> Validated:
    """Check a typed study list against the PMIDs `seen` this run (Incisive
    `validate_capped`); considered papers are checked the same way and must not also be
    selected."""
    selected: list[Selected] = []
    dropped = duplicates = over = 0
    for s in lst["selected"]:
        pmid = clean_pmid(s["pmid"])
        if pmid is None or pmid not in seen:
            dropped += 1
            continue
        if any(x.pmid == pmid for x in selected):
            duplicates += 1
            continue
        if len(selected) >= cap:
            over += 1
            continue
        selected.append(Selected(pmid, s["design"], clip_chars(s["why"], MAX_NOTE)))
    chosen = {x.pmid for x in selected}
    considered: list[Considered] = []
    for c in lst["considered_not_selected"]:
        if len(considered) >= MAX_CONSIDERED:
            break
        pmid = clean_pmid(c["pmid"])
        if (pmid is None or pmid not in seen or pmid in chosen
                or any(x.pmid == pmid for x in considered)):
            continue
        considered.append(Considered(pmid, clip_chars(c["reason"], MAX_NOTE)))
    contradictions: list[Contradiction] = []
    for c in lst["possible_contradictions"]:
        if len(contradictions) >= MAX_CONTRADICTIONS:
            break
        pmid = clean_pmid(c["pmid"])
        if pmid is not None and pmid in seen:
            contradictions.append(Contradiction(pmid, clip_chars(c["note"], MAX_NOTE)))
    return Validated(tuple(selected), tuple(considered), tuple(contradictions), dropped,
                     duplicates, over)


def is_empty_list(lst: dict) -> bool:
    """Nothing selected and nothing considered: the model skipped the task (S3)."""
    return not lst["selected"] and not lst["considered_not_selected"]


# A record's `pmid` as F1's tools serialise it (json.dumps default: a space after the colon).
SEEN_PMID = re.compile(r'"pmid": "(\d{1,9})"')


def seen_pmids(results: Iterable[str]) -> set[str]:
    """Every PMID that appears as a record's `pmid` in these tool results (the texts the model
    saw). Ids in `not_found`, in inputs, or inside a title never count."""
    return {m.group(1) for r in results for m in SEEN_PMID.finditer(r)}
