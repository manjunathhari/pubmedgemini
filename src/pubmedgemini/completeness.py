"""The completeness check (F4a): code only, after the answer is stored.

Two counts, from the written answer and the records the run already holds:

1. how many of the answer's structured numbers (`Outcome.numbers`) are printed in the
   abstract of the study each one cites;
2. how many of the trial names (`Study.label`) agree with the record PubMed gave: its year,
   and its first author's surname or a name printed in the study's own title or abstract.

The rules are the plan's sections 3.1 and 3.2 (owner, 13 Nov 2026: O1 b, scope i; O2 T2). A
count says "this figure is printed somewhere in the cited abstract", never "this figure is
right": the rule does not read the unit, the arm, the sign or the words around a number. It
is not a port of anything.

Everything here takes plain data: no model call, no network call. The rules are pure and
change nothing they are given; `run_check` stores the four counts and never raises. Nothing
of an answer, a label, an author or an abstract is logged: counts, run ids and fixed kinds
only. Every regular expression is linear (no nested repeats).
"""

import logging
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from pubmedgemini.lit.paper import Paper
from pubmedgemini.writer.answer import Number, WrittenAnswer

log = logging.getLogger("pubmedgemini.completeness")

# ---------- 3.1 the numbers ----------

# A middle dot between two digits is a decimal point ("0·40").
_MID_DOT = re.compile(r"(?<=\d)[·∙⋅](?=\d)")
# 1 to 3 digits, then groups of a comma and exactly 3 digits, touching no other digit or comma.
_THOUSANDS = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,])")
# A number, only where the character before is not a letter, digit, underscore or dot.
_NUMBER = re.compile(r"(?<![\w.])(?:\d+(?:\.\d+)?|\.\d+)")


def abstract_numbers(text: str) -> list[Decimal]:
    """Every number printed in `text`, in order, as printed (its decimals kept), without its
    sign: a hyphen before a digit is a minus in one abstract and a range dash in the next, so
    sizes only are compared."""
    text = _MID_DOT.sub(".", text)
    text = _THOUSANDS.sub(lambda m: m.group().replace(",", ""), text)
    return [Decimal(m.group()) for m in _NUMBER.finditer(text)]


def figure(v: float) -> tuple[Decimal, int] | None:
    """The answer's figure: its size as the shortest decimal that reads back, and how many
    decimals that has (0 for a whole number: JSON cannot tell 14 from 14.0). None when the
    value is not finite."""
    if not math.isfinite(v):
        return None
    size = Decimal(repr(abs(float(v))))
    exponent = size.normalize().as_tuple().exponent
    return size, max(0, -int(exponent))


def _decimals(t: Decimal) -> int:
    return max(0, -int(t.as_tuple().exponent))


def figure_found(v: float, numbers: Sequence[Decimal]) -> bool:
    """True when some number `t` of the abstract equals the figure's size, or has more
    decimals than the figure and lies within half a unit of the figure's last decimal (the
    abstract's figure, rounded to the answer's decimals; the half-way case counts either
    way). Never the other way round: 14.3 is not found by "14"."""
    fig = figure(v)
    if fig is None:
        return False
    size, d = fig
    half = Decimal(5).scaleb(-(d + 1))  # 0.5 x 10^-d, exact
    low, high = size - half, size + half  # exact: a float's shortest form has few digits
    return any(t == size or (_decimals(t) > d and low <= t <= high) for t in numbers)


def _number_in(n: Number, numbers: Sequence[Decimal]) -> bool:
    return (figure_found(n.value, numbers)
            and (n.ci_low is None or figure_found(n.ci_low, numbers))
            and (n.ci_high is None or figure_found(n.ci_high, numbers)))


def number_found(n: Number, abstract: str | None) -> bool:
    """True when the entry's value, and each interval end that is given, is found in
    `abstract` by `figure_found`. `p` is not checked. No abstract, or a blank one: False."""
    if abstract is None or not abstract.strip():
        return False
    return _number_in(n, abstract_numbers(abstract))


# ---------- 3.2 the trial names ----------

_LETTERS = str.maketrans({"ø": "o", "ł": "l", "đ": "d", "æ": "ae", "œ": "oe", "ß": "ss"})
_WORD = re.compile(r"[^\W\d_]+")  # a run of letters
_YEAR = re.compile(r"(?<!\d)\d{4}(?!\d)")  # four digits, a number of its own
_TOKEN = re.compile(r"[^\W_]+(?:-[^\W_]+)*")  # letters and digits, with inner hyphens
_SUFFIXES = frozenset({"jr", "sr", "2nd", "3rd"})
_NOT_A_NAME = frozenset({"the", "a", "trial", "study", "et", "al"})
_MIN_SURNAME_WORD = 4
_MIN_LEADING_NAME = 3


def fold(s: str) -> str:
    """NFKD, combining marks dropped, case-folded, and the letters that do not decompose
    (ø, ł, đ, æ, œ, ß) written plainly."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return plain.casefold().translate(_LETTERS)


def _is_initials(token: str) -> bool:
    t = unicodedata.normalize("NFC", token)
    return 1 <= len(t) <= 3 and t.isalpha() and t.isupper()


def first_surname(authors: Sequence[str]) -> list[str]:
    """The words of the first author's surname, folded. PubMed writes "Surname AB" and
    "Surname AB Jr": a trailing Jr, Sr, 2nd or 3rd goes, then a last token of 1 to 3 capital
    letters (the initials). A group author (no initials) keeps all its words; a name of one
    token is never taken for initials."""
    if not authors:
        return []
    tokens = authors[0].split()
    if len(tokens) > 1 and tokens[-1].rstrip(".").casefold() in _SUFFIXES:
        tokens.pop()
    if len(tokens) > 1 and _is_initials(tokens[-1]):
        tokens.pop()
    return _WORD.findall(fold(" ".join(tokens)))


def year_matches(label: str, year: int | None) -> bool:
    """True when the record has a year and the label holds it as a number of its own."""
    if year is None:
        return False
    return any(int(y) == year for y in _YEAR.findall(label))


def _surname_matches(label: str, authors: Sequence[str]) -> bool:
    words = first_surname(authors)
    long_words = [w for w in words if len(w) >= _MIN_SURNAME_WORD]
    label_words = set(_WORD.findall(fold(label)))
    return any(w in label_words for w in (long_words or words))


def _leading_name(label: str) -> str | None:
    """The label's first token that is not a number (so not the year) and not one of the
    filler words; None when there is none."""
    for token in _TOKEN.findall(fold(label)):
        if token.isdigit() or token in _NOT_A_NAME:
            continue
        return token
    return None


def name_matches(label: str, paper: Paper) -> bool:
    """T2: PubMed's first-author surname is a word of the label, or the label's leading name
    (at least 3 characters) is printed as a whole token in the study's own title or
    abstract."""
    if _surname_matches(label, paper.authors):
        return True
    name = _leading_name(label)
    if name is None or len(name) < _MIN_LEADING_NAME:
        return False
    printed = fold(f"{paper.title or ''}\n{paper.abstract or ''}")
    return name in set(_TOKEN.findall(printed))


def trial_name_matches(label: str, paper: Paper | None) -> bool:
    """A trial row matches when the year matches and the name matches. No record: no match."""
    if paper is None:
        return False
    return year_matches(label, paper.year) and name_matches(label, paper)


# ---------- the counts ----------


@dataclass(frozen=True)
class Counts:
    numbers_found: int
    numbers_total: int
    names_match: int
    names_total: int


def check_answer(w: WrittenAnswer, papers: Mapping[str, Paper]) -> Counts:
    """The four counts for a written answer and the run's fetched records. One count per
    entry of every outcome's `numbers` (the same entry twice counts twice) and one per trial
    row. Reads only: neither `w` nor `papers` is changed."""
    printed: dict[str, list[Decimal] | None] = {}  # each abstract is read once

    def numbers_of(pmid: str) -> list[Decimal] | None:
        if pmid not in printed:
            p = papers.get(pmid)
            text = p.abstract if p is not None else None
            printed[pmid] = abstract_numbers(text) if text is not None and text.strip() else None
        return printed[pmid]

    found = total = 0
    for o in w.outcomes:
        for n in o.numbers:
            total += 1
            numbers = numbers_of(n.pmid)
            if numbers is not None and _number_in(n, numbers):
                found += 1
    match = sum(1 for s in w.studies if trial_name_matches(s.label, papers.get(s.pmid)))
    return Counts(found, total, match, len(w.studies))
