import json
from html.parser import HTMLParser

import httpx
import pytest
from conftest import search_client
from lit_fakes import (
    EPMC_HOST,
    EPMC_SEARCH_PATH,
    EUTILS_HOST,
    EUTILS_PATH,
    FakeUpstream,
    europepmc_routes,
    fixture,
    pubmed_routes,
)

from pubmedgemini.lit.europepmc import strip_markup
from pubmedgemini.lit.tools import ToolInputError
from pubmedgemini.search import PAGE_RESULTS, QueryRejected, run_page_search
from pubmedgemini.web import drop_trailing_question_marks

H = {"x-dev-user-email": "owner@example.com"}
ID1, ID2, ID3 = "36262096", "33763744", "31696731"
SHARED_ID = ID2  # in esearch.json and europepmc.json
ESEARCH = (EUTILS_HOST, EUTILS_PATH + "esearch.fcgi")
EFETCH = (EUTILS_HOST, EUTILS_PATH + "efetch.fcgi")
ESUMMARY = (EUTILS_HOST, EUTILS_PATH + "esummary.fcgi")
EPMC = (EPMC_HOST, EPMC_SEARCH_PATH)
BOTH_SEARCHED = "Searched PubMed and Europe PMC (free sources)"
BOTH_DOWN = "PubMed and Europe PMC could not be reached. Please try again in a minute."


def upstream_with(override: dict) -> FakeUpstream:
    return FakeUpstream(pubmed_routes() | europepmc_routes() | override)


@pytest.fixture
def client_for(db_url):
    """Builds a search client over the fixtures with some routes replaced."""
    opened = []

    def build(override: dict):
        up = upstream_with(override)
        c = search_client(db_url, up)
        c.__enter__()
        opened.append(c)
        return c, up

    yield build
    for c in opened:
        c.__exit__(None, None, None)


def sent_to(up: FakeUpstream, name: str) -> list[httpx.Request]:
    return [r for r in up.sent if r.url.path.endswith(name)]


def epmc_reply_with(edit) -> httpx.Response:
    body = json.loads(fixture("europepmc.json"))
    edit(body["resultList"]["result"])
    return httpx.Response(200, json=body)


# ---------- the home page ----------

def test_home_has_one_box_and_no_script(client_dev):
    r = client_dev.get("/", headers=H)
    assert r.text.count('name="q"') == 1 and 'method="post" action="/search"' in r.text
    assert "<script" not in r.text
    assert '<label for="q">Search PubMed and Europe PMC</label>' in r.text
    assert 'maxlength="1000"' in r.text and '<button type="submit">Search</button>' in r.text
    assert ("What you type is sent to PubMed and Europe PMC as the search. "
            "Do not type anything that identifies a patient.") in r.text
    assert "Answers written from these papers arrive in the next stages." in r.text


def test_get_with_query_string_does_not_search(client_search, upstream):
    r = client_search.get("/?q=hernia", headers=H)
    assert r.status_code == 200 and upstream.sent == [] and "hernia" not in r.text


def test_search_text_in_the_url_is_ignored(client_search, upstream):
    r = client_search.post("/search?q=hernia", headers=H)
    assert r.status_code == 400 and "query rejected (empty)" in r.text and upstream.sent == []


def test_get_search_is_not_a_route(client_search, upstream):
    assert client_search.get("/search?q=hernia", headers=H).status_code == 405
    assert upstream.sent == []


def test_search_needs_sign_in(client_prod):
    assert client_prod.post("/search", data={"q": "mesh"}).status_code == 401


def test_search_needs_a_set_up_user(client_prod):
    r = client_prod.post("/search", data={"q": "mesh"},
                         headers={"x-forwarded-email": "stranger@example.com"})
    assert r.status_code == 403


# ---------- results ----------

def test_search_shows_merged_results(client_search, upstream):
    r = client_search.post("/search", data={"q": "inguinal hernia mesh"}, headers=H)
    assert r.status_code == 200 and BOTH_SEARCHED in r.text
    assert "PubMed found 5160; Europe PMC found 9672. Showing the first 6." in r.text
    assert r.text.count(f"https://pubmed.ncbi.nlm.nih.gov/{SHARED_ID}/") == 1  # shown once
    assert f">PMID {ID1}</a>" in r.text
    assert "<p>BACKGROUND: " in r.text and "<p>METHODS: " in r.text  # paragraphs per section
    assert "RCT" in r.text and "systematic review" in r.text
    assert 'href="https://doi.org/10.21203/rs.3.rs-9087670/v1"' in r.text  # the preprint
    assert "Publication types: Meta-Analysis, Systematic Review, Journal Article" in r.text
    assert r.text.count("<article>") == 6
    assert 'value="inguinal hernia mesh"' in r.text  # the box refilled


def test_each_source_is_asked_for_page_results(client_search, upstream):
    client_search.post("/search", data={"q": "inguinal hernia mesh"}, headers=H)
    assert PAGE_RESULTS == 20
    assert sent_to(upstream, "esearch.fcgi")[0].url.params["retmax"] == "20"
    assert upstream.sent[-1].url  # sanity
    [epmc] = [r for r in upstream.sent if r.url.host == EPMC_HOST]
    assert epmc.url.params["pageSize"] == "20"


def test_journal_year_and_design_line(client_search):
    r = client_search.post("/search", data={"q": "inguinal hernia mesh"}, headers=H)
    # 36262096 is typed Meta-Analysis and Systematic Review: the rule checks SR first
    assert "ANZ journal of surgery · 2023 · systematic review" in r.text
    assert " · RCT" in r.text


def test_second_search_takes_abstracts_from_the_cache(client_search, upstream):
    client_search.post("/search", data={"q": "inguinal hernia mesh"}, headers=H)
    upstream.sent.clear()
    r = client_search.post("/search", data={"q": "inguinal hernia mesh"}, headers=H)
    assert r.status_code == 200 and sent_to(upstream, "efetch.fcgi") == []


def test_retracted_is_shown_in_bold(client_for):
    def retract(results):
        results[3]["pubTypeList"]["pubType"].append("Retracted Publication")

    c, _ = client_for({EPMC: epmc_reply_with(retract)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.text.count("<strong>Retracted</strong>") == 1


def test_no_abstract_says_so(client_for):
    def drop_abstracts(results):
        for rec in results:
            rec.pop("abstractText", None)

    c, _ = client_for({EPMC: epmc_reply_with(drop_abstracts)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert "No abstract available." in r.text


def test_no_results_says_no_papers_found(client_for):
    c, up = client_for({
        ESEARCH: httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}}),
        EPMC: httpx.Response(200, json={"hitCount": 0, "resultList": {"result": []}}),
    })
    r = c.post("/search", data={"q": "zzzz"}, headers=H)
    assert r.status_code == 200 and "No papers found." in r.text and "<article>" not in r.text
    assert "PubMed found 0; Europe PMC found 0." in r.text and "Showing the first" not in r.text
    assert sent_to(up, "esummary.fcgi") == []


# ---------- one source down, both down ----------

def test_europepmc_down_shows_pubmed_only(client_for):  # Review Focus 4
    c, _ = client_for({EPMC: httpx.Response(503)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200
    assert "Europe PMC could not be reached; searched PubMed only" in r.text
    assert "Searched PubMed and Europe PMC" not in r.text
    assert "PubMed found 5160. Showing the first 3." in r.text and "Europe PMC found" not in r.text


def test_pubmed_down_shows_europepmc_only(client_for):
    c, _ = client_for({ESEARCH: httpx.Response(503)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200
    assert "PubMed could not be reached; searched Europe PMC only" in r.text
    assert "Europe PMC found 9672. Showing the first 4." in r.text and "PubMed found" not in r.text


def test_both_down_is_503_plain(client_for):
    c, _ = client_for({ESEARCH: httpx.Response(503), EPMC: httpx.Response(503)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 503 and BOTH_DOWN in r.text and "Traceback" not in r.text


def test_esummary_down_is_pubmed_not_reached_and_is_not_asked_again(client_for):
    # PubMed's search needs esummary, so PubMed counts as not reached; the page does not ask
    # NCBI again for abstracts and shows Europe PMC's (review finding B).
    c, up = client_for({ESUMMARY: httpx.Response(503), EFETCH: httpx.Response(503)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200
    assert "PubMed could not be reached; searched Europe PMC only" in r.text
    assert sent_to(up, "efetch.fcgi") == [] and len(sent_to(up, "esummary.fcgi")) == 3
    assert "Abstracts could not be fetched just now; titles only." not in r.text


def test_efetch_down_alone_shows_titles_only(client_for):
    c, _ = client_for({EFETCH: httpx.Response(503)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200 and BOTH_SEARCHED in r.text
    assert "Abstracts could not be fetched just now; titles only." in r.text


# ---------- the query rule on the page ----------

def test_rejected_query_is_400_and_sends_nothing(client_search, upstream):  # Review Focus 1
    r = client_search.post("/search", data={"q": "hernia? suture"}, headers=H)
    assert r.status_code == 400 and "query rejected (character)" in r.text and upstream.sent == []
    assert ", _" not in r.text  # the PubMed message


@pytest.mark.parametrize("q", ["a ; b", "café", "see www.example.com", "a\nb", "a" * 1001, "(a"])
def test_other_refused_texts_are_400(client_search, upstream, q):
    r = client_search.post("/search", data={"q": q}, headers=H)
    assert r.status_code == 400 and "query rejected (" in r.text and upstream.sent == []


def test_underscore_searches_europepmc_only(client_search, upstream):  # decision 6
    r = client_search.post("/search", data={"q": 'PUB_TYPE:"review"'}, headers=H)
    assert r.status_code == 200
    assert "PubMed does not take the underscore; searched Europe PMC only" in r.text
    assert sent_to(upstream, "esearch.fcgi") == []
    assert "Europe PMC found 9672." in r.text and "PubMed found" not in r.text


def test_refused_text_is_refilled_escaped(client_search):
    r = client_search.post("/search", data={"q": '"><script>alert(1)</script>'}, headers=H)
    assert r.status_code == 400 and "<script>alert" not in r.text
    assert "&lt;script&gt;" in r.text


# ---------- owner decision (10 Nov): trailing question marks are dropped on the page ----------

@pytest.mark.parametrize(("typed", "searched"), [
    ("Is mesh better than suture?", "Is mesh better than suture"),
    ("mesh repair ?? ?  ", "mesh repair"),
    ("mesh repair?\t?", "mesh repair"),
    ("?", ""),
    ("  ? ?  ", ""),
    ("mesh? suture", "mesh? suture"),
    ("mesh", "mesh"),
    ("", ""),
])
def test_drop_trailing_question_marks(typed, searched):
    assert drop_trailing_question_marks(typed).strip() == searched


def test_one_trailing_question_mark_is_dropped(client_search, upstream):
    r = client_search.post("/search", data={"q": "Is mesh better than suture?"}, headers=H)
    assert r.status_code == 200 and BOTH_SEARCHED in r.text
    [esearch] = sent_to(upstream, "esearch.fcgi")
    [epmc] = [x for x in upstream.sent if x.url.host == EPMC_HOST]
    assert esearch.url.params["term"] == "Is mesh better than suture"
    assert epmc.url.params["query"] == "Is mesh better than suture"
    assert "query rejected" not in r.text  # quietly


def test_several_trailing_question_marks_with_spaces_are_dropped(client_search, upstream):
    r = client_search.post("/search", data={"q": "  mesh repair ? ??  "}, headers=H)
    assert r.status_code == 200
    assert sent_to(upstream, "esearch.fcgi")[0].url.params["term"] == "mesh repair"


def test_question_mark_in_the_middle_is_still_refused(client_search, upstream):
    r = client_search.post("/search", data={"q": "mesh? or suture?"}, headers=H)
    assert r.status_code == 400 and "query rejected (character)" in r.text and upstream.sent == []


@pytest.mark.parametrize("q", ["?", " ? ?? "])
def test_only_question_marks_is_empty_input(client_search, upstream, q):
    r = client_search.post("/search", data={"q": q}, headers=H)
    empty = client_search.post("/search", data={"q": ""}, headers=H)
    assert (r.status_code, empty.status_code) == (400, 400)
    assert "query rejected (empty)" in r.text and "query rejected (empty)" in empty.text
    assert upstream.sent == []


def test_tools_called_directly_still_refuse_a_trailing_question_mark(tools, upstream):
    for name in ("search_pubmed", "search_europepmc"):
        with pytest.raises(ToolInputError) as e:
            getattr(tools, name)("Is mesh better than suture?", 5)
        assert str(e.value).startswith("query rejected (character)")
    with pytest.raises(QueryRejected) as rejected:
        run_page_search(tools, "Is mesh better than suture?")  # the drop is the route's only
    assert (rejected.value.kind, rejected.value.source) == ("character", "pubmed")
    assert upstream.sent == []


# ---------- nothing from upstream is rendered as HTML (Review Focus 3) ----------

def test_script_title_is_escaped(client_search):
    r = client_search.post("/search", data={"q": "mesh"}, headers=H)
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Mesh repair" in r.text
    assert "<script>alert" not in r.text


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.attrs: set[str] = set()

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        self.attrs.update(name for name, _ in attrs)


PAGE_TAGS = {"html", "head", "meta", "title", "style", "body", "main", "h1", "h2", "p", "form",
             "label", "input", "button", "article", "a", "strong"}
PAGE_ATTRS = {"lang", "charset", "name", "content", "method", "action", "for", "id", "type",
              "maxlength", "required", "value", "href"}


def test_nothing_from_upstream_is_rendered_as_html(client_for):
    hostile_abstract = "Mortality p < 0.05 <h4>Methods</h4> and <img src=x onerror=alert(1)> end"
    # Incisive's strip_markup quirk, kept for F1: after a stray "<" the text up to the next ">"
    # stays as written, so the abstract really holds a literal "<h4>".
    assert "<h4>" in strip_markup(hostile_abstract)

    def poison(results):
        rec = results[1]
        rec["title"] = 'T <b>x</b> a < b <iframe src="//x">'
        rec["abstractText"] = hostile_abstract
        rec["journalInfo"]["journal"]["title"] = 'J <svg onload=alert(1)> " onmouseover="x'
        rec["pubTypeList"]["pubType"] = ["a < <style>x</style>", 'Letter" onclick="y']
        results[2]["doi"] = '10.1/"><script>alert(1)</script>'

    c, _ = client_for({EPMC: epmc_reply_with(poison)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200
    parser = _Tags()
    parser.feed(r.text)
    assert parser.tags <= PAGE_TAGS, parser.tags - PAGE_TAGS
    assert parser.attrs <= PAGE_ATTRS, parser.attrs - PAGE_ATTRS
    assert "&lt;h4&gt;" in r.text and "<h4>" not in r.text
    for raw in ("<iframe", "<svg", "<img", "<script>alert", "onmouseover=\"x"):
        assert raw not in r.text, raw
    assert r.text.count("<style>") == 1  # the page's own
    assert "https://doi.org/10.1/%22%3E%3Cscript%3E" in r.text


# ---------- one bad upstream reply never gives a 500 (review finding C) ----------

DEEP_JSON = "[" * 100_000 + "]" * 100_000
DEEP_XML = ("<PubmedArticleSet><PubmedArticle><MedlineCitation>" + "<x>" * 50_000
            + "</x>" * 50_000 + "</MedlineCitation></PubmedArticle></PubmedArticleSet>")


def test_deeply_nested_json_from_pubmed_shows_europepmc(client_for):
    c, _ = client_for({ESEARCH: httpx.Response(200, text=DEEP_JSON)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200
    assert "PubMed could not be reached; searched Europe PMC only" in r.text


def test_deeply_nested_json_from_europepmc_shows_pubmed(client_for):
    c, _ = client_for({EPMC: httpx.Response(200, text=DEEP_JSON)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200
    assert "Europe PMC could not be reached; searched PubMed only" in r.text


def test_deeply_nested_json_from_both_is_503(client_for):
    c, _ = client_for({ESEARCH: httpx.Response(200, text=DEEP_JSON),
                       EPMC: httpx.Response(200, text=DEEP_JSON)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 503 and BOTH_DOWN in r.text


def test_deeply_nested_efetch_xml_shows_titles_only(client_for):
    # efetch is the only XML reply and only abstracts come from it, so both searches stand
    # and the page says the abstracts could not be fetched.
    c, _ = client_for({EFETCH: httpx.Response(200, text=DEEP_XML)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200 and BOTH_SEARCHED in r.text
    assert "Abstracts could not be fetched just now; titles only." in r.text


def test_a_lone_surrogate_in_a_title_is_shown_as_fffd_and_cached(client_for, db_url):
    body = json.loads(fixture("esummary.json"))
    body["result"][ID1]["title"] = "Lap\ud800aroscopic mesh"
    raw = json.dumps(body)  # ensure_ascii: the lone surrogate is written as \ud800
    assert "\\ud800" in raw
    c, _ = client_for({ESUMMARY: httpx.Response(200, text=raw)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200 and "Lap�aroscopic mesh" in r.text
    import psycopg

    with psycopg.connect(db_url) as conn:
        [(title,)] = conn.execute("SELECT title FROM papers WHERE pmid = %s", (ID1,)).fetchall()
    assert title == "Lap�aroscopic mesh"


# ---------- an unexpected error is a plain 500 page and one fixed log line ----------

INTERNAL = "Something went wrong. Please try again in a minute."


def test_unexpected_error_in_a_route_is_a_plain_500(client_search, monkeypatch, caplog):
    import logging

    from pubmedgemini import web

    def boom(*_a, **_k):
        raise RuntimeError("zq7 hernia secret")

    monkeypatch.setattr(web, "run_page_search", boom)
    caplog.set_level(logging.DEBUG)
    r = client_search.post("/search", data={"q": "zq7 hernia"}, headers=H)
    assert r.status_code == 500 and INTERNAL in r.text
    assert "zq7" not in r.text.replace('value="zq7 hernia"', "") and "Traceback" not in r.text
    messages = [x.getMessage() for x in caplog.records]
    assert [m for m in messages if "kind=internal" in m] == ["kind=internal error=RuntimeError"]
    assert "request method=POST route=/search status=500" in messages
    assert "zq7" not in caplog.text and "Traceback" not in caplog.text
    assert all(x.exc_info is None for x in caplog.records)


def test_unexpected_error_from_upstream_is_a_plain_500(db_url, caplog):
    import logging

    from crash_app import _explode

    caplog.set_level(logging.DEBUG)
    up = FakeUpstream({})
    up.transport = httpx.MockTransport(_explode)
    with search_client(db_url, up) as c:
        r = c.post("/search", data={"q": "zq7 hernia"}, headers=H)
    assert r.status_code == 500 and INTERNAL in r.text
    assert "zq7" not in caplog.text and "eutils" not in caplog.text
    assert [m for m in (x.getMessage() for x in caplog.records) if "kind=internal" in m] == [
        "kind=internal error=RuntimeError"]


# ---------- abstracts when PubMed is down, and the "titles only" line (findings B and F) ----------

TITLES_ONLY = "Abstracts could not be fetched just now; titles only."
NCBI_DOWN = {ESEARCH: httpx.Response(503), ESUMMARY: httpx.Response(503),
             EFETCH: httpx.Response(503)}


def ncbi_sent(up: FakeUpstream) -> list[httpx.Request]:
    return [r for r in up.sent if r.url.host == EUTILS_HOST]


def test_pubmed_down_asks_ncbi_once_and_shows_europepmc_abstracts(client_for):
    c, up = client_for(NCBI_DOWN)
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200
    assert "PubMed could not be reached; searched Europe PMC only" in r.text
    # One round of attempts (the first and its two retries), all for the search itself.
    assert [x.url.path for x in ncbi_sent(up)] == [EUTILS_PATH + "esearch.fcgi"] * 3
    assert "There is substantial media and patient interest" in r.text
    assert TITLES_ONLY not in r.text


def test_pubmed_down_still_uses_abstracts_already_cached(client_for):
    c, up = client_for({})
    c.post("/search", data={"q": "mesh"}, headers=H)  # warms the cache
    up.routes.update(NCBI_DOWN)
    up.sent.clear()
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert "PubMed could not be reached; searched Europe PMC only" in r.text
    assert len(ncbi_sent(up)) == 3
    assert "<p>BACKGROUND: There is substantial media" in r.text  # PubMed's, from the cache
    assert TITLES_ONLY not in r.text


def test_efetch_429_with_papers_lacking_abstracts_shows_titles_only(client_for):
    c, _ = client_for({EFETCH: httpx.Response(429)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200 and BOTH_SEARCHED in r.text
    assert "No abstract available." in r.text and TITLES_ONLY in r.text


def test_efetch_failure_with_every_abstract_shown_has_no_titles_only_line(client_for):
    c, up = client_for({
        ESEARCH: httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}}),
        EFETCH: httpx.Response(503),
    })
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200 and BOTH_SEARCHED in r.text
    assert sent_to(up, "efetch.fcgi")  # the fetch was tried and failed
    assert "No abstract available." not in r.text
    assert TITLES_ONLY not in r.text


# ---------- cross-site form posts (review finding G) ----------

CROSS_SITE = "This request did not come from this page. Open the page and search again."


@pytest.mark.parametrize(("headers", "refused"), [
    ({}, False),  # old browsers, tests, curl
    ({"sec-fetch-site": "cross-site"}, True),
    ({"sec-fetch-site": "Cross-Site"}, True),
    ({"sec-fetch-site": "same-origin"}, False),
    ({"sec-fetch-site": " Same-Origin "}, False),
    ({"sec-fetch-site": "none"}, False),
    # a sibling subdomain of the same parent domain is not this page
    ({"sec-fetch-site": "same-site"}, True),
    ({"sec-fetch-site": "Same-Site"}, True),
    ({"sec-fetch-site": "something-new"}, True),
    ({"sec-fetch-site": ""}, True),
    ({"sec-fetch-site": "same-site", "origin": "https://app.test", "host": "app.test"}, True),
    # Sec-Fetch-Site decides when present, whatever Origin says
    ({"sec-fetch-site": "same-origin", "origin": "https://evil.example", "host": "app.test"}, False),
    ({"sec-fetch-site": "cross-site", "origin": "https://app.test", "host": "app.test"}, True),
    # no Sec-Fetch-Site: Origin against the request's own host
    ({"origin": "https://app.test", "host": "app.test"}, False),
    ({"origin": "https://APP.test", "host": "app.TEST"}, False),
    ({"origin": "https://app.test", "host": "app.test:443"}, False),
    ({"origin": "http://app.test", "host": "app.test:80"}, False),
    ({"origin": "https://app.test:8443", "host": "app.test"}, True),
    ({"origin": "https://evil.example", "host": "app.test"}, True),
    ({"origin": "https://app.test", "host": "app:8000", "x-forwarded-host": "app.test"}, False),
    ({"origin": "https://app.test", "host": "app.test", "x-forwarded-host": "other.test"}, True),
    ({"origin": "https://app.test", "host": "x", "x-forwarded-host": "app.test, proxy.test"}, False),
    ({"origin": "null", "host": "app.test"}, False),  # no host to compare: not evidence
    ({"origin": "https://app.test"}, False),  # no own host to compare
    ({"host": "app.test"}, False),
])
def test_is_cross_site(headers, refused):
    from pubmedgemini.web import is_cross_site

    assert is_cross_site(headers) is refused


@pytest.mark.parametrize("headers", [
    {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site"},
    {"origin": "https://evil.example"},
])
def test_cross_site_post_is_refused_before_anything_is_sent(client_search, upstream, caplog,
                                                            headers):
    import logging

    caplog.set_level(logging.INFO)
    r = client_search.post("/search", data={"q": "zq7 mesh"}, headers=H | headers)
    assert r.status_code == 403 and CROSS_SITE in r.text and upstream.sent == []
    assert "zq7" not in r.text
    lines = [x.getMessage() for x in caplog.records if x.getMessage().startswith("search ")]
    assert lines == ["search kind=refused_cross_site"]
    assert "evil" not in caplog.text and "cross-site" not in caplog.text
    assert "same-site" not in caplog.text


@pytest.mark.parametrize("headers", [
    {"sec-fetch-site": "same-origin", "origin": "http://testserver"},
    {"sec-fetch-site": "none"},
    {"origin": "http://testserver"},
    {},
])
def test_same_site_post_is_served(client_search, headers):
    r = client_search.post("/search", data={"q": "mesh"}, headers=H | headers)
    assert r.status_code == 200 and BOTH_SEARCHED in r.text


# ---------- a non-digit id from upstream never reaches a link or the cache (finding H) ----------

def test_non_digit_ids_from_upstream_never_reach_a_link_or_the_cache(client_for, db_url,
                                                                     monkeypatch):
    import psycopg

    from pubmedgemini.lit import tools as tools_module

    stored: list[str | None] = []
    real_put_many = tools_module.put_many

    def spy(pool, papers):
        stored.extend(p.pmid for p in papers)
        return real_put_many(pool, papers)

    monkeypatch.setattr(tools_module, "put_many", spy)
    esearch = json.loads(fixture("esearch.json"))
    esearch["esearchresult"]["idlist"].insert(0, "12x")
    esummary = json.loads(fixture("esummary.json"))
    esummary["result"]["uids"].insert(0, "12x")
    esummary["result"]["12x"] = {"uid": "12x", "title": "Odd uid paper", "pubtype": [],
                                 "articleids": [{"idtype": "doi", "value": "10.9/odd"}]}

    def odd_pmid(results):
        results[1]["pmid"] = "9a9"  # Europe PMC, a record that had no PMID

    c, up = client_for({ESEARCH: httpx.Response(200, json=esearch),
                        ESUMMARY: httpx.Response(200, json=esummary),
                        EPMC: epmc_reply_with(odd_pmid)})
    r = c.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 200 and "Odd uid paper" in r.text  # shown, without a PMID link
    for odd in ("12x", "9a9"):
        assert f"pubmed.ncbi.nlm.nih.gov/{odd}" not in r.text and f"PMID {odd}" not in r.text
    assert 'href="https://doi.org/10.9/odd"' in r.text
    assert stored and all(p is not None and p.isdigit() for p in stored), stored
    for req in sent_to(up, "efetch.fcgi"):
        assert "12x" not in req.url.params["id"] and "9a9" not in req.url.params["id"]
    with psycopg.connect(db_url) as conn:
        pmids = [row[0] for row in conn.execute("SELECT pmid FROM papers").fetchall()]
    assert pmids and "12x" not in pmids and "9a9" not in pmids
