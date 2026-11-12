"""Task 7 (F3): the answer page's derivation, pure functions over a stored answer.

Ported rules follow Incisive (commit 12630f7, `crates/incisive-app/src/answer_page.rs`,
`agent/answer_v3.rs` `arm_lines`); the tests named after Incisive's are ported from it. New
rules (the frozen mock's scales, draw checks and fold) are checked against the mock's own
numbers (`docs/mocks/f3-answer-page-v5.html`; its numbers are v4's).

HERNIA is the hand-written hernia answer (`tests/fixtures/writer/whole.json`), which follows
Incisive's rules; where the rules give rows the mock's example does not show, the mock's own
arithmetic is checked separately (coordinator ruling 1, 12 Nov 2026)."""

import dataclasses
import json
import math
from pathlib import Path

import pytest

from pubmedgemini.answer_page import (
    GROUP,
    HEAD,
    MORE,
    OUT100_BASE,
    ForestRow,
    Item,
    Panel,
    all_groups,
    answer_view,
    chart_kind,
    cite_pieces,
    compare,
    compare_panel,
    diff_axis,
    diff_estimate,
    draw_problem,
    first_sentence,
    fmt_axis,
    fmt_pct,
    fmt_value,
    fold_pair,
    forest,
    forest_panel,
    normalise_comparison,
    normalise_p,
    out100_panel,
    out_of_100,
    out_of_100_count,
    outcome_cards,
    outcomes_panel,
    p_label,
    pieces_text,
    pill_class,
    ratio_axis,
    ratio_estimate,
    refs_of,
    strip_citations,
    study_short,
    with_unit,
)
from pubmedgemini.writer.answer import CERTAINTY_ALL as CERTAINTIES
from pubmedgemini.writer.answer import (
    AnswerV3,
    Applicability,
    BottomLine,
    IntegrityV3,
    Outcome,
    ReferenceV3,
    Study,
    WrittenAnswer,
)
from pubmedgemini.writer.answer import Number as _Number
from pubmedgemini.writer.read import evidence_types, into_answer

# ---------- constructors (F3b port): f3-writer's positional dataclasses, as main's models ----

FIXTURES = Path(__file__).parent / "fixtures" / "writer"


def fixture(name: str) -> dict:
    """The parsed JSON of tests/fixtures/writer/<name>."""
    return json.loads((FIXTURES / name).read_text())


def Number(label, value, unit, arm, ci_low, ci_high, p, pmid) -> _Number:
    return _Number(label=label, value=value, unit=unit, arm=arm, ci_low=ci_low,
                   ci_high=ci_high, p=p, pmid=pmid)


def Reference(n, pmid, title, journal, year, doi, retracted) -> ReferenceV3:
    return ReferenceV3(n=n, pmid=pmid, title=title, journal=journal, year=year, doi=doi,
                       retracted=retracted)


Integrity = IntegrityV3


def AnswerBody(**kw) -> AnswerV3:
    return AnswerV3(question="Example question", **kw)


PMIDS = [f"9000000{i}" for i in range(1, 9)]
DESIGNS = {"90000001": "SR_MA", "90000002": "SR_MA", "90000003": "SR_MA"}


def hernia() -> AnswerBody:
    a, _ = into_answer(fixture("whole.json"))
    evidence_types(a, lambda p: {"SR_MA": "ma"}.get(DESIGNS.get(p, "RCT"), "rct")
                   if p in PMIDS else None)
    refs = [Reference(i, p, f"Title {i}", "Hernia", 2020, None, False)
            for i, p in enumerate(PMIDS, start=1)]
    return AnswerBody(created_at="2026-11-12T00:00:00Z", answer=a, empty_reason=None,
                      references=refs, searches=[], integrity=Integrity(), stopped_early=None,
                      elapsed_s=60, notes=[])


HERNIA = hernia()
HERNIA_REFS = refs_of(HERNIA)
REFS = {"1": 1, "11111": 1, "22222": 2}


def num(label, v, unit=None, arm=None, pmid="1", lo=None, hi=None, p=None) -> Number:
    return Number(label, v, unit, arm, lo, hi, p, pmid)


def ratio(v, lo, hi, pmid="1", unit="RR", label="Seroma") -> Number:
    return num(label, v, unit, None, pmid, lo, hi)


def outcome(name, numbers, certainty="Low", effect="", why="", evidence="") -> Outcome:
    return Outcome(outcome=name, rct_evidence=evidence, effect_text=effect, numbers=numbers,
                   certainty=certainty, why=why, pmids=[])


def study(pmid, comparison, label=None) -> Study:
    return Study(pmid=pmid, label=label or f"Trial {pmid}, 2020", design="RCT", n=None,
                 setting="", comparison=comparison, key_results="", appraisal="")


def written(outcomes, studies=()) -> WrittenAnswer:
    return WrittenAnswer(bottom_line=BottomLine(text="x", certainty="Low", certainty_reason=""),
                         outcomes=list(outcomes), studies=list(studies), conflicts=[],
                         applicability=Applicability(case_mix="", india_cost_context="",
                                                     training=""),
                         would_change=[], at_a_glance=None, clinical_practice=None,
                         notable_findings=[])


def answer_with(*numbers: Number) -> WrittenAnswer:
    return written([outcome("Seroma", list(numbers))])


# ---------- the forest against the frozen mock (Review Focus 3) ----------

def test_forest_positions_match_the_frozen_mock():
    f = forest(HERNIA.answer, HERNIA_REFS)
    assert (f.ratio_axis.lo, f.ratio_axis.hi, fmt_pct(f.ratio_axis.null)) == (0.1, 2, "76.9")
    r = f.ratios[0]  # RR 0.38 (0.28–0.51)
    assert (fmt_pct(r.left), fmt_pct(r.width), fmt_pct(r.est)) == ("34.4", "20", "44.6")
    assert [fmt_pct(x) for x in (f.ratios[1].left, f.ratios[1].width, f.ratios[1].est)] == \
        ["13.5", "56.3", "42.8"]
    # RR 1.03: 100 * log10(10.3) / log10(20) = 77.85, so 77.8 (the mock, drawn by hand,
    # shows 77.9; every other position of the mock is matched to one decimal).
    assert [fmt_pct(x) for x in (f.ratios[2].left, f.ratios[2].width, f.ratios[2].est)] == \
        ["60.9", "34.1", "77.8"]
    assert [fmt_pct(x) for x in (f.ratios[3].left, f.ratios[3].width, f.ratios[3].est)] == \
        ["37.8", "29.5", "52.4"]
    d = f.diffs[0]  # MD −4.74 (−6.78 to −2.70) days
    assert (d.scale.lo, d.scale.hi, fmt_pct(d.null), fmt_pct(d.left), fmt_pct(d.width),
            fmt_pct(d.est)) == (-8, 2, "80", "12.2", "40.8", "32.6")
    assert (d.scale.lo_text, d.scale.mid_text, d.scale.hi_text) == ("−8", "0", "+2")
    assert (f.ratio_axis.lo_text, f.ratio_axis.mid_text, f.ratio_axis.hi_text) == \
        ("0.1", "1", "2")
    assert r.aria == "Risk ratio 0.38, interval 0.28 to 0.51, below 1"
    assert f.ratios[2].aria.endswith("crossing 1")
    assert d.aria == ("Mean difference minus 4.74 days, interval minus 6.78 to minus 2.7, "
                      "below 0")
    assert r.estimate_text == "RR 0.38 (0.28–0.51)" and r.label == \
        "Chronic pain, TEP vs Lichtenstein"
    assert d.estimate_text == "−4.74 days (−6.78 to −2.7)"
    assert r.sub == "TEP vs Lichtenstein meta-analysis [1]" and d.sub == \
        "TEP vs Lichtenstein meta-analysis [1] · days"
    assert all(x.drawn for x in f.ratios + f.diffs) and len(f.ratios) == 4 and len(f.diffs) == 1


def test_the_mocks_positions_on_its_scales():
    # The mock's own scale arithmetic, independent of any fixture (coordinator ruling 1).
    ax = ratio_axis([(0.28, 0.51), (0.15, 0.81), (0.62, 1.72), (0.31, 0.75)])
    assert ax == (0.1, 2)
    lo, hi, _ = diff_axis(-6.78, -2.70)
    assert (lo, hi) == (-8, 2)
    assert fmt_pct(100 * (0 - lo) / (hi - lo)) == "80"
    assert fmt_pct(100 * math.log10(1 / 0.1) / math.log10(2 / 0.1)) == "76.9"


@pytest.mark.parametrize("n,problem", [
    (ratio(0, 0.1, 2), "a ratio must be above 0"),
    (ratio(0.5, 0.8, 0.2), "the interval is reversed"),
    (ratio(3.0, 0.5, 2.0), "the estimate is outside its interval"),
    (ratio(float("nan"), 0.1, 1), "a value is not a finite number"),
    (ratio(float("inf"), 0.1, 1), "a value is not a finite number"),
    (ratio(-0.4, -0.9, 0.1), "a ratio must be above 0"),
    (ratio(0.5, 0.2, 0.9, pmid="99999999"), "its study is not in the references")])
def test_draw_checks_refuse_and_say_why(n, problem):  # Review Focus 2
    row = forest(answer_with(n), REFS).ratios[0]
    assert not row.drawn and row.problem == problem
    assert forest(answer_with(n), REFS).ratio_axis is None  # nothing drawn, no axis


def test_draw_problem_order_and_kinds():
    assert draw_problem(ratio(0.5, 0.2, 0.9, pmid=" 1 "), "ratio", REFS) is None
    # An interval of no width is listed (fourth check F4; it was drawn).
    assert draw_problem(num("d", 0.0, lo=0.0, hi=0.0), "diff", REFS) == \
        "the interval has no width"
    assert draw_problem(num("d", 0.0, lo=-1.0, hi=1.0), "diff", REFS) is None
    assert draw_problem(num("d", 1.0, lo=2.0, hi=0.5), "diff", REFS) == \
        "the interval is reversed"
    assert draw_problem(num("p", 140.0, "%", "a"), "percent", REFS) == \
        "a percentage must be from 0 to 100"
    assert draw_problem(num("p", -3.0, "%", "a"), "percent", REFS) == \
        "a percentage must be from 0 to 100"
    assert draw_problem(num("p", 100.0, "%", "a"), "percent", REFS) is None
    assert draw_problem(num("p", 3.0, "%", "a", pmid="5"), "percent", REFS) == \
        "its study is not in the references"


def test_a_row_not_drawn_is_listed_as_text_with_its_reason():
    f = forest(answer_with(ratio(0.5, 0.8, 0.2), ratio(0.4, 0.2, 0.9, label="Pain")), REFS)
    assert [x.drawn for x in f.ratios] == [False, True]
    assert f.ratios[0].estimate_text == "RR 0.5 (0.8–0.2)"
    assert (f.ratio_axis.lo, f.ratio_axis.hi) == (0.2, 2)  # only the drawn row sets it


def test_results_without_an_interval_get_no_line():
    f = forest(answer_with(num("Seroma", 0.4, "RR")), REFS)
    assert f.ratios == [] and f.diffs == [] and f.ratio_axis is None


def test_absurd_interval_is_clamped_to_the_scale_and_says_so():
    f = forest(answer_with(ratio(1.2, 0.0001, 9000)), REFS)
    r = f.ratios[0]
    assert (f.ratio_axis.lo, f.ratio_axis.hi) == (0.05, 50) and r.drawn
    assert (r.left, r.width) == (0, 100)
    assert r.aria.endswith(", interval runs past the scale")
    assert r.sub.endswith(" · interval runs past the scale")


def test_difference_axis_never_has_zero_at_an_edge():
    assert diff_axis(0.0, 3.1)[:2] == (-1, 4) and diff_axis(-0.4, 0.0)[:2] == (-0.4, 0.1)
    assert diff_axis(0, 0)[:2] == (-1, 1)
    assert diff_axis(-0.3, 0.0)[:2] == (-0.3, 0.1)  # -0.3 / 0.1 is -2.9999999999999996
    assert diff_axis(2.0, 2.5)[:2] == (-0.5, 2.5)
    assert diff_axis(-120, 80)[:2] == (-150, 100)


def test_ratio_axis_ladder_and_limits():
    assert ratio_axis([(0.9, 1.0)]) == (0.5, 2) and ratio_axis([(0.01, 900)]) == (0.05, 50)
    assert ratio_axis([(1.2, 3.0)]) == (0.5, 5) and ratio_axis([(0.2, 0.5)]) == (0.2, 2)


def test_a_difference_row_has_its_own_scale_and_aria():
    f = forest(answer_with(num("LOS", 0.33, "days", "MD", lo=-0.03, hi=0.7)), REFS)
    d = f.diffs[0]
    assert d.drawn and (d.scale.lo, d.scale.hi) == (-0.2, 0.8)
    assert d.aria == "Mean difference 0.33 days, interval minus 0.03 to 0.7, crossing 0"
    f = forest(answer_with(num("Difference in pain", 2.0, None, None, lo=1.0, hi=3.0)), REFS)
    assert f.diffs[0].aria == "Difference 2, interval 1 to 3, above 0"


def test_ratio_kinds_name_their_aria():
    f = forest(answer_with(ratio(2.0, 1.5, 3.0, unit="HR"), ratio(0.5, 0.3, 0.8, unit="OR",
                                                                       label="x")), REFS)
    assert f.ratios[0].aria == "Hazard ratio 2, interval 1.5 to 3, above 1"
    assert f.ratios[1].aria.startswith("Odds ratio 0.5, ")


# ---------- out of 100 ----------

def test_out_of_100_counts_match_the_frozen_mock():
    items = {it.label: it for it in out_of_100(HERNIA.answer, HERNIA_REFS)}
    assert [c for c, _, _ in items["Chronic pain at 5 years"].arms] == [9, 19]
    assert [c for c, _, _ in items["Moderate or severe pain at 5 years"].arms] == [2, 4]
    assert pieces_text(items["Chronic pain at 5 years"].sentence_pieces) == (
        "Chronic pain at 5 years: 9.4% with TEP vs 18.8% with Lichtenstein [4]. Certainty for "
        "this outcome: Moderate.")
    assert [(a, s) for _, a, s in items["Chronic pain at 5 years"].arms] == [
        ("TEP", "A"), ("Lichtenstein", "B")]
    assert pill_class(items["Moderate or severe pain at 5 years"].certainty) == "lo"


def test_out_of_100_follows_incisives_rule_for_every_percent_pair():
    # Incisive's rule takes every two-arm percent group, so the fixture gives five items
    # (the mock's example shows two of them), in outcome order.
    labels = [it.label for it in out_of_100(HERNIA.answer, HERNIA_REFS)]
    assert labels == ["Chronic pain at 5 years", "Non-ignorable pain at about 8 years",
                      "Chronic pain at 52 months", "Moderate or severe pain at 5 years",
                      "Recurrence at 5 years"]


def test_out_of_100_rounds_half_up_as_incisive():  # Review Focus 4
    assert [out_of_100_count(v) for v in (0.37, 2.5, 3.5, 43.6, 120, -3, float("nan"),
                                          0.5, 99.5, float("inf"))] == \
        [0, 3, 4, 44, 100, 0, 0, 1, 100, 0]


def test_out_of_100_needs_percentages_in_range():
    w = written([outcome("Pain", [num("Pain", 140.0, "%", "TEP"), num("Pain", 20.0, "%", "L")])],
                [study("1", "TEP vs L")])
    assert out_of_100(w, REFS) == []
    rows = compare(w, REFS)
    assert [r.values_text for g in rows for r in g.rows] == ["TEP 140% vs L 20%"]


def test_three_arm_groups_stay_in_compare():  # X20
    w = written([outcome("Pain", [num("Pain", 1.0, "%", "a"), num("Pain", 2.0, "%", "b"),
                                  num("Pain", 3.0, "%", "c")])])
    assert out_of_100(w, REFS) == []
    assert [r.values_text for g in compare(w, REFS) for r in g.rows] == ["a 1% vs b 2% vs c 3%"]


def test_out_of_100_takes_two_arm_percent_groups_only():  # Incisive's test, two arms (X20)
    p = lambda v, arm: num("Relief", v, "%", arm)
    w = written([
        outcome("Two arms", [p(44.0, "POEM"), p(30.0, "LHM")]),
        outcome("Three arms", [p(1.0, "a"), p(2.0, "b"), p(3.0, "c")]),
        outcome("Mixed units", [p(44.0, "POEM"), num("Relief", 30.0, "patients", "LHM")]),
        outcome("One arm", [p(44.0, "POEM")]),
        outcome("With a difference", [p(44.0, "POEM"), p(30.0, "LHM"),
                                      p(14.0, "risk difference")]),
    ], [study("1", "POEM vs LHM")])  # squares need the study's two sides (fourth check F1)
    assert [it.label for it in out_of_100(w, REFS)] == ["Relief"]


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_a_pair_with_an_unnamed_arm_is_not_drawn_out_of_100(blank):  # owner decision d23
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP"), num("Pain", 18.8, "%", blank)])],
                [study("1", "TEP vs Lichtenstein")])
    assert out_of_100(w, REFS) == []
    assert compare(w, REFS) == []
    [c] = outcome_cards(w, REFS)
    assert [(x.at, x.problem) for x in c.listed] == [((0, 0), "the other arm is not named"),
                                                     ((0, 1), "arm not named")]
    assert shown_at(w, REFS) == every_number(w)


def test_a_pair_with_both_arms_named_is_drawn_out_of_100():
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP"),
                                  num("Pain", 18.8, "%", "Lichtenstein")])],
                [study("1", "TEP vs Lichtenstein")])
    [it] = out_of_100(w, REFS)
    assert it.arms == [(9, "TEP", "A"), (19, "Lichtenstein", "B")]


# ---------- compare the arms ----------

def test_compare_groups_by_normalised_comparison():
    pair = lambda pmid: [num("x", 1.0, None, "a", pmid), num("x", 2.0, None, "b", pmid)]
    w = written([outcome("A", pair("11111")), outcome("B", pair("22222")),
                 outcome("C", pair("33333"))],
                [study("11111", "TEP vs Lichtenstein (open)"),
                 study("22222", "tep vs  lichtenstein")])
    refs = REFS | {"33333": 3}  # a reference with no trials-table row: "Other" (L-F)
    g = compare(w, refs)
    assert [x.label for x in g] == ["TEP vs Lichtenstein (open)", "Other"]
    assert [len(x.rows) for x in g] == [2, 1]
    assert pieces_text(g[0].rows[0].study_pieces) == "Trial 11111 [1]"
    assert pieces_text(g[1].rows[0].study_pieces) == "PMID 33333 [3]"


def test_compare_rows_against_the_mock():
    g = compare(HERNIA.answer, HERNIA_REFS)
    assert g[0].label == "TEP vs Lichtenstein"
    r = g[0].rows[0]
    assert (r.label, r.certainty, r.values_text, r.p_text) == (
        "Chronic pain at 5 years", "Moderate", "TEP 9.4% vs Lichtenstein 18.8%", "p <0.001")
    assert pieces_text(r.study_pieces) == "Swedish trial [4]"
    assert [x.label for x in g] == ["TEP vs Lichtenstein", "TAPP vs Lichtenstein"]


def test_comparisons_normalise_as_incisive():
    assert normalise_comparison("  Robotic TAPP (Xi)  vs   Laparoscopic TAPP ") == \
        "robotic tapp vs laparoscopic tapp"


# ---------- ported from Incisive ----------

def test_chart_kinds_follow_the_shape_of_the_numbers():
    rr = num("Seroma", 0.4, "RR", None, lo=0.08, hi=2.03)
    rr2 = num("Seroma", 0.6, "OR", "x", lo=0.1, hi=3.0)
    assert chart_kind([rr, rr2]) == "ratio"
    assert chart_kind([num("Seroma", 0.4, "HR")]) == "text"
    assert chart_kind([num("LOS", 0.33, "days", "MD", lo=-0.03, hi=0.7)]) == "diff"
    assert chart_kind([num("Mean difference in pain", -1.2, lo=-2.0, hi=-0.4)]) == "diff"
    # Incisive draws "Robotic minus lap" as a difference; here a difference must name its
    # measure (third check M2, plan X33, an accepted difference): it is text.
    assert chart_kind([num("Robotic minus lap", 4.0, "min", lo=1.0, hi=7.0)]) == "text"
    assert chart_kind([num("Amd score", 4.0, lo=1.0, hi=7.0)]) == "text"
    a = num("Discharge", 95.9, "%", "R-TAPP")
    b = num("Discharge", 81.5, "%", "L-TAPP")
    assert chart_kind([a, b]) == "pair"
    assert chart_kind([a, b, num("Discharge", 80.0, "%", "R-TAPP")]) == "text"
    assert chart_kind([num("Pain", 2.0), num("Pain", 3.0)]) == "text"
    assert chart_kind([a, num("Discharge", 1.2, "RR", "pooled")]) == "text"
    assert chart_kind([a, num("Discharge", 14.4, "%", "difference")]) == "text"
    assert chart_kind([a, b, num("Discharge", 14.4, "%", "MD")]) == "text"
    assert chart_kind([a]) == "text" and chart_kind([]) == "text"


def test_numbers_group_by_label_and_study_intervention_first():
    w = written([outcome("Pain", [
        num("VAS 24 h", 3.0, None, "laparoscopic TEP", "11111"),
        num("VAS 24 h", 2.0, None, "robotic TAPP", "11111"),
        num("VAS 24 h", 4.0, None, "robotic TAPP", "22222"),
        num(" VAS 24 h ", 5.0, None, "laparoscopic TEP", "22222 "),
        num("VAS 7 d", 1.0, None, None, "11111"),
    ])], [study("11111", "Robotic TAPP vs laparoscopic TEP")])
    g = all_groups(w, REFS)
    assert len(g) == 3
    assert (g[0].label, g[0].pmid) == ("VAS 24 h", "11111")
    assert g[0].numbers == [1, 0] and g[0].kind == "pair"  # robotic first by the comparison
    assert g[1].numbers == [2, 3]  # no study row: the writer's order
    assert g[2].kind == "text"


def test_p_is_normalised_for_display():
    assert normalise_p("P < .001") == "< .001"
    assert normalise_p("p=0.03") == "0.03"
    assert normalise_p(" P = 0.431 ") == "0.431"
    assert normalise_p("<0.001") == "<0.001"
    assert normalise_p("0.012") == "0.012"
    assert p_label("P < .001") == "p < .001"
    assert p_label("p") is None
    assert normalise_p("P<.001") == "<.001"
    assert normalise_p("p.03") == ".03"
    assert normalise_p("p ≤ 0.05") == "≤ 0.05"
    assert normalise_p("pooled 0.03") == "pooled 0.03"
    assert normalise_p("p-value 0.03") == "p-value 0.03"
    assert normalise_p("Pinteraction 0.2") == "Pinteraction 0.2"
    assert p_label("p-value 0.03") == "p p-value 0.03"
    assert p_label("  ") is None and p_label(None) is None


def test_values_print_as_stored():
    assert fmt_value(0.431) == "0.431"
    assert fmt_value(-0.03) == "−0.03"
    assert fmt_value(3258.0) == "3,258" and fmt_value(7658) == "7,658"
    assert fmt_value(1234567.0) == "1,234,567"
    assert fmt_value(80.0) == "80" and fmt_value(-4.74) == "−4.74"
    assert fmt_value(0.00001) == "0.00001" and fmt_value(1e16) == "10,000,000,000,000,000"
    assert with_unit(44.0, "%") == "44%" and with_unit(0.4, "RR") == "RR 0.4"
    assert with_unit(80.3, "min") == "80.3 min" and with_unit(3.0, " ") == "3"
    n = num("LOS", 0.33, "days", "MD", lo=-0.03, hi=0.7)
    assert diff_estimate(n) == "0.33 days (−0.03 to 0.7)"
    assert ratio_estimate(num("Seroma", 0.4, "RR", lo=0.08, hi=2.03)) == "RR 0.4 (0.08–2.03)"
    assert study_short("ROGER (2025), Br J Surg") == "ROGER (2025)"
    assert fmt_axis(0.805) == "0.81" and fmt_axis(-8) == "−8" and fmt_axis(0.1) == "0.1"


def test_first_sentences_drop_citations():
    assert first_sentence("No difference in pain [12345678; 23456789]. Costs were higher [1].") \
        == "No difference in pain."
    assert first_sentence("p 0.03 favoured robotic") == "p 0.03 favoured robotic"
    assert strip_citations("Primary unilateral [42927985 as 10] hernias .") == \
        "Primary unilateral hernias."


# ---------- citations, pills, outcome cards ----------

def test_unknown_citation_is_flagged_not_dropped():
    assert cite_pieces("Less pain [33763744; 11111111].", {"33763744": 1}) == \
        [("text", "Less pain "), ("cite", [1]), ("unknown", 1), ("text", ".")]
    assert cite_pieces("x [90000004; 90000001, 90000004]", HERNIA_REFS) == \
        [("text", "x "), ("cite", [1, 4])]
    assert cite_pieces("LOS 2 [1, 3] days", {}) == [("text", "LOS 2 [1, 3] days")]
    assert pieces_text(cite_pieces("a [99999999] b", {})) == "a [not in the study list] b"


def test_pills():
    assert [pill_class(c) for c in CERTAINTIES] == ["hi", "mo", "lo", "vl"]


def test_outcome_cards_follow_the_mock():
    cards = outcome_cards(HERNIA.answer, HERNIA_REFS)
    c = cards[0]
    assert (c.title, c.certainty, c.pill, c.evidence_label, c.n_numbers) == (
        "Chronic postoperative groin pain", "Moderate", "mo", "RCTs and meta-analyses",
        "9 numbers")
    assert c.first_sentence == ("Laparoscopic repair roughly halves to thirds the risk of any "
                                "chronic groin pain compared with Lichtenstein.")
    assert pieces_text(c.evidence_pieces) == "Two meta-analyses [1, 2] and three RCTs [4, 7, 8]"
    assert pieces_text(c.why_pieces).startswith("Consistent direction")
    assert cards[3].n_numbers == "0 numbers" and cards[4].n_numbers == "1 number"
    assert cards[3].evidence_label == "RCTs"


# ---------- the fold (X14) ----------

def test_fold_matches_the_frozen_mock():  # Review Focus 3
    assert fold_pair(forest_panel(HERNIA), outcomes_panel(HERNIA)) == (5, 4)
    # The mock's counselling pair: its two out-of-100 items beside its four compare rows in
    # two comparisons (the hernia's rows without "Moderate or severe pain"); no fold.
    out, cmp = out100_panel(HERNIA), compare_panel(HERNIA)
    mock_out = Panel(out.head, [i for i in out.items if i.payload.label in (
        "Chronic pain at 5 years", "Moderate or severe pain at 5 years")])
    mock_cmp = Panel(cmp.head, [i for i in cmp.items
                                if i.payload.label != "Moderate or severe pain at 5 years"])
    assert (len(mock_out.items), len(mock_cmp.items)) == (2, 4)
    assert fold_pair(mock_out, mock_cmp) == (2, 4)


def test_fold_arithmetic_is_the_rules():  # third check L4b: pixels at 1,000 px (Estimate)
    f, o = forest_panel(HERNIA), outcomes_panel(HERNIA)
    # forest: head, two group headings, three one-line rows and two two-line rows (the
    # label and estimate need two lines: 56 and 70 characters)
    assert [i.weight for i in f.items] == [101, 101, 101, 125, 125]
    assert f.total() == HEAD["forest"] + 2 * GROUP + 3 * 101 + 2 * 125
    assert [i.weight for i in o.items] == [147] * 6  # each first sentence takes two lines
    # outcomes: 65 + 39 + 147k <= 737 keeps 4: "Show 2 more outcomes", the mock's fold
    assert fold_pair(f, o) == (5, 4)
    out, cmp = out100_panel(HERNIA), compare_panel(HERNIA)
    assert [i.weight for i in cmp.items] == [101] * 5
    assert cmp.total() == HEAD["compare"] + 2 * GROUP + 5 * 101
    k = fold_pair(out, cmp)[0]
    assert out.total(k) + MORE <= cmp.total() < out.total(k + 1) + MORE


def test_lines_wrap_words_and_break_long_ones():
    from pubmedgemini.answer_page import text_lines

    assert text_lines("", 10) == 1 and text_lines("abc", 10) == 1
    assert text_lines("aaaa bbbb", 9) == 1 and text_lines("aaaa bbbbb", 9) == 2
    assert text_lines("x" * 300, 55) == 6  # broken anywhere, as the v5 wrap rule
    assert text_lines("ab " + "x" * 20, 10) == 3  # a long word starts a new line


def _row(label="Seroma", drawn=True, problem=None, sub="Trial [1]"):
    return ForestRow(label, "RR 0.5 (0.2–0.9)", [("text", sub)], drawn, problem)


def test_item_weights_follow_their_lines():  # third check L4b
    from pubmedgemini.answer_page import CompareRow, OutcomeCard, OutItem, item_weight

    assert item_weight("forest", _row()) == 101
    assert item_weight("forest", _row(drawn=False, problem="the interval is reversed")) == 84
    assert item_weight("forest", _row(label="x" * 300)) == 101 + 5 * 24
    long_reason = _row(drawn=False, problem="its arm text reads as a comparison, not an arm " * 2)
    assert item_weight("forest", long_reason) == 84 + 21
    row = CompareRow("Pain", "Low", "TEP 9.4% vs Lichtenstein 18.8%", [("text", "Trial [1]")],
                     None)
    assert item_weight("compare", row) == 101
    assert item_weight("compare", dataclasses.replace(row, problem="both arms round to 0 of 100")
                       ) == 101 + 27
    assert item_weight("compare", dataclasses.replace(row, values_text="y" * 120)) == 101 + 2 * 20
    card = OutcomeCard("Pain", "Low", "lo", "RCTs", "2 numbers", "One line.", [], [], [])
    assert item_weight("outcomes", card) == 126
    assert item_weight("outcomes", dataclasses.replace(card, first_sentence="w " * 40)) == 147
    item = OutItem("Pain", [("text", "Pain: 9.4% with TEP vs 18.8% with L [1].")], "Low",
                   [(9, "TEP", "A"), (19, "L", "B")])
    assert item_weight("out100", item) == OUT100_BASE + 24
    longer = dataclasses.replace(item, arms=[(9, "z" * 60, "A"), (19, "L", "B")])
    assert item_weight("out100", longer) == OUT100_BASE + 24 + 3 * 21


def test_a_long_group_heading_weighs_its_lines():
    from pubmedgemini.answer_page import heading_weight

    assert heading_weight("TEP vs Lichtenstein") == GROUP
    assert heading_weight("Robotic transabdominal preperitoneal repair vs laparoscopic") == \
        GROUP + 17
    w = written([outcome("P", [num("P", 1.0, "%", "a"), num("P", 2.0, "%", "b")])],
                [study("1", "x" * 100)])
    [i] = compare_panel(_body(w)).items
    assert i.heading_weight == GROUP + 2 * 17


def test_the_fold_counts_its_show_more_line():
    left = Panel(100, [Item(100)] * 5)  # 600
    right = Panel(100, [Item(100)] * 10)
    # 100 + 100k + MORE <= 600 keeps 4 (539), not 5 (639)
    assert fold_pair(left, right) == (5, 4)


def test_many_not_drawn_lines_weigh_what_they_take():  # the reviewer's 614 px case
    b = hernia()
    for k, o in enumerate(b.answer.outcomes):
        for i in range(6):
            o.numbers += [num(f"Wound {k}.{i}", 12.0, "%", "TEP", PMIDS[i % 8], 13.0, 20.0),
                          num(f"Wound {k}.{i}", 15.0, "%", "Lichtenstein", PMIDS[i % 8])]
    cmp = compare_panel(b)
    assert {i.weight for i in cmp.items if i.payload.problem} == {128}



def test_fold_keeps_at_least_two_and_folds_the_left_too():
    left = Panel(70, [Item(100, "Ratios (RR · OR · HR)", 37)] + [Item(100)] * 11)
    right = Panel(30, [Item(130)] * 2)
    assert fold_pair(left, right) == (2, 2)
    right = Panel(30, [Item(130)] * 4)  # 550: 70 + 37 + 100k + MORE <= 550 -> k = 4
    assert fold_pair(left, right) == (4, 4)


def test_fold_with_an_empty_panel_or_equal_totals_is_none():
    assert fold_pair(Panel(7, []), Panel(3, [Item(13)] * 9)) == (0, 9)
    assert fold_pair(Panel(3, [Item(13)] * 9), Panel(6, [])) == (9, 0)
    assert fold_pair(Panel(3, [Item(13)] * 2), Panel(3, [Item(13)] * 2)) == (2, 2)


def test_fold_puts_a_group_heading_inside_only_when_the_group_starts_there():
    left = Panel(70, [Item(100, "Ratios", 37), Item(100), Item(100),
                      Item(100, "Differences", 37), Item(100)])
    right = Panel(30, [Item(150)] * 3)  # 480: 70 + 37 + 300 + MORE = 446 -> 3 shown
    assert fold_pair(left, right) == (3, 3)
    shown, hidden = left.split(3)
    assert [i.heading for i in shown] == ["Ratios", None, None]
    assert [i.heading for i in hidden] == ["Differences", None]
    shown, hidden = left.split(2)
    assert [i.heading for i in hidden] == [None, "Differences", None]  # no repeat of Ratios


def test_fold_is_deterministic():
    assert fold_pair(forest_panel(HERNIA), outcomes_panel(HERNIA)) == \
        fold_pair(forest_panel(hernia()), outcomes_panel(hernia()))


# ---------- every number is on the page (review M3) ----------

def shown_at(w, refs) -> set[tuple[int, int]]:
    """Every (outcome, number) the page shows: forest rows, compare rows, listed numbers."""
    f = forest(w, refs)
    at = {r.at for r in f.ratios + f.diffs}
    at |= {x for g in compare(w, refs) for r in g.rows for x in r.at}
    at |= {x.at for c in outcome_cards(w, refs) for x in c.listed}
    return at


def every_number(w) -> set[tuple[int, int]]:
    return {(i, j) for i, o in enumerate(w.outcomes) for j in range(len(o.numbers))}


@pytest.mark.parametrize("n,problem", [
    (num("Seroma", 0.4, "RR"), "no interval"),
    (num("Seroma", 0.4, "aHR", lo=0.2, hi=0.8), "unit not RR, OR or HR"),
    (num("Seroma", 0.4, "rr", lo=0.2, hi=0.8), "unit not RR, OR or HR"),
    (num("Seroma RR", 0.4, None, lo=0.2, hi=0.8), "unit not RR, OR or HR"),
    (num("Hospital stay", -4.7, "days", lo=-6.8, hi=-2.7),
     "not marked as a ratio or a difference"),
    (num("Seroma", 9.4, "%", "TEP"), "single arm"),
    (num("Mean difference in stay", -1.0, "days"), "no interval"),
])
def test_a_number_drawn_nowhere_is_listed_in_its_outcome(n, problem):
    w = answer_with(n)
    [c] = outcome_cards(w, REFS)
    assert [(x.problem, x.at) for x in c.listed] == [(problem, (0, 0))]
    assert shown_at(w, REFS) == every_number(w)


def test_a_group_mixing_kinds_lists_every_number_not_drawn():
    w = answer_with(num("Pain", 9.4, "%", "TEP"), num("Pain", 0.5, "RR", "L"))
    [c] = outcome_cards(w, REFS)
    assert [(x.at, x.problem) for x in c.listed] == [((0, 0), "the group mixes kinds"),
                                                     ((0, 1), "no interval")]
    assert shown_at(w, REFS) == every_number(w)


def test_a_listed_number_reads_as_text():
    w = answer_with(num("Seroma", 0.4, "aHR", "TEP", lo=0.2, p="0.03"))
    [x] = outcome_cards(w, REFS)[0].listed
    assert (x.label, x.value_text) == ("Seroma", "0.4 aHR (0.2 to not given)")
    assert pieces_text(x.sub_pieces) == "PMID 1 [1] · TEP · p 0.03"
    assert x.problem == "unit not RR, OR or HR"


def test_numbers_drawn_elsewhere_are_not_listed_again():
    w = answer_with(ratio(0.4, 0.2, 0.8), num("Pain", 9.4, "%", "TEP"),
                    num("Pain", 18.8, "%", "L"))
    assert outcome_cards(w, REFS)[0].listed == []
    assert shown_at(w, REFS) == every_number(w)


def test_every_hernia_number_is_on_the_page_and_the_counts_match():
    w = HERNIA.answer
    assert shown_at(w, HERNIA_REFS) == every_number(w)
    for i, c in enumerate(outcome_cards(w, HERNIA_REFS)):
        on_page = {j for o, j in shown_at(w, HERNIA_REFS) if o == i}
        assert c.n_numbers.split()[0] == str(len(on_page)), c.title


# ---------- numbers no scale can hold (review M5) ----------

def md(v, lo, hi, label="Mean difference in stay") -> Number:
    return num(label, v, "days", None, "1", lo, hi)


@pytest.mark.parametrize("n,problem", [
    (md(0.0, -1e308, 1e308), "the interval is too wide to draw"),
    (md(1e308, -1.7e308, 1.7e308), "the interval is too wide to draw"),
    (md(5e-13, 0.0, 1e-12), "the interval is too narrow to draw"),
    (md(1e-10, 1e-10, 3e-10), "the interval is too narrow to draw"),
])
def test_a_difference_no_scale_can_hold_is_listed_not_drawn(n, problem):
    [row] = forest(answer_with(n), REFS).diffs
    assert (row.drawn, row.problem) == (False, problem)


@pytest.mark.parametrize("n", [ratio(1e-300, 1e-300, 1e-299), ratio(1e300, 1e299, 1e300),
                               ratio(5e-324, 5e-324, 1e308), ratio(1.0, 1e-300, 1e300)])
def test_extreme_ratios_never_raise_and_stay_on_the_scale(n):
    [row] = forest(answer_with(n), REFS).ratios
    assert row.drawn and row.sub.endswith("interval runs past the scale")
    assert all(math.isfinite(x) and 0 <= x <= 100 for x in (row.left, row.width, row.est))


def test_a_row_whose_derivation_raises_is_listed_and_logged(monkeypatch, caplog):
    import pubmedgemini.answer_page as ap

    real = ap._pos

    def boom(kind, x, scale):
        if kind == "diff":
            raise ZeroDivisionError("x")
        return real(kind, x, scale)

    monkeypatch.setattr(ap, "_pos", boom)
    caplog.set_level("WARNING", logger="pubmedgemini.answer_page")
    f = forest(answer_with(md(-1.0, -2.0, 0.5), ratio(0.5, 0.2, 0.9)), REFS)
    assert [(r.drawn, r.problem) for r in f.diffs] == [
        (False, "the numbers could not be placed on a scale")]
    assert [r.drawn for r in f.ratios] == [True]
    assert [m.getMessage() for m in caplog.records] == [
        "answer_page kind=row_not_derived rows=1"]


# ---------- out of 100: both arms round to 0 (review L7) ----------

def pct_pair(a, b, label="Mesh infection"):
    # "Lichtenstein", not "L": a one-letter arm matches no side of a comparison (Incisive's
    # `words` drop one-byte words), so it never gets squares (fourth check F1).
    return written([outcome(label, [num(label, a, "%", "TEP"),
                                    num(label, b, "%", "Lichtenstein")])],
                   [study("1", "TEP vs Lichtenstein")])


def test_both_arms_rounding_to_0_of_100_are_not_drawn_and_say_why():
    w = pct_pair(0.4, 0.2)
    assert out_of_100(w, REFS) == []
    [r] = [r for g in compare(w, REFS) for r in g.rows]
    assert (r.values_text, r.problem) == ("TEP 0.4% vs Lichtenstein 0.2%",
                                          "both arms round to 0 of 100")


def test_one_arm_above_0_of_100_is_still_drawn_with_incisives_rounding():
    [it] = out_of_100(pct_pair(0.4, 0.6), REFS)
    assert [c for c, _, _ in it.arms] == [0, 1]
    assert [r.problem for g in compare(pct_pair(0.4, 0.6), REFS) for r in g.rows] == [None]


# ---------- out of 100: a percentage's own interval (review L8) ----------

@pytest.mark.parametrize("lo,hi,problem", [
    (12.0, 8.0, "the interval is reversed"),
    (11.0, 14.0, "the estimate is outside its interval"),
    (None, 9.0, "the estimate is outside its interval"),
    (11.0, None, "the estimate is outside its interval"),
    (float("inf"), None, "a value is not a finite number"),
    (8.0, 12.0, None), (None, 12.0, None), (None, None, None),
])
def test_a_percentage_is_checked_against_its_own_interval(lo, hi, problem):
    assert draw_problem(num("Pain", 10.0, "%", "TEP", lo=lo, hi=hi), "percent", REFS) == problem


def test_a_pair_dropped_from_out_of_100_shows_its_reason_in_compare():
    w = written([outcome("Pain", [num("Pain", 140.0, "%", "TEP"), num("Pain", 20.0, "%", "L")]),
                 outcome("Ache", [num("Ache", 10.0, "%", "TEP", lo=11.0, hi=14.0),
                                  num("Ache", 20.0, "%", "L")])],
                [study("1", "TEP vs L")])
    assert out_of_100(w, REFS) == []
    assert [(r.label, r.problem) for g in compare(w, REFS) for r in g.rows] == [
        ("Pain", "a percentage must be from 0 to 100"),
        ("Ache", "the estimate is outside its interval")]


# ---------- compare the arms: each value with its own arm's name (review H1, d23) ----------

def test_each_value_is_shown_with_its_own_arm_name():  # the reviewer's case
    w = written([outcome("Pain", [num("Pain", 18.8, "%", "Open repair"),
                                  num("Pain", 9.4, "%", "Laparoscopic repair")])],
                [study("1", "TEP vs Lichtenstein")])
    [r] = [r for g in compare(w, REFS) for r in g.rows]
    assert r.values_text == "Open repair 18.8% vs Laparoscopic repair 9.4%"


def test_rows_under_other_carry_arm_names_too():
    w = written([outcome("Pain", [num("Pain", 2.0, None, "a", "33333"),
                                  num("Pain", 1.0, None, "b", "33333")])])
    [g] = compare(w, REFS | {"33333": 3})
    assert (g.label, g.rows[0].values_text) == ("Other", "a 2 vs b 1")


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_a_value_without_an_arm_name_is_listed_not_paired(blank):
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP"), num("Pain", 18.8, "%", blank)])],
                [study("1", "TEP vs Lichtenstein")])
    assert compare(w, REFS) == []
    [c] = outcome_cards(w, REFS)
    assert [(x.at, x.problem) for x in c.listed] == [((0, 0), "the other arm is not named"),
                                                     ((0, 1), "arm not named")]
    assert shown_at(w, REFS) == every_number(w)


# ---------- a per-arm rate is never a difference (review M2, owner decision d24) ----------

ONE_ARM = "one arm's rate, not a difference"


def test_an_armed_rate_labelled_difference_is_not_in_the_ci_card():
    w = answer_with(num("Recurrence (no significant difference)", 2.1, "%", "TEP", lo=1.0,
                        hi=3.5))
    f = forest(w, REFS)
    assert f.diffs == [] and f.ratios == []
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [((0, 0), ONE_ARM)]
    assert shown_at(w, REFS) == every_number(w)


def test_a_two_arm_pair_labelled_difference_is_a_pair_not_two_differences():
    label = "Pain, difference not significant"
    w = written([outcome("Pain", [num(label, 9.4, "%", "TEP", lo=7.0, hi=12.0),
                                  num(label, 18.8, "%", "Lichtenstein", lo=15.0, hi=22.0)])],
                [study("1", "TEP vs Lichtenstein")])
    f = forest(w, REFS)
    assert f.diffs == [] and f.ratios == []
    assert [g.kind for g in all_groups(w, REFS)] == ["pair"]
    assert [r.values_text for g in compare(w, REFS) for r in g.rows] == [
        "TEP 9.4% vs Lichtenstein 18.8%"]
    assert [[c for c, _, _ in it.arms] for it in out_of_100(w, REFS)] == [[9, 19]]
    # Drawn in "Out of 100" and shown in "Compare the arms": never also "Not drawn" in the
    # outcome (third check L2; it was listed again with "one arm's rate").
    assert outcome_cards(w, REFS)[0].listed == []
    assert shown_at(w, REFS) == every_number(w)


def test_a_difference_without_an_arm_is_still_drawn():
    f = forest(answer_with(num("Mean difference in stay", -1.0, "days", None, lo=-2.0, hi=0.5),
                           num("Risk difference", -3.0, "%", "  ", lo=-5.0, hi=-1.0)), REFS)
    assert [r.drawn for r in f.diffs] == [True, True]


# ---------- an arm name that contains "MD" or "minus" is an arm name (re-check H-A) ----------

@pytest.mark.parametrize("arm", ["MD Anderson protocol", "Md. Ali technique"])
def test_an_arm_name_containing_a_marker_is_one_arms_rate(arm):
    w = answer_with(num("Recurrence at 2 y", 12.5, "%", arm, lo=8.0, hi=17.0))
    f = forest(w, REFS)
    assert f.diffs == [] and f.ratios == []
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [((0, 0), ONE_ARM)]
    assert shown_at(w, REFS) == every_number(w)


@pytest.mark.parametrize("arm", ["MD Anderson protocol", "Md. Ali technique"])
def test_an_arm_name_containing_a_marker_pairs_as_a_normal_arm(arm):
    w = written([outcome("Recurrence", [num("Recurrence at 2 y", 12.5, "%", arm),
                                        num("Recurrence at 2 y", 20.0, "%", "Standard")])],
                [study("1", f"{arm} vs Standard")])
    assert [g.kind for g in all_groups(w, REFS)] == ["pair"]
    assert forest(w, REFS).diffs == []
    assert [r.values_text for g in compare(w, REFS) for r in g.rows] == [
        f"{arm} 12.5% vs Standard 20%"]
    [it] = out_of_100(w, REFS)
    assert it.arms == [(13, arm, "A"), (20, "Standard", "B")]
    assert outcome_cards(w, REFS)[0].listed == []


@pytest.mark.parametrize("arm", ["MD", " md ", "SMD", "wmd", "difference", "Mean difference",
                                 "risk  difference", "Absolute difference"])
def test_an_arm_text_that_is_only_a_marker_is_not_an_arm_name(arm):
    w = answer_with(num("LOS", 0.33, "days", arm, lo=-0.03, hi=0.7))
    [d] = forest(w, REFS).diffs
    assert d.drawn
    p = written([outcome("D", [num("D", 95.9, "%", "R-TAPP"), num("D", 14.4, "%", arm)])])
    assert chart_kind([n for n in p.outcomes[0].numbers]) == "text"


# ---------- a row whose axis cannot be labelled never fails the page (re-check M-B) ----------

HUGE_DIFFS = [(0.0, -5.0, 1e308), (10.0, 8.0, 1e308), (0.0, -1e308, 0.8),
              (-10.0, -1e308, -5.0), (5e306, 1e306, 1e307)]


@pytest.mark.parametrize("v,lo,hi", HUGE_DIFFS)
def test_a_difference_whose_axis_overflows_is_listed_not_drawn(v, lo, hi):
    w = answer_with(md(v, lo, hi), ratio(0.5, 0.2, 0.9))
    f = forest(w, REFS)
    assert [(r.drawn, r.problem) for r in f.diffs] == [(False, "the interval is too wide to draw")]
    assert [r.drawn for r in f.ratios] == [True]
    body = AnswerBody(created_at="2026-11-12T00:00:00Z", answer=w, empty_reason=None,
                      references=[Reference(1, "1", "T", "J", 2020, None, False)], searches=[],
                      integrity=Integrity(), stopped_early=None, elapsed_s=1, notes=[])
    v_ = answer_view(body)
    rows = [i.payload for p in v_["pairs"] for side in (p.left, p.right) if side
            for i in side.shown + side.hidden if isinstance(i.payload, ForestRow)]
    assert [r.problem for r in rows if r.at == (0, 0)] == ["the interval is too wide to draw"]


def test_any_arithmetic_error_in_a_rows_derivation_lists_that_row_only(monkeypatch):
    import pubmedgemini.answer_page as ap

    def boom(v):
        raise OverflowError("x")

    monkeypatch.setattr(ap, "_signed", lambda *a: boom(a[0]))  # the difference axis labels
    f = forest(answer_with(md(-1.0, -2.0, 0.5), ratio(0.5, 0.2, 0.9)), REFS)
    assert [(r.drawn, r.problem) for r in f.diffs] == [
        (False, "the numbers could not be placed on a scale")]
    assert [r.drawn for r in f.ratios] == [True]


# ---------- an arm named on a difference says so (re-check M-C, coordinator option a) ----------

ARM_ON_DIFF = "an arm is named on a difference"


@pytest.mark.parametrize("n", [
    num("MD", -0.34, "points", "robotic", lo=-0.9, hi=0.2),  # Incisive's writer_v3.rs example
    num("Pain", -1.2, "MD", "TEP", lo=-2.0, hi=-0.4),
    num("Robotic minus lap", 4.0, "min", "robotic", lo=1.0, hi=7.0),
])
def test_an_arm_named_on_a_labelled_difference_is_listed_with_a_true_reason(n):
    w = answer_with(n)
    f = forest(w, REFS)
    assert f.diffs == [] and f.ratios == []
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [
        ((0, 0), ARM_ON_DIFF)]


@pytest.mark.parametrize("label", ["Recurrence (no significant difference)",
                                   "Pain, difference not significant"])
def test_a_number_not_labelled_as_a_difference_keeps_the_one_arm_reason(label):
    w = answer_with(num(label, 2.1, "%", "TEP", lo=1.0, hi=3.5))
    assert [x.problem for x in outcome_cards(w, REFS)[0].listed] == [ONE_ARM]


# ---------- arms that differ only by case or spacing are one arm (re-check L-D) ----------

@pytest.mark.parametrize("a,b", [("TEP", "tep"), ("TEP ", "TEP"), ("Robotic  TAPP", "robotic TAPP"),
                                 ("Lap\tTEP", "LAP TEP")])
def test_arms_differing_only_by_case_or_spacing_are_not_a_pair(a, b):
    w = written([outcome("Pain", [num("Pain", 9.4, "%", a), num("Pain", 18.8, "%", b)])],
                [study("1", "TEP vs Lichtenstein")])
    assert [g.kind for g in all_groups(w, REFS)] == ["text"]
    assert compare(w, REFS) == [] and out_of_100(w, REFS) == []
    listed = outcome_cards(w, REFS)[0].listed
    assert [(x.at, x.problem) for x in listed] == [((0, 0), "its arms are not distinct"),
                                                   ((0, 1), "its arms are not distinct")]
    assert pieces_text(listed[0].sub_pieces).endswith(f" · {a.strip()}")  # the writer's spelling


def test_distinct_arms_still_pair():
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP"), num("Pain", 18.8, "%", "TAPP")])])
    assert [g.kind for g in all_groups(w, REFS)] == ["pair"]


# ---------- numbers with no study are never paired (re-check L-F) ----------

NO_STUDY = "its study is not in the references"


@pytest.mark.parametrize("pmid", ["", "  ", "99999999"])
def test_numbers_with_no_study_are_never_paired(pmid):
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP", pmid),
                                  num("Pain", 18.8, "%", "Lichtenstein", pmid),
                                  num("Pain", 0.5, "RR", None, pmid, 0.2, 0.9)])],
                [study("1", "TEP vs Lichtenstein")])
    assert all(len(g.numbers) == 1 for g in all_groups(w, REFS))
    assert compare(w, REFS) == [] and out_of_100(w, REFS) == []
    f = forest(w, REFS)
    assert [(r.at, r.drawn, r.problem) for r in f.ratios] == [((0, 2), False, NO_STUDY)]
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [
        ((0, 0), NO_STUDY), ((0, 1), NO_STUDY)]
    assert shown_at(w, REFS) == every_number(w)


def test_a_known_study_still_pairs_beside_numbers_with_none():
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP"), num("Pain", 3.0, "%", "TEP", ""),
                                  num("Pain", 18.8, "%", "Lichtenstein")])],
                [study("1", "TEP vs Lichtenstein")])
    assert [r.values_text for g in compare(w, REFS) for r in g.rows] == [
        "TEP 9.4% vs Lichtenstein 18.8%"]
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [((0, 1), NO_STUDY)]


# ---------- random answers: the page's promises hold (re-check, property-style) ----------

_VALUES = [0.0, -0.0, 1.0, -1.0, 0.5, 12.5, 20.0, 99.5, 100.0, 140.0, -3.0, 1e-300, 5e-324,
           1e306, 1e307, 1e308, -1e308, 1.7e308, float("nan"), float("inf"), float("-inf")]
# Independent oracles for the random test (fourth check F6): plain lists of the reviewer's
# examples from all four checks and a simple re-implementation of "an arm matches a side",
# written here and never calling the page's own classifiers.

# Arm texts that are never an arm of a drawn pair (a compare row or squares).
BAD_ARMS = [
    # markers (re-check H-A)
    "MD", " md ", "SMD", "WMD", "difference", "Mean difference", "risk  difference",
    "Absolute difference",
    # third check M1
    "difference vs laparoscopic", "TEP vs Lichtenstein", "TEP vs. Lichtenstein",
    "TEP versus Lichtenstein", "Drain minus", "POEM minus PD", "TEP compared with open",
    "relative to open", "MD.", "M.D.", "Difference:", "ARR", "RD", "ARD", "NNT", "NNH",
    # fourth check F1
    "Δ", "absolute risk reduction", "ARR 5%", "RRR", "delta", "Diff", "mean diff",
    "TEP – Lichtenstein", "TEP − Lichtenstein", "TEP - Lichtenstein", "TEP/Lichtenstein",
    "TEP v Lichtenstein", "TEP > Lichtenstein", "NNT 12", "relative risk", "pooled", "overall",
    "both groups", "OR", "HR", "RR", "odds", "total", "all patients", "combined", "change",
]
# Plain arm names (some match no side of the comparisons below).
GOOD_ARMS = ["TEP", "tep", "TEP ", "Lichtenstein", "MD Anderson protocol", "Md. Ali technique",
             "Robotic  TAPP", "robotic TAPP", "\u202eTEP", "TEP\u200b", "\ufeffLichtenstein",
             "ＴＥＰ", "Laparoscopic repair", "Open repair", "diferencia", "TEP at 1 y",
             "TEP at 5 y", "TEP (n=665)", "Lichtenstein group", "Standard", "TAPP"]
# Labels with a negation or significance word: never a difference row, whatever the unit.
NEGATED_LABELS = ["Recurrence (no significant difference)", "Pain, difference not significant",
                  "No difference in pain", "No mean difference was found; recurrence",
                  "Recurrence rate (risk difference not significant)",
                  "Patients without a mean difference in pain",
                  "Rate difference not reported; pain",
                  "Risk difference was not computed; event rate", "Risk difference, n.s."]
# Labels that only mention a difference: a difference row only by its unit or arm marker.
MENTION_LABELS = ["Differences between groups", "MD Anderson Dysphagia Inventory score",
                  "MD (Anderson score)", "MD, Anderson score", "MD in Hospital stay", "Md. score",
                  "md", "Amd score", "MDA levels", "Robotic minus lap",
                  "Event rate where the risk difference was small", "Recurrence (risk difference)",
                  "Length of stay (MD)", "Pain", "Seroma RR", "Ache at 2 y"]
# Labels that name one side of "TEP vs Lichtenstein": never a drawn difference row there.
ONE_SIDE_LABELS = ["MD in TEP group", "Difference: recurrence in TEP arm",
                   "Mean difference, Lichtenstein"]
# Labels that name a difference: never squares (F3) and never a drawn ratio row (F4).
DIFF_LABELS = ["Risk difference", "Mean difference in pain", "MD", "Difference",
               "Difference in pain", "MD, days", "Risk difference, wound infection",
               "Mean difference in pain score"]
# Labels that name a change, not a rate: never squares (coordinator ruling 2).
CHANGE_LABELS = ["Reduction in recurrence", "Absolute risk reduction", "Change from baseline",
                 "Recurrence, relative risk", "Odds ratio of pain", "Δ pain", "NNT", "Weight gain",
                 "Increased pain", "Hazard ratio", "Excess recurrence", "Pain delta"]
NOT_SQUARES_LABELS = DIFF_LABELS + CHANGE_LABELS + [
    "MD in pain", "Pain minus", "Recurrence, risk difference", "Difference: recurrence",
    "Robotic minus lap"]
_ORACLE_MARKERS = {"md", "smd", "wmd", "difference", "mean difference", "risk difference",
                   "absolute difference"}
_ORACLE_UNITS = _ORACLE_MARKERS | {"median difference", "rate difference",
                                   "weighted mean difference", "standardised mean difference",
                                   "standardized mean difference"}


_O_INVISIBLE = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f"
                   "\u061c\u200b\u200c\u200d\u2060\ufeff")


def _o_norm(s):
    """Oracle: no invisible characters, NFKC, case-folded, spaces squashed."""
    import unicodedata

    s = unicodedata.normalize("NFKC", "".join(c for c in s or "" if c not in _O_INVISIBLE))
    return " ".join(s.casefold().split())


def _o_words(s):
    import re

    return [w for w in re.split(r"[\W_]+", _o_norm(s)) if len(w.encode("utf-8")) > 1]


def _o_sides(comparison):
    import re

    parts = re.split(r" (?:vs|vs\.|versus|compared with) ", _o_norm(comparison))
    if not comparison or len(parts) != 2:
        return None
    left, right = set(_o_words(parts[0])), set(_o_words(parts[1]))
    return (left - right, right - left) if left and right else None


def _o_side(arm, comparison):
    sides = _o_sides(comparison)
    if sides is None:
        return None
    score = sum((w in sides[0]) - (w in sides[1]) for w in _o_words(arm))
    if score:
        return 0 if score > 0 else 1
    g = _o_group(arm, comparison)
    return g if g is not None else _o_role(arm, comparison)


def _o_role(arm, comparison):
    """Oracle (S2, owner decision N6): an arm that is exactly experimental / standard of care /
    standard / control, with "group" or "arm" or not; experimental on the first side,
    the others on the second, each only where that side alone holds the word."""
    import re

    a = _o_norm(arm)
    for suffix in (" group", " arm"):
        a = a.removesuffix(suffix)
    if a not in ("experimental", "standard of care", "standard", "control"):
        return None
    parts = re.split(r" (?:vs|vs\.|versus|compared with) ", _o_norm(comparison))
    side = 0 if a == "experimental" else 1
    has = [re.search(rf"(?<![\w]){a}(?![\w])", p) is not None for p in parts]
    return side if has[side] and not has[1 - side] else None


def _o_group(arm, comparison):
    """Oracle (S1, owner decision N6): an arm "group <letter>" and exactly one side of the
    comparison holding "(group <letter>)"."""
    import re

    m = re.fullmatch(r"group ([a-z])", _o_norm(arm))
    if m is None:
        return None
    parts = re.split(r" (?:vs|vs\.|versus|compared with) ", _o_norm(comparison))
    has = [f"(group {m.group(1)})" in p for p in parts]
    return has.index(True) if has.count(True) == 1 else None


def _o_ratio(n):
    """Oracle (R1, owner decision N5, strict shape after review A1): a unit exactly RR, OR or
    HR; or a blank unit and `_o_r1_label`."""
    u = (n.unit or "").strip()
    if u:
        return u in ("RR", "OR", "HR")
    return _o_r1_label(n.label)


_O_QUALIFIERS = ["stratified", "adjusted", "unadjusted", "pooled", "crude", "multivariable",
                 "multivariate"]
_O_RATIO_NAMES = ["hazard ratio", "odds ratio", "risk ratio", "relative risk", "rate ratio"]
_O_AFTER = [",", ":", "(", "\u2014", "\u2013", " ,", " :", " (", " \u2014", " \u2013", " for ",
            " of ", " in ", " at "]
_O_R1_BANNED = ["reduction", "reductions", "reduced", "increase", "increases", "increased",
                "decrease", "decreases", "decreased", "change", "changes", "changed",
                "improvement", "improvements", "gain", "gains", "excess", "delta", "\u03b4",
                "\u2206", "difference", "differences", "diff", "minus", "md", "smd", "wmd",
                "no", "not", "non", "without", "none", "neither", "nor", "ns", "n.s",
                "nonsignificant", "insignificant", "nr", "ne", "na", "n/a",
                "hr", "rr", "irr", "ahr", "aor", "arr", "rrr", "nnt", "nnh"]


def _o_tokens(s):
    """Oracle: the label's tokens (runs of letters, digits, "." and "/"; trailing dots off)."""
    import re

    return {t.rstrip(".") for t in re.findall(r"[\w./]+|[\u03b4\u2206]", s)}


def _o_r1_label(label):
    """Oracle (R1): the label, cleaned and case-folded, is an optional qualifier then one ratio
    name, then the end of the label or a separator; and holds no change, difference or
    negation word, no measure abbreviation (OR only in capitals), and no second ratio name."""
    k = _o_norm(label)
    for q in _O_QUALIFIERS:
        if k.startswith(q + " "):
            k = k[len(q) + 1:]
            break
    name = next((x for x in _O_RATIO_NAMES if k.startswith(x)), None)
    if name is None:
        return False
    rest = k[len(name):]
    if rest and not any(rest.startswith(a) for a in _O_AFTER):
        return False
    import re

    whole = _o_norm(label)
    if _o_tokens(whole) & set(_O_R1_BANNED):
        return False
    if re.search(r"(?<![\w])OR(?![\w])", label):
        return False
    names = whole.count("ratio") + whole.count("relative risk")
    return names == 1


R1_LABELS = ["Hazard ratio for death", "Stratified hazard ratio for relapse",
             "adjusted odds ratio", "Rate ratio of readmission",
             "Recurrence, hazard ratio not significant", "Hazard ratios for death",
             "Unadjusted hazard ratio", "Crude odds ratio: example outcome",
             "Hazard ratio at 3 years", "Multivariable hazard ratio (example)"]
# The reviewer's A1/B1 labels (12 Nov 2026): never a ratio row, never a drawn row of any kind.
R1_REFUSED = ["Relative risk reduction for recurrence", "Relative risk reduction",
              "Relative risk increase in leak", "Hazard ratio not reported; recurrence 12%",
              "Odds ratio difference", "Risk ratio difference in example outcome",
              "Hazard ratio change from baseline", "Hazard ratio (NS)",
              "Hazard ratio, not significant", "Hazard ratio not significant for DFS",
              "Hazard ratio, n.s.", "Hazard ratio not estimable", "Hazard ratio not reached",
              "Hazard ratio, non-significant", "Hazard ratio, nonsignificant",
              "Hazard ratio insignificant", "Odds ratio without adjustment", "Hazard ratio: none",
              "Relative risk, reduced", "Odds ratio decrease", "Hazard ratio improvement",
              "Odds ratio gain", "Risk ratio, excess", "Hazard ratio delta", "Hazard ratio \u0394",
              "Hazard ratio, mean difference", "Odds ratio, risk difference",
              "Relative risk, absolute risk difference", "Hazard ratio minus one",
              "Hazard ratio, odds ratio", "Hazard ratio for death (relative risk)",
              "Odds ratio (HR)", "Hazard ratio, NNT", "Hazard ratio OR odds", "Hazard ratio NR",
              "Hazard ratio-adjusted death", "Hazard ratio overall survival",
              "Stratified adjusted hazard ratio", "Hazard ratio for difference in pain"]
_BAD_ARM_KEYS = {_o_norm(a) for a in BAD_ARMS}
_LABELS = (["Pain", "Seroma RR", "", "  ", "Pain\u200b", "\u202eRecurrence", "Robotic minus lap"]
           + NEGATED_LABELS + MENTION_LABELS + ONE_SIDE_LABELS + NOT_SQUARES_LABELS + R1_LABELS
           + R1_REFUSED)
_ARMS = [None, "", " ", " ", "\u200b"] + GOOD_ARMS + BAD_ARMS
_UNITS = [None, "", "%", " % ", "RR", "OR", "HR", "rr", "aHR", "days", "points", "MD"]
_PMIDS = ["1", "2", " 1 ", "", "  ", "99999999"]
_P = [None, "", "p", "P < .001", "0.03"]
_COMPARISONS = ["TEP vs Lichtenstein"] * 4 + ["MD Anderson protocol vs Standard", "",
                "TEP vs TAPP vs Lichtenstein", "Laparoscopic repair vs Open repair",
                "TAPP vs Lichtenstein"] + ["Drug X then surgery (group A) vs surgery (group B)"] * 3 + [
                "Drug X (group A) vs drug Y (group A)"] + [
                "Experimental group (drug X) vs control"] * 3 + [
                "Experimental arm vs standard of care arm", "Control drug X vs control"]


def _rand_value(r):
    return r.choice(_VALUES) if r.random() < 0.5 else r.uniform(-1e3, 1e3) * 10 ** r.randint(-8, 8)


def _rand_number(r) -> Number:
    lo = None if r.random() < 0.3 else _rand_value(r)
    hi = None if r.random() < 0.3 else _rand_value(r)
    if lo is not None and hi is not None and r.random() < 0.6:
        lo, hi = sorted((lo, hi), key=lambda x: (math.isnan(x), x))
    v = _rand_value(r)
    if lo is not None and hi is not None and r.random() < 0.5 and lo <= hi:
        v = lo + (hi - lo) / 2 if math.isfinite(hi - lo) else lo
    return Number(r.choice(_LABELS), v, r.choice(_UNITS), r.choice(_ARMS), lo, hi, r.choice(_P),
                  r.choice(_PMIDS))


def _rand_pair(r) -> list[Number]:
    """Two percentages of one label and study, as an out-of-100 candidate (F6)."""
    label = r.choice(["Pain", "Ache at 2 y"] if r.random() < 0.5 else _LABELS)
    arms = [r.choice(GOOD_ARMS if r.random() < 0.7 else _ARMS) for _ in range(2)]
    if r.random() < 0.15:  # S2: role words, which some comparisons use
        return [Number(label, r.uniform(0, 100), "%", a, None, None, None, "1")
                for a in r.sample(["Experimental group", "Control group", "control arm",
                                   "Standard of care arm", "Experimental control group",
                                   "Intervention group"], 2)]
    if r.random() < 0.15:  # S1: group letters, which some comparisons name
        return [Number(label, r.uniform(0, 100), "%", a, None, None, None, "1")
                for a in r.sample(["Group A", "group b", "Group B", "Group C", "Group A (n=9)"],
                                  2)]
    if r.random() < 0.3:  # a likely match for "TEP vs Lichtenstein", in either order
        arms = r.sample([r.choice(["TEP", "ＴＥＰ", "TEP (n=665)", "\u202eTEP"]),
                         r.choice(["Lichtenstein", "Lichtenstein group", "\ufeffLichtenstein"])], 2)
    return [Number(label, r.uniform(0, 100), "%", a, None, None, None, "1") for a in arms]


def _rand_diff(r) -> Number:
    """A number with a CI and no arm, as a difference or ratio row candidate (F6)."""
    lo = r.uniform(-5, 5)
    hi = lo if r.random() < 0.1 else lo + r.uniform(0, 5)
    unit = r.choice([None, "%", "days", "MD", "RR", "mean difference"])
    if unit == "RR":
        lo, hi = abs(lo) + 0.1, abs(lo) + 0.1 + (hi - lo)
    return Number(r.choice(_LABELS), (lo + hi) / 2, unit, r.choice([None, "", "MD"]), lo, hi,
                  None, "1")


def _rand_body(r) -> AnswerBody:
    outs = [outcome(f"Outcome {i}", [_rand_number(r) for _ in range(r.randint(0, 6))]
                    + (_rand_pair(r) if r.random() < 0.6 else [])
                    + ([_rand_diff(r)] if r.random() < 0.6 else []),
                    r.choice(CERTAINTIES)) for i in range(r.randint(1, 3))]
    studies = [study("1", r.choice(_COMPARISONS))] if r.random() < 0.8 else []
    w = written(outs, studies)
    refs = [Reference(1, "1", "T1", "J", 2020, None, False),
            Reference(2, "2", "T2", None, None, None, False)]
    return AnswerBody(created_at="2026-11-12T00:00:00Z", answer=w, empty_reason=None,
                      references=refs, searches=[], integrity=Integrity(), stopped_early=None,
                      elapsed_s=1, notes=[])


def test_random_answers_keep_the_pages_promises():
    import random

    from pubmedgemini.answer_page import arm_name, out_of_100_groups

    r = random.Random(20261112)
    squares_seen = diffs_seen = r1_seen = r1_refused_seen = s1_seen = s2_seen = 0
    for case in range(4000):
        body = _rand_body(r)
        w, refs = body.answer, refs_of(body)

        def comparison(n, w=w):  # oracle: the number's own trials row, by its PMID
            return next((x.comparison for x in w.studies if x.pmid.strip() == n.pmid.strip()),
                        None)

        def at(o_j, w=w):
            return w.outcomes[o_j[0]].numbers[o_j[1]]

        answer_view(body)  # never raises
        assert shown_at(w, refs) == every_number(w), case  # every number is on the page
        f = forest(w, refs)
        for row in f.ratios + f.diffs:  # a drawn row never carries a reason
            assert (row.problem is None) == row.drawn, case
            if row.drawn:  # F4: a drawn interval has width
                n = at(row.at)
                assert n.ci_low != n.ci_high, case
        for row in f.ratios:  # F4: a drawn ratio's label names no difference
            assert not (row.drawn and at(row.at).label in DIFF_LABELS), case
        # R1: the ratio rows are exactly the numbers the oracle calls ratios, with both bounds
        def finite(x):
            return x is not None and math.isfinite(x)

        expected = {(i, j) for i, o in enumerate(w.outcomes) for j, n in enumerate(o.numbers)
                    if _o_ratio(n) and finite(n.ci_low) and finite(n.ci_high)}
        assert {row.at for row in f.ratios} == expected, case
        r1_seen += sum(row.drawn and not (at(row.at).unit or "").strip() for row in f.ratios)
        for row in f.ratios + f.diffs:  # A1: a refused ratio label with no unit is never drawn
            n = at(row.at)
            assert not (n.label in R1_REFUSED and not (n.unit or "").strip()), case
        r1_refused_seen += sum(n.label in R1_REFUSED and not (n.unit or "").strip()
                               and finite(n.ci_low) and finite(n.ci_high)
                               for o in w.outcomes for n in o.numbers)
        for row in f.diffs:
            n = at(row.at)
            # d24: no number with an arm name is a difference row (oracle: blank or a marker)
            assert not _o_norm(n.arm) or _o_norm(n.arm) in _ORACLE_MARKERS, case
            # M2, F2: no difference row from a negated label, or from a label that only
            # mentions a difference unless its unit or arm marker names the measure
            assert n.label not in NEGATED_LABELS, case
            if n.label in MENTION_LABELS:
                assert _o_norm(n.unit) in _ORACLE_UNITS or _o_norm(n.arm) in _ORACLE_MARKERS, case
            # F5: a drawn difference row's label does not name one side
            if row.drawn and comparison(n) == TEP_VS_L:
                assert n.label not in ONE_SIDE_LABELS, case
            diffs_seen += row.drawn
        rows = [row for g in compare(w, refs) for row in g.rows]
        for row in rows:  # every compared value has its own arm name and a known study
            assert all(arm_name(at(x)) for x in row.at), case
            assert all(rust_trim_pmid(at(x)) in refs for x in row.at), case
            # M1, F1: no compared arm text is a comparison or a marker (oracle list)
            assert not any(_o_norm(at(x).arm) in _BAD_ARM_KEYS for x in row.at), case
        items = out_of_100(w, refs)
        for it in items:
            assert all(name.strip() for _, name, _ in it.arms), case
        # each out-of-100 candidate is drawn or its compare row gives the reason, never both
        candidates = out_of_100_groups(w, all_groups(w, refs))
        assert len(items) + sum(row.problem is not None for row in rows) == len(candidates), case
        cand_at = {tuple((g.outcome, j) for j in g.numbers) for g in candidates}
        squares = [row for row in rows if row.problem is None and row.at in cand_at]
        assert len(squares) == len(items), case
        for row in squares:  # F1, F3: squares only for arms on opposite sides, rate labels
            nums = [at(x) for x in row.at]
            assert {_o_side(n.arm, comparison(n)) for n in nums} == {0, 1}, case
            assert not any(_o_norm(n.arm) in _BAD_ARM_KEYS for n in nums), case
            assert nums[0].label not in NOT_SQUARES_LABELS, case
            squares_seen += 1
            s1_seen += all(_o_norm(n.arm).startswith("group ") for n in nums)
            s2_seen += any(_o_role(n.arm, comparison(n)) is not None for n in nums)
        # third check L1: drawn difference axes have three distinct labels and 0 inside
        for row in f.diffs:
            if row.drawn:
                sc = row.scale
                assert len({sc.lo_text, sc.mid_text, sc.hi_text}) == 3, case
                assert sc.lo < 0 < sc.hi, case
        # third check L2: a number shown in a compare row is never also listed as not drawn
        in_rows = {x for row in rows for x in row.at}
        assert not in_rows & {x.at for c in outcome_cards(w, refs) for x in c.listed}, case
        # third check L3: nothing the page draws holds a bidi control or zero-width character
        assert not any(ch in INVISIBLE for t in _strings(answer_view(body)) for ch in t), case
    assert squares_seen > 50 and diffs_seen > 50 and r1_seen > 20 and s1_seen > 5 and s2_seen > 5
    assert r1_refused_seen > 20
    # (the oracles were exercised)


def rust_trim_pmid(n: Number) -> str:
    from pubmedgemini.gemini.structured import rust_trim

    return rust_trim(n.pmid)


# ---------- an arm qualifies for a pair only as a plain arm name (third check M1) ----------

COMPARISON_ARM = "its arm text reads as a comparison, not an arm"
OTHER_COMPARISON_ARM = "the other arm's text reads as a comparison, not an arm"


@pytest.mark.parametrize("arm,kind", [
    (None, "none"), ("", "none"), ("  ", "none"),
    ("MD", "marker"), (" md ", "marker"), ("risk  difference", "marker"), ("SMD", "marker"),
    ("MD.", "comparison"), ("M.D.", "comparison"), ("Difference:", "comparison"),
    ("ARR", "comparison"), ("rd", "comparison"), ("A.R.D.", "comparison"), ("NNT", "comparison"),
    ("nnh", "comparison"), ("difference vs laparoscopic", "comparison"),
    ("TEP vs Lichtenstein", "comparison"), ("TEP vs. Lichtenstein", "comparison"),
    ("TEP versus Lichtenstein", "comparison"), ("Drain minus", "comparison"),
    ("POEM minus PD", "comparison"), ("TEP compared with open", "comparison"),
    ("TEP compared  to open", "comparison"), ("relative to open", "comparison"),
    ("Mean difference.", "comparison"), ("Differences", "comparison"),
    ("MD Anderson protocol", "name"), ("Md. Ali technique", "name"), ("TEP", "name"),
    ("Robotic TAPP", "name"), ("Lichtenstein", "name"), ("Versius robot", "name"),
    ("Minusinsk protocol", "name"), ("ＴＥＰ", "name"),
    # "relative risk" reads as a comparison since the fourth check (F1); it was a name.
    ("Relative risk group", "comparison"),
])
def test_each_arm_text_has_one_kind(arm, kind):
    from pubmedgemini.answer_page import arm_kind

    assert arm_kind(num("Pain", 1.0, "%", arm)) == kind


def test_a_comparison_in_the_arm_text_is_never_paired():  # the reviewer's case
    w = written([outcome("Conversion", [
        num("Conversion", 2.0, "%", "robotic"),
        num("Conversion", 6.0, "%", "difference vs laparoscopic", lo=2.0, hi=10.0)])],
        [study("1", "Robotic vs laparoscopic")])
    assert out_of_100(w, REFS) == [] and compare(w, REFS) == []
    f = forest(w, REFS)
    assert f.diffs == [] and f.ratios == []
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [
        ((0, 0), OTHER_COMPARISON_ARM), ((0, 1), COMPARISON_ARM)]
    assert shown_at(w, REFS) == every_number(w)


@pytest.mark.parametrize("arm", ["MD.", "M.D.", "Difference:", "ARR", "RD", "ARD", "NNT", "NNH",
                                 "Drain minus", "TEP vs Lichtenstein"])
def test_a_marker_with_punctuation_or_a_comparison_is_not_an_arm_of_a_pair(arm):
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP"), num("Pain", 3.0, "%", arm)])],
                [study("1", "TEP vs Lichtenstein")])
    assert out_of_100(w, REFS) == [] and compare(w, REFS) == []
    assert [x.problem for x in outcome_cards(w, REFS)[0].listed] == [OTHER_COMPARISON_ARM,
                                                                     COMPARISON_ARM]


def test_drain_minus_is_now_listed_not_paired():
    # Changed by the third check (M1): "Drain minus" contains the word "minus", so it reads as
    # a comparison, not an arm; it was paired as an arm name after re-check H-A.
    w = written([outcome("Recurrence", [num("Recurrence at 2 y", 12.5, "%", "Drain minus"),
                                        num("Recurrence at 2 y", 20.0, "%", "Standard")])],
                [study("1", "Drain minus vs Standard")])
    assert compare(w, REFS) == [] and out_of_100(w, REFS) == []
    assert [x.problem for x in outcome_cards(w, REFS)[0].listed] == [COMPARISON_ARM,
                                                                     OTHER_COMPARISON_ARM]


@pytest.mark.parametrize("n", [
    num("Recurrence at 2 y", 12.5, "%", "Drain minus", lo=8.0, hi=17.0),
    num("Mean difference in pain", -1.2, None, "TEP vs Lichtenstein", lo=-2.0, hi=-0.4),
    num("Difference", 41.0, "percentage points", "POEM minus PD", lo=25.0, hi=57.0),
])
def test_a_single_number_with_a_comparison_arm_says_so(n):
    w = answer_with(n)
    assert forest(w, REFS).diffs == []
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [
        ((0, 0), COMPARISON_ARM)]


def test_a_ratio_with_a_comparison_arm_is_still_a_ratio_row():
    w = answer_with(num("Seroma", 0.5, "RR", "TEP vs Lichtenstein", lo=0.2, hi=0.9))
    [r] = forest(w, REFS).ratios
    assert r.drawn and r.sub.endswith(" · TEP vs Lichtenstein")


# ---------- a difference row names its measure (third check M2, plan X33) ----------

NOT_CLEAR_DIFF = "not clearly a difference between arms"


@pytest.mark.parametrize("label,unit,arm,term", [
    ("MD", None, None, "MD"),
    ("Mean difference in pain", None, None, "mean difference"),
    ("Return to daily activities, mean difference", "days", None, "mean difference"),
    ("Risk difference", "%", None, "risk difference"),
    ("Absolute risk difference in recurrence", "%", None, "risk difference"),
    ("Standardised mean difference", None, None, "standardised mean difference"),
    ("Difference", "percentage points", None, "difference"),
    ("difference in length of stay", "days", None, "difference"),
    ("Difference: pain", None, None, "difference"),
    ("Difference (TEP minus Lichtenstein)", None, None, "difference"),
    ("Difference — pain", None, None, "difference"),
    ("MD, days", None, None, "MD"),
    ("MD in VAS pain", None, None, "MD"),
    ("MD (95% CI)", None, None, "MD"),
    ("MD: return to work", None, None, "MD"),
    ("SMD", None, None, "SMD"),
    ("WMD for pain", None, None, "WMD"),
    ("MD of LOS", None, None, "MD"),
    ("Pain", "MD", None, "MD"),
    ("Pain", "mean difference", None, "mean difference"),
    ("Pain", " smd ", None, "SMD"),
    ("LOS", "days", "MD", "MD"),
    ("LOS", "days", "risk  difference", "risk difference"),
    ("ＭＤ", None, None, "MD"),
])
def test_a_named_difference_measure_is_found(label, unit, arm, term):
    from pubmedgemini.answer_page import difference_measure

    assert difference_measure(num(label, 1.0, unit, arm)) == term


@pytest.mark.parametrize("label,unit", [
    ("Recurrence (no significant difference)", "%"),
    ("Pain, difference not significant", None),
    ("No difference in pain", None),
    ("Differences between groups", None),
    ("MD Anderson Dysphagia Inventory score", None),
    ("MD (Anderson score)", None),
    ("MD, Anderson score", None),
    ("MD in Hospital stay", None),  # a capitalised word after MD: not counted (allow-list)
    ("Md. score", None),
    ("md", None),  # an abbreviation counts in a label only in capitals
    ("Amd score", None),
    ("MDA levels", None),
    ("Robotic minus lap", "min"),
    ("Pain", "days"),
    ("Pain", "difference not significant"),
    ("Pain", None),
])
def test_text_that_only_mentions_a_difference_names_no_measure(label, unit):
    from pubmedgemini.answer_page import difference_measure

    assert difference_measure(num(label, 1.0, unit)) is None


@pytest.mark.parametrize("n", [
    num("Recurrence (no significant difference)", 12.5, "%", None, lo=8.0, hi=17.0),
    num("MD Anderson Dysphagia Inventory score", 72.3, None, None, lo=68.0, hi=76.0),
    num("Robotic minus lap", 4.0, "min", None, lo=1.0, hi=7.0),
    num("Recurrence (no significant difference)", 12.5, "%", None),
])
def test_a_number_that_only_mentions_a_difference_is_listed_not_drawn(n):  # the reviewer's
    w = answer_with(n)
    f = forest(w, REFS)
    assert f.diffs == [] and f.ratios == []
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [
        ((0, 0), NOT_CLEAR_DIFF)]
    assert shown_at(w, REFS) == every_number(w)


def test_a_named_measure_without_an_arm_is_drawn_and_named_in_its_aria():
    f = forest(answer_with(
        num("Difference in MD Anderson score", 2.0, None, None, lo=1.0, hi=3.0),
        num("Pain", -1.2, "MD", None, lo=-2.0, hi=-0.4),
        num("SMD for pain", -0.5, None, None, lo=-0.9, hi=-0.1)), REFS)
    assert [r.drawn for r in f.diffs] == [True, True, True]
    assert [r.aria.split(" ")[0:2] for r in f.diffs] == [
        ["Difference", "2,"], ["Mean", "difference"], ["Difference", "minus"]]


# ---------- difference axis labels are distinct and true (third check L1) ----------

@pytest.mark.parametrize("v,lo,hi,labels", [
    (0.002, 0.001, 0.003, ("−0.001", "0", "+0.003")),
    (-0.003, -0.006, 0.0005, ("−0.006", "0", "+0.002")),
    # (0.004 and 5.0 with no width are listed since the fourth check, F4: now in
    # test_an_interval_of_no_width_is_listed)
    (0.004, 0.003, 0.004, ("−0.001", "0", "+0.004")),
    (5.0, 4.5, 5.0, ("−1", "0", "+5")),
    (0.33, -0.03, 0.7, ("−0.2", "0", "+0.8")),
    (-4.74, -6.78, -2.7, ("−8", "0", "+2")),
    (-120.0, -120.0, 80.0, ("−150", "0", "+100")),
    (2.5e-5, 1e-5, 4e-5, ("−0.00001", "0", "+0.00004")),
])
def test_difference_axis_labels_take_their_precision_from_the_step(v, lo, hi, labels):
    [d] = forest(answer_with(num("Risk difference", v, None, None, lo=lo, hi=hi)), REFS).diffs
    assert d.drawn and (d.scale.lo_text, d.scale.mid_text, d.scale.hi_text) == labels
    assert 0 < d.null < 100  # 0 is never at an edge (X16)


def test_a_tiny_interval_whose_axis_would_put_0_at_an_edge_is_not_drawn():
    # 5e-10 (5e-10 to 1e-9): at the axis's 9-decimal rounding its lower edge is 0, so no axis
    # keeps 0 off the edge (X16): listed, not drawn on a wrong scale. (The point interval
    # 1e-9 to 1e-9 is listed with "no width" since the fourth check, F4.)
    [d] = forest(answer_with(num("Risk difference", 5e-10, None, None, lo=5e-10, hi=1e-9)),
                 REFS).diffs
    assert (d.drawn, d.problem) == (False, "the interval is too narrow to draw")


# ---------- a number shown in a pair is never also "Not drawn" (third check L2) ----------

@pytest.mark.parametrize("label", ["Recurrence, difference not significant", "Pain minus",
                                   "MD in pain"])
def test_a_compared_number_is_not_listed_again(label):
    w = written([outcome("Pain", [num(label, 9.4, "%", "TEP", lo=7.0, hi=12.0),
                                  num(label, 18.8, "%", "Lichtenstein", lo=15.0, hi=22.0)])],
                [study("1", "TEP vs Lichtenstein")])
    in_pairs = {x for g in compare(w, REFS) for r in g.rows for x in r.at}
    assert in_pairs == {(0, 0), (0, 1)}
    assert outcome_cards(w, REFS)[0].listed == []


# ---------- bidi controls and zero-width characters never reach the page (third check L3) ----

INVISIBLE = ("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c"
             "\u200b\u200c\u200d\u2060\ufeff")


def test_drop_invisible_removes_bidi_and_zero_width_characters_only():
    from pubmedgemini.lit.text import drop_invisible

    assert drop_invisible("T" + INVISIBLE + "EP") == "TEP"
    kept = "\t\n\x01\x1f\u00ad\u00a0é🦷ТЕР"  # other controls and characters are kept
    assert drop_invisible(kept) == kept


def _body(w) -> AnswerBody:
    return AnswerBody(created_at="2026-11-12T00:00:00Z", answer=w, empty_reason=None,
                      references=[Reference(1, "1", "T", "J", 2020, None, False)], searches=[],
                      integrity=Integrity(), stopped_early=None, elapsed_s=1, notes=[])


def _compare_rows(v):
    return [i.payload for p in v["pairs"] for side in (p.left, p.right) if side
            for i in side.shown + side.hidden if i.payload.__class__.__name__ == "CompareRow"]


def _out_items(v):
    return [i.payload for p in v["pairs"] for side in (p.left, p.right) if side
            for i in side.shown + side.hidden if i.payload.__class__.__name__ == "OutItem"]


def test_a_right_to_left_override_in_an_arm_cannot_reorder_the_row():  # the reviewer's case
    w = written([outcome("Pain", [num("Chronic pain", 9.4, "%", "\u202eTEP"),
                                  num("Chronic pain", 18.8, "%", "Lichtenstein\u202c")])],
                [study("1", "TEP vs Lichtenstein")])
    v = answer_view(_body(w))
    [row] = _compare_rows(v)
    assert row.values_text == "TEP 9.4% vs Lichtenstein 18.8%"
    [it] = _out_items(v)
    assert [(c, a) for c, a, _ in it.arms] == [(9, "TEP"), (19, "Lichtenstein")]
    assert pieces_text(it.sentence_pieces).startswith(
        "Chronic pain: 9.4% with TEP vs 18.8% with Lichtenstein [1].")


def test_an_arm_differing_only_by_a_zero_width_character_is_the_same_arm():
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP\u200b"), num("Pain", 18.8, "%", "TEP")])],
                [study("1", "TEP vs Lichtenstein")])
    assert [g.kind for g in all_groups(w, REFS)] == ["text"]
    assert compare(w, REFS) == []
    assert [x.problem for x in outcome_cards(w, REFS)[0].listed] == [
        "its arms are not distinct", "its arms are not distinct"]


@pytest.mark.parametrize("arm", ["\u200b", "\u202e", " \ufeff\u2060 "])
def test_an_arm_of_invisible_characters_only_is_unnamed(arm):  # d23
    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP"), num("Pain", 18.8, "%", arm)])],
                [study("1", "TEP vs Lichtenstein")])
    from pubmedgemini.answer_page import arm_kind

    assert arm_kind(w.outcomes[0].numbers[1]) == "none"
    assert compare(w, REFS) == [] and out_of_100(w, REFS) == []
    assert [x.problem for x in outcome_cards(w, REFS)[0].listed] == [
        "the other arm is not named", "arm not named"]


def test_invisible_characters_in_a_marker_or_measure_are_ignored():
    from pubmedgemini.answer_page import arm_kind, difference_measure

    assert arm_kind(num("LOS", 1.0, "days", "M\u200bD")) == "marker"
    assert difference_measure(num("M\u200bD, days", 1.0)) == "MD"
    assert difference_measure(num("Pain", 1.0, "\u202eMD")) == "MD"


def test_the_view_drops_invisible_characters_from_every_model_string():
    w = written([outcome("Pa\u202ein", [num("Pain\u200b", 9.4, "%", "TEP", p="<0.0\u200e1"),
                                         num("Pain", 18.8, "%", "Lichtenstein")],
                         effect="E\u2066x", why="W\u061c", evidence="R\ufeff")],
                [study("1", "TEP\u202d vs Lichtenstein")])
    body = _body(w)
    body.notes.append("n\u200f")
    v = answer_view(body)
    assert not any(ch in INVISIBLE for t in _strings(v) for ch in t)
    assert any("Pain" == t for t in _strings(v))  # the strings were walked
    assert body.answer.outcomes[0].outcome == "Pa\u202ein"  # the stored answer is unchanged


def _strings(x, seen=None):
    """Every string in an answer_view value, at any depth (dataclasses, lists, dicts)."""
    import dataclasses

    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(x, (list, tuple, set)):
        for v in x:
            yield from _strings(v)
    elif dataclasses.is_dataclass(x) and not isinstance(x, type):
        for f in dataclasses.fields(x):
            yield from _strings(getattr(x, f.name))


# ---------- squares need arms that match the study's two sides (fourth check F1) ----------

NOT_MATCHED = "its arms do not match the study's comparison"
NO_SIDES = "the study's comparison does not name two sides"
TEP_VS_L = "TEP vs Lichtenstein"


def _pct_pair(a, b, comparison=TEP_VS_L, label="Chronic pain", va=9.4, vb=18.8):
    studies = [study("1", comparison)] if comparison is not None else []
    return written([outcome("Pain", [num(label, va, "%", a), num(label, vb, "%", b)])], studies)


F1_COMPARISON_ARMS = ["Δ", "absolute risk reduction", "ARR 5%", "RRR", "delta", "Diff",
                      "mean diff", "TEP – Lichtenstein", "TEP − Lichtenstein",
                      "TEP - Lichtenstein", "TEP/Lichtenstein", "TEP v Lichtenstein",
                      "TEP v. Lichtenstein", "TEP > Lichtenstein", "TEP < Lichtenstein",
                      "NNT 12", "relative risk", "pooled", "overall", "both groups", "OR",
                      "HR", "RR", "odds", "hazard", "ratio", "total", "all patients",
                      "combined", "change", "increase", "∆"]


@pytest.mark.parametrize("arm", F1_COMPARISON_ARMS)
def test_a_comparison_arm_text_is_never_paired_even_as_text(arm):  # fourth check F1
    from pubmedgemini.answer_page import arm_kind

    assert arm_kind(num("Pain", 1.0, "%", arm)) == "comparison"
    w = _pct_pair("TEP", arm)
    assert out_of_100(w, REFS) == [] and compare(w, REFS) == []
    assert [x.problem for x in outcome_cards(w, REFS)[0].listed] == [OTHER_COMPARISON_ARM,
                                                                     COMPARISON_ARM]
    w = _pct_pair(arm, "Lichtenstein")
    assert out_of_100(w, REFS) == [] and compare(w, REFS) == []
    assert shown_at(w, REFS) == every_number(w)


@pytest.mark.parametrize("arm", ["or", "TEP or open", "Ordinary care", "Hrs group", "Drainage",
                                 "R-TAPP", "TEP (n=665)", "Lichtenstein group", "diferencia"])
def test_words_inside_names_and_lower_case_or_are_not_comparisons(arm):
    from pubmedgemini.answer_page import arm_kind

    assert arm_kind(num("Pain", 1.0, "%", arm)) == "name"


@pytest.mark.parametrize("a,b", [("diferencia", "Lichtenstein"), ("TEP at 1 y", "TEP at 5 y"),
                                 ("Laparoscopic repair", "Open repair"), ("TEP", "TEP group"),
                                 ("Lichtenstein", "Lichtenstein (open)"), ("TAPP", "Lichtenstein")])
def test_arms_that_do_not_match_opposite_sides_get_no_squares(a, b):  # fourth check F1
    w = _pct_pair(a, b)
    assert out_of_100(w, REFS) == []
    [r] = [r for g in compare(w, REFS) for r in g.rows]
    assert r.problem == NOT_MATCHED
    assert r.values_text == f"{a} 9.4% vs {b} 18.8%"  # still a named text row
    assert outcome_cards(w, REFS)[0].listed == []
    assert shown_at(w, REFS) == every_number(w)


@pytest.mark.parametrize("comparison", [None, "", "TEP and Lichtenstein", "TEP vs TAPP vs Lichtenstein",
                                        "TEP vs ", "Hernia repair"])
def test_no_two_sided_comparison_means_no_squares(comparison):
    w = _pct_pair("TEP", "Lichtenstein", comparison)
    assert out_of_100(w, REFS) == []
    [r] = [r for g in compare(w, REFS) for r in g.rows]
    assert (r.values_text, r.problem) == ("TEP 9.4% vs Lichtenstein 18.8%", NO_SIDES)


@pytest.mark.parametrize("a,b,counts", [
    ("TEP", "Lichtenstein", [9, 19]), ("Lichtenstein", "TEP", [9, 19]),
    ("TEP (n=665)", "Lichtenstein group", [9, 19]), ("tep", "LICHTENSTEIN", [9, 19]),
    ("ＴＥＰ", "Lichtenstein\u200b", [9, 19])])
def test_arms_on_opposite_sides_draw_squares(a, b, counts):
    w = _pct_pair(a, b)
    [it] = out_of_100(w, REFS)
    assert sorted(c for c, _, _ in it.arms) == counts
    assert [r.problem for g in compare(w, REFS) for r in g.rows] == [None]


def test_the_genuine_pair_still_draws_9_and_19():
    [it] = out_of_100(_pct_pair("TEP", "Lichtenstein"), REFS)
    assert it.arms == [(9, "TEP", "A"), (19, "Lichtenstein", "B")]


def test_the_hernia_squares_are_unchanged():
    items = out_of_100(HERNIA.answer, HERNIA_REFS)
    assert [(it.label, [c for c, _, _ in it.arms]) for it in items] == [
        ("Chronic pain at 5 years", [9, 19]), ("Non-ignorable pain at about 8 years", [8, 7]),
        ("Chronic pain at 52 months", [15, 31]), ("Moderate or severe pain at 5 years", [2, 4]),
        ("Recurrence at 5 years", [4, 1])]
    assert all(r.problem is None for g in compare(HERNIA.answer, HERNIA_REFS) for r in g.rows)


# ---------- a named difference counts only where it names the row (fourth check F2) ----------

@pytest.mark.parametrize("n", [
    num("No mean difference was found; recurrence", 12.5, "%", None, lo=8.0, hi=17.0),
    num("Recurrence rate (risk difference not significant)", 12.5, "%", None, lo=8.0, hi=17.0),
    num("Event rate where the risk difference was small", 12.5, "%", None, lo=8.0, hi=17.0),
    num("Patients without a mean difference in pain", 12.5, "%", None, lo=8.0, hi=17.0),
    num("Mean difference not significant", -1.2, None, None, lo=-2.0, hi=-0.4),
    num("Risk difference, n.s.", -1.2, "%", None, lo=-2.0, hi=-0.4),
    num("MD, NS", -1.2, None, None, lo=-2.0, hi=-0.4),
    num("Difference in pain: none", -1.2, None, None, lo=-2.0, hi=-0.4),
    num("Mean difference (non-significant)", -1.2, None, None, lo=-2.0, hi=-0.4),
    num("Mean difference, nonsignificant", -1.2, None, None, lo=-2.0, hi=-0.4),
    num("Mean difference insignificant", -1.2, None, None, lo=-2.0, hi=-0.4),
    num("Pain, neither arm", -1.2, "MD", None, lo=-2.0, hi=-0.4),
    num("Pain nor stay", -1.2, "days", "MD", lo=-2.0, hi=-0.4),
])
def test_a_difference_mentioned_mid_label_or_negated_is_listed(n):  # fourth check F2
    from pubmedgemini.answer_page import difference_measure

    assert difference_measure(n) is None
    w = answer_with(n)
    f = forest(w, REFS)
    assert f.diffs == [] and f.ratios == []
    assert [(x.at, x.problem) for x in outcome_cards(w, REFS)[0].listed] == [
        ((0, 0), NOT_CLEAR_DIFF)]
    assert shown_at(w, REFS) == every_number(w)


@pytest.mark.parametrize("n,term", [
    (num("Mean difference in pain score", -1.2, None, None, lo=-2.0, hi=-0.4), "mean difference"),
    (num("Risk difference, wound infection", -3.0, "%", None, lo=-5.0, hi=-1.0), "risk difference"),
    (num("  Absolute risk difference in recurrence", -3.0, "%", None, lo=-5.0, hi=-1.0),
     "risk difference"),
    (num("Return to daily activities, mean difference", -4.74, "days", None, lo=-6.78, hi=-2.7),
     "mean difference"),
    (num("Pain", -1.2, "mean difference", None, lo=-2.0, hi=-0.4), "mean difference"),
    (num("Pain", -1.2, None, "MD", lo=-2.0, hi=-0.4), "MD"),
    (num("Notable pain score", -1.2, "MD", None, lo=-2.0, hi=-0.4), "MD"),
])
def test_a_named_difference_at_the_start_or_as_the_unit_is_drawn(n, term):
    from pubmedgemini.answer_page import difference_measure

    assert difference_measure(n) == term
    [d] = forest(answer_with(n), REFS).diffs
    assert d.drawn


# ---------- a pair whose label names a difference gets no squares (fourth check F3) ----------

LABEL_DIFF = "the label names a difference, not a rate"


@pytest.mark.parametrize("label", ["Risk difference", "Mean difference in pain", "MD in pain",
                                   "Pain minus", "Recurrence, risk difference",
                                   "Difference: recurrence"])
def test_a_pair_labelled_as_a_difference_is_a_text_row_not_squares(label):
    w = _pct_pair("TEP", "Lichtenstein", label=label)
    assert out_of_100(w, REFS) == []
    [r] = [r for g in compare(w, REFS) for r in g.rows]
    assert (r.values_text, r.problem) == ("TEP 9.4% vs Lichtenstein 18.8%", LABEL_DIFF)
    assert outcome_cards(w, REFS)[0].listed == []
    assert shown_at(w, REFS) == every_number(w)


# ---------- a pair whose label names a change gets no squares (coordinator ruling 2) ----------

LABEL_CHANGE = "the label names a change, not a rate"


@pytest.mark.parametrize("label", ["Reduction in recurrence", "Absolute risk reduction",
                                   "Change from baseline", "Pain, increased", "Odds ratio",
                                   "Relative risk of recurrence", "Δ pain", "NNT", "ARR",
                                   "Weight gain", "Excess risk", "Improvement in pain"])
def test_a_pair_labelled_as_a_change_is_a_text_row_not_squares(label):
    w = _pct_pair("TEP", "Lichtenstein", label=label)
    assert out_of_100(w, REFS) == []
    [r] = [r for g in compare(w, REFS) for r in g.rows]
    assert (r.values_text, r.problem) == ("TEP 9.4% vs Lichtenstein 18.8%", LABEL_CHANGE)
    assert outcome_cards(w, REFS)[0].listed == []
    assert shown_at(w, REFS) == every_number(w)


@pytest.mark.parametrize("label", ["Recurrence at 5 years", "Ratiocination score",
                                   "Unchanged pain", "Increasingly common pain"])
def test_a_pair_whose_label_names_no_change_keeps_its_squares(label):
    w = _pct_pair("TEP", "Lichtenstein", label=label)
    assert [[c for c, _, _ in it.arms] for it in out_of_100(w, REFS)] == [[9, 19]]


def test_a_pair_whose_label_only_mentions_a_difference_keeps_its_squares():
    w = _pct_pair("TEP", "Lichtenstein", label="Pain, difference not significant")
    assert [[c for c, _, _ in it.arms] for it in out_of_100(w, REFS)] == [[9, 19]]


# ---------- a ratio labelled as a difference, and an interval of no width (fourth check F4) ----

RATIO_ON_DIFF = "the label names a difference but the unit is a ratio"
NO_WIDTH = "the interval has no width"


@pytest.mark.parametrize("label,unit", [("Mean difference in pain", "RR"), ("Risk difference", "OR"),
                                        ("MD, days", "HR"), ("Difference in recurrence", "RR"),
                                        ("Recurrence, risk difference", "RR")])
def test_a_ratio_whose_label_names_a_difference_is_listed(label, unit):
    w = answer_with(num(label, 0.8, unit, None, lo=0.6, hi=0.95))
    f = forest(w, REFS)
    assert [(r.drawn, r.problem) for r in f.ratios] == [(False, RATIO_ON_DIFF)]
    assert f.diffs == [] and f.ratio_axis is None
    assert shown_at(w, REFS) == every_number(w)


@pytest.mark.parametrize("label", ["Seroma", "Recurrence (no significant difference)",
                                   "Chronic pain, TEP vs Lichtenstein"])
def test_a_ratio_whose_label_does_not_name_a_difference_is_drawn(label):
    [r] = forest(answer_with(num(label, 0.8, "RR", None, lo=0.6, hi=0.95)), REFS).ratios
    assert r.drawn


@pytest.mark.parametrize("n,kind", [
    (num("Seroma", 1.0, "RR", None, lo=1.0, hi=1.0), "ratios"),
    (num("Seroma", 0.5, "HR", None, lo=0.5, hi=0.5), "ratios"),
    (num("Risk difference", 0.004, None, None, lo=0.004, hi=0.004), "diffs"),
    (num("Mean difference in pain", 5.0, None, None, lo=5.0, hi=5.0), "diffs"),
    (num("MD", 0.0, "days", None, lo=0.0, hi=0.0), "diffs"),
    (num("Risk difference", 1e-9, None, None, lo=1e-9, hi=1e-9), "diffs"),
])
def test_an_interval_of_no_width_is_listed(n, kind):
    w = answer_with(n)
    f = forest(w, REFS)
    assert [(r.drawn, r.problem) for r in getattr(f, kind)] == [(False, NO_WIDTH)]
    assert f.ratio_axis is None
    assert shown_at(w, REFS) == every_number(w)


# ---------- a difference whose label names one arm is listed (fourth check F5) ----------

ONE_ARM_LABEL = "the label names one arm"


@pytest.mark.parametrize("n", [
    num("MD in TEP group", 4.2, "days", None, lo=3.0, hi=5.0),
    num("Difference: recurrence in TEP arm", 2.0, "%", None, lo=1.0, hi=3.0),
    num("Mean difference, Lichtenstein", 2.0, None, None, lo=1.0, hi=3.0),
    num("Risk difference for ＴＥＰ", 2.0, "%", None, lo=1.0, hi=3.0),
])
def test_a_difference_whose_label_names_one_arm_is_listed(n):
    w = written([outcome("Stay", [n])], [study("1", TEP_VS_L)])
    [d] = forest(w, REFS).diffs
    assert (d.drawn, d.problem) == (False, ONE_ARM_LABEL)
    assert shown_at(w, REFS) == every_number(w)


@pytest.mark.parametrize("label,comparison", [
    ("Difference (TEP minus Lichtenstein)", TEP_VS_L), ("MD in pain", TEP_VS_L),
    ("MD in TEP group", None), ("MD in TEP group", "TEP vs TAPP vs Lichtenstein"),
    ("Mean difference, TEP vs Lichtenstein", TEP_VS_L)])
def test_a_difference_naming_both_arms_or_neither_is_drawn(label, comparison):
    studies = [study("1", comparison)] if comparison is not None else []
    w = written([outcome("Stay", [num(label, 4.2, "days", None, lo=3.0, hi=5.0)])], studies)
    [d] = forest(w, REFS).diffs
    assert d.drawn


# ---------- F3b: the port reads main's stored answer (plan Task 2) ----------


def test_scrub_keeps_enums():
    from pubmedgemini.answer_page import model_text_cleaned
    from pubmedgemini.writer.answer import Certainty, EvidenceType

    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP")], certainty=Certainty.LOW)])
    w.outcomes[0].evidence_type = EvidenceType.RCT
    cleaned = model_text_cleaned(_body(w))
    o = cleaned.answer.outcomes[0]
    assert type(o.certainty) is Certainty and o.certainty is Certainty.LOW
    assert type(o.evidence_type) is EvidenceType and o.evidence_type is EvidenceType.RCT
    assert type(cleaned.answer.bottom_line.certainty) is Certainty
    assert outcome_cards(cleaned.answer, refs_of(cleaned))[0].evidence_label == "RCTs"


def test_cite_pieces_reads_mains_segments():
    # a bracket with one known id is a citation as a whole, so the short "1" counts as
    # unknown: the same result as f3-writer's rule on the same input
    assert cite_pieces("A [33763744; 1] b.", {"33763744": 1}) == [
        ("text", "A "), ("cite", [1]), ("unknown", 1), ("text", " b.")]
    assert cite_pieces("LOS 2 [1, 3] days", {}) == [("text", "LOS 2 [1, 3] days")]


def _hr(label, v, lo, hi):
    return num(label, v, None, None, "1", lo, hi)


def test_old_live_answer_shapes_render():
    # The live writer stores a hazard ratio with no unit and the measure in its label (plan
    # §1). Before R1 the strict rule listed all three and the CI card was absent; with R1
    # (owner decision N5) the two with an interval are drawn and the one-sided one is listed
    # with "no interval", as plan §2 counted on the real answer. (Deliberately changed in the
    # R1 commit.)
    w = written([outcome("Survival", [_hr("Hazard ratio for death", 0.7, 0.5, 0.9),
                                      _hr("Stratified hazard ratio for relapse", 0.8, 0.6, 1.1),
                                      _hr("Hazard ratio for progression", 0.9, None, 1.2)])],
                [study("1", "Drug A vs Drug B")])
    assert [(r.label, r.drawn) for r in forest(w, REFS).ratios] == [
        ("Hazard ratio for death", True), ("Stratified hazard ratio for relapse", True)]
    listed = outcome_cards(w, REFS)[0].listed
    assert [(x.label, x.problem) for x in listed] == [
        ("Hazard ratio for progression", "no interval")]
    v = answer_view(_body(w))
    [why] = [p for p in v["pairs"] if p.heading == "Why this conclusion"]
    assert why.left is not None and why.right is not None and why.note is None


def test_guidelines_in_the_view():
    from pubmedgemini.writer.answer import GuidelineV3

    w = written([outcome("Pain", [num("Pain", 9.4, "%", "TEP")])])
    assert "guidelines" not in answer_view(_body(w))
    w.guidelines = [
        GuidelineV3(pmid="1", body=" Society A ", year=2021, statement="Use mesh [1]."),
        GuidelineV3(pmid=None, body="Society B", year=None, statement="Prefer X.",
                    pmid_removed=True)]
    assert answer_view(_body(w))["guidelines"] == [
        ("Society A", [("text", "Use mesh "), ("cite", [1]), ("text", ".")], 2021, "1", False),
        ("Society B", [("text", "Prefer X.")], None, None, True)]


# ---------- R1: a ratio named at the start of its label (owner decision N5, 12 Nov 2026) ----

def _hr_row(label, unit=None, v=0.7, lo=0.5, hi=0.9, arm=None):
    return num(label, v, unit, arm, "1", lo, hi)


@pytest.mark.parametrize("label,name", [
    ("Hazard ratio for death", "Hazard ratio"), ("hazard ratio, overall survival", "Hazard ratio"),
    ("Stratified hazard ratio for relapse", "Hazard ratio"),
    ("Adjusted odds ratio of wound infection", "Odds ratio"),
    ("Pooled risk ratio: recurrence", "Risk ratio"), ("Relative risk of pain", "Risk ratio"),
    ("Rate ratio of readmission", "Rate ratio"), ("  HAZARD RATIO (95% CI)", "Hazard ratio"),
    # review A1: the labels that must still be drawn under the strict shape
    ("Hazard ratio for overall survival", "Hazard ratio"),
    ("Stratified hazard ratio for disease-free survival", "Hazard ratio"),
    ("Odds ratio, example outcome at 3 years", "Odds ratio"),
    ("Relative risk of recurrence", "Risk ratio"), ("Rate ratio", "Rate ratio"),
    ("Unadjusted hazard ratio", "Hazard ratio"), ("Crude odds ratio: example outcome", "Odds ratio"),
    ("Multivariable hazard ratio (example)", "Hazard ratio"),
    ("Multivariate odds ratio in example patients", "Odds ratio"),
    ("Hazard ratio at 3 years", "Hazard ratio"), ("Hazard ratio \u2014 example", "Hazard ratio"),
    ("Risk ratio\u2013example outcome", "Risk ratio")])
@pytest.mark.parametrize("unit", [None, "", "  "])
def test_ratio_named_at_label_start_is_drawn(label, name, unit):
    from pubmedgemini.answer_page import ratio_unit

    n = _hr_row(label, unit)
    assert ratio_unit(n) is not None
    assert _o_ratio(n)  # the oracle agrees
    f = forest(answer_with(n), REFS)
    [row] = f.ratios
    assert row.drawn and row.problem is None
    assert row.aria.startswith(f"{name} 0.7, interval 0.5 to 0.9, below 1")
    assert row.estimate_text == "0.7 (0.5–0.9)"  # the label already names the measure


NOT_MARKED = "not marked as a ratio or a difference"


@pytest.mark.parametrize("label,reason", [
    ("Recurrence, hazard ratio not significant", "unit not RR, OR or HR"),
    ("Recurrence, hazard ratio", "unit not RR, OR or HR"),
    ("Death (hazard ratio)", "unit not RR, OR or HR"),
    ("Overall survival hazard ratio", "unit not RR, OR or HR"),
    ("Stratified adjusted hazard ratio", "unit not RR, OR or HR"),
    ("HR for death", "unit not RR, OR or HR"),
    ("HR for overall survival", "unit not RR, OR or HR"),
    ("Hazard ratios for death", NOT_MARKED), ("Hazardratio", NOT_MARKED),
    ("Hazard rate", NOT_MARKED), ("Ratio of hazards", NOT_MARKED)])
def test_ratio_named_later_is_not(label, reason):
    from pubmedgemini.answer_page import ratio_unit

    n = _hr_row(label)
    assert ratio_unit(n) is None
    assert forest(answer_with(n), REFS).ratios == []
    [c] = outcome_cards(answer_with(n), REFS)
    assert [x.problem for x in c.listed] == [reason]


@pytest.mark.parametrize("label", R1_REFUSED)
@pytest.mark.parametrize("v,lo,hi", [(0.35, 0.2, 0.5), (35.0, 20.0, 48.0), (72.0, 65.0, 79.0)])
@pytest.mark.parametrize("unit", [None, ""])
def test_r1_refuses_what_is_not_plainly_one_ratio(label, v, lo, hi, unit):  # review A1, B1
    from pubmedgemini.answer_page import ratio_unit

    n = num(label, v, unit, None, "1", lo, hi)
    assert ratio_unit(n) is None and not _o_ratio(n)
    w = answer_with(n)
    f = forest(w, REFS)
    assert f.ratios == [] and f.diffs == []  # no row of any kind: listed, as before R1
    [c] = outcome_cards(w, REFS)
    assert [x.problem for x in c.listed] == ["unit not RR, OR or HR"]
    assert shown_at(w, REFS) == every_number(w)


def test_r1_refused_label_with_a_difference_marker_arm_is_not_a_difference_row():
    # "Hazard ratio, mean difference" names a ratio and a difference: neither is drawn
    n = num("Hazard ratio, mean difference", 0.35, None, "MD", "1", 0.2, 0.5)
    f = forest(answer_with(n), REFS)
    assert f.ratios == [] and f.diffs == []


@pytest.mark.parametrize("label", ["Relative risk reduction for recurrence",
                                   "Hazard ratio not reported; recurrence 12%",
                                   "Hazard ratio change from baseline", "Odds ratio (NS)"])
@pytest.mark.parametrize("unit", ["RR", "OR", "HR"])
def test_a_given_ratio_unit_keeps_the_existing_rule(label, unit):
    # The unit decides as before R1; only the draw checks (F4 and the value checks) apply.
    [row] = forest(answer_with(num(label, 0.35, unit, None, "1", 0.2, 0.5)), REFS).ratios
    assert row.drawn and row.problem is None


def test_unit_wins_over_label():
    from pubmedgemini.answer_page import ratio_unit

    assert ratio_unit(_hr_row("Hazard ratio for death", "OR")) == "OR"
    [row] = forest(answer_with(_hr_row("Hazard ratio for death", "OR")), REFS).ratios
    assert row.aria.startswith("Odds ratio") and row.estimate_text == "OR 0.7 (0.5–0.9)"
    for unit in ("%", "months", "aHR", "rr"):  # a unit that is not blank decides
        assert ratio_unit(_hr_row("Hazard ratio for death", unit)) is None, unit
        assert forest(answer_with(_hr_row("Hazard ratio for death", unit)), REFS).ratios == []


def test_one_sided_ratio_listed_no_interval():
    w = answer_with(_hr_row("Hazard ratio for progression", lo=None, hi=1.2),
                    _hr_row("Hazard ratio for death", lo=0.5, hi=None))
    assert forest(w, REFS).ratios == []
    [c] = outcome_cards(w, REFS)
    assert [(x.value_text, x.problem) for x in c.listed] == [
        ("0.7 (not given to 1.2)", "no interval"), ("0.7 (0.5 to not given)", "no interval")]


def test_r1_ratio_keeps_every_draw_check():
    rows = forest(answer_with(_hr_row("Hazard ratio for a", v=0.0, lo=0.0, hi=0.9),
                              _hr_row("Hazard ratio for b", v=0.7, lo=0.7, hi=0.7),
                              _hr_row("Hazard ratio for c", v=0.7, lo=0.9, hi=0.5),
                              _hr_row("Hazard ratio for d", v=2.0, lo=0.5, hi=0.9),
                              num("Hazard ratio for e", 0.7, None, None, "99999999", 0.5, 0.9),
                              _hr_row("Hazard ratio for f")), REFS).ratios
    assert [r.problem for r in rows] == [
        "a ratio must be above 0", "the interval has no width", "the interval is reversed",
        "the estimate is outside its interval", "its study is not in the references", None]


# ---------- S1: group letters named by the comparison (owner decision N6, 12 Nov 2026) ----

GROUP_CMP = "Short-course radiotherapy then chemotherapy (group A) vs chemoradiotherapy (group B)"


@pytest.mark.parametrize("arm,side", [("Group A", 0), ("group b", 1), (" GROUP  A ", 0),
                                      ("Group B", 1)])
def test_s1_group_letter_matches_its_side(arm, side):
    from pubmedgemini.answer_page import arm_side

    assert arm_side(arm, GROUP_CMP) == side


@pytest.mark.parametrize("arm,comparison", [
    ("Group A", "Drug X (group A) vs drug Y (group A)"),    # both sides name the letter
    ("Group C", GROUP_CMP),                                  # letter absent
    ("Group A", "Drug X vs drug Y"),                         # no group letters at all
    ("Group A", "Drug X group A vs drug Y group B"),         # the letter is not "(group A)"
    ("Group A (n=60)", GROUP_CMP),                           # not exactly "group <letter>"
    ("Groups A", GROUP_CMP), ("Group AB", GROUP_CMP), ("A", GROUP_CMP),
    ("Group A", "Drug X (group A) and drug Y (group B)"),   # not two-sided
])
def test_s1_near_misses_do_not_match(arm, comparison):
    from pubmedgemini.answer_page import arm_side

    assert arm_side(arm, comparison) is None


def test_s1_group_letters_get_squares():
    w = _pct_pair("Group A", "Group B", comparison=GROUP_CMP)
    [it] = out_of_100(w, REFS)
    assert [(c, a, s) for c, a, s in it.arms] == [(9, "Group A", "A"), (19, "Group B", "B")]
    w = _pct_pair("Group B", "Group A", comparison=GROUP_CMP)  # the writer's order kept
    assert [a for _, a, _ in out_of_100(w, REFS)[0].arms] == ["Group B", "Group A"]


@pytest.mark.parametrize("a,b,comparison", [
    ("Group A", "Group B", "Drug X vs drug Y"),                      # letters not named
    ("Group A", "Group A ", GROUP_CMP),                              # both the same side
    ("Group A", "Group C", GROUP_CMP),                               # one letter absent
    ("Group A", "Group B", "Drug X (group A) vs drug Y (group A)"),  # both sides name A
])
def test_s1_pairs_that_stay_text(a, b, comparison):
    w = _pct_pair(a, b, comparison=comparison)
    assert out_of_100(w, REFS) == []
    rows = [r for g in compare(w, REFS) for r in g.rows]
    assert all(r.problem is not None for r in rows) or not rows


# ---------- S2: experimental / standard / control named by the comparison (N6) ----------

EXP_CMP = "Experimental group (drug X then surgery) vs control"


@pytest.mark.parametrize("arm,comparison,side", [
    ("Control group", EXP_CMP, 1),        # the word score ties (group +1, control -1): S2 places it
    ("control", EXP_CMP, 1), ("Control arm", EXP_CMP, 1),
    ("Experimental group", EXP_CMP, 0),   # placed by the word score already
    ("Standard of care group", "Experimental group vs standard of care group", 1),
    ("Standard arm", "Experimental arm vs standard arm", 1),
])
def test_s2_role_word_matches_its_side(arm, comparison, side):
    from pubmedgemini.answer_page import arm_side

    assert arm_side(arm, comparison) == side


@pytest.mark.parametrize("arm,comparison", [
    ("Experimental", "Drug X then surgery vs surgery alone"),   # the word is not in it
    ("Control group", "Drug X group vs surgery alone"),          # tie, and no "control"
    ("Control group", "Control drug X group vs control"),        # the word on both sides
    ("Experimental control group", "Experimental group vs control"),  # both kinds
    ("Control group", "Control group vs drug X group"),          # control on the first side
    ("Intervention group", "Intervention group vs control group"),  # not the owner's word
    ("Experimental group A", "Experimental group vs control"),   # more than the role word
])
def test_s2_near_misses_do_not_match(arm, comparison):
    from pubmedgemini.answer_page import _key, _role_side, comparison_parts

    assert _role_side(_key(arm), comparison_parts(comparison)) is None


def test_s2_pair_gets_squares_only_with_the_words_in_the_comparison():
    w = _pct_pair("Experimental group", "Control group", comparison=EXP_CMP)
    [it] = out_of_100(w, REFS)
    assert [(a, s) for _, a, s in it.arms] == [("Experimental group", "A"), ("Control group", "B")]
    # the real answer's shape: the comparison names the treatments only, so no squares
    for a, b in [("Experimental group", "Standard of care group"), ("TNT group", "CRT group")]:
        w = _pct_pair(a, b, comparison="Short-course radiotherapy then chemotherapy vs "
                                       "long-course chemoradiotherapy")
        assert out_of_100(w, REFS) == []
        [row] = [r for g in compare(w, REFS) for r in g.rows]
        assert row.problem == NOT_MATCHED


def test_s2_both_arms_on_one_side_stay_text():
    w = _pct_pair("Control group", "Control arm", comparison=EXP_CMP)
    assert out_of_100(w, REFS) == []
    [row] = [r for g in compare(w, REFS) for r in g.rows]
    assert row.problem == NOT_MATCHED
