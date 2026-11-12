"""Reading the writer's replies, and turning them into the answer, as Incisive does.

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/agent/writer_v3.rs`, `w.rs:N`):

| Here | Incisive |
|---|---|
| `parse_certainty` | `w.rs:282-296` |
| `opt_text`, `opt_number`, `opt_count` | `w.rs:298-329` |
| wire readers (`as_whole`; the private `_WIRE_*`) | `w.rs:331-406` (`WireNumber` .. `WireAnswer`) |
| `cited_ids` | `w.rs:407-420` |
| `unbullet`, `after_label` | `w.rs:422-443` |
| `parse_glance` | `w.rs:445-490` |
| `parse_practice` | `w.rs:492-536` |
| `notable_line` | `w.rs:538-550` |
| `evidence_types` | `w.rs:552-578` |
| `design_class` | `w.rs:580-596` |
| `text_or` | `w.rs:597-605` |
| `into_answer` | `w.rs:607-731` (`WireAnswer::into_answer`) |
| `as_trials`, `as_outcomes`, `as_context`, `as_summary` | `w.rs:1965-1995` (`TrialsPart` .. `SummaryPart`) |
| `merge_sections` | `w.rs:1997-2016` |
| `early_answer` | `w.rs:2037-2073` |
| `merge_checks` (`certainty_rank`, `NAME_STOP`, `name_words`, `names_an_outcome`) | `w.rs:2312-2420` |

A wire value is a dict of the Rust struct's fields, exactly as serde read it
(`gemini/structured.py`): `Option<Value>` fields hold a JSON value or None.

Rust string rules kept here: lines split as `str::lines` (`rust_lines`, never `splitlines`);
`trim` is Unicode White_Space (`rust_trim`); `after_label` slices the original line by the
label's length in bytes; a label's 80 is bytes; `to_lowercase`, `is_alphanumeric` and the
regex classes are Rust's (`writer/_rust_unicode.py`); `to_ascii_lowercase` lowers ASCII only.
"""

import math
import re
from collections.abc import Callable
from typing import Any

from pubmedgemini.gemini.structured import (
    Field,
    from_value,
    option,
    rust_trim,
    rust_trim_start,
    string,
    struct,
    typed,
    value,
    value_string,
    vec,
)
from pubmedgemini.gemini.structured import f64 as _f64
from pubmedgemini.writer._rust_unicode import rust_is_alphanumeric, rust_lower
from pubmedgemini.writer.answer import (
    Applicability,
    AtAGlance,
    BottomLine,
    Certainty,
    Cite,
    ClinicalPractice,
    Conflict,
    EvidenceType,
    KeyNumber,
    NotableFinding,
    Number,
    Outcome,
    Study,
    WrittenAnswer,
    split_citations,
)

__all__ = [
    "after_label", "as_context", "as_outcomes", "as_summary", "as_trials", "as_whole",
    "cited_ids", "design_class", "early_answer", "evidence_types", "into_answer",
    "merge_checks", "merge_sections", "notable_line", "opt_count", "opt_number", "opt_text",
    "parse_certainty", "parse_glance", "parse_practice", "rust_lines", "unbullet",
]


def rust_lines(s: str) -> list[str]:
    """Rust's `str::lines`: split on `\\n`, one `\\r` before it dropped; no empty last line
    after a final `\\n`. (A `\\r` at the very end, with no `\\n`, stays.)"""
    if not s:
        return []
    parts = s.split("\n")
    last = parts.pop()
    out = [p.removesuffix("\r") for p in parts]
    if last:
        out.append(last)
    return out


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def _ascii_lower(s: str) -> str:
    """Rust's `to_ascii_lowercase`: A-Z only."""
    return "".join(chr(ord(c) + 32) if "A" <= c <= "Z" else c for c in s)


# ---------- the reply, read leniently and checked in code ----------


def parse_certainty(s: str) -> Certainty | None:
    """A certainty word, case and separators ignored ("Very low", "very_low", "VERY LOW").
    None when it is none of the four."""
    k = _ascii_lower("".join(c for c in s if "A" <= c <= "Z" or "a" <= c <= "z"))
    return {"high": Certainty.HIGH, "moderate": Certainty.MODERATE, "low": Certainty.LOW,
            "verylow": Certainty.VERY_LOW}.get(k)


def opt_text(v: str | None) -> str | None:
    """Optional text: None for null or blank (the schema's `""` sentinel)."""
    if v is None:
        return None
    t = rust_trim(v)
    return t or None


# Rust's `str::parse::<f64>` grammar, finite numbers only (inf and NaN are dropped after).
_RUST_F64 = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_RUST_I64 = re.compile(r"[+-]?[0-9]+")


def opt_number(v: Any) -> float | None:
    """A number written as JSON number or text (`"-0.34"`, `"−0.34"`); None for null, blank or
    anything else. Text is parsed as Rust's `str::parse::<f64>` (correctly rounded, as
    Python's `float`, on the same grammar)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return float(v)
    if isinstance(v, float):
        return v
    if isinstance(v, str):
        t = rust_trim(v).replace("−", "-").replace("–", "-")
        if not _RUST_F64.fullmatch(t):
            return None
        x = float(t)
        return x if math.isfinite(x) else None
    return None


def opt_count(v: Any) -> int | None:
    """A positive integer written as JSON number or text; None for null, 0 (the sentinel),
    blank or anything else (a float such as 60.0 included: serde_json's `as_i64`)."""
    n: int | None = None
    if v is None or isinstance(v, (bool, float)):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, str):
        t = rust_trim(v)
        if _RUST_I64.fullmatch(t):
            n = int(t)
    if n is None or not -(2**63) <= n < 2**63:
        return None
    return n if n > 0 else None


def _none() -> None:
    return None


# The wire's structs (w.rs:331-406, 1965-1995), fields in declaration order (serde reads an
# array into a struct field by field).
_WIRE_NUMBER = struct(
    Field("label", string),
    Field("value", _f64),
    Field("unit", option(string), _none),
    Field("arm", option(string), _none),
    Field("ci_low", value, _none),
    Field("ci_high", value, _none),
    Field("p", option(string), _none),
    Field("pmid", string),
)
_WIRE_OUTCOME = struct(
    Field("outcome", string),
    Field("rct_evidence", string),
    Field("effect_text", string),
    Field("numbers", vec(_WIRE_NUMBER)),
    Field("certainty", string),
    Field("why", string),
    Field("pmids", vec(string)),
)
_WIRE_STUDY = struct(
    Field("pmid", string),
    Field("label", string),
    Field("design", string),
    Field("n", value, _none),
    Field("setting", string),
    Field("comparison", string),
    Field("key_results", string),
    Field("appraisal", string),
)
_WIRE_BOTTOM_LINE = struct(
    Field("text", string),
    Field("certainty", string),
    Field("certainty_reason", string),
)
_CONFLICT = struct(Field("text", string), Field("pmids", vec(string)))  # a.rs:313-317
_APPLICABILITY = struct(  # a.rs:319-325
    Field("case_mix", string),
    Field("india_cost_context", string),
    Field("training", string),
)
_WIRE_NOTABLE = struct(Field("text", string), Field("pmids", vec(string), list))
_KEY_NUMBER = struct(Field("label", string), Field("value_text", string), Field("pmid", string))
_AT_A_GLANCE = struct(  # a.rs:171-177
    Field("verdict", string),
    Field("key_numbers", vec(_KEY_NUMBER)),
    Field("main_caveat", string),
)
_CLINICAL_PRACTICE = struct(  # a.rs:179-193
    Field("default_choice", string),
    Field("who_may_benefit", string),
    Field("counselling", string),
    Field("cost_per_case", string),
    Field("text", string, str),
)
_WIRE_ANSWER = struct(
    Field("bottom_line", _WIRE_BOTTOM_LINE),
    Field("outcomes", vec(_WIRE_OUTCOME)),
    Field("studies", vec(_WIRE_STUDY)),
    Field("conflicts", vec(_CONFLICT)),
    Field("applicability", _APPLICABILITY),
    Field("would_change", vec(string)),
    Field("at_a_glance", value, _none),
    Field("clinical_practice", value, _none),
    Field("notable_findings", value, _none),
)
_TRIALS_PART = struct(
    Field("studies", vec(_WIRE_STUDY)),
    Field("conflicts", vec(_CONFLICT)),
    Field("notable_findings", value, _none),
)
_OUTCOMES_PART = struct(Field("outcomes", vec(_WIRE_OUTCOME)))
_CONTEXT_PART = struct(Field("applicability", _APPLICABILITY), Field("would_change", vec(string)))
_SUMMARY_PART = struct(
    Field("bottom_line", _WIRE_BOTTOM_LINE),
    Field("at_a_glance", value, _none),
    Field("clinical_practice", value, _none),
)

# Typed readers for `structured.read_structured` (and serde's `from_value` on a JSON value).
as_whole = typed(_WIRE_ANSWER)  # WireAnswer: the one-piece writer
as_trials = typed(_TRIALS_PART)
as_outcomes = typed(_OUTCOMES_PART)
as_context = typed(_CONTEXT_PART)
as_summary = typed(_SUMMARY_PART)


def cited_ids(text: str) -> list[str]:
    """The PMIDs of the citation markers in `text` (ids of 5+ digits), each once, in order."""
    out: list[str] = []
    for seg in split_citations(text, lambda _: False):
        if isinstance(seg, Cite):
            for i in seg.ids:
                if i not in out:
                    out.append(i)
    return out


def unbullet(line: str) -> str:
    """A line without a list bullet ("- ", "• ", "* ", "– ") and outer blanks."""
    t = rust_trim(line)
    for b in ("- ", "• ", "* ", "– "):
        if t.startswith(b):
            return rust_trim(t[len(b):])
    return t


def after_label(line: str, labels: tuple[str, ...]) -> str | None:
    """`line` without a leading label (case ignored) and the colon after it. As Rust, the
    rest is the original line from byte `len(label)` on."""
    low = rust_lower(line)
    raw = line.encode("utf-8")
    for label in labels:
        if not low.startswith(rust_lower(label)):
            continue
        rest = rust_trim_start(raw[_utf8_len(label):].decode("utf-8"))
        if rest.startswith(":"):
            return rust_trim(rest[1:])
    return None


def parse_glance(text: str) -> AtAGlance | None:
    """The at-a-glance text (flat), parsed: the first line is the verdict ("Verdict:"
    optional), a "Main caveat:" / "Caveat:" line the caveat, every other line a key number
    ("label: value [PMID]", or the whole line as its value). Every line is kept; markers stay
    in the text. None when empty."""
    lines = [x for x in (unbullet(raw) for raw in rust_lines(text)) if x]
    if not lines:
        return None
    first, rest = lines[0], lines[1:]
    v = after_label(first, ("Verdict", "At a glance"))
    verdict = first if v is None else v
    main_caveat = ""
    key_numbers: list[KeyNumber] = []
    for line in rest:
        c = after_label(line, ("Main caveat", "Caveat"))
        if c is not None:
            if main_caveat:
                main_caveat += " "
            main_caveat += c
            continue
        k = after_label(line, ("Key number", "Key numbers"))
        line = line if k is None else k
        a, sep, b = line.partition(": ")
        if sep and rust_trim(a) and _utf8_len(a) <= 80:
            label, value_text = rust_trim(a), rust_trim(b)
        else:
            label, value_text = "", line
        key_numbers.append(KeyNumber(label=label, value_text=value_text, pmid=""))
    return AtAGlance(verdict=verdict, key_numbers=key_numbers, main_caveat=main_caveat)


def parse_practice(text: str) -> ClinicalPractice | None:
    """The clinical-practice text (flat), parsed by its labelled lines ("Default choice:",
    "Who may benefit:", "Counselling:", "Cost per case:"; a line without a label continues
    the one before). Text before any label is kept in `text`. None when empty."""
    if not rust_trim(text):
        return None
    slots = ["", "", "", "", ""]  # text, default_choice, who_may_benefit, counselling, cost
    at = 0
    for raw in rust_lines(text):
        line = unbullet(raw)
        if not line:
            continue
        for field, labels in ((1, ("Default choice",)), (2, ("Who may benefit",)),
                              (3, ("Counselling", "Counseling")), (4, ("Cost per case",))):
            body = after_label(line, labels)
            if body is not None:
                break
        else:
            field, body = at, line
        at = field
        if slots[field]:
            slots[field] += " "
        slots[field] += body
    return ClinicalPractice(default_choice=slots[1], who_may_benefit=slots[2],
                            counselling=slots[3], cost_per_case=slots[4], text=slots[0])


def notable_line(line: str) -> NotableFinding | None:
    """One notable finding from a line: its text without a bullet, and the PMIDs of its
    markers. None for a blank line."""
    t = unbullet(line)
    return NotableFinding(pmids=cited_ids(t), text=t, spans=[]) if t else None


def evidence_types(a: WrittenAnswer, design_of: Callable[[str], str | None]) -> None:
    """The evidence type of each outcome row, derived in code from the designs of the studies
    it cites (its `pmids` and the markers of its evidence and effect text): all RCTs → rct;
    all meta-analyses → meta_analysis; both → rcts_and_meta_analyses; all `other` → other;
    any mix with `other` → mixed; nothing known → unknown. `design_of` gives a PMID's class
    (`design_class`)."""
    for o in a.outcomes:
        ids = [rust_trim(p) for p in o.pmids]
        for t in (o.rct_evidence, o.effect_text):
            ids.extend(cited_ids(t))
        kinds = {k for k in (design_of(p) for p in ids) if k is not None}
        o.evidence_type = {
            (False, False, False): EvidenceType.UNKNOWN,
            (True, False, False): EvidenceType.RCT,
            (False, True, False): EvidenceType.META_ANALYSIS,
            (True, True, False): EvidenceType.RCTS_AND_META_ANALYSES,
            (False, False, True): EvidenceType.OTHER,
        }.get(("rct" in kinds, "ma" in kinds, "other" in kinds), EvidenceType.MIXED)


def design_class(label: str) -> str | None:
    """The design class of a study's researcher label (`RCT`, `SR_MA`, `other`; a trial the
    skeptic found missing is an RCT): "rct", "ma" or "other". None for anything else."""
    low = _ascii_lower(rust_trim(label))
    if low == "rct" or low.startswith("found by the skeptic"):
        return "rct"
    if low == "sr_ma":
        return "ma"
    if low == "other":
        return "other"
    return None


def _text_or(v: Any, reader: Callable[[Any], dict]) -> tuple[dict | None, str | None]:
    """A value the wire gave as text or as an object: (the typed value, None), or (None, the
    text), the text being the value's JSON when it does not read as the type (kept, never
    dropped)."""
    if isinstance(v, str):
        return None, v
    got = from_value(reader, v)
    if got is None:
        return None, value_string(v)
    return got, None


def into_answer(wire: dict) -> tuple[WrittenAnswer, int]:
    """The answer, and how many labels were not recognised (flag, never fail): an unknown
    certainty becomes Very low; a CI bound that is not a number is kept, written into its
    number's label as "CI as written: …". The evidence type is left to `evidence_types`."""
    defaulted = 0

    def cert(s: str) -> Certainty:
        nonlocal defaulted
        c = parse_certainty(s)
        if c is None:
            defaulted += 1
            return Certainty.VERY_LOW
        return c

    bl = wire["bottom_line"]
    bottom_line = BottomLine(certainty=cert(bl["certainty"]), text=bl["text"],
                             certainty_reason=bl["certainty_reason"])
    outcomes = []
    for o in wire["outcomes"]:
        certainty = cert(o["certainty"])
        numbers = []
        for n in o["numbers"]:
            label = n["label"]
            bounds = []
            for v in (n["ci_low"], n["ci_high"]):
                x = opt_number(v)
                if x is None and isinstance(v, str) and rust_trim(v):
                    label += f" (CI as written: {rust_trim(v)})"
                bounds.append(x)
            numbers.append(Number(label=label, value=n["value"], unit=opt_text(n["unit"]),
                                  arm=opt_text(n["arm"]), ci_low=bounds[0], ci_high=bounds[1],
                                  p=opt_text(n["p"]), pmid=n["pmid"]))
        outcomes.append(Outcome(outcome=o["outcome"], rct_evidence=o["rct_evidence"],
                                effect_text=o["effect_text"], numbers=numbers,
                                certainty=certainty, why=o["why"], pmids=list(o["pmids"]),
                                evidence_type=EvidenceType.UNKNOWN))
    studies = [Study(n=opt_count(x["n"]), pmid=x["pmid"], label=x["label"],
                     design=x["design"], setting=x["setting"], comparison=x["comparison"],
                     key_results=x["key_results"], appraisal=x["appraisal"])
               for x in wire["studies"]]
    # One finding per line (or per array item), its studies read from its markers; blank
    # lines are not findings.
    nf = wire["notable_findings"]
    notable: list[NotableFinding] = []
    if isinstance(nf, list):
        for item in nf:
            got, text = _text_or(item, _WIRE_NOTABLE)
            if got is not None:
                notable.append(NotableFinding(text=got["text"], pmids=got["pmids"], spans=[]))
            else:
                one = notable_line(text)
                if one is not None:
                    notable.append(one)
    elif isinstance(nf, str):
        notable = [x for x in (notable_line(line) for line in rust_lines(nf)) if x is not None]
    elif nf is not None:
        one = notable_line(value_string(nf))
        notable = [] if one is None else [one]
    at_a_glance = None
    if wire["at_a_glance"] is not None:
        got, text = _text_or(wire["at_a_glance"], _AT_A_GLANCE)
        if got is not None:
            at_a_glance = AtAGlance(
                verdict=got["verdict"], main_caveat=got["main_caveat"],
                key_numbers=[KeyNumber(**k) for k in got["key_numbers"]])
        else:
            at_a_glance = parse_glance(text)
    clinical_practice = None
    if wire["clinical_practice"] is not None:
        got, text = _text_or(wire["clinical_practice"], _CLINICAL_PRACTICE)
        clinical_practice = ClinicalPractice(**got) if got is not None else parse_practice(text)
    return WrittenAnswer(
        bottom_line=bottom_line,
        outcomes=outcomes,
        studies=studies,
        conflicts=[Conflict(text=c["text"], pmids=list(c["pmids"])) for c in wire["conflicts"]],
        applicability=Applicability(**wire["applicability"]),
        would_change=list(wire["would_change"]),
        at_a_glance=at_a_glance,
        clinical_practice=clinical_practice,
        guidelines=[],  # the writer no longer writes guidelines (w.rs:722-724)
        notable_findings=notable,
    ), defaulted


# ---------- the four parts ----------


def merge_sections(t: dict, o: dict, c: dict, s: dict) -> dict:
    """The four parts as one reply, of the single writer's shape (each field taken from the
    part that owns it, never from another)."""
    return {
        "bottom_line": s["bottom_line"],
        "outcomes": o["outcomes"],
        "studies": t["studies"],
        "conflicts": t["conflicts"],
        "applicability": c["applicability"],
        "would_change": c["would_change"],
        "at_a_glance": s["at_a_glance"],
        "clinical_practice": s["clinical_practice"],
        "notable_findings": t["notable_findings"],
    }


def early_answer(summary: dict | None, context: dict | None) -> WrittenAnswer:
    """The answer holding only `summary` and `context` (either may be absent), read exactly
    as the merged four parts are (`merge_sections`, then `into_answer`), every other field
    empty. Each field of a part present equals the merged answer's for the same replies."""
    trials = {"studies": [], "conflicts": [], "notable_findings": None}
    outcomes = {"outcomes": []}
    if context is None:
        context = {"applicability": {"case_mix": "", "india_cost_context": "", "training": ""},
                   "would_change": []}
    if summary is None:
        summary = {"bottom_line": {"text": "", "certainty": "", "certainty_reason": ""},
                   "at_a_glance": None, "clinical_practice": None}
    return into_answer(merge_sections(trials, outcomes, context, summary))[0]


# ---------- the merge checks ----------

_LEAD = "Consistency check (answer written in four parts):"
_RANK = {Certainty.HIGH: 3, Certainty.MODERATE: 2, Certainty.LOW: 1, Certainty.VERY_LOW: 0}
# Words that say little about which outcome a label names.
NAME_STOP = ("with", "from", "than", "versus", "after", "mean", "median", "rate")


def name_words(s: str) -> list[str]:
    """`s` split on every character that is not alphanumeric (Rust's), lowered, words of 4+
    characters that are not in NAME_STOP."""
    words, cur = [], []
    for ch in s:
        if rust_is_alphanumeric(ch):
            cur.append(ch)
        else:
            words.append("".join(cur))
            cur = []
    words.append("".join(cur))
    return [w for w in (rust_lower(x) for x in words) if len(w) >= 4 and w not in NAME_STOP]


def names_an_outcome(label: str, outcomes: list[Outcome]) -> bool:
    """Whether an at-a-glance label names one of `outcomes` (case ignored: one contains the
    other, or they share a word of 4+ letters). Lenient on purpose."""
    low = rust_lower(rust_trim(label))
    lw = name_words(low)
    for o in outcomes:
        n = rust_lower(rust_trim(o.outcome))
        if (n and (n in low or low in n)) or any(w in lw for w in name_words(n)):
            return True
    return False


def merge_checks(a: WrittenAnswer) -> list[str]:
    """The checks after merging the four parts (code only; flag, never delete, nothing
    rewritten). Each finding is a note for the answer: every PMID the outcomes cite is a row
    of the trials table; every outcome the at-a-glance key numbers name is in the outcome
    table; the bottom line's certainty is not above the highest outcome's."""
    notes: list[str] = []
    listed = [rust_trim(s.pmid) for s in a.studies]
    missing: list[str] = []
    for o in a.outcomes:
        ids = ([rust_trim(p) for p in o.pmids] + cited_ids(o.rct_evidence)
               + cited_ids(o.effect_text) + cited_ids(o.why)
               + [rust_trim(n.pmid) for n in o.numbers])
        for i in ids:
            if i and i not in listed and i not in missing:
                missing.append(i)
    if missing:
        what = "1 study" if len(missing) == 1 else f"{len(missing)} studies"
        notes.append(f"{_LEAD} the outcome table cites {what} the trials table does not list "
                     f"(PMID {', '.join(missing)}).")
    if a.at_a_glance is not None:
        unnamed = [f"“{label}”"
                   for label in (rust_trim(k.label) for k in a.at_a_glance.key_numbers)
                   if label and not names_an_outcome(label, a.outcomes)]
        if unnamed:
            what = "1 outcome" if len(unnamed) == 1 else f"{len(unnamed)} outcomes"
            notes.append(f"{_LEAD} At a glance names {what} not in the outcome table "
                         f"({', '.join(unnamed)}).")
    if a.outcomes:
        top = max((o.certainty for o in a.outcomes), key=lambda c: _RANK[c])
        if _RANK[a.bottom_line.certainty] > _RANK[top]:
            notes.append(f"{_LEAD} the bottom line's certainty ({a.bottom_line.certainty.as_str()}"
                         f") is higher than any outcome's (highest: {top.as_str()}); both are "
                         "shown as written.")
    return notes
