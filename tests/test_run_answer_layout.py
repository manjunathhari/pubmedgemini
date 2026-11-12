"""F3b Task 4: the run page draws a stored answer as mock v6 (`docs/mocks/f3b-answer-page-v6.html`),
ported from f3-writer's `tests/test_answer_route.py` onto `/runs/{id}` (owner decision N1 A).

The page tests store the hand-written hernia answer (`test_answer_page.hernia`: the frozen
mock's numbers, eight references, made-up PMIDs) for a finished run of the dev app, with steps,
searches and a study list written straight into the run's tables, then open the run page through
the real route. One test runs a whole question inline (the writer's fake parts). No test reaches
the network."""

import copy
import re
import uuid
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from test_answer_page import PMIDS, hernia, num, outcome
from test_ask_page import HAPPY_RUN, OWNER, QUESTION, RUN_NOT_FOUND, H, ask_env, insert_run

ROOT = Path(__file__).resolve().parents[1]
MOCK = ROOT / "docs" / "mocks" / "f3b-answer-page-v6.html"
CSS = ROOT / "src" / "pubmedgemini" / "static" / "app.css"
INVISIBLE = "".join(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E,
                                     0x200F, 0x061C, *range(0x200B, 0x200E), 0x2060, 0xFEFF))
STEPS = ["Researcher: searching PubMed and Europe PMC", "Researcher: selected 2 studies",
         "Answer ready"]


def store(env, body, *, status="done", email=OWNER, extras=True):
    """A finished run of `email` with `body` (an AnswerV3) as its stored answer; with `extras`,
    also steps, two searches, two selected studies, one considered and one contradiction."""
    rid = insert_run(env, status=status, error_kind=None, email=email)
    with env.pool.connection() as conn:
        conn.execute("INSERT INTO answers (run_id, version, body) VALUES (%s, 3, %s)",
                     (rid, Jsonb(body.to_body())))
        if extras:
            for i, line in enumerate(STEPS, start=1):
                conn.execute("INSERT INTO run_steps (run_id, seq, kind, line) "
                             "VALUES (%s, %s, 'note', %s)", (rid, i, line))
            for i, (source, query, hits) in enumerate(
                    [("PubMed", "hernia[tiab] AND mesh", 1234), ("Europe PMC", "hernia", 56)],
                    start=1):
                conn.execute("INSERT INTO run_searches (run_id, seq, source, query, hits) "
                             "VALUES (%s, %s, %s, %s, %s)", (rid, i, source, query, hits))
            for rank, (role, pmid, design, note) in enumerate([
                    ("selected", PMIDS[0], "SR_MA", "The newest meta-analysis."),
                    ("selected", PMIDS[3], "RCT", "A large trial."),
                    ("considered", "10000008", None, "Older."),
                    ("contradiction", "10000009", None, "Found more pain.")]):
                conn.execute("INSERT INTO run_papers (run_id, role, rank, pmid, design, note) "
                             "VALUES (%s, %s, %s, %s, %s, %s)", (rid, role, rank, pmid, design,
                                                                 note))
    return f"/runs/{rid}"


@pytest.fixture
def env(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN) as e:
        yield e


@pytest.fixture
def url(env):
    return store(env, hernia())


def page(env, url):
    r = env.c.get(url, headers=H)
    assert r.status_code == 200, r.status_code
    return r.text


# ---------- the mock's blocks ----------

def test_blocks_in_the_mocks_order(env, url):
    t = page(env, url)
    order = ["Bottom line", "Why this conclusion", "Every estimate with a 95% CI",
             "Outcome by outcome", "Counselling dashboard",
             "Out of 100 people like those in the trials", "Compare the arms", "The trials",
             "Search trail", "References", "Selected studies"]
    assert [t.index(x) for x in order] == sorted(t.index(x) for x in order)
    assert t.count('class="two"') == 2
    assert t.startswith("<!doctype html>")
    assert t.index("<h1>") < t.index("Steps (3)") < t.index("<h2>At a glance</h2>")
    assert "<p class=\"sub\">Done · Quick · Decision support. The clinician decides.</p>" in t
    assert t.index("Selected studies") < t.index("Considered, not selected") \
        < t.index("Possible contradictions") < t.index("answer written by")


def test_no_style_no_script_no_new_link(env, url):
    t = page(env, url)
    plain = env.run_page(f"/runs/{insert_run(env)}").text  # a run with no answer
    assert "style=" not in t and "<style" not in t
    assert "<script" not in t.lower() and not re.search(r"\son[a-z]+=", t)
    assert t.count("<link") == plain.count("<link") == 1


def test_svg_rows_carry_code_numbers_only(env, url):
    t = page(env, url)
    xs = re.findall(r'\sx[12]?="([^"]*)"', t)
    assert xs and all(re.fullmatch(r"\d+(\.\d)?%|0|100%", x) for x in xs), xs
    ys = re.findall(r'\sy[12]?="([^"]*)"', t)
    assert set(ys) <= {"0", "3", "11", "19", "21.5", "22", "35"}
    assert ('<line class="null" x1="76.9%" x2="76.9%" y1="0" y2="22"/>'
            '<line class="span" x1="34.4%" x2="54.4%" y1="11" y2="11"/>'
            '<line class="est" x1="44.6%" x2="44.6%" y1="3" y2="19"/>') in t
    assert ('<text x="0" y="35">0.1</text><text class="mid" x="76.9%" y="35">1</text>'
            '<text class="end" x="100%" y="35">2</text>') in t
    # the difference row: -8 to +2, 0 at 80 %
    assert ('<line class="null" x1="80%" x2="80%" y1="0" y2="22"/>'
            '<line class="span" x1="12.2%" x2="53%" y1="11" y2="11"/>') in t
    assert ('<text x="0" y="35">−8</text><text class="mid" x="80%" y="35">0</text>'
            '<text class="end" x="100%" y="35">+2</text>') in t
    assert ('<svg class="track" role="img" aria-label="Risk ratio 0.38, interval 0.28 to 0.51, '
            'below 1">') in t
    assert "viewBox" not in t


def test_hundred_squares_each(env, url):
    t = page(env, url)
    grids = re.findall(r'<div class="dots" aria-hidden="true">(.*?)</div>', t)
    assert len(grids) == 10  # five out-of-100 items, two arms each
    for g in grids:
        cells = re.findall(r"<i[^>]*>", g)
        assert len(cells) == 100 and set(cells) <= {'<i class="on">', '<i class="off">'}
        assert g == "".join(cells).replace(">", "></i>")  # nothing else in a grid
    assert [g.count('class="on"') for g in grids[:2]] == [9, 19]
    assert ('<div class="A"><span class="big">9</span> <span class="armname"><i class="key"></i>'
            'of 100 · TEP</span>') in t
    assert ('<div class="B"><span class="big">19</span> <span class="armname"><i class="key">'
            '</i>of 100 · Lichtenstein</span>') in t
    assert ("<p><b>Chronic pain at 5 years:</b> 9.4% with TEP vs 18.8% with Lichtenstein "
            '[<a href="#ref-4">4</a>]. Certainty for this outcome: Moderate.</p>') in t
    assert t.count('<div class="cp first">') == 1


def test_outcomes_closed_and_fold(env, url):
    t = page(env, url)
    assert t.count('<details class="oc"') == 6 and '<details class="oc" open' not in t
    assert ('<details class="more"><summary><span class="a">Show 2 more outcomes</span>'
            '<span class="b">Show fewer</span></summary>') in t
    assert t.count('<details class="more">') == 2  # the outcomes card and the out-of-100 card
    assert '<span class="sub">6 outcomes · tap to open</span>' in t
    assert not re.search(r"<details[^>]*\sopen", t)  # every fold starts closed


def test_steps_folded(env, url):
    t = page(env, url)
    m = re.search(r'<section class="card">\n?<details class="fold"><summary>Steps \(3\)</summary>'
                  r"(.*?)</details>", t, re.DOTALL)
    assert m
    for line in STEPS:
        assert f"<li>{line}</li>" in m.group(1)


def test_studies_folded(env, url):
    t = page(env, url)
    for head, inside in [
            ("Selected studies, with their abstracts (2)",
             ["<p>The newest meta-analysis.</p>", "<p>A large trial.</p>",
              f'<a href="https://pubmed.ncbi.nlm.nih.gov/{PMIDS[0]}/">PMID {PMIDS[0]}</a>',
              "Abstract not available."]),
            ("Considered, not selected (1)", ["Older."]),
            ("Possible contradictions (1)", ["Found more pain."])]:
        m = re.search(r'<details class="fold"><summary>' + re.escape(head) + r"</summary>(.*?)"
                      r"</details>", t, re.DOTALL)
        assert m, head
        for x in inside:
            assert x in m.group(1), (head, x)


def test_main_is_wide_only_here(env, url):
    assert "<main class=\"wide\">" in page(env, url)
    for other in ("/", "/library", "/activity", f"/runs/{insert_run(env)}",
                  f"/runs/{insert_run(env, status='running', error_kind=None)}"):
        t = page(env, other)
        assert "<main>" in t and "wide" not in t, other


def test_bottom_line_and_default_choice(env, url):
    t = page(env, url)
    assert ('Laparoscopic repair reduces chronic postoperative pain compared with Lichtenstein '
            'repair [<a href="#ref-1">1</a>, <a href="#ref-2">2</a>, <a href="#ref-3">3</a>].'
            ) in t
    assert '<span class="pill mo">Moderate certainty</span> Consistent direction' in t
    assert "<b>Default choice:</b> Offer TEP or TAPP" in t
    assert "<b>Who may benefit:</b> Patients most concerned" in t
    assert "<b>Cost per case:</b> not known" in t
    assert "<h2>At a glance</h2>" in t and "<b>Main caveat:</b> pain definitions vary" in t
    assert t.index("At a glance") < t.index("Bottom line")
    assert "<h2>Clinical practice</h2>" not in t  # the practice lines sit in Bottom line


def test_the_other_parts_have_their_places(env, url):
    t = page(env, url)
    order = ["Compare the arms", "<h2>Conflicting evidence</h2>", "<h2>Applicability</h2>",
             "<h2>What would change this answer</h2>", "<h2>The trials</h2>",
             "<h2>Other notable findings</h2>", "<h2>Search trail</h2>", "<h2>References</h2>"]
    assert [t.index(x) for x in order] == sorted(t.index(x) for x in order)
    assert "<b>India and cost:</b> Not known from these abstracts" in t
    assert "A trial reporting costs in Indian practice." in t
    assert "One surgeon&#39;s results raised the TEP recurrence rate" in t
    assert "<li>PubMed: hernia[tiab] AND mesh (1,234 found)</li>" in t
    assert "<li>Europe PMC: hernia (56 found)</li>" in t


def test_trials_and_references(env, url):
    t = page(env, url)
    assert ('<p><b>[<a href="#ref-1">1</a>] TEP vs Lichtenstein meta-analysis, 2025, Hernia.'
            '</b> Meta-analysis of 27 RCTs · n = 7,658 · Mixed centres, mostly Europe · TEP vs '
            'Lichtenstein. Chronic pain RR 0.38; recurrence RR 1.03 [<a href="#ref-1">1</a>].'
            '</p>') in t
    assert '<p class="muted">Limitations: Open-label trials; pain definitions vary.</p>' in t
    assert ('<li id="ref-1">[1] Title 1. Hernia, 2020. '
            '<a href="https://pubmed.ncbi.nlm.nih.gov/90000001/">PMID 90000001</a></li>') in t
    assert ('<p class="foot muted">Searched PubMed and Europe PMC (free sources); answer written '
            'by gemini-3.5-flash-lite</p>') in t
    assert "Google may use what you type here" in t


def test_compare_rows_are_text(env, url):
    t = page(env, url)
    assert ('<div class="cmp"><b>Chronic pain at 5 years</b><span class="pill mo">Moderate'
            '</span><span class="num">TEP 9.4% vs Lichtenstein 18.8%</span>'
            '<span class="muted">Swedish trial [<a href="#ref-4">4</a>] · p &lt;0.001</span>'
            '</div>') in t
    assert '<p class="eyebrow">TAPP vs Lichtenstein</p>' in t


# ---------- untrusted text, flags, empty answers ----------

def _hostile():
    b = hernia()
    o = b.answer.outcomes[0]
    o.outcome = '"><img src=x onerror=1>'
    o.effect_text = "<script>x</script></details> [90000001]"
    o.numbers[3].arm = "<b>"
    o.numbers[0].label = "<svg onload=1>"
    b.answer.studies[0].comparison = '</p><details open>'
    b.references[0].title = "<i>"
    b.answer.bottom_line.text = "a [99999999] b"
    return b


def test_model_text_escaped_everywhere(env):
    t = page(env, store(env, _hostile()))
    assert "&#34;&gt;&lt;img src=x onerror=1&gt;" in t and "<img" not in t
    assert "&lt;script&gt;x&lt;/script&gt;&lt;/details&gt;" in t and "<script" not in t
    assert "&lt;b&gt;" in t and "&lt;i&gt;" in t and "&lt;/p&gt;&lt;details open&gt;" in t
    assert "&lt;svg onload=1&gt;" in t
    # code's details only: 3 folds at the end, 1 for the steps, 6 outcomes, 2 "Show N more"
    assert t.count("<details") == t.count("</details>") == 3 + 1 + 6 + 2
    assert t.count("<svg") == t.count("</svg>") == len(re.findall(r'<svg class="track"', t))
    assert not re.search(r"\son[a-z]+=", t.replace("onerror=1&gt;", "")
                         .replace("onload=1&gt;", ""))


def test_unknown_citation_is_flagged_not_dropped(env):
    t = page(env, store(env, _hostile()))
    assert "a [not in the study list] b" in t


def test_another_users_answered_run_is_404(env, url):
    env.add_user("b@example.com")
    other = env.c.get(url, headers={"x-dev-user-email": "b@example.com"})
    assert other.status_code == 404 and RUN_NOT_FOUND in other.text
    assert env.c.get(f"{url}/answer", headers=H).status_code == 404  # no separate page (N1 A)


def test_unreadable_answer_still_plain_line(env, url, caplog):
    rid = url.split("/")[2]
    with env.pool.connection() as conn:
        conn.execute("UPDATE answers SET body = '{\"answer\": 3}'::jsonb WHERE run_id = %s",
                     (rid,))
    with caplog.at_level("WARNING"):
        t = page(env, url)
    assert "<p>The answer could not be shown.</p>" in t
    assert "run page answer kind=invalid" in caplog.text
    assert "<main>" in t and "Why this conclusion" not in t and "<h2>Steps</h2>" in t


def test_a_whole_run_shows_its_answer_on_the_run_page(env):
    r = env.ask(QUESTION)
    loc = r.headers["location"]
    t = page(env, loc)
    assert '<main class="wide">' in t and "Read the answer" not in t
    assert "<h2>Bottom line</h2>" in t and "w chronic pain" in t  # writer_fakes.whole_answer
    assert "Selected studies, with their abstracts (2)" in t
    assert env.c.get(f"{loc}/answer", headers=H).status_code == 404


def test_unwritten_answer_shows_reason_and_references(env):
    from pubmedgemini.runs import WRITER_TRUNCATED

    b = hernia()
    b.answer, b.empty_reason = None, WRITER_TRUNCATED
    t = page(env, store(env, b, status="partial"))
    assert WRITER_TRUNCATED.replace("'", "&#39;") in t
    assert '<li id="ref-8">[8] Title 8.' in t and "<h2>References</h2>" in t
    assert 'class="two"' not in t and "Why this conclusion" not in t
    assert "Counselling dashboard" not in t and "<h2>The trials</h2>" not in t
    assert "Selected studies, with their abstracts (2)" in t and "Steps (3)" in t
    assert t.index(WRITER_TRUNCATED.replace("'", "&#39;")) < t.index("<h2>References</h2>") \
        < t.index("Selected studies")
    assert "studies selected by gemini" in t and "answer written by" not in t


def test_no_numbers_note(env):
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
    head = t.index("Why this conclusion")
    assert t[head:].index('<section class="card">') < t[head:].index('class="two"')


def test_run_again_unchanged(env):
    b = hernia()
    rid = store(env, b, status="partial").split("/")[2]
    t = page(env, f"/runs/{rid}")
    assert f'<form method="post" action="/runs/{rid}/again">' in t
    assert '<button type="submit">Run again</button>' in t
    assert t.index("Steps (3)") < t.index("Run again") < t.index("<h2>At a glance</h2>")
    done = page(env, store(env, hernia()))
    assert "Run again" not in done and "/again" not in done


def test_phone_width_rules_are_present(env, url):
    t = page(env, url)
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in t
    css = CSS.read_text(encoding="utf-8")
    assert css.index(".two{display:grid;gap:14px;grid-template-columns:minmax(0,1fr)}") < \
        css.index("@media (min-width:700px){.two{")
    assert not re.search(r'width="\d', t)


def test_mock_v6_rules_are_in_the_stylesheet_word_for_word():
    mock = MOCK.read_text(encoding="utf-8")
    block = mock.split("/* ---- new for the answer page (F3b)", 1)[1].split("*/", 1)[1]
    block = block.split("</style>", 1)[0].strip()
    assert block in CSS.read_text(encoding="utf-8")


def test_long_unbroken_model_text_may_wrap_anywhere():
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.num\{[^}]*overflow-wrap:anywhere", css)
    assert re.search(r"\.card, main > article \{[^}]*overflow-wrap: anywhere", css)
    assert re.search(r"main\.wide > h1 \{[^}]*overflow-wrap: anywhere", css)


def test_old_run_without_answer_is_as_before(env):
    rid = insert_run(env, status="done", error_kind=None)
    t = page(env, f"/runs/{rid}")
    assert "<main>" in t and "No relevant evidence found." in t and "<h2>Steps</h2>" in t
    assert "<h2>Question</h2>" in t and "details" not in t


def test_the_page_never_changes_the_stored_answer(env, url):
    rid = url.split("/")[2]
    before = env.one("SELECT body FROM answers WHERE run_id = %s", (rid,))
    page(env, url)
    assert env.one("SELECT body FROM answers WHERE run_id = %s", (rid,)) == copy.deepcopy(before)


# ---------- numbers drawn nowhere else, and pairs that are text only ----------

def _with_numbers(*numbers):
    body = copy.deepcopy(hernia())
    body.answer.outcomes.append(outcome("Odd numbers", list(numbers)))
    return body


def test_numbers_not_drawn_are_listed_inside_their_outcome(env):  # review M3
    t = page(env, store(env, _with_numbers(num("Seroma", 0.4, "RR", pmid=PMIDS[0]))))
    last = t[t.rindex('<details class="oc"'):]
    last = last[:last.index("</details>")]
    assert '<span class="meta">1 number</span>' in last
    assert ('<div class="ci"><p class="lab"><b>Seroma</b> <span class="num">RR 0.4</span></p>'
            '<p class="muted">TEP vs Lichtenstein meta-analysis [<a href="#ref-1">1</a>]</p>'
            '<p class="notdrawn">Not drawn: no interval.</p></div>') in last


def test_numbers_no_scale_can_hold_never_500_the_page(env):  # review M5
    t = page(env, store(env, _with_numbers(
        num("Mean difference", 0.0, "days", None, PMIDS[0], -1e308, 1e308),
        num("Mean difference B", 5e-13, "days", None, PMIDS[0], 0.0, 1e-12),
        num("Seroma", 1e-300, "RR", None, PMIDS[0], 1e-300, 1e-299),
        num("Pain", 1e300, "OR", None, PMIDS[0], 1e299, 1e300))))
    assert t.count("Not drawn: the interval is too wide to draw.") == 1
    assert t.count("Not drawn: the interval is too narrow to draw.") == 1


def _rows_of(t, label):
    return re.findall(rf'<div class="cmp"><b>{label}</b>.*?</div>', t)


def test_a_pair_both_rounding_to_0_shows_its_reason_in_compare(env):  # review L7
    t = page(env, store(env, _with_numbers(
        num("Mesh infection", 0.4, "%", "TEP", PMIDS[0]),
        num("Mesh infection", 0.2, "%", "Lichtenstein", PMIDS[0]))))
    [row] = _rows_of(t, "Mesh infection")
    assert '<span class="notdrawn">Not drawn as squares: both arms round to 0 of 100.</span>' in row
    assert "of 100 · TEP</span>" in t  # the hernia's own items are still drawn
    assert "Mesh infection:</b>" not in t


def test_a_pair_whose_arms_match_no_side_shows_its_reason_in_compare(env):  # fourth check F1
    t = page(env, store(env, _with_numbers(num("Mesh infection", 4.0, "%", "TEP", PMIDS[0]),
                                           num("Mesh infection", 2.0, "%", "L", PMIDS[0]))))
    [row] = _rows_of(t, "Mesh infection")
    assert "TEP 4% vs L 2%" in row
    assert ('<span class="notdrawn">Not drawn as squares: its arms do not match the study&#39;s '
            'comparison.</span>') in row
    assert "Mesh infection:</b>" not in t


def test_a_pair_failing_a_check_shows_its_reason_in_compare(env):  # review L8
    t = page(env, store(env, _with_numbers(num("Seroma", 140.0, "%", "TEP", PMIDS[0]),
                                           num("Seroma", 20.0, "%", "L", PMIDS[0]))))
    [row] = _rows_of(t, "Seroma")
    assert '<span class="num">TEP 140% vs L 20%</span>' in row
    assert ('<span class="notdrawn">Not drawn as squares: a percentage must be from 0 to 100.'
            '</span>') in row


def test_compare_rows_name_each_arm_with_its_value(env):  # review H1 (d23)
    t = page(env, store(env, _with_numbers(num("Groin pain", 18.8, "%", "Open repair", PMIDS[0]),
                                           num("Groin pain", 9.4, "%", "Laparoscopic repair",
                                               PMIDS[0]))))
    [row] = _rows_of(t, "Groin pain")
    assert '<span class="num">Open repair 18.8% vs Laparoscopic repair 9.4%</span>' in row
    assert "18.8% vs 9.4%" not in t.replace("Open repair 18.8% vs Laparoscopic repair 9.4%", "")
    for r in re.findall(r'<div class="cmp">.*?</div>', t):  # no bare "x% vs y%" anywhere
        assert not re.search(r'<span class="num">[\d.]+%? vs', r), r


def test_long_numbers_sit_in_num_spans(env):  # review M6 (d25)
    t = page(env, store(env, _with_numbers(
        num("Pain score difference", -1.2345, "points on the VAS (0 to 10)", None, PMIDS[0],
            -1.9876, -0.5432),
        num("Tiny ratio", 1e-300, "RR", None, PMIDS[0], 1e-300, 1e-299),
        num("Tiny listed", 1e-300, "RR", None, PMIDS[0]))))
    assert ('<span class="num">−1.2345 points on the VAS (0 to 10) (−1.9876 to −0.5432)'
            '</span>') in t
    assert re.search(r'<b>Tiny listed</b> <span class="num">RR 0\.0{299}1</span>', t)


@pytest.mark.parametrize("v,lo,hi", [(0.0, -5.0, 1e308), (10.0, 8.0, 1e308), (0.0, -1e308, 0.8),
                                     (-10.0, -1e308, -5.0), (5e306, 1e306, 1e307)])
def test_a_difference_whose_axis_overflows_never_500s_the_page(env, v, lo, hi):  # re-check M-B
    t = page(env, store(env, _with_numbers(
        num("Mean difference in stay", v, "days", None, PMIDS[0], lo, hi))))
    i = t.index("<b>Mean difference in stay</b>")
    assert "Not drawn: the interval is too wide to draw." in t[i:i + 2000]


def test_a_right_to_left_override_never_reaches_the_page(env):  # third check L3
    body = copy.deepcopy(hernia())
    for o in body.answer.outcomes:
        for n in o.numbers:
            if n.arm == "TEP":
                n.arm = chr(0x202E) + "TEP"
    body.answer.outcomes[0].effect_text += " " + chr(0x2067) + "x" + chr(0x2069) + " " + \
        chr(0x200B)
    t = page(env, store(env, body))
    assert not any(ch in t for ch in INVISIBLE)
    assert '<span class="num">TEP 9.4% vs Lichtenstein 18.8%</span>' in t
    assert "9.4% with TEP vs 18.8% with Lichtenstein" in t


def test_pct_filter_takes_numbers_only():
    from pubmedgemini.web import _pct

    assert (_pct(34.4), _pct(20), _pct(0.0), _pct(100.0)) == ("34.4", "20", "0", "100")
    for bad in ("34.4", None, True, [1]):
        with pytest.raises(TypeError):
            _pct(bad)


def test_an_unknown_run_id_stays_404(env):
    r = env.c.get(f"/runs/{uuid.uuid4()}", headers=H)
    assert r.status_code == 404 and RUN_NOT_FOUND in r.text
