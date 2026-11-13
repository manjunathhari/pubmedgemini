"""F4a Task 2: the completeness check's two rules (plan §3.1 and §3.2), pure code. Every
text here is made up: no question, abstract, author or answer from a real run."""

import copy
import re
import time
from decimal import Decimal
from pathlib import Path

import pytest

from pubmedgemini.completeness import (
    Counts,
    abstract_numbers,
    check_answer,
    figure,
    figure_found,
    first_surname,
    fold,
    name_matches,
    number_found,
    trial_name_matches,
    year_matches,
)
from pubmedgemini.lit.paper import Paper
from pubmedgemini.writer.answer import Number, WrittenAnswer

D = Decimal


def paper(pmid="1", *, title="A made-up title", authors=("Surname AB",), year=2019,
          abstract="A made-up abstract."):
    return Paper(pmid=pmid, pmcid=None, doi=None, title=title, authors=list(authors),
                 journal="Example Journal", year=year, pub_types=[], abstract=abstract,
                 retracted=False, source="pubmed")


def number(value, *, ci_low=None, ci_high=None, p=None, pmid="1", label="Made-up label"):
    return Number(label=label, value=value, unit=None, arm=None, ci_low=ci_low,
                  ci_high=ci_high, p=p, pmid=pmid)


# ---------- §3.1 reading the abstract ----------


@pytest.mark.parametrize("text,want", [
    ("14.3% vs 20.1%", ["14.3", "20.1"]),
    ("0·40", ["0.40"]),  # a middle dot between two digits
    ("0∙0 and 1⋅5", ["0.0", "1.5"]),  # the two other dots
    ("a · b", []),  # a middle dot that is not between digits stays
    ("1,250 patients", ["1250"]),
    ("12,345,678", ["12345678"]),
    ("1,250.5 ml", ["1250.5"]),
    ("n=120", ["120"]),
    ("p<0.05", ["0.05"]),
    ("(.05)", ["0.05"]),
    ("(14.3%)", ["14.3"]),
    ("10-20", ["10", "20"]),
    ("0.61-0.94", ["0.61", "0.94"]),
    ("−0.03", ["0.03"]),  # the sign is dropped
    ("-3.2", ["3.2"]),
    ("CD4", []),
    ("T2", []),
    ("v1.2.3", []),
    ("1.2.3", ["1.2"]),  # the tail of 1.2.3 is not a number
    ("COVID-19", ["19"]),
    ("0,40", ["0", "40"]),  # a decimal comma: a documented miss
    ("1 250", ["1", "250"]),  # thousands split by a space: a documented miss
    ("1,25,000", ["1", "25", "0"]),  # not groups of three throughout: no comma goes
    ("1,2345", ["1", "2345"]),
    ("12,345,6", ["12", "345", "6"]),
    ("", []),
    ("no figures here", []),
])
def test_abstract_numbers(text, want):
    got = abstract_numbers(text)
    assert got == [D(w) for w in want]
    assert [str(g) for g in got] == want  # the decimals as printed are kept


def test_figure():
    assert figure(14.3) == (D("14.3"), 1)
    assert figure(14.0) == (D("14"), 0) and figure(14) == (D("14"), 0)
    assert figure(-3.25) == (D("3.25"), 2)
    assert figure(1250.0) == (D("1250"), 0)
    assert figure(0.0) == (D("0"), 0)
    assert figure(1e-7) == (D("0.0000001"), 7)
    assert figure(1e16) == (D("10000000000000000"), 0)
    assert figure(float("nan")) is None
    assert figure(float("inf")) is None and figure(float("-inf")) is None


@pytest.mark.parametrize("value,text,want", [
    (0.4, "0.40", True),
    (14.0, "14%", True),
    (14.0, "14.0%", True),
    (1250.0, "1,250", True),
    (14.3, "14.29", True),  # the abstract's figure, rounded to the answer's decimals
    (14.3, "14.31", True),
    (14.0, "14.3", True),
    (14.3, "14", False),  # rounding goes one way only
    (14.29, "14.3", False),  # the answer added precision
    (14.3, "14.36", False),
    (14.0, "14.51", False),
    (-3.2, "3.2", True),  # sizes only
    (3.2, "-3.2", True),
    (float("nan"), "1 2 3", False),
    (float("inf"), "1 2 3", False),
    (0.5, "0.45", True),  # half-way counts either way
    (0.5, "0.55", True),
    (0.5, "0.56", False),
    (0.5, "0.44", False),
    (14.0, "13.5", True),
    (14.0, "14.5", True),
    (5.0, "", False),
    (0.76, "0.7600000000000000000000000000000000000001", True),
    (0.76, "0.7650000000000000000000000000000000000001", False),  # exact, however long
])
def test_figure_found(value, text, want):
    assert figure_found(value, abstract_numbers(text)) is want


ABSTRACT = "Made-up: the ratio was 0.76 (95% CI 0.49 to 1.10; P = .21) in 120 patients."


def test_number_found_needs_every_given_ci_end():
    assert number_found(number(0.76, ci_low=0.49, ci_high=1.1), ABSTRACT) is True
    assert number_found(number(0.76, ci_low=0.49, ci_high=1.2), ABSTRACT) is False
    assert number_found(number(0.76, ci_low=0.4, ci_high=1.1), ABSTRACT) is False
    assert number_found(number(0.76, ci_low=0.5, ci_high=1.1), ABSTRACT) is True  # 0.49 rounds
    assert number_found(number(0.77, ci_low=0.49, ci_high=1.1), ABSTRACT) is False
    assert number_found(number(0.76), ABSTRACT) is True  # no ends: the value alone decides
    assert number_found(number(0.76, ci_low=0.49), ABSTRACT) is True
    assert number_found(number(0.76, ci_high=1.3), ABSTRACT) is False
    assert number_found(number(0.76, ci_low=float("nan")), ABSTRACT) is False
    # p is text "as written" and is not checked
    assert number_found(number(0.76, p="<0.001"), ABSTRACT) is True
    assert number_found(number(0.76, p="made-up words"), ABSTRACT) is True


@pytest.mark.parametrize("abstract", [None, "", "   \n\t "])
def test_number_edges_no_abstract(abstract):
    assert number_found(number(5.0), abstract) is False


def test_number_edges():
    assert number_found(number(float("nan")), ABSTRACT) is False
    assert number_found(number(float("inf")), ABSTRACT) is False
    w = answer([number(120.0, pmid="1"), number(120.0, pmid="404"), number(120.0, pmid="2"),
                number(120.0, pmid="1")], [])
    papers = {"1": paper("1", abstract=ABSTRACT), "2": paper("2", abstract=None)}
    # a pmid with no record and a study with no abstract: counted, not found; the same number
    # listed twice is counted twice
    assert check_answer(w, papers) == Counts(2, 4, 0, 0)


def test_known_false_matches_are_as_documented():
    """Plan §3.1: the count says "this figure is printed in the cited abstract", never "this
    figure is right". These are the rule, not bugs."""
    assert number_found(number(5.0), "Made-up: patients were followed for 5 years.") is True
    assert number_found(number(19.0), "Made-up: after COVID-19 the unit closed.") is True
    # the other arm's figure, and a wrong sign
    other_arm = "Made-up: 14.3% in arm A and 20.1% in arm B; the change was -3.2 points."
    assert number_found(number(20.1, label="Arm A"), other_arm) is True
    assert number_found(number(3.2), other_arm) is True
    # a whole number is found by any printed figure within half of it
    assert number_found(number(14.0), "Made-up: 13.6 units.") is True


def test_known_misses_are_as_documented():
    """Plan §3.1: true figures the rule reports "not found"."""
    assert number_found(number(14.3), "Made-up: 6 of 42 patients.") is False  # worked out
    assert number_found(number(0.14), "Made-up: 14% of patients.") is False  # another scale
    assert number_found(number(12.0), "Made-up: twelve patients.") is False  # in words
    assert number_found(number(0.4), "Made-up: a ratio of 0,40.") is False  # decimal comma
    assert number_found(number(1250.0), "Made-up: 1 250 patients.") is False
    assert number_found(number(14.29), "Made-up: 14.3%.") is False


# ---------- §3.2 the trial names ----------


def test_fold():
    assert fold("Exämple Ångström") == "example angstrom"
    assert fold("Øster Łuk Đorđe Æsop Œuvre Straße") == "oster luk dorde aesop oeuvre strasse"
    assert fold("EXAMPLE-2") == "example-2"


@pytest.mark.parametrize("authors,want", [
    (["Surname AB"], ["surname"]),
    (["Surname AB", "Other CD"], ["surname"]),  # the first author only
    (["van der Example JC"], ["van", "der", "example"]),
    (["Example-Other A"], ["example", "other"]),
    (["Exämple Å"], ["example"]),
    (["Surname AB Jr"], ["surname"]),
    (["Surname AB 3rd"], ["surname"]),
    (["Surname A Sr"], ["surname"]),
    (["O'Example AB"], ["o", "example"]),
    (["The EXAMPLE Study Group"], ["the", "example", "study", "group"]),
    (["Li"], ["li"]),  # one word: never dropped as initials
    ([""], []),
    ([], []),
])
def test_first_surname(authors, want):
    assert first_surname(authors) == want


def test_year_must_be_the_records():
    assert year_matches("Surname 2019, Example Journal", 2019) is True
    assert year_matches("Surname 2019, Example Journal", 2020) is False
    assert year_matches("Surname 2019, Example Journal", None) is False
    assert year_matches("Surname 20190", 2019) is False
    assert year_matches("Surname 12019", 2019) is False
    assert year_matches("Surname, Example Journal", 2019) is False
    assert year_matches("Surname (2019)", 2019) is True
    assert year_matches("Surname 2018/2019", 2019) is True
    assert year_matches("", 2019) is False


@pytest.mark.parametrize("label,p,want", [
    ("Surname 2019, Example Journal", paper(), True),
    ("SURNAME et al. 2019", paper(), True),
    ("Example 2019", paper(authors=["Exämple Å"]), True),  # folded accents
    ("Exämple 2019", paper(authors=["Example A"]), True),
    ("Othername 2019, Example Journal", paper(), False),  # another surname
    ("Surnames 2019", paper(), False),  # a whole word, not a part of one
    ("Li 2019", paper(authors=["Li X"]), True),  # no word of four letters: any of its words
    ("van Other 2019", paper(authors=["van der Example JC"]), False),  # "van" is not enough
    ("Example 2019", paper(authors=["van der Example JC"]), True),
    ("EXAMPLE-2 2019", paper(abstract="Made-up: the EXAMPLE-2 trial enrolled 120."), True),
    ("EXAMPLE-2 2019", paper(title="The EXAMPLE-2 trial: a made-up title"), True),
    ("The EXAMPLE-2 trial, 2019", paper(abstract="Made-up (EXAMPLE-2)."), True),
    ("EXAMPLE-2 2019", paper(), False),  # an acronym the record does not print
    ("EXAMPLE-2 2019", paper(abstract="Made-up: the EXAMPLE-22 trial."), False),
    ("EXAMPLE-2 2019", paper(abstract="Made-up: the EXAMPLE trial, 2 arms."), False),
    ("2019", paper(abstract="Made-up: in 2019, 120 patients."), False),  # only a year
    ("120 2019", paper(abstract="Made-up: in 2019, 120 patients."), False),  # only numbers
    ("XY 2019", paper(abstract="Made-up: the XY trial."), False),  # under three characters
    ("Othername 2019", paper(authors=[], abstract=None), False),  # no authors, no title hit
    ("Made-up 2019", paper(authors=[], abstract=None), True),  # no authors: the title route
    ("", paper(), False),
    # documented false pass (plan §3.2): a label starting with an ordinary title word
    ("Laparoscopic 2019", paper(title="Laparoscopic made-up repair"), True),
])
def test_name_matches(label, p, want):
    assert name_matches(label, p) is want


def test_a_row_needs_the_year_and_the_name():
    assert trial_name_matches("Surname 2019, Example Journal", paper()) is True
    assert trial_name_matches("Surname 2018, Example Journal", paper()) is False
    assert trial_name_matches("Othername 2019", paper()) is False
    assert trial_name_matches("Surname 2019", paper(year=None)) is False
    assert trial_name_matches("Surname 2019", None) is False  # no record for the pmid


# ---------- the counts ----------


def answer(numbers, labels):
    """A made-up written answer: `numbers` in two outcomes, one trial row per (pmid, label)."""
    half = len(numbers) // 2

    def outcome(ns):
        return {"outcome": "Made-up outcome", "rct_evidence": "", "effect_text": "",
                "numbers": [n.model_dump() for n in ns], "certainty": "Moderate", "why": "",
                "pmids": []}

    return WrittenAnswer.model_validate({
        "bottom_line": {"text": "Made-up.", "certainty": "Moderate", "certainty_reason": ""},
        "outcomes": [outcome(numbers[:half]), outcome(numbers[half:])],
        "studies": [{"pmid": pmid, "label": label, "design": "RCT", "n": 60, "setting": "",
                     "comparison": "", "key_results": "", "appraisal": ""}
                    for pmid, label in labels],
        "conflicts": [],
        "applicability": {"case_mix": "", "india_cost_context": "", "training": ""},
        "would_change": [],
    })


def test_check_answer_counts():
    w = answer([number(0.76, ci_low=0.49, ci_high=1.1, pmid="1"), number(42.0, pmid="2"),
                number(9.9, pmid="2")],
               [("1", "Surname 2019, Example Journal"), ("2", "Othername 2016")])
    papers = {"1": paper("1", abstract=ABSTRACT),
              "2": paper("2", authors=["Second CD"], year=2016,
                         abstract="Made-up: 42 of 60 patients; n = 60 again.")}
    before_w, before_papers = w.model_copy(deep=True), copy.deepcopy(papers)
    assert check_answer(w, papers) == Counts(2, 3, 1, 2)
    assert w == before_w and papers == before_papers  # reads only


def test_check_answer_with_nothing_to_count():
    assert check_answer(answer([], []), {}) == Counts(0, 0, 0, 0)


def test_n_and_prose_numbers_are_not_counted():
    """Scope O1 (i): only the outcomes' structured numbers; not a trial's `n`, not text."""
    w = answer([], [("1", "Surname 2019")])
    w.studies[0].n = 99999
    w.bottom_line.text = "Made-up 12345."
    assert check_answer(w, {"1": paper("1")}) == Counts(0, 0, 1, 1)


def test_module_imports_no_client():
    src = Path("src/pubmedgemini/completeness.py").read_text(encoding="utf-8")
    imports = [ln.strip() for ln in src.splitlines()
               if re.match(r"\s*(import|from)\s", ln)]
    assert imports, "no imports read"
    for ln in imports:
        assert "pubmedgemini.gemini" not in ln, ln
        assert "lit.tools" not in ln and "lit.http" not in ln and "lit import" not in ln, ln
        assert "httpx" not in ln and "requests" not in ln and "urllib" not in ln, ln
        assert "socket" not in ln and "subprocess" not in ln, ln


@pytest.mark.parametrize("text", [
    "9" * 200_000,
    "1," * 100_000,
    "1,234," * 33_000 + "5",
    "1.2." * 50_000,
    "0." + "5" * 199_998,
    ("12,345.678 " * 18_000),
    "·" * 200_000,
])
def test_a_long_hostile_abstract_is_quick(text):
    start = time.monotonic()
    nums = abstract_numbers(text)
    found = [number_found(number(v, ci_low=0.5, ci_high=1e300), text)
             for v in (0.5, 12345.678, 1e300, 5.0)]
    assert time.monotonic() - start < 1.0
    assert isinstance(nums, list) and all(isinstance(f, bool) for f in found)


def test_a_long_hostile_label_is_quick():
    label = "a-" * 100_000 + " 2019 " + "9" * 100_000
    p = paper(title="x-" * 100_000, abstract="1-" * 100_000)
    start = time.monotonic()
    assert trial_name_matches(label, p) in (True, False)
    assert time.monotonic() - start < 1.0
