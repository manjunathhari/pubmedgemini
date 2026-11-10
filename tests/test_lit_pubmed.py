import json

import httpx
import pytest
from lit_fakes import EUTILS_HOST, EUTILS_PATH, FakeUpstream, fixture, pubmed_routes

from pubmedgemini.lit.http import LitClient, LitError
from pubmedgemini.lit.paper import Paper, paper_key
from pubmedgemini.lit.pubmed import (
    FetchedRecord,
    PubMed,
    SearchHits,
    parse_efetch,
    parse_elink,
    parse_esearch,
    parse_esummary,
)

ID1, ID2, ID3 = "36262096", "33763744", "31696731"  # esearch.json, in its order
RETRACTED_ID = "38265557"  # efetch.xml only


def fake_pubmed() -> FakeUpstream:
    return FakeUpstream(pubmed_routes())


def pubmed(up: FakeUpstream, key: str | None = None, email: str | None = None) -> PubMed:
    return PubMed(LitClient(up.transport), key, email)


def make_paper(**kw) -> Paper:
    base = {"pmid": None, "pmcid": None, "doi": None, "title": "t", "authors": [], "journal": None,
            "year": None, "pub_types": [], "abstract": None, "retracted": False, "source": "pubmed"}
    return Paper(**(base | kw))


# ---------- the paper key ----------

def test_paper_key_is_pmid_then_lower_doi_then_title():
    assert paper_key(make_paper(pmid=" 12 ", doi="10.1/X")) == "12"
    assert paper_key(make_paper(pmid="  ", doi=" 10.1/AbC ")) == "10.1/abc"
    assert paper_key(make_paper(title=" Mesh Repair ")) == "title:mesh repair"


# ---------- requests ----------

def test_every_request_carries_tool_and_no_key_when_unset():
    up = fake_pubmed()
    pubmed(up).search("hernia", 5)
    p = up.sent[0].url.params
    assert (p["db"], p["tool"], "api_key" in p, "email" in p) == ("pubmed", "pubmedgemini", False, False)


def test_key_and_email_sent_when_set():
    up = fake_pubmed()
    pm = pubmed(up, "test-key", "dev@example.org")
    pm.search("hernia", 5)
    pm.summaries([ID1])
    pm.details([ID1])
    pm.related(ID1, 5)
    for r in up.sent:
        p = r.url.params
        assert (p["db"], p["tool"], p["api_key"], p["email"]) == (
            "pubmed", "pubmedgemini", "test-key", "dev@example.org")


def test_blank_key_and_email_are_not_sent():
    up = fake_pubmed()
    pubmed(up, "", "  ").search("hernia", 5)
    p = up.sent[0].url.params
    assert "api_key" not in p and "email" not in p


def test_search_request_shape():
    up = fake_pubmed()
    pubmed(up).search("hernia", 5)
    r = up.sent[0]
    assert (r.url.scheme, r.url.host, r.url.path) == ("https", EUTILS_HOST, EUTILS_PATH + "esearch.fcgi")
    p = r.url.params
    assert (p["term"], p["retmax"], p["sort"], p["retmode"]) == ("hernia", "5", "relevance", "json")


@pytest.mark.parametrize(("asked", "sent"), [(0, "1"), (-3, "1"), (1, "1"), (1000, "1000"), (5000, "1000")])
def test_retmax_is_clamped(asked, sent):
    up = fake_pubmed()
    pubmed(up).search("hernia", asked)
    assert up.sent[0].url.params["retmax"] == sent


def test_text_is_parameter_encoded():
    up = fake_pubmed()
    pubmed(up).search('hernia[tiab] AND "mesh"&retmax=999', 5)
    p = up.sent[0].url.params
    assert p["term"] == 'hernia[tiab] AND "mesh"&retmax=999' and p.get_list("retmax") == ["5"]
    assert "&retmax=999" not in up.sent[0].url.query.decode()


def test_summaries_details_and_related_request_shapes():
    up = fake_pubmed()
    pm = pubmed(up)
    pm.summaries([ID1, ID2])
    pm.details([ID1, ID2])
    pm.related(ID1, 5)
    s, f, e = (r.url for r in up.sent)
    assert s.path.endswith("/esummary.fcgi") and (s.params["id"], s.params["retmode"]) == (f"{ID1},{ID2}", "json")
    assert f.path.endswith("/efetch.fcgi") and (f.params["id"], f.params["rettype"], f.params["retmode"]) == (
        f"{ID1},{ID2}", "abstract", "xml")
    assert e.path.endswith("/elink.fcgi")
    assert (e.params["dbfrom"], e.params["id"], e.params["cmd"], e.params["linkname"], e.params["retmode"]) == (
        "pubmed", ID1, "neighbor_score", "pubmed_pubmed", "json")


def test_summaries_go_in_batches_of_100():
    up = fake_pubmed()
    pubmed(up).summaries([str(i) for i in range(1, 151)])
    ids = [r.url.params["id"].split(",") for r in up.sent]
    assert [len(x) for x in ids] == [100, 50] and ids[1][0] == "101"


def test_details_go_in_batches_of_50():
    up = fake_pubmed()
    pubmed(up).details([str(i) for i in range(1, 121)])
    assert [len(r.url.params["id"].split(",")) for r in up.sent] == [50, 50, 20]


def test_no_ids_sends_nothing():
    up = fake_pubmed()
    pm = pubmed(up)
    assert pm.summaries([]) == [] and pm.details([]) == {} and up.sent == []


def test_upstream_error_is_lit_error():
    up = FakeUpstream({(EUTILS_HOST, EUTILS_PATH + "esearch.fcgi"): httpx.Response(400)})
    with pytest.raises(LitError) as e:
        pubmed(up).search("hernia", 5)
    assert (e.value.kind, e.value.status) == ("http", 400)


# ---------- esearch ----------

def test_esearch_fixture():
    hits = pubmed(fake_pubmed()).search("inguinal hernia mesh", 3)
    assert hits == SearchHits(count=5160, pmids=(ID1, ID2, ID3))


def test_esearch_error_is_parse():
    with pytest.raises(LitError) as e:
        parse_esearch(json.loads(fixture("esearch_error.json")))
    assert e.value.kind == "parse"


@pytest.mark.parametrize("body", [
    {}, [], {"esearchresult": None}, {"esearchresult": {"idlist": []}},
    {"esearchresult": {"count": "x", "idlist": []}}, {"esearchresult": {"count": 5}},
    {"esearchresult": {"count": "-5"}}, {"esearchresult": {"count": "5", "idlist": [5]}},
])
def test_esearch_without_a_usable_count_is_parse(body):
    with pytest.raises(LitError) as e:
        parse_esearch(body)
    assert e.value.kind == "parse"


def test_esearch_count_with_no_ids():
    assert parse_esearch({"esearchresult": {"count": " 0 "}}) == SearchHits(count=0, pmids=())


# ---------- esummary ----------

def test_esummary_fixture():
    papers = parse_esummary(json.loads(fixture("esummary.json")), [ID1, ID2, ID3, "999999999"])
    assert [p.pmid for p in papers] == [ID1, ID2, ID3]  # the unknown id is absent
    a, b, c = papers
    assert a.title == ("Laparoscopic mesh placement during inguinal hernia repair: "
                       "a meta-analysis of two methods of repair.")
    assert (a.journal, a.year, a.doi, a.pmcid) == ("ANZ journal of surgery", 2023, "10.1111/ans.18124", None)
    assert a.pub_types == ["Meta-Analysis", "Systematic Review", "Journal Article"]
    assert a.authors == ["Demetriou G", "Ahmad MS", "Robinson SJ"]
    assert (a.abstract, a.retracted, a.source) == (None, False, "pubmed")
    assert (b.journal, b.year) == ("Surgical endoscopy", 2022)
    assert (c.pmcid, c.doi, c.year) == ("PMC6996425", "10.1308/rcsann.2019.0137", 2020)
    assert c.title == "<script>alert(1)</script> Mesh repair"  # parsers keep text; the page escapes


def _summary(**rec):
    return {"result": {"uids": ["7"], "7": {"title": "T"} | rec}}


def test_esummary_journal_falls_back_to_source():
    [p] = parse_esummary(_summary(fulljournalname=" ", source="Surg Endosc"), ["7"])
    assert p.journal == "Surg Endosc"


def test_esummary_year_falls_back_to_epubdate():
    assert parse_esummary(_summary(pubdate="Spring", epubdate="2021 Mar 24"), ["7"])[0].year == 2021
    assert parse_esummary(_summary(pubdate="20219", epubdate=""), ["7"])[0].year is None
    assert parse_esummary(_summary(pubdate="2024 Mar"), ["7"])[0].year == 2024


def test_esummary_old_pmcid_form_and_retracted_type():
    [p] = parse_esummary(_summary(
        articleids=[{"idtype": "pmcid", "value": "pmc-id: PMC123;"}],
        pubtype=["Journal Article", " retracted publication "]), ["7"])
    assert (p.pmcid, p.retracted) == ("PMC123", True)


def test_esummary_skips_records_without_title_and_errors():
    body = {"result": {"uids": ["1", "2", "3"], "1": {"title": "  "}, "2": {"error": "x", "title": "T"},
                       "3": {"title": "Kept"}}}
    assert [p.pmid for p in parse_esummary(body, ["1", "2", "3"])] == ["3"]


def test_esummary_without_uids_uses_the_ids_asked_for():
    body = {"result": {"9": {"title": "T"}}}
    assert [p.pmid for p in parse_esummary(body, ["9", "10"])] == ["9"]


def test_esummary_without_result_is_parse():
    with pytest.raises(LitError) as e:
        parse_esummary({"error": "bad"}, ["1"])
    assert e.value.kind == "parse"


# ---------- efetch ----------

def _records() -> dict[str, FetchedRecord]:
    return {r.pmid: r for r in parse_efetch(fixture("efetch.xml").decode())}


def test_efetch_fixture_records():
    recs = _records()
    assert sorted(recs) == sorted([ID1, ID2, ID3, RETRACTED_ID])
    assert recs[ID2].pub_types == ("Journal Article",)
    assert recs[RETRACTED_ID].abstract is None


def test_efetch_structured_abstract():
    rec = _records()[ID1]
    assert rec.abstract.startswith("BACKGROUND: There are several modifications")
    assert "\n\nMETHODS: The reporting" in rec.abstract and "\n\nCONCLUSION: There was no" in rec.abstract
    assert "slit Vs nonslit" in rec.abstract  # the <i> element's text is kept, the tag is not
    assert rec.pub_types == ("Meta-Analysis", "Systematic Review", "Journal Article")
    assert rec.retracted is False


def test_efetch_escaped_less_than_and_sup_text():
    recs = _records()
    assert "(all p\u2009<\u20090.05)" in recs[ID2].abstract  # thin spaces kept, as recorded
    assert "MEDLINE®, Scopus®" in recs[ID3].abstract


def test_efetch_retraction_comment_marks_retracted():
    rec = _records()[RETRACTED_ID]
    assert rec.retracted is True and "Retracted Publication" in rec.pub_types


def test_efetch_retraction_comment_alone_marks_retracted():
    xml = ("<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>5</PMID>"
           "<CommentsCorrectionsList><CommentsCorrections RefType=\"RetractionIn\"><PMID>6</PMID>"
           "</CommentsCorrections></CommentsCorrectionsList></MedlineCitation></PubmedArticle>"
           "</PubmedArticleSet>")
    [rec] = parse_efetch(xml)
    assert (rec.pmid, rec.retracted, rec.pub_types) == ("5", True, ())


def test_efetch_unknown_entity_kept_visible():
    assert "95%&thinsp;CI (0.27-1.41)" in _records()[ID1].abstract
    xml = ("<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>5</PMID><Article><Abstract>"
           "<AbstractText>a&nbsp;b &amp; c&#8201;d &lt; e &unknown; f</AbstractText>"
           "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>")
    assert parse_efetch(xml)[0].abstract == "a b & c\u2009d < e &unknown; f"


def test_efetch_collapses_ascii_whitespace_only():
    xml = ("<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID> 5 </PMID><Article><Abstract>"
           "<AbstractText Label=\" AIM \">  a \n\t b\u00a0c  </AbstractText><AbstractText></AbstractText>"
           "<AbstractText>second</AbstractText>"
           "</Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>")
    [rec] = parse_efetch(xml)
    assert (rec.pmid, rec.abstract) == ("5", "AIM: a b\u00a0c\n\nsecond")


def test_efetch_reads_book_records_and_skips_records_without_pmid():
    xml = ("<PubmedArticleSet>"
           "<PubmedBookArticle><BookDocument><PMID>8</PMID><Abstract><AbstractText>book</AbstractText>"
           "</Abstract></BookDocument></PubmedBookArticle>"
           "<PubmedArticle><MedlineCitation><Article><Abstract><AbstractText>x</AbstractText>"
           "</Abstract></Article></MedlineCitation></PubmedArticle>"
           "</PubmedArticleSet>")
    assert [(r.pmid, r.abstract) for r in parse_efetch(xml)] == [("8", "book")]


def test_efetch_ignores_pmids_and_abstracts_outside_their_places():
    xml = ("<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>5</PMID>"
           "<CommentsCorrectionsList><CommentsCorrections RefType=\"CommentOn\"><PMID>6</PMID>"
           "</CommentsCorrections></CommentsCorrectionsList>"
           "<OtherAbstract><AbstractText>other</AbstractText></OtherAbstract>"
           "</MedlineCitation></PubmedArticle></PubmedArticleSet>")
    [rec] = parse_efetch(xml)
    assert (rec.pmid, rec.abstract, rec.retracted) == ("5", None, False)


def test_efetch_empty_set_is_no_records():
    assert parse_efetch('<?xml version="1.0" ?>\n<PubmedArticleSet>\n</PubmedArticleSet>\n') == []


@pytest.mark.parametrize("xml", ["<PubmedArticleSet><PubmedArticle>", "", "not xml", "<a>&#0;</a>"])
def test_efetch_malformed_is_parse(xml):
    with pytest.raises(LitError) as e:
        parse_efetch(xml)
    assert e.value.kind == "parse"
    assert e.value.__cause__ is None and e.value.__suppress_context__


# ---------- elink ----------

def test_elink_fixture():
    ids = pubmed(fake_pubmed()).related("31482863", 30)
    assert ids == ["24736447", "28130603", "31353033", "31022677", "23292669"]


def test_elink_skips_self_duplicates_and_non_digits_and_caps():  # Review Focus 5
    body = {"linksets": [{"linksetdbs": [{"linkname": "pubmed_pubmed",
            "links": [{"id": "123", "score": 9}, {"id": "9"}, 9, {"id": "x1"}, "77"]}]}]}
    assert parse_elink(body, "123", 30) == ["9", "77"]
    assert parse_elink(body, "123", 1) == ["9"]


def test_elink_reads_numbers_and_skips_bools_floats_and_negatives():
    body = {"linksets": [{"linksetdbs": [{"linkname": "pubmed_pubmed",
            "links": [True, 4.0, -5, {"id": 12}, " 13 ", {"id": None}, None, ""]}]}]}
    assert parse_elink(body, "1", 30) == ["12", "13"]


def test_elink_prefers_the_pubmed_pubmed_link_set():
    body = {"linksets": [{"linksetdbs": [
        {"linkname": "pubmed_pubmed_reviews", "links": ["1"]},
        {"linkname": "pubmed_pubmed", "links": ["2"]}]}]}
    assert parse_elink(body, "9", 30) == ["2"]
    body["linksets"][0]["linksetdbs"].pop()
    assert parse_elink(body, "9", 30) == ["1"]


def test_elink_no_neighbours_is_empty():
    assert parse_elink({"linksets": [{"dbfrom": "pubmed"}]}, "9", 30) == []


@pytest.mark.parametrize("body", [{"ERROR": "x"}, {}, {"linksets": []}, {"linksets": [{"ERROR": "x"}]}, []])
def test_elink_error_is_parse(body):
    with pytest.raises(LitError) as e:
        parse_elink(body, "9", 30)
    assert e.value.kind == "parse"
