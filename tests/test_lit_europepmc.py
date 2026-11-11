import json

import httpx
import pytest
from lit_fakes import EPMC_HOST, EPMC_SEARCH_PATH, FakeUpstream, europepmc_routes, fixture

from pubmedgemini.lit.europepmc import EpmcHits, EuropePmc, strip_markup, to_paper
from pubmedgemini.lit.http import LitClient, LitError
from pubmedgemini.lit.paper import Paper, merge_results
from pubmedgemini.lit.query import validate_query

SHARED_ID = "33763744"  # in esummary.json too


def fake_europepmc() -> FakeUpstream:
    return FakeUpstream(europepmc_routes())


def search(q="inguinal hernia mesh", n=4) -> EpmcHits:
    return EuropePmc(LitClient(fake_europepmc().transport)).search(q, n)


def paper(**kw) -> Paper:
    base = {"pmid": None, "pmcid": None, "doi": None, "title": "t", "authors": [], "journal": None,
            "year": None, "pub_types": [], "abstract": None, "retracted": False, "source": "pubmed"}
    return Paper(**(base | kw))


# ---------- requests ----------

def test_europepmc_filter_query_passes_and_is_sent_encoded():  # decision 6
    q = 'PUB_TYPE:"randomized controlled trial" AND PUB_YEAR:[2015 TO 2026]'
    assert validate_query(q, "europepmc") is None
    up = fake_europepmc()
    EuropePmc(LitClient(up.transport)).search(q, 5)
    r = up.sent[0]
    assert (r.url.scheme, r.url.host, r.url.path) == ("https", EPMC_HOST, EPMC_SEARCH_PATH)
    assert r.url.params["query"] == q
    assert (r.url.params["resultType"], r.url.params["format"], r.url.params["pageSize"]) == (
        "core", "json", "5")
    raw = r.url.query.decode()
    assert all(ch not in raw for ch in ' "[]') and "PUB_TYPE" in raw


@pytest.mark.parametrize(("asked", "sent"), [(0, "1"), (1, "1"), (100, "100"), (500, "100")])
def test_page_size_is_clamped(asked, sent):
    up = fake_europepmc()
    EuropePmc(LitClient(up.transport)).search("mesh", asked)
    assert up.sent[0].url.params["pageSize"] == sent


def test_upstream_down_is_lit_error():
    up = FakeUpstream({(EPMC_HOST, EPMC_SEARCH_PATH): httpx.Response(503)})
    with pytest.raises(LitError) as e:
        EuropePmc(LitClient(up.transport, sleep=lambda s: None)).search("mesh", 5)
    assert (e.value.kind, e.value.status, len(up.sent)) == ("http", 503, 3)


@pytest.mark.parametrize("body", [{}, {"hitCount": 3}, [], {"resultList": None}])
def test_reply_without_result_list_is_parse(body):
    up = FakeUpstream({(EPMC_HOST, EPMC_SEARCH_PATH): httpx.Response(200, json=body)})
    with pytest.raises(LitError) as e:
        EuropePmc(LitClient(up.transport)).search("mesh", 5)
    assert e.value.kind == "parse"


def test_count_falls_back_to_the_page_length():
    body = {"resultList": {"result": [{"id": "1", "source": "MED", "title": "A"}]}}
    up = FakeUpstream({(EPMC_HOST, EPMC_SEARCH_PATH): httpx.Response(200, json=body)})
    hits = EuropePmc(LitClient(up.transport)).search("mesh", 5)
    assert hits.count == 1 and hits.papers[0].pmid == "1"
    body = {"hitCount": 0, "resultList": {}}
    up = FakeUpstream({(EPMC_HOST, EPMC_SEARCH_PATH): httpx.Response(200, json=body)})
    assert EuropePmc(LitClient(up.transport)).search("mesh", 5) == EpmcHits(count=0, papers=())


# ---------- the recorded reply ----------

def test_core_fixture_to_paper():
    hits = search()
    assert hits.count == 9672 and len(hits.papers) == 4
    shared, med, preprint, rct = hits.papers
    assert (shared.pmid, shared.doi, shared.journal, shared.year) == (
        SHARED_ID, "10.1007/s00464-021-08442-w", "Surgical endoscopy", 2022)
    assert shared.title == "Inguinal hernia mesh is safe in 1720 patients"  # trailing "." gone
    assert shared.source == "europepmc" and shared.authors == ["Forester B", "Attaar M", "Lach M"]
    # A MEDLINE record with only `id` gets its PMID from it.
    assert (med.pmid, med.pub_types) == ("39801280", ["Journal Article", "Case Reports"])
    # The preprint has a DOI and no PMID.
    assert (preprint.pmid, preprint.doi, preprint.journal) == (None, "10.21203/rs.3.rs-9087670/v1", None)
    assert preprint.year == 2026
    # <i> gone from the title; the PMCID is kept.
    assert rct.title == ("A randomized controlled trial of a plastic-sheath technique to facilitate "
                         "self-gripping mesh placement in laparoscopic inguinal hernia repair")
    assert (rct.pmid, rct.pmcid, rct.retracted) == ("42622923", "PMC13493447", False)
    assert "Randomized Controlled Trial" in rct.pub_types


def test_core_fixture_abstracts_lose_their_markup():
    shared, med, preprint, _ = search().papers
    assert shared.abstract.startswith("Background There is substantial media")
    # Incisive quirk, kept: after the stray "<" of "p < 0.05" the next tag stays as written.
    assert "p < 0.05.<h4>Results" in shared.abstract
    assert "<i>" not in med.abstract and "as Mycobacterium" in med.abstract
    assert preprint.abstract.startswith("Abstract Background: ")


# ---------- to_paper rules ----------

def test_pmid_from_id_only_for_medline_records():
    assert to_paper({"id": "12", "source": "MED", "title": "x"}).pmid == "12"
    assert to_paper({"id": "PPR1", "source": "PPR", "title": "x"}).pmid is None
    assert to_paper({"id": "12", "source": "MED", "pmid": " 34 ", "title": "x"}).pmid == "34"


def test_pmcid_only_when_pmc_and_digits():
    assert to_paper({"pmcid": "PMC123", "title": "x"}).pmcid == "PMC123"
    for bad in ["pmc123", "PMC", "PMC12a", "123"]:
        assert to_paper({"pmcid": bad, "title": "x"}).pmcid is None, bad


def test_year_falls_back_through_journal_info_then_first_publication_date():
    assert to_paper({"pubYear": " 2019 ", "title": "x"}).year == 2019
    assert to_paper({"pubYear": "n/a", "journalInfo": {"yearOfPublication": 2018}, "title": "x"}).year == 2018
    assert to_paper({"journalInfo": {"yearOfPublication": "2017"}, "title": "x"}).year == 2017
    assert to_paper({"journalInfo": {"yearOfPublication": 0},
                     "firstPublicationDate": "2016-05-01", "title": "x"}).year == 2016
    assert to_paper({"firstPublicationDate": "20", "title": "x"}).year is None


def test_authors_from_author_string_when_no_list():
    p = to_paper({"authorString": "Smith J, Lee K, Ng P.", "title": "x"})
    assert p.authors == ["Smith J", "Lee K", "Ng P"]
    p = to_paper({"authorList": {"author": [{"fullName": " A B "}, {}]}, "authorString": "X", "title": "x"})
    assert p.authors == ["A B"]


def test_retracted_by_pub_type_and_blank_types_dropped():
    p = to_paper({"pubTypeList": {"pubType": ["Journal Article", " ", "retracted publication"]}, "title": "x"})
    assert p.retracted is True and p.pub_types == ["Journal Article", "retracted publication"]


def test_missing_title_and_blank_abstract():
    p = to_paper({"abstractText": " <p> </p> "})
    assert (p.title, p.abstract, p.journal, p.doi) == ("", None, None, None)


# ---------- strip_markup ----------

def test_strip_markup_keeps_stray_less_than():  # Review Focus 3
    assert strip_markup("mortality p < 0.05") == "mortality p < 0.05"
    assert strip_markup("<i>H. pylori</i><h4>Methods</h4>x &amp; y") == "H. pylori Methods x & y"
    # Incisive quirk, kept: after a stray "<" the text up to the next ">" is kept as written
    assert strip_markup("p < 0.05 <i>x</i>") == "p < 0.05 <i>x"


def test_strip_markup_tags_and_entities():
    assert strip_markup("a<sub>2</sub>b<SUP>3</SUP> <br/>c<p class='x'>d</p>") == "a2b3 c d"
    assert strip_markup("<script>alert(1)</script>T") == "alert(1) T"
    assert strip_markup("x <") == "x <"
    assert strip_markup("a&lt;b&gt;c &quot;q&quot; &apos;s&apos; &nbsp;n") == "a<b>c \"q\" 's' n"
    assert strip_markup("&#65;&#x42;&#X43; &#xD800; &#1114112; &unknown; &&amp;") == (
        "ABC &#xD800; &#1114112; &unknown; &&")
    assert strip_markup("&verylongentityname; & ;") == "&verylongentityname; & ;"
    assert strip_markup("  many \n\t spaces\u00a0here ") == "many spaces here"


def _searched_record(record: dict) -> Paper:
    """`record` as Europe PMC's only result, through the client's cleaning and to_paper."""
    up = FakeUpstream(
        {(EPMC_HOST, EPMC_SEARCH_PATH): httpx.Response(
            200, json={"hitCount": 1, "resultList": {"result": [record]}})}
    )
    return EuropePmc(LitClient(up.transport)).search("mesh", 1).papers[0]


def test_numeric_entity_for_nul_never_produces_a_nul():
    p = _searched_record({"title": "a&#0;b&#x0;c&#X00;d&#000;e", "abstractText": "x&#0;y"})
    assert (p.title, p.abstract) == ("abcde", "xy")
    assert "\x00" not in p.title + p.abstract


@pytest.mark.parametrize("entity", ["&#xD800;", "&#xdfff;", "&#55296;", "&#57343;"])
def test_numeric_entity_for_a_surrogate_is_kept_as_written(entity):
    # As Incisive `decode_entities`: char::from_u32 refuses it, so the "&" stays literal.
    p = _searched_record({"title": f"a{entity}b", "abstractText": f"x{entity}y"})
    assert (p.title, p.abstract) == (f"a{entity}b", f"x{entity}y")
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in p.title + p.abstract)
    (p.title + p.abstract).encode("utf-8")  # encodes cleanly


# ---------- merge ----------

def test_merge_by_pmid_then_doi():  # Review Focus 5
    a = paper(pmid="1", doi="10.1/X")
    a2 = paper(pmid="1", source="europepmc", journal="J")
    b = paper(pmid=None, doi="10.1/x", source="europepmc")
    c = paper(pmid="2", doi="10.1/X", source="europepmc")
    out = merge_results([a], [a2, b, c])
    assert [p.pmid for p in out] == ["1", "2"] and out[0].source == "both" and out[0].journal == "J"


def test_merge_pubmed_wins_gaps_filled_types_united_retracted_ored():
    pm = paper(pmid="1", title="PubMed title", journal="PM J", abstract=None, year=None,
               pub_types=["Journal Article"], authors=[])
    ep = paper(pmid="1", title="EPMC title", journal="EP J", abstract="abs", year=2020, pmcid="PMC9",
               pub_types=["Journal Article", "Retracted Publication"], retracted=True,
               authors=["A"], source="europepmc")
    [m] = merge_results([pm], [ep])
    assert (m.title, m.journal, m.abstract, m.year, m.pmcid, m.authors) == (
        "PubMed title", "PM J", "abs", 2020, "PMC9", ["A"])
    assert m.pub_types == ["Journal Article", "Retracted Publication"] and m.retracted is True
    assert m.source == "both"
    assert pm.abstract is None and pm.pub_types == ["Journal Article"]  # inputs untouched


def test_merge_by_pmcid_when_no_doi():
    a = paper(pmid=None, pmcid="PMC5", title="x")
    b = paper(pmid="7", pmcid="PMC5", source="europepmc")
    [m] = merge_results([a], [b])
    assert (m.pmid, m.source) == ("7", "both")


def test_merge_keeps_order_and_collapses_duplicates_within_a_list():
    out = merge_results([paper(pmid="2"), paper(pmid="1"), paper(pmid="2")],
                        [paper(pmid="3", source="europepmc"), paper(pmid="1", source="europepmc")])
    assert [p.pmid for p in out] == ["2", "1", "3"]
    assert [p.source for p in out] == ["pubmed", "both", "europepmc"]


def test_merge_recorded_replies_shows_the_shared_paper_once():
    from pubmedgemini.lit.pubmed import parse_esummary

    pm = parse_esummary(json.loads(fixture("esummary.json")), [])
    out = merge_results(pm, list(search().papers))
    assert [p.pmid for p in out].count(SHARED_ID) == 1 and len(out) == 6
    shared = next(p for p in out if p.pmid == SHARED_ID)
    assert shared.source == "both" and shared.abstract  # filled from Europe PMC
    assert shared.title == "Inguinal hernia mesh is safe in 1720 patients."  # PubMed's value wins


def test_merge_empty():
    assert merge_results([], []) == []
