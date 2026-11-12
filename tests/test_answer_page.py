"""Task 7 (F3): the answer page's derivation, pure functions over a stored answer.

Ported rules follow Incisive (commit 12630f7, `crates/incisive-app/src/answer_page.rs`,
`agent/answer_v3.rs` `arm_lines`); the tests named after Incisive's are ported from it. New
rules (the frozen mock's scales, draw checks and fold) are checked against the mock's own
numbers (`docs/mocks/f3-answer-page-v5.html`; its numbers are v4's).

HERNIA is the hand-written hernia answer (`tests/fixtures/writer/whole.json`), which follows
Incisive's rules; where the rules give rows the mock's example does not show, the mock's own
arithmetic is checked separately (coordinator ruling 1, 12 Nov 2026)."""

import dataclasses
import math

import pytest
from writer_fakes import fixture

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
from pubmedgemini.writer.answer import (
    AnswerBody,
    Applicability,
    BottomLine,
    Integrity,
    Number,
    Outcome,
    Reference,
    Study,
    WrittenAnswer,
    evidence_types,
    into_answer,
)
from pubmedgemini.writer.prompts import CERTAINTIES

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
    return Outcome(name, evidence, effect, numbers, certainty, why, [])


def study(pmid, comparison, label=None) -> Study:
    return Study(pmid, label or f"Trial {pmid}, 2020", "RCT", None, "", comparison, "", "")


def written(outcomes, studies=()) -> WrittenAnswer:
    return WrittenAnswer(BottomLine("x", "Low", ""), list(outcomes), list(studies), [],
                         Applicability("", "", ""), [], None, None, [])


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
    assert draw_problem(num("d", 0.0, lo=0.0, hi=0.0), "diff", REFS) is None
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
_LABELS = ["Pain", "MD", "Mean difference in pain", "Recurrence (no significant difference)",
           "Seroma RR", "Robotic minus lap", "Difference", "", "  ", "Ache at 2 y",
           "Risk difference", "Md. score", "MD Anderson Dysphagia Inventory score",
           "Difference in pain", "MD, days", "Pain\u200b", "\u202eRecurrence"]
_ARMS = [None, "", " ", " ", "MD", " md ", "SMD", "risk  difference", "TEP", "tep", "TEP ",
         "Lichtenstein", "MD Anderson protocol", "Md. Ali technique", "Drain minus",
         "TEP vs Lichtenstein", "POEM minus PD", "Robotic  TAPP", "robotic TAPP", "MD.",
         "M.D.", "Difference:", "ARR", "NNT", "difference vs laparoscopic", "\u202eTEP",
         "TEP\u200b", "\u200b", "\ufeffLichtenstein", "ＴＥＰ"]
_UNITS = [None, "", "%", " % ", "RR", "OR", "HR", "rr", "aHR", "days", "points", "MD"]
_PMIDS = ["1", "2", " 1 ", "", "  ", "99999999"]
_P = [None, "", "p", "P < .001", "0.03"]


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


def _rand_body(r) -> AnswerBody:
    outs = [outcome(f"Outcome {i}", [_rand_number(r) for _ in range(r.randint(0, 6))],
                    r.choice(CERTAINTIES)) for i in range(r.randint(1, 3))]
    studies = [study("1", r.choice(["TEP vs Lichtenstein", "MD Anderson protocol vs Standard",
                                    ""]))] if r.random() < 0.7 else []
    w = written(outs, studies)
    refs = [Reference(1, "1", "T1", "J", 2020, None, False),
            Reference(2, "2", "T2", None, None, None, False)]
    return AnswerBody(created_at="2026-11-12T00:00:00Z", answer=w, empty_reason=None,
                      references=refs, searches=[], integrity=Integrity(), stopped_early=None,
                      elapsed_s=1, notes=[])


def test_random_answers_keep_the_pages_promises():
    import random

    from pubmedgemini.answer_page import (
        arm_name,
        difference_measure,
        names_an_arm,
        out_of_100_groups,
        plain_arm,
    )

    r = random.Random(20261112)
    for case in range(3000):
        body = _rand_body(r)
        w, refs = body.answer, refs_of(body)
        answer_view(body)  # never raises
        assert shown_at(w, refs) == every_number(w), case  # every number is on the page
        f = forest(w, refs)
        for row in f.ratios + f.diffs:  # a drawn row never carries a reason
            assert (row.problem is None) == row.drawn, case
        for row in f.diffs:  # d24: no number with an arm name is a difference row
            assert not names_an_arm(w.outcomes[row.at[0]].numbers[row.at[1]]), case
        rows = [row for g in compare(w, refs) for row in g.rows]
        for row in rows:  # every compared value has its own arm name and a known study
            assert all(arm_name(w.outcomes[o].numbers[j]) for o, j in row.at), case
            assert all(rust_trim_pmid(w.outcomes[o].numbers[j]) in refs
                       for o, j in row.at), case
        items = out_of_100(w, refs)
        for it in items:
            assert all(name.strip() for _, name, _ in it.arms), case
        # each out-of-100 candidate is drawn or its compare row gives the reason, never both
        candidates = out_of_100_groups(w, all_groups(w, refs))
        assert len(items) + sum(row.problem is not None for row in rows) == len(candidates), case
        # third check M1: no drawn pair has an arm that is not a plain arm name
        for row in rows:
            assert all(plain_arm(w.outcomes[o].numbers[j]) for o, j in row.at), case
        for g in candidates:
            assert all(plain_arm(n) for n in g.nums(w)), case
        # third check M2: no difference row lacks a named measure
        for row in f.diffs:
            assert difference_measure(w.outcomes[row.at[0]].numbers[row.at[1]]), case
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
    (0.004, 0.004, 0.004, ("−0.001", "0", "+0.004")),
    (5.0, 5.0, 5.0, ("−1", "0", "+5")),
    (0.33, -0.03, 0.7, ("−0.2", "0", "+0.8")),
    (-4.74, -6.78, -2.7, ("−8", "0", "+2")),
    (-120.0, -120.0, 80.0, ("−150", "0", "+100")),
    (2.5e-5, 1e-5, 4e-5, ("−0.00001", "0", "+0.00004")),
])
def test_difference_axis_labels_take_their_precision_from_the_step(v, lo, hi, labels):
    [d] = forest(answer_with(num("Risk difference", v, None, None, lo=lo, hi=hi)), REFS).diffs
    assert d.drawn and (d.scale.lo_text, d.scale.mid_text, d.scale.hi_text) == labels
    assert 0 < d.null < 100  # 0 is never at an edge (X16)


def test_a_tiny_point_interval_whose_axis_would_put_0_at_an_edge_is_not_drawn():
    # 1e-9 (1e-9 to 1e-9): at the axis's 9-decimal rounding its lower edge is 0, so no axis
    # keeps 0 off the edge (X16): listed, not drawn on a wrong scale.
    [d] = forest(answer_with(num("Risk difference", 1e-9, None, None, lo=1e-9, hi=1e-9)),
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
