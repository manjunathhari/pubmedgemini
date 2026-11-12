"""Task 4 (F3): the answer's types, Incisive's reading of the writer's reply, the merge of the
four parts, the evidence labels, the merge notes and the stored JSON. Tests named after
Incisive's port its tests of the same name (`agent/writer_v3.rs` tests)."""

import json

import pytest
from writer_fakes import fixture, fixture_part, outcomes_part, whole

from pubmedgemini.gemini.client import Reply, Usage
from pubmedgemini.gemini.structured import Unreadable, read_structured
from pubmedgemini.researcher.loop import SearchEntry
from pubmedgemini.writer.answer import (
    EVIDENCE_LABELS,
    AnswerBody,
    AtAGlance,
    Integrity,
    KeyNumber,
    Outcome,
    Reference,
    body_from_json,
    body_to_json,
    cited_ids,
    design_class,
    evidence_types,
    into_answer,
    merge_checks,
    merge_sections,
    notable_line,
    parse_certainty,
    parse_glance,
    parse_practice,
    split_citations,
    typed_part,
    typed_whole,
)
from pubmedgemini.writer.prompts import CERTAINTIES, Section


def read(value) -> dict:
    """Through the real reader, as a reply would be (strict JSON text)."""
    r = Reply(finish="STOP", block_reason="", content=None, text=json.dumps(value), calls=(),
              usage=Usage(0, 0, 0))
    return read_structured(r, typed_whole)


def answer_of(value):
    return into_answer(read(value))


# ---------- the merge and the typing ----------


def test_four_parts_merge_into_the_single_writers_answer():
    merged = merge_sections({s: typed_part(s)(fixture_part(s.word)) for s in Section})
    single = answer_of(fixture("whole.json"))
    assert into_answer(merged) == single
    assert single[1] == 0
    # A part with another part's field: that field is not taken from it.
    extra = fixture_part("context") | {"bottom_line": {"text": "other", "certainty": "High",
                                                       "certainty_reason": ""}}
    parts = {s: typed_part(s)(fixture_part(s.word)) for s in Section}
    parts[Section.CONTEXT] = typed_part(Section.CONTEXT)(extra)
    assert into_answer(merge_sections(parts)) == single
    # A part missing a field is unreadable.
    assert typed_part(Section.CONTEXT)({"would_change": []}) is None


@pytest.mark.parametrize("bad", [{"value": "0.38"}, {"value": True}, {"pmid": 31482863},
                                 {"label": None}, {"unit": 3}, {"value": float("inf")}])
def test_wire_typing_follows_serde(bad):
    assert typed_part(Section.OUTCOMES)(outcomes_part(number_overrides=bad)) is None
    assert typed_part(Section.OUTCOMES)(outcomes_part(extra_key="x")) is not None


def test_optional_fields_may_be_missing_or_null():
    p = outcomes_part()
    n = p["outcomes"][0]["numbers"][0]
    for k in ("unit", "arm", "ci_low", "ci_high", "p"):
        n.pop(k)
    assert typed_part(Section.OUTCOMES)(p) is not None
    s = fixture_part("summary")
    s.pop("at_a_glance")
    s["clinical_practice"] = None
    assert typed_part(Section.SUMMARY)(s) is not None
    assert typed_part(Section.TRIALS)({"studies": [], "conflicts": []}) is not None


def test_a_duplicate_field_is_refused_as_serde_does():
    text = ('{"applicability": {"case_mix": "a", "case_mix": "b", "india_cost_context": "",'
            ' "training": ""}, "would_change": []}')
    r = Reply(finish="STOP", block_reason="", content=None, text=text, calls=(),
              usage=Usage(0, 0, 0))
    with pytest.raises(Unreadable):
        read_structured(r, typed_part(Section.CONTEXT))


def test_unknown_certainty_becomes_very_low_and_is_counted():
    a, defaulted = answer_of(whole(bottom_certainty="medium", outcome_certainty="VERY_LOW"))
    assert (a.bottom_line.certainty, a.outcomes[0].certainty, defaulted) == \
        ("Very low", "Very low", 1)
    assert [parse_certainty(x) for x in ("high", "Very low", "very_low", "MODERATE", "sure")] \
        == ["High", "Very low", "Very low", "Moderate", None]


def test_ci_bound_as_text_is_kept_in_the_label():
    a, _ = answer_of(whole(number={"ci_low": "0.28 to 0.51", "ci_high": "−0.12"}))
    n = a.outcomes[0].numbers[0]
    assert n.ci_low is None and n.ci_high == -0.12
    assert n.label.endswith(" (CI as written: 0.28 to 0.51)")


def test_the_text_fields_are_parsed_and_nothing_is_dropped():
    g = parse_glance(
        "Verdict: No patient benefit; costs more.\n- Cost: $3258 robotic vs $1421 laparoscopic "
        "[32186683]\n- Time: 75.5 vs 40.5 min [32186683]\nAn unlabelled line\nMain caveat: "
        "Small trials.")
    assert g.verdict == "No patient benefit; costs more."
    assert [(k.label, k.value_text) for k in g.key_numbers] == [
        ("Cost", "$3258 robotic vs $1421 laparoscopic [32186683]"),
        ("Time", "75.5 vs 40.5 min [32186683]"),
        ("", "An unlabelled line"),
    ]
    assert g.main_caveat == "Small trials."
    assert parse_glance("  \n ") is None
    assert parse_glance("Just one line").verdict == "Just one line"
    c = parse_practice("Default choice: Laparoscopic repair.\nWho may benefit: Not shown\nby the "
                       "trials.\nCOUNSELLING: Similar recovery [32186683].\nCost per case: not "
                       "known")
    assert c.default_choice == "Laparoscopic repair."
    assert c.who_may_benefit == "Not shown by the trials."
    assert c.counselling == "Similar recovery [32186683]."
    assert c.cost_per_case == "not known" and c.text == ""
    raw = parse_practice("No labels at all, but advice.")
    assert raw.text == "No labels at all, but advice." and raw.default_choice == ""
    assert parse_glance("Key numbers: 3 of 4").verdict == "Key numbers: 3 of 4"
    assert [(k.label, k.value_text) for k in parse_glance("v\nKey numbers: Pain: 4").key_numbers] \
        == [("Pain", "4")]


def _wire(**kw) -> dict:
    base = {
        "bottom_line": {"text": "t", "certainty": "Very low", "certainty_reason": "r"},
        "outcomes": [{
            "outcome": "Pain", "rct_evidence": "2 RCTs", "effect_text": "e",
            "numbers": [
                {"label": "RR", "value": 0.76, "unit": None, "arm": None, "ci_low": 0.49,
                 "ci_high": 1.17, "p": None, "pmid": "11111111"},
                {"label": "MD", "value": -0.34, "unit": "points", "arm": "robotic",
                 "ci_low": "−0.9", "ci_high": "about one", "p": "0.26",
                 "pmid": "11111111"},
            ],
            "certainty": "SURE", "why": "w", "pmids": ["11111111"],
        }],
        "studies": [
            {"pmid": "11111111", "label": "A", "design": "RCT", "n": None, "setting": "",
             "comparison": "", "key_results": "", "appraisal": ""},
            {"pmid": "22222222", "label": "B", "design": "RCT", "n": 60, "setting": "",
             "comparison": "", "key_results": "", "appraisal": ""},
        ],
        "conflicts": [],
        "applicability": {"case_mix": "", "india_cost_context": "", "training": ""},
        "would_change": [],
        "at_a_glance": "No benefit.\nCost: $3258 vs $1421 [11111111]\nMain caveat: small trials",
        "clinical_practice": "Default choice: lap\nCost per case: not known",
        "notable_findings": "- Withdrawals 2 vs 9 [11111111; 22222222].\n\n- Funding by the "
                            "maker [22222222].",
    }
    base.update(kw)
    return base


def test_the_wire_reply_is_checked_in_code_with_defaults():
    a, defaulted = answer_of(_wire())
    assert a.bottom_line.certainty == "Very low"
    o = a.outcomes[0]
    assert o.certainty == "Very low" and defaulted == 1 and o.evidence_type == "unknown"
    n = o.numbers
    assert (n[0].unit, n[0].ci_low, n[0].ci_high) == (None, 0.49, 1.17)
    assert (n[1].ci_low, n[1].ci_high) == (-0.9, None)
    assert n[1].label == "MD (CI as written: about one)"
    assert (a.studies[0].n, a.studies[1].n) == (None, 60)
    g = a.at_a_glance
    assert (g.verdict, len(g.key_numbers), g.main_caveat) == ("No benefit.", 1, "small trials")
    c = a.clinical_practice
    assert (c.default_choice, c.cost_per_case) == ("lap", "not known")
    assert [(x.text, x.pmids) for x in a.notable_findings] == [
        ("Withdrawals 2 vs 9 [11111111; 22222222].", ["11111111", "22222222"]),
        ("Funding by the maker [22222222].", ["22222222"]),
    ]
    old = {k: v for k, v in _wire(outcomes=[], studies=[]).items()
           if k not in ("at_a_glance", "clinical_practice", "notable_findings")}
    old["bottom_line"]["certainty"] = "Low"
    a, d = answer_of(old)
    assert (a.at_a_glance is None, a.notable_findings == [], d) == (True, True, 0)


@pytest.mark.parametrize("n,expected", [
    (None, None), (0, None), (-3, None), ("0", None), ("about 300", None), (" 300 ", 300),
    ("+12", 12), (300.0, None), (2**63, None), (True, None), ("1e3", None), (60, 60)])
def test_n_is_a_positive_whole_number_or_none(n, expected):
    w = _wire()
    w["studies"][1]["n"] = n
    assert answer_of(w)[0].studies[1].n == expected


@pytest.mark.parametrize("raw,expected,in_label", [
    (None, None, False), ("", None, False), ("  ", None, False), ("0.5", 0.5, False),
    ("–.5", -0.5, False), ("1e400", None, True), ("1_000", None, True),
    ("nan", None, True), (True, None, False), ({"x": 1}, None, False), (2, 2.0, False)])
def test_ci_bounds_as_rust_reads_them(raw, expected, in_label):
    w = _wire()
    w["outcomes"][0]["numbers"][0]["ci_low"] = raw
    n = answer_of(w)[0].outcomes[0].numbers[0]
    assert n.ci_low == expected
    assert ("CI as written" in n.label) == in_label


def test_blank_unit_arm_and_p_are_none():
    w = _wire()
    w["outcomes"][0]["numbers"][1].update({"unit": "  ", "arm": "", "p": " 　"})
    n = answer_of(w)[0].outcomes[0].numbers[1]
    assert (n.unit, n.arm, n.p) == (None, None, None)


def test_the_text_fields_may_come_as_objects_or_arrays():
    w = _wire(
        at_a_glance={"verdict": "v", "key_numbers": [{"label": "l", "value_text": "x",
                                                      "pmid": ""}], "main_caveat": "c"},
        clinical_practice={"default_choice": "d", "who_may_benefit": "w", "counselling": "c",
                           "cost_per_case": "not known"},
        notable_findings=["- a [11111111]", {"text": "b", "pmids": ["2"]}, {"x": 1}, "  "])
    a, _ = answer_of(w)
    assert a.at_a_glance == AtAGlance("v", [KeyNumber("l", "x", "")], "c")
    assert a.clinical_practice.default_choice == "d" and a.clinical_practice.text == ""
    assert [(x.text, x.pmids) for x in a.notable_findings] == [
        ("a [11111111]", ["11111111"]), ("b", ["2"]), ('{"x":1}', [])]
    a, _ = answer_of(_wire(at_a_glance={"verdict": 1}, notable_findings=7))
    assert a.at_a_glance.verdict == '{"verdict":1}'
    assert [x.text for x in a.notable_findings] == ["7"]


def test_lines_are_read_as_rust_reads_them():
    assert notable_line("• x [12345678]").pmids == ["12345678"]
    assert notable_line("   ") is None
    g = parse_glance("v\r\nCaveat: one\nMain caveat: two\n")
    assert (g.verdict, g.main_caveat, g.key_numbers) == ("v", "one two", [])


# ---------- evidence labels ----------


def test_evidence_types_are_derived_from_the_cited_designs():
    a, _ = answer_of(_wire())
    base = a.outcomes[0]

    def row(pmids, text):
        return Outcome(base.outcome, base.rct_evidence, text, [], base.certainty, base.why,
                       list(pmids))

    a.outcomes = [
        row(["11111111"], ""), row(["33333333"], ""),
        row(["11111111"], "and a pooled RR [33333333]"), row(["44444444"], ""),
        row(["99999999"], ""), row(["55555555"], ""), row(["11111111", "44444444"], ""),
        row(["11111111"], "pooled [33333333] and a cohort [44444444]"),
    ]
    labels = {"11111111": "RCT", "33333333": "SR_MA", "44444444": "other",
              "55555555": "found by the skeptic; not extracted"}
    evidence_types(a, lambda p: design_class(labels.get(p, "quoted by the skeptic")))
    assert [o.evidence_type for o in a.outcomes] == [
        "rct", "meta_analysis", "rcts_and_meta_analyses", "other", "unknown", "rct", "mixed",
        "mixed"]
    assert EVIDENCE_LABELS["rcts_and_meta_analyses"] == "RCTs and meta-analyses"
    assert EVIDENCE_LABELS["other"] == "other / non-randomised"
    assert EVIDENCE_LABELS["mixed"] == "mixed evidence" and EVIDENCE_LABELS["unknown"] is None
    assert [design_class(x) for x in (" rct ", "SR_MA", "Other", "cohort")] == \
        ["rct", "ma", "other", None]


def test_the_fixture_gets_its_labels_from_the_researchers_designs():
    a, _ = answer_of(fixture("whole.json"))
    designs = {f"9000000{i}": d for i, d in enumerate(
        ["SR_MA", "SR_MA", "SR_MA", "RCT", "RCT", "RCT", "RCT", "RCT"], start=1)}
    evidence_types(a, lambda p: design_class(designs.get(p, "")))
    assert [o.evidence_type for o in a.outcomes] == [
        "rcts_and_meta_analyses", "rct", "rcts_and_meta_analyses", "rct", "meta_analysis",
        "meta_analysis"]


# ---------- the merge checks ----------


def _written(bottom, outcomes, glance):
    a, _ = answer_of(_wire())
    a.bottom_line.certainty = bottom
    a.outcomes = [Outcome(name, "", "", [], c, "", [pmid]) for name, c, pmid in outcomes]
    a.at_a_glance = AtAGlance("v", [KeyNumber(lab, "x", "") for lab in glance], "")
    return a


def test_the_merge_checks_flag_and_never_change():
    p = "11111111"
    ok = _written("Low", [("Chronic pain", "Low", p), ("Operative time", "Moderate", p)],
                  ["Chronic pain at 3 months", "Time in theatre"])
    assert merge_checks(ok) == []
    high = _written("High", [("Pain", "Low", p), ("Cost", "Moderate", p)], [])
    before = json.dumps(body_to_json(_body(high)))
    n = merge_checks(high)
    assert len(n) == 1
    assert "certainty (High) is higher than any outcome's (highest: Moderate)" in n[0]
    assert json.dumps(body_to_json(_body(high))) == before
    assert merge_checks(_written("Moderate", [("Pain", "Moderate", p)], [])) == []
    assert merge_checks(_written("High", [], [])) == []
    cites = _written("Low", [("Pain", "Low", "33333333")], [])
    cites.outcomes[0].effect_text = "less [44444444]"
    n = merge_checks(cites)
    assert n == [("Consistency check (answer written in four parts): the outcome table cites 2 "
                 "studies the trials table does not list (PMID 33333333, 44444444).")]
    g = _written("Low", [("Chronic pain", "Low", p)], ["Recurrence", "Pain (VAS)"])
    n = merge_checks(g)
    assert n == [("Consistency check (answer written in four parts): At a glance names 1 "
                 "outcome not in the outcome table (“Recurrence”).")]


def test_the_fixture_merges_without_notes():
    a, _ = answer_of(fixture("whole.json"))
    assert merge_checks(a) == []


# ---------- citations ----------


def test_citations_split_as_incisive():
    assert split_citations("LOS 2 [1, 3] days [31482863; PMID: 33763744].", lambda p: False) == \
        [("text", "LOS 2 [1, 3] days "), ("cite", ["31482863", "33763744"]), ("text", ".")]
    assert split_citations("[1, 3]", lambda p: p == "3") == [("cite", ["1", "3"])]
    assert split_citations("x [PMIDs 12345 , 67890] [1234567890]", lambda p: False) == \
        [("text", "x "), ("cite", ["12345", "67890"]), ("text", " [1234567890]")]
    assert cited_ids("a [11111111] b [11111111; 22222222]") == ["11111111", "22222222"]


# ---------- the stored JSON ----------


def _body(a=None, **kw) -> AnswerBody:
    d = {"created_at": "2026-11-12T10:00:00Z", "answer": a, "empty_reason": None,
         "references": [Reference(1, "90000001", "T", "Hernia", 2025, None, False),
                        Reference(2, "90000002", "U", None, None, "10.1/x", True)],
         "searches": [SearchEntry("q", "PubMed", 5160)], "integrity": Integrity(labels_defaulted=1),
         "stopped_early": None, "elapsed_s": 95, "notes": ["n"]}
    d.update(kw)
    return AnswerBody(**d)


def test_body_round_trips_and_bad_stored_json_is_none():
    a, _ = answer_of(fixture("whole.json"))
    evidence_types(a, lambda p: "rct")
    for b in (_body(a), _body(None, empty_reason="why", stopped_early="time limit")):
        stored = json.loads(json.dumps(body_to_json(b)))
        assert body_from_json(stored) == b
    good = body_to_json(_body(a))
    assert good["version"] == 3 and good["mode"] == "quick"
    assert good["answer"]["outcomes"][0]["evidence_type"] == "rct"
    assert "text" not in good["answer"]["clinical_practice"]  # skipped when empty, as Incisive
    for bad in ({"answer": 3}, [], None, "x", {**good, "version": 2}, {**good, "mode": "full"},
                {**good, "elapsed_s": -1}, {**good, "references": [{"n": 1}]},
                {**good, "integrity": {}}, {**good, "notes": "n"}):
        obj = bad if not isinstance(bad, dict) or "version" in bad else {**good, **bad}
        assert body_from_json(obj) is None, bad
    broken = json.loads(json.dumps(good))
    broken["answer"]["outcomes"][0]["certainty"] = "medium"
    assert body_from_json(broken) is None
    broken = json.loads(json.dumps(good))
    broken["answer"]["outcomes"][0]["numbers"][0]["value"] = "0.38"
    assert body_from_json(broken) is None


def test_certainties_are_incisives():
    assert CERTAINTIES == ("High", "Moderate", "Low", "Very low")
