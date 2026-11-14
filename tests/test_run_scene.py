# ruff: noqa: F811  (pytest fixtures imported from test_run_answer_layout)
"""The researcher at work (owner, 13 Nov; Mock Board "Run animation: researcher at work", v3
frozen): while a question runs, a small drawing takes the place of the step list (books pile up
while searching; he writes once the writer starts), the list folded underneath; when the answer
has just arrived, the report rises out. The drawings are files (static/researcher-*.svg) with
their own animation, never inline, and carry no script or link."""

from pathlib import Path

from test_run_answer_layout import (  # noqa: F401 (env is a fixture)
    env,
    hernia,
    page,
    run_page,
    running,
    store,
    with_early,
)

STATIC = Path(__file__).resolve().parent.parent / "src" / "pubmedgemini" / "static"
SEARCH = ('<img class="sai-scene" src="/static/researcher-search.svg" '
          'alt="The researcher, with books piling up beside him">')
WRITE = ('<img class="sai-scene" src="/static/researcher-write.svg" '
         'alt="The researcher writing the answer">')
REPORT = ('<img class="sai-scene" src="/static/researcher-report.svg" '
          'alt="The finished report">')


def test_searching_shows_the_books_and_the_last_step_with_the_list_folded(env):
    rid = running(env, steps=("Researcher: searching PubMed and Europe PMC",
                              "Researcher: 4 searches so far (turn 1 of 8)"))
    t = run_page(env, rid)
    assert SEARCH in t and WRITE not in t
    card = t.split('<section class="card sai-scene-card">', 1)[1].split("</section>", 1)[0]
    assert '<p class="sai-label">Researching</p>' in card
    assert '<p class="sai-scene-line">Researcher: 4 searches so far (turn 1 of 8)</p>' in card
    assert "Steps" not in card and "<details" not in card  # owner, 13 Nov: no step list here
    assert "<h2>Steps</h2>" not in t


def test_writing_shows_him_writing(env):
    rid = running(env, steps=("Researcher: searching PubMed and Europe PMC",
                              "Writer: writing the answer from 8 studies (8 with an abstract)"))
    t = run_page(env, rid)
    assert WRITE in t and SEARCH not in t
    assert '<p class="sai-label">Writing</p>' in t


def test_the_early_summary_page_shows_him_writing(env):
    rid = running(env)
    with_early(env, rid)
    t = run_page(env, rid)
    assert WRITE in t and t.index(WRITE) < t.index('<span class="tag">Early</span>')


def test_waiting_to_start(env):
    rid = running(env, steps=())
    t = run_page(env, rid)
    assert SEARCH in t and '<p class="sai-scene-line">Waiting to start.</p>' in t


def test_a_finished_page_never_shows_the_working_drawings(env):
    t = page(env, store(env, hernia()))
    assert SEARCH not in t and WRITE not in t


def test_the_drawings_are_plain_files():
    for name in ("researcher-search", "researcher-write", "researcher-report"):
        text = (STATIC / f"{name}.svg").read_text(encoding="utf-8")
        assert text.startswith('<svg xmlns="http://www.w3.org/2000/svg"'), name
        low = text.lower()
        for bad in ("<script", "href", "onload", "onclick", "@import", "url(http", "<foreignobject"):
            assert bad not in low, (name, bad)
        assert "prefers-reduced-motion" in text, name  # holds still when asked


def test_the_report_stays_on_every_finished_answer_marked_done(env):
    # the owner, 14 Nov: after the writing the report band stays on, with "Done" next to it
    from test_run_answer_layout import with_check

    for status in ("pending", "done", None):
        url = store(env, hernia())
        if status:
            with_check(env, url, status)
        t = page(env, url)
        assert t.count(REPORT) == 1, status
        card = t.split('<section class="card sai-scene-card">', 1)[1].split("</section>", 1)[0]
        assert '<p class="sai-label">Done</p>' in card and "Report" not in card, status
        assert '<p class="sai-scene-line">The answer is ready. It is below.</p>' in card, status
        assert t.index('<p class="sub">Done') < t.index(REPORT) < t.index("At a glance"), status


def test_the_drawing_sits_in_a_band_beside_the_words():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ".card.sai-scene-card { display: grid; grid-template-columns: 128px minmax(0, 1fr);" in css
    assert ".sai-scene { grid-row: 1 / span 2; display: block; width: 128px; height: auto; }" in css
