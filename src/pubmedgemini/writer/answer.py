"""The written answer: its types, Incisive's reading of the writer's reply, the merge of the
four parts, the evidence labels, the merge notes, citations, and the stored JSON.

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/agent/`):

| Here | Incisive |
|---|---|
| the dataclasses | `answer_v3.rs` `Number`, `Outcome`, `Study`, `Conflict`, `Applicability`, `BottomLine`, `KeyNumber`, `AtAGlance`, `ClinicalPractice`, `NotableFinding`, `WrittenAnswer`, `ReferenceV3`, `IntegrityV3`, `AnswerV3` (Quick fields only) |
| `typed_part`, `typed_whole` | serde's typing of `writer_v3.rs` `TrialsPart`, `OutcomesPart`, `ContextPart`, `SummaryPart`, `WireAnswer` |
| `merge_sections` | `writer_v3.rs` `merge_sections` |
| `into_answer` | `writer_v3.rs` `WireAnswer::into_answer`, `opt_text`, `opt_number`, `opt_count`, `text_or` |
| `parse_certainty`, `parse_glance`, `parse_practice`, `notable_line`, `cited_ids` | `writer_v3.rs` (with `unbullet`, `after_label`) |
| `design_class`, `evidence_types`, `EVIDENCE_LABELS` | `writer_v3.rs` `design_class`, `evidence_types`; `answer_v3.rs` `EvidenceType::label` |
| `merge_checks` | `writer_v3.rs` `merge_checks`, `certainty_rank`, `names_an_outcome`, `name_words`, `NAME_STOP` |
| `split_citations` | `answer_v3.rs` `CITE`, `DIGITS`, `split_citations`, `MIN_PMID_DIGITS` |

Two differences from serde's typing (Estimate: a model under a response schema never sends
either): a struct is read from a JSON object only (serde also takes an array of its fields, as
F2's study list reader does not), and any non-finite number makes the value unreadable (serde
refuses `1e400` while parsing; Python's `json` reads it as infinity).

Nothing here logs anything. Stored data is read defensively (`body_from_json`).
"""

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.researcher.loop import SearchEntry, compact_json
from pubmedgemini.writer.prompts import CERTAINTIES, Section

# ---------- the answer ----------


@dataclass
class Number:
    label: str
    value: float
    unit: str | None
    arm: str | None
    ci_low: float | None
    ci_high: float | None
    p: str | None
    pmid: str


EVIDENCE_TYPES = ("rct", "meta_analysis", "rcts_and_meta_analyses", "other", "mixed", "unknown")
EVIDENCE_LABELS: dict[str, str | None] = {
    "rct": "RCTs",
    "meta_analysis": "meta-analysis",
    "rcts_and_meta_analyses": "RCTs and meta-analyses",
    "other": "other / non-randomised",
    "mixed": "mixed evidence",
    "unknown": None,
}


@dataclass
class Outcome:
    outcome: str
    rct_evidence: str
    effect_text: str
    numbers: list[Number]
    certainty: str  # one of CERTAINTIES
    why: str
    pmids: list[str]
    evidence_type: str = "unknown"  # one of EVIDENCE_TYPES, set by code


@dataclass
class Study:
    pmid: str
    label: str
    design: str
    n: int | None
    setting: str
    comparison: str
    key_results: str
    appraisal: str


@dataclass
class Conflict:
    text: str
    pmids: list[str]


@dataclass
class Applicability:
    case_mix: str
    india_cost_context: str
    training: str


@dataclass
class BottomLine:
    text: str
    certainty: str
    certainty_reason: str


@dataclass
class KeyNumber:
    label: str
    value_text: str
    pmid: str


@dataclass
class AtAGlance:
    verdict: str
    key_numbers: list[KeyNumber]
    main_caveat: str


@dataclass
class ClinicalPractice:
    default_choice: str
    who_may_benefit: str
    counselling: str
    cost_per_case: str
    text: str = ""


@dataclass
class NotableFinding:
    text: str
    pmids: list[str]


@dataclass
class WrittenAnswer:
    bottom_line: BottomLine
    outcomes: list[Outcome]
    studies: list[Study]
    conflicts: list[Conflict]
    applicability: Applicability
    would_change: list[str]
    at_a_glance: AtAGlance | None
    clinical_practice: ClinicalPractice | None
    notable_findings: list[NotableFinding]


@dataclass
class Reference:
    n: int
    pmid: str
    title: str
    journal: str | None
    year: int | None
    doi: str | None
    retracted: bool


@dataclass
class Integrity:
    dropped_unseen: int = 0
    dropped_unfetched: int = 0
    removed_citations: int = 0
    dropped_numbers: int = 0
    dropped_studies: int = 0
    numbers_checked: int = 0
    numbers_untraced: int = 0
    labels_defaulted: int = 0


@dataclass
class AnswerBody:
    created_at: str
    answer: WrittenAnswer | None
    empty_reason: str | None
    references: list[Reference]
    searches: list[SearchEntry]
    integrity: Integrity
    stopped_early: str | None
    elapsed_s: int
    notes: list[str]
    untraced: list = field(default_factory=list)  # F4
    numbers_completed: list = field(default_factory=list)  # F4
    version: int = 3
    mode: str = "quick"


# ---------- serde's typing of the wire structs ----------

# Field kinds: "str" (String), "f64", "opt_str" (Option<String>, default), "any"
# (Option<Value>, default), "list_str" (Vec<String>), "list_str_default", "str_default",
# ("list", spec) and ("struct", spec).
_NUMBER = {"label": "str", "value": "f64", "unit": "opt_str", "arm": "opt_str", "ci_low": "any",
           "ci_high": "any", "p": "opt_str", "pmid": "str"}
_OUTCOME = {"outcome": "str", "rct_evidence": "str", "effect_text": "str",
            "numbers": ("list", _NUMBER), "certainty": "str", "why": "str",
            "pmids": "list_str"}
_STUDY = {"pmid": "str", "label": "str", "design": "str", "n": "any", "setting": "str",
          "comparison": "str", "key_results": "str", "appraisal": "str"}
_BOTTOM = {"text": "str", "certainty": "str", "certainty_reason": "str"}
_CONFLICT = {"text": "str", "pmids": "list_str"}
_APPLICABILITY = {"case_mix": "str", "india_cost_context": "str", "training": "str"}
_FIELD_KINDS: dict[str, Any] = {
    "studies": ("list", _STUDY),
    "conflicts": ("list", _CONFLICT),
    "notable_findings": "any",
    "outcomes": ("list", _OUTCOME),
    "applicability": ("struct", _APPLICABILITY),
    "would_change": "list_str",
    "bottom_line": ("struct", _BOTTOM),
    "at_a_glance": "any",
    "clinical_practice": "any",
}
_KEY_NUMBER = {"label": "str", "value_text": "str", "pmid": "str"}
_GLANCE = {"verdict": "str", "key_numbers": ("list", _KEY_NUMBER), "main_caveat": "str"}
_PRACTICE = {"default_choice": "str", "who_may_benefit": "str", "counselling": "str",
             "cost_per_case": "str", "text": "str_default"}
_NOTABLE = {"text": "str", "pmids": "list_str_default"}

_DEFAULTS = {"opt_str": None, "any": None, "str_default": "", "list_str_default": []}


class _Refused(Exception):
    """serde would refuse the value (internal; never leaves this module)."""


def _f64(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise _Refused
    try:
        x = float(v)
    except OverflowError:
        raise _Refused from None
    if not math.isfinite(x):
        raise _Refused
    return x


def _has_non_finite(v: Any) -> bool:
    stack = [v]
    while stack:
        x = stack.pop()
        if isinstance(x, float) and not math.isfinite(x):
            return True
        if isinstance(x, dict):
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return False


def _list_str(v: Any) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _Refused
    return list(v)


def _value(kind: Any, v: Any) -> Any:
    if kind == "str" or kind == "str_default":
        if not isinstance(v, str):
            raise _Refused
        return v
    if kind == "f64":
        return _f64(v)
    if kind == "opt_str":
        if v is not None and not isinstance(v, str):
            raise _Refused
        return v
    if kind == "any":
        return v
    if kind in ("list_str", "list_str_default"):
        return _list_str(v)
    tag, spec = kind
    if tag == "list":
        if not isinstance(v, list):
            raise _Refused
        return [_struct(spec, x) for x in v]
    return _struct(spec, v)


def _struct(spec: Mapping[str, Any], v: Any) -> dict:
    """A struct read as serde's derive reads it from a JSON object: every field present
    (unless it has a default) with its type; a duplicate field refused; unknown keys
    ignored."""
    if not isinstance(v, dict):
        raise _Refused
    dups = getattr(v, "dups", frozenset())
    out: dict[str, Any] = {}
    for name, kind in spec.items():
        if name in dups:
            raise _Refused
        if name not in v:
            if isinstance(kind, str) and kind in _DEFAULTS:
                d = _DEFAULTS[kind]
                out[name] = list(d) if isinstance(d, list) else d
                continue
            raise _Refused
        out[name] = _value(kind, v[name])
    return out


def _typed(spec: Mapping[str, Any]) -> Callable[[Any], dict | None]:
    def typed(v: Any) -> dict | None:
        if _has_non_finite(v):
            return None
        try:
            return _struct(spec, v)
        except _Refused:
            return None
    return typed


_WHOLE = {f: _FIELD_KINDS[f] for f in ("bottom_line", "outcomes", "studies", "conflicts",
                                        "applicability", "would_change", "at_a_glance",
                                        "clinical_practice", "notable_findings")}
typed_whole = _typed(_WHOLE)
_PARTS = {s: _typed({f: _FIELD_KINDS[f] for f in s.fields}) for s in Section}


def typed_part(section: Section) -> Callable[[Any], dict | None]:
    """The typed reading of one part's reply: its own fields only (another part's field in
    it is not taken)."""
    return _PARTS[section]


def merge_sections(parts: Mapping[Section, dict]) -> dict:
    """The four typed parts as one reply of the single writer's shape."""
    t, o, c, s = (parts[x] for x in (Section.TRIALS, Section.OUTCOMES, Section.CONTEXT,
                                     Section.SUMMARY))
    return {
        "bottom_line": s["bottom_line"], "outcomes": o["outcomes"], "studies": t["studies"],
        "conflicts": t["conflicts"], "applicability": c["applicability"],
        "would_change": c["would_change"], "at_a_glance": s["at_a_glance"],
        "clinical_practice": s["clinical_practice"],
        "notable_findings": t["notable_findings"],
    }


# ---------- reading the reply ----------


def parse_certainty(s: str) -> str | None:
    """A certainty word, case and separators ignored ("Very low", "very_low", "VERY LOW")."""
    k = "".join(ch for ch in s if ch.isascii() and ch.isalpha()).lower()
    return {"high": "High", "moderate": "Moderate", "low": "Low",
            "verylow": "Very low"}.get(k)


def _opt_text(v: str | None) -> str | None:
    if v is None:
        return None
    t = rust_trim(v)
    return t or None


# Rust's f64 grammar (`str::parse`), finite numbers only.
_F64 = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_I64 = re.compile(r"[+-]?[0-9]+")
I64_MIN, I64_MAX = -(2**63), 2**63 - 1


def _opt_number(v: Any) -> float | None:
    """A number written as JSON number or text (`"-0.34"`, `"−0.34"`); None otherwise."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        try:
            x = float(v)
        except OverflowError:
            return None
        return x if math.isfinite(x) else None
    if isinstance(v, str):
        t = rust_trim(v).replace("−", "-").replace("–", "-")
        if not _F64.fullmatch(t):
            return None
        x = float(t)
        return x if math.isfinite(x) else None
    return None


def _opt_count(v: Any) -> int | None:
    """A positive whole number written as JSON number or text; None for 0 or anything else."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, str):
        t = rust_trim(v)
        if not _I64.fullmatch(t):
            return None
        n = int(t)
    else:
        return None  # a JSON float: serde's as_i64 is None
    return n if 0 < n <= I64_MAX else None


def _rust_lines(s: str) -> list[str]:
    """Rust `str::lines`: split on `\\n`, one trailing `\\r` removed, no final empty line."""
    if not s:
        return []
    parts = s.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [p.removesuffix("\r") for p in parts]


def _unbullet(line: str) -> str:
    t = rust_trim(line)
    for b in ("- ", "• ", "* ", "– "):
        if t.startswith(b):
            return rust_trim(t[len(b):])
    return t


def _after_label(line: str, labels: tuple[str, ...]) -> str | None:
    low = line.lower()
    for label in labels:
        if low.startswith(label.lower()):
            rest = line[len(label):].lstrip("\t\n\x0b\x0c\r \x85\xa0    "
                                            "       "
                                            "     　")
            if rest.startswith(":"):
                return rust_trim(rest[1:])
    return None


def parse_glance(text: str) -> AtAGlance | None:
    """The at-a-glance text: the first line the verdict, a "Main caveat:" line the caveat,
    every other line a key number. Every line kept; None when empty."""
    lines = [x for x in (_unbullet(raw) for raw in _rust_lines(text)) if x]
    if not lines:
        return None
    first, rest = lines[0], lines[1:]
    v = _after_label(first, ("Verdict", "At a glance"))
    verdict = first if v is None else v
    caveat = ""
    keys: list[KeyNumber] = []
    for line in rest:
        c = _after_label(line, ("Main caveat", "Caveat"))
        if c is not None:
            caveat = f"{caveat} {c}" if caveat else c
            continue
        k = _after_label(line, ("Key number", "Key numbers"))
        line = line if k is None else k
        a, sep, b = line.partition(": ")
        if sep and rust_trim(a) and len(a.encode("utf-8", "surrogatepass")) <= 80:
            keys.append(KeyNumber(rust_trim(a), rust_trim(b), ""))
        else:
            keys.append(KeyNumber("", line, ""))
    return AtAGlance(verdict, keys, caveat)


_PRACTICE_LABELS = ((1, ("Default choice",)), (2, ("Who may benefit",)),
                    (3, ("Counselling", "Counseling")), (4, ("Cost per case",)))


def parse_practice(text: str) -> ClinicalPractice | None:
    """The clinical-practice text by its labelled lines; a line without a label continues
    the one before; text before any label is kept in `text`. None when empty."""
    if not rust_trim(text):
        return None
    slots = ["", "", "", "", ""]  # text, default, who, counselling, cost
    at = 0
    for raw in _rust_lines(text):
        line = _unbullet(raw)
        if not line:
            continue
        found, body = at, line
        for n, labels in _PRACTICE_LABELS:
            b = _after_label(line, labels)
            if b is not None:
                found, body = n, b
                break
        at = found
        slots[found] = f"{slots[found]} {body}" if slots[found] else body
    return ClinicalPractice(default_choice=slots[1], who_may_benefit=slots[2],
                            counselling=slots[3], cost_per_case=slots[4], text=slots[0])


def notable_line(line: str) -> NotableFinding | None:
    t = _unbullet(line)
    return NotableFinding(t, cited_ids(t)) if t else None


def _glance_value(v: Any) -> AtAGlance | None:
    if v is None:
        return None
    if isinstance(v, str):
        return parse_glance(v)
    g = _typed(_GLANCE)(v)
    if g is None:
        return parse_glance(compact_json(v))
    return AtAGlance(g["verdict"], [KeyNumber(**k) for k in g["key_numbers"]],
                     g["main_caveat"])


def _practice_value(v: Any) -> ClinicalPractice | None:
    if v is None:
        return None
    if isinstance(v, str):
        return parse_practice(v)
    c = _typed(_PRACTICE)(v)
    return parse_practice(compact_json(v)) if c is None else ClinicalPractice(**c)


def _notables(v: Any) -> list[NotableFinding]:
    if v is None:
        return []
    out: list[NotableFinding] = []
    if isinstance(v, list):
        for item in v:
            if isinstance(item, str):
                n = notable_line(item)
            else:
                w = _typed(_NOTABLE)(item)
                n = (notable_line(compact_json(item)) if w is None
                     else NotableFinding(w["text"], w["pmids"]))
            if n is not None:
                out.append(n)
        return out
    if isinstance(v, str):
        return [n for n in (notable_line(x) for x in _rust_lines(v)) if n is not None]
    n = notable_line(compact_json(v))
    return [n] if n is not None else []


def into_answer(wire: dict) -> tuple[WrittenAnswer, int]:
    """The answer from a typed whole reply, and how many certainty labels were not
    recognised (each set to Very low; flag, never fail). A CI bound written as text that is
    not a number is kept in its number's label as "CI as written: …"."""
    defaulted = 0

    def cert(s: str) -> str:
        nonlocal defaulted
        c = parse_certainty(s)
        if c is None:
            defaulted += 1
            return "Very low"
        return c

    b = wire["bottom_line"]
    bottom = BottomLine(text=b["text"], certainty=cert(b["certainty"]),
                        certainty_reason=b["certainty_reason"])
    outcomes = []
    for o in wire["outcomes"]:
        certainty = cert(o["certainty"])
        numbers = []
        for n in o["numbers"]:
            label = n["label"]
            bounds = []
            for raw in (n["ci_low"], n["ci_high"]):
                x = _opt_number(raw)
                if x is None and isinstance(raw, str) and rust_trim(raw):
                    label += f" (CI as written: {rust_trim(raw)})"
                bounds.append(x)
            numbers.append(Number(label=label, value=n["value"], unit=_opt_text(n["unit"]),
                                  arm=_opt_text(n["arm"]), ci_low=bounds[0], ci_high=bounds[1],
                                  p=_opt_text(n["p"]), pmid=n["pmid"]))
        outcomes.append(Outcome(outcome=o["outcome"], rct_evidence=o["rct_evidence"],
                                effect_text=o["effect_text"], numbers=numbers,
                                certainty=certainty, why=o["why"], pmids=list(o["pmids"])))
    studies = [Study(pmid=s["pmid"], label=s["label"], design=s["design"],
                     n=_opt_count(s["n"]), setting=s["setting"], comparison=s["comparison"],
                     key_results=s["key_results"], appraisal=s["appraisal"])
               for s in wire["studies"]]
    a = WrittenAnswer(
        bottom_line=bottom,
        outcomes=outcomes,
        studies=studies,
        conflicts=[Conflict(c["text"], list(c["pmids"])) for c in wire["conflicts"]],
        applicability=Applicability(**wire["applicability"]),
        would_change=list(wire["would_change"]),
        at_a_glance=_glance_value(wire["at_a_glance"]),
        clinical_practice=_practice_value(wire["clinical_practice"]),
        notable_findings=_notables(wire["notable_findings"]),
    )
    return a, defaulted


# ---------- citations ----------

_WS = "[\t\n\x0b\x0c\r \x85\xa0  -     　]"
# Rust regex `\d` is Unicode (\p{Nd}), as Python's `\d` on str; `\s` is spelled out (Python's
# `\s` also matches U+001C..U+001F).
CITE = re.compile(rf"\[{_WS}*(?:PMIDs?:?{_WS}*)?\d{{1,9}}"
                  rf"(?:{_WS}*[;,]{_WS}*(?:PMIDs?:?{_WS}*)?\d{{1,9}})*{_WS}*\]")
_DIGITS = re.compile(r"\d{1,9}")
MIN_PMID_DIGITS = 5


def split_citations(text: str, known: Callable[[str], bool]) -> list[tuple[str, Any]]:
    """`text` cut into ("text", s) and ("cite", [ids]) runs. A bracket of numbers is a
    citation only if one id is `known` or any id has at least 5 digits (UTF-8 bytes, as
    Rust's `len`); otherwise ("LOS 2 [1, 3] days") it stays text."""
    out: list[tuple[str, Any]] = []
    plain = ""
    last = 0
    for m in CITE.finditer(text):
        ids = _DIGITS.findall(m.group(0))
        citation = any(known(i) for i in ids) or any(
            len(i.encode("utf-8")) >= MIN_PMID_DIGITS for i in ids)
        plain += text[last:m.start()]
        if citation:
            if plain:
                out.append(("text", plain))
                plain = ""
            out.append(("cite", ids))
        else:
            plain += m.group(0)
        last = m.end()
    plain += text[last:]
    if plain:
        out.append(("text", plain))
    return out


def cited_ids(text: str) -> list[str]:
    """The PMIDs of the citation markers in `text` (ids of 5+ digits), each once."""
    out: list[str] = []
    for kind, ids in split_citations(text, lambda _p: False):
        if kind == "cite":
            out.extend(i for i in ids if i not in out)
    return out


# ---------- code-only steps after the writer ----------


def design_class(label: str) -> str | None:
    """`rct`, `ma` or `other` from the researcher's design label; None otherwise."""
    lab = "".join(ch.lower() if ch.isascii() else ch for ch in rust_trim(label))
    if lab == "rct" or lab.startswith("found by the skeptic"):
        return "rct"
    if lab == "sr_ma":
        return "ma"
    if lab == "other":
        return "other"
    return None


def evidence_types(a: WrittenAnswer, design_of: Callable[[str], str | None]) -> None:
    """Each outcome's evidence label from the designs of the studies it cites (its `pmids`
    and the markers of its evidence and effect text). A label only; it blocks nothing."""
    for o in a.outcomes:
        ids = [rust_trim(p) for p in o.pmids]
        for t in (o.rct_evidence, o.effect_text):
            ids.extend(cited_ids(t))
        kinds = {k for k in (design_of(p) for p in ids) if k is not None}
        key = ("rct" in kinds, "ma" in kinds, "other" in kinds)
        o.evidence_type = {
            (False, False, False): "unknown",
            (True, False, False): "rct",
            (False, True, False): "meta_analysis",
            (True, True, False): "rcts_and_meta_analyses",
            (False, False, True): "other",
        }.get(key, "mixed")


_RANK = {"High": 3, "Moderate": 2, "Low": 1, "Very low": 0}
NAME_STOP = ("with", "from", "than", "versus", "after", "mean", "median", "rate")
MERGE_LEAD = "Consistency check (answer written in four parts):"


def _name_words(s: str) -> list[str]:
    # Rust splits on `!char::is_alphanumeric` (Estimate: Python's \w less `_` is the same set
    # for the words of outcome names), lower-cases each piece, then filters.
    words = (w.lower() for w in re.split(r"[^\w]|_", s))
    return [w for w in words if len(w) >= 4 and w not in NAME_STOP]


def _names_an_outcome(label: str, outcomes: list[Outcome]) -> bool:
    lab = rust_trim(label).lower()
    lw = _name_words(lab)
    for o in outcomes:
        n = rust_trim(o.outcome).lower()
        if n and (n in lab or lab in n):
            return True
        if any(w in lw for w in _name_words(n)):
            return True
    return False


def merge_checks(a: WrittenAnswer) -> list[str]:
    """The checks after merging the four parts: notes only, nothing changed."""
    notes: list[str] = []
    listed = [rust_trim(s.pmid) for s in a.studies]
    missing: list[str] = []
    for o in a.outcomes:
        ids = [rust_trim(p) for p in o.pmids]
        ids += cited_ids(o.rct_evidence) + cited_ids(o.effect_text) + cited_ids(o.why)
        ids += [rust_trim(n.pmid) for n in o.numbers]
        for i in ids:
            if i and i not in listed and i not in missing:
                missing.append(i)
    if missing:
        what = "1 study" if len(missing) == 1 else f"{len(missing)} studies"
        notes.append(f"{MERGE_LEAD} the outcome table cites {what} the trials table does not "
                     f"list (PMID {', '.join(missing)}).")
    if a.at_a_glance is not None:
        unnamed = [f"“{lab}”" for lab in (rust_trim(k.label)
                                                    for k in a.at_a_glance.key_numbers)
                   if lab and not _names_an_outcome(lab, a.outcomes)]
        if unnamed:
            what = "1 outcome" if len(unnamed) == 1 else f"{len(unnamed)} outcomes"
            notes.append(f"{MERGE_LEAD} At a glance names {what} not in the outcome table "
                         f"({', '.join(unnamed)}).")
    if a.outcomes:
        top = max((o.certainty for o in a.outcomes), key=lambda c: _RANK[c])
        if _RANK[a.bottom_line.certainty] > _RANK[top]:
            notes.append(f"{MERGE_LEAD} the bottom line's certainty ({a.bottom_line.certainty}) "
                         f"is higher than any outcome's (highest: {top}); both are shown as "
                         "written.")
    return notes


# ---------- the stored JSON ----------


def _answer_json(a: WrittenAnswer) -> dict:
    def practice(c: ClinicalPractice) -> dict:
        d = {"default_choice": c.default_choice, "who_may_benefit": c.who_may_benefit,
             "counselling": c.counselling, "cost_per_case": c.cost_per_case}
        if c.text:
            d["text"] = c.text
        return d

    return {
        "bottom_line": vars(a.bottom_line).copy(),
        "outcomes": [{**{k: v for k, v in vars(o).items() if k != "numbers"},
                      "pmids": list(o.pmids),
                      "numbers": [vars(n).copy() for n in o.numbers]} for o in a.outcomes],
        "studies": [vars(s).copy() for s in a.studies],
        "conflicts": [{"text": c.text, "pmids": list(c.pmids)} for c in a.conflicts],
        "applicability": vars(a.applicability).copy(),
        "would_change": list(a.would_change),
        "at_a_glance": None if a.at_a_glance is None else {
            "verdict": a.at_a_glance.verdict,
            "key_numbers": [vars(k).copy() for k in a.at_a_glance.key_numbers],
            "main_caveat": a.at_a_glance.main_caveat},
        "clinical_practice": None if a.clinical_practice is None else practice(
            a.clinical_practice),
        "notable_findings": [{"text": n.text, "pmids": list(n.pmids)}
                             for n in a.notable_findings],
    }


def body_to_json(b: AnswerBody) -> dict:
    """The stored document (Incisive `AnswerV3`'s field names, Quick fields only)."""
    return {
        "version": b.version,
        "mode": b.mode,
        "created_at": b.created_at,
        "answer": None if b.answer is None else _answer_json(b.answer),
        "empty_reason": b.empty_reason,
        "references": [vars(r).copy() for r in b.references],
        "searches": [{"query": s.query, "source": s.source, "hits": s.hits} for s in b.searches],
        "integrity": vars(b.integrity).copy(),
        "stopped_early": b.stopped_early,
        "elapsed_s": b.elapsed_s,
        "notes": list(b.notes),
        "untraced": list(b.untraced),
        "numbers_completed": list(b.numbers_completed),
    }


class _Bad(Exception):
    """Stored JSON of the wrong shape (internal; never leaves this module)."""


def _s(v: Any) -> str:
    if not isinstance(v, str):
        raise _Bad
    return v


def _os(v: Any) -> str | None:
    return None if v is None else _s(v)


def _i(v: Any, lo: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < lo:
        raise _Bad
    return v


def _oi(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise _Bad
    return v


def _f(v: Any) -> float:
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise _Bad
    x = float(v)
    if not math.isfinite(x):
        raise _Bad
    return x


def _of(v: Any) -> float | None:
    return None if v is None else _f(v)


def _b(v: Any) -> bool:
    if not isinstance(v, bool):
        raise _Bad
    return v


def _l(v: Any) -> list:
    if not isinstance(v, list):
        raise _Bad
    return v


def _ls(v: Any) -> list[str]:
    return [_s(x) for x in _l(v)]


def _d(v: Any, keys: tuple[str, ...]) -> dict:
    if not isinstance(v, dict) or any(k not in v for k in keys):
        raise _Bad
    return v


def _cert(v: Any) -> str:
    if v not in CERTAINTIES:
        raise _Bad
    return v


def _read_answer(v: Any) -> WrittenAnswer:
    d = _d(v, ("bottom_line", "outcomes", "studies", "conflicts", "applicability",
               "would_change", "at_a_glance", "clinical_practice", "notable_findings"))
    bl = _d(d["bottom_line"], ("text", "certainty", "certainty_reason"))
    outcomes = []
    for o in _l(d["outcomes"]):
        o = _d(o, ("outcome", "rct_evidence", "effect_text", "numbers", "certainty", "why",
                   "pmids", "evidence_type"))
        if o["evidence_type"] not in EVIDENCE_TYPES:
            raise _Bad
        numbers = []
        for n in _l(o["numbers"]):
            n = _d(n, ("label", "value", "unit", "arm", "ci_low", "ci_high", "p", "pmid"))
            numbers.append(Number(_s(n["label"]), _f(n["value"]), _os(n["unit"]), _os(n["arm"]),
                                  _of(n["ci_low"]), _of(n["ci_high"]), _os(n["p"]),
                                  _s(n["pmid"])))
        outcomes.append(Outcome(_s(o["outcome"]), _s(o["rct_evidence"]), _s(o["effect_text"]),
                                numbers, _cert(o["certainty"]), _s(o["why"]), _ls(o["pmids"]),
                                o["evidence_type"]))
    studies = []
    for s in _l(d["studies"]):
        s = _d(s, ("pmid", "label", "design", "n", "setting", "comparison", "key_results",
                   "appraisal"))
        studies.append(Study(_s(s["pmid"]), _s(s["label"]), _s(s["design"]), _oi(s["n"]),
                             _s(s["setting"]), _s(s["comparison"]), _s(s["key_results"]),
                             _s(s["appraisal"])))
    conflicts = [Conflict(_s(c["text"]), _ls(c["pmids"]))
                 for c in (_d(x, ("text", "pmids")) for x in _l(d["conflicts"]))]
    ap = _d(d["applicability"], ("case_mix", "india_cost_context", "training"))
    glance = None
    if d["at_a_glance"] is not None:
        g = _d(d["at_a_glance"], ("verdict", "key_numbers", "main_caveat"))
        keys = [KeyNumber(_s(k["label"]), _s(k["value_text"]), _s(k["pmid"]))
                for k in (_d(x, ("label", "value_text", "pmid")) for x in _l(g["key_numbers"]))]
        glance = AtAGlance(_s(g["verdict"]), keys, _s(g["main_caveat"]))
    practice = None
    if d["clinical_practice"] is not None:
        c = _d(d["clinical_practice"], ("default_choice", "who_may_benefit", "counselling",
                                        "cost_per_case"))
        practice = ClinicalPractice(_s(c["default_choice"]), _s(c["who_may_benefit"]),
                                    _s(c["counselling"]), _s(c["cost_per_case"]),
                                    _s(c.get("text", "")))
    notables = [NotableFinding(_s(n["text"]), _ls(n["pmids"]))
                for n in (_d(x, ("text", "pmids")) for x in _l(d["notable_findings"]))]
    return WrittenAnswer(
        BottomLine(_s(bl["text"]), _cert(bl["certainty"]), _s(bl["certainty_reason"])),
        outcomes, studies, conflicts,
        Applicability(_s(ap["case_mix"]), _s(ap["india_cost_context"]), _s(ap["training"])),
        _ls(d["would_change"]), glance, practice, notables)


def body_from_json(obj: object) -> AnswerBody | None:
    """The stored document, read defensively: a wrong type anywhere gives None (the page
    then says the answer could not be read). Never raises."""
    try:
        d = _d(obj, ("version", "mode", "created_at", "answer", "empty_reason", "references",
                     "searches", "integrity", "stopped_early", "elapsed_s", "notes"))
        if d["version"] != 3 or isinstance(d["version"], bool) or d["mode"] != "quick":
            raise _Bad
        refs = []
        for r in _l(d["references"]):
            r = _d(r, ("n", "pmid", "title", "journal", "year", "doi", "retracted"))
            refs.append(Reference(_i(r["n"], 1), _s(r["pmid"]), _s(r["title"]),
                                  _os(r["journal"]), _oi(r["year"]), _os(r["doi"]),
                                  _b(r["retracted"])))
        searches = [SearchEntry(_s(s["query"]), _s(s["source"]), _i(s["hits"]))
                    for s in (_d(x, ("query", "source", "hits")) for x in _l(d["searches"]))]
        ig = _d(d["integrity"], tuple(Integrity.__dataclass_fields__))
        integrity = Integrity(**{k: _i(ig[k]) for k in Integrity.__dataclass_fields__})
        return AnswerBody(
            created_at=_s(d["created_at"]),
            answer=None if d["answer"] is None else _read_answer(d["answer"]),
            empty_reason=_os(d["empty_reason"]),
            references=refs,
            searches=searches,
            integrity=integrity,
            stopped_early=_os(d["stopped_early"]),
            elapsed_s=_i(d["elapsed_s"]),
            notes=_ls(d["notes"]),
            untraced=list(_l(d.get("untraced", []))),
            numbers_completed=list(_l(d.get("numbers_completed", []))),
        )
    except _Bad:
        return None
