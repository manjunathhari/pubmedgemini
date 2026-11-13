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
from page_scripts import HEAD_LINKS, without_theme_scripts
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
    assert "<script" not in without_theme_scripts(t).lower() and not re.search(r"\son[a-z]+=", t)
    assert t.count("<link") == plain.count("<link") == len(HEAD_LINKS)


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
        main = '<main class="home">' if other == "/" else "<main>"  # the Ask page has its own layout
        assert main in t and "wide" not in t, other


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
    assert "&lt;script&gt;x&lt;/script&gt;&lt;/details&gt;" in t
    assert "<script" not in without_theme_scripts(t)
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
    # the mock was drawn in greys; the stylesheet writes each grey as its look's property
    greys = {"#F4F4F4": "var(--page)", "#FFFFFF": "var(--card)", "#DDDDDD": "var(--line)",
             "#BDBDBD": "var(--line2)", "#8C8C8C": "var(--faint)", "#666666": "var(--muted)",
             "#4D4D4D": "var(--strong)", "#333333": "var(--ink)"}
    block = re.sub(r"#[0-9A-Fa-f]{6}\b", lambda m: greys[m.group(0).upper()], block)
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


# ---------- F3c Task 4: the Introduction panel and the early summary (mock f3c v1) ----------

from pubmedgemini import intro as intro_words

INTRO = "Example paragraph one.\n\nExample paragraph two\nwith a line break."
BG = ("Background only, written by the model from your question alone. It is not drawn from "
      "the studies and carries no citations.")  # O1 (a), typed from the plan
EARLY_NOTE = ("The summary was written first. Its citations have been checked against the "
              "selected studies. The outcomes, the trials and the check that the parts agree "
              "are still being written, so this summary may still change or be withdrawn. The "
              "full answer replaces it on this page.")  # mock v1, typed
KEYS_HELD = "The key numbers are shown with the full answer."  # O3 (b), mock v1
STILL = ("Still being written: why this conclusion (every estimate with a 95% CI, outcome by "
         "outcome), the counselling dashboard, the trials, conflicting evidence, applicability, "
         "what would change this answer, and the references.")  # mock v1, typed
STILL_WITH_CONTEXT = ("Still being written: why this conclusion (every estimate with a 95% CI, "
                      "outcome by outcome), the counselling dashboard, the trials, conflicting "
                      "evidence, and the references.")


def running(env, steps=("Researcher: searching PubMed and Europe PMC",)):
    rid = insert_run(env, status="running", error_kind=None)
    with env.pool.connection() as conn:
        conn.execute("UPDATE runs SET finished_at = NULL WHERE id = %s", (rid,))
        for i, line in enumerate(steps, start=1):
            conn.execute("INSERT INTO run_steps (run_id, seq, kind, line) "
                         "VALUES (%s, %s, 'note', %s)", (rid, i, line))
    return rid


def with_intro(env, rid, text=INTRO, status="done"):
    with env.pool.connection() as conn:
        conn.execute("INSERT INTO run_intros (run_id, status, reason, text) "
                     "VALUES (%s, %s, %s, %s)",
                     (rid, status, "api" if status == "failed" else None,
                      text if status == "done" else None))


def with_early(env, rid, summary=True, context=False, glance=True):
    """The early row EarlyStore stores for the made-up parts (a real citation check)."""
    from writer_fakes import whole_answer

    from pubmedgemini.runner import EarlyStore, _Answer
    from pubmedgemini.writer.answer import IntegrityV3, ReferenceV3
    from pubmedgemini.writer.prompts import Section
    from pubmedgemini.writer.read import as_context, as_summary

    w = whole_answer("ex", (PMIDS[0], PMIDS[1]))
    if not glance:
        w["at_a_glance"] = None
    a = _Answer("q", [], None, IntegrityV3())
    a.references = [ReferenceV3(n=i, pmid=p, title=f"Title {i}", journal="J", year=2020,
                                doi=None, retracted=False, found_by_skeptic=False)
                    for i, p in enumerate(PMIDS[:2], start=1)]
    es = EarlyStore(env.pool, uuid.UUID(rid) if isinstance(rid, str) else rid, a, PMIDS[:2])
    if context:
        es(("context", as_context({f: w[f] for f in Section.CONTEXT.fields})))
    if summary:
        es(("summary", as_summary({f: w[f] for f in Section.SUMMARY.fields})))
    with env.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM run_early").fetchone()[0] == 1


def run_page(env, rid):
    return page(env, f"/runs/{rid}")


def test_working_page_shows_the_introduction_open_with_its_labels(env):
    rid = running(env)
    with_intro(env, rid)
    t = run_page(env, rid)
    sec = t.split("<h2>Introduction</h2>", 1)[1].split("</section>", 1)[0]
    assert f'<p class="sub">{intro_words.TAG_RUNNING}</p>' in sec
    assert f'<p class="muted">{BG}</p>' in sec and intro_words.BACKGROUND == BG
    assert "<p>Example paragraph one.</p>" in sec
    assert "<p>Example paragraph two\nwith a line break.</p>" in sec
    assert f"<p><b>{intro_words.CLOSING}</b></p>" in sec
    # order: Question, Introduction, Steps; the 5 s refresh while working
    # the step list is now folded under the researcher drawing (owner, 13 Nov)
    assert t.index("<h2>Question</h2>") < t.index("<h2>Introduction</h2>") < t.index(
        'class="card sai-scene-card"')
    assert '<meta http-equiv="refresh" content="5">' in t
    assert "<details" not in t and "Early" not in t


@pytest.mark.parametrize("case", ["no_row", "failed", "pending"])
def test_no_introduction_row_no_panel(env, case):
    rid = running(env)
    if case == "failed":
        with_intro(env, rid, status="failed")
    elif case == "pending":
        with env.pool.connection() as conn:
            conn.execute("INSERT INTO run_intros (run_id) VALUES (%s)", (rid,))
    t = run_page(env, rid)
    assert "Introduction" not in t and BG not in t
    url = store(env, hernia())
    rid2 = url.rsplit("/", 1)[1]
    if case == "failed":
        with_intro(env, rid2, status="failed")
    assert "Introduction" not in page(env, url)


def test_working_page_without_an_introduction_is_as_before(env):
    rid = running(env)
    t = run_page(env, rid)
    # the researcher drawing with the latest step takes the step list's place (owner, 13 Nov)
    assert '<p class="sai-scene-line">Researcher: searching PubMed and Europe PMC</p>' in t


def test_introduction_text_is_escaped_and_never_linked(env):
    rid = running(env)
    with_intro(env, rid, text="<script>alert(1)</script> see [90000001] and "
                              "[PMID 90000002]\u202e\u200b.")
    t = run_page(env, rid)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in t
    assert "<script" not in without_theme_scripts(t)
    sec = t.split("<h2>Introduction</h2>", 1)[1].split("</section>", 1)[0]
    assert "[90000001]" in sec and "<a" not in sec
    assert "\u202e" not in t and "\u200b" not in t


def test_early_summary_marked_and_without_key_numbers(env):
    rid = running(env, steps=["Researcher: searching PubMed and Europe PMC",
                              "Writer: writing the answer from 2 studies (2 with an abstract)"])
    with_intro(env, rid)
    with_early(env, rid)
    t = run_page(env, rid)
    assert '<meta http-equiv="refresh" content="5">' in t
    assert '<h2>Early summary <span class="tag">Early</span></h2>' in t
    assert f"<p>{EARLY_NOTE}</p>" in t
    assert t.count('<span class="tag">Early</span>') == 3
    assert '<h2>At a glance <span class="tag">Early</span></h2>' in t
    assert '<h2>Bottom line <span class="tag">Early</span></h2>' in t
    glance = t.split("<h2>At a glance", 1)[1].split("</section>", 1)[0]
    assert "<p>ex no clear benefit.</p>" in glance  # the verdict
    assert "<p><b>Main caveat:</b> small</p>" in glance
    assert f'<p class="muted">{KEYS_HELD}</p>' in glance
    assert "Chronic pain" not in glance and "RR 0.76" not in t  # the key number held back
    assert "<svg" not in t and 'class="dots"' not in t and 'class="cmp"' not in t
    bottom = t.split("<h2>Bottom line", 1)[1].split("</section>", 1)[0]
    assert "ex no clear benefit [1, 2]." in bottom  # no references card yet: no links
    assert '<span class="pill mo">Moderate certainty</span>' in bottom
    assert "<p><b>Default choice:</b> ex lap</p>" in bottom
    assert f'<p class="muted">{STILL}</p>' in t
    assert "<h2>Applicability" not in t and "<h2>What would change" not in t
    # Introduction and Steps fold, in the mock's order
    assert ('<section class="card">\n<details class="fold"><summary>Introduction</summary>\n'
            f'<p class="sub">{intro_words.TAG_FINISHED}</p>\n<p class="muted">{BG}</p>') in t
    assert intro_words.CLOSING not in t
    # the researcher drawing (writing) takes the folded step list's place (owner, 13 Nov)
    assert "<summary>Steps (" not in t and 'src="/static/researcher-write.svg"' in t
    order = ["<h2>Question</h2>", "<summary>Introduction</summary>", 'class="card sai-scene-card"',
             "<h2>Early summary", "<h2>At a glance", "<h2>Bottom line", "Still being written"]
    assert [t.index(x) for x in order] == sorted(t.index(x) for x in order)
    assert "<h2>Steps</h2>" not in t and "<h2>References</h2>" not in t


def test_early_cards_cite_as_plain_text_and_the_finished_page_still_links(env):
    """While the run works there is no references card, so an early citation is the plain
    text [n] (no link to a #ref- anchor that is not there); the finished page keeps its links."""
    rid = running(env)
    with_early(env, rid, summary=True, context=True)
    t = run_page(env, rid)
    assert "[1, 2]" in t
    assert 'href="#ref-' not in t
    _end(env, rid)
    done = page(env, store(env, hernia()))
    assert 'href="#ref-' in done and 'id="ref-' in done


def test_early_context_cards_only_when_context_arrived(env):
    rid = running(env)
    with_early(env, rid, summary=True, context=True)
    t = run_page(env, rid)
    assert t.count('<span class="tag">Early</span>') == 5
    assert '<h2>Applicability <span class="tag">Early</span></h2>' in t
    assert "<p><b>Case mix:</b> ex simple</p>" in t
    assert '<h2>What would change this answer <span class="tag">Early</span></h2>' in t
    assert "<li>ex a larger RCT</li>" in t
    assert f'<p class="muted">{STILL_WITH_CONTEXT}</p>' in t
    order = ["<h2>Bottom line", "<h2>Applicability", "<h2>What would change", "Still being"]
    assert [t.index(x) for x in order] == sorted(t.index(x) for x in order)


def test_context_alone_shows_no_early_summary(env):
    """Until the summary part is in, the page stays as it works (the early card's words say
    the summary was written first)."""
    rid = running(env)
    with_intro(env, rid)
    with_early(env, rid, summary=False, context=True)
    t = run_page(env, rid)
    assert "Early" not in t and "<h2>Applicability" not in t
    assert "<h2>Introduction</h2>" in t and "sai-scene-card" in t


def test_early_without_at_a_glance_has_no_glance_card(env):
    rid = running(env)
    with_early(env, rid, glance=False)
    t = run_page(env, rid)
    assert "<h2>At a glance" not in t and t.count('<span class="tag">Early</span>') == 2


def test_an_unreadable_early_body_is_ignored(env, caplog):
    rid = running(env)
    with env.pool.connection() as conn:
        conn.execute("INSERT INTO run_early (run_id, body) VALUES (%s, %s)",
                     (rid, Jsonb({"summary": True, "context": False,
                                  "answer": {"version": 3, "SECRET-EARLY-TEXT": "x"}})))
    with caplog.at_level("WARNING"):
        t = run_page(env, rid)
    assert "Early" not in t and "sai-scene-card" in t
    assert "SECRET-EARLY-TEXT" not in t and "SECRET-EARLY-TEXT" not in caplog.text
    assert "run page early kind=invalid" in caplog.text


def test_early_text_is_escaped(env):
    rid = running(env)
    with_early(env, rid)
    with env.pool.connection() as conn:
        body = conn.execute("SELECT body FROM run_early").fetchone()[0]
        body["answer"]["answer"]["bottom_line"]["text"] = "<script>x</script>\u202e"
        conn.execute("UPDATE run_early SET body = %s", (Jsonb(body),))
    t = run_page(env, rid)
    assert "&lt;script&gt;x&lt;/script&gt;" in t and "\u202e" not in t
    assert "<script" not in without_theme_scripts(t)


def test_finished_page_never_shows_the_early_summary(env):
    url = store(env, hernia())
    rid = url.rsplit("/", 1)[1]
    with env.pool.connection() as conn:  # a leftover row, inserted by hand
        conn.execute("INSERT INTO run_early (run_id, body) VALUES (%s, %s)",
                     (rid, Jsonb({"summary": True, "context": True, "answer": {}})))
    t = page(env, url)
    assert "Early" not in t and "Still being written" not in t
    assert '<meta http-equiv="refresh"' not in t


def test_finished_page_has_the_introduction_fold_first(env):
    url = store(env, hernia())
    with_intro(env, url.rsplit("/", 1)[1])
    t = page(env, url)
    assert t.index('<p class="sub">Done · Quick') < t.index("<summary>Introduction</summary>") \
        < t.index("<summary>Steps (")
    # the owner, 13 Nov: when the run is done the Introduction is open
    assert '<details class="fold" open><summary>Introduction</summary>' in t
    sec = t.split("<summary>Introduction</summary>", 1)[1].split("</details>", 1)[0]
    assert sec == (f'\n<p class="sub">{intro_words.TAG_FINISHED}</p>\n<p class="muted">{BG}</p>'
                   "\n<p>Example paragraph one.</p>\n<p>Example paragraph two\nwith a line "
                   "break.</p>\n")
    assert intro_words.CLOSING not in t and "Early" not in t


@pytest.mark.parametrize("status", ["done", "partial"])
def test_the_finished_page_is_unchanged_but_for_the_introduction(env, status):
    """Byte for byte: the page with an Introduction, less its one card, is the page without
    one (so everything below the sub line, Bottom line included, is as before)."""
    with_one = store(env, hernia(), status=status)
    without = store(env, hernia(), status=status)
    with_intro(env, with_one.rsplit("/", 1)[1])
    a = page(env, with_one).replace(with_one.rsplit("/", 1)[1], "RID")
    b = page(env, without).replace(without.rsplit("/", 1)[1], "RID")
    card = a.split("<section class=\"card\">\n<details class=\"fold\" open><summary>Introduction", 1)
    assert len(card) == 2
    rest = card[1].split("</section>\n", 1)[1]
    assert card[0] + rest == b


def _end(env, rid):  # one active run for the whole app: end it before the next
    with env.pool.connection() as conn:
        conn.execute("UPDATE runs SET status = 'done' WHERE id = %s", (rid,))


def test_new_states_have_no_script_and_no_new_css(env):
    plain_rid = running(env)
    plain = run_page(env, plain_rid)
    _end(env, plain_rid)
    rid = running(env)
    with_intro(env, rid)
    working = run_page(env, rid)
    _end(env, rid)
    rid2 = running(env)
    with_intro(env, rid2)
    with_early(env, rid2, context=True)
    early = run_page(env, rid2)
    url = store(env, hernia())
    with_intro(env, url.rsplit("/", 1)[1])
    done = page(env, url)
    css = CSS.read_text()
    states = []
    for status in ("pending", "running", "done", "failed"):  # F4a: the completeness check box
        check_url = store(env, hernia())
        with_check(env, check_url, status)
        states.append(page(env, check_url))
        assert 'class="steps"' in states[-1]
    for t in (working, early, done, *states):
        low = t.lower()
        assert "<script" not in without_theme_scripts(t).lower()
        assert "style=" not in low and "<style" not in low
        assert t.count("<link") == plain.count("<link")
        for cls in set(re.findall(r'class="([^"]+)"', t)):
            for c in cls.split():
                assert re.search(rf"\.{re.escape(c)}\b", css) or c in ("wide",), c


# ---------- F4a Task 4: the completeness check box (mock docs/mocks/f4a-completeness-v3.html) ----

F4A_MOCK = ROOT / "docs" / "mocks" / "f4a-completeness-v3.html"
BOX = '<section class="card"><div class="steps">'
NOT_COMPLETED = "The check could not be completed. The answer is unchanged."
AR = ' <span class="ar">›</span> '
REFRESH = '<meta http-equiv="refresh" content="5">'


def with_check(env, url, status, counts=(16, 16, 4, 4), age=0):
    """A check row inserted by hand for the run of `url`, created `age` seconds ago."""
    rid = url.rsplit("/", 1)[1]
    found = counts if status == "done" else (None, None, None, None)
    with env.pool.connection() as conn:
        conn.execute(
            "INSERT INTO run_checks (run_id, status, reason, numbers_found, numbers_total, "
            "names_match, names_total, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, "
            "now() - make_interval(secs => %s))",
            (rid, status, "internal" if status == "failed" else None, *found, age))
    return rid


def box_of(t):
    """The box's own <section>, start to end; fails when the page has none or two."""
    assert t.count(BOX) == 1 and t.count('class="steps"') == 1
    return BOX + t.split(BOX, 1)[1].split("</section>", 1)[0] + "</section>"


def steps_box(pending, running, done, line=None):
    sub = f' <span class="sub" data-x>{line}</span>' if line else ""
    return (f'{BOX}<span class="name">Completeness check</span>'
            f'<span class="{pending}">Pending</span>{AR}<span class="{running}">Running</span>'
            f'{AR}<span class="{done}">Done</span>{sub}</div></section>')


def mock_box_line():
    (line,) = [ln for ln in F4A_MOCK.read_text(encoding="utf-8").splitlines()
               if ln.startswith(BOX)]
    return line


def test_box_markup_is_the_mocks(env):
    done = store(env, hernia())
    with_check(env, done, "done")
    assert box_of(page(env, done)) == mock_box_line()  # the mock's line, character for character
    other = store(env, hernia())
    with_check(env, other, "done", counts=(3, 17, 0, 5))
    assert box_of(page(env, other)) == mock_box_line().replace(
        "16 of 16 numbers found · 4 of 4 trial names match",
        "3 of 17 numbers found · 0 of 5 trial names match")
    pending = store(env, hernia())
    with_check(env, pending, "pending")
    assert box_of(page(env, pending)) == steps_box("st now", "st", "st")
    running_url = store(env, hernia())
    with_check(env, running_url, "running")
    assert box_of(page(env, running_url)) == steps_box("st was", "st now", "st")
    for t in (page(env, pending), page(env, running_url)):
        assert 'class="sub" data-x' not in t  # no second line while Pending or Running


def test_box_is_last_before_the_closing_lines(env, url):
    with_check(env, url, "done")
    t = page(env, url)
    at = t.index(BOX)
    for before in ("<h2>References</h2>", "Selected studies, with their abstracts (",
                   "Considered, not selected (", "Possible contradictions ("):
        assert t.index(before) < at, before
    assert at < t.index("Searched PubMed and Europe PMC")
    assert t.count("<section", at) == 1  # the last box on the page
    bare = store(env, hernia(), extras=False)  # no folds at the bottom: still last
    with_check(env, bare, "done")
    t2 = page(env, bare)
    assert t2.index("<h2>References</h2>") < t2.index(BOX) < t2.index("Searched PubMed")
    assert t2.count("<section", t2.index(BOX)) == 1


def test_the_page_is_unchanged_but_for_the_box(env):
    """Byte for byte: the page with a done check, less the box's one line, is the page of an
    answer with no check row."""
    with_one, without = store(env, hernia()), store(env, hernia())
    with_check(env, with_one, "done")
    a = page(env, with_one).replace(with_one.rsplit("/", 1)[1], "RID")
    b = page(env, without).replace(without.rsplit("/", 1)[1], "RID")
    assert a.replace(mock_box_line() + "\n", "") == b and a != b


@pytest.mark.parametrize("status,refreshes", [("pending", True), ("running", True),
                                              ("done", False), ("failed", False)])
def test_refresh_stops_at_done(env, url, status, refreshes):
    with_check(env, url, status, age=50)  # young: under the cap
    t = page(env, url)
    assert env.one("SELECT status FROM runs WHERE id = %s", (url.rsplit("/", 1)[1],)) == "done"
    assert (REFRESH in t) is refreshes and t.count("http-equiv") == (1 if refreshes else 0)


@pytest.mark.parametrize("status", ["pending", "running"])
def test_refresh_stops_after_the_cap(env, url, status):
    rid = with_check(env, url, status, age=61)
    before = env.rows("SELECT status, reason, started_at, finished_at, created_at "
                      "FROM run_checks WHERE run_id = %s", (rid,))
    t = page(env, url)
    assert "http-equiv" not in t
    assert env.rows("SELECT status, reason, started_at, finished_at, created_at "
                    "FROM run_checks WHERE run_id = %s", (rid,)) == before  # a view changes nothing


@pytest.mark.parametrize("status", ["pending", "running"])
def test_a_stuck_check_is_not_shown_as_running(env, url, status):
    with_check(env, url, status, age=61)
    t = page(env, url)
    assert "st now" not in t
    assert box_of(t) == steps_box("st was", "st was", "st", NOT_COMPLETED)


@pytest.mark.parametrize("age", [-1, -30, -3600])
@pytest.mark.parametrize("status,classes", [("pending", ("st now", "st", "st")),
                                            ("running", ("st was", "st now", "st"))])
def test_a_check_dated_in_the_future_does_not_keep_the_page_refreshing(env, url, status,
                                                                       classes, age):
    """Review B8: the database's clock stepped back, so the row's age is negative. The box
    shows the stored step and no line (it claims neither Done nor a failure), and the page
    does not refresh; a view changes nothing."""
    rid = with_check(env, url, status, age=age)
    before = env.rows("SELECT status, reason, started_at, finished_at, created_at "
                      "FROM run_checks WHERE run_id = %s", (rid,))
    t = page(env, url)
    assert "http-equiv" not in t
    assert box_of(t) == steps_box(*classes)
    assert NOT_COMPLETED not in t and "numbers found" not in t
    assert env.rows("SELECT status, reason, started_at, finished_at, created_at "
                    "FROM run_checks WHERE run_id = %s", (rid,)) == before


def test_a_finished_check_dated_in_the_future_reads_as_any_other(env):
    done, failed = store(env, hernia()), store(env, hernia())
    with_check(env, done, "done", age=-30)
    with_check(env, failed, "failed", age=-30)
    t = page(env, done)
    assert box_of(t) == steps_box(
        "st was", "st was", "st now", "16 of 16 numbers found · 4 of 4 trial names match")
    assert "http-equiv" not in t
    t = page(env, failed)
    assert box_of(t) == steps_box("st was", "st was", "st", NOT_COMPLETED)
    assert "http-equiv" not in t


@pytest.mark.parametrize("age,wait", [(0, True), (0.0, True), (59.9, True), (60, False),
                                      (-0.001, False), (-1e9, False), (float("nan"), False),
                                      (float("-inf"), False), (float("inf"), False)])
def test_the_box_waits_only_for_an_age_from_zero_to_the_cap(age, wait):
    from types import SimpleNamespace

    from pubmedgemini.runs import CheckRow
    from pubmedgemini.web import CHECK_WAIT_SECONDS, _check_box

    assert CHECK_WAIT_SECONDS == 60
    for status in ("pending", "running"):
        box = _check_box(SimpleNamespace(check=CheckRow(status, None, None, None, None, age)))
        assert box["wait"] is wait
        if age >= CHECK_WAIT_SECONDS:  # past the cap: not completed, as before
            assert box["line"] == NOT_COMPLETED
        else:  # under the cap, or no usable age: the stored step, no line
            assert box["line"] is None
            assert [c for _, c in box["steps"]].index("st now") == ("pending", "running").index(
                status)


def test_failed_check_shows_the_plain_line(env, url):
    with_check(env, url, "failed")
    t = page(env, url)
    assert box_of(t) == steps_box("st was", "st was", "st", NOT_COMPLETED)
    assert "st now" not in t and "numbers found" not in t and "trial names match" not in t
    assert "http-equiv" not in t and "internal" not in box_of(t)
    assert "Run again" not in t and "/again" not in t  # the check never offers a re-run


@pytest.mark.parametrize("counts,line", [
    ((0, 0, 4, 4), "No numbers to check · 4 of 4 trial names match"),
    ((2, 3, 0, 0), "2 of 3 numbers found · No trial names to check"),
    ((0, 0, 0, 0), "No numbers to check · No trial names to check"),
    ((0, 5, 0, 2), "0 of 5 numbers found · 0 of 2 trial names match"),
    ((1, 1, 1, 1), "1 of 1 numbers found · 1 of 1 trial names match"),
])
def test_zero_totals_read_plainly(env, url, counts, line):
    with_check(env, url, "done", counts=counts)
    assert box_of(page(env, url)) == steps_box("st was", "st was", "st now", line)


def test_no_check_row_no_box(env, url):
    assert 'class="steps"' not in page(env, url)  # an answered run with no row (every old run)
    assert "Completeness check" not in page(env, url)
    b = hernia()
    b.answer, b.empty_reason = None, ""  # a partial run with an empty reason
    assert 'class="steps"' not in page(env, store(env, b, status="partial"))
    plain = insert_run(env, status="done", error_kind=None)  # a run with no answer at all
    with_check(env, f"/runs/{plain}", "pending")  # even with a row: not the answer page
    t = env.run_page(f"/runs/{plain}").text
    assert 'class="steps"' not in t and "Completeness check" not in t
    assert "http-equiv" not in t


def test_the_working_page_has_no_box(env):
    rid = running(env)
    with_check(env, f"/runs/{rid}", "pending")
    t = run_page(env, rid)
    assert 'class="steps"' not in t and "Completeness check" not in t
    assert t.count(REFRESH) == 1  # the run's own refresh, once


def test_box_holds_only_fixed_words_and_counts(env):
    b = hernia()
    b.answer.studies[0].label = "ZQ7LABEL <b>2019</b>"
    b.answer.outcomes[0].numbers[0].label = "ZQ7NUMBER"
    b.answer.outcomes[0].numbers[0].value = 98765.4321
    url = store(env, b)
    with_check(env, url, "done", counts=(7, 9, 2, 3))
    t = page(env, url)
    assert "ZQ7LABEL" in t and "ZQ7NUMBER" in t  # the markers are on the page, outside the box
    box = box_of(t)
    assert box == steps_box("st was", "st was", "st now",
                            "7 of 9 numbers found · 2 of 3 trial names match")
    for marker in ("ZQ7", "98765", "98,765", "2019", "<b>"):
        assert marker not in box
    assert "<script" not in without_theme_scripts(t).lower() and "style=" not in t.lower()
