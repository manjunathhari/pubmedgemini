"""F9: the Library like Incisive's (frozen mock v3 + PDF, owner, 21 Nov 2026).

Rules are Incisive's (`crates/incisive-app/src/library.rs`); pages run on a dev app over fakes
(as test_ask_page)."""

import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from test_answer_page import hernia
from test_ask_page import HAPPY_RUN, OWNER, H, HoldStarter, ask_env, insert_run
from test_run_answer_layout import store

from pubmedgemini import library as lib

OTHER = "b@example.com"
HB = {"x-dev-user-email": OTHER}
CROSS = {"sec-fetch-site": "cross-site"}


@pytest.fixture
def env(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN, starter=HoldStarter()) as e:
        yield e


def rid_of(url):
    return url.split("/")[2]


def card_of(page, rid):
    m = re.search(r'<article class="lib-card"[^>]*>(?:(?!</article>).)*?/runs/' + rid
                  + r'(?:(?!</article>).)*</article>', page, re.DOTALL)
    assert m, rid
    return m.group(0)


def set_times(env, rid, created, started=None, finished=None):
    with env.pool.connection() as conn:
        conn.execute("UPDATE runs SET created_at = %s, started_at = %s, finished_at = %s "
                     "WHERE id = %s", (created, started, finished, rid))


def count_runs(env):
    with env.pool.connection() as conn:
        return conn.execute("SELECT count(*) FROM runs").fetchone()[0]


# ---------- Incisive's rules ----------

def test_pips_and_four_segment_spine_fill_from_the_bottom():
    assert [lib.pips(n) for n in (1, 2, 3, 4)] == ["●○○○", "●●○○", "●●●○", "●●●●"]
    assert lib.spine(1) == [False, False, False, True]
    assert lib.spine(2) == [False, False, True, True]
    assert lib.spine(3) == [False, True, True, True]
    assert lib.spine(4) == [True, True, True, True]
    assert lib.spine(None) == [False] * 4
    assert [lib.certainty_name(n) for n in (1, 2, 3, 4)] == ["Very low", "Low", "Moderate", "High"]


def test_day_headings_and_summaries():
    today = date(2026, 11, 21)
    assert lib.day_heading(today, today) == "Today · Sat 21 Nov"
    assert lib.day_heading(date(2026, 11, 20), today) == "Yesterday · Fri 20 Nov"
    assert lib.day_heading(date(2026, 11, 9), today) == "Mon 9 Nov"
    assert lib.day_heading(date(2025, 12, 31), today) == "Wed 31 Dec 2025"
    one = lib.Group([lib.Card(uuid.uuid4(), "q", "done", datetime.now(UTC), None, None, None,
                              None)])
    two = lib.Group(one.runs * 2)
    assert lib.day_summary([one]) == "1 question · 1 run"
    assert lib.day_summary([one, two]) == "2 questions · 3 runs"


def test_meta_line_and_durations():
    t = datetime(2026, 11, 21, 2, 10, tzinfo=UTC)  # 07:40 IST
    c = lib.Card(uuid.uuid4(), "q", "done", t, 170, "v", 2, 8)
    assert lib.meta_line(c) == "07:40 · 3 min · 8 references"
    assert lib.meta_line(lib.Card(c.id, "q", "done", t, 41, "v", 2, 1)) == \
        "07:40 · 41 s · 1 reference"
    assert lib.meta_line(lib.Card(c.id, "q", "failed", t, None, None, None, None)) == "07:40"


def test_same_question_is_grouped_newest_first():
    t = datetime(2026, 11, 21, 3, 0, tzinfo=UTC)
    a = lib.Card(uuid.uuid4(), "Hernia  mesh?", "done", t, None, None, None, None)
    b = lib.Card(uuid.uuid4(), "hernia mesh?", "failed", t + timedelta(hours=1), None, None,
                 None, None)
    c = lib.Card(uuid.uuid4(), "other", "done", t + timedelta(minutes=30), None, None, None,
                 None)
    groups = lib.group_runs([a, b, c])
    assert [[r.id for r in g.runs] for g in groups] == [[b.id, a.id], [c.id]]


# ---------- the page ----------

def test_card_shows_verdict_certainty_spine_and_meta(env):
    url = store(env, hernia())
    rid = rid_of(url)
    created = datetime.now(UTC) - timedelta(minutes=5)
    set_times(env, rid, created, created, created + timedelta(seconds=170))
    page = env.c.get("/library", headers=H).text
    card = card_of(page, rid)
    assert '<span class="lib-mode">Quick</span>' in card
    assert "3 min · 8 references" in card
    assert "●●●○</span> Moderate" in card
    # the at-a-glance verdict, citations removed
    assert re.search(r'<span class="lib-verdict">[^<\[]+</span>', card)
    assert '<span class="lib-spine" aria-hidden="true"><span></span><span class="on"></span>' \
           '<span class="on"></span><span class="on"></span></span>' in card
    assert 'data-c="3"' in card


def test_no_answer_card_has_grey_spine_and_no_copy_or_pdf(env):
    rid = str(insert_run(env, status="failed", error_kind="internal"))
    card = card_of(env.c.get("/library", headers=H).text, rid)
    assert '<span class="lib-spine none" aria-hidden="true">' + "<span></span>" * 4 in card
    assert "lib-copy" not in card.replace("lib-copy-area", "") and "/pdf" not in card
    assert "Read answer" in card and "Edit &amp; re-run" in card and "lib-del" in card


def test_actions_on_an_answered_card(env):
    url = store(env, hernia())
    card = card_of(env.c.get("/library", headers=H).text, rid_of(url))
    assert f'<a class="lib-read" href="{url}">Read answer</a>' in card
    assert f'<a href="{url}/pdf">PDF</a>' in card
    assert f'<button type="button" class="lib-copy" data-id="{rid_of(url)}" hidden>Copy</button>' \
        in card
    assert f'action="{url}/delete"' in card and ">Delete?<" in card and ">Keep<" in card
    assert '<a href="/?q=' in card


def test_working_run_cannot_be_deleted_from_the_page(env):
    rid = str(insert_run(env, status="running", error_kind=None))
    card = card_of(env.c.get("/library", headers=H).text, rid)
    assert "lib-del" not in card and "Working on it…" in card


def test_repeated_question_is_one_card_asked_n_times(env):
    a = insert_run(env, question="Same question", status="failed", error_kind="internal")
    b = insert_run(env, question="same  question", status="done", error_kind=None)
    now = datetime.now(UTC)
    set_times(env, a, now - timedelta(minutes=10))
    set_times(env, b, now - timedelta(minutes=2))
    page = env.c.get("/library", headers=H).text
    assert page.count('<article class="lib-card"') == 1
    assert "Asked 2 times" in page and "newest shown" in page
    assert page.index(f'/runs/{b}"') < page.index(f'/runs/{a}"')
    assert "Latest · Done" in page and "Failed</span>" in page


def test_day_groups_today_and_yesterday(env):
    a = insert_run(env, question="today's")
    b = insert_run(env, question="yesterday's")
    now = datetime.now(UTC)
    set_times(env, a, now)
    set_times(env, b, now - timedelta(days=1))
    page = env.c.get("/library", headers=H).text
    heads = re.findall(r'<div class="lib-dayhead"><h2>([^<]+)</h2>'
                       r'<span class="lib-daysum">([^<]+)</span>', page)
    assert [h.split(" · ")[0] for h, _ in heads] == ["Today", "Yesterday"]
    assert [s for _, s in heads] == ["1 question · 1 run", "1 question · 1 run"]


def test_search_filters_and_footer(env):
    insert_run(env)
    page = env.c.get("/library", headers=H).text
    assert 'placeholder="Search questions and verdicts"' in page
    assert [b for b in re.findall(r'class="lib-filter" data-c="(\d?)"[^>]*>([^<]+)<', page)] == [
        ("", "All"), ("4", "High"), ("3", "Moderate"), ("2", "Low"), ("1", "Very low")]
    assert "Times in IST. Decision support &mdash; the clinician decides." in page
    assert "Delete all my history" in page and "Delete all? This cannot be undone." in page
    assert '<a class="lib-new" href="/">New question' in page


def test_model_text_and_question_are_escaped(env):
    body = hernia()
    body.answer.at_a_glance.verdict = "<img src=x onerror=1> better"
    url = store(env, body)
    with env.pool.connection() as conn:
        conn.execute("UPDATE runs SET question = %s WHERE id = %s",
                     ('"><script>alert(1)</script>', rid_of(url)))
    page = env.c.get("/library", headers=H).text
    assert "<img src=x" not in page and "&lt;img src=x onerror=1&gt; better" in page
    assert "<script>alert(1)" not in page


# ---------- delete ----------

def test_delete_my_finished_run(env):
    url = store(env, hernia())
    r = env.c.post(f"{url}/delete", headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/library?deleted=1"
    assert env.c.get(url, headers=H).status_code == 404
    assert "Deleted." in env.c.get("/library?deleted=1", headers=H).text


def test_cannot_delete_another_users_or_a_working_run(env):
    env.add_user(OTHER)
    theirs = insert_run(env, email=OTHER)
    working = insert_run(env, status="running", error_kind=None)
    assert env.c.post(f"/runs/{theirs}/delete", headers=H).status_code == 404
    assert env.c.post(f"/runs/{working}/delete", headers=H).status_code == 404
    assert env.c.post("/runs/not-a-run/delete", headers=H).status_code == 404
    assert count_runs(env) == 2


def test_cross_site_delete_is_refused(env):
    rid = insert_run(env)
    assert env.c.post(f"/runs/{rid}/delete", headers=H | CROSS).status_code == 403
    assert env.c.post("/library/delete-all", headers=H | CROSS).status_code == 403
    assert count_runs(env) == 1


def test_delete_all_keeps_working_runs_and_other_users(env):
    env.add_user(OTHER)
    insert_run(env)
    store(env, hernia())
    working = insert_run(env, status="running", error_kind=None)
    theirs = insert_run(env, email=OTHER)
    r = env.c.post("/library/delete-all", headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/library?deleted=all"
    with env.pool.connection() as conn:
        left = {row[0] for row in conn.execute("SELECT id FROM runs").fetchall()}
    assert left == {working, theirs}


def test_deleting_keeps_usage_counts(env):
    """Free runs left today counts model_calls by user: deleting a run keeps them (SET NULL)."""
    rid = insert_run(env)
    with env.pool.connection() as conn:
        conn.execute("INSERT INTO model_calls (model, step, attempt, status_kind, user_id, run_id) "
                     "VALUES ('m', 'researcher', 1, 'ok', %s, %s)", (env.user_id(OWNER), rid))
    env.c.post(f"/runs/{rid}/delete", headers=H)
    with env.pool.connection() as conn:
        assert conn.execute("SELECT count(*), count(run_id) FROM model_calls "
                            "WHERE user_id = %s", (env.user_id(OWNER),)).fetchone() == (1, 0)


# ---------- copy text and Edit & re-run ----------

def test_text_route_gives_plain_answer(env):
    url = store(env, hernia())
    r = env.c.get(f"{url}/text", headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert r.headers.get("cache-control") == "no-store"
    t = r.text
    assert "Bottom line (Moderate certainty)" in t and "References" in t
    assert "PMID 90000001" in t and "<a " not in t and "<b>" not in t and "&lt;" not in t


def test_text_route_404s(env):
    env.add_user(OTHER)
    theirs = store(env, hernia(), email=OTHER)
    assert env.c.get(f"{theirs}/text", headers=H).status_code == 404
    bare = insert_run(env)
    assert env.c.get(f"/runs/{bare}/text", headers=H).status_code == 404


def test_edit_and_rerun_fills_the_ask_box_without_starting(env):
    insert_run(env, question="Robotic vs laparoscopic colectomy?")
    page = env.c.get("/library", headers=H).text
    href = re.search(r'<a href="(/\?q=[^"]+)">Edit &amp; re-run</a>', page).group(1)
    home = env.c.get(href.replace("&amp;", "&"), headers=H)
    assert home.status_code == 200 and "Robotic vs laparoscopic colectomy?" in home.text
    assert env.starter.started == []
