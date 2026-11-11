"""The Library page, the Activity page and the menu (owner decisions, 11 Nov 2026).

Pages run on a dev app over fakes (as test_ask_page); the day counts run on a migrated pool."""

import re
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from test_ask_page import HAPPY_RUN, MODEL, H, HoldStarter, ask_env, insert_run

from pubmedgemini.runs import daily_question_counts

OTHER = "b@example.com"
HB = {"x-dev-user-email": OTHER}


@pytest.fixture
def env(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN, starter=HoldStarter()) as e:
        yield e


def nav_of(html):
    blocks = re.findall(r'<nav aria-label="Main">(.*?)</nav>', html, re.DOTALL)
    assert len(blocks) == 1
    return blocks[0]


def nav_links(html):
    return re.findall(r'<a href="([^"]+)"([^>]*)>([^<]+)</a>', nav_of(html))


def current_of(html):
    return [text for _, attrs, text in nav_links(html) if 'aria-current="page"' in attrs]


def set_created(env, run_id, created_at):
    with env.pool.connection() as conn:
        conn.execute("UPDATE runs SET created_at = %s WHERE id = %s", (created_at, run_id))


# ---------- the menu ----------

def test_menu_on_signed_in_pages(env):
    rid = insert_run(env)
    pages = {
        "home": (env.c.get("/", headers=H), "Ask"),
        "library": (env.c.get("/library", headers=H), "Library"),
        "activity": (env.c.get("/activity", headers=H), "Activity"),
        "results": (env.c.post("/search", data={"q": "hernia mesh"}, headers=H), "Ask"),
        "run": (env.c.get(f"/runs/{rid}", headers=H), "Library"),
    }
    for name, (r, current) in pages.items():
        assert r.status_code == 200, name
        links = nav_links(r.text)
        assert [(h, t) for h, _, t in links] == [("/", "Ask"), ("/library", "Library"),
                                                 ("/activity", "Activity")], name
        assert current_of(r.text) == [current], name
        assert 'class="current"' in nav_of(r.text), name
        assert r.text.count("<nav") == 1 and "<script" not in r.text, name


def test_nav_is_inside_the_header(env):
    page = env.c.get("/library", headers=H).text
    header = re.search(r"<header\b.*?</header>", page, re.DOTALL).group(0)
    assert '<nav aria-label="Main">' in header


def test_no_menu_when_not_set_up(env):
    for path in ("/", "/library", "/activity"):
        r = env.c.get(path, headers={"x-dev-user-email": "stranger@example.com"})
        assert r.status_code == 403
        assert "haven&#39;t been set up yet" in r.text
        assert "<nav" not in r.text
        assert "<header" in r.text
    # a message page reached with no known user (no sign-in) has none either
    r = env.c.get("/library")
    assert r.status_code == 401 and "<nav" not in r.text and "<header" in r.text


def test_menu_on_message_page_for_signed_in_user(env):
    for path in (f"/runs/{uuid.uuid4()}", "/runs/not-a-uuid"):
        r = env.c.get(path, headers=H)
        assert r.status_code == 404 and "That question wasn&#39;t found" in r.text
        assert [(h, t) for h, _, t in nav_links(r.text)] == [
            ("/", "Ask"), ("/library", "Library"), ("/activity", "Activity")]
        assert current_of(r.text) == [] and 'class="current"' not in r.text
    again = env.c.post(f"/runs/{uuid.uuid4()}/again", headers=H)
    assert again.status_code == 404 and len(nav_links(again.text)) == 3


# ---------- Library ----------

def test_library_lists_my_questions_newest_first(env):
    first = insert_run(env, question="first question", status="done", error_kind=None)
    second = insert_run(env, question="second question", status="failed", error_kind="internal")
    r = env.c.get("/library", headers=H)
    assert r.status_code == 200 and "<h1>Library</h1>" in r.text
    assert r.text.index("second question") < r.text.index("first question")
    assert r.text.index(f'href="/runs/{second}"') < r.text.index(f'href="/runs/{first}"')
    assert re.search(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d\d:\d\d · Failed", r.text)
    assert "· Done" in r.text
    assert "No questions yet." not in r.text


def test_library_links_to_runs(env):
    rid = insert_run(env, question="w" * 130)
    page = env.c.get("/library", headers=H).text
    assert f'<a href="/runs/{rid}">' + "w" * 120 + "\u2026</a>" in page
    assert "w" * 121 not in page
    assert env.c.get(f"/runs/{rid}", headers=H).status_code == 200


def test_library_shows_at_most_50(env):
    for _ in range(52):
        insert_run(env, question="q", status="done", error_kind=None)
    page = env.c.get("/library", headers=H).text
    assert page.count('href="/runs/') == 50


def test_library_does_not_show_another_users_questions(env):
    env.add_user(OTHER)
    other = insert_run(env, question="zebra-quartz-7731 distinctive", email=OTHER)
    insert_run(env, question="mine")
    page = env.c.get("/library", headers=H).text
    assert "zebra-quartz-7731" not in page and str(other) not in page
    assert "mine" in page
    theirs = env.c.get("/library", headers=HB).text
    assert "zebra-quartz-7731" in theirs and "mine" not in theirs


def test_library_empty_state(env):
    page = env.c.get("/library", headers=H).text
    assert "No questions yet." in page and 'href="/runs/' not in page


def test_library_escapes_question(env):
    insert_run(env, question="<b>x</b> bold?")
    page = env.c.get("/library", headers=H).text
    assert "&lt;b&gt;x&lt;/b&gt; bold?" in page and "<b>x</b>" not in page


def test_home_no_longer_lists_questions(env):
    rid = insert_run(env, question="listed question")
    page = env.c.get("/", headers=H).text
    assert "Your questions" not in page and "No questions yet." not in page
    assert "listed question" not in page and f"/runs/{rid}" not in page
    assert 'name="question"' in page and 'name="q"' in page
    assert "Key-word search" in page


# ---------- daily_question_counts ----------

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # 17:30 on 11 Nov in India


def add_user(pool, email):
    with pool.connection() as conn:
        return conn.execute(
            "INSERT INTO users (email, role) VALUES (%s, 'surgeon') RETURNING id", (email,)
        ).fetchone()[0]


def add_run(pool, user_id, created_at, status="done"):
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO runs (id, user_id, question, status, model, created_at) "
            "VALUES (%s, %s, 'q', %s, %s, %s)",
            (uuid.uuid4(), user_id, status, MODEL, created_at))


def test_daily_question_counts_has_14_days_with_zeros(migrated_pool):
    uid = add_user(migrated_pool, "a@example.com")
    got = daily_question_counts(migrated_pool, uid, NOW)
    assert len(got) == 14 and len({d for d, _ in got}) == 14
    assert all(n == 0 for _, n in got)
    assert len(daily_question_counts(migrated_pool, uid, NOW, days=3)) == 3


def test_daily_question_counts_newest_first_ends_today(migrated_pool):
    uid = add_user(migrated_pool, "a@example.com")
    add_run(migrated_pool, uid, NOW)
    add_run(migrated_pool, uid, NOW - timedelta(days=2), status="failed")
    got = daily_question_counts(migrated_pool, uid, NOW)
    assert got[0] == (date(2026, 11, 11), 1)
    assert got[-1][0] == date(2026, 10, 29)
    assert [d for d, _ in got] == sorted((d for d, _ in got), reverse=True)
    assert dict(got)[date(2026, 11, 9)] == 1 and dict(got)[date(2026, 11, 10)] == 0


def test_daily_question_counts_uses_india_day(migrated_pool):
    uid = add_user(migrated_pool, "a@example.com")
    add_run(migrated_pool, uid, datetime(2026, 11, 10, 18, 45, tzinfo=UTC))  # 00:15 on 11 Nov
    add_run(migrated_pool, uid, datetime(2026, 11, 10, 18, 15, tzinfo=UTC))  # 23:45 on 10 Nov
    add_run(migrated_pool, uid, datetime(2026, 10, 28, 18, 45, tzinfo=UTC))  # 00:15 on 29 Oct
    add_run(migrated_pool, uid, datetime(2026, 10, 28, 18, 15, tzinfo=UTC))  # 28 Oct: outside
    got = dict(daily_question_counts(migrated_pool, uid, NOW))
    assert got[date(2026, 11, 11)] == 1 and got[date(2026, 11, 10)] == 1
    assert got[date(2026, 10, 29)] == 1 and sum(got.values()) == 3
    # "today" is the India day: 19:00 UTC on 10 Nov is already 11 Nov there
    late = datetime(2026, 11, 10, 19, 0, tzinfo=UTC)
    assert daily_question_counts(migrated_pool, uid, late)[0][0] == date(2026, 11, 11)


def test_daily_question_counts_cuts_at_india_midnight_to_the_second(migrated_pool):
    uid = add_user(migrated_pool, "a@example.com")
    add_run(migrated_pool, uid, datetime(2026, 11, 10, 18, 29, 59, tzinfo=UTC))  # 23:59:59, 10 Nov
    add_run(migrated_pool, uid, datetime(2026, 11, 10, 18, 30, 0, tzinfo=UTC))  # 00:00:00, 11 Nov
    got = dict(daily_question_counts(migrated_pool, uid, NOW))
    assert got[date(2026, 11, 10)] == 1 and got[date(2026, 11, 11)] == 1
    # and the same cut for "now" itself
    at = datetime(2026, 11, 10, 18, 29, 59, tzinfo=UTC)
    assert daily_question_counts(migrated_pool, uid, at)[0][0] == date(2026, 11, 10)
    assert daily_question_counts(migrated_pool, uid, at + timedelta(seconds=1))[0][0] == date(
        2026, 11, 11)


def test_daily_question_counts_only_my_runs(migrated_pool):
    mine = add_user(migrated_pool, "a@example.com")
    other = add_user(migrated_pool, OTHER)
    add_run(migrated_pool, mine, NOW)
    for _ in range(4):
        add_run(migrated_pool, other, NOW)
    assert daily_question_counts(migrated_pool, mine, NOW)[0] == (date(2026, 11, 11), 1)
    assert daily_question_counts(migrated_pool, other, NOW)[0] == (date(2026, 11, 11), 4)


# ---------- Activity ----------

def rows_of(html):
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.DOTALL).group(1)
    return re.findall(r'<tr><td>([^<]+)</td><td class="num">(\d+)</td></tr>', body)


def test_activity_shows_14_rows_and_todays_count(env):
    before = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    insert_run(env, question="one")
    insert_run(env, question="two")
    r = env.c.get("/activity", headers=H)
    assert r.status_code == 200 and "<h1>Activity</h1>" in r.text
    assert "My questions, last 14 days" in r.text
    after = datetime.now(UTC) + timedelta(hours=5, minutes=30)
    rows = rows_of(r.text)
    assert len(rows) == 14 and r.text.count("<tr>") == 15  # one header row
    assert re.fullmatch(r"[A-Z][a-z]{2} \d{1,2} [A-Z][a-z]{2}", rows[0][0])
    # The page's "today" is the India day. The real clock is used, so a test that straddles
    # India midnight (18:30 UTC) may see either adjacent day first; the exact cut is tested on
    # `daily_question_counts` with fixed times.
    labels = {f"{d:%a} {d.day} {d:%b}" for d in (before, after)}
    assert rows[0][0] in labels
    assert sum(int(n) for _, n in rows) == 2 and all(int(n) <= 2 for _, n in rows)
    assert 'class="table-scroll"' in r.text


def test_activity_does_not_show_another_users_counts_or_email(env):
    env.add_user(OTHER)
    for _ in range(7):
        insert_run(env, question="theirs", email=OTHER)
    insert_run(env, question="mine")
    page = env.c.get("/activity", headers=H).text
    assert OTHER not in page and "theirs" not in page
    assert rows_of(page)[0][1] == "1" and all(n != "7" for _, n in rows_of(page))
    assert "All doctors" not in page


def test_no_script_tags_on_new_pages(env):
    for path in ("/library", "/activity"):
        assert "<script" not in env.c.get(path, headers=H).text.lower(), path
