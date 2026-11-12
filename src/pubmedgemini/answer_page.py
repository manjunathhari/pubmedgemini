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

Defined here for the frozen mock (`docs/mocks/f3-answer-page-v4.html`; F3 plan Task 7): the
draw checks (D3a), the ratio axis from the data (D3b), the difference axis that never puts 0
at an edge (X16), the out-of-100 sentence (X21), the pills and the fold that keeps the two
cards of a pair level (X14). Rounding is Rust's `f64::round` (half away from zero).
"""

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.writer.answer import (
    EVIDENCE_LABELS,
    AnswerBody,
    Number,
    Outcome,
    Study,
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


def refs_of(body: AnswerBody) -> dict[str, int]:
    """The reference number of each PMID (code's, in selection order)."""
    return {r.pmid: r.n for r in body.references}


def cite_pieces(text: str, refs: Mapping[str, int]) -> list[Piece]:
    """`text` as text runs and citations: known PMIDs as sorted, de-duplicated reference
    numbers; PMIDs not in the references counted, to be drawn as "[not in the study list]"
    (flag, never delete: plan D4)."""
    out: list[Piece] = []
    for kind, v in split_citations(text, lambda p: p in refs):
        if kind == "text":
            out.append(("text", v))
            continue
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


def is_ratio(n: Number) -> bool:
    """A ratio: unit RR, OR or HR."""
    return n.unit is not None and rust_trim(n.unit) in ("RR", "OR", "HR")


_DIFF = re.compile(r"difference|\bMD\b|minus", re.IGNORECASE)


def is_diff(n: Number) -> bool:
    """A difference: its label or arm reads as one (MD, "difference", "minus")."""
    return bool(_DIFF.search(f"{n.label} {n.arm or ''}"))


def arm_name(n: Number) -> str:
    """The arm name the writer gave for this number, trimmed ("" when none)."""
    return rust_trim(n.arm) if n.arm is not None else ""


def names_an_arm(n: Number) -> bool:
    """The number carries an arm name: its arm text is not blank and is not itself a
    difference marker (Incisive's own examples put "MD" or "difference" in `arm`)."""
    a = arm_name(n)
    return bool(a) and not _DIFF.search(a)


def is_difference(n: Number) -> bool:
    """A difference for the page: Incisive's `is_diff` AND no arm name. A number with an arm
    name is one arm's value, never drawn as a difference (review M2, owner decision d24,
    12 Nov 2026: an approved difference from Incisive)."""
    return is_diff(n) and not names_an_arm(n)


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
    arms = {rust_trim(n.arm) if n.arm is not None else None for n in nums}
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
    return "diff" if is_difference(n) else None


def compared(w: WrittenAnswer, g: "NumGroup") -> bool:
    """A pair group "Compare the arms" shows: every value has its own arm name (review H1,
    owner decision d23: an arm is never inferred from position or from the heading)."""
    return g.kind == "pair" and all(arm_name(n) for n in g.nums(w))


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


def arm_lines(o: Outcome, comparison_of) -> list[tuple[str, str, list[int]]]:
    """The outcome's numbers grouped by label and study in first-appearance order, each
    group with the intervention arm (more words shared with the left side of the study's
    comparison) first; a stable sort, so equal scores keep the writer's order."""
    lines: list[tuple[str, str, list[int]]] = []
    for i, n in enumerate(o.numbers):
        label, pmid = rust_trim(n.label), rust_trim(n.pmid)
        for line in lines:
            if line[0] == label and line[1] == pmid:
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


def outcome_groups(w: WrittenAnswer, i: int) -> list[NumGroup]:
    o = w.outcomes[i]

    def comparison_of(p: str) -> str | None:
        s = study_of(w, p)
        return s.comparison if s is not None else None

    return [NumGroup(i, label, pmid, idx, chart_kind([o.numbers[j] for j in idx]))
            for label, pmid, idx in arm_lines(o, comparison_of)]


def all_groups(w: WrittenAnswer) -> list[NumGroup]:
    """Every outcome's groups, in page order."""
    return [g for i in range(len(w.outcomes)) for g in outcome_groups(w, i)]


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
    if kind == "ratio" and not (n.value > 0 and lo > 0 and hi > 0):
        return "a ratio must be above 0"
    if lo > hi:
        return "the interval is reversed"
    if not lo <= n.value <= hi:
        return "the estimate is outside its interval"
    return None


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


def listed_problem(n: Number, group: Sequence[Number], pair: bool = False) -> str:
    """Why a number that is neither a forest row nor a "Compare the arms" value is not drawn.
    `pair`: its group is a pair left out of "Compare the arms" for an arm with no name."""
    if pair:
        return "the other arm is not named" if arm_name(n) else "arm not named"
    if is_diff(n) and names_an_arm(n):
        return ONE_ARM_RATE
    if not is_ratio(n) and _looks_ratio(n):
        return "unit not RR, OR or HR"
    if (is_ratio(n) or is_difference(n)) and not has_ci(n):
        return "no interval"
    if len(group) == 1:
        return "not marked as a ratio or a difference" if has_ci(n) else "single arm"
    if any(is_ratio(x) or is_difference(x) or _looks_ratio(x) for x in group):
        return "the group mixes kinds"
    return "its arms are not distinct"


ONE_ARM_RATE = "one arm's rate, not a difference"


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
    with the reason (flag, never delete: every number the writer gave is on the page)."""
    out: dict[int, list[ListedNumber]] = {}
    for g in all_groups(w):
        in_compare = compared(w, g)
        o = w.outcomes[g.outcome]
        group = g.nums(w)
        for j in g.numbers:
            n = o.numbers[j]
            if forest_kind(n) is not None:
                continue
            if in_compare and not (has_ci(n) and is_diff(n)):
                continue  # shown in "Compare the arms"; an armed "difference" is also listed
            value = with_unit(n.value, n.unit)
            if n.ci_low is not None or n.ci_high is not None:
                value += f" ({_opt_bound(n.ci_low)} to {_opt_bound(n.ci_high)})"
            sub = study_pieces(w, n.pmid, refs)
            arm = rust_trim(n.arm) if n.arm is not None else ""
            p = p_label(n.p)
            sub += [("text", f" · {x}") for x in (arm, p or "") if x]
            out.setdefault(g.outcome, []).append(ListedNumber(
                rust_trim(n.label) or o.outcome, value, sub,
                listed_problem(n, group, g.kind == "pair" and not in_compare),
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


def _signed(v: float) -> str:
    t = fmt_axis(v)
    return f"+{t}" if v > 0 else ("0" if v == 0 else t)


def _spoken(v: float) -> str:
    return fmt_value(v).replace("−", "minus ")


_RATIO_NAMES = {"RR": "Risk ratio", "OR": "Odds ratio", "HR": "Hazard ratio"}
_MEAN_DIFF = re.compile(r"\bMD\b|mean difference", re.IGNORECASE)


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
        a, b, _ = diff_axis(lo, hi)
    except (ArithmeticError, ValueError):
        return None
    span = b - a
    if not (math.isfinite(span) and span > 0 and a - lo <= _SPAN_SLACK * span
            and hi - b <= _SPAN_SLACK * span):
        return None
    return Axis(a, b, _lin_pos(0, a, b), _signed(a), "0", _signed(b))


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
        name = _RATIO_NAMES[rust_trim(n.unit or "")]
        past = lo_v < scale.lo or hi_v > scale.hi
    else:
        null_v = 0.0
        name = ("Mean difference" if _MEAN_DIFF.search(f"{n.label} {n.arm or ''}")
                else "Difference")
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
    for g in all_groups(w):
        o = w.outcomes[g.outcome]
        for j in g.numbers:
            n = o.numbers[j]
            fk = forest_kind(n)
            if fk is not None:
                found.append((fk, n, o, (g.outcome, j)))
    problems = [draw_problem(n, kind, refs)  # type: ignore[arg-type]
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
            scale = r_axis if kind == "ratio" else _diff_scale(n)
            if scale is None:
                problem = _diff_scale_problem(n)
            else:
                try:
                    drawn = _drawn_row(kind, n, scale, label, text, sub, extra, at)
                except (ArithmeticError, ValueError):
                    drawn = None
                if drawn is None:
                    failed += 1
                    problem = NOT_PLACED
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


@dataclass(frozen=True)
class OutItem:
    label: str
    sentence_pieces: list[Piece]
    certainty: str
    arms: list[tuple[int, str, str]]  # (count of 100, arm name, "A" or "B")


ALL_ZERO = "both arms round to 0 of 100"


def out_of_100_problem(w: WrittenAnswer, g: NumGroup, refs: Mapping[str, int]) -> str | None:
    """Why a two-arm percent group is not drawn as squares (shown in "Compare the arms"), or
    None. The counts keep Incisive's rounding; Incisive has no rule for both arms at 0
    (`answer_page.rs` `out_of_100`, `app/page.rs` `out_of_100_view`), so two empty grids
    are not drawn (review L7)."""
    nums = g.nums(w)
    for n in nums:
        problem = draw_problem(n, "percent", refs)
        if problem is not None:
            return problem
    if all(out_of_100_count(n.value) == 0 for n in nums):
        return ALL_ZERO
    return None


def out_of_100(w: WrittenAnswer, refs: Mapping[str, int]) -> list[OutItem]:
    """The "Out of 100" items: every two-arm percent group whose numbers pass the checks."""
    items: list[OutItem] = []
    for g in out_of_100_groups(w, all_groups(w)):
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
    groups = all_groups(w)
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
                        EVIDENCE_LABELS.get(o.evidence_type),
                        count_noun(len(o.numbers), "number", "numbers"),
                        first_sentence(o.effect_text),
                        cite_pieces(rust_trim(_SPACES.sub(" ", o.rct_evidence)), refs),
                        paragraphs(o.effect_text, refs),
                        cite_pieces(rust_trim(_SPACES.sub(" ", o.why)), refs),
                        listed.get(i, []))
            for i, o in enumerate(w.outcomes)]


# ---------- the fold that keeps a pair level (X14) ----------


@dataclass(frozen=True)
class W:
    head: int
    item: int
    group: int


# Weights in tenths of the mock's rendered height (Estimate, from its CSS at about 500 px a
# column; plan Task 7).
WEIGHTS = {"forest": W(7, 10, 4), "outcomes": W(3, 13, 0), "out100": W(6, 24, 0),
           "compare": W(6, 7, 4)}
MIN_SHOWN = 2
FOLD_NOUNS = {"forest": ("estimate", "estimates"), "outcomes": ("outcome", "outcomes"),
              "out100": ("outcome", "outcomes"), "compare": ("result", "results")}


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
    total is at most the shorter panel's, never fewer than MIN_SHOWN; equal totals or an
    empty panel: no fold."""
    shown = [len(left.items), len(right.items)]
    if not left.items or not right.items:
        return shown[0], shown[1]
    lt, rt = left.total(), right.total()
    if lt == rt:
        return shown[0], shown[1]
    i, long_p, short_total = (0, left, rt) if lt > rt else (1, right, lt)
    k = 0
    while k < len(long_p.items) and long_p.total(k + 1) <= short_total:
        k += 1
    shown[i] = min(len(long_p.items), max(k, MIN_SHOWN))
    return shown[0], shown[1]


def _items(kind: str, entries: Sequence[tuple[str | None, Any]]) -> Panel:
    w = WEIGHTS[kind]
    return Panel(w.head, [Item(w.item, h, w.group if h is not None else 0, payload)
                          for h, payload in entries])


def forest_panel(body: AnswerBody) -> Panel:
    if body.answer is None:
        return Panel(WEIGHTS["forest"].head, [])
    f = forest(body.answer, refs_of(body))
    entries = [(RATIO_HEADING if i == 0 else None, r) for i, r in enumerate(f.ratios)]
    entries += [(DIFF_HEADING if i == 0 else None, r) for i, r in enumerate(f.diffs)]
    return _items("forest", entries)


def outcomes_panel(body: AnswerBody) -> Panel:
    cards = outcome_cards(body.answer, refs_of(body)) if body.answer is not None else []
    return _items("outcomes", [(None, c) for c in cards])


def out100_panel(body: AnswerBody) -> Panel:
    items = out_of_100(body.answer, refs_of(body)) if body.answer is not None else []
    return _items("out100", [(None, it) for it in items])


def compare_panel(body: AnswerBody) -> Panel:
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


def answer_view(body: AnswerBody) -> dict[str, Any]:
    """Everything the answer page draws, computed here; the template only places it."""
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
    no_numbers = not all_groups(w)
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
    return v
