"""F7 Task 4: every question runs on the asking doctor's own Gemini key, from their cookie.

No key on the server; the key reaches the run thread in memory only, and is never written to a
table, a log line or a page."""

import logging

import httpx
from gemini_fakes import FAKE_KEY
from test_ask_page import (
    HAPPY_RUN,
    OWNER,
    QUESTION,
    H,
    HoldStarter,
    add_calls,
    ask_env,
)

from pubmedgemini.gemini.quota import calls_today
from pubmedgemini.keycookie import COOKIE_NAME, MAX_AGE_SECONDS, seal
from pubmedgemini.runs import create_run

OTHER = "b@example.com"
HB = {"x-dev-user-email": OTHER}
OTHER_KEY = "AIzaOtherDoctorsKey_0000000000"
NO_KEY_YET = "To ask questions, add your own free Gemini key."
MODEL = "gemini-3.5-flash-lite"


def keyed(steps, seen):
    """The same replies, recording the key each researcher request carried."""

    def wrap(step):
        def handler(request: httpx.Request):
            seen.append(request.headers.get("x-goog-api-key"))
            if callable(step) and not isinstance(step, httpx.Response):
                return step(request)
            if isinstance(step, dict):
                return httpx.Response(200, json=step)
            return step

        return handler

    return [wrap(s) for s in steps]


# ---------- no key ----------

def test_home_without_a_key_offers_the_key_page(db_url, upstream):
    with ask_env(db_url, upstream, key=None) as env:
        r = env.c.get("/", headers=H)
    assert r.status_code == 200
    assert NO_KEY_YET in r.text and '<form method="get" action="/key">' in r.text
    assert 'name="question"' not in r.text  # no question box without a key
    assert 'name="q"' in r.text  # the key-word search works without one


def test_ask_without_a_key_is_refused_and_starts_nothing(db_url, upstream):
    with ask_env(db_url, upstream, key=None) as env:
        r = env.ask(QUESTION)
        assert r.status_code == 403 and NO_KEY_YET in r.text
        assert env.one("SELECT count(*) FROM runs") == 0
        assert env.gemini.sent == []


def test_a_cookie_made_for_another_account_is_no_key(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN) as env:
        env.add_user(OTHER)
        sealed = seal(FAKE_KEY, OWNER, env.secret)  # the owner's cookie, left on a shared PC
        env.c.cookies.set(COOKIE_NAME, sealed)
        r = env.c.post("/ask", data={"question": QUESTION}, headers=HB, follow_redirects=False)
        assert r.status_code == 403 and NO_KEY_YET in r.text
        assert env.one("SELECT count(*) FROM runs") == 0


def test_run_again_without_a_key_is_refused(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN) as env:
        loc = env.ask(QUESTION).headers["location"]
        env.c.cookies.clear()
        r = env.c.post(f"{loc}/again", headers=H, follow_redirects=False)
        assert r.status_code == 403 and NO_KEY_YET in r.text
        assert env.one("SELECT count(*) FROM runs") == 1


# ---------- each doctor's own key ----------

def test_each_run_uses_its_own_doctors_key(db_url, upstream):
    seen: list[str | None] = []
    with ask_env(db_url, upstream, keyed(HAPPY_RUN, seen) + keyed(HAPPY_RUN, seen)) as env:
        env.add_user(OTHER)
        assert env.ask(QUESTION).status_code == 303
        n_owner = len(seen)
        assert env.ask(QUESTION, headers=HB, key=OTHER_KEY).status_code == 303
    assert n_owner > 0 and len(seen) > n_owner
    assert set(seen[:n_owner]) == {FAKE_KEY}
    assert set(seen[n_owner:]) == {OTHER_KEY}


def test_the_day_is_counted_per_doctor(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN) as env:
        env.add_user(OTHER)
        env.ask(QUESTION)
        owner, other = env.user_id(OWNER), env.user_id(OTHER)
        used = env.one("SELECT count(*) FROM model_calls WHERE user_id = %s", (owner,))
        assert used > 0
        assert env.one("SELECT count(*) FROM model_calls WHERE user_id IS NULL") == 0
        assert calls_today(env.pool, MODEL, owner) == used
        assert calls_today(env.pool, MODEL, other) == 0


def test_one_doctors_used_day_does_not_stop_another(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN, gemini_rpd=100) as env:
        env.add_user(OTHER)
        add_calls(env, 100, user_id=env.user_id(OWNER))
        assert env.ask(QUESTION).status_code == 429  # the owner's own day is used
        env.prime(OTHER, OTHER_KEY)
        page = env.c.get("/", headers=HB)
        assert "Free runs left today: about 4" in page.text  # 1 + (100 - 26) // 23
        assert env.ask("their own question?", headers=HB, key=OTHER_KEY).status_code == 303


def test_a_run_whose_key_is_gone_fails_without_a_call(db_url, upstream):
    """After a restart the key is gone with the process: the run cannot go on."""
    with ask_env(db_url, upstream, HAPPY_RUN, starter=HoldStarter()) as env:
        rid = create_run(env.pool, env.user_id(), QUESTION, MODEL)
        env.c.app.state.run_question(rid)
        assert env.one("SELECT status || '/' || error_kind FROM runs") == "failed/key_missing"
        assert env.gemini.sent == []


def test_a_key_handed_to_a_run_is_dropped_when_it_cannot_start(db_url, upstream):
    from test_ask_page import FailingStarter

    with ask_env(db_url, upstream, starter=FailingStarter()) as env:
        env.ask(QUESTION)
        assert env.c.app.state.run_keys.pending() == 0


# ---------- the key never leaves memory ----------

def test_the_key_is_in_no_table_log_or_page(db_url, upstream, caplog):
    caplog.set_level(logging.DEBUG)
    with ask_env(db_url, upstream, HAPPY_RUN) as env:
        r = env.ask(QUESTION)
        pages = [env.run_page(r.headers["location"]).text, env.c.get("/", headers=H).text,
                 env.c.get("/library", headers=H).text]
        dump = env.rows(
            "SELECT string_agg(t::text, ' ') FROM ("
            " SELECT row_to_json(x)::text AS t FROM runs x UNION ALL"
            " SELECT row_to_json(x)::text FROM model_calls x UNION ALL"
            " SELECT row_to_json(x)::text FROM run_steps x UNION ALL"
            " SELECT row_to_json(x)::text FROM users x) s")[0][0]
    for text in (*pages, dump, caplog.text):
        assert FAKE_KEY not in text


# ---------- the cookie is renewed, so it stays until removed (O3) ----------

def test_a_signed_in_page_renews_the_cookie(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN) as env:
        env.prime(OWNER)
        sealed = env.c.cookies.get(COOKIE_NAME)
        r = env.c.get("/library", headers=H)
    header = r.headers.get("set-cookie", "")
    assert header.startswith(f"{COOKIE_NAME}={sealed};")
    assert f"max-age={MAX_AGE_SECONDS}" in header.lower()


def test_no_cookie_means_nothing_is_set(db_url, upstream):
    with ask_env(db_url, upstream, key=None) as env:
        r = env.c.get("/library", headers=H)
    assert "set-cookie" not in r.headers


# ---------- Google refuses the doctor's own key during a run ----------

def test_a_refused_key_points_to_the_key_page(db_url, upstream):
    from gemini_fakes import error

    with ask_env(db_url, upstream, [error(403, "PERMISSION_DENIED")]) as env:
        page = env.run_page(env.ask(QUESTION).headers["location"]).text
    assert "Google refused your Gemini key. Check it on the" in page
    assert '<a href="/key">Your key</a>' in page
    assert "The owner needs to check it on the server" not in page


def test_a_billing_key_says_what_to_do(db_url, upstream):
    from gemini_fakes import error

    with ask_env(db_url, upstream, [error(402, "X")]) as env:
        page = env.run_page(env.ask(QUESTION).headers["location"]).text
    assert "linked to billing" in page and "billing turned off" in page
    assert '<a href="/key">Your key</a>' in page
