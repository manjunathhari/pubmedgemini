"""Task 8 (F3): the answer page as the frozen mock (`docs/mocks/f3-answer-page-v4.html`), and
the run page's link to it.

The page tests store the hand-written hernia answer (`test_answer_page.hernia`: the mock's
numbers, eight references) for a run of the dev app, then open the page through the real
route; one test runs a whole question inline (the writer's fake parts) and follows the run
page's link. No test reaches the network."""

import copy
import re
import uuid
from pathlib import Path

import pytest
from test_answer_page import PMIDS, hernia, num, outcome
from test_ask_page import HAPPY_RUN, OWNER, QUESTION, RUN_NOT_FOUND, H, ask_env, insert_run

from pubmedgemini.runs import (
    WRITER_TRUNCATED,
    create_run,
    finish_run,
    save_answer,
    set_running,
)

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "docs" / "mocks" / "f3-answer-page-v4.html"


def style_block(page: str) -> str:
    m = re.search(r"<style>(.*?)</style>", page, re.DOTALL)
    assert m, "no style block"
    return m.group(1)


def store(env, body, question=QUESTION, email=OWNER, written="parts"):
    """A finished run of `email` with `body` as its stored answer; returns its answer URL."""
    rid = create_run(env.pool, env.user_id(email), question, "gemini-3.5-flash-lite")
    set_running(env.pool, rid)
    assert save_answer(env.pool, rid, body, written) is not None
    finish_run(env.pool, rid, "done", None, "turn_cap", 8)
    return f"/runs/{rid}/answer"


@pytest.fixture
def env(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN) as e:
        yield e


@pytest.fixture
def answer_url(env):
    return store(env, hernia())


def page(env, url):
    r = env.c.get(url, headers=H)
    assert r.status_code == 200, r.status_code
    return r.text


# ---------- the mock's blocks ----------

def test_answer_page_has_the_mocks_blocks_in_order(env, answer_url):
    t = page(env, answer_url)
    order = ["Bottom line", "Why this conclusion", "Every estimate with a 95% CI",
             "Outcome by outcome", "Counselling dashboard",
             "Out of 100 people like those in the trials", "Compare the arms", "The trials",
             "References"]
    assert [t.index(x) for x in order] == sorted(t.index(x) for x in order)
    assert t.count('class="two"') == 2 and "<script" not in t.lower()
    assert not re.search(r"\son[a-z]+=", t)
    assert t.startswith("<!doctype html>") and "<title>pubmedgemini answer</title>" in t


def test_stylesheet_is_the_mocks_byte_for_byte(env, answer_url):
    assert style_block(page(env, answer_url)) == style_block(MOCK.read_text())


def test_outcomes_are_closed_details_and_the_fold_is_a_details(env, answer_url):
    t = page(env, answer_url)
    assert t.count('<details class="oc"') == 6 and '<details class="oc" open' not in t
    assert ('<details class="more"><summary><span class="a">Show 2 more outcomes</span>'
            '<span class="b">Show fewer</span></summary>') in t
    assert t.count('<details class="more">') == 2  # the outcomes card and the out-of-100 card
    assert "6 outcomes · tap to open" in t


def test_drawn_rows_carry_code_positions_only(env, answer_url):
    t = page(env, answer_url)
    assert 'style="left:34.4%;width:20%"' in t and 'style="left:76.9%"' in t
    assert 'style="left:12.2%;width:40.8%"' in t and 'style="left:80%"' in t
    assert ('<div class="scale"><span class="l">0.1</span><span style="left:76.9%">1</span>'
            '<span class="r">2</span></div>') in t
    assert ('<div class="scale"><span class="l">−8</span><span style="left:80%">0</span>'
            '<span class="r">+2</span></div>') in t
    assert 'aria-label="Risk ratio 0.38, interval 0.28 to 0.51, below 1"' in t
    styles = re.findall(r'style="([^"]*)"', t)
    assert styles and all(re.fullmatch(
        r"(left|width):-?\d+(\.\d)?%(;(left|width):\d+(\.\d)?%)?|margin-top:12px|"
        r"margin-top:10px|margin-top:0", s) for s in styles), styles


def test_dots_are_one_hundred_with_the_count_on(env, answer_url):
    t = page(env, answer_url)
    grids = re.findall(r'<div class="dots" aria-hidden="true">(.*?)</div>', t)
    assert len(grids) == 10  # five out-of-100 items, two arms each
    assert all(g.count("<i ") == 100 for g in grids)
    assert [g.count('class="on"') for g in grids[:2]] == [9, 19]
    assert '<span class="big">9</span> <span class="muted">of 100 · TEP</span>' in t
    assert ("<p><b>Chronic pain at 5 years:</b> 9.4% with TEP vs 18.8% with Lichtenstein "
            '[<a href="#ref-4">4</a>]. Certainty for this outcome: Moderate.</p>') in t


def test_bottom_line_and_default_choice(env, answer_url):
    t = page(env, answer_url)
    assert ('Laparoscopic repair reduces chronic postoperative pain compared with Lichtenstein '
            'repair [<a href="#ref-1">1</a>, <a href="#ref-2">2</a>, <a href="#ref-3">3</a>].'
            ) in t
    assert '<span class="pill mo">Moderate certainty</span> Consistent direction' in t
    assert "<b>Default choice:</b> Offer TEP or TAPP" in t
    assert "<b>Who may benefit:</b> Patients most concerned" in t
    assert "<b>Cost per case:</b> not known" in t
    assert "<h3>At a glance</h3>" in t and "<b>Main caveat:</b> pain definitions vary" in t
    assert t.index("At a glance") < t.index("Bottom line")


def test_the_other_parts_have_their_places(env, answer_url):  # D5 option b
    t = page(env, answer_url)
    order = ["Compare the arms", "Conflicting evidence", "Applicability",
             "What would change this answer", "The trials", "Other notable findings",
             "Search trail", "References"]
    assert [t.index(f"<h2>{x}</h2>") if x != "Compare the arms" else t.index(x)
            for x in order[:4]] == sorted(t.index(f"<h2>{x}</h2>") if x != "Compare the arms"
                                          else t.index(x) for x in order[:4])
    assert t.index("<h2>The trials</h2>") < t.index("<h2>Other notable findings</h2>") \
        < t.index("<h2>References</h2>")
    assert "<b>India and cost:</b> Not known from these abstracts" in t
    assert "A trial reporting costs in Indian practice." in t
    assert "One surgeon&#39;s results raised the TEP recurrence rate" in t
    assert "<h2>Search trail</h2>" not in t  # the hand-written answer has no searches


def test_trials_and_references(env, answer_url):
    t = page(env, answer_url)
    assert ('<p><b>[<a href="#ref-1">1</a>] TEP vs Lichtenstein meta-analysis, 2025, Hernia.'
            '</b> Meta-analysis of 27 RCTs · n = 7,658 · Mixed centres, mostly Europe · TEP vs '
            'Lichtenstein. Chronic pain RR 0.38; recurrence RR 1.03 [<a href="#ref-1">1</a>].'
            '</p>') in t
    assert '<p class="muted">Limitations: Open-label trials; pain definitions vary.</p>' in t
    assert ('<p id="ref-1">[1] Title 1. Hernia, 2020. '
            '<a href="https://pubmed.ncbi.nlm.nih.gov/90000001/">PMID 90000001</a></p>') in t
    assert ("<p class=\"muted\">Searched PubMed and Europe PMC (free sources); written by "
            "gemini-3.5-flash-lite from the abstracts.</p>") in t
    assert "Google may use what you type here" in t
    assert '<a href="/">Home</a> · <a href="/runs/' in t and ">Steps and studies</a>" in t


def test_compare_rows_are_text(env, answer_url):
    t = page(env, answer_url)
    assert ('<div class="row"><b>Chronic pain at 5 years</b><span class="pill mo">Moderate'
            '</span><span class="mono">TEP 9.4% vs Lichtenstein 18.8%</span><span></span>'
            '<span class="muted">'
            'Swedish trial [<a href="#ref-4">4</a>] · p &lt;0.001</span></div>') in t
    assert '<p class="eyebrow">TAPP vs Lichtenstein</p>' in t


# ---------- untrusted text, flags, empty answers ----------

def _hostile():
    b = hernia()
    o = b.answer.outcomes[0]
    o.outcome = '"><img src=x onerror=1>'
    o.effect_text = "<script>x</script></details> [90000001]"
    o.numbers[3].arm = "<b>"
    b.answer.studies[0].comparison = '</p><details open>'
    b.references[0].title = "<i>"
    b.answer.bottom_line.text = "a [99999999] b"
    return b


def test_model_text_is_escaped_everywhere_on_the_answer_page(env):
    t = page(env, store(env, _hostile()))
    assert "&#34;&gt;&lt;img src=x onerror=1&gt;" in t and "<img" not in t
    assert "&lt;script&gt;x&lt;/script&gt;&lt;/details&gt;" in t and "<script" not in t
    assert "&lt;b&gt;" in t and "&lt;i&gt;" in t and "&lt;/p&gt;&lt;details open&gt;" in t
    assert t.count("<details") == t.count("</details>") == 6 + 2
    assert not re.search(r"\son[a-z]+=", t.replace("onerror=1&gt;", ""))


def test_unknown_citation_is_flagged_not_dropped(env):
    t = page(env, store(env, _hostile()))
    assert "a [not in the study list] b" in t


def test_answer_route_404s(env, answer_url):
    assert env.c.get(answer_url, headers=H).status_code == 200
    env.add_user("b@example.com")
    other = env.c.get(answer_url, headers={"x-dev-user-email": "b@example.com"})
    assert other.status_code == 404 and RUN_NOT_FOUND in other.text
    f2 = insert_run(env, status="done", error_kind=None)
    for url in (f"/runs/{f2}/answer", f"/runs/{uuid.uuid4()}/answer", "/runs/zq7-x/answer",
                answer_url.upper().replace("/RUNS/", "/runs/").replace("/ANSWER", "/answer")):
        r = env.c.get(url, headers=H)
        assert r.status_code == 404 and RUN_NOT_FOUND in r.text, url


def test_unreadable_stored_answer_is_404_and_logged(env, answer_url, caplog):
    import logging

    rid = answer_url.split("/")[2]
    with env.pool.connection() as conn:
        conn.execute("UPDATE answers SET body = '{\"answer\": 3}'::jsonb WHERE run_id = %s",
                     (rid,))
    caplog.set_level(logging.INFO)
    r = env.c.get(answer_url, headers=H)
    assert r.status_code == 404 and RUN_NOT_FOUND in r.text
    assert f"answer kind=unreadable run_id={rid}" in [m.getMessage() for m in caplog.records]


def test_run_page_links_to_the_answer_only_when_there_is_one(env):
    r = env.ask(QUESTION)
    loc = r.headers["location"]
    run_page = env.run_page(loc).text
    assert f'<p><a href="/">Home</a></p>\n<p><a href="{loc}/answer">Read the answer</a></p>' \
        in run_page
    t = page(env, f"{loc}/answer")  # a whole run, written by the writer's fake parts
    assert "Bottom line" in t and "Chronic postoperative groin pain" in t
    assert "[not in the study list]" in t  # the fixture cites studies this run did not select
    f2 = insert_run(env, status="done", error_kind=None)
    assert "Read the answer" not in env.run_page(f"/runs/{f2}").text


def test_empty_answer_shows_its_reason_and_the_references(env):
    b = hernia()
    b.answer, b.empty_reason = None, WRITER_TRUNCATED
    t = page(env, store(env, b, written=None))
    assert WRITER_TRUNCATED.replace("'", "&#39;") in t
    assert '<p id="ref-8">[8] Title 8.' in t and "<h2>References</h2>" in t
    assert 'class="two"' not in t and "Why this conclusion" not in t
    assert "Counselling dashboard" not in t and "<h2>The trials</h2>" not in t


def test_no_numbers_note_replaces_the_ci_card(env):
    b = hernia()
    for o in b.answer.outcomes:
        o.numbers = []
    t = page(env, store(env, b))
    assert ("This answer has no structured numbers, so the arm comparison and CI charts are "
            "not shown. Everything else renders from the stored text.") in t
    assert "Every estimate with a 95% CI" not in t and "Outcome by outcome" in t
    assert t.count('class="two"') == 1 and "Counselling dashboard" not in t


def test_a_pair_with_one_empty_panel_draws_the_other_alone(env):
    b = hernia()
    for o in b.answer.outcomes:  # no CI rows: the outcomes card alone; pairs stay
        for n in o.numbers:
            n.ci_low = n.ci_high = None
    t = page(env, store(env, b))
    assert "Every estimate with a 95% CI" not in t and "Outcome by outcome" in t
    assert t.count('class="two"') == 1 and "Show 2 more outcomes" not in t
    assert '<section class="card" style="margin-top:10px">' in t


def test_phone_width_rules_are_present(env, answer_url):
    t = page(env, answer_url)
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in t
    css = style_block(t)
    assert css.index(".two{display:grid;gap:14px;grid-template-columns:minmax(0,1fr)") < \
        css.index("@media (min-width:700px)")
    assert not re.search(r'style="[^"]*width:\d+px', t)


def test_old_f2_runs_have_no_answer_page_and_look_as_before(env):
    rid = insert_run(env, status="done", error_kind=None)
    t = env.run_page(f"/runs/{rid}").text
    assert "Read the answer" not in t and "No relevant evidence found." in t
    assert env.c.get(f"/runs/{rid}/answer", headers=H).status_code == 404


def test_the_page_never_changes_the_stored_answer(env, answer_url):
    rid = answer_url.split("/")[2]
    before = env.one("SELECT body FROM answers WHERE run_id = %s", (rid,))
    page(env, answer_url)
    assert env.one("SELECT body FROM answers WHERE run_id = %s", (rid,)) == copy.deepcopy(before)


def _with_numbers(*numbers):
    body = copy.deepcopy(hernia())
    body.answer.outcomes.append(outcome("Odd numbers", list(numbers)))
    return body


def test_numbers_not_drawn_are_listed_inside_their_outcome(env):  # review M3
    t = page(env, store(env, _with_numbers(num("Seroma", 0.4, "RR", pmid=PMIDS[0]))))
    last = t[t.rindex('<details class="oc"'):]
    last = last[:last.index("</details>")]
    assert "<span class=\"meta\">1 number</span>" in last
    assert ('<div class="ci"><p class="lab"><b>Seroma</b> <span class="mono">RR 0.4</span></p>'
            '<p class="muted">TEP vs Lichtenstein meta-analysis [<a href="#ref-1">1</a>] · '
            'Not drawn: no interval.</p></div>') in last


def test_numbers_no_scale_can_hold_never_500_the_page(env):  # review M5
    t = page(env, store(env, _with_numbers(
        num("Mean difference", 0.0, "days", None, PMIDS[0], -1e308, 1e308),
        num("Mean difference B", 5e-13, "days", None, PMIDS[0], 0.0, 1e-12),
        num("Seroma", 1e-300, "RR", None, PMIDS[0], 1e-300, 1e-299),
        num("Pain", 1e300, "OR", None, PMIDS[0], 1e299, 1e300))))
    assert t.count("Not drawn: the interval is too wide to draw.") == 1
    assert t.count("Not drawn: the interval is too narrow to draw.") == 1


def _rows_of(t, label):
    return re.findall(rf'<div class="row"><b>{label}</b>.*?</div>', t)


def test_a_pair_both_rounding_to_0_shows_its_reason_in_compare(env):  # review L7
    t = page(env, store(env, _with_numbers(num("Mesh infection", 0.4, "%", "TEP", PMIDS[0]),
                                           num("Mesh infection", 0.2, "%", "L", PMIDS[0]))))
    [row] = _rows_of(t, "Mesh infection")
    assert '<span class="muted">Not drawn: both arms round to 0 of 100.</span>' in row
    assert "of 100 · TEP</span>" in t  # the hernia's own items are still drawn
    assert "Mesh infection:</b>" not in t


def test_a_pair_failing_a_check_shows_its_reason_in_compare(env):  # review L8
    t = page(env, store(env, _with_numbers(num("Seroma", 140.0, "%", "TEP", PMIDS[0]),
                                           num("Seroma", 20.0, "%", "L", PMIDS[0]))))
    [row] = _rows_of(t, "Seroma")
    assert "<span class=\"mono\">TEP 140% vs L 20%</span>" in row
    assert '<span class="muted">Not drawn: a percentage must be from 0 to 100.</span>' in row


def test_compare_rows_name_each_arm_with_its_value(env):  # review H1 (d23), the reviewer's case
    t = page(env, store(env, _with_numbers(num("Groin pain", 18.8, "%", "Open repair", PMIDS[0]),
                                           num("Groin pain", 9.4, "%", "Laparoscopic repair",
                                               PMIDS[0]))))
    [row] = _rows_of(t, "Groin pain")
    assert '<span class="mono">Open repair 18.8% vs Laparoscopic repair 9.4%</span>' in row
    assert "18.8% vs 9.4%" not in t.replace("Open repair 18.8% vs Laparoscopic repair 9.4%", "")
    for r in re.findall(r'<div class="row">.*?</div>', t):  # no bare "x% vs y%" anywhere
        assert not re.search(r'<span class="mono">[\d.]+%? vs', r), r


def test_long_numbers_sit_where_the_v4_wrap_rule_applies(env):  # review M6 (d25)
    rule = ".ci .lab .mono,.row .mono{white-space:normal;overflow-wrap:anywhere}"
    t = page(env, store(env, _with_numbers(
        num("Pain score difference", -1.2345, "points on the VAS (0 to 10)", None, PMIDS[0],
            -1.9876, -0.5432),
        num("Tiny ratio", 1e-300, "RR", None, PMIDS[0], 1e-300, 1e-299),
        num("Tiny listed", 1e-300, "RR", None, PMIDS[0]))))
    assert rule in style_block(t)
    assert ('<span class="mono">−1.2345 points on the VAS (0 to 10) (−1.9876 to −0.5432)'
            '</span>') in t
    # every .mono is inside a .ci .lab or a .row, so the rule reaches every long number
    for m in re.finditer(r'<span class="mono">', t):
        before = t[:m.start()]
        assert (before.rfind('<p class="lab">') > before.rfind("</p>")
                or before.rfind('<div class="row">') > before.rfind("</div>")), t[m.start():][:60]


@pytest.mark.parametrize("v,lo,hi", [(0.0, -5.0, 1e308), (10.0, 8.0, 1e308), (0.0, -1e308, 0.8),
                                     (-10.0, -1e308, -5.0), (5e306, 1e306, 1e307)])
def test_a_difference_whose_axis_overflows_never_500s_the_page(env, v, lo, hi):  # re-check M-B
    t = page(env, store(env, _with_numbers(
        num("Mean difference in stay", v, "days", None, PMIDS[0], lo, hi))))
    i = t.index("<b>Mean difference in stay</b>")
    assert "Not drawn: the interval is too wide to draw." in t[i:i + 2000]
