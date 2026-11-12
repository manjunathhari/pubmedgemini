"""The writer's answer (Incisive `WrittenAnswer`) and the citation markers in its text.

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/agent/answer_v3.rs`, `a.rs:N`):

| Here | Incisive |
|---|---|
| `Certainty` | `a.rs:36-66` |
| `BottomLine`, `Number` | `a.rs:68-89` |
| `EvidenceType`, `.label()` | `a.rs:91-129` |
| `Outcome` | `a.rs:131-144` |
| `GuidelineV3` | `a.rs:146-158` |
| `KeyNumber`, `AtAGlance`, `ClinicalPractice`, `NotableFinding` | `a.rs:163-207` |
| `Span` | `a.rs:236-243` |
| `Study`, `Conflict`, `Applicability`, `WrittenAnswer` | `a.rs:295-335` |
| `ReferenceV3` | `a.rs:337-352` |
| `IntegrityV3` | `a.rs:511-549` |
| `CompletionRule`, `CompletedNumber` | `a.rs:551-605` |
| `AnswerV3` (Quick fields), `ANSWER_VERSION` | `a.rs:32-33, 892-944` |
| `CITE`, `DIGITS`, `MIN_PMID_DIGITS`, `split_citations` | `a.rs:979-1031` |
| `clean_citations` | `a.rs:1895-1946` |
| `check_citations` | `w.rs:2641-2772` (`agent/writer_v3.rs`) |
| `references_from` | `t.rs:448-462` (`agent/team.rs`, inline in `run`) |
| `plural`, `citation_check_line` | `t.rs:198-200`, `t.rs:649-669` (inline in `run`) |

Task 4 built the written-answer models and `split_citations`; Task 5 added the stored answer,
the references, the citation check and serde's JSON form.

**The JSON form is serde's** (`AnswerV3.to_body()`, what `serde_json::to_value(&answer)` gives,
`t.rs:176`): field names and order as the Rust structs declare them; `Option` as `null`; `Vec`
as `[]`; left out only where Rust says `skip_serializing_if` (`ClinicalPractice.text` empty,
`NotableFinding.spans` empty, `GuidelineV3.pmid_removed` false, `IntegrityV3.numbers_completed`
0, `AnswerV3.numbers_completed` empty, `coverage` and `completeness` None); `Certainty` by
variant name with `"Very low"`; `EvidenceType`, `CompletionRule` and the mode in snake_case;
`created_at` as chrono writes a `DateTime<Utc>` (RFC 3339, `Z`, no fraction or 3/6 digits).

Rust's regex `\\d` is Unicode `Nd` and `\\s` Unicode White_Space; Python's `re` differs (it lacks
Unicode 16's digits, and its `\\s` also matches U+001C..U+001F), so both classes are spelled
out. An id's length is counted in UTF-8 bytes, as Rust's `str::len`.
"""

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_serializer,
    model_serializer,
)
from pydantic_core import PydanticSerializationError

from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.researcher.loop import SearchEntry
from pubmedgemini.writer._rust_unicode import DIGIT_CLASS, SPACE_CLASS
from pubmedgemini.writer.prompts import StudyInput

ANSWER_VERSION = 3  # a.rs:33 `run_answers.answer_version` of an AnswerV3


class Certainty(StrEnum):
    """GRADE-style certainty."""

    HIGH = "High"
    MODERATE = "Moderate"
    LOW = "Low"
    VERY_LOW = "Very low"

    def as_str(self) -> str:
        return self.value


CERTAINTY_ALL = ("High", "Moderate", "Low", "Very low")  # a.rs:47 Certainty::ALL


class EvidenceType(StrEnum):
    """What kind of evidence an outcome row rests on: a label only; it blocks nothing.
    Derived in code from the cited studies' designs (`read.evidence_types`)."""

    RCT = "rct"
    RCT_META_ANALYSIS = "rct_meta_analysis"
    NETWORK_META_ANALYSIS = "network_meta_analysis"
    META_ANALYSIS = "meta_analysis"
    RCTS_AND_META_ANALYSES = "rcts_and_meta_analyses"
    OTHER = "other"
    OBSERVATIONAL = "observational"
    MIXED = "mixed"
    UNKNOWN = "unknown"

    def label(self) -> str | None:
        """The label drawn under the outcome; None draws nothing."""
        return _EVIDENCE_LABELS[self]


_EVIDENCE_LABELS = {
    EvidenceType.RCT: "RCTs",
    EvidenceType.RCT_META_ANALYSIS: "RCT meta-analysis",
    EvidenceType.NETWORK_META_ANALYSIS: "network meta-analysis",
    EvidenceType.META_ANALYSIS: "meta-analysis",
    EvidenceType.RCTS_AND_META_ANALYSES: "RCTs and meta-analyses",
    EvidenceType.OTHER: "other / non-randomised",
    EvidenceType.OBSERVATIONAL: "observational",
    EvidenceType.MIXED: "mixed evidence",
    EvidenceType.UNKNOWN: None,
}


def _skip(d: dict, **empty: Any) -> dict:
    """`d` without each key whose value is the given empty value (serde's
    `skip_serializing_if`); every other key, `None` included, is kept."""
    return {k: v for k, v in d.items() if not (k in empty and v == empty[k])}


class BottomLine(BaseModel):
    """2-3 sentences and the overall certainty with its reason."""

    text: str
    certainty: Certainty
    certainty_reason: str


class Number(BaseModel):
    """One number, with its source study."""

    label: str
    value: float
    unit: str | None
    arm: str | None  # the arm or group it belongs to, if any
    ci_low: float | None
    ci_high: float | None
    p: str | None  # as written, e.g. "0.431" or "<0.001"
    pmid: str


class Outcome(BaseModel):
    """One row of the outcome table."""

    outcome: str
    rct_evidence: str
    effect_text: str
    numbers: list[Number]
    certainty: Certainty
    why: str
    pmids: list[str]
    evidence_type: EvidenceType = EvidenceType.UNKNOWN


class GuidelineV3(BaseModel):
    """A guideline or consensus statement (Quick leaves the list empty)."""

    pmid: str | None
    body: str
    year: int | None
    statement: str
    pmid_removed: bool = False  # not in the writer's schema; set by the citation check

    @model_serializer(mode="wrap")
    def _serde(self, handler: SerializerFunctionWrapHandler) -> dict:
        # a.rs:157 `skip_serializing_if = "std::ops::Not::not"`
        return _skip(handler(self), pmid_removed=False)


class KeyNumber(BaseModel):
    """One key number of the at-a-glance box."""

    label: str
    value_text: str
    pmid: str


class AtAGlance(BaseModel):
    """The at-a-glance box: about five lines at the top."""

    verdict: str
    key_numbers: list[KeyNumber]
    main_caveat: str


class ClinicalPractice(BaseModel):
    """What the evidence means for clinical practice."""

    default_choice: str
    who_may_benefit: str
    counselling: str
    cost_per_case: str
    text: str = ""  # the writer's text under no label, drawn as written

    @model_serializer(mode="wrap")
    def _serde(self, handler: SerializerFunctionWrapHandler) -> dict:
        # a.rs:191 `skip_serializing_if = "String::is_empty"`
        return _skip(handler(self), text="")


class Span(BaseModel):
    """A marked run of stored text: `len` bytes from byte `start` of the text at `field`."""

    field: str
    start: int
    len: int


class NotableFinding(BaseModel):
    """A notable finding outside the outcome tables."""

    text: str
    pmids: list[str]
    spans: list[Span] = []

    @model_serializer(mode="wrap")
    def _serde(self, handler: SerializerFunctionWrapHandler) -> dict:
        # a.rs:203 `skip_serializing_if = "Vec::is_empty"`
        return _skip(handler(self), spans=[])


class Study(BaseModel):
    """One row of the trials table."""

    pmid: str
    label: str
    design: str
    n: int | None
    setting: str
    comparison: str
    key_results: str
    appraisal: str


class Conflict(BaseModel):
    """Conflicting evidence and why it differs."""

    text: str
    pmids: list[str]


class Applicability(BaseModel):
    """Who and where the evidence applies to."""

    case_mix: str
    india_cost_context: str
    training: str


class WrittenAnswer(BaseModel):
    """The writer's answer (its structured output, read in code)."""

    bottom_line: BottomLine
    outcomes: list[Outcome]
    studies: list[Study]
    conflicts: list[Conflict]
    applicability: Applicability
    would_change: list[str]
    at_a_glance: AtAGlance | None = None
    clinical_practice: ClinicalPractice | None = None
    guidelines: list[GuidelineV3] = []
    notable_findings: list[NotableFinding] = []


# ---------- citation markers ----------

_D = DIGIT_CLASS
_S = SPACE_CLASS
# a.rs:982-984: `[` PMIDs separated by `;` or `,` `]`, each optionally prefixed `PMID`/`PMID:`.
CITE = re.compile(
    rf"\[{_S}*(?:PMIDs?:?{_S}*)?{_D}{{1,9}}(?:{_S}*[;,]{_S}*(?:PMIDs?:?{_S}*)?{_D}{{1,9}})*{_S}*\]")
DIGITS = re.compile(rf"{_D}{{1,9}}")  # a.rs:986-987
MIN_PMID_DIGITS = 5  # bytes (Rust `str::len`) of the shortest id that counts on its own


@dataclass(frozen=True)
class Text:
    """A plain run of answer text."""

    text: str


@dataclass(frozen=True)
class Cite:
    """A citation of these PMIDs."""

    ids: tuple[str, ...]


Segment = Text | Cite


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def split_citations(text: str, known) -> list[Segment]:
    """`text` cut into plain runs and citation runs, in order. A bracket of numbers is a
    citation only if one of its ids is `known` (a selected study) or any id has at least
    MIN_PMID_DIGITS bytes; otherwise (e.g. "LOS 2 [1, 3] days") it stays text."""
    out: list[Segment] = []
    plain = ""
    last = 0
    for m in CITE.finditer(text):
        ids = tuple(d.group() for d in DIGITS.finditer(m.group()))
        citation = any(known(i) for i in ids) or any(_utf8_len(i) >= MIN_PMID_DIGITS
                                                    for i in ids)
        plain += text[last:m.start()]
        if citation:
            if plain:
                out.append(Text(plain))
                plain = ""
            out.append(Cite(ids))
        else:
            plain += m.group()
        last = m.end()
    plain += text[last:]
    if plain:
        out.append(Text(plain))
    return out


def clean_citations(text: str, known: Callable[[str], bool]) -> tuple[str, int]:
    """`text` with every citation marker (`split_citations`) rewritten to keep only PMIDs for
    which `known` is true (de-duplicated, in order, as `[a; b]`); a marker left empty is
    removed with the spaces and tabs before it. Returns the text and how many PMID citations
    were removed. Port of `a.rs:1895-1946`."""
    removed = 0
    out = ""
    for seg in split_citations(text, known):
        if isinstance(seg, Text):
            out += seg.text
        elif not any(known(i) for i in seg.ids):
            # No selected study in it: unknown PMIDs (5+ bytes) are removed and counted; short
            # numbers stay as text.
            short: list[str] = []
            for i in seg.ids:
                if _utf8_len(i) >= MIN_PMID_DIGITS:
                    removed += 1
                else:
                    short.append(i)
            if not short:
                out = out.rstrip(" \t")  # trim_end_matches([' ', '\t'])
            else:
                out += "[" + "; ".join(short) + "]"
        else:
            keep: list[str] = []
            for i in seg.ids:
                if not known(i):
                    removed += 1
                elif i not in keep:
                    keep.append(i)
            if not keep:
                out = out.rstrip(" \t")
            else:
                out += "[" + "; ".join(keep) + "]"
    return out, removed


# ---------- the stored answer ----------

AnswerInvalidKind = Literal["not_quick", "invalid", "unserialisable"]

# Top-level keys whose refusal means a body that is not a Quick AnswerV3 (Full parts, another
# version or mode), as opposed to a Quick body that is malformed.
_NOT_QUICK = frozenset({"version", "mode", "skeptic", "evidence", "coverage", "completeness"})


class AnswerInvalid(Exception):
    """A stored answer, or one of Task 5's models, could not be read, built or written.
    Carries only `kind`: never the input, the question or model text. Raised `from None`
    outside the `except` block, so it has no `__cause__` and no `__context__` (pydantic's
    ValidationError quotes the refused input)."""

    def __init__(self, kind: AnswerInvalidKind) -> None:
        self.kind = kind
        super().__init__(kind)


def _kind_of(e: ValidationError) -> AnswerInvalidKind:
    for err in e.errors(include_input=False, include_url=False, include_context=False):
        loc = err.get("loc") or ()
        if loc and loc[0] in _NOT_QUICK:
            return "not_quick"
    return "invalid"


class _Safe(BaseModel):
    """Validation never raises with the input in it: building or `model_validate` raise
    AnswerInvalid with a kind only."""

    def __init__(self, /, **data: Any) -> None:
        kind: AnswerInvalidKind | None = None
        try:
            super().__init__(**data)
        except ValidationError as e:
            kind = _kind_of(e)
        if kind is not None:
            raise AnswerInvalid(kind) from None

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any):
        kind: AnswerInvalidKind | None = None
        try:
            return super().model_validate(obj, **kwargs)
        except ValidationError as e:
            kind = _kind_of(e)
        raise AnswerInvalid(kind) from None


def _finite(v: Any) -> Any:
    """`v` with every non-finite float as None: serde_json writes a NaN or infinite f64 as
    `null` (JSON has neither; Postgres refuses them)."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return v


class ReferenceV3(_Safe):
    """A numbered reference, built by code from the fetched record (`references_from`)."""

    n: int = Field(ge=0, le=65535)  # u16: 1-based, in the order the researcher selected
    pmid: str
    title: str
    journal: str | None
    year: int | None
    doi: str | None
    retracted: bool
    found_by_skeptic: bool = False  # Full only; serde(default), always written


class IntegrityV3(_Safe):
    """What the code checks removed (counts only)."""

    dropped_unseen: int = 0  # selected studies that no tool returned in this run
    dropped_unfetched: int = 0  # selected studies whose record could not be fetched
    removed_citations: int = 0  # [PMID] citations of a study outside the selection
    dropped_numbers: int = 0  # number entries whose source study is outside the selection
    dropped_studies: int = 0  # trial rows whose study is outside the selection
    numbers_checked: int = 0
    numbers_untraced: int = 0
    numbers_unverified: int = 0
    outcomes_unverified: int = 0
    labels_defaulted: int = 0  # certainty or evidence labels not recognised
    numbers_completed: int = 0

    @model_serializer(mode="wrap")
    def _serde(self, handler: SerializerFunctionWrapHandler) -> dict:
        # a.rs:543 `skip_serializing_if = "is_zero"`
        return _skip(handler(self), numbers_completed=0)


class CompletionRule(StrEnum):
    """How a completed number follows from its source's words (F4)."""

    AS_WRITTEN = "as_written"
    SIGN_IN_WORDS = "sign_in_words"
    SUM = "sum"
    DIFFERENCE = "difference"
    PERCENT_FROM_COUNTS = "percent_from_counts"


class CompletedNumber(_Safe):
    """A "not traced" number completed against a cited abstract (F4)."""

    span: Span
    written: str
    pmid: str
    quote: str
    rule: CompletionRule


class AnswerV3(_Safe):
    """The stored answer (`answers.body`), Quick fields only: Quick has no skeptic, no
    evidence rows, no coverage and no completeness review (`t.rs:203-226` `base`)."""

    version: Literal[3] = ANSWER_VERSION
    question: str
    mode: Literal["quick"] = "quick"
    created_at: AwareDatetime
    answer: WrittenAnswer | None  # None when nothing could be written; empty_reason says why
    empty_reason: str | None
    references: list[ReferenceV3]
    searches: list[SearchEntry]
    integrity: IntegrityV3
    stopped_early: str | None  # "time limit" / "a failed model call"
    cost_paise: int = 0
    elapsed_s: int = Field(ge=0)
    skeptic: None = None
    evidence: list[Any] = Field(default=[], max_length=0)
    notes: list[str] = []
    untraced: list[Span] = []
    notable_code: list[NotableFinding] = []
    coverage: None = None
    completeness: None = None
    numbers_completed: list[CompletedNumber] = []

    @field_serializer("created_at")
    def _chrono(self, t, _info: SerializationInfo) -> str:
        """chrono's `DateTime<Utc>` serialisation: RFC 3339, `Z`, SecondsFormat::AutoSi (no
        fraction, else 3, 6 or 9 digits; Python holds microseconds, so at most 6)."""
        t = t.astimezone(UTC)
        us = t.microsecond
        frac = "" if us == 0 else f".{us // 1000:03d}" if us % 1000 == 0 else f".{us:06d}"
        return t.strftime("%Y-%m-%dT%H:%M:%S") + frac + "Z"

    @model_serializer(mode="wrap")
    def _serde(self, handler: SerializerFunctionWrapHandler) -> dict:
        # a.rs:932-943 `skip_serializing_if`: Option::is_none, Option::is_none, Vec::is_empty
        return _skip(handler(self), coverage=None, completeness=None, numbers_completed=[])

    def to_body(self) -> dict:
        """The answer as Incisive stores it (`serde_json::to_value(&answer)`): a non-finite
        number as `null`. A value no checked answer holds raises AnswerInvalid
        ("unserialisable"), never pydantic's error (which quotes the value)."""
        failed = False
        try:
            body = self.model_dump(mode="json", warnings=False)
        except (PydanticSerializationError, ValueError, TypeError):
            failed = True
        if failed:
            raise AnswerInvalid("unserialisable") from None
        return _finite(body)


def read_body(body: Any) -> AnswerV3:
    """A stored body (`answers.body`) read into an AnswerV3. Anything that is not a Quick
    AnswerV3 raises AnswerInvalid: `not_quick` (a Full part, another version or mode) or
    `invalid` (malformed); the body's content never reaches the exception."""
    return AnswerV3.model_validate(body)


def references_from(studies: Sequence[StudyInput]) -> list[ReferenceV3]:
    """The references, from the fetched records in selection order (`t.rs:448-462`): `n` from
    1, `pmid`, `title`, `journal`, `year`, `doi`, `retracted` from the record;
    `found_by_skeptic` false. Never from the model's text."""
    return [
        ReferenceV3(n=min(i + 1, 65535), pmid=s.pmid, title=s.title, journal=s.journal,
                    year=s.year, doi=s.doi, retracted=s.retracted, found_by_skeptic=False)
        for i, s in enumerate(studies)
    ]


# ---------- the citation check ----------


def check_citations(a: WrittenAnswer, selected: Sequence[str], integrity: IntegrityV3) -> None:
    """The citation check, in place (`w.rs:2641-2772`): `[PMID]` markers in every text field
    and the `pmids` / `pmid` fields must name a selected study. Unknown markers are removed
    from the text, unknown ids from `pmids`; number entries and trial rows whose `pmid` is
    unknown are dropped. Sets (does not add to) `integrity.removed_citations`,
    `dropped_numbers` and `dropped_studies`. Owner's Q8: removal, as Incisive."""

    def known(p: str) -> bool:
        return any(s == p for s in selected)

    removed = 0

    def clean(t: str) -> str:
        nonlocal removed
        c, n = clean_citations(t, known)
        removed += n
        return c

    for o in a.outcomes:
        for n in o.numbers:
            n.label = clean(n.label)
            # `[&mut n.unit, &mut n.arm, &mut n.p].into_iter().flatten()`: Some values only
            if n.unit is not None:
                n.unit = clean(n.unit)
            if n.arm is not None:
                n.arm = clean(n.arm)
            if n.p is not None:
                n.p = clean(n.p)
    a.bottom_line.text = clean(a.bottom_line.text)
    a.bottom_line.certainty_reason = clean(a.bottom_line.certainty_reason)
    for o in a.outcomes:
        o.outcome = clean(o.outcome)
        o.rct_evidence = clean(o.rct_evidence)
        o.effect_text = clean(o.effect_text)
        o.why = clean(o.why)
    for s in a.studies:
        s.label = clean(s.label)
        s.design = clean(s.design)
        s.setting = clean(s.setting)
        s.comparison = clean(s.comparison)
        s.key_results = clean(s.key_results)
        s.appraisal = clean(s.appraisal)
    for c in a.conflicts:
        c.text = clean(c.text)
    a.applicability.case_mix = clean(a.applicability.case_mix)
    a.applicability.india_cost_context = clean(a.applicability.india_cost_context)
    a.applicability.training = clean(a.applicability.training)
    a.would_change = [clean(w) for w in a.would_change]
    for g in a.guidelines:
        g.body = clean(g.body)
        g.statement = clean(g.statement)
    if a.at_a_glance is not None:
        g = a.at_a_glance
        g.verdict = clean(g.verdict)
        g.main_caveat = clean(g.main_caveat)
        for k in g.key_numbers:
            k.label = clean(k.label)
            k.value_text = clean(k.value_text)
    if a.clinical_practice is not None:
        c = a.clinical_practice
        c.default_choice = clean(c.default_choice)
        c.who_may_benefit = clean(c.who_may_benefit)
        c.counselling = clean(c.counselling)
        c.cost_per_case = clean(c.cost_per_case)
        c.text = clean(c.text)
    for f in a.notable_findings:
        f.text = clean(f.text)

    def filter_ids(ids: list[str]) -> list[str]:
        nonlocal removed
        kept: list[str] = []
        for i in ids:
            i = rust_trim(i)
            if not known(i):
                removed += 1  # an empty id counts too (Rust: `!known("")`)
            elif i not in kept:
                kept.append(i)
        return kept

    for o in a.outcomes:
        o.pmids = filter_ids(o.pmids)
    for c in a.conflicts:
        c.pmids = filter_ids(c.pmids)
    for f in a.notable_findings:
        f.pmids = filter_ids(f.pmids)
    # A key number of an unknown study keeps its text; only the id goes.
    if a.at_a_glance is not None:
        for k in a.at_a_glance.key_numbers:
            p = rust_trim(k.pmid)
            if known(p):
                k.pmid = p
            else:
                if p:
                    removed += 1
                k.pmid = ""
    # A guideline citing an unknown study keeps its text; only the id goes, and the page flags
    # it. An id that is empty after trimming becomes None, neither counted nor flagged.
    for g in a.guidelines:
        if g.pmid is not None:
            p = rust_trim(g.pmid)
            g.pmid = None
            if known(p):
                g.pmid = p
            elif p:
                removed += 1
                g.pmid_removed = True
    dropped_numbers = 0
    for o in a.outcomes:
        before = len(o.numbers)
        o.numbers = [n for n in o.numbers if known(rust_trim(n.pmid))]
        dropped_numbers += before - len(o.numbers)
        for n in o.numbers:
            n.pmid = rust_trim(n.pmid)
    before = len(a.studies)
    a.studies = [s for s in a.studies if known(rust_trim(s.pmid))]
    for s in a.studies:
        s.pmid = rust_trim(s.pmid)
    integrity.removed_citations = removed
    integrity.dropped_numbers = dropped_numbers
    integrity.dropped_studies = before - len(a.studies)


def plural(n: int, one: str, many: str) -> str:
    """"1 number" / "2 numbers" (`t.rs:198-200`)."""
    return f"{n} {one if n == 1 else many}"


def citation_check_line(integrity: IntegrityV3) -> str:
    """The citation check's step line (kind `check`), word for word `t.rs:649-669`."""
    if (integrity.removed_citations == 0 and integrity.dropped_numbers == 0
            and integrity.dropped_studies == 0):
        return "Citation check: every citation is one of the selected studies"
    return "Citation check: removed {}, dropped {} and {}".format(
        plural(integrity.removed_citations, "unknown citation", "unknown citations"),
        plural(integrity.dropped_numbers, "number", "numbers"),
        plural(integrity.dropped_studies, "trial row", "trial rows"),
    )
