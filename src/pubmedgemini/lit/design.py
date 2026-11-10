"""Design label of a paper: a port of Incisive `agent/screen.rs` `EvidenceLevel::of` (Ruling R-10c).

PubMed publication types decide first (compared ignoring case, after trimming); only when no
type decides do whole title phrases count.
"""

import re
from collections.abc import Sequence

DESIGN_LABELS = (
    "guideline",
    "systematic review",
    "meta-analysis",
    "RCT",
    "prospective cohort",
    "other",
)

# Publication types that are not research reports; they force "other".
NON_RESEARCH_TYPES = (
    "Comment",
    "Letter",
    "Editorial",
    "News",
    "Published Erratum",
    "Retraction Notice",
    "Retraction of Publication",
)
GUIDELINE_TYPES = (
    "Practice Guideline",
    "Guideline",
    "Consensus Development Conference",
    "Consensus Development Conference, NIH",
)

TITLE_GUIDELINE = re.compile(
    r"\b(?:clinical )?practice guidelines?\b|^\W*guidelines?\b|\bconsensus (?:statement|guidelines?)\b"
)
TITLE_SR = re.compile(r"\bsystematic review\b")
TITLE_MA = re.compile(r"\bmeta-?analys[ie]s\b")
TITLE_RCT = re.compile(r"\brandomi[sz]ed (?:controlled )?trials?\b")
TITLE_PROSPECTIVE = re.compile(r"\bprospective (?:cohort|study)\b")
TITLE_PROSPECTIVE_WORD = re.compile(r"\bprospective\b")


def _ascii_lower(s: str) -> str:
    return "".join(c.lower() if c.isascii() else c for c in s)


def title_says_rct(title: str) -> bool:
    """A "randomi[sz]ed (controlled) trial" phrase in a lower-case title that is not
    negated ("non-randomized", "quasi-randomized"); `\\b` already rules out "nonrandomized"."""
    for m in TITLE_RCT.finditer(title):
        before = title[: m.start()].rstrip(" -")
        if not before.endswith(("non", "quasi")):
            return True
    return False


def design_label(title: str, pub_types: Sequence[str]) -> str:
    """One of DESIGN_LABELS."""
    types = {_ascii_lower(t.strip()) for t in pub_types}

    def has_type(names: Sequence[str]) -> bool:
        return any(_ascii_lower(n) in types for n in names)

    t = title.lower()
    if has_type(NON_RESEARCH_TYPES):
        return "other"
    if has_type(GUIDELINE_TYPES):
        return "guideline"
    if has_type(("Systematic Review",)):
        return "systematic review"
    if has_type(("Meta-Analysis",)):
        return "meta-analysis"
    if has_type(("Randomized Controlled Trial",)):
        return "RCT"
    if has_type(("Observational Study",)):
        return "prospective cohort" if TITLE_PROSPECTIVE_WORD.search(t) else "other"
    if TITLE_GUIDELINE.search(t):
        return "guideline"
    if TITLE_SR.search(t):
        return "systematic review"
    if TITLE_MA.search(t):
        return "meta-analysis"
    if title_says_rct(t):
        return "RCT"
    if TITLE_PROSPECTIVE.search(t):
        return "prospective cohort"
    return "other"
