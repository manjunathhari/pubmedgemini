"""Task 6: the study list's schema, strict reading and checks (Incisive `agent/researcher.rs`
`study_list_schema`, `validate_capped`, `seen_pmids`, `is_empty_list`; `agent/llm.rs`
`read_structured`, `parse_strict`, `answer_shaped`, `parse_reply`)."""

import json

import pytest

from pubmedgemini.gemini.client import Reply, Usage, to_openapi
from pubmedgemini.lit.tools import AbstractRecord, AbstractsResult, Item, SearchResult
from pubmedgemini.researcher.prompts import wrap_source
from pubmedgemini.researcher.studylist import (
    DESIGNS,
    MAX_CONSIDERED,
    MAX_CONTRADICTIONS,
    MAX_NOTE,
    Unreadable,
    is_empty_list,
    read_study_list,
    seen_pmids,
    study_list_schema,
    validate,
)


def s(pmid, design, why="w"):
    return {"pmid": pmid, "design": design, "why": why}


def c(pmid, note):
    return {"pmid": pmid, "note": note}


def k(pmid, reason="r"):
    return {"pmid": pmid, "reason": reason}


def lst(selected=(), considered=(), contradictions=(), searches=()):
    return {"searches": list(searches), "selected": list(selected),
            "considered_not_selected": list(considered),
            "possible_contradictions": list(contradictions)}


def text_reply(text, finish="STOP"):
    return Reply(finish, "", {"role": "model", "parts": [{"text": text}]}, text, (),
                 Usage(10, 10, 0))


def item(pmid, title="t"):
    return Item(pmid=pmid, title=title, year=2020, journal="J", pub_types=())


def record(pmid):
    return AbstractRecord(pmid=pmid, title="t", journal=None, year=None, abstract="a",
                          pub_types=(), doi=None, pmcid=None, retracted=False)


def test_constants_are_incisives():
    assert DESIGNS == ("RCT", "SR_MA", "other")
    assert (MAX_NOTE, MAX_CONTRADICTIONS, MAX_CONSIDERED) == (500, 10, 30)


def test_schema_is_incisives_verbatim():  # researcher.rs study_list_schema
    pmid = {"type": "string",
            "description": "PubMed id, digits only, exactly as a tool returned it."}

    def obj(props, req):
        return {"type": "object", "additionalProperties": False, "properties": props,
                "required": req}

    assert study_list_schema() == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "searches": {"type": "array", "items": obj({
                "query": {"type": "string"},
                "source": {"type": "string", "description": "pubmed, europepmc or related"},
                "hits": {"type": "integer"},
            }, ["query", "source", "hits"])},
            "selected": {"type": "array", "items": obj({
                "pmid": pmid,
                "design": {"type": "string", "enum": ["RCT", "SR_MA", "other"]},
                "why": {"type": "string",
                        "description": "One line: why this study matters for the question."},
            }, ["pmid", "design", "why"])},
            "considered_not_selected": {"type": "array", "items": obj({
                "pmid": pmid,
                "reason": {"type": "string"},
            }, ["pmid", "reason"])},
            "possible_contradictions": {"type": "array", "items": obj({
                "pmid": pmid,
                "note": {"type": "string",
                         "description": "How it may contradict the majority view."},
            }, ["pmid", "note"])},
        },
        "required": ["searches", "selected", "considered_not_selected",
                     "possible_contradictions"],
    }


def test_schema_is_strict_and_lists_the_designs():  # Incisive schema_is_strict_and_lists_...
    sch = study_list_schema()
    assert sch["additionalProperties"] is False
    assert sch["properties"]["selected"]["items"]["properties"]["design"]["enum"] == \
        ["RCT", "SR_MA", "other"]
    assert "minimum" not in json.dumps(sch) and "maximum" not in json.dumps(sch)


def test_to_openapi_keeps_words_and_enums():  # plan Task 3, written here (it needs Task 6)
    sch = to_openapi(study_list_schema())
    assert "additionalProperties" not in json.dumps(sch) and sch["type"] == "OBJECT"
    assert sch["properties"]["selected"]["items"]["properties"]["design"]["enum"] == \
        ["RCT", "SR_MA", "other"]
    assert sch["required"] == ["searches", "selected", "considered_not_selected",
                               "possible_contradictions"]
    assert sch["properties"]["selected"]["items"]["properties"]["why"]["description"] == \
        "One line: why this study matters for the question."
    assert sch["properties"]["searches"]["items"]["properties"]["hits"]["type"] == "INTEGER"


# ---------- validate ----------

def test_incisive_validation_cases_carry_over():  # Review Focus 1; researcher.rs tests
    # unseen_and_malformed_pmids_are_dropped_and_counted
    v = validate(lst(selected=[s("111", "RCT"), s("999", "RCT"), s(" 222 ", "SR_MA"),
                               s("PMID 333", "other"), s("", "other"), s("111", "SR_MA")]),
                 {"111", "222"})
    assert [x.pmid for x in v.selected] == ["111", "222"]
    assert (v.dropped_unseen, v.duplicates) == (3, 1)
    assert v.selected[0].design == "RCT"  # the first mention wins
    # selection_is_capped_at_ten
    ids = [str(i) for i in range(100, 115)]
    v = validate(lst(selected=[s(i, "RCT") for i in ids]), set(ids))
    assert (len(v.selected), v.over_cap, v.selected[9].pmid) == (10, 5, "109")
    # contradictions_only_on_seen_studies
    v = validate(lst(contradictions=[c("111", "n" * 900), c("222", "x")]), {"111"})
    assert len(v.contradictions) == 1 and len(v.contradictions[0].note) == MAX_NOTE


def test_cap_is_a_parameter():  # validate_capped
    ids = [str(i) for i in range(100, 115)]
    v = validate(lst(selected=[s(i, "RCT") for i in ids]), set(ids), cap=12)
    assert (len(v.selected), v.over_cap) == (12, 3)


def test_ten_digits_and_non_ascii_digits_are_not_pmids():
    seen = {"1234567890", "١٢٣"}
    v = validate(lst(selected=[s("1234567890", "RCT"), s("١٢٣", "RCT")]), seen)
    assert v.selected == () and v.dropped_unseen == 2


def test_long_why_is_cut_and_kept_as_text():
    v = validate(lst(selected=[s("111", "RCT", why="<script>" + "w" * 5000)]), {"111"})
    assert len(v.selected[0].why) == 500 and v.selected[0].why.startswith("<script>")


def test_why_is_trimmed_before_the_cut():  # clip_chars: trim, then take
    v = validate(lst(selected=[s("111", "RCT", why="  why  ")]), {"111"})
    assert v.selected[0].why == "why"


def test_contradictions_capped_at_ten_seen_only():
    ids = [str(i) for i in range(100, 120)]
    v = validate(lst(contradictions=[c("999", "x")] + [c(i, "n") for i in ids]), set(ids))
    assert [x.pmid for x in v.contradictions] == ids[:MAX_CONTRADICTIONS]


def test_considered_are_checked_like_selected():
    seen = {str(i) for i in range(100, 150)} | {"111"}
    v = validate(lst(selected=[s("111", "RCT")],
                     considered=[k("111"), k("999"), k(" 101 ", " r " + "x" * 600), k("101")]
                     + [k(str(i)) for i in range(102, 150)]), seen)
    assert v.considered[0].pmid == "101" and len(v.considered[0].reason) == 500
    assert v.considered[0].reason.startswith("r x")
    assert "111" not in [x.pmid for x in v.considered] and "999" not in [x.pmid for x in v.considered]
    assert len(v.considered) == MAX_CONSIDERED


def test_is_empty_list():
    assert is_empty_list(lst())
    assert is_empty_list(lst(contradictions=[c("1", "x")]))
    assert not is_empty_list(lst(selected=[s("1", "RCT")]))
    assert not is_empty_list(lst(considered=[k("1")]))


# ---------- reading the reply ----------

def test_bad_design_makes_the_list_unreadable():  # serde: an unknown enum value fails it all
    with pytest.raises(Unreadable) as e:
        read_study_list(text_reply(json.dumps(lst(selected=[s("1", "cohort")]))))
    assert e.value.category == "shape"


def test_cut_off_list_is_never_read():
    with pytest.raises(Unreadable) as e:
        read_study_list(text_reply(json.dumps(lst()), finish="MAX_TOKENS"))
    assert e.value.category == "truncated"


def test_invalid_json_is_syntax():
    with pytest.raises(Unreadable) as e:
        read_study_list(text_reply("not json"))
    assert e.value.category == "syntax"
    with pytest.raises(Unreadable) as e:
        read_study_list(text_reply(""))
    assert e.value.category == "syntax"


@pytest.mark.parametrize("bad", [
    {"selected": []},                                             # keys missing
    lst() | {"selected": {}},                                     # not a list
    lst(selected=[{"pmid": 111, "design": "RCT", "why": "w"}]),   # pmid not a string
    lst(selected=[{"pmid": "1", "design": "RCT"}]),               # why missing
    lst(considered=[{"pmid": "1"}]),
    lst(contradictions=[{"pmid": "1", "note": None}]),
    lst(searches=[{"query": "q", "source": "pubmed", "hits": True}]),   # bool is not an int
    lst(searches=[{"query": "q", "source": "pubmed", "hits": 1.5}]),
    lst(selected=["111"]),
])
def test_shape_misses_are_shape(bad):
    with pytest.raises(Unreadable) as e:
        read_study_list(text_reply(json.dumps(bad)))
    assert e.value.category == "shape"


def test_unknown_keys_are_ignored():  # serde without deny_unknown_fields
    body = lst(selected=[s("1", "RCT") | {"extra": 1}]) | {"note": "x"}
    assert read_study_list(text_reply(json.dumps(body)))["selected"][0]["pmid"] == "1"


def test_whole_number_hits_float_is_an_integer():
    # serde_json reads 5.0 as a float, which an i64 field refuses.
    with pytest.raises(Unreadable):
        read_study_list(text_reply(json.dumps(lst(searches=[{"query": "q", "source": "p",
                                                             "hits": 5.0}]))))


def test_fenced_json_is_read():  # parse_strict
    body = json.dumps(lst(selected=[s("1", "RCT")]))
    assert read_study_list(text_reply(f"```json\n{body}\n```"))["selected"][0]["pmid"] == "1"
    assert read_study_list(text_reply(f"```\n{body}\n```"))["selected"][0]["pmid"] == "1"


def test_one_json_value_in_prose_is_read():  # parse_reply
    body = json.dumps(lst(selected=[s("1", "RCT")]))
    assert read_study_list(text_reply(f"Here it is: {body} done [1]"))["selected"][0]["pmid"] == "1"
    # Two lists: none is read. Incisive's `text_fault` reads the whole trimmed text, which is
    # not JSON, so the category is `syntax` (the plan's sketch said `shape`).
    with pytest.raises(Unreadable) as e:
        read_study_list(text_reply(f"one {body} two {body}"))
    assert e.value.category == "syntax"
    # Two lists as one JSON array: valid JSON of the wrong shape.
    with pytest.raises(Unreadable) as e:
        read_study_list(text_reply(f"[{body}, {body}]"))
    assert e.value.category == "shape"


def test_prose_value_that_is_not_a_list_is_skipped():
    body = json.dumps(lst(selected=[s("1", "RCT")]))
    got = read_study_list(text_reply(f'Note {{"a": 1}} then {body}'))
    assert got["selected"][0]["pmid"] == "1"


def test_value_cut_off_in_prose_stops_the_scan():
    body = json.dumps(lst(selected=[s("1", "RCT")]))
    with pytest.raises(Unreadable):
        read_study_list(text_reply(f"x {body[:-5]}"))


# ---------- seen PMIDs ----------

def test_seen_pmids_reads_f1_json_and_records_only():  # Review Focus 5
    r = [wrap_source("t1-1", SearchResult(count=2, items=(
            item("111"), item(None), item("222", title='c "pmid": "333"'))).to_json()),
         AbstractsResult(papers=(record("444"),), not_found=("555",), cached=0).to_json()]
    assert seen_pmids(r) == {"111", "222", "444"}


def test_seen_pmids_ignores_text_past_a_cut():
    full = SearchResult(count=2, items=(item("111"), item("222"))).to_json()
    assert seen_pmids([full[: full.index('"222"') - 3]]) == {"111"}


def test_seen_pmids_needs_the_f1_spacing():
    # Incisive's pattern is serde's compact `"pmid":"1"`; F1's JSON has a space.
    assert seen_pmids(['{"pmid":"111"}']) == set()
    assert seen_pmids(['{"pmid": "111"}']) == {"111"}
    assert seen_pmids(['{"pmid": "1234567890"}']) == set()
