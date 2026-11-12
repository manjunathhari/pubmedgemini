"""Task 4: reading the writer's replies and `into_answer` (Incisive `agent/writer_v3.rs`,
`w.rs:N` below; `agent/llm.rs` `read_structured`).

The first five tests are Incisive's own, one for one, inputs and expected values unchanged
(their Rust names in the comments). The rest are the plan's, and the tests that pin each place
where Rust and Python differ; their expected values were taken from Incisive's own code, run
by a Rust probe (serde_json 1.0.154, regex 1.13.1: the versions in Incisive's Cargo.lock)."""

import json

import pytest

from pubmedgemini.gemini.client import Reply, Usage
from pubmedgemini.gemini.structured import (
    Unreadable,
    f64_string,
    number,
    read_structured,
    value_string,
)
from pubmedgemini.writer.answer import (
    Applicability,
    AtAGlance,
    BottomLine,
    Certainty,
    Conflict,
    EvidenceType,
    GuidelineV3,
    KeyNumber,
    Number,
    Outcome,
    Study,
    WrittenAnswer,
)
from pubmedgemini.writer.prompts import Section
from pubmedgemini.writer.read import (
    as_context,
    as_outcomes,
    as_summary,
    as_trials,
    as_whole,
    cited_ids,
    design_class,
    early_answer,
    evidence_types,
    into_answer,
    merge_checks,
    merge_sections,
    opt_count,
    opt_number,
    parse_certainty,
    parse_glance,
    parse_practice,
    rust_lines,
)

LEAD = "Consistency check (answer written in four parts):"  # w.rs:2354


def text_reply(text, finish="STOP"):
    return Reply(finish, "", {"role": "model", "parts": [{"text": text}]}, text, (),
                 Usage(10, 10, 0))


def reads(text, typed=as_whole):
    return read_structured(text_reply(text), typed)


# ---------- fixtures, as Incisive's `tests` module (w.rs:2779-2838, 3972-4005, 4183-4218) ----


def answer() -> WrittenAnswer:
    def num(pmid):
        return Number(label="pain", value=4.0, unit=None, arm="robotic", ci_low=None,
                      ci_high=None, p="0.431", pmid=pmid)

    def study(pmid):
        return Study(pmid=pmid, label=f"Trial [{pmid}]", design="RCT", n=100, setting="s",
                     comparison="c", key_results="k", appraisal="a")

    return WrittenAnswer(
        bottom_line=BottomLine(text="No difference [11111111; 99999999].",
                               certainty=Certainty.LOW,
                               certainty_reason="small trials [88888888]"),
        outcomes=[Outcome(
            outcome="Pain", rct_evidence="two RCTs", effect_text="4 vs 5 [11111111]",
            numbers=[num("11111111"), num("99999999"), num(" 22222222 ")],
            certainty=Certainty.MODERATE, why="w",
            pmids=["11111111", "99999999", "11111111"],
            evidence_type=EvidenceType.UNKNOWN,
        )],
        studies=[study("11111111"), study("77777777")],
        conflicts=[Conflict(text="differs [22222222]", pmids=["22222222", "55555555"])],
        applicability=Applicability(case_mix="lean [11111111]", india_cost_context="unknown",
                                    training="t"),
        would_change=["a large RCT [44444444]"],
        at_a_glance=None,
        clinical_practice=None,
        guidelines=[GuidelineV3(pmid="66666666", body="A society [66666666]", year=2020,
                                statement="Use either [11111111].", pmid_removed=False)],
        notable_findings=[],
    )


def whole_reply() -> dict:
    """A whole single-writer reply (every live field, odd values included)."""
    return {
        "bottom_line": {"text": "No benefit [11111111].", "certainty": "moderate",
                        "certainty_reason": "two RCTs"},
        "outcomes": [{
            "outcome": "Chronic pain", "rct_evidence": "2 RCTs [11111111]",
            "effect_text": "RR 0.76 [11111111]",
            "numbers": [{"label": "RR", "value": 0.76, "unit": None, "arm": None,
                         "ci_low": "0.49", "ci_high": "about one", "p": None,
                         "pmid": "11111111"}],
            "certainty": "SURE", "why": "small [22222222]", "pmids": ["11111111"],
        }],
        "studies": [
            {"pmid": "11111111", "label": "A 2020", "design": "RCT", "n": "60", "setting": "s",
             "comparison": "c", "key_results": "k [11111111]", "appraisal": "a"},
            {"pmid": "22222222", "label": "B 2021", "design": "RCT", "n": 0, "setting": "",
             "comparison": "", "key_results": "", "appraisal": ""},
        ],
        "conflicts": [{"text": "B differs [22222222]", "pmids": ["22222222"]}],
        "applicability": {"case_mix": "simple", "india_cost_context": "not known",
                          "training": "none"},
        "would_change": ["A larger RCT"],
        "at_a_glance": "No benefit.\nChronic pain: RR 0.76 [11111111]\nMain caveat: small",
        "clinical_practice": "Default choice: lap\nCost per case: not known",
        "notable_findings": "- Withdrawals 2 vs 9 [11111111].\n- Funding [22222222].",
    }


def part_of(whole: dict, s: Section) -> dict:
    """The part of `whole` a section writes (its own fields only)."""
    return {f: whole[f] for f in s.fields}


def written(bottom, outcomes, glance) -> WrittenAnswer:
    a = answer()
    a.bottom_line.certainty = bottom
    template = a.outcomes[0]
    a.outcomes = [template.model_copy(update={
        "outcome": name, "certainty": c, "pmids": [pmid], "rct_evidence": "",
        "effect_text": "", "why": "", "numbers": [],
    }) for name, c, pmid in outcomes]
    a.at_a_glance = AtAGlance(
        verdict="v",
        key_numbers=[KeyNumber(label=label, value_text="x", pmid="") for label in glance],
        main_caveat="",
    )
    return a


# ---------- Incisive's tests, one for one ----------


def test_the_text_fields_are_parsed_and_nothing_is_dropped():  # w.rs:2972
    g = parse_glance(
        "Verdict: No patient benefit; costs more.\n- Cost: $3258 robotic vs $1421 laparoscopic "
        "[32186683]\n- Time: 75.5 vs 40.5 min [32186683]\nAn unlabelled line\nMain caveat: "
        "Small trials.")
    assert g.verdict == "No patient benefit; costs more."
    k = [(x.label, x.value_text) for x in g.key_numbers]
    assert k == [
        ("Cost", "$3258 robotic vs $1421 laparoscopic [32186683]"),
        ("Time", "75.5 vs 40.5 min [32186683]"),
        ("", "An unlabelled line"),
    ]
    assert g.main_caveat == "Small trials."
    assert parse_glance("  \n ") is None
    assert parse_glance("Just one line").verdict == "Just one line"

    c = parse_practice(
        "Default choice: Laparoscopic repair.\nWho may benefit: Not shown\nby the trials.\n"
        "COUNSELLING: Similar recovery [32186683].\nCost per case: not known")
    assert c.default_choice == "Laparoscopic repair."
    assert c.who_may_benefit == "Not shown by the trials.", "continuation kept"
    assert c.counselling == "Similar recovery [32186683]."
    assert c.cost_per_case == "not known"
    assert c.text == ""
    raw = parse_practice("No labels at all, but advice.")
    assert raw.text == "No labels at all, but advice.", "drawn as written"
    assert raw.default_choice == ""


def test_the_wire_reply_is_checked_in_code_with_defaults():  # w.rs:3024
    reply = {
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
    a, defaulted = into_answer(as_whole(reply))
    assert a.bottom_line.certainty == Certainty.VERY_LOW
    o = a.outcomes[0]
    assert o.certainty == Certainty.VERY_LOW, "unknown: the default"
    assert defaulted == 1
    assert o.evidence_type == EvidenceType.UNKNOWN, "left to code"
    n = o.numbers
    assert (n[0].unit, n[0].ci_low, n[0].ci_high) == (None, 0.49, 1.17)
    assert (n[1].ci_low, n[1].ci_high) == (-0.9, None)
    assert n[1].label == "MD (CI as written: about one)", "kept, never dropped"
    assert (a.studies[0].n, a.studies[1].n) == (None, 60)
    g = a.at_a_glance
    assert (g.verdict, len(g.key_numbers), g.main_caveat) == ("No benefit.", 1, "small trials")
    c = a.clinical_practice
    assert (c.default_choice, c.cost_per_case) == ("lap", "not known")
    nf = [(x.text, x.pmids) for x in a.notable_findings]
    assert nf == [
        ("Withdrawals 2 vs 9 [11111111; 22222222].", ["11111111", "22222222"]),
        ("Funding by the maker [22222222].", ["22222222"]),
    ]
    assert a.guidelines == [], "the writer no longer writes guidelines"
    # A Stage 3 reply (none of the new fields) still reads.
    old = {
        "bottom_line": {"text": "t", "certainty": "Low", "certainty_reason": "r"},
        "outcomes": [], "studies": [], "conflicts": [],
        "applicability": {"case_mix": "", "india_cost_context": "", "training": ""},
        "would_change": [],
    }
    a, d = into_answer(as_whole(old))
    assert (a.at_a_glance is None, a.notable_findings == [], d) == (True, True, 0)


def test_evidence_types_are_derived_from_the_cited_designs():  # w.rs:3123
    a = answer()
    base = a.outcomes[0]

    def row(pmids, text):
        return base.model_copy(update={"pmids": list(pmids), "effect_text": text})

    a.outcomes = [
        row(["11111111"], ""),
        row(["33333333"], ""),
        row(["11111111"], "and a pooled RR [33333333]"),
        row(["44444444"], ""),
        row(["99999999"], ""),
        row(["55555555"], ""),
        # RCT + other: a genuine mix.
        row(["11111111", "44444444"], ""),
        # RCT + meta-analysis + other: mixed too.
        row(["11111111"], "pooled [33333333] and a cohort [44444444]"),
    ]

    def design(p):
        return {
            "11111111": design_class("RCT"),
            "33333333": design_class("SR_MA"),
            "44444444": design_class("other"),
            "55555555": design_class("found by the skeptic; not extracted"),
        }.get(p, design_class("quoted by the skeptic"))

    evidence_types(a, design)
    got = [o.evidence_type for o in a.outcomes]
    assert got == [
        EvidenceType.RCT,
        EvidenceType.META_ANALYSIS,
        EvidenceType.RCTS_AND_META_ANALYSES,
        EvidenceType.OTHER,
        EvidenceType.UNKNOWN,
        EvidenceType.RCT,
        EvidenceType.MIXED,
        EvidenceType.MIXED,
    ]
    # Stage 4 final review: the labels as drawn.
    assert EvidenceType.RCTS_AND_META_ANALYSES.label() == "RCTs and meta-analyses"
    assert EvidenceType.OTHER.label() == "other / non-randomised"
    assert EvidenceType.MIXED.label() == "mixed evidence"
    assert EvidenceType.UNKNOWN.label() is None, "no label drawn"


def test_four_parts_merge_into_the_single_writers_answer():  # w.rs:4008
    whole = whole_reply()
    single = into_answer(as_whole(whole))
    merged = into_answer(merge_sections(
        as_trials(part_of(whole, Section.TRIALS)),
        as_outcomes(part_of(whole, Section.OUTCOMES)),
        as_context(part_of(whole, Section.CONTEXT)),
        as_summary(part_of(whole, Section.SUMMARY)),
    ))
    assert merged == single
    assert merged[1] == 1, "the unknown certainty, counted once"
    # A part with another part's field: that field is not taken from it.
    extra = part_of(whole, Section.CONTEXT)
    extra["bottom_line"] = {"text": "other", "certainty": "High", "certainty_reason": ""}
    c = as_context(extra)
    again = into_answer(merge_sections(
        as_trials(part_of(whole, Section.TRIALS)),
        as_outcomes(part_of(whole, Section.OUTCOMES)),
        c,
        as_summary(part_of(whole, Section.SUMMARY)),
    ))
    assert again == single
    # A part missing a field is unreadable.
    assert as_context({"would_change": []}) is None


def test_the_merge_checks_flag_and_never_change():  # w.rs:4221
    base = answer()
    p = base.studies[0].pmid
    # All consistent: no note.
    ok = written(Certainty.LOW,
                 [("Chronic pain", Certainty.LOW, p), ("Operative time", Certainty.MODERATE, p)],
                 ["Chronic pain at 3 months", "Time in theatre"])
    assert merge_checks(ok) == []
    # Summary certainty above every outcome's.
    high = written(Certainty.HIGH,
                   [("Pain", Certainty.LOW, p), ("Cost", Certainty.MODERATE, p)], [])
    before = high.model_copy(deep=True)
    n = merge_checks(high)
    assert len(n) == 1, n
    assert "certainty (High) is higher than any outcome's (highest: Moderate)" in n[0]
    assert high == before, "nothing rewritten"
    # Equal to the highest: no note; no outcomes: nothing to compare.
    assert merge_checks(written(Certainty.MODERATE, [("Pain", Certainty.MODERATE, p)], [])) == []
    assert merge_checks(written(Certainty.HIGH, [], [])) == []
    # An outcome citing a study the trials table does not list.
    cites = written(Certainty.LOW, [("Pain", Certainty.LOW, "33333333")], [])
    cites.outcomes[0].effect_text = "less [44444444]"
    n = merge_checks(cites)
    assert len(n) == 1
    assert ("cites 2 studies the trials table does not list (PMID 33333333, 44444444)"
            in n[0]), n[0]
    # At a glance naming an outcome the table does not have.
    g = written(Certainty.LOW, [("Chronic pain", Certainty.LOW, p)], ["Recurrence", "Pain (VAS)"])
    n = merge_checks(g)
    assert len(n) == 1
    assert ("At a glance names 1 outcome not in the outcome table (“Recurrence”)"
            in n[0]), n[0]


# ---------- the plan's tests ----------


def test_ci_written_as_text_stays_in_the_label():
    w = whole_reply()
    w["outcomes"][0]["numbers"][0].update(ci_low="0.4 to", ci_high=None)
    a, _ = into_answer(as_whole(w))
    n = a.outcomes[0].numbers[0]
    assert n.ci_low is None
    assert n.label.endswith("(CI as written: 0.4 to)")
    assert n.label == "RR (CI as written: 0.4 to)"
    assert n.value == 0.76


def test_lines_split_as_rust():
    # U+2028 and form feed are not line ends for Rust's `lines` (Python's splitlines splits on
    # both); "\r\n" is one line end; a lone "\r" at the very end stays (then trimmed away).
    g = parse_glance("Verdict\nCost: 1 Time: 2\x0cx\r\nMain caveat: c\r")
    assert g.verdict == "Verdict"
    assert [(k.label, k.value_text) for k in g.key_numbers] == [("Cost", "1 Time: 2\x0cx")]
    assert g.main_caveat == "c"
    assert rust_lines("a\r\nb\r\n") == ["a", "b"]
    assert rust_lines("a\rb\n\nc\r") == ["a\rb", "", "c\r"]
    assert rust_lines("") == [] and rust_lines("\n") == [""]


def test_early_answer_fields_equal_the_merged():
    whole = whole_reply()
    s = as_summary(part_of(whole, Section.SUMMARY))
    c = as_context(part_of(whole, Section.CONTEXT))
    merged, _ = into_answer(merge_sections(
        as_trials(part_of(whole, Section.TRIALS)), as_outcomes(part_of(whole, Section.OUTCOMES)),
        c, s))
    early = early_answer(s, c)
    for f in ("bottom_line", "at_a_glance", "clinical_practice", "applicability",
              "would_change"):
        assert getattr(early, f) == getattr(merged, f), f
    assert (early.outcomes, early.studies, early.conflicts, early.notable_findings) == (
        [], [], [], [])
    # Absent parts: every field empty, the certainty the default.
    none = early_answer(None, None)
    assert none.bottom_line == BottomLine(text="", certainty=Certainty.VERY_LOW,
                                          certainty_reason="")
    assert none.applicability == Applicability(case_mix="", india_cost_context="", training="")
    assert (none.at_a_glance, none.clinical_practice, none.would_change) == (None, None, [])


def test_cut_off_part_is_never_read():
    body = json.dumps(part_of(whole_reply(), Section.TRIALS))
    with pytest.raises(Unreadable) as e:
        read_structured(text_reply(body, finish="MAX_TOKENS"), as_trials)
    assert e.value.category == "truncated"
    assert read_structured(text_reply(body), as_trials) is not None


def test_value_as_string_is_shape():
    w = part_of(whole_reply(), Section.OUTCOMES)
    w["outcomes"][0]["numbers"][0]["value"] = "0.76"
    with pytest.raises(Unreadable) as e:
        reads(json.dumps(w), as_outcomes)
    assert e.value.category == "shape"  # serde's `data`
    w["outcomes"][0]["numbers"][0]["value"] = True
    with pytest.raises(Unreadable):
        reads(json.dumps(w), as_outcomes)


# ---------- the merge checks' notes, word for word ----------


def test_merge_check_notes_word_for_word():  # w.rs:2353-2420, texts from Incisive's code
    a = written(Certainty.HIGH, [("Pain", Certainty.LOW, "33333333"),
                                 ("Cost", Certainty.MODERATE, "11111111")],
                ["Recurrence", "Pain (VAS)", "Time"])
    a.outcomes[0].effect_text = "[44444444]"
    a.studies = a.studies[:1]
    assert merge_checks(a) == [
        (f"{LEAD} the outcome table cites 2 studies the trials table does not list (PMID "
         "33333333, 44444444)."),
        (f"{LEAD} At a glance names 2 outcomes not in the outcome table (\u201cRecurrence\u201d, "
         "\u201cTime\u201d)."),
        (f"{LEAD} the bottom line's certainty (High) is higher than any outcome's (highest: "
         "Moderate); both are shown as written."),
    ]
    one = written(Certainty.LOW, [("Pain", Certainty.LOW, "33333333")], ["Recurrence"])
    one.studies = one.studies[:1]
    assert merge_checks(one) == [
        (f"{LEAD} the outcome table cites 1 study the trials table does not list (PMID "
         "33333333)."),
        f"{LEAD} At a glance names 1 outcome not in the outcome table (\u201cRecurrence\u201d).",
    ]


# ---------- where Rust and Python differ, pinned to what Incisive does ----------


def test_numbers_are_parsed_as_serde_json_parses_them():
    # serde_json (no `float_roundtrip`) is not always correctly rounded: a 17-digit value
    # can read one place off Python's float. Incisive's value is the one kept.
    assert number("1.7795083120561395") == 1.7795083120561397
    assert float("1.7795083120561395") == 1.7795083120561395
    assert number("0.76") == 0.76 and number("-0.34") == -0.34 and number("75.5") == 75.5
    assert number("60") == 60 and isinstance(number("60"), int)
    assert number("60.0") == 60.0 and isinstance(number("60.0"), float)
    neg_zero = number("-0")
    assert neg_zero == 0 and isinstance(neg_zero, float) and str(neg_zero) == "-0.0"
    assert number("18446744073709551615") == 2**64 - 1
    assert number("18446744073709551616") == 1.8446744073709552e19
    assert repr(number("1e400")) == "OVERFLOW"
    w = part_of(whole_reply(), Section.OUTCOMES)
    w["outcomes"][0]["numbers"][0]["value"] = "@V@"
    text = json.dumps(w)
    a, _ = into_answer(merge_sections(
        as_trials({"studies": [], "conflicts": []}),
        reads(text.replace('"@V@"', "1.7795083120561395"), as_outcomes),
        as_context(part_of(whole_reply(), Section.CONTEXT)),
        as_summary(part_of(whole_reply(), Section.SUMMARY))))
    assert a.outcomes[0].numbers[0].value == 1.7795083120561397
    for bad, cat in (("1e400", "syntax"), ('"0.76"', "shape")):
        with pytest.raises(Unreadable) as e:
            reads(text.replace('"@V@"', bad), as_outcomes)
        assert e.value.category == cat


def test_ci_text_is_parsed_as_rusts_f64_parse():
    # A CI written as text goes through Rust's `str::parse::<f64>` (correctly rounded; sign,
    # ".5" and "5." allowed; no underscores, no other digits), after `−` and `–` become `-`.
    cases = {
        "+1.5": 1.5, " 1.5 ": 1.5, ".5": 0.5, "5.": 5.0, "-.5e1": -5.0, "–0.9": -0.9,
        "1.7795083120561395": 1.7795083120561395,
        "1_0": None, "inf": None, "infinity": None, "NaN": None, "1e400": None,
        "١.٥": None, "− 0.9": None, "0x10": None, "1e": None,
    }
    for text, want in cases.items():
        assert opt_number(text) == want, text
    assert opt_number(None) is None and opt_number(True) is None and opt_number(3) == 3.0
    w = whole_reply()
    w["outcomes"][0]["numbers"][0].update(ci_low="1_0", ci_high="− 0.9")
    n = into_answer(as_whole(w))[0].outcomes[0].numbers[0]
    assert n.label == "RR (CI as written: 1_0) (CI as written: − 0.9)"


def test_counts_are_read_as_rust_reads_them():
    cases = [("60", 60), ("+60", 60), (" 60 ", 60), ("6_0", None), ("60.0", None), (60.0, None),
             (True, None), ("٦٠", None), (2**63, None), ("-5", None), (0, None),
             (2**63 - 1, 2**63 - 1), (str(2**63), None), (-3, None), (None, None)]
    for v, want in cases:
        assert opt_count(v) == want, v


def test_certainty_words_as_rust():
    assert parse_certainty("very_low") == Certainty.VERY_LOW
    assert parse_certainty("VERY LOW") == Certainty.VERY_LOW
    assert parse_certainty("Very-Low!") == Certainty.VERY_LOW
    assert parse_certainty("MODERATE.") == Certainty.MODERATE
    assert parse_certainty("Hígh") is None  # only ASCII letters count: "hgh"
    assert parse_certainty("") is None and parse_certainty("lowish") is None


def test_glance_labels_as_rust():
    # `after_label` slices the original line by the label's byte length; a Kelvin sign
    # (U+212A) lowercases to ASCII "k", so the slice lands one character short of the colon
    # and the label is not taken off (Python's character slicing would take it off).
    g = parse_glance("v\nKey number: 5\nKey number: Pain: 3 [11111111]\nkey numbers: x")
    assert [(k.label, k.value_text) for k in g.key_numbers] == [
        ("Key number", "5"), ("Pain", "3 [11111111]"), ("", "x")]
    # A label of more than 80 bytes is not a label (é is 2 bytes: 40 of them are 80).
    g = parse_glance("v\n" + "a" * 81 + ": b\n" + "é" * 40 + ": c\n" + "é" * 41
                     + ": d\n  : e\nf:g")
    assert [(k.label, k.value_text) for k in g.key_numbers] == [
        ("", "a" * 81 + ": b"), ("é" * 40, "c"), ("", "é" * 41 + ": d"), ("", ": e"),
        ("", "f:g")]


def test_cited_ids_as_rusts_regex():
    # Rust's `\d` is any Unicode decimal digit (Unicode 16 too); an id's length is in bytes,
    # so three Devanagari digits (9 bytes) count as a PMID on their own.
    assert cited_ids("x [१२३]") == ["१२३"]
    assert cited_ids("[१२]") == ["१२"]
    assert cited_ids("x [\U00010d40\U00010d41]") == ["\U00010d40\U00010d41"]
    assert cited_ids("LOS 2 [1, 3] days") == []
    assert cited_ids("[PMID: 12345678; PMIDs 23456789]") == ["12345678", "23456789"]
    assert cited_ids("[1234][12345]") == ["12345"]
    assert cited_ids("[12345678;12345678]") == ["12345678"]
    assert cited_ids("[ 12345678 ]") == ["12345678"]
    # U+001F is whitespace to Python's `\s`, not to Rust's.
    assert cited_ids("[12345678\x1f]") == []


def test_outcome_words_as_rusts_is_alphanumeric():
    # Devanagari vowel signs are alphanumeric to Rust (Other_Alphabetic), not to Python:
    # "दवाई" is one 4-letter word to Rust, so the two names share it.
    g = written(Certainty.LOW, [("कुल दवाई", Certainty.LOW,
                                 "11111111")], ["दवाई समय"])
    g.studies = g.studies[:1]
    assert merge_checks(g) == []


def test_duplicate_keys_keep_the_last_as_incisive():
    # serde refuses a duplicate field when reading the text typed, then Incisive's lenient
    # pass reads the same value as a `Value`, where the last one wins.
    t = ('{"applicability":{"case_mix":"a","case_mix":"x","india_cost_context":"b",'
         '"training":"c"},"would_change":[]}')
    assert reads(t, as_context)["applicability"]["case_mix"] == "x"
    assert reads("x " + t, as_context)["applicability"]["case_mix"] == "x"
    # The replaced value was parsed too: one serde_json cannot build refuses the reply.
    bad = ('{"bottom_line":{"text":"t","certainty":"Low","certainty_reason":"r"},'
           '"clinical_practice":{"zz":"\\ud800"},"clinical_practice":"Default choice: x"}')
    with pytest.raises(Unreadable) as e:
        reads(bad, as_summary)
    assert e.value.category == "syntax"
    assert reads(bad.replace("\\ud800", "y"), as_summary)["clinical_practice"] == \
        "Default choice: x"


def test_a_struct_reads_from_an_array_as_serde():
    # serde's derived structs read an array field by field (`visit_seq`).
    assert reads('[["a","b","c"],["w"]]', as_context) == {
        "applicability": {"case_mix": "a", "india_cost_context": "b", "training": "c"},
        "would_change": ["w"]}
    s = reads('[["t","Low","r"]]', as_summary)
    assert s == {"bottom_line": {"text": "t", "certainty": "Low", "certainty_reason": "r"},
                 "at_a_glance": None, "clinical_practice": None}
    for bad in ('[["a","b","c"],[],1]', '[["a","b","c"]]'):
        with pytest.raises(Unreadable) as e:
            reads(bad, as_context)
        assert e.value.category == "shape"


def test_unknown_keys_are_skipped_unread_as_serde():
    ok = '{"applicability":{"case_mix":"a","india_cost_context":"b","training":"c"},' \
         '"would_change":[]%s}'
    # Under an unknown key serde never parses the value: a lone surrogate, a number out of
    # range or deep nesting there do not matter.
    for extra in (',"zz":"\\ud800"', ',"yy":1e400', ',"z":' + "[" * 300 + "]" * 300):
        assert reads(ok % extra, as_context)["applicability"]["case_mix"] == "a"
    # A lone surrogate in any key, or in a value that is read, refuses the reply.
    for bad in (',"\\ud800":1', ):
        with pytest.raises(Unreadable) as e:
            reads(ok % bad, as_context)
        assert e.value.category == "syntax"
    with pytest.raises(Unreadable):
        reads(ok.replace('"a"', '"\\ud800"') % "", as_context)
    # Nesting: serde_json lets 127 levels in (the part's object is one), not 128.
    deep = '{"bottom_line":{"text":"t","certainty":"Low","certainty_reason":"r"},' \
           '"at_a_glance":%s}'
    assert reads(deep % ("[" * 126 + "]" * 126), as_summary) is not None
    with pytest.raises(Unreadable) as e:
        reads(deep % ("[" * 127 + "]" * 127), as_summary)
    assert e.value.category == "syntax"


def test_a_value_that_is_not_text_is_kept_as_serdes_json():
    # `text_or`: a glance (or practice, or finding) given as an object that is not the typed
    # box is kept as serde_json writes it: keys in order, floats as serde writes them.
    w = whole_reply()
    w["at_a_glance"] = {"b": 1e20, "a": "é\n", "c": [0.1, 1e-7, 60.0, 5, -0.0]}
    w["clinical_practice"] = ["d", "w", "c", "k", "t"]
    w["notable_findings"] = [{"text": " t ", "pmids": ["1"]}, 7, "- line [12345678]",
                             {"x": 1}, True]
    a, _ = into_answer(as_whole(w))
    assert a.at_a_glance.verdict == '{"a":"é\\n","b":1e+20,"c":[0.1,1e-7,60.0,5,-0.0]}'
    assert a.clinical_practice.model_dump() == {
        "default_choice": "d", "who_may_benefit": "w", "counselling": "c", "cost_per_case": "k",
        "text": "t"}
    assert [(x.text, x.pmids) for x in a.notable_findings] == [
        (" t ", ["1"]), ("7", []), ("line [12345678]", ["12345678"]), ('{"x":1}', []),
        ("true", [])]
    w["notable_findings"] = 12.5
    assert [x.text for x in into_answer(as_whole(w))[0].notable_findings] == ["12.5"]
    w["at_a_glance"] = {"verdict": "v", "key_numbers": [], "main_caveat": "m", "x": 1}
    assert into_answer(as_whole(w))[0].at_a_glance == AtAGlance(verdict="v", key_numbers=[],
                                                                 main_caveat="m")


def test_value_string_and_float_format_as_serde_json():
    assert [f64_string(x) for x in (0.1, 1e-7, 1e-6, 1e-5, 1e15, 1e16, 1e20, 60.0, -0.0,
                                     0.0, 1.5e300, 123456.789, 5e-324)] == [
        "0.1", "1e-7", "1e-6", "0.00001", "1000000000000000.0", "1e+16", "1e+20", "60.0",
        "-0.0", "0.0", "1.5e+300", "123456.789", "5e-324"]
    assert value_string({"s": "\x7f\x1f/é\"", "a": None, "b": [True, False]}) == \
        '{"a":null,"b":[true,false],"s":"\x7f\\u001f/é\\""}'
