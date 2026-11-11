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
| `CITE`, `DIGITS`, `MIN_PMID_DIGITS`, `split_citations` | `a.rs:979-1031` |

Task 4 builds only what reading the writer's parts needs. Task 5 adds AnswerV3, the
references, the citation check and these models' JSON form (serde's: `"Very low"`,
`ClinicalPractice.text` and `NotableFinding.spans` left out when empty, `pmid_removed` when
false); until then `model_dump` is pydantic's default.

Rust's regex `\\d` is Unicode `Nd` and `\\s` Unicode White_Space; Python's `re` differs (it lacks
Unicode 16's digits, and its `\\s` also matches U+001C..U+001F), so both classes are spelled
out. An id's length is counted in UTF-8 bytes, as Rust's `str::len`.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel

from pubmedgemini.writer._rust_unicode import DIGIT_CLASS, SPACE_CLASS


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
    pmid_removed: bool = False


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
