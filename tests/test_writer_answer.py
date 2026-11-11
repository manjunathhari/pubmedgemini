"""Task 5: the stored answer (AnswerV3), the references built by code and the citation check
(Incisive `agent/answer_v3.rs`, `a.rs:N`; `agent/writer_v3.rs`, `w.rs:N`; `agent/team.rs`,
`t.rs:N`; commit 12630f7).

The tests named after a Rust test are Incisive's own, one for one, inputs and expected values
unchanged (the Rust name in the comment). The rest are the plan's and the tests that pin each
place where Rust and Python could differ; their expected values were taken from Incisive's own
code run by a Rust probe (serde_json 1.0.154, regex 1.13.1, chrono 0.4.45: the versions in
Incisive's Cargo.lock)."""

import json
from datetime import UTC, datetime

import pytest

from pubmedgemini.gemini.structured import value_string
from pubmedgemini.researcher.loop import SearchEntry
from pubmedgemini.writer.answer import (
    ANSWER_VERSION,
    AnswerInvalid,
    AnswerV3,
    Applicability,
    AtAGlance,
    BottomLine,
    Certainty,
    Cite,
    ClinicalPractice,
    Conflict,
    EvidenceType,
    GuidelineV3,
    IntegrityV3,
    KeyNumber,
    NotableFinding,
    Number,
    Outcome,
    ReferenceV3,
    Span,
    Study,
    Text,
    WrittenAnswer,
    check_citations,
    citation_check_line,
    clean_citations,
    read_body,
    references_from,
    split_citations,
)
from pubmedgemini.writer.prompts import StudyInput

A = "41060645"  # a.rs:1952
B = "39979619"  # a.rs:1953


def known(p):  # a.rs:1955-1957
    return p == A or p == B


# ---------- a.rs tests, one for one ----------


def test_splits_markers_of_every_shape():
    # a.rs:1959-1980 splits_markers_of_every_shape
    s = split_citations(f"Pain was similar [{A}; {B}]. Cost [PMID: {A}], [{A},{B}] end", known)
    assert s == [
        Text("Pain was similar "),
        Cite((A, B)),
        Text(". Cost "),
        Cite((A,)),
        Text(", "),
        Cite((A, B)),
        Text(" end"),
    ]
    # Brackets that are not citations stay text.
    s = split_citations("95% CI [0.2 to 1.39] and [n=12] and [a]", known)
    assert all(isinstance(x, Text) for x in s), s


def test_short_bracket_numbers_stay_text():
    # a.rs:1982-1995 short_bracketed_numbers_stay_text (the plan's
    # test_short_bracket_numbers_stay_text: "LOS 2 [1, 3] days" unchanged)
    t = "LOS 2 [1, 3] days"
    assert clean_citations(t, known) == (t, 0)
    assert split_citations(t, known) == [Text(t)]
    # An unknown long id is a citation (and removed); a short one beside a selected study is too.
    c, n = clean_citations(f"x [99999999] y [{A}; 12]", known)
    assert c == f"x y [{A}]"
    assert n == 2


def test_unknown_long_ids_beside_short_numbers_are_removed():
    # a.rs:1997-2014 unknown_long_ids_beside_short_numbers_are_removed
    assert split_citations("x [12; 99999999] y", known) == [
        Text("x "),
        Cite(("12", "99999999")),
        Text(" y"),
    ]
    assert clean_citations("x [12; 99999999] y", known) == ("x [12] y", 1)
    assert clean_citations("x [1, 3; 88888888; 99999999] y", known) == ("x [1; 3] y", 2)


def test_clean_keeps_known_and_removes_unknown():
    # a.rs:2016-2026 clean_keeps_known_and_removes_unknown
    t, n = clean_citations(f"A [{A}; 99999999]. B [88888888]. C [{B}; {B}; {A}].", known)
    assert t == f"A [{A}]. B. C [{B}; {A}]."
    assert n == 2
    assert clean_citations("No markers here.", known) == ("No markers here.", 0)


# ---------- w.rs tests, one for one ----------


def answer() -> WrittenAnswer:
    # w.rs:2779-2838 answer()
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
            outcome="Pain",
            rct_evidence="two RCTs",
            effect_text="4 vs 5 [11111111]",
            numbers=[num("11111111"), num("99999999"), num(" 22222222 ")],
            certainty=Certainty.MODERATE,
            why="w",
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


def test_unknown_citations_numbers_and_rows_are_removed_and_counted():
    # w.rs:2840-2874 unknown_citations_numbers_and_rows_are_removed_and_counted
    a = answer()
    a.outcomes[0].numbers[0].label = "VAS [99999999]"
    a.outcomes[0].numbers[0].arm = "robotic [88888888]"
    i = IntegrityV3()
    check_citations(a, ["11111111", "22222222"], i)
    # Finding 7: number text fields are cleaned too.
    assert a.outcomes[0].numbers[0].label == "VAS"
    assert a.outcomes[0].numbers[0].arm == "robotic"
    assert a.bottom_line.text == "No difference [11111111]."
    assert a.bottom_line.certainty_reason == "small trials"
    assert a.would_change == ["a large RCT"]
    assert a.outcomes[0].pmids == ["11111111"]
    assert a.conflicts[0].pmids == ["22222222"]
    assert [n.pmid for n in a.outcomes[0].numbers] == ["11111111", "22222222"]
    assert len(a.studies) == 1
    assert a.studies[0].label == "Trial [11111111]"
    # Text markers 999, 888, 777 (the label of the row that is then dropped) and 444; ids 999
    # (outcome) and 555 (conflict). The repeated known 111 is de-duplicated, not counted.
    # A guideline citing an unknown study keeps its text; its id and marker go (2 more).
    assert a.guidelines[0].pmid is None
    assert a.guidelines[0].pmid_removed, "flagged for the page"
    assert a.guidelines[0].body == "A society"
    assert a.guidelines[0].statement == "Use either [11111111]."
    assert i.removed_citations == 10
    assert i.dropped_numbers == 1
    assert i.dropped_studies == 1


def test_the_new_fields_get_the_citation_check():
    # w.rs:2876-2927 the_new_fields_get_the_citation_check
    a = answer()
    a.guidelines.clear()
    a.at_a_glance = AtAGlance(
        verdict="No benefit [11111111; 99999999].",
        key_numbers=[
            KeyNumber(label="Pain", value_text="4 vs 5", pmid=" 11111111 "),
            KeyNumber(label="Cost", value_text="$3258 vs $1421", pmid="99999999"),
        ],
        main_caveat="Small trials [88888888].",
    )
    a.clinical_practice = ClinicalPractice(default_choice="Either [11111111].",
                                           who_may_benefit="unknown", counselling="say so",
                                           cost_per_case="not known [77777777]", text="")
    a.notable_findings = [NotableFinding(text="Withdrawals were 2 vs 9 [11111111].",
                                         pmids=["11111111", "66666666"], spans=[])]
    i = IntegrityV3()
    before = i.removed_citations
    check_citations(a, ["11111111", "22222222"], i)
    g = a.at_a_glance
    assert g.verdict == "No benefit [11111111]."
    assert g.main_caveat == "Small trials."
    assert g.key_numbers[0].pmid == "11111111"
    assert g.key_numbers[1].pmid == "", "unknown id goes, text stays"
    assert g.key_numbers[1].value_text == "$3258 vs $1421"
    assert a.clinical_practice.cost_per_case == "not known"
    assert len(a.notable_findings) == 1, "kept"
    assert a.notable_findings[0].pmids == ["11111111"]
    assert i.removed_citations >= before + 5, i.removed_citations


# ---------- the plan's tests and the Rust/Python differences ----------


def test_unicode_digits_follow_rust():
    # Rust's `\d` is Unicode Nd (Unicode 16) and an id's length is in UTF-8 bytes. Expected
    # values from the Rust probe running Incisive's clean_citations / split_citations.
    assert clean_citations("x [١٢٣] y [१२] z [١٢٣٤٥]", lambda p: False) == ("x y z", 3)
    assert split_citations("a [١٢] b", lambda p: False) == [Text("a [١٢] b")]
    # [١٢٣٤٥]: five Arabic-Indic digits, a citation of no selected study: removed and counted.
    a = answer()
    a.bottom_line.text = "Fewer [١٢٣٤٥]."
    i = IntegrityV3()
    check_citations(a, ["11111111", "22222222"], i)
    assert a.bottom_line.text == "Fewer."
    # A digit set Python 3.12's `re` lacks but Rust's regex (Unicode 16) has: U+1E4F0..U+1E4F9
    # (Nag Mundari). Two of them are 8 bytes, so a citation.
    assert clean_citations("x [\U0001e4f1\U0001e4f2] y", lambda p: False) == ("x y", 1)


def test_clean_trims_only_spaces_and_tabs_before_a_removed_marker():
    # a.rs:1917, 1935: `trim_end_matches([' ', '\t'])`. Expected values from the Rust probe.
    assert clean_citations("a  \t[99999]. b [99999] c\n[99999]", lambda p: False) == (
        "a. b  c\n", 3)
    assert clean_citations("[99999] start", lambda p: False) == (" start", 1)
    assert clean_citations("x [PMID: 11111111, PMIDs 11111111; 22222] y",
                           lambda p: p == "11111111") == ("x [11111111] y", 1)


def edge_answer() -> WrittenAnswer:
    def num(pmid):
        return Number(label="pain", value=4.0, unit=None, arm="robotic", ci_low=None,
                      ci_high=None, p="0.431", pmid=pmid)

    return WrittenAnswer(
        bottom_line=BottomLine(text="t", certainty=Certainty.LOW, certainty_reason="r"),
        outcomes=[Outcome(outcome="o", rct_evidence="e", effect_text="x",
                          numbers=[num("11111111\x1f"), num("11111111 ")],
                          certainty=Certainty.LOW, why="w",
                          pmids=["", "11111111\x1f", " 11111111 "])],
        studies=[],
        conflicts=[Conflict(text="c", pmids=[])],
        applicability=Applicability(case_mix="a", india_cost_context="b", training="c"),
        would_change=[],
        at_a_glance=AtAGlance(verdict="v", key_numbers=[
            KeyNumber(label="l", value_text="v", pmid="  "),
            KeyNumber(label="l", value_text="v", pmid="\x1f"),
        ], main_caveat="m"),
        guidelines=[GuidelineV3(pmid="  ", body="b", year=None, statement="s"),
                    GuidelineV3(pmid=None, body="b [12345]", year=None, statement="s")],
    )


def test_check_trims_ids_as_rust_and_sets_the_counts():
    # Rust `trim` is Unicode White_Space: U+2003 and U+2028 go, U+001F stays (Python's
    # `strip()` would take it). An empty id in `pmids` is counted as removed; an empty key
    # number or guideline id is not. The three counts are set, not added to; the other counts
    # are left alone. Expected values from the Rust probe running Incisive's check_citations.
    a = edge_answer()
    i = IntegrityV3(dropped_unseen=2, dropped_unfetched=1, removed_citations=50,
                    dropped_numbers=7, dropped_studies=7)
    check_citations(a, ["11111111"], i)
    assert [n.pmid for n in a.outcomes[0].numbers] == ["11111111"]
    assert a.outcomes[0].pmids == ["11111111"]
    assert [k.pmid for k in a.at_a_glance.key_numbers] == ["", ""]
    assert [(g.pmid, g.pmid_removed, g.body) for g in a.guidelines] == [
        (None, False, "b"), (None, False, "b")]
    assert i == IntegrityV3(dropped_unseen=2, dropped_unfetched=1, removed_citations=4,
                            dropped_numbers=1, dropped_studies=0)
    assert citation_check_line(i) == (
        "Citation check: removed 4 unknown citations, dropped 1 number and 0 trial rows")


def test_citation_check_line_words():
    # t.rs:649-669, word for word.
    assert citation_check_line(IntegrityV3()) == (
        "Citation check: every citation is one of the selected studies")
    assert citation_check_line(IntegrityV3(removed_citations=10, dropped_numbers=1,
                                           dropped_studies=1)) == (
        "Citation check: removed 10 unknown citations, dropped 1 number and 1 trial row")
    assert citation_check_line(IntegrityV3(removed_citations=1, dropped_numbers=2,
                                           dropped_studies=3)) == (
        "Citation check: removed 1 unknown citation, dropped 2 numbers and 3 trial rows")
    # Only the three citation counts decide the line.
    assert citation_check_line(IntegrityV3(dropped_unfetched=3, labels_defaulted=2)) == (
        "Citation check: every citation is one of the selected studies")


def test_references_are_built_by_code_from_the_records():
    # t.rs:450-462: numbered from 1 in selection order; pmid, title, journal, year, doi and
    # retracted from the fetched record (StudyInput); found_by_skeptic false. Nothing else of
    # the record (abstract, publication types, design, why) and nothing the model wrote.
    studies = [
        StudyInput(pmid="22222222", title="Second trial.", journal=None, year=None,
                   pub_types=("Review",), doi=None, retracted=True, abstract="x",
                   design="SR_MA", why="w"),
        StudyInput(pmid="11111111", title="First trial.", journal="Br J Surg", year=2025,
                   pub_types=(), doi="10.1093/bjs/znaf283", retracted=False, abstract=None,
                   design="RCT", why="w"),
    ]
    refs = references_from(studies)
    assert refs == [
        ReferenceV3(n=1, pmid="22222222", title="Second trial.", journal=None, year=None,
                    doi=None, retracted=True, found_by_skeptic=False),
        ReferenceV3(n=2, pmid="11111111", title="First trial.", journal="Br J Surg",
                    year=2025, doi="10.1093/bjs/znaf283", retracted=False,
                    found_by_skeptic=False),
    ]
    assert [r.model_dump(mode="json") for r in refs][1] == {
        "n": 2, "pmid": "11111111", "title": "First trial.", "journal": "Br J Surg",
        "year": 2025, "doi": "10.1093/bjs/znaf283", "retracted": False,
        "found_by_skeptic": False}
    assert references_from([]) == []


def quick_sample() -> AnswerV3:
    """Incisive's test `sample()` (a.rs:2137-2235, with `n` and `outcome` from a.rs:2069-2093)
    as a Quick answer: mode quick, no skeptic, no code-built notable findings (Quick has
    neither), created_at fixed, and the Quick-reachable values set (the probe's
    `sample_quick` makes the same changes on Incisive's own struct)."""

    def n(label, value, unit, arm, pmid):
        return Number(label=label, value=value, unit=unit, arm=arm, ci_low=None, ci_high=None,
                      p=None, pmid=pmid)

    numbers = [n("Time", 64.2, "min", "laparoscopic TEP", A),
               n("Time", 80.3, "min", "robotic TAPP", A)]
    numbers[0].ci_low = 0.5
    numbers[0].ci_high = 1e-7
    numbers[0].p = "<0.001"
    numbers[1].value = 80.0
    return AnswerV3(
        version=ANSWER_VERSION,
        question="Robotic vs lap?",
        mode="quick",
        created_at=datetime(2025, 11, 11, 10, 0, 0, tzinfo=UTC),
        answer=WrittenAnswer(
            bottom_line=BottomLine(text=f"No benefit; 37% fewer [{A}; 99999999].",
                                   certainty=Certainty.VERY_LOW, certainty_reason="small"),
            outcomes=[Outcome(outcome="Operative time", rct_evidence="ROGER",
                              effect_text="64.2 vs 80.3 min", numbers=numbers,
                              certainty=Certainty.VERY_LOW, why="w", pmids=[A],
                              evidence_type=EvidenceType.RCT)],
            studies=[Study(pmid=A, label="ROGER (2025)", design="RCT", n=None,
                           setting="Switzerland", comparison="Robotic TAPP vs laparoscopic TEP",
                           key_results=f"Longer [{A}]", appraisal="blinded")],
            conflicts=[],
            applicability=Applicability(case_mix="primary", india_cost_context="not known",
                                        training="none"),
            would_change=["a big RCT"],
            at_a_glance=AtAGlance(
                verdict="No patient benefit; costs more.",
                key_numbers=[KeyNumber(label="Cost",
                                       value_text="$3327 robotic vs $2732 laparoscopic",
                                       pmid=A)],
                main_caveat="Small trials."),
            clinical_practice=ClinicalPractice(default_choice="Laparoscopic repair.",
                                               who_may_benefit="Not shown.",
                                               counselling="Similar recovery.",
                                               cost_per_case="not known",
                                               text="Unlabelled line."),
            guidelines=[
                GuidelineV3(pmid=None, body="A society", year=2020, statement="Use either.",
                            pmid_removed=True),
                GuidelineV3(pmid=A, body="EHS", year=None, statement="Mesh.",
                            pmid_removed=False),
            ],
            notable_findings=[NotableFinding(text=f"Withdrawals 2 vs 9 [{A}].", pmids=[A],
                                             spans=[])],
        ),
        empty_reason=None,
        references=[ReferenceV3(n=1, pmid=A, title="ROGER trial.", journal=None, year=None,
                                doi=None, retracted=True, found_by_skeptic=False)],
        searches=[SearchEntry("robotic inguinal hernia", "PubMed", 37)],
        integrity=IntegrityV3(dropped_unfetched=1, removed_citations=2, labels_defaulted=1),
        stopped_early="time limit",
        cost_paise=0,
        elapsed_s=95,
        notes=["note"],
        untraced=[Span(field="bottom_line.text", start=12, len=3)],
        notable_code=[],
    )


# Typed from the Rust probe's output for `sample_quick` (Incisive's own structs through
# `serde_json::to_value(&answer)`, as `team.rs:176` stores it, then `Value::to_string`: keys in
# byte order, because serde_json's `Value` map is a BTreeMap without `preserve_order`).
EXPECTED_BODY = (
    '{"answer":{"applicability":{"case_mix":"primary","india_cost_context":"not known",'
    '"training":"none"},"at_a_glance":{"key_numbers":[{"label":"Cost","pmid":"41060645",'
    '"value_text":"$3327 robotic vs $2732 laparoscopic"}],"main_caveat":"Small trials.",'
    '"verdict":"No patient benefit; costs more."},"bottom_line":{"certainty":"Very low",'
    '"certainty_reason":"small","text":"No benefit; 37% fewer [41060645; 99999999]."},'
    '"clinical_practice":{"cost_per_case":"not known","counselling":"Similar recovery.",'
    '"default_choice":"Laparoscopic repair.","text":"Unlabelled line.",'
    '"who_may_benefit":"Not shown."},"conflicts":[],"guidelines":[{"body":"A society",'
    '"pmid":null,"pmid_removed":true,"statement":"Use either.","year":2020},{"body":"EHS",'
    '"pmid":"41060645","statement":"Mesh.","year":null}],"notable_findings":[{"pmids":'
    '["41060645"],"text":"Withdrawals 2 vs 9 [41060645]."}],"outcomes":[{"certainty":'
    '"Very low","effect_text":"64.2 vs 80.3 min","evidence_type":"rct","numbers":[{"arm":'
    '"laparoscopic TEP","ci_high":1e-7,"ci_low":0.5,"label":"Time","p":"<0.001","pmid":'
    '"41060645","unit":"min","value":64.2},{"arm":"robotic TAPP","ci_high":null,"ci_low":null,'
    '"label":"Time","p":null,"pmid":"41060645","unit":"min","value":80.0}],"outcome":'
    '"Operative time","pmids":["41060645"],"rct_evidence":"ROGER","why":"w"}],"studies":'
    '[{"appraisal":"blinded","comparison":"Robotic TAPP vs laparoscopic TEP","design":"RCT",'
    '"key_results":"Longer [41060645]","label":"ROGER (2025)","n":null,"pmid":"41060645",'
    '"setting":"Switzerland"}],"would_change":["a big RCT"]},"cost_paise":0,"created_at":'
    '"2025-11-11T10:00:00Z","elapsed_s":95,"empty_reason":null,"evidence":[],"integrity":'
    '{"dropped_numbers":0,"dropped_studies":0,"dropped_unfetched":1,"dropped_unseen":0,'
    '"labels_defaulted":1,"numbers_checked":0,"numbers_untraced":0,"numbers_unverified":0,'
    '"outcomes_unverified":0,"removed_citations":2},"mode":"quick","notable_code":[],"notes":'
    '["note"],"question":"Robotic vs lap?","references":[{"doi":null,"found_by_skeptic":false,'
    '"journal":null,"n":1,"pmid":"41060645","retracted":true,"title":"ROGER trial.",'
    '"year":null}],"searches":[{"hits":37,"query":"robotic inguinal hernia","source":'
    '"PubMed"}],"skeptic":null,"stopped_early":"time limit","untraced":[{"field":'
    '"bottom_line.text","len":3,"start":12}],"version":3}'
)


def test_body_serialises_as_serde():
    body = quick_sample().to_body()
    # The same JSON text, byte for byte, written as serde_json writes a Value (so 80.0 stays a
    # float, 95 an integer, 1e-7 is a number, and every key and absent value is as Rust's).
    assert value_string(body) == EXPECTED_BODY
    assert body == json.loads(EXPECTED_BODY)
    # Plain JSON (what psycopg's Jsonb sends) reads back to the same value.
    assert json.loads(json.dumps(body)) == body
    # Keys in serde's field order (struct declaration order; Postgres JSONB keeps no order).
    assert list(body) == ["version", "question", "mode", "created_at", "answer",
                          "empty_reason", "references", "searches", "integrity",
                          "stopped_early", "cost_paise", "elapsed_s", "skeptic", "evidence",
                          "notes", "untraced", "notable_code"]
    assert list(body["integrity"]) == [
        "dropped_unseen", "dropped_unfetched", "removed_citations", "dropped_numbers",
        "dropped_studies", "numbers_checked", "numbers_untraced", "numbers_unverified",
        "outcomes_unverified", "labels_defaulted"]
    assert list(body["answer"]) == [
        "bottom_line", "outcomes", "studies", "conflicts", "applicability", "would_change",
        "at_a_glance", "clinical_practice", "guidelines", "notable_findings"]
    # The stored body reads back to the same answer.
    assert AnswerV3.model_validate(body) == quick_sample()
    assert AnswerV3.model_validate(body).to_body() == body


def test_absent_values_as_serde():
    # skip_serializing_if: ClinicalPractice.text empty (a.rs:191), NotableFinding.spans empty
    # (a.rs:203), GuidelineV3.pmid_removed false (a.rs:157), IntegrityV3.numbers_completed 0
    # (a.rs:543), AnswerV3 coverage / completeness None and numbers_completed empty
    # (a.rs:932-943). Every other Option is written null, every other Vec as [].
    a = quick_sample()
    a.answer.clinical_practice.text = ""
    a.answer.guidelines[0].pmid_removed = False
    a.answer.notable_findings[0].spans = [Span(field="text", start=0, len=2)]
    a.integrity.numbers_completed = 2
    body = a.to_body()
    assert "text" not in body["answer"]["clinical_practice"]
    assert "pmid_removed" not in body["answer"]["guidelines"][0]
    assert body["answer"]["notable_findings"][0]["spans"] == [
        {"field": "text", "start": 0, "len": 2}]
    assert body["integrity"]["numbers_completed"] == 2
    for key in ("coverage", "completeness", "numbers_completed"):
        assert key not in body
    # An answer with nothing written: answer null, empty_reason set.
    a = quick_sample()
    a.answer = None
    a.empty_reason = "The selected studies' records could not be fetched, so no answer could be written."
    body = a.to_body()
    assert body["answer"] is None and body["empty_reason"].startswith("The selected")


def test_created_at_as_chrono():
    # chrono 0.4.45 `DateTime<Utc>` serialises RFC 3339 with `Z` and SecondsFormat::AutoSi:
    # no fraction, or 3, 6 or 9 digits (from the Rust probe; Python holds microseconds).
    def at(us):
        a = quick_sample()
        a.created_at = datetime(2025, 11, 11, 10, 0, 0, us, tzinfo=UTC)
        return a.to_body()["created_at"]

    assert at(0) == "2025-11-11T10:00:00Z"
    assert at(400000) == "2025-11-11T10:00:00.400Z"
    assert at(123456) == "2025-11-11T10:00:00.123456Z"
    assert at(120) == "2025-11-11T10:00:00.000120Z"
    with pytest.raises(AnswerInvalid):  # a naive time has no UTC offset to write
        AnswerV3.model_validate({**quick_sample().to_body(), "created_at": "2025-11-11T10:00:00"})


def test_enum_spellings_as_serde():
    # Certainty: variant names, VeryLow renamed "Very low" (a.rs:36-43); EvidenceType
    # rename_all snake_case (a.rs:96-112), default unknown; RunMode snake_case
    # (runs/mod.rs:33-39).
    assert [c.value for c in Certainty] == ["High", "Moderate", "Low", "Very low"]
    assert [e.value for e in EvidenceType] == [
        "rct", "rct_meta_analysis", "network_meta_analysis", "meta_analysis",
        "rcts_and_meta_analyses", "other", "observational", "mixed", "unknown"]
    assert quick_sample().to_body()["mode"] == "quick"
    o = Outcome(outcome="o", rct_evidence="e", effect_text="x", numbers=[],
                certainty=Certainty.HIGH, why="w", pmids=[])
    assert o.model_dump(mode="json")["evidence_type"] == "unknown"


def test_quick_answer_has_no_full_parts():
    # Quick never has a skeptic or evidence rows (t.rs:203-226 base()).
    body = quick_sample().to_body()
    with pytest.raises(AnswerInvalid):
        AnswerV3.model_validate({**body, "skeptic": {"verdict": "holds"}})
    with pytest.raises(AnswerInvalid):
        AnswerV3.model_validate({**body, "evidence": [{"pmid": A}]})
    with pytest.raises(AnswerInvalid):
        AnswerV3.model_validate({**body, "version": 2})


# ---------- Round 2: nothing private in an exception; non-finite numbers ----------

MARK = "PRIVATE-MARKER-7f3a"


def assert_no_marker(e: BaseException) -> None:
    assert e.__cause__ is None and e.__context__ is None
    assert e.__suppress_context__
    for text in (str(e), repr(e), repr(e.args), str(e.__dict__)):
        assert MARK not in text, text


@pytest.mark.parametrize("change, kind", [
    (lambda b: b.update(skeptic={"verdict": MARK}), "not_quick"),
    (lambda b: b.update(evidence=[{"pmid": MARK, "text": MARK}]), "not_quick"),
    (lambda b: b.update(mode=MARK), "not_quick"),
    (lambda b: b.update(version=2, question=MARK), "not_quick"),
    (lambda b: b["answer"]["bottom_line"].update(certainty=MARK), "invalid"),
    (lambda b: b.update(question=[MARK]), "invalid"),
    (lambda b: b.update(created_at=MARK), "invalid"),
    (lambda b: b["references"].append({"n": 2, "pmid": MARK}), "invalid"),
])
def test_reading_a_stored_body_never_raises_with_its_content(change, kind):
    body = quick_sample().to_body()
    change(body)
    with pytest.raises(AnswerInvalid) as caught:
        read_body(body)
    assert caught.value.kind == kind
    assert caught.value.args == (kind,)
    assert_no_marker(caught.value)
    # The model's own entry points route the same way.
    with pytest.raises(AnswerInvalid) as caught:
        AnswerV3.model_validate(body)
    assert_no_marker(caught.value)
    assert read_body(quick_sample().to_body()) == quick_sample()


def test_building_task_5_models_never_raises_with_their_content():
    with pytest.raises(AnswerInvalid) as caught:
        AnswerV3(question=MARK, created_at=datetime(2025, 11, 11, 10, 0), answer=None,
                 empty_reason=MARK, references=[], searches=[], integrity=IntegrityV3(),
                 stopped_early=None, elapsed_s=0)  # a naive time
    assert caught.value.kind == "invalid"
    assert_no_marker(caught.value)
    with pytest.raises(AnswerInvalid) as caught:
        ReferenceV3(n=1, pmid="1", title=[MARK], journal=None, year=None, doi=None,
                    retracted=False)
    assert_no_marker(caught.value)
    with pytest.raises(AnswerInvalid) as caught:
        references_from([StudyInput(pmid="1", title=None, journal=MARK, year=None, pub_types=(),
                                    doi=None, retracted=False, abstract=MARK, design="RCT",
                                    why=MARK)])
    assert_no_marker(caught.value)
    with pytest.raises(AnswerInvalid) as caught:
        IntegrityV3(removed_citations=MARK)
    assert_no_marker(caught.value)


def test_to_body_never_raises_with_its_content():
    a = quick_sample()
    a.answer.bottom_line.text = object()  # what no checked answer holds
    a.question = MARK
    with pytest.raises(AnswerInvalid) as caught:
        a.to_body()
    assert caught.value.kind == "unserialisable"
    assert_no_marker(caught.value)


@pytest.mark.parametrize("x", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_numbers_are_stored_as_null(x):
    # serde_json writes a non-finite f64 as null (Rust probe: `to_value(f64::INFINITY)`,
    # `f64::NAN` give Value::Null); JSON has no Infinity or NaN and Postgres refuses them.
    a = quick_sample()
    n = a.answer.outcomes[0].numbers[0]
    n.value, n.ci_low, n.ci_high = x, x, x
    body = a.to_body()
    assert body["answer"]["outcomes"][0]["numbers"][0]["value"] is None
    assert body["answer"]["outcomes"][0]["numbers"][0]["ci_low"] is None
    assert body["answer"]["outcomes"][0]["numbers"][0]["ci_high"] is None
    assert "Infinity" not in json.dumps(body) and "NaN" not in json.dumps(body)
    json.dumps(body, allow_nan=False)
    # Finite numbers are untouched.
    assert body["answer"]["outcomes"][0]["numbers"][1]["value"] == 80.0
