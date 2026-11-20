"""F8: the answer as a PDF laid out as a short paper (frozen mock `docs/mocks/f8-answer-pdf-v1.pdf`,
owner, 20 Nov 2026). The server makes it from the stored answer; nothing is asked of Gemini and
the answer does not change. Left out (owner): search steps, abstracts, considered studies, the
completeness box. Ends with the references."""

import copy
import re
import uuid

import pytest
from test_answer_page import PMIDS, hernia
from test_ask_page import OWNER, H, insert_run
from test_run_answer_layout import store

from pubmedgemini.answer_pdf import SECTIONS, paper_html, pdf_name
from pubmedgemini.runs import get_run

PDF_HEADERS = {"content-type": "application/pdf"}


@pytest.fixture
def env(db_url, upstream):
    from test_ask_page import HAPPY_RUN, ask_env
    with ask_env(db_url, upstream, HAPPY_RUN) as e:
        yield e


@pytest.fixture
def url(env):
    return store(env, hernia())


# ---------- the route ----------

def test_own_finished_answer_downloads_as_a_pdf(env, url):
    r = env.c.get(f"{url}/pdf", headers=H)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    cd = r.headers["content-disposition"]
    assert cd.startswith("attachment;") and re.search(r'filename="PubMedAi-\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.pdf"', cd)
    assert r.content.startswith(b"%PDF-") and len(r.content) > 5000
    assert r.headers.get("cache-control") == "no-store"


def test_another_users_run_is_404(env, url):
    env.add_user("b@example.com")
    r = env.c.get(f"{url}/pdf", headers={"x-dev-user-email": "b@example.com"})
    assert r.status_code == 404 and not r.content.startswith(b"%PDF")


@pytest.mark.parametrize("status", ["running", "queued"])
def test_a_working_run_has_no_pdf(env, status):
    u = store(env, hernia(), status=status)
    assert env.c.get(f"{u}/pdf", headers=H).status_code == 404


def test_a_run_without_an_answer_has_no_pdf(env):
    rid = insert_run(env)  # stopped, nothing stored
    assert env.c.get(f"/runs/{rid}/pdf", headers=H).status_code == 404


def test_malformed_id_is_404(env):
    assert env.c.get("/runs/not-a-run/pdf", headers=H).status_code == 404


def test_signed_out_gets_no_pdf(env, url):
    r = env.c.get(f"{url}/pdf", follow_redirects=False)
    assert r.status_code in (302, 303, 307, 401, 403) and not r.content.startswith(b"%PDF")


# ---------- the button ----------

def test_answer_page_has_the_download_link(env, url):
    t = env.c.get(url, headers=H).text
    assert f'<a class="pdf" href="{url}/pdf">Download PDF</a>' in t


def test_no_download_link_while_working_or_without_answer(env):
    working = store(env, hernia(), status="running")
    assert "Download PDF" not in env.c.get(working, headers=H).text
    rid = insert_run(env)
    assert "Download PDF" not in env.c.get(f"/runs/{rid}", headers=H).text


# ---------- the paper ----------

def paper_of(env, url):
    v = get_run(env.pool, uuid.UUID(url.split("/")[2]), env.user_id(OWNER))
    return paper_html(v)


@pytest.fixture
def paper(env, url):
    return paper_of(env, url)


def test_sections_in_the_mocks_order_ending_with_references(paper):
    assert SECTIONS == ("Summary", "Introduction", "Methods", "Results", "Discussion",
                        "Conclusion and clinical practice", "References")
    # this run has no Introduction stored: the numbers close up
    heads = re.findall(r"<h2>([^<]+)</h2>", paper)
    assert heads == ["Summary", "1. Methods", "2. Results", "3. Discussion",
                     "4. Conclusion and clinical practice", "References"]


def test_introduction_is_section_1_when_stored(env, url):
    rid = url.split("/")[2]
    with env.pool.connection() as conn:
        conn.execute("INSERT INTO run_intros (run_id, status, text) VALUES (%s, 'done', %s)",
                     (rid, "Chronic groin pain matters."))
    html = paper_of(env, url)
    heads = re.findall(r"<h2>([^<]+)</h2>", html)
    assert heads[:3] == ["Summary", "1. Introduction", "2. Methods"]
    assert "Chronic groin pain matters." in html


def test_every_reference_with_its_pubmed_link(paper):
    for n, p in enumerate(PMIDS, start=1):
        assert f'href="https://pubmed.ncbi.nlm.nih.gov/{p}/"' in paper
        assert f"Title {n}." in paper


def test_results_have_tables_and_the_ci_figure(paper):
    for words in ("Table 1.", "Figure 1.", "Table 2.", "Selected studies",
                  "Every estimate with a 95% confidence interval"):
        assert words in paper, words
    assert "<svg" in paper and "0.38" in paper


def test_left_out_parts_are_not_in_the_paper(env, url, paper):
    # store() writes steps, a considered study and a contradiction note: none reach the paper
    for left_out in ("Researcher: searching PubMed", "Older.", "Steps (", "Completeness",
                     "Considered, not selected", "Abstract not available"):
        assert left_out not in paper, left_out


def test_methods_name_the_searches_and_counts(paper):
    m = paper[paper.index(". Methods</h2>"):paper.index(". Results</h2>")]
    assert "PubMed" in m and "Europe PMC" in m and "hernia[tiab] AND mesh" in m
    assert "2 studies were selected" in m


def test_model_text_is_escaped(env):
    body = hernia()
    b = copy.deepcopy(body)
    b.answer.bottom_line.text = "<script>alert(1)</script> Laparoscopic is better [90000001]."
    html = paper_of(env, store(env, b))
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_no_outside_address_but_pubmed_and_the_app(paper):
    for m in re.findall(r'(?:href|src)="([^"]+)"', paper):
        assert m.startswith(("https://pubmed.ncbi.nlm.nih.gov/", "#")), m
    assert "@import" not in paper and "http://" not in paper


def test_file_name_is_safe():
    assert pdf_name("In adults with an inguinal hernia, TEP vs Lichtenstein?",
                    "2026-11-20") == "PubMedAi-2026-11-20-in-adults-with-an-inguinal-hernia.pdf"
    assert pdf_name('"; rm -rf / <x>', "2026-11-20") == "PubMedAi-2026-11-20-rm-rf-x.pdf"
    assert pdf_name("???", "2026-11-20") == "PubMedAi-2026-11-20-answer.pdf"
