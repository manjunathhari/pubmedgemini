"""The answer page's derivation: pure functions from a stored answer to the plain values the
template draws. Nothing here changes the answer or reads model text as markup; the one log
line is a fixed kind and a count (a forest row whose derivation failed, review M5).

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/`):

| Here | Incisive |
|---|---|
| `is_ratio`, `is_diff`, `has_ci`, `chart_kind` | `answer_page.rs` (the same names) |
| `arm_lines`, `_sides`, `_words`, `_arm_score` | `agent/answer_v3.rs` `arm_lines`, `sides`, `words`, `arm_score` |
| `NumGroup`, `all_groups`, `study_of` | `answer_page.rs` `NumGroup`, `outcome_groups`, `all_groups`, `study_of` |
| `normalise_comparison`, `compare` | `answer_page.rs` `normalise_comparison`, `comparison_groups`; arm names `app/page.rs` `compare_view` (`PairBar.arm`) |
| `out_of_100_groups`, `out_of_100_count` | `answer_page.rs` `out_of_100` (two arms only here: plan X20), `out_of_100_count` |
| `normalise_p`, `p_label`, `fmt_value`, `fmt_axis`, `with_unit`, `ratio_estimate`, `diff_estimate`, `study_short`, `strip_citations`, `first_sentence` | `answer_page.rs` (the same names) |
| `forest` rows | `answer_page.rs` `forest_rows` (every ratio or difference with both CI bounds) |
| `cite_pieces` | `agent/answer_v3.rs` `split_citations`; `app/page.rs` `chips` |
| `NO_NUMBERS` | `app/page.rs` `no_numbers_view` |
| `PAIRS` | `answer_page.rs` `PAIRS` |

Defined here for the frozen mock (`docs/mocks/f3-answer-page-v5.html`; F3 plan Task 7): the
draw checks (D3a), the ratio axis from the data (D3b), the difference axis that never puts 0
at an edge (X16), the out-of-100 sentence (X21), the pills and the fold that keeps the two
cards of a pair level (X14). Rounding is Rust's `f64::round` (half away from zero).

F3b: ported from branch `f3-writer` (3eb82a1) with its rules unchanged; only the types differ
(main's stored `AnswerV3`, pydantic, instead of f3-writer's `AnswerBody` dataclasses; plan §4).
"""

import dataclasses
import enum
import logging
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel

from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.lit.text import drop_invisible
from pubmedgemini.writer.answer import (
    AnswerV3,
    Cite,
    Number,
    Outcome,
    Study,
    Text,
    WrittenAnswer,
    split_citations,
)

Kind = Literal["ratio", "diff", "pair", "text"]
# A run of drawn text: ("text", s), ("bold", s), ("cite", [reference numbers]) or
# ("unknown", how many PMIDs are not in the references).
Piece = tuple[str, Any]

NOT_IN_LIST = "not in the study list"
NO_NUMBERS = ("This answer has no structured numbers, so the arm comparison and CI charts are "
              "not shown. Everything else renders from the stored text.")
PAIRS = (("Why this conclusion", "Every outcome, its numbers and how sure we can be"),
         ("Counselling dashboard", "What it means for 100 patients like these, arm by arm"))
RATIO_HEADING = "Ratios (RR · OR · HR)"
DIFF_HEADING = "Differences"
PAST_SCALE = "interval runs past the scale"

# One line, a fixed kind and a count, when a row's derivation fails (never a number or text).
log = logging.getLogger("pubmedgemini.answer_page")

# Rust `char::is_whitespace` (Unicode White_Space), as a regex class.
_WS = "[\t\n\x0b\x0c\r \x85\xa0  -     　]"


# ---------- numbers as text ----------


def round_half_away(x: float) -> float:
    """Rust `f64::round`: half away from zero (Python's `round` is half to even)."""
    a = abs(x)
    f = math.floor(a)
    r = f + 1 if a - f >= 0.5 else f
    return math.copysign(r, x)


def _plain_float(x: float) -> str:
    """Rust's `{}` of a finite f64: the shortest form that reads back, never an exponent."""
    r = repr(x)
    if "e" in r or "E" in r:
        r = format(Decimal(r), "f")
    return r


def fmt_value(v: float) -> str:
    """A value as the charts print it: as stored (no rounding), a true minus, thousands
    separated for whole numbers of 1,000 or more."""
    if not math.isfinite(v):
        return "NaN" if math.isnan(v) else ("inf" if v > 0 else "-inf")
    a = abs(v)
    if a == math.floor(a) and a < 1e15:
        body = f"{a:,.0f}" if a >= 1000 else f"{a:.0f}"
    elif a == math.floor(a):
        body = f"{int(a):,}"
    else:
        body = _plain_float(a)
    return f"−{body}" if v < 0 else body


def fmt_axis(v: float) -> str:
    """An axis label (a computed scale, not a stored value): two decimals at most."""
    return fmt_value(round_half_away(v * 100.0) / 100.0)


def with_unit(v: float, unit: str | None) -> str:
    """A value with its unit: "44%", "RR 0.4", "80.3 min"."""
    u = rust_trim(unit) if unit is not None else ""
    if not u:
        return fmt_value(v)
    if u == "%":
        return f"{fmt_value(v)}%"
    if u in ("RR", "OR", "HR"):
        return f"{u} {fmt_value(v)}"
    return f"{fmt_value(v)} {u}"


def _bound(x: float | None) -> str:
    return fmt_value(x if x is not None else math.nan)


def ratio_estimate(n: Number) -> str:
    """A ratio with its CI: "RR 0.4 (0.08–2.03)"."""
    return f"{with_unit(n.value, n.unit)} ({_bound(n.ci_low)}–{_bound(n.ci_high)})"


def diff_estimate(n: Number) -> str:
    """A difference with its CI: "0.33 days (−0.03 to 0.7)"."""
    unit = rust_trim(n.unit) if n.unit is not None else ""
    v = with_unit(n.value, n.unit) if unit in ("", "%") else f"{fmt_value(n.value)} {unit}"
    return f"{v} ({_bound(n.ci_low)} to {_bound(n.ci_high)})"


def normalise_p(p: str) -> str:
    """`p` as written, without a leading `P`/`p` or `=` that stands alone."""
    t = p.lstrip("".join(_WS_CHARS))
    if t[:1] in ("P", "p"):
        rest = t[1:]
        c = rest[:1]
        if not c or c in _WS_CHARS or "0" <= c <= "9" or c in "<>=.≤≥":
            t = rest.lstrip("".join(_WS_CHARS))
    if t.startswith("="):
        t = t[1:].lstrip("".join(_WS_CHARS))
    return t.rstrip("".join(_WS_CHARS))


_WS_CHARS = frozenset("\t\n\x0b\x0c\r \x85\xa0       "
                      "         　")


def p_label(p: str | None) -> str | None:
    """"p <normalised>", or None when nothing is left."""
    if p is None:
        return None
    n = normalise_p(p)
    return f"p {n}" if n else None


def study_short(label: str) -> str:
    """A study's short name: its label before the first comma."""
    return rust_trim(label.split(",", 1)[0])


_BRACKETS = re.compile(r"\[[^\]]*\]")
_SPACE_PUNCT = re.compile(rf"{_WS}+([.,;])")
_SPACES = re.compile(rf"{_WS}+")
_SENTENCE = re.compile(rf".*?[.!?](?:{_WS}|$)")


def strip_citations(text: str) -> str:
    """`text` without bracketed citations, spaces before punctuation removed."""
    t = _BRACKETS.sub("", text)
    t = _SPACE_PUNCT.sub(r"\1", t)
    return rust_trim(_SPACES.sub(" ", t))


def first_sentence(text: str) -> str:
    """The first sentence of `text`, citations stripped."""
    t = strip_citations(text)
    m = _SENTENCE.match(t)
    return rust_trim(m.group(0) if m else t)


def fmt_pct(x: float) -> str:
    """A computed position as a style value: one decimal, a trailing `.0` dropped."""
    s = f"{x:.1f}"
    if s in ("-0.0", "0.0"):
        return "0"
    return s.removesuffix(".0")


def count_noun(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


# ---------- citations ----------


def refs_of(body: AnswerV3) -> dict[str, int]:
    """The reference number of each PMID (code's, in selection order)."""
    return {r.pmid: r.n for r in body.references}


def cite_pieces(text: str, refs: Mapping[str, int]) -> list[Piece]:
    """`text` as text runs and citations: known PMIDs as sorted, de-duplicated reference
    numbers; PMIDs not in the references counted, to be drawn as "[not in the study list]"
    (flag, never delete: plan D4)."""
    out: list[Piece] = []
    for seg in split_citations(text, lambda p: p in refs):
        # main's segments (F3b): Text(text) is ("text", s), Cite(ids) the ids, as on f3-writer
        if isinstance(seg, Text):
            out.append(("text", seg.text))
            continue
        assert isinstance(seg, Cite)
        v = list(seg.ids)
        known = sorted({refs[i] for i in v if i in refs})
        unknown = len({i for i in v if i not in refs})
        if known:
            out.append(("cite", known))
        if unknown:
            out.append(("unknown", unknown))
    return out


def paragraphs(text: str, refs: Mapping[str, int]) -> list[list[Piece]]:
    """Model text as paragraphs (split on blank lines; a single line break is a space)."""
    out = []
    for para in re.split(rf"\n{_WS}*\n", text):
        p = rust_trim(_SPACES.sub(" ", para))
        if p:
            out.append(cite_pieces(p, refs))
    return out


def pieces_text(pieces: Sequence[Piece]) -> str:
    """The pieces as plain text (as the page reads without its links)."""
    out = []
    for kind, v in pieces:
        if kind in ("text", "bold"):
            out.append(v)
        elif kind == "cite":
            out.append("[" + ", ".join(str(n) for n in v) + "]")
        else:
            out.append(f"[{NOT_IN_LIST}]")
    return "".join(out)


# ---------- chart kinds and groups ----------


# R1 (owner decision N5, 12 Nov 2026; strict shape after review A1): with a blank unit, a label
# that is plainly one ratio. On the label as `_display_key` gives it, case-folded:
#   1. it STARTS with at most one qualifier (`_R1_QUALIFIERS`) and a space, then one ratio
#      name (`_R1_NAMES`), and what follows the name is the end of the label or a separator:
#      ",", ":", "(", "—", "–" (each after at most one space), or " for ", " of ", " in ",
#      " at " (`_LABEL_RATIO`);
#   2. AND the whole label holds, as a token, none of `_R1_BANNED`: a change word (reduction,
#      increase, change, gain, excess, delta, Δ ...), a difference word (difference, diff,
#      minus, MD, SMD, WMD), a negation or significance word (no, not, non, without, none,
#      neither, nor, NS, n.s., nonsignificant, insignificant, NR, NE, NA, n/a; so "not
#      reported", "not estimable", "not reached", "non-significant" too), or a measure
#      abbreviation (HR, RR, IRR, aHR, aOR, ARR, RRR, NNT, NNH; OR only in capitals,
#      `_UPPER_OR`);
#   3. AND it names one measure only: "ratio" and "relative risk" occur once in all
#      (`_R1_SECOND`).
# The live writer stores hazard ratios so ("Hazard ratio for ...", unit null). Anything else is
# no ratio by its label: "Relative risk reduction ...", "Hazard ratio not reported; ...",
# "Odds ratio difference", "HR for ...", "Recurrence, hazard ratio". A label with a blank unit
# that names a ratio anywhere but fails is also no difference row (`r1_refused`), whatever its
# arm marker says: it is listed, "Not drawn: unit not RR, OR or HR.", as before R1.
_R1_QUALIFIERS = ("stratified", "adjusted", "unadjusted", "pooled", "crude", "multivariable",
                  "multivariate")
_R1_NAMES = {"hazard ratio": "HR", "odds ratio": "OR", "risk ratio": "RR",
             "relative risk": "RR", "rate ratio": "IRR"}
_R1_START = (r"(?:(?:" + "|".join(_R1_QUALIFIERS) + r") )?("
             + "|".join(re.escape(x) for x in _R1_NAMES) + ")")
_LABEL_RATIO = re.compile(_R1_START + r"(?=$| ?[,:(—–]| (?:for|of|in|at) )")
_LABEL_RATIO_ANYWHERE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(x) for x in _R1_NAMES) + r")(?!\w)")
_R1_BANNED = frozenset({
    # change words (the change part of `_CHANGE_WORDS`, with plurals)
    "reduction", "reductions", "reduced", "increase", "increases", "increased", "decrease",
    "decreases", "decreased", "change", "changes", "changed", "improvement", "improvements",
    "gain", "gains", "excess", "delta", "δ", "∆",
    # difference words
    "difference", "differences", "diff", "minus", "md", "smd", "wmd",
    # negation and significance words (`_NEGATION`, and NR, NE, NA, n/a)
    "no", "not", "non", "without", "none", "neither", "nor", "ns", "n.s", "nonsignificant",
    "insignificant", "nr", "ne", "na", "n/a",
    # a measure abbreviation (a second measure, or one the label does not spell out)
    "hr", "rr", "irr", "ahr", "aor", "arr", "rrr", "nnt", "nnh"})
_R1_TOKEN = re.compile(r"[\w./]+|[δ∆]")
_R1_SECOND = re.compile(r"ratio|relative risk")


def _r1_tokens(key: str) -> set[str]:
    return {t.rstrip(".") for t in _R1_TOKEN.findall(key)}


def label_ratio(label: str) -> str | None:
    """R1: the ratio a label alone names, as a unit ("HR", "OR", "RR", or "IRR" for a rate
    ratio), when the label is plainly one ratio (the three rules above); else None."""
    shown = _display_key(label)
    key = shown.casefold()
    m = _LABEL_RATIO.match(key)
    if m is None:
        return None
    if _r1_tokens(key) & _R1_BANNED or _UPPER_OR.search(shown):
        return None
    if len(_R1_SECOND.findall(key)) != 1:
        return None
    return _R1_NAMES[m.group(1)]


def r1_refused(n: Number) -> bool:
    """A blank unit and a label that names a ratio anywhere (a ratio name as whole words) but
    is not plainly one (`label_ratio`): never a forest row of any kind (review A1)."""
    u = rust_trim(n.unit) if n.unit is not None else ""
    if u:
        return False
    key = _display_key(n.label).casefold()
    return _LABEL_RATIO_ANYWHERE.search(key) is not None and label_ratio(n.label) is None


def ratio_unit(n: Number) -> str | None:
    """The ratio a number is, as a unit: "RR", "OR" or "HR" when its unit is exactly one of
    them (the unit always decides when it is not blank); else, only when the unit is blank,
    the ratio its label plainly names (R1, `label_ratio`); else None. Every draw check still
    applies to such a ratio."""
    u = rust_trim(n.unit) if n.unit is not None else ""
    if u:
        return u if u in ("RR", "OR", "HR") else None
    return label_ratio(n.label)


def is_ratio(n: Number) -> bool:
    """A ratio: unit RR, OR or HR, or (R1) a blank unit and a ratio named at the label's
    start (`ratio_unit`)."""
    return ratio_unit(n) is not None


_DIFF = re.compile(r"difference|\bMD\b|minus", re.IGNORECASE)


def is_diff(n: Number) -> bool:
    """A difference: its label or arm reads as one (MD, "difference", "minus")."""
    return bool(_DIFF.search(f"{n.label} {n.arm or ''}"))


def arm_name(n: Number) -> str:
    """The arm name the writer gave for this number, without bidi controls or zero-width
    characters (L3), trimmed ("" when none)."""
    return rust_trim(drop_invisible(n.arm)) if n.arm is not None else ""


# Arm texts that are a difference marker, not an arm name: the whole text, trimmed, case-folded
# and with inner spaces squashed, must be one of these (re-check H-A). Incisive's own examples
# put "MD", "difference" and "risk difference" in `arm` (`answer_page.rs` tests); the rest
# are the coordinator's list. A full match, never a search: "MD Anderson protocol" and
# "Md. Ali technique" are arm names.
DIFF_MARKERS = frozenset({"md", "smd", "wmd", "difference", "mean difference",
                          "risk difference", "absolute difference"})
# The whole arm text, punctuation ignored, is one of these: a measure, not an arm ("MD.",
# "M.D.", "Difference:", "ARR", "RD", "ARD", "NNT", "NNH"; third check M1).
_MEASURE_ARMS = DIFF_MARKERS | {"arr", "rd", "ard", "nnt", "nnh"}
# Words that make an arm text read as a comparison (third check M1), matched as whole words
# in the key (case-folded): "difference(s)", "minus", "versus", "vs", "vs.", "compared with",
# "compared to", "relative to".
_COMPARISON_WORDS = re.compile(
    r"(?<!\w)(?:differences?|minus|versus|vs\.?|compared with|compared to|relative to)(?!\w)")
_PUNCT = re.compile(r"[^\w\s]|_")
# More of what makes an arm text a comparison or a summary, not an arm (fourth check F1), on
# the case-folded key: operator punctuation between words (a spaced hyphen, en dash or minus
# sign, "/", ">", "<", a lone "v" or "v."), the delta or increment sign anywhere, and these words or
# abbreviations as whole words: diff, delta, reduction, increase, change, ARR, RRR, ARD, NNT,
# NNH, RR, HR, relative risk, odds, hazard, ratio, pooled, overall, total, both, all patients,
# combined. OR counts only as a whole upper-case token (`_UPPER_OR`): "TEP or open" is a name.
_COMPARISON_MORE = re.compile(
    r"\s[-\u2013\u2212]\s|[/<>\u03b4\u0394\u2206]|(?<!\w)v\.?(?!\w)"
    r"|(?<!\w)(?:diffs?|delta|reductions?|increases?|changes?|arr|rrr|ard|nnt|nnh|rr|hr"
    r"|relative risk|odds|hazards?|ratios?|pooled|overall|total|both|all patients|combined)(?!\w)")
_UPPER_OR = re.compile(r"(?<!\w)OR(?!\w)")

ArmKind = Literal["none", "marker", "comparison", "name"]


def _key(s: str) -> str:
    """A text as a classification key: bidi controls and zero-width characters removed (L3:
    "TEP\u200b" is "TEP"), Unicode NFKC ("ＭＤ" is "MD"), trimmed, inner whitespace squashed,
    case-folded. Never shown."""
    return _display_key(s).casefold()


def _display_key(s: str) -> str:
    """A text for matching with its case kept: bidi controls and zero-width characters
    removed, NFKC, trimmed, inner whitespace squashed."""
    return rust_trim(_SPACES.sub(" ", unicodedata.normalize("NFKC", drop_invisible(s))))



def arm_kind(n: Number) -> ArmKind:
    """The one rule for what a number's arm text is (third check M1; owner rule D3a, decisions
    d23 and d24). On the key (`_key`):

    - "none": empty (no arm; d23: never shown as an arm);
    - "marker": the whole key is one of `DIFF_MARKERS` (Incisive puts the measure in `arm`:
      the number has no arm and the marker names its measure);
    - "comparison": the key with punctuation removed is one of `_MEASURE_ARMS`, or the key
      contains, as a whole word, one of `_COMPARISON_WORDS`, or it matches `_COMPARISON_MORE`
      (operator punctuation, the delta sign, ARR, NNT, RR, pooled, both and the like), or the
      text holds OR as a whole upper-case token (fourth check F1);
    - "name": anything else, a plain arm name ("TEP", "MD Anderson protocol").

    Only a "name" may be paired (Compare the arms, Out of 100); a "comparison" is never
    paired and never makes a difference row; it is listed, with
    "its arm text reads as a comparison, not an arm"."""
    k = _key(n.arm) if n.arm is not None else ""
    if not k:
        return "none"
    if k in DIFF_MARKERS:
        return "marker"
    if (rust_trim(_SPACES.sub(" ", _PUNCT.sub("", k))) in _MEASURE_ARMS
            or _COMPARISON_WORDS.search(k) or _COMPARISON_MORE.search(k)
            or _UPPER_OR.search(_display_key(n.arm or ""))):
        return "comparison"
    return "name"


def arm_is_marker(n: Number) -> bool:
    """The arm text is a difference marker as a whole (`DIFF_MARKERS`)."""
    return arm_kind(n) == "marker"


def names_an_arm(n: Number) -> bool:
    """The number carries arm text that is not blank and not, as a whole, a difference marker
    (`arm_kind` "comparison" or "name"): d24, such a number is never a difference row."""
    return arm_kind(n) in ("comparison", "name")


def plain_arm(n: Number) -> bool:
    """The arm text is a plain arm name (`arm_kind` "name"): the only kind that is paired."""
    return arm_kind(n) == "name"


# The named kinds of difference (third check M2): the whole unit (`fullmatch`), or the start
# of the label (`_START_DIFF`, an "absolute" before the kind allowed) or, as in Incisive's own
# "Return to daily activities, mean difference", the whole part of the label after its last
# comma (`_END_DIFF`); never anywhere else in a label (fourth check F2).
_KINDS = r"(mean|risk|absolute|median|rate|weighted mean|standardi[sz]ed mean)"
_NAMED_DIFF = re.compile(rf"(?<!\w){_KINDS} differences?(?!\w)", re.IGNORECASE)
_START_DIFF = re.compile(rf"(?:absolute )?{_KINDS} differences?(?!\w)", re.IGNORECASE)
_END_DIFF = re.compile(rf".*, ?(?:absolute )?{_KINDS} differences?$", re.IGNORECASE | re.DOTALL)
# A negation or significance word, as a whole token anywhere in the label: then the label
# names no difference, whatever else it holds (fourth check F2): no, not, non, without, none,
# neither, nor, NS, n.s., non-significant, nonsignificant, insignificant.
_NEGATION = re.compile(r"(?<!\w)(?:no|not|non|without|none|neither|nor|ns|n\.s|"
                       r"nonsignificant|insignificant)(?!\w)", re.IGNORECASE)
# A term that counts in a label only standing alone: the whole label, or the start of the
# label followed by a separator (",", ":", "(", "—", "–" after optional spaces, or " in ",
# " for ", " of "). The abbreviations count only in capitals.
_LEAD_TERM = re.compile(
    r"(?:(?P<abbr>MD|SMD|WMD)|(?P<word>(?i:difference)))"
    r"(?:$|(?P<sep>\s*[,:(—–]|\s(?:in|for|of)\s)(?P<rest>.*))", re.DOTALL)
_FIRST_WORD = re.compile(r"[^\W\d_]+")
_UNIT_TERMS = {"md": "MD", "smd": "SMD", "wmd": "WMD", "difference": "difference"}



def _named(m: re.Match[str]) -> str:
    return f"{m.group(1).lower()} difference"


def difference_measure(n: Number) -> str | None:
    """The difference measure the number names, or None (third check M2; owner rule D3a,
    decision d24). One rule, on the label and the unit as `_display_key` gives them and the
    arm as `arm_kind` reads it; the first that applies:

    0. never when the label holds a negation or significance word as a token (`_NEGATION`:
       "No mean difference was found", "Risk difference, n.s."; fourth check F2);
    1. the unit, as a whole and ignoring case, is a measure term: MD, SMD, WMD, "difference"
       or a named kind of difference ("mean difference", "risk difference", "absolute
       difference", "median difference", "rate difference", "weighted mean difference",
       "standardised mean difference");
    2. the label starts with a named kind of difference ("Mean difference in pain score",
       "Absolute risk difference in recurrence"), or its part after the last comma is one
       ("Return to daily activities, mean difference"); a named kind elsewhere ("Event rate
       where the risk difference was small") names no measure (fourth check F2);
    3. the label is "difference" (any case) or MD, SMD or WMD (in capitals), or starts with
       one followed by a separator: optional spaces then ",", ":", "(", "—" or "–", or a
       space, "in", "for" or "of" and a space ("Difference in length of stay", "MD, days").
       After MD, SMD or WMD, the first word after the separator must not be a capitalised
       word (a capital then a small letter, as "Anderson"): "MD (Anderson score)" names no
       measure;
    4. the arm text is, as a whole, a difference marker (`arm_kind` "marker": Incisive puts
       "MD" or "difference" in `arm`).

    Merely the word "difference" elsewhere ("Recurrence (no significant difference)"), MD
    inside a name ("MD Anderson Dysphagia Inventory score"), or "minus" names no measure.
    Returned: the term, as "MD", "SMD", "WMD", "difference" or "<kind> difference"."""
    label = _display_key(n.label)
    if _NEGATION.search(label):
        return None
    unit = _display_key(n.unit).casefold() if n.unit is not None else ""
    if unit in _UNIT_TERMS:
        return _UNIT_TERMS[unit]
    m = _NAMED_DIFF.fullmatch(unit)
    if m:
        return _named(m)
    term = label_measure(n.label)
    if term is not None:
        return term
    if arm_kind(n) == "marker":
        k = _key(n.arm or "")
        return {"md": "MD", "smd": "SMD", "wmd": "WMD"}.get(k, k)
    return None


def label_measure(label: str) -> str | None:
    """The difference measure the label alone names (`difference_measure` rules 0, 2 and 3),
    or None. Also used to keep a pair (fourth check F3) and a ratio (F4) whose label names a
    difference from being drawn as what its unit says."""
    label = _display_key(label)
    if _NEGATION.search(label):
        return None
    m = _START_DIFF.match(label) or _END_DIFF.fullmatch(label)
    if m:
        return _named(m)
    m = _LEAD_TERM.match(label)
    if m:
        rest = m.group("rest") or ""
        word = _FIRST_WORD.search(rest)
        proper = bool(word) and len(word.group(0)) > 1 and word.group(0)[0].isupper() and \
            word.group(0)[1].islower()
        if m.group("word"):
            return "difference"
        if not proper:
            return m.group("abbr")
    return None


_MEAN_TERMS = frozenset({"MD", "WMD", "mean difference", "weighted mean difference"})


def is_difference(n: Number) -> bool:
    """A difference row for the page (allow-list, D3a): no arm text other than a whole marker
    (`names_an_arm` false; review M2, owner decision d24) AND a named measure
    (`difference_measure`; third check M2: merely the word "difference" or "minus" is not
    enough). Both are accepted differences from Incisive, which draws any number whose label
    or arm matches `is_diff`."""
    return not names_an_arm(n) and difference_measure(n) is not None


def has_ci(n: Number) -> bool:
    """Both CI bounds stored and finite."""
    return (n.ci_low is not None and n.ci_high is not None and math.isfinite(n.ci_low)
            and math.isfinite(n.ci_high))


def chart_kind(nums: Sequence[Number]) -> Kind:
    """How a group of numbers (one label, one study) is drawn, by their shape."""
    if not nums:
        return "text"
    if all(is_ratio(n) and has_ci(n) for n in nums):
        return "ratio"
    if len(nums) == 1 and has_ci(nums[0]) and is_difference(nums[0]):
        return "diff"
    # Arms that differ only by case, spacing or Unicode form are one arm (re-check L-D:
    # Incisive compares them trimmed only); the page still shows the writer's own spelling.
    arms = {_key(n.arm) if n.arm is not None else None for n in nums}
    if len(nums) >= 2 and len(arms) == len(nums) and not any(
            is_ratio(n) or is_difference(n) for n in nums):
        return "pair"
    return "text"


def forest_kind(n: Number) -> Literal["ratio", "diff"] | None:
    """The forest's kind of a number (Incisive `forest_rows`: a ratio or a difference with
    both CI bounds), or None when it is not a forest row."""
    if not has_ci(n):
        return None
    if is_ratio(n):
        return "ratio"
    if r1_refused(n):  # starts like a ratio, is not plainly one: listed (review A1)
        return None
    return "diff" if is_difference(n) else None


def compared(w: WrittenAnswer, g: "NumGroup") -> bool:
    """A pair group "Compare the arms" shows: every value has its own plain arm name
    (`plain_arm`; review H1, owner decision d23: an arm is never inferred from position or
    from the heading; third check M1: an arm text that reads as a comparison is no name)."""
    return g.kind == "pair" and all(plain_arm(n) for n in g.nums(w))


def _words(s: str) -> list[str]:
    return [w.lower() for w in re.split(r"[^\w]|_", s) if len(w.encode("utf-8")) > 1]


def _sides(comparison: str) -> tuple[str, str] | None:
    low = comparison.lower()
    for sep in (" vs. ", " vs ", " versus ", " compared with "):
        i = low.find(sep)
        if i >= 0:
            return low[:i], low[i + len(sep):]
    return None


def _arm_score(arm: str, comparison: str) -> int:
    sides = _sides(comparison)
    if sides is None:
        return 0
    left, right = (_words(x) for x in sides)
    return sum(int(w in left) - int(w in right) for w in _words(arm))


# The separators that split a study's comparison into its two sides (Incisive `sides`), on
# the key; a comparison is two-sided only when exactly one of them splits it into two parts
# that both hold a word (fourth check F1).
_SIDE_SEP = re.compile(r" (?:vs\.?|versus|compared with) ")


def comparison_parts(comparison: str | None) -> tuple[str, str] | None:
    """The two sides of a two-sided comparison as keys (`_key`), or None: no comparison, not
    exactly one separator, or a side without a word (fourth check F1)."""
    if comparison is None:
        return None
    parts = _SIDE_SEP.split(_key(comparison))
    if len(parts) != 2 or not _words(parts[0]) or not _words(parts[1]):
        return None
    return parts[0], parts[1]


def comparison_sides(comparison: str | None) -> tuple[set[str], set[str]] | None:
    """The words only the left side, and only the right side, of a two-sided comparison
    holds ("TEP vs Lichtenstein": {"tep"}, {"lichtenstein"}), on the key and Incisive's
    `words`; None when there is no comparison or it does not split into exactly two sides."""
    parts = comparison_parts(comparison)
    if parts is None:
        return None
    left, right = (set(_words(x)) for x in parts)
    return left - right, right - left


# S1 (owner decision N6, 12 Nov 2026): an arm that is exactly "Group <letter>" (on the key).
_GROUP_ARM = re.compile(r"group ([^\W\d_])")


def _group_side(arm_key: str, parts: tuple[str, str]) -> int | None:
    """S1: an arm "Group X" matches the side of the comparison that names "(group X)" when the
    other side does not; else None."""
    m = _GROUP_ARM.fullmatch(arm_key)
    if m is None:
        return None
    mark = f"(group {m.group(1)})"
    has = [mark in p for p in parts]
    return has.index(True) if has.count(True) == 1 else None


# S2 (owner decision N6, 12 Nov 2026): an arm that is exactly one of the owner's role words,
# optionally followed by "group" or "arm" (on the key). "experimental" is the first side of the
# comparison; "standard of care", "standard" and "control" the second.
_ROLE_ARM = re.compile(r"(experimental|standard of care|standard|control)(?: (?:group|arm))?")


def _has_word(term: str, text: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def _role_side(arm_key: str, parts: tuple[str, str] | None) -> int | None:
    """S2: an arm "Experimental (group|arm)" matches the first side, and "Standard of care",
    "Standard" or "Control" (with "group" or "arm") the second, only when the comparison has
    that word, as a whole word, on that side and not on the other; else None."""
    if parts is None:
        return None
    m = _ROLE_ARM.fullmatch(arm_key)
    if m is None:
        return None
    term = m.group(1)
    side = 0 if term == "experimental" else 1
    if _has_word(term, parts[side]) and not _has_word(term, parts[1 - side]):
        return side
    return None


def arm_side(arm: str | None, comparison: str | None) -> int | None:
    """Which side of the comparison an arm text matches: 0 (left), 1 (right) or None (no
    two-sided comparison, or a tie or zero score). The score is `_arm_score`'s token overlap
    (each word on the left only +1, on the right only -1) on the key (fourth check F1). When
    the score is zero, the owner's recovery rules may still place the arm: S1 (`_group_side`),
    then S2 (`_role_side`)."""
    sides = comparison_sides(comparison)
    if sides is None or arm is None:
        return None
    left, right = sides
    score = sum(int(w in left) - int(w in right) for w in _words(_key(arm)))
    if score:
        return 0 if score > 0 else 1
    # Recovery rules the owner chose (N6), only where the score finds no side, so an arm the
    # score placed is never moved: S1 group letters, S2 experimental / standard / control.
    parts = comparison_parts(comparison)
    assert parts is not None  # comparison_sides was not None
    k = _key(arm)
    group = _group_side(k, parts)
    return group if group is not None else _role_side(k, parts)


def names_one_side(text: str, comparison: str | None) -> bool:
    """`text` holds words of exactly one side of a two-sided comparison (the same words as
    `arm_side`): "MD in TEP group" under "TEP vs Lichtenstein" (fourth check F5)."""
    sides = comparison_sides(comparison)
    if sides is None:
        return False
    words = set(_words(_key(text)))
    return bool(words & sides[0]) != bool(words & sides[1])


@dataclass(frozen=True)
class NumGroup:
    outcome: int  # index of the writer's outcome
    label: str
    pmid: str
    numbers: list[int]  # indexes into the outcome's numbers, intervention arm first
    kind: Kind

    def nums(self, w: WrittenAnswer) -> list[Number]:
        o = w.outcomes[self.outcome]
        return [o.numbers[j] for j in self.numbers]

    def p_text(self, w: WrittenAnswer) -> str | None:
        """The distinct p values, normalised, joined " · "."""
        ps: list[str] = []
        for n in self.nums(w):
            p = p_label(n.p)
            if p is not None and p not in ps:
                ps.append(p)
        return " · ".join(ps) if ps else None


def study_of(w: WrittenAnswer, pmid: str) -> Study | None:
    """The trials-table row of `pmid`, if the writer gave one."""
    for s in w.studies:
        if rust_trim(s.pmid) == rust_trim(pmid):
            return s
    return None


def comparison_of(w: WrittenAnswer, pmid: str) -> str | None:
    """The comparison of `pmid`'s trials-table row, if the writer gave one."""
    s = study_of(w, pmid)
    return s.comparison if s is not None else None


def arm_lines(o: Outcome, comparison_of,
              known: Callable[[str], bool]) -> list[tuple[str, str, list[int]]]:
    """The outcome's numbers grouped by label and study in first-appearance order, each
    group with the intervention arm (more words shared with the left side of the study's
    comparison) first; a stable sort, so equal scores keep the writer's order. A number
    whose study is not `known` is a line of its own: numbers with no study may come from
    different studies and are never grouped (re-check L-F)."""
    lines: list[tuple[str, str, list[int]]] = []
    for i, n in enumerate(o.numbers):
        label, pmid = rust_trim(n.label), rust_trim(n.pmid)
        if not (pmid and known(pmid)):
            lines.append((label, pmid, [i]))
            continue
        for line in lines:
            if line[0] == label and line[1] == pmid:  # both known: the same PMID
                line[2].append(i)
                break
        else:
            lines.append((label, pmid, [i]))
    for _label, pmid, idx in lines:
        c = comparison_of(pmid)
        if c is None:
            continue
        idx.sort(key=lambda i: -(_arm_score(o.numbers[i].arm, c)
                                 if o.numbers[i].arm is not None else 0))
    return lines


def outcome_groups(w: WrittenAnswer, i: int, refs: Mapping[str, int]) -> list[NumGroup]:
    o = w.outcomes[i]

    def comparison_of(p: str) -> str | None:
        s = study_of(w, p)
        return s.comparison if s is not None else None

    return [NumGroup(i, label, pmid, idx, chart_kind([o.numbers[j] for j in idx]))
            for label, pmid, idx in arm_lines(o, comparison_of, lambda p: p in refs)]


def all_groups(w: WrittenAnswer, refs: Mapping[str, int]) -> list[NumGroup]:
    """Every outcome's groups, in page order; a number whose PMID is blank or not in
    `refs` is a group of its own (re-check L-F)."""
    return [g for i in range(len(w.outcomes)) for g in outcome_groups(w, i, refs)]


# ---------- the draw checks (plan D3 option a) ----------


def draw_problem(n: Number, kind: Literal["ratio", "diff", "percent"],
                 refs: Mapping[str, int]) -> str | None:
    """Why a number may not be drawn (the first check that fails), or None."""
    if rust_trim(n.pmid) not in refs:
        return "its study is not in the references"
    if kind == "percent":  # its interval is checked only where one is given (review L8)
        values = [v for v in (n.value, n.ci_low, n.ci_high) if v is not None]
    else:
        values = [n.value, n.ci_low, n.ci_high]
    if any(v is None or not math.isfinite(v) for v in values):
        return "a value is not a finite number"
    if kind == "percent":
        if not 0 <= n.value <= 100:
            return "a percentage must be from 0 to 100"
        if n.ci_low is not None and n.ci_high is not None and n.ci_low > n.ci_high:
            return "the interval is reversed"
        if ((n.ci_low is not None and n.value < n.ci_low)
                or (n.ci_high is not None and n.value > n.ci_high)):
            return "the estimate is outside its interval"
        return None
    lo, hi = n.ci_low, n.ci_high
    assert lo is not None and hi is not None
    # A ratio whose label names a difference is not clearly either (fourth check F4).
    if kind == "ratio" and label_measure(n.label) is not None:
        return RATIO_ON_DIFF
    if kind == "ratio" and not (n.value > 0 and lo > 0 and hi > 0):
        return "a ratio must be above 0"
    if lo == hi:  # a point drawn as an interval (fourth check F4)
        return NO_WIDTH
    if lo > hi:
        return "the interval is reversed"
    if not lo <= n.value <= hi:
        return "the estimate is outside its interval"
    return None


RATIO_ON_DIFF = "the label names a difference but the unit is a ratio"
NO_WIDTH = "the interval has no width"
# A difference row whose label names one side of its study's comparison: one arm's value, not
# the difference between them (fourth check F5).
ONE_ARM_LABEL = "the label names one arm"


# ---------- numbers drawn nowhere (review M3) ----------

# A ratio the writer did not mark with the unit exactly RR, OR or HR ("aHR", "rr", "RR" in
# the label). Only the reason given changes; what is drawn does not.
_RATIO_UNIT = re.compile(r"(?i)a?(?:rr|or|hr)")
_RATIO_ABBR = re.compile(r"\ba?(?:RR|OR|HR)\b")
_RATIO_WORDS = re.compile(r"(?i)\b(?:risk|odds|hazard|rate) ratio\b|\brelative risk\b")


def _looks_ratio(n: Number) -> bool:
    u = rust_trim(n.unit) if n.unit is not None else ""
    return bool(_RATIO_UNIT.fullmatch(u) or _RATIO_ABBR.search(n.label)
                or _RATIO_WORDS.search(f"{n.label} {u}"))


def listed_problem(n: Number, group: Sequence[Number], pair: bool = False,
                   has_study: bool = True) -> str:
    """Why a number that is neither a forest row nor a "Compare the arms" value is not drawn.
    `pair`: its group is a pair left out of "Compare the arms" for an arm with no name.
    `has_study`: its PMID is in the references (else it is never grouped: re-check L-F)."""
    if not has_study:
        return NO_STUDY
    kind = arm_kind(n)
    if pair:
        if kind == "none":
            return "arm not named"
        if kind != "name":
            return COMPARISON_ARM
        if any(arm_kind(x) == "none" for x in group if x is not n):
            return "the other arm is not named"
        return OTHER_COMPARISON_ARM
    if kind == "comparison" and not is_ratio(n):
        return COMPARISON_ARM
    if names_an_arm(n) and labelled_difference(n):
        return ARM_ON_DIFF
    if is_diff(n) and names_an_arm(n):
        return ONE_ARM_RATE
    if not is_ratio(n) and _looks_ratio(n):
        return "unit not RR, OR or HR"
    if not names_an_arm(n) and (is_diff(n) or labelled_difference(n)) and not is_difference(n):
        return NOT_CLEAR_DIFF
    if (is_ratio(n) or is_difference(n)) and not has_ci(n):
        return "no interval"
    if len(group) == 1:
        return "not marked as a ratio or a difference" if has_ci(n) else "single arm"
    if any(is_ratio(x) or is_difference(x) or _looks_ratio(x) for x in group):
        return "the group mixes kinds"
    return "its arms are not distinct"


ONE_ARM_RATE = "one arm's rate, not a difference"
COMPARISON_ARM = "its arm text reads as a comparison, not an arm"
# A number with no arm whose label or arm reads as a difference by Incisive's rule (`is_diff`)
# but names no measure (`difference_measure`; third check M2).
NOT_CLEAR_DIFF = "not clearly a difference between arms"
OTHER_COMPARISON_ARM = "the other arm's text reads as a comparison, not an arm"
NO_STUDY = "its study is not in the references"
# A number whose label or unit names a difference measure and which also carries an arm name:
# not drawn (d24), and the reason says what is wrong (re-check M-C, coordinator option a).
ARM_ON_DIFF = "an arm is named on a difference"

# For the reason only, never for drawing (that is `difference_measure`): a label or unit that
# reads as naming a difference (MD, SMD, WMD, a named kind of difference, "minus", a label
# starting "difference"), so an arm named on it gets "an arm is named on a difference".
_DIFF_MEASURE = re.compile(
    r"\b[SW]?MD\b|\b(?:mean|risk|absolute|median|rate|weighted mean|standardi[sz]ed mean)"
    rf"{_WS}+difference\b|\bminus\b|^{_WS}*difference\b", re.IGNORECASE)


def labelled_difference(n: Number) -> bool:
    """The label or the unit reads as naming a difference (`_DIFF_MEASURE`): which reason an
    armed number gets. Drawing uses `is_difference` only."""
    return any(_DIFF_MEASURE.search(t) for t in (n.label, n.unit or ""))


def _opt_bound(x: float | None) -> str:
    return "not given" if x is None else fmt_value(x)


@dataclass(frozen=True)
class ListedNumber:
    label: str
    value_text: str
    sub_pieces: list[Piece]
    problem: str
    at: tuple[int, int]  # (outcome, number)


def listed_numbers(w: WrittenAnswer, refs: Mapping[str, int]) -> dict[int, list[ListedNumber]]:
    """By outcome, the numbers no forest row and no "Compare the arms" row shows, as text
    with the reason (flag, never delete: every number the writer gave is on the page). A
    number in a compare row is shown there, so it is never listed here as "Not drawn" (third
    check L2), whatever its label says."""
    out: dict[int, list[ListedNumber]] = {}
    for g in all_groups(w, refs):
        in_compare = compared(w, g)
        o = w.outcomes[g.outcome]
        group = g.nums(w)
        for j in g.numbers:
            n = o.numbers[j]
            if forest_kind(n) is not None:
                continue
            if in_compare:
                continue  # shown in "Compare the arms" (and Out of 100): never "Not drawn" (L2)
            value = with_unit(n.value, n.unit)
            if n.ci_low is not None or n.ci_high is not None:
                value += f" ({_opt_bound(n.ci_low)} to {_opt_bound(n.ci_high)})"
            sub = study_pieces(w, n.pmid, refs)
            arm = rust_trim(n.arm) if n.arm is not None else ""
            p = p_label(n.p)
            sub += [("text", f" · {x}") for x in (arm, p or "") if x]
            out.setdefault(g.outcome, []).append(ListedNumber(
                rust_trim(n.label) or o.outcome, value, sub,
                listed_problem(n, group, g.kind == "pair" and not in_compare,
                               rust_trim(n.pmid) in refs),
                (g.outcome, j)))
    return out


# ---------- the forest ----------

LADDER = (0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50)
LOG_MIN, LOG_MAX = LADDER[0], LADDER[-1]


@dataclass(frozen=True)
class Axis:
    lo: float
    hi: float
    null: float  # position of the dashed mark (1 or 0), in percent
    lo_text: str
    mid_text: str
    hi_text: str


def ratio_axis(bounds: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """The shared log axis of the drawn ratio rows: the ladder value at or below the
    smallest lower bound (and below 1), the one at or above the largest upper bound (and
    above 1), never past 0.05 or 50."""
    lo_b = min([1.0, *(lo for lo, _ in bounds)])
    hi_b = max([1.0, *(hi for _, hi in bounds)])
    below = [x for x in LADDER if x < 1 and x <= lo_b]
    above = [x for x in LADDER if x > 1 and x >= hi_b]
    return (below[-1] if below else LOG_MIN), (above[0] if above else LOG_MAX)


def _log_pos(x: float, lo: float, hi: float) -> float:
    if x <= 0:
        return 0
    return min(100, max(0, 100 * math.log10(x / lo) / math.log10(hi / lo)))


def _step(span: float) -> float:
    """The smallest of 1, 2, 5 × 10^k with span / step ≤ 5."""
    if span <= 0:
        return 1.0
    k = math.floor(math.log10(span / 5)) - 1
    while True:
        for m in (1, 2, 5):
            step = float(f"{m}e{k}")
            if span / step <= 5:
                return step
        k += 1


def diff_axis(lo: float, hi: float) -> tuple[float, float, float]:
    """A difference row's own axis (lo, hi, step): nice steps, 0 never at an edge."""
    a, b = min(lo, 0.0), max(hi, 0.0)
    step = _step(b - a)
    lo_axis = round(math.floor(round(a / step, 9)) * step, 9)
    hi_axis = round(math.ceil(round(b / step, 9)) * step, 9)
    if lo_axis == 0:
        lo_axis = round(lo_axis - step, 9)
    if hi_axis == 0:
        hi_axis = round(hi_axis + step, 9)
    return lo_axis + 0.0, hi_axis + 0.0, step


def _lin_pos(x: float, lo: float, hi: float) -> float:
    return min(100, max(0, 100 * (x - lo) / (hi - lo)))


def _pos(kind: str, x: float, scale: Axis) -> float:
    """Where `x` sits on the row's scale, in percent (clamped to 0–100)."""
    if kind == "ratio":
        return _log_pos(x, scale.lo, scale.hi)
    return _lin_pos(x, scale.lo, scale.hi)


def step_places(step: float) -> int:
    """The decimals a difference axis label needs: those of its step (1, 2 or 5 × 10^k has
    max(0, −k)), so the edges, multiples of the step, print exactly and distinctly
    (third check L1: two decimals made RD 0.001 to 0.003 read "0 · 0 · +0")."""
    return max(0, math.ceil(-math.log10(step) - 1e-9))


def _signed(v: float, places: int = 2) -> str:
    f = 10.0 ** places
    t = fmt_value(round_half_away(v * f) / f)
    return f"+{t}" if v > 0 else ("0" if v == 0 else t)


def _spoken(v: float) -> str:
    return fmt_value(v).replace("−", "minus ")


_RATIO_NAMES = {"RR": "Risk ratio", "OR": "Odds ratio", "HR": "Hazard ratio",
                "IRR": "Rate ratio"}


@dataclass(frozen=True)
class ForestRow:
    label: str
    estimate_text: str
    sub_pieces: list[Piece]
    drawn: bool
    problem: str | None
    left: float = 0
    width: float = 0
    est: float = 0
    null: float = 0
    scale: Axis | None = None
    aria: str = ""
    at: tuple[int, int] = (-1, -1)  # (outcome, number)

    @property
    def sub(self) -> str:
        return pieces_text(self.sub_pieces)


@dataclass(frozen=True)
class Forest:
    ratios: list[ForestRow]
    diffs: list[ForestRow]
    ratio_axis: Axis | None


def study_pieces(w: WrittenAnswer, pmid: str, refs: Mapping[str, int]) -> list[Piece]:
    """The study's short name and its reference number, or "(not in the study list)"."""
    s = study_of(w, pmid)
    p = rust_trim(pmid)
    name = study_short(s.label) if s is not None and study_short(s.label) else f"PMID {p}"
    if p in refs:
        return [("text", f"{name} "), ("cite", [refs[p]])]
    return [("text", f"{name} ({NOT_IN_LIST})")]


NOT_PLACED = "the numbers could not be placed on a scale"
_SPAN_SLACK = 1e-6  # of the axis span: float noise allowed where the axis meets the interval


def _diff_scale(n: Number) -> Axis | None:
    """The row's own difference axis, or None when no finite axis with nice steps holds the
    interval (wider than floats can measure, or narrower than its 9-decimal rounding)."""
    lo, hi = n.ci_low, n.ci_high
    assert lo is not None and hi is not None
    try:
        a, b, step = diff_axis(lo, hi)
    except (ArithmeticError, ValueError):
        return None
    span = b - a
    # 0 strictly inside (X16): at the 9-decimal rounding a tiny interval can put an edge on 0.
    if not (math.isfinite(span) and span > 0 and a < 0 < b and a - lo <= _SPAN_SLACK * span
            and hi - b <= _SPAN_SLACK * span):
        return None
    places = step_places(step)
    # An edge above about 1.8e306 is too wide to label (re-check M-B, kept as it was: an edge
    # × 100 not finite), and an edge × 10^places must be finite for its label.
    if not all(math.isfinite(x * f) for x in (a, b) for f in (100.0, 10.0 ** places)):
        return None
    return Axis(a, b, _lin_pos(0, a, b), _signed(a, places), "0", _signed(b, places))


def _diff_scale_problem(n: Number) -> str:
    lo, hi = n.ci_low, n.ci_high
    assert lo is not None and hi is not None
    span = max(hi, 0.0) - min(lo, 0.0)
    if not math.isfinite(span) or span >= 1:
        return "the interval is too wide to draw"
    return "the interval is too narrow to draw"


def _drawn_row(kind: str, n: Number, scale: Axis, label: str, text: str, sub: list[Piece],
               extra: list[str], at: tuple[int, int]) -> ForestRow | None:
    """A drawn row on `scale`, or None when a position is not a finite number."""
    lo_v, hi_v = n.ci_low, n.ci_high
    assert lo_v is not None and hi_v is not None
    if kind == "ratio":
        null_v = 1.0
        name = _RATIO_NAMES[ratio_unit(n) or ""]
        past = lo_v < scale.lo or hi_v > scale.hi
    else:
        null_v = 0.0
        name = "Mean difference" if difference_measure(n) in _MEAN_TERMS else "Difference"
        past = False
    left = _pos(kind, lo_v, scale)
    width = max(_pos(kind, hi_v, scale) - left, 0.5)
    est = _pos(kind, n.value, scale)
    if not all(math.isfinite(x) for x in (left, width, est, scale.null)):
        return None
    side = ("below" if hi_v < null_v else "above" if lo_v > null_v else "crossing")
    unit = rust_trim(n.unit or "")
    value = _spoken(n.value)
    if kind == "diff" and unit:
        value += "%" if unit == "%" else f" {unit}"
    aria = (f"{name} {value}, interval {_spoken(lo_v)} to {_spoken(hi_v)}, {side} "
            f"{fmt_value(null_v)}")
    extra = list(extra)
    if past:
        aria += f", {PAST_SCALE}"
        extra.append(PAST_SCALE)
    sub = sub + [("text", f" · {x}") for x in extra]
    return ForestRow(label, text, sub, True, None, left, width, est, scale.null, scale, aria,
                     at)


def forest(w: WrittenAnswer, refs: Mapping[str, int]) -> Forest:
    """Every ratio or difference with both CI bounds (Incisive `forest_rows`), ratios then
    differences, each in group order; drawn only when the checks pass."""
    found: list[tuple[str, Number, Outcome, tuple[int, int]]] = []
    for g in all_groups(w, refs):
        o = w.outcomes[g.outcome]
        for j in g.numbers:
            n = o.numbers[j]
            fk = forest_kind(n)
            if fk is not None:
                found.append((fk, n, o, (g.outcome, j)))
    problems = [draw_problem(n, kind, refs)  # type: ignore[arg-type]
                or (ONE_ARM_LABEL if kind == "diff" and names_one_side(
                    n.label, comparison_of(w, n.pmid)) else None)
                for kind, n, _, _ in found]
    drawn_ratios = [(n.ci_low, n.ci_high) for (kind, n, _, _), p in zip(found, problems,
                                                                       strict=True)
                    if kind == "ratio" and p is None]
    r_axis = None
    if drawn_ratios:
        lo, hi = ratio_axis(drawn_ratios)  # type: ignore[arg-type]
        r_axis = Axis(lo, hi, _log_pos(1, lo, hi), fmt_axis(lo), "1", fmt_axis(hi))
    ratios: list[ForestRow] = []
    diffs: list[ForestRow] = []
    failed = 0
    for (kind, n, o, at), problem in zip(found, problems, strict=True):
        label = rust_trim(n.label) or o.outcome
        sub = study_pieces(w, n.pmid, refs)
        extra = [x for x in (rust_trim(n.arm) if n.arm else "",
                             rust_trim(n.unit) if kind == "diff" and n.unit else "") if x]
        text = ratio_estimate(n) if kind == "ratio" else diff_estimate(n)
        drawn = None
        if problem is None:
            # The whole derivation of one row (scale, axis labels, positions, aria) is
            # guarded: any arithmetic failure lists this row only (re-check M-B).
            try:
                scale = r_axis if kind == "ratio" else _diff_scale(n)
                if scale is None:
                    problem = _diff_scale_problem(n)
                else:
                    drawn = _drawn_row(kind, n, scale, label, text, sub, extra, at)
                    if drawn is None:
                        problem = NOT_PLACED
            except (ArithmeticError, ValueError):
                drawn, problem = None, NOT_PLACED
            if problem == NOT_PLACED:
                failed += 1
        if drawn is None:
            sub += [("text", f" · {x}") for x in extra]
            drawn = ForestRow(label, text, sub, False, problem, at=at)
        (ratios if kind == "ratio" else diffs).append(drawn)
    if failed:
        log.warning("answer_page kind=row_not_derived rows=%d", failed)
    return Forest(ratios, diffs, r_axis)


# ---------- out of 100 ----------


def out_of_100_count(v: float) -> int:
    """The filled cells of a 10 × 10 grid for a percentage (half away from zero)."""
    if not math.isfinite(v):
        return 0
    return int(min(100, max(0, round_half_away(v))))


def out_of_100_groups(w: WrittenAnswer, groups: Sequence[NumGroup]) -> list[NumGroup]:
    """Incisive's selection (paired, every unit `%`, no difference), two arms only (X20); a
    difference is `is_difference` (review M2). Both arms must be named (`compared`, owner
    decision d23, X29): Incisive labels a missing arm "arm 2"; here the pair is not drawn
    and its numbers are listed in the outcome with the reason."""
    return [g for g in groups if g.kind == "pair" and len(g.numbers) == 2 and compared(w, g)
            and all(n.unit is not None and rust_trim(n.unit) == "%" and not is_difference(n)
                    for n in g.nums(w))]


def arms_match_sides(nums: Sequence[Number], comparison: str | None) -> bool:
    """Two numbers whose arm texts match opposite sides of the comparison (`arm_side`)."""
    sides = [arm_side(n.arm, comparison) for n in nums]
    return len(sides) == 2 and None not in sides and set(sides) == {0, 1}


@dataclass(frozen=True)
class OutItem:
    label: str
    sentence_pieces: list[Piece]
    certainty: str
    arms: list[tuple[int, str, str]]  # (count of 100, arm name, "A" or "B")


ALL_ZERO = "both arms round to 0 of 100"
LABEL_DIFF = "the label names a difference, not a rate"
LABEL_CHANGE = "the label names a change, not a rate"
NO_SIDES = "the study's comparison does not name two sides"
# Words that make a label name a change rather than a rate (coordinator ruling 2 on the
# fourth check): matched as whole words on the case-folded display key.
_CHANGE_WORDS = ("reduction", "reduced", "increase", "increased", "decrease", "decreased",
                 "change", "changed", "improvement", "gain", "excess", "delta", "δ",
                 "relative risk", "risk ratio", "odds ratio", "hazard ratio", "ratio", "nnt",
                 "nnh", "arr", "rrr")
_CHANGE = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in _CHANGE_WORDS) + r")(?!\w)")


def label_names_change(label: str) -> bool:
    """The label names a change, not a rate (coordinator ruling 2): one of `_CHANGE_WORDS` as
    a whole word, case-folded. Such a pair gets no squares."""
    return _CHANGE.search(_display_key(label).casefold()) is not None
NOT_MATCHED = "its arms do not match the study's comparison"


def out_of_100_problem(w: WrittenAnswer, g: NumGroup, refs: Mapping[str, int]) -> str | None:
    """Why a two-arm percent group is not drawn as squares (shown in "Compare the arms"), or
    None. After the value checks: the label must not name a difference (fourth check F3)
    nor a change (`label_names_change`; coordinator ruling 2),
    the study's comparison must split into two sides
    (`comparison_sides`) and the two arm texts match opposite sides (`arm_side`; fourth
    check F1, coordinator ruling option a). The counts keep Incisive's rounding; Incisive has no rule for both arms at 0
    (`answer_page.rs` `out_of_100`, `app/page.rs` `out_of_100_view`), so two empty grids
    are not drawn (review L7)."""
    nums = g.nums(w)
    try:  # one pair's arithmetic never fails the page (re-check M-B)
        for n in nums:
            problem = draw_problem(n, "percent", refs)
            if problem is not None:
                return problem
        counts = [out_of_100_count(n.value) for n in nums]
        _ = [fmt_value(n.value) for n in nums]  # the sentence's values
    except (ArithmeticError, ValueError):
        return NOT_PLACED
    # A label that names a difference is no rate to draw as squares (fourth check F3), by
    # either rule: the reason's (`labelled_difference`) or the drawing's (`label_measure`).
    if any(labelled_difference(n) or label_measure(n.label) is not None for n in nums):
        return LABEL_DIFF
    if any(label_names_change(n.label) for n in nums):
        return LABEL_CHANGE
    # Squares need positive evidence of which arm is which (fourth check F1): each arm text
    # matches one side of the study's two-sided comparison, the two arms opposite sides.
    comparison = comparison_of(w, g.pmid)
    if comparison_sides(comparison) is None:
        return NO_SIDES
    if not arms_match_sides(nums, comparison):
        return NOT_MATCHED
    if all(c == 0 for c in counts):
        return ALL_ZERO
    return None


def out_of_100(w: WrittenAnswer, refs: Mapping[str, int]) -> list[OutItem]:
    """The "Out of 100" items: every two-arm percent group whose numbers pass the checks."""
    items: list[OutItem] = []
    for g in out_of_100_groups(w, all_groups(w, refs)):
        nums = g.nums(w)
        if out_of_100_problem(w, g, refs) is not None:
            continue
        o = w.outcomes[g.outcome]
        names = [arm_name(n) for n in nums]
        exact = " vs ".join(f"{fmt_value(n.value)}% with {a}" for n, a in zip(nums, names,
                                                                              strict=True))
        sentence: list[Piece] = [("bold", f"{g.label}:"), ("text", f" {exact} "),
                                 ("cite", [refs[rust_trim(g.pmid)]]),
                                 ("text", f". Certainty for this outcome: {o.certainty}.")]
        arms = [(out_of_100_count(n.value), a, "AB"[i]) for i, (n, a) in
                enumerate(zip(nums, names, strict=True))]
        items.append(OutItem(g.label, sentence, o.certainty, arms))
    return items


# ---------- compare the arms ----------

_PARENS = re.compile(r"\(.*?\)")


def normalise_comparison(c: str) -> str:
    """A comparison as a grouping key: lower case, parentheticals removed, spaces squashed."""
    return rust_trim(_SPACES.sub(" ", _PARENS.sub("", c.lower())))


@dataclass(frozen=True)
class CompareRow:
    label: str
    certainty: str
    values_text: str
    study_pieces: list[Piece]
    p_text: str | None
    at: tuple[tuple[int, int], ...] = ()  # (outcome, number) of each value
    problem: str | None = None  # why an out-of-100 pair is not drawn as squares

@dataclass(frozen=True)
class CompareGroup:
    label: str
    rows: list[CompareRow]


def compare(w: WrittenAnswer, refs: Mapping[str, int]) -> list[CompareGroup]:
    """Paired groups by their study's comparison, first-appearance order (text only). Each
    value is shown after the arm name the writer gave for that number ("TEP 9.4% vs
    Lichtenstein 18.8%", Incisive `app/page.rs` `compare_view`, `PairBar.arm`); a pair with
    an arm not named is listed in its outcome instead (review H1)."""
    out: list[tuple[str, str, list[CompareRow]]] = []
    groups = all_groups(w, refs)
    squares = {id(g) for g in out_of_100_groups(w, groups)}
    for g in groups:
        if not compared(w, g):
            continue
        s = study_of(w, g.pmid)
        label = rust_trim(s.comparison) if s is not None and rust_trim(s.comparison) else "Other"
        key = normalise_comparison(label)
        nums = g.nums(w)
        row = CompareRow(g.label, w.outcomes[g.outcome].certainty,
                         " vs ".join(f"{arm_name(n)} {with_unit(n.value, n.unit)}"
                                     for n in nums),
                         study_pieces(w, g.pmid, refs), g.p_text(w),
                         tuple((g.outcome, j) for j in g.numbers),
                         out_of_100_problem(w, g, refs) if id(g) in squares else None)
        for k, _label, rows in out:
            if k == key:
                rows.append(row)
                break
        else:
            out.append((key, label, [row]))
    return [CompareGroup(label, rows) for _, label, rows in out]


# ---------- pills and outcome cards ----------

_PILLS = {"High": "hi", "Moderate": "mo", "Low": "lo", "Very low": "vl"}


def pill_class(certainty: str) -> str:
    return _PILLS.get(certainty, "vl")


@dataclass(frozen=True)
class OutcomeCard:
    title: str
    certainty: str
    pill: str
    evidence_label: str | None
    n_numbers: str
    first_sentence: str
    evidence_pieces: list[Piece]
    effect_paragraphs: list[list[Piece]]
    why_pieces: list[Piece]
    listed: list[ListedNumber] = field(default_factory=list)  # numbers drawn nowhere else


def outcome_cards(w: WrittenAnswer, refs: Mapping[str, int]) -> list[OutcomeCard]:
    listed = listed_numbers(w, refs)
    return [OutcomeCard(o.outcome, o.certainty, pill_class(o.certainty),
                        o.evidence_type.label(),
                        count_noun(len(o.numbers), "number", "numbers"),
                        first_sentence(o.effect_text),
                        cite_pieces(rust_trim(_SPACES.sub(" ", o.rct_evidence)), refs),
                        paragraphs(o.effect_text, refs),
                        cite_pieces(rust_trim(_SPACES.sub(" ", o.why)), refs),
                        listed.get(i, []))
            for i, o in enumerate(w.outcomes)]


# ---------- the fold that keeps a pair level (X14) ----------


# The fold's weights are estimated heights in pixels at a 1,000 px wide window (each card's
# text about 443 px wide), measured in Chromium on the hernia page and on one with many
# "Not drawn" lines (Estimate; third check L4b, replacing plan Task 7's tenths). An item
# weighs a fixed part plus one line height for each line its texts take (`text_lines`, at
# the characters a line holds for that text's font).
HEAD = {"forest": 110, "outcomes": 65, "out100": 88, "compare": 110}  # padding, title, note
GROUP = 37  # a group heading (eyebrow) of one line; 17 more for each further line (45)
MORE = 39  # the "Show N more" line a folded card gains
LINE, SMALL, MONO = 24, 21, 20  # a line of body text, of .muted text, of .mono text
OUT100_BASE = 200  # an out-of-100 item without its sentence: margins, numbers, two grids
MIN_SHOWN = 2
FOLD_NOUNS = {"forest": ("estimate", "estimates"), "outcomes": ("outcome", "outcomes"),
              "out100": ("outcome", "outcomes"), "compare": ("result", "results")}


def text_lines(text: str, per_line: int) -> int:
    """The lines `text` takes at `per_line` characters a line: words wrap whole, a word longer
    than a line is broken anywhere (mock v5's wrap rule); at least one line."""
    n, cur = 1, 0
    for word in text.split():
        w = len(word)
        need = w if cur == 0 else cur + 1 + w
        if need <= per_line:
            cur = need
            continue
        if cur:
            n, cur = n + 1, 0
        while w > per_line:
            n, w = n + 1, w - per_line
        cur = w
    return n


def item_weight(kind: str, payload: Any) -> int:
    """An item's estimated height in pixels (deterministic; Estimate from measurements):

    - CI row: 56 + 24 a line of label and estimate (55 characters a line) + 21 a line of the
      study line (66); not drawn: 18 + the same + 21 a line of "Not drawn: <reason>." (66);
    - closed outcome: 60 + 24 a line of title (40) + 21 a line of evidence and count (60) +
      21 a line of the first sentence (66);
    - out-of-100 item: 200 + 24 a line of its sentence (50) + 21 for each line past the first
      of the longer "of 100 · <arm>" (22);
    - compare row: 36 + 24 a line of label (40) + 20 a line of values (44: the pill takes
      the right column) + 21 a line of study and p (66), + 6 + 21 a line of
      "Not drawn: <reason>." (66) when it has one."""
    if kind == "forest":
        r = payload
        h = LINE * text_lines(f"{r.label} {r.estimate_text}", 55) + SMALL * text_lines(r.sub, 66)
        if r.drawn:
            return 56 + h
        return 18 + h + SMALL * text_lines(f"Not drawn: {r.problem}.", 66)
    if kind == "outcomes":
        c = payload
        meta = f"{c.evidence_label} {c.n_numbers}" if c.evidence_label else c.n_numbers
        return (60 + LINE * text_lines(c.title, 40) + SMALL * text_lines(meta, 60)
                + SMALL * text_lines(c.first_sentence, 66))
    if kind == "out100":
        it = payload
        arm = max((text_lines(f"of 100 · {a}", 22) for _, a, _ in it.arms), default=1)
        return (OUT100_BASE + LINE * text_lines(pieces_text(it.sentence_pieces), 50)
                + SMALL * (arm - 1))
    r = payload
    study = pieces_text(r.study_pieces) + (f" · {r.p_text}" if r.p_text else "")
    h = (36 + LINE * text_lines(r.label, 40) + MONO * text_lines(r.values_text, 44)
         + SMALL * text_lines(study, 66))
    if r.problem:
        h += 6 + SMALL * text_lines(f"Not drawn: {r.problem}.", 66)
    return h


@dataclass(frozen=True)
class Item:
    weight: int
    heading: str | None = None  # the heading of the group this item starts
    heading_weight: int = 0
    payload: Any = field(default=None, compare=False)


@dataclass(frozen=True)
class Panel:
    head: int
    items: list[Item]

    def total(self, k: int | None = None) -> int:
        """The head, the first `k` items (all by default) and the headings they start."""
        items = self.items if k is None else self.items[:k]
        return self.head + sum(i.weight + i.heading_weight for i in items)

    def split(self, k: int) -> tuple[list[Item], list[Item]]:
        return self.items[:k], self.items[k:]


def fold_pair(left: Panel, right: Panel) -> tuple[int, int]:
    """How many items each panel shows. The longer panel keeps the longest prefix whose
    total, with the "Show N more" line it then gains (`MORE`), is at most the shorter
    panel's, never fewer than MIN_SHOWN; equal totals or an empty panel: no fold."""
    shown = [len(left.items), len(right.items)]
    if not left.items or not right.items:
        return shown[0], shown[1]
    lt, rt = left.total(), right.total()
    if lt == rt:
        return shown[0], shown[1]
    i, long_p, short_total = (0, left, rt) if lt > rt else (1, right, lt)
    k = 0
    while k < len(long_p.items) and long_p.total(k + 1) + MORE <= short_total:
        k += 1
    shown[i] = min(len(long_p.items), max(k, MIN_SHOWN))
    return shown[0], shown[1]


def heading_weight(text: str) -> int:
    """A group heading's estimated height in pixels (`GROUP`, 17 for each further line)."""
    return GROUP + 17 * (text_lines(text, 45) - 1)


def _items(kind: str, entries: Sequence[tuple[str | None, Any]]) -> Panel:
    return Panel(HEAD[kind], [Item(item_weight(kind, payload), h,
                                   heading_weight(h) if h is not None else 0, payload)
                              for h, payload in entries])


def forest_panel(body: AnswerV3) -> Panel:
    if body.answer is None:
        return Panel(HEAD["forest"], [])
    f = forest(body.answer, refs_of(body))
    entries = [(RATIO_HEADING if i == 0 else None, r) for i, r in enumerate(f.ratios)]
    entries += [(DIFF_HEADING if i == 0 else None, r) for i, r in enumerate(f.diffs)]
    return _items("forest", entries)


def outcomes_panel(body: AnswerV3) -> Panel:
    cards = outcome_cards(body.answer, refs_of(body)) if body.answer is not None else []
    return _items("outcomes", [(None, c) for c in cards])


def out100_panel(body: AnswerV3) -> Panel:
    items = out_of_100(body.answer, refs_of(body)) if body.answer is not None else []
    return _items("out100", [(None, it) for it in items])


def compare_panel(body: AnswerV3) -> Panel:
    groups = compare(body.answer, refs_of(body)) if body.answer is not None else []
    return _items("compare", [(g.label if i == 0 else None, r)
                              for g in groups for i, r in enumerate(g.rows)])


# ---------- the whole page (Task 8) ----------


@dataclass(frozen=True)
class PanelView:
    """One card of a pair: its items as shown, then those folded into "Show N more"."""

    kind: str
    shown: list[Item]
    hidden: list[Item]
    more_text: str


@dataclass(frozen=True)
class PairView:
    heading: str
    sub: str
    left: PanelView | None  # None: no card (an empty panel)
    right: PanelView | None
    note: str | None = None  # the "no structured numbers" note, in place of the left card


def _panel_view(kind: str, p: Panel, k: int) -> PanelView | None:
    if not p.items:
        return None
    shown, hidden = p.split(k)
    one, many = FOLD_NOUNS[kind]
    n = len(hidden)
    return PanelView(kind, shown, hidden, f"Show {n} more {one if n == 1 else many}")


def _pair(n: int, kinds: tuple[str, str], left: Panel, right: Panel,
          note: str | None = None) -> PairView | None:
    heading, sub = PAIRS[n]
    if not left.items and not right.items and note is None:
        return None
    if left.items and right.items:
        kl, kr = fold_pair(left, right)
    else:  # one card alone, at full width, unfolded
        kl, kr = len(left.items), len(right.items)
    return PairView(heading, sub, _panel_view(kinds[0], left, kl),
                    _panel_view(kinds[1], right, kr), note)


@dataclass(frozen=True)
class TrialView:
    ref_pieces: list[Piece]
    label: str
    facts: str
    key_pieces: list[Piece]
    appraisal: str


@dataclass(frozen=True)
class ReferenceView:
    n: int
    pmid: str
    text: str
    retracted: bool


def _ref_text(title: str, journal: str | None, year: int | None) -> str:
    t = rust_trim(title).rstrip(".")
    tail = ", ".join(x for x in (rust_trim(journal) if journal else "",
                                 str(year) if year is not None else "") if x)
    return f"{t}. {tail}." if tail else f"{t}."


def _clean(text: str) -> str:
    return rust_trim(_SPACES.sub(" ", text))


def _scrub(x: Any) -> Any:
    """`x` with `drop_invisible` applied to every string in it, at any depth (pydantic models,
    dataclasses and lists); numbers and None as they are. A new value: `x` is not changed. An
    `Enum` is returned as it is: a StrEnum is a `str`, and scrubbing it would turn `Certainty`
    or `EvidenceType` into a plain string (F3b plan §4)."""
    if isinstance(x, enum.Enum):
        return x
    if isinstance(x, str):
        return drop_invisible(x)
    if isinstance(x, list):
        return [_scrub(i) for i in x]
    if isinstance(x, BaseModel):
        return x.model_copy(update={name: _scrub(getattr(x, name))
                                    for name in type(x).model_fields})
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return dataclasses.replace(x, **{f.name: _scrub(getattr(x, f.name))
                                         for f in dataclasses.fields(x) if f.init})
    return x


def model_text_cleaned(body: AnswerV3) -> AnswerV3:
    """The body with bidi controls and zero-width characters removed from the model's text
    (`answer`, `notes`, `searches`; third check L3). On main nothing strips them before save
    (owner decision N10: on the page only), so this is the page's one defence."""
    return body.model_copy(update={"answer": _scrub(body.answer), "notes": _scrub(body.notes),
                                   "searches": _scrub(body.searches)})


def answer_view(body: AnswerV3) -> dict[str, Any]:
    """Everything the answer page draws, computed here; the template only places it."""
    body = model_text_cleaned(body)
    refs = refs_of(body)
    v: dict[str, Any] = {
        "empty_reason": body.empty_reason,
        "references": [ReferenceView(r.n, r.pmid, _ref_text(r.title, r.journal, r.year),
                                     r.retracted) for r in body.references],
        "trail": [f"{e.source}: {e.query} ({e.hits:,} found)" for e in body.searches],
        "notes": list(body.notes),
        "answer": body.answer is not None,
    }
    w = body.answer
    if w is None:
        return v

    def cp(text: str) -> list[Piece]:
        return cite_pieces(_clean(text), refs)

    b = w.bottom_line
    v["bottom"] = {"paragraphs": paragraphs(b.text, refs), "pill": pill_class(b.certainty),
                   "certainty": b.certainty, "reason": cp(b.certainty_reason)}
    practice = []
    c = w.clinical_practice
    if c is not None:
        if _clean(c.text):
            practice.append((None, cp(c.text)))
        for label, text in (("Default choice", c.default_choice),
                            ("Who may benefit", c.who_may_benefit),
                            ("Counselling", c.counselling), ("Cost per case", c.cost_per_case)):
            if _clean(text):
                practice.append((label, cp(text)))
    v["practice"] = practice
    g = w.at_a_glance
    v["glance"] = None if g is None else {
        "verdict": cp(g.verdict),
        "keys": [(_clean(k.label), cp(k.value_text)) for k in g.key_numbers
                 if _clean(k.label) or _clean(k.value_text)],
        "caveat": cp(g.main_caveat) if _clean(g.main_caveat) else None}
    no_numbers = not all_groups(w, refs)
    forest_p = forest_panel(body)
    v["pairs"] = [p for p in (
        _pair(0, ("forest", "outcomes"), Panel(forest_p.head, []) if no_numbers else forest_p,
              outcomes_panel(body), NO_NUMBERS if no_numbers else None),
        _pair(1, ("out100", "compare"), out100_panel(body), compare_panel(body)),
    ) if p is not None]
    v["outcome_count"] = count_noun(len(w.outcomes), "outcome", "outcomes")
    v["conflicts"] = [cp(x.text) for x in w.conflicts if _clean(x.text)]
    a = w.applicability
    v["applicability"] = [(label, cp(text)) for label, text in (
        ("Case mix", a.case_mix), ("India and cost", a.india_cost_context),
        ("Training", a.training)) if _clean(text)]
    v["would_change"] = [cp(x) for x in w.would_change if _clean(x)]
    trials = []
    for s in w.studies:
        p = rust_trim(s.pmid)
        ref: list[Piece] = [("cite", [refs[p]])] if p in refs else [
            ("text", f"PMID {p} ({NOT_IN_LIST})")]
        facts = " · ".join(x for x in (_clean(s.design),
                                       f"n = {fmt_value(s.n)}" if s.n is not None else "",
                                       _clean(s.setting), _clean(s.comparison)) if x)
        trials.append(TrialView(ref, _clean(s.label).rstrip("."), facts, cp(s.key_results),
                                _clean(s.appraisal)))
    v["trials"] = trials
    v["notable"] = [cp(n.text) for n in w.notable_findings if _clean(n.text)]
    # Main stores guidelines (Quick leaves the list empty); drawn as main's run page drew them.
    if w.guidelines:
        v["guidelines"] = [(_clean(x.body), cp(x.statement), x.year,
                            rust_trim(x.pmid) if x.pmid is not None else None, x.pmid_removed)
                           for x in w.guidelines]
    return v
