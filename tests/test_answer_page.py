"""Task 7 (F3): the answer page's derivation, pure functions over a stored answer.

Ported rules follow Incisive (commit 12630f7, `crates/incisive-app/src/answer_page.rs`,
`agent/answer_v3.rs` `arm_lines`); the tests named after Incisive's are ported from it. New
rules (the frozen mock's scales, draw checks and fold) are checked against the mock's own
numbers (`docs/mocks/f3-answer-page-v3.html`).

HERNIA is the hand-written hernia answer (`tests/fixtures/writer/whole.json`), which follows
Incisive's rules; where the rules give rows the mock's example does not show, the mock's own
arithmetic is checked separately (coordinator ruling 1, 12 Nov 2026)."""

import math

import pytest
from writer_fakes import fixture

from pubmedgemini.answer_page import (
    MIN_SHOWN,
    WEIGHTS,
    Item,
    Panel,
    all_groups,
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
    f = forest(answer_with(num("Pain, difference", 2.0, None, None, lo=1.0, hi=3.0)), REFS)
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
    assert [r.values_text for g in rows for r in g.rows] == ["140% vs 20%"]


def test_three_arm_groups_stay_in_compare():  # X20
    w = written([outcome("Pain", [num("Pain", 1.0, "%", "a"), num("Pain", 2.0, "%", "b"),
                                  num("Pain", 3.0, "%", "c")])])
    assert out_of_100(w, REFS) == []
    assert [r.values_text for g in compare(w, REFS) for r in g.rows] == ["1% vs 2% vs 3%"]


def test_out_of_100_takes_two_arm_percent_groups_only():  # Incisive's test, two arms (X20)
    p = lambda v, arm: num("Relief", v, "%", arm)
    w = written([
        outcome("Two arms", [p(44.0, "POEM"), p(30.0, "LHM")]),
        outcome("Three arms", [p(1.0, "a"), p(2.0, "b"), p(3.0, "c")]),
        outcome("Mixed units", [p(44.0, "POEM"), num("Relief", 30.0, "patients", "LHM")]),
        outcome("One arm", [p(44.0, "POEM")]),
        outcome("With a difference", [p(44.0, "POEM"), p(30.0, "LHM"),
                                      p(14.0, "risk difference")]),
    ])
    assert [it.label for it in out_of_100(w, REFS)] == ["Relief"]


def test_an_arm_without_a_name_is_numbered():
    w = written([outcome("Pain", [num("Pain", 1.0, "%", "TEP"), num("Pain", 2.0, "%", None)])])
    assert [a for _, a, _ in out_of_100(w, REFS)[0].arms] == ["TEP", "arm 2"]


# ---------- compare the arms ----------

def test_compare_groups_by_normalised_comparison():
    pair = lambda pmid: [num("x", 1.0, None, "a", pmid), num("x", 2.0, None, "b", pmid)]
    w = written([outcome("A", pair("11111")), outcome("B", pair("22222")),
                 outcome("C", pair("33333"))],
                [study("11111", "TEP vs Lichtenstein (open)"),
                 study("22222", "tep vs  lichtenstein")])
    g = compare(w, REFS)
    assert [x.label for x in g] == ["TEP vs Lichtenstein (open)", "Other"]
    assert [len(x.rows) for x in g] == [2, 1]
    assert pieces_text(g[0].rows[0].study_pieces) == "Trial 11111 [1]"
    assert pieces_text(g[1].rows[0].study_pieces) == "PMID 33333 (not in the study list)"


def test_compare_rows_against_the_mock():
    g = compare(HERNIA.answer, HERNIA_REFS)
    assert g[0].label == "TEP vs Lichtenstein"
    r = g[0].rows[0]
    assert (r.label, r.certainty, r.values_text, r.p_text) == (
        "Chronic pain at 5 years", "Moderate", "9.4% vs 18.8%", "p <0.001")
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
    assert chart_kind([num("Robotic minus lap", 4.0, "min", lo=1.0, hi=7.0)]) == "diff"
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
    g = all_groups(w)
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
    # The mock's counselling pair: two out-of-100 items beside four compare rows in two
    # comparisons; no fold.
    out = Panel(WEIGHTS["out100"].head, [Item(WEIGHTS["out100"].item)] * 2)
    cmp = Panel(WEIGHTS["compare"].head,
                [Item(7, "TEP vs Lichtenstein", 4), Item(7), Item(7),
                 Item(7, "TAPP vs Lichtenstein", 4)])
    assert fold_pair(out, cmp) == (2, 4)


def test_fold_arithmetic_is_the_plans():
    f, o = forest_panel(HERNIA), outcomes_panel(HERNIA)
    assert (f.total(), o.total()) == (65, 81)
    out, cmp = out100_panel(HERNIA), compare_panel(HERNIA)
    assert (out.total(), len(out.items)) == (6 + 5 * 24, 5)
    assert cmp.total() == 6 + 2 * 4 + 5 * 7
    assert fold_pair(out, cmp) == (MIN_SHOWN, 5)  # 6 + 24k <= 49: k = 1, at least 2


def test_fold_keeps_at_least_two_and_folds_the_left_too():
    left = Panel(7, [Item(10, "Ratios (RR · OR · HR)", 4)] + [Item(10)] * 11)
    right = Panel(3, [Item(13)] * 2)
    assert fold_pair(left, right) == (2, 2)
    right = Panel(3, [Item(13)] * 4)  # 55: 7 + 4 + 10k <= 55 -> k = 4
    assert fold_pair(left, right) == (4, 4)


def test_fold_with_an_empty_panel_or_equal_totals_is_none():
    assert fold_pair(Panel(7, []), Panel(3, [Item(13)] * 9)) == (0, 9)
    assert fold_pair(Panel(3, [Item(13)] * 9), Panel(6, [])) == (9, 0)
    assert fold_pair(Panel(3, [Item(13)] * 2), Panel(3, [Item(13)] * 2)) == (2, 2)


def test_fold_puts_a_group_heading_inside_only_when_the_group_starts_there():
    left = Panel(7, [Item(10, "Ratios", 4), Item(10), Item(10), Item(10, "Differences", 4),
                     Item(10)])
    right = Panel(3, [Item(13)] * 3)  # 42: 7 + 4 + 10 + 10 + 10 = 41 -> 3 shown
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
