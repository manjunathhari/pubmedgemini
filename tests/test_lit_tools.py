import json
import logging

import httpx
import psycopg
import pytest
from conftest import make_tools
from lit_fakes import (
    EPMC_HOST,
    EPMC_SEARCH_PATH,
    EUTILS_HOST,
    EUTILS_PATH,
    FakeUpstream,
    europepmc_routes,
    pubmed_routes,
)

from pubmedgemini.lit import tools as tools_module
from pubmedgemini.lit.tools import (
    MAX_ABSTRACTS,
    MAX_RELATED,
    MAX_SEARCH_RESULTS,
    AbstractRecord,
    AbstractsResult,
    Item,
    SearchResult,
    ToolFailed,
    ToolInputError,
)

ID1, ID2, ID3 = "36262096", "33763744", "31696731"  # esearch.json, in its order
RETRACTED_ID = "38265557"  # in efetch.xml only, not in esummary.json
UNKNOWN_ID = "999999999"  # esummary.json answers it with `error`
ESEARCH = (EUTILS_HOST, EUTILS_PATH + "esearch.fcgi")
ESUMMARY = (EUTILS_HOST, EUTILS_PATH + "esummary.fcgi")
EFETCH = (EUTILS_HOST, EUTILS_PATH + "efetch.fcgi")
ELINK = (EUTILS_HOST, EUTILS_PATH + "elink.fcgi")
EPMC = (EPMC_HOST, EPMC_SEARCH_PATH)

FAILED_UNREACHABLE = "the database could not be reached; try again later or use another tool"
FAILED_NOTHING = "the database returned nothing usable for this input"
BAD_PMIDS = "`pmids` must be a list of 1 to 20 PubMed ids (digits only)"


def paths(up: FakeUpstream) -> list[str]:
    return [r.url.path.rsplit("/", 1)[-1] for r in up.sent]


def cached_rows(pool) -> int:
    with pool.connection() as conn:
        return conn.execute("SELECT count(*) FROM papers").fetchone()[0]


def tools_with(pool, override=None):
    up = FakeUpstream(pubmed_routes() | europepmc_routes() | (override or {}))
    return make_tools(pool, up), up


def test_caps_match_incisive():
    assert (MAX_SEARCH_RESULTS, MAX_ABSTRACTS, MAX_RELATED) == (50, 20, 30)


# ---------- search_pubmed ----------

def test_search_pubmed_json_shape(tools):
    d = json.loads(tools.search_pubmed("inguinal hernia mesh", 3).to_json())
    assert set(d) == {"count", "items"} and d["count"] == 5160
    assert [i["pmid"] for i in d["items"]] == [ID1, ID2, ID3]
    for item in d["items"]:  # no pmcid/doi from search_pubmed; retracted only when true
        assert set(item) == {"pmid", "title", "year", "journal", "pub_types"}


def test_search_pubmed_sends_the_trimmed_query_and_cap(tools, upstream):
    tools.search_pubmed("  inguinal hernia mesh  ", 7)
    p = upstream.sent[0].url.params
    assert (p["term"], p["retmax"]) == ("inguinal hernia mesh", "7")
    assert paths(upstream) == ["esearch.fcgi", "esummary.fcgi"]


def test_search_results_are_not_cached(tools, migrated_pool):
    tools.search_pubmed("inguinal hernia mesh", 3)
    assert cached_rows(migrated_pool) == 0  # a bare summary would shadow the abstract later


def test_search_pubmed_uses_cached_summaries(tools, upstream):
    tools.get_abstracts([ID1, ID2, ID3])
    upstream.sent.clear()
    tools.search_pubmed("inguinal hernia mesh", 3)
    assert paths(upstream) == ["esearch.fcgi"]


# ---------- search_europepmc ----------

def test_search_europepmc_items_carry_ids(tools):
    d = json.loads(tools.search_europepmc("inguinal hernia mesh", 10).to_json())
    assert d["count"] == 9672 and len(d["items"]) == 4
    preprint = d["items"][2]
    assert preprint["pmid"] is None and preprint["doi"] == "10.21203/rs.3.rs-9087670/v1"
    assert "pmcid" not in preprint or preprint["pmcid"]  # only when present
    assert d["items"][0]["doi"] == "10.1007/s00464-021-08442-w"


def test_search_europepmc_returns_at_most_max_results(tools, upstream):
    result = tools.search_europepmc("inguinal hernia mesh", 2)
    assert len(result.items) == 2 and upstream.sent[0].url.params["pageSize"] == "2"


def test_europepmc_takes_the_underscore_pubmed_does_not(tools, upstream):  # decision 6
    q = 'PUB_TYPE:"randomized controlled trial"'
    tools.search_europepmc(q, 5)
    with pytest.raises(ToolInputError) as e:
        tools.search_pubmed(q, 5)
    assert str(e.value).startswith("query rejected (character)")
    assert len(upstream.sent) == 1 and upstream.sent[0].url.host == EPMC_HOST


# ---------- inputs checked before any request ----------

@pytest.mark.parametrize("name", ["search_pubmed", "search_europepmc"])
@pytest.mark.parametrize("q", ["hernia?", "Is mesh better than suture?", "a ; b"])
def test_rejected_query_sends_nothing(tools, upstream, name, q):  # Review Focus 1
    with pytest.raises(ToolInputError) as e:
        getattr(tools, name)(q, 5)
    assert str(e.value).startswith("query rejected (character)") and upstream.sent == []
    assert e.value.kind == "input"
    assert ("_" in str(e.value)) == (name == "search_europepmc")


@pytest.mark.parametrize("q", ["", "   ", None, 5, ["hernia"]])
def test_query_must_be_a_non_empty_string(tools, upstream, q):
    with pytest.raises(ToolInputError) as e:
        tools.search_pubmed(q, 5)
    assert str(e.value) == "`query` must be a non-empty string" and upstream.sent == []


@pytest.mark.parametrize("n", [0, 51, True, "5", 2.5, 5.0, None, -1])
def test_max_results_out_of_range(tools, upstream, n):
    for name in ("search_pubmed", "search_europepmc"):
        with pytest.raises(ToolInputError) as e:
            getattr(tools, name)("hernia", n)
        assert str(e.value) == "`max_results` must be a whole number from 1 to 50"
    assert upstream.sent == []


def test_max_results_bounds_are_accepted(tools):
    tools.search_pubmed("hernia", 1)
    tools.search_pubmed("hernia", 50)


@pytest.mark.parametrize(
    "ids",
    [[], ["12a"], ["1234567890"], [str(i) for i in range(1, 22)], [""], [" "], [123], "123",
     None, ["12", "x"], ["-1"], ["1.5"], ["١٢"]],
)
def test_bad_pmids_rejected_before_any_request(tools, upstream, ids):  # Review Focus 5
    with pytest.raises(ToolInputError) as e:
        tools.get_abstracts(ids)
    assert str(e.value) == BAD_PMIDS and upstream.sent == []


def test_twenty_pmids_are_accepted(tools):
    result = tools.get_abstracts([str(i) for i in range(1, 21)])
    assert len(result.not_found) == 20


def test_input_errors_never_repeat_the_input(tools):
    for call in (lambda: tools.get_abstracts(["12zq7"]),
                 lambda: tools.find_related("zq7", 5),
                 lambda: tools.search_pubmed("zq7?", 5)):
        with pytest.raises(ToolInputError) as e:
            call()
        assert "zq7" not in str(e.value) + repr(e.value)


@pytest.mark.parametrize("pmid", ["", "12a", "1234567890", None, 123, " "])
def test_find_related_needs_a_valid_pmid(tools, upstream, pmid):
    with pytest.raises(ToolInputError) as e:
        tools.find_related(pmid, 5)
    expected = ("`pmid` must be a non-empty string" if not isinstance(pmid, str) or not pmid.strip()
                else "`pmid` must be a PubMed id (1 to 9 digits)")
    assert str(e.value) == expected and upstream.sent == []


@pytest.mark.parametrize("n", [0, 31, True])
def test_find_related_max_results_cap_is_30(tools, upstream, n):
    with pytest.raises(ToolInputError) as e:
        tools.find_related(ID1, n)
    assert str(e.value) == "`max_results` must be a whole number from 1 to 30" and upstream.sent == []


# ---------- get_abstracts ----------

def test_get_abstracts_second_call_hits_cache(tools, upstream):
    first = tools.get_abstracts([ID1, ID2, f" {ID1} "])
    n = len(upstream.sent)
    second = tools.get_abstracts([ID1, ID2])
    assert [p.pmid for p in first.papers] == [ID1, ID2]  # trimmed, repeat dropped
    assert first.papers[0].abstract and first.papers[0].abstract.startswith("BACKGROUND: ")
    assert first.cached == 0 and second.cached == 2 and len(upstream.sent) == n
    assert second.papers == first.papers


def test_get_abstracts_keeps_the_order_asked_cache_or_not(tools):
    asked = [ID3, ID1, ID2]
    assert [p.pmid for p in tools.get_abstracts(asked).papers] == asked
    # the cache returns rows in no set order; the result still follows the call
    for order in ([ID2, ID3, ID1], [ID1, ID2, ID3], [ID3, ID2, ID1]):
        result = tools.get_abstracts(order)
        assert result.cached == 3 and [p.pmid for p in result.papers] == order


def test_get_abstracts_mixed_cache_keeps_order(tools, upstream):
    tools.get_abstracts([ID2])
    upstream.sent.clear()
    result = tools.get_abstracts([ID3, ID2, ID1])
    assert [p.pmid for p in result.papers] == [ID3, ID2, ID1] and result.cached == 1
    assert upstream.sent[0].url.params["id"] == f"{ID3},{ID1}"  # only the missing ids


def test_get_abstracts_unknown_id_is_not_found(tools):
    result = tools.get_abstracts([UNKNOWN_ID, ID1, RETRACTED_ID])
    assert [p.pmid for p in result.papers] == [ID1]
    assert result.not_found == (UNKNOWN_ID, RETRACTED_ID)


def test_get_abstracts_json_shape(tools):
    d = json.loads(tools.get_abstracts([ID3, UNKNOWN_ID]).to_json())
    assert set(d) == {"papers", "not_found"} and d["not_found"] == [UNKNOWN_ID]
    [rec] = d["papers"]
    assert set(rec) == {"pmid", "title", "journal", "year", "abstract", "pub_types", "doi",
                        "pmcid", "retracted"}
    assert (rec["pmid"], rec["pmcid"], rec["doi"], rec["retracted"]) == (
        ID3, "PMC6996425", "10.1308/rcsann.2019.0137", False)


def test_only_ids_asked_for_are_returned_or_cached(migrated_pool):
    def esummary(request: httpx.Request) -> httpx.Response:
        body = {"result": {"uids": [ID1, "12345", "abc"],
                           ID1: {"uid": ID1, "title": "A"},
                           "12345": {"uid": "12345", "title": "Not asked"},
                           "abc": {"uid": "abc", "title": "Bad id"}}}
        return httpx.Response(200, json=body)

    tools, _ = tools_with(migrated_pool, {ESUMMARY: esummary})
    result = tools.get_abstracts([ID1])
    assert [p.pmid for p in result.papers] == [ID1]
    with migrated_pool.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT pmid FROM papers")] == [ID1]


def test_efetch_fills_abstract_and_retraction(migrated_pool):
    # esummary knows the retracted letter only from this test's reply; efetch marks it retracted
    def esummary(request: httpx.Request) -> httpx.Response:
        body = {"result": {"uids": [RETRACTED_ID],
                           RETRACTED_ID: {"uid": RETRACTED_ID, "title": "A letter"}}}
        return httpx.Response(200, json=body)

    tools, _ = tools_with(migrated_pool, {ESUMMARY: esummary})
    [p] = tools.get_abstracts([RETRACTED_ID]).papers
    assert p.retracted and p.pub_types  # pub types from efetch when esummary had none


def test_efetch_down_gives_titles_and_caches_nothing(migrated_pool, caplog):
    caplog.set_level(logging.INFO, logger="pubmedgemini")
    tools, _ = tools_with(migrated_pool, {EFETCH: httpx.Response(503)})
    result = tools.get_abstracts([ID1, ID2])
    assert [p.pmid for p in result.papers] == [ID1, ID2]
    assert all(p.abstract is None for p in result.papers) and p_titles(result)
    assert cached_rows(migrated_pool) == 0
    assert "lit tool=get_abstracts error_kind=http" in caplog.text


def p_titles(result: AbstractsResult) -> bool:
    return all(p.title for p in result.papers)


def test_esummary_down_with_nothing_cached_is_tool_failed(migrated_pool):
    tools, _ = tools_with(migrated_pool, {ESUMMARY: httpx.Response(503)})
    with pytest.raises(ToolFailed) as e:
        tools.get_abstracts([ID1])
    assert str(e.value) == FAILED_UNREACHABLE and e.value.kind == "failed"


def test_esummary_down_with_some_cached_returns_the_cached(migrated_pool, caplog):
    caplog.set_level(logging.INFO, logger="pubmedgemini")
    tools, _ = tools_with(migrated_pool)
    tools.get_abstracts([ID1])
    tools_down, _ = tools_with(migrated_pool, {ESUMMARY: httpx.Response(503)})
    result = tools_down.get_abstracts([ID2, ID1])
    assert [p.pmid for p in result.papers] == [ID1] and result.not_found == (ID2,)
    assert "lit tool=get_abstracts error_kind=http" in caplog.text


# ---------- the cache never fails a tool ----------

def test_cache_read_failure_is_a_miss(tools, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="pubmedgemini")

    def broken(*_a, **_k):
        raise psycopg.OperationalError("connection to server at 127.0.0.9 failed zq7")

    monkeypatch.setattr(tools_module, "get_many", broken)
    result = tools.get_abstracts([ID1])
    assert [p.pmid for p in result.papers] == [ID1] and result.cached == 0
    assert "lit kind=cache_read_failed" in caplog.text
    assert "zq7" not in caplog.text and "127.0.0.9" not in caplog.text


def test_cache_write_failure_never_fails_the_request(tools, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="pubmedgemini")

    def broken(*_a, **_k):
        raise psycopg.errors.CheckViolation("new row violates check constraint zq7")

    monkeypatch.setattr(tools_module, "put_many", broken)
    result = tools.get_abstracts([ID1, ID2])
    assert [p.pmid for p in result.papers] == [ID1, ID2] and result.papers[0].abstract
    assert "lit kind=cache_write_failed" in caplog.text and "zq7" not in caplog.text


def test_cache_write_on_a_closed_pool_never_fails(tools, migrated_pool):
    migrated_pool.close()  # get_many and put_many both raise
    result = tools.get_abstracts([ID1])
    assert [p.pmid for p in result.papers] == [ID1]


# ---------- upstream failures ----------

def test_upstream_down_is_tool_failed_with_fixed_text(migrated_pool):
    tools, up = tools_with(migrated_pool, {ESEARCH: httpx.Response(503),
                                             EPMC: httpx.Response(503)})
    for call in (lambda: tools.search_pubmed("hernia", 5),
                 lambda: tools.search_europepmc("hernia", 5)):
        with pytest.raises(ToolFailed) as e:
            call()
        assert str(e.value) == FAILED_UNREACHABLE and e.value.error_kind == "http"
    assert len(up.sent) == 6  # each retried twice (Task 2)


@pytest.mark.parametrize("reply", [httpx.Response(404), httpx.Response(200, content=b"{}")])
def test_not_found_or_parse_is_nothing_usable(migrated_pool, reply):
    tools, _ = tools_with(migrated_pool, {ESEARCH: reply})
    with pytest.raises(ToolFailed) as e:
        tools.search_pubmed("hernia", 5)
    assert str(e.value) == FAILED_NOTHING


def test_failed_text_holds_no_url_or_query(migrated_pool):
    tools, _ = tools_with(migrated_pool, {ESEARCH: httpx.Response(503)})
    with pytest.raises(ToolFailed) as e:
        tools.search_pubmed("zq7 hernia", 5)
    text = str(e.value) + repr(e.value)
    assert "zq7" not in text and "eutils" not in text and "https" not in text
    assert e.value.__cause__ is None and e.value.__suppress_context__


# ---------- find_related ----------

def elink_with(ids: list) -> httpx.Response:
    return httpx.Response(200, json={"linksets": [{"linksetdbs": [
        {"linkname": "pubmed_pubmed", "links": [{"id": i, "score": 1} for i in ids]}]}]})


def test_find_related_returns_neighbours_without_the_paper(migrated_pool):
    tools, up = tools_with(migrated_pool, {ELINK: elink_with([ID3, ID1, ID2, ID1])})
    result = tools.find_related(ID3, 30)
    assert [i.pmid for i in result.items] == [ID1, ID2] and result.count == 2
    assert set(json.loads(result.to_json())) == {"count", "items"}
    assert up.sent[0].url.params["id"] == ID3


def test_find_related_with_no_neighbours_asks_for_no_summaries(migrated_pool):
    tools, up = tools_with(migrated_pool, {ELINK: elink_with([])})
    result = tools.find_related(ID1, 5)
    assert (result.count, result.items, paths(up)) == (0, (), ["elink.fcgi"])


def test_find_related_fixture_neighbours_unknown_to_esummary(tools):
    # elink.json's neighbours are not in esummary.json: nothing usable is listed
    assert tools.find_related("31482863", 5).count == 0


# ---------- json shapes and logs ----------

def test_item_json_has_ids_and_retracted_only_when_present():
    plain = Item(pmid="1", title="t", year=None, journal=None, pub_types=())
    full = Item(pmid=None, title="t", year=2020, journal="J", pub_types=("Letter",),
                pmcid="PMC1", doi="10.1/x", retracted=True)
    d = json.loads(SearchResult(count=2, items=(plain, full)).to_json())
    assert d["items"][0] == {"pmid": "1", "title": "t", "year": None, "journal": None,
                             "pub_types": []}
    assert d["items"][1] == {"pmid": None, "title": "t", "year": 2020, "journal": "J",
                             "pub_types": ["Letter"], "pmcid": "PMC1", "doi": "10.1/x",
                             "retracted": True}


def test_abstract_record_json_keeps_nulls():
    rec = AbstractRecord(pmid="1", title="t", journal=None, year=None, abstract=None,
                         pub_types=(), doi=None, pmcid=None, retracted=False)
    d = json.loads(AbstractsResult(papers=(rec,), not_found=("2",), cached=1).to_json())
    assert d == {"papers": [{"pmid": "1", "title": "t", "journal": None, "year": None,
                             "abstract": None, "pub_types": [], "doi": None, "pmcid": None,
                             "retracted": False}], "not_found": ["2"]}


def test_log_lines_hold_counts_and_kinds_only(tools, caplog):
    caplog.set_level(logging.DEBUG)
    tools.search_pubmed("zq7 hernia", 3)
    tools.search_europepmc("zq7 hernia", 4)
    tools.get_abstracts([ID1, UNKNOWN_ID])
    tools.get_abstracts([ID1])
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("pubmedgemini")]
    assert lines == [
        "lit tool=search_pubmed count=5160 items=3",
        "lit tool=search_europepmc count=9672 items=4",
        "lit tool=get_abstracts asked=2 cached=0 returned=1",
        "lit tool=get_abstracts asked=1 cached=1 returned=1",
    ]
    assert "zq7" not in caplog.text and "Mesh repair" not in caplog.text


def test_failure_is_logged_with_its_kind_only(migrated_pool, caplog):
    caplog.set_level(logging.DEBUG)
    tools, _ = tools_with(migrated_pool, {EPMC: httpx.Response(429)})
    with pytest.raises(ToolFailed):
        tools.search_europepmc("zq7", 5)
    lines = [r.getMessage() for r in caplog.records if r.name.startswith("pubmedgemini")]
    assert lines == ["lit tool=search_europepmc error_kind=http"]
