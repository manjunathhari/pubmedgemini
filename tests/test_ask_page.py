"""Task 9: ask a question, the run page and past runs (plan Task 9; Incisive `ask.rs`
`run_ask` for the order of the question checks, `runs/mod.rs` `RUN_NOT_FOUND_MESSAGE`).

Every test runs the real routes on a dev app over fakes: `FakeUpstream` for PubMed and Europe
PMC, `RunGemini` for Google (F2's `FakeGemini` for the researcher, the writer's parts by route;
F3a Task 7). No test reaches the network."""

import re
import uuid
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from gemini_fakes import FAKE_KEY, QUOTA_FAILURE, call, error
from test_runs import BUSY_RUN, END, HAPPY_RUN, ID1, ID2, ID3, Q1, list_reply, turn
from writer_fakes import RunGemini

from pubmedgemini.config import Settings
from pubmedgemini.gemini.quota import calls_today
from pubmedgemini.main import create_app
from pubmedgemini.runner import InlineStarter

OWNER = "owner@example.com"
H = {"x-dev-user-email": OWNER}
MODEL = "gemini-3.5-flash-lite"
QUESTION = "Is mesh better than suture repair for inguinal hernia?"
RUN_NOT_FOUND = "That question wasn&#39;t found. It may have been deleted."
PRIVACY = ("Google may use what you type here, and its reviewers may read it (Gemini free "
           "tier). Do not type anything that identifies a patient.")
NO_KEY = "Questions are not set up yet: the Gemini key is missing on the server."
CROSS_SITE = "This request did not come from this page. Open the page and search again."
ACTIVE = "A question is already running. Please wait for it to finish, then ask again."
BLOCKED = ("pubmedgemini found details that could identify a patient. Please remove them and "
           "resend your question.")


class HoldStarter:
    """Records the run ids and runs nothing: the run stays queued until a test runs it."""

    def __init__(self):
        self.run = None
        self.started = []

    def start(self, run_id):
        self.started.append(run_id)


class FailingStarter:
    def __init__(self):
        self.run = None

    def start(self, run_id):
        raise RuntimeError("can't start new thread")


class AskEnv:
    """One dev app over the fakes, with handles for the test."""

    def __init__(self, client, gemini, upstream, starter):
        self.c = client
        self.gemini = gemini
        self.upstream = upstream
        self.starter = starter

    @property
    def pool(self):
        return self.c.app.state.pool

    def rows(self, sql, params=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.rows(sql, params)[0][0]

    def user_id(self, email=OWNER):
        return self.one("SELECT id FROM users WHERE email = %s", (email,))

    def add_user(self, email):
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO users (email, role) VALUES (%s, 'surgeon')", (email,))

    def ask(self, question, headers=H, **kw):
        return self.c.post("/ask", data={"question": question}, headers=headers,
                           follow_redirects=False, **kw)

    def run_page(self, location, headers=H):
        return self.c.get(location, headers=headers)


@contextmanager
def ask_env(db_url, upstream=None, script=(), key=FAKE_KEY, starter=None, parts=None,
            **settings_extra):
    """A dev app: literature over `upstream`, Gemini over a RunGemini serving `script` to the
    researcher and `parts` to the writer (by default every part answers at once), an
    InlineStarter (the run happens inside the POST) unless another starter is given."""
    if upstream is None:
        from lit_fakes import FakeUpstream, europepmc_routes, pubmed_routes

        upstream = FakeUpstream(pubmed_routes() | europepmc_routes())
    gemini = RunGemini(list(script), parts)
    starter = starter if starter is not None else InlineStarter()
    extra = {"gemini_rpm": 1000} | settings_extra  # no pacing waits in tests
    if key is not None:
        extra["gemini_api_key"] = key
    settings = Settings(app_env="dev", database_url=db_url, owner_email=OWNER, **extra)
    app = create_app(settings, lit_transport=upstream.transport, ncbi_gap_seconds=0,
                     gemini_transport=gemini.transport, gemini_sleep=lambda _s: None,
                     run_starter=starter)
    with TestClient(app) as c:
        yield AskEnv(c, gemini, upstream, starter)


@pytest.fixture
def env(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN) as e:
        yield e


@pytest.fixture
def held(db_url, upstream):
    with ask_env(db_url, upstream, HAPPY_RUN, starter=HoldStarter()) as e:
        yield e


@pytest.fixture
def no_key(db_url, upstream):
    with ask_env(db_url, upstream, key=None) as e:
        yield e


def add_calls(env, n, model=MODEL):
    with env.pool.connection() as conn:
        conn.execute(
            "INSERT INTO model_calls (model, step, attempt, status_kind) "
            "SELECT %s, 'researcher', 1, 'ok' "
            "FROM generate_series(1, %s)", (model, n))


def insert_run(env, question=QUESTION, status="stopped", error_kind="restarted", email=OWNER):
    rid = uuid.uuid4()
    with env.pool.connection() as conn:
        conn.execute(
            "INSERT INTO runs (id, user_id, question, status, error_kind, model, finished_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, now())",
            (rid, env.user_id(email), question, status, error_kind, MODEL))
    return rid


def textarea(page):
    m = re.search(r'<textarea id="question"[^>]*>\n(.*?)</textarea>', page, re.DOTALL)
    assert m, "no question box"
    return m.group(1)


# ---------- the home page ----------

def test_home_has_both_boxes_and_no_script(env):
    r = env.c.get("/", headers=H)
    assert r.status_code == 200
    assert r.text.count('name="question"') == 1 and r.text.count('name="q"') == 1
    assert "<script" not in r.text and "src=" not in r.text
    assert re.findall(r"<link[^>]*>", r.text) == ['<link rel="stylesheet" href="/static/app.css">']
    assert r.text.index("Ask a question") < r.text.index("Key-word search")
    assert "Your questions" not in r.text  # moved to the Library page
    assert '<form method="post" action="/ask">' in r.text
    assert '<label for="question">Ask a question</label>' in r.text
    assert '<textarea id="question" name="question" maxlength="4000" required rows="4">' in r.text
    assert '<button type="submit">Ask</button>' in r.text
    assert "Free runs left today: about 21" in r.text and PRIVACY in r.text
    assert "<h2>Key-word search</h2>" in r.text
    assert "Searches PubMed and Europe PMC with exactly what you type. No model." in r.text
    assert '<label for="q">Search PubMed and Europe PMC</label>' in r.text  # F1's form
    assert "Written answers arrive in the next stage." in r.text
    assert "Answers written from these papers" not in r.text
    assert "Daily free limit reached" not in r.text


def test_runs_left_goes_down_and_zero_shows_the_reset_time(env):
    add_calls(env, 474)
    assert "Free runs left today: about 1" in env.c.get("/", headers=H).text
    add_calls(env, 1)
    page = env.c.get("/", headers=H).text
    assert "Free runs left today: about 0" in page
    assert re.search(r"Daily free limit reached\. Resets at \d\d:\d\d IST\.", page)


def test_no_key_shows_not_set_up_and_search_still_works(no_key):
    page = no_key.c.get("/", headers=H).text
    assert NO_KEY in page and 'name="question"' not in page and "Free runs left" not in page
    assert page.count('name="q"') == 1 and "Your questions" not in page
    r = no_key.ask("Is mesh better than suture?")
    assert r.status_code == 503 and NO_KEY in r.text
    assert no_key.one("SELECT count(*) FROM runs") == 0 and no_key.gemini.sent == []
    s = no_key.c.post("/search", data={"q": "hernia"}, headers=H)
    assert s.status_code == 200 and "Searched PubMed and Europe PMC (free sources)" in s.text
    assert no_key.gemini.sent == []


def test_library_lists_past_runs_newest_first(env):
    long_q = "w" * 130
    first = insert_run(env, question=long_q, status="done", error_kind=None)
    second = insert_run(env, question="<b>second</b> question", status="failed",
                        error_kind="internal")
    page = env.c.get("/library", headers=H).text
    assert page.index(f'href="/runs/{second}"') < page.index(f'href="/runs/{first}"')
    assert "w" * 120 + "\u2026" in page and "w" * 121 not in page
    assert "&lt;b&gt;second&lt;/b&gt; question" in page and "<b>second" not in page
    assert re.search(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d\d:\d\d · Failed", page)
    assert "· Done" in page


@pytest.mark.parametrize("status,word", [("queued", "Waiting"), ("running", "Running"),
                                         ("done", "Done"), ("partial", "Partial"),
                                         ("failed", "Failed"), ("stopped", "Stopped")])
def test_status_words(env, status, word):
    insert_run(env, status=status, error_kind=None)
    assert f"· {word}</p>" in env.c.get("/library", headers=H).text


def test_other_users_runs_are_not_listed(env):
    env.add_user("b@example.com")
    rid = insert_run(env, question="b's question", email="b@example.com")
    page = env.c.get("/library", headers=H).text
    assert str(rid) not in page and "b&#39;s question" not in page


# ---------- asking ----------

def test_ask_redirects_to_an_unguessable_run_page(env):
    r = env.ask(QUESTION)
    assert r.status_code == 303 and re.fullmatch(r"/runs/[0-9a-f-]{36}", r.headers["location"])
    rid = uuid.UUID(r.headers["location"].removeprefix("/runs/"))
    assert rid.version == 4
    page = env.run_page(r.headers["location"]).text
    assert ("Searched PubMed and Europe PMC (free sources); studies selected by "
            "gemini-3.5-flash-lite") in page
    assert "Selected studies" in page and "Considered, not selected" in page
    assert "Search trail" in page and PRIVACY in page
    assert env.starter.started == [rid] and len(env.gemini.sent) == 9


def test_question_never_in_a_url(env):
    r = env.ask("zq7 mesh versus suture?")
    assert "zq7" not in r.headers["location"]
    page = env.run_page(r.headers["location"]).text
    assert all("zq7" not in u for u in re.findall(r'(?:href|action)="([^"]*)"', page))
    home = env.c.get("/", headers=H).text
    assert all("zq7" not in u for u in re.findall(r'(?:href|action)="([^"]*)"', home))
    g = env.c.get("/ask?question=yq8", headers=H, follow_redirects=False)
    assert (g.status_code, g.headers["location"]) == (303, "/")
    assert env.c.get("/?question=yq8", headers=H).status_code == 200
    assert env.one("SELECT count(*) FROM runs WHERE question LIKE '%%yq8%%'") == 0


def test_get_ask_needs_sign_in_like_home(db_url):
    settings = Settings(app_env="prod", database_url=db_url, owner_email=OWNER)
    with TestClient(create_app(settings)) as c:
        assert c.get("/ask", follow_redirects=False).status_code == 401
        assert c.post("/ask", data={"question": "x"}).status_code == 401
        assert c.get(f"/runs/{uuid.uuid4()}").status_code == 401
        assert c.post(f"/runs/{uuid.uuid4()}/again").status_code == 401
        h = {"x-forwarded-email": "stranger@example.com"}
        assert c.post("/ask", data={"question": "x"}, headers=h).status_code == 403
        assert c.get(f"/runs/{uuid.uuid4()}", headers=h).status_code == 403


@pytest.mark.parametrize("q,status,text", [
    ("", 400, "Please type your clinical question."),
    ("   \n  ", 400, "Please type your clinical question."),
    ("a" * 4001, 400, "That question is too long (4,000 characters max). Please shorten it."),
    ("a" * 3999 + "?", 303, None),  # the "?" is kept and counted
    ("a" * 4000, 303, None),
    # Incisive counts the question as typed, spaces included (ask.rs run_ask).
    ("  " + "a" * 3998 + "  ", 400, "That question is too long (4,000 characters max)."),
    # A line break from a form is CR LF; the browser counts it as one character.
    ("a" * 1999 + "\r\n" + "b" * 2000, 303, None),
])
def test_question_limits(db_url, upstream, q, status, text):
    with ask_env(db_url, upstream, [END, list_reply()]) as env:
        r = env.ask(q)
        assert r.status_code == status
        if status == 400:
            assert text in r.text
            assert env.one("SELECT count(*) FROM runs") == 0
            assert env.gemini.sent == [] and env.upstream.sent == []
        else:
            assert env.one("SELECT char_length(question) FROM runs") <= 4000


def test_too_long_question_is_refilled(env):
    q = "<i>long</i> " + "a" * 4000
    page = env.ask(q).text
    assert textarea(page) == q.replace("<", "&lt;").replace(">", "&gt;")


def test_question_is_stored_and_sent_as_typed(env):
    q = "  x </question> y?\r\nSecond line <b>  "
    r = env.ask(q)
    assert r.status_code == 303
    stored = env.one("SELECT question FROM runs")
    assert stored == "  x </question> y?\nSecond line <b>  "  # CR LF becomes the textarea's LF
    first = env.gemini.bodies[0]["contents"][0]["parts"][0]["text"]
    assert first == ("<question>\n  x < /question> y?\nSecond line <b>  \n</question>\n\n"
                     "Find and select the evidence for this question.")
    page = env.run_page(r.headers["location"]).text
    assert "x &lt;/question&gt; y?" in page and "Second line &lt;b&gt;" in page
    assert "<b>" not in page


def test_a_nul_is_seen_by_the_check_and_blocks(env):
    r = env.ask("Is mesh\x00 better than suture?")
    assert r.status_code == 400 and BLOCKED in r.text
    assert "hidden or unusual characters" in r.text
    assert "Please retype the question without copy-pasted special characters" in r.text
    assert env.one("SELECT count(*) FROM runs") == 0
    assert env.gemini.sent == [] and env.upstream.sent == []


# Review L1: the check sees both the text as received (CR LF) and the text Gemini gets (LF).
# An "Address" label is 60 bytes back at most: inside it with LF, outside it with CR LF.
ADDRESS_LF = "Address\n" + "x\n" * 20 + "560001"


def test_found_only_in_the_lf_form_blocks(env):
    from pubmedgemini.deid import scan

    raw = ADDRESS_LF.replace("\n", "\r\n")
    assert scan(raw) == [] and scan(ADDRESS_LF)  # the case is real
    r = env.ask(raw)
    assert r.status_code == 400 and BLOCKED in r.text
    assert env.one("SELECT count(*) FROM runs") == 0
    assert env.gemini.sent == [] and env.upstream.sent == []


def test_found_only_in_the_raw_form_blocks(env):
    from pubmedgemini.deid import scan

    lf = "Medicare\n\n123456\n\nbeneficiaries"
    raw = lf.replace("\n", "\r\n")
    assert scan(lf) == [] and scan(raw)
    r = env.ask(raw)
    assert r.status_code == 400 and BLOCKED in r.text
    assert env.one("SELECT count(*) FROM runs") == 0 and env.gemini.sent == []


def test_blocked_question_shows_the_message_and_refills_the_box(env):
    q = "Mrs. Lakshmi ph 9845012345 and 9845012346 <b>hernia</b> mesh?"
    r = env.ask(q)
    assert r.status_code == 400 and "location" not in r.headers
    assert BLOCKED in r.text
    assert "Found: phone number (2), person&#39;s name (1)." in r.text
    assert textarea(r.text) == q.replace("<", "&lt;").replace(">", "&gt;")
    assert "<b>hernia" not in r.text
    assert env.one("SELECT count(*) FROM runs") == 0
    assert env.gemini.sent == [] and env.upstream.sent == []
    # the rest of the home page is there
    assert "Key-word search" in r.text and PRIVACY in r.text


def test_cross_site_ask_is_refused_before_anything(env):
    for headers in ({"sec-fetch-site": "cross-site"}, {"sec-fetch-site": "same-site"},
                    {"origin": "https://evil.example"}):
        r = env.ask("zq7 mesh?", headers=H | headers)
        assert r.status_code == 403 and CROSS_SITE in r.text and "zq7" not in r.text
    assert env.one("SELECT count(*) FROM runs") == 0 and env.gemini.sent == []
    ok = env.ask(QUESTION, headers=H | {"sec-fetch-site": "same-origin"})
    assert ok.status_code == 303


def test_cross_site_run_again_is_refused(env):
    rid = insert_run(env)
    r = env.c.post(f"/runs/{rid}/again", headers=H | {"sec-fetch-site": "cross-site"})
    assert r.status_code == 403 and env.one("SELECT count(*) FROM runs") == 1


def test_daily_limit_refuses_before_any_call(env):
    add_calls(env, 475)
    r = env.ask(QUESTION)
    assert r.status_code == 429
    assert re.search(r"Daily free limit reached\. Resets at \d\d:\d\d IST\.", r.text)
    assert textarea(r.text) == QUESTION
    assert env.one("SELECT count(*) FROM runs") == 0 and env.gemini.sent == []


def test_daily_limit_leaves_one_run_at_474(env):
    add_calls(env, 474)
    assert env.ask(QUESTION).status_code == 303


def test_active_run_is_409_with_a_link_for_its_owner(held):
    first = held.ask(QUESTION)
    assert first.status_code == 303 and len(held.starter.started) == 1
    r = held.ask("another question?")
    assert r.status_code == 409 and ACTIVE in r.text
    assert f'<a href="{first.headers["location"]}">See the question that is running</a>' in r.text
    assert textarea(r.text) == "another question?"
    held.add_user("b@example.com")
    other = held.ask("third?", headers={"x-dev-user-email": "b@example.com"})
    assert other.status_code == 409 and ACTIVE in other.text
    assert "See the question that is running" not in other.text
    assert held.one("SELECT count(*) FROM runs") == 1


def test_a_run_that_cannot_start_is_failed_not_left_active(db_url, upstream):
    with ask_env(db_url, upstream, starter=FailingStarter()) as env:
        r = env.ask(QUESTION)
        assert r.status_code == 303
        assert env.one("SELECT status || '/' || error_kind FROM runs") == "failed/internal"
        assert "Something went wrong. Please try again in a minute." in \
            env.run_page(r.headers["location"]).text


# ---------- the run page ----------

def test_other_users_run_is_404_with_incisives_words(env):
    r = env.ask(QUESTION)
    env.add_user("b@example.com")
    other = env.run_page(r.headers["location"], headers={"x-dev-user-email": "b@example.com"})
    unknown = env.run_page(f"/runs/{uuid.uuid4()}")
    for bad in ("/runs/not-a-uuid", "/runs/" + "0" * 36, f"/runs/{str(uuid.uuid4()).upper()}"):
        page = env.run_page(bad)
        assert page.status_code == 404 and page.text == unknown.text
    assert other.status_code == unknown.status_code == 404
    assert other.text == unknown.text and RUN_NOT_FOUND in other.text
    again = env.c.post(r.headers["location"] + "/again",
                       headers={"x-dev-user-email": "b@example.com"})
    assert again.status_code == 404 and again.text == unknown.text


def test_running_page_refreshes_itself_and_finished_does_not(held):
    r = held.ask(QUESTION)
    page = held.run_page(r.headers["location"]).text
    assert '<meta http-equiv="refresh" content="5">' in page
    assert ("Working. This page refreshes itself; you can leave it and come back from the "
            "home page.") in page
    assert "Searched PubMed and Europe PMC (free sources); studies selected by" not in page
    assert PRIVACY in page
    held.starter.run(held.starter.started[0])  # the app's run function, set at start-up
    done = held.run_page(r.headers["location"]).text
    assert "http-equiv" not in done and "Working." not in done
    assert "Answer ready" in done  # F3a Task 7: the run now writes the answer


def test_finished_page_shows_the_study_list(env):
    page = env.run_page(env.ask(QUESTION).headers["location"]).text
    sel = page.split("<h2>Selected studies</h2>", 1)[1].split("<h2>", 1)[0]
    assert sel.index(f"PMID {ID2}") < sel.index(f"PMID {ID1}")  # the model's order
    assert f'<a href="https://pubmed.ncbi.nlm.nih.gov/{ID2}/">PMID {ID2}</a>' in sel
    assert re.search(r"<p>[^<]+ · \d{4} · SR_MA</p>", sel)
    assert re.search(r"<p>[^<]+ · \d{4} · RCT</p>", sel)
    assert "<p>The newest meta-analysis.</p>" in sel and "<p>A large trial.</p>" in sel
    assert "Publication types: " in sel and sel.count("<h3>") == 2
    assert "Abstract not available." not in sel
    con = page.split("<h2>Considered, not selected</h2>", 1)[1].split("<h2>", 1)[0]
    assert f'<a href="https://pubmed.ncbi.nlm.nih.gov/{ID3}/">PMID {ID3}</a>' in con
    assert "Older." in con
    contra = page.split("<h2>Possible contradictions</h2>", 1)[1].split("<h2>", 1)[0]
    assert "Found more pain." in contra
    trail = page.split("<h2>Search trail</h2>", 1)[1]
    assert "<li>PubMed: hernia[tiab] (5,160 found)</li>" in trail
    assert "<li>Europe PMC: hernia (" in trail
    assert "<li>Researcher: searching PubMed and Europe PMC</li>" in page
    assert "<li>Search stopped at the round cap after 8 rounds</li>" in page


def test_abstract_not_available_when_records_cannot_be_fetched(db_url):
    from lit_fakes import EUTILS_HOST, EUTILS_PATH, FakeUpstream, europepmc_routes, pubmed_routes

    routes = pubmed_routes() | europepmc_routes()
    routes[(EUTILS_HOST, EUTILS_PATH + "efetch.fcgi")] = httpx.Response(503)
    with ask_env(db_url, FakeUpstream(routes), HAPPY_RUN) as env:
        page = env.run_page(env.ask(QUESTION).headers["location"]).text
    sel = page.split("<h2>Selected studies</h2>", 1)[1].split("<h2>", 1)[0]
    assert sel.count("Abstract not available.") == 2 and sel.count("<h3>") == 2


def test_model_text_is_escaped_on_the_run_page(db_url, upstream):
    hostile = list_reply(
        selected=[{"pmid": ID1, "design": "RCT", "why": "<script>alert(1)</script> why"}],
        considered=[{"pmid": ID2, "reason": "<img src=x onerror=alert(2)>"}],
        contradictions=[{"pmid": ID3, "note": "<a href='javascript:x'>n</a>"}])
    with ask_env(db_url, upstream, [turn(call("search_pubmed", Q1)), END, hostile]) as env:
        page = env.run_page(env.ask(QUESTION).headers["location"]).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; why" in page and "<script" not in page
    assert "&lt;img src=x onerror=alert(2)&gt;" in page and "<img" not in page
    assert "javascript:" not in page.replace("&lt;a href=&#39;javascript:x&#39;&gt;", "")


def test_no_evidence_message(db_url, upstream):
    nothing = [turn(call("search_pubmed", Q1)), END,
               list_reply(considered=[{"pmid": ID1, "reason": "Off topic."}])]
    with ask_env(db_url, upstream, nothing) as env:
        page = env.run_page(env.ask(QUESTION).headers["location"]).text
    assert "No relevant evidence found." in page and "<h2>Selected studies</h2>" not in page


@pytest.mark.parametrize("script,text", [
    ([error(402, "X")], ("Google refused the Gemini key because its Google project is linked to "
                        "billing. Make a key in a project with billing turned off.")),
    ([error(401, "X")], ("Google refused the Gemini key. The owner needs to check it on the "
                        "server.")),
    ([error(503, "X")] * 3, ("Gemini could not be reached or refused the request. Please try "
                            "again in a minute.")),
    ([error(429, "X", [QUOTA_FAILURE("GenerateRequestsPerDayPerProjectPerModel-FreeTier")])],
     "Daily free limit reached. Resets at "),
    ([error(429, "X")] * 3, ("The free Gemini limit stayed busy, so the run could not finish. "
                            "Please try again in a minute.")),
])
def test_kind_messages_are_plain(db_url, upstream, script, text):
    with ask_env(db_url, upstream, script) as env:
        page = env.run_page(env.ask(QUESTION).headers["location"]).text
        assert env.one("SELECT status FROM runs") == "failed"
    assert text in page and "Traceback" not in page and "made-up message" not in page


def test_a_failed_records_fetch_shows_its_plain_message(db_url):
    # Owner's answer Q6: the selected studies' records could not be fetched (`failed lit`).
    from lit_fakes import EUTILS_HOST, EUTILS_PATH, FakeUpstream, europepmc_routes, pubmed_routes

    listed = []
    routes = pubmed_routes() | europepmc_routes()
    summaries = routes[(EUTILS_HOST, EUTILS_PATH + "esummary.fcgi")]

    def down_after_the_list(request):  # the searches work; the records fetch after the list
        if listed:
            return httpx.Response(503)
        return httpx.Response(200, headers=summaries.headers, content=summaries.content)

    def the_list(request):
        listed.append(True)
        return httpx.Response(200, json=HAPPY_RUN[-1])

    routes[(EUTILS_HOST, EUTILS_PATH + "esummary.fcgi")] = down_after_the_list
    with ask_env(db_url, FakeUpstream(routes), HAPPY_RUN[:-1] + [the_list]) as env:
        page = env.run_page(env.ask(QUESTION).headers["location"]).text
        assert env.rows("SELECT status, error_kind FROM runs") == [("failed", "lit")]
        assert env.one("SELECT count(*) FROM answers") == 0
    assert ("PubMed could not be reached to fetch the selected studies, so no answer could be "
            "written. Please run the question again.") in page
    assert "<h2>Selected studies</h2>" in page and "Traceback" not in page


def test_hard_stop_and_internal_messages(env):
    hs = insert_run(env, status="failed", error_kind="hard_stop")
    assert "This run ran out of time (7 minutes). Please run the question again." in \
        env.run_page(f"/runs/{hs}").text
    with env.pool.connection() as conn:
        conn.execute("UPDATE runs SET status = 'done' WHERE id = %s", (hs,))
    internal = insert_run(env, status="failed", error_kind="internal")
    assert "Something went wrong. Please try again in a minute." in \
        env.run_page(f"/runs/{internal}").text


CAP_MESSAGE = ("Gemini stayed busy, so this run used up its share of today&#39;s free calls. "
               "Please run the question again.")


def test_the_run_cap_shows_its_plain_message(db_url, upstream):
    with ask_env(db_url, upstream, BUSY_RUN) as env:
        page = env.run_page(env.ask(QUESTION).headers["location"]).text
        assert env.rows("SELECT status, error_kind FROM runs") == [("failed", "run_call_cap")]
    assert CAP_MESSAGE in page and "Traceback" not in page


def test_a_run_admitted_with_one_left_never_passes_the_day_limit(db_url, upstream):
    # Owner decision, 11 Nov 2026: cap calls per run. 474 of 500 used leaves one run; Gemini
    # busy on every call takes it to its cap, which lands exactly on the day limit.
    with ask_env(db_url, upstream, BUSY_RUN) as env:
        add_calls(env, 474)
        assert "Free runs left today: about 1" in env.c.get("/", headers=H).text
        assert env.ask(QUESTION).status_code == 303
        assert env.one("SELECT error_kind FROM runs") == "run_call_cap"
        assert calls_today(env.pool, MODEL) == 500
        assert "Free runs left today: about 0" in env.c.get("/", headers=H).text
        assert env.ask(QUESTION).status_code == 429


# ---------- run again ----------

def test_stopped_run_has_run_again_and_it_reuses_the_question(env):
    rid = insert_run(env, question="Stored question about mesh?")
    page = env.run_page(f"/runs/{rid}").text
    assert "This run was stopped because the app restarted." in page
    assert f'<form method="post" action="/runs/{rid}/again">' in page
    assert '<button type="submit">Run again</button>' in page
    assert "http-equiv" not in page
    r = env.c.post(f"/runs/{rid}/again", headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] != f"/runs/{rid}"
    new = uuid.UUID(r.headers["location"].removeprefix("/runs/"))
    assert env.one("SELECT question FROM runs WHERE id = %s", (new,)) == \
        "Stored question about mesh?"
    assert "Stored question about mesh?" in env.gemini.bodies[0]["contents"][0]["parts"][0][
        "text"]


def test_finished_run_has_no_run_again_button(env):
    page = env.run_page(env.ask(QUESTION).headers["location"]).text
    assert "Run again" not in page


# Owner decision, 11 Nov 2026: Run again on failed and partial runs too.
@pytest.mark.parametrize("status,error_kind", [
    ("stopped", "restarted"), ("failed", "hard_stop"), ("failed", "run_call_cap"),
    ("failed", "overloaded"), ("partial", "empty_selection"), ("partial", None)])
def test_run_again_is_offered_on_stopped_failed_and_partial_runs(env, status, error_kind):
    rid = insert_run(env, question="Stored question about mesh?", status=status,
                     error_kind=error_kind)
    page = env.run_page(f"/runs/{rid}").text
    assert f'<form method="post" action="/runs/{rid}/again">' in page
    assert '<button type="submit">Run again</button>' in page
    r = env.c.post(f"/runs/{rid}/again", headers=H, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] != f"/runs/{rid}"
    new = uuid.UUID(r.headers["location"].removeprefix("/runs/"))
    assert env.one("SELECT question FROM runs WHERE id = %s", (new,)) == \
        "Stored question about mesh?"


@pytest.mark.parametrize("status", ["done", "running", "queued"])
def test_run_again_is_not_offered_on_done_running_or_queued_runs(env, status):
    rid = insert_run(env, status=status, error_kind=None)
    page = env.run_page(f"/runs/{rid}").text
    assert "Run again" not in page and "/again" not in page


def test_cross_site_run_again_of_a_failed_run_is_refused(env):
    rid = insert_run(env, status="failed", error_kind="run_call_cap")
    r = env.c.post(f"/runs/{rid}/again", headers=H | {"sec-fetch-site": "cross-site"})
    assert r.status_code == 403 and CROSS_SITE in r.text
    assert env.one("SELECT count(*) FROM runs") == 1 and env.gemini.sent == []


def test_run_again_of_a_blocked_partial_question_shows_the_refusal(env):
    q = "Mrs. Lakshmi asks: is mesh better?"  # stored before a check: simulated
    rid = insert_run(env, question=q, status="partial", error_kind="empty_selection")
    r = env.c.post(f"/runs/{rid}/again", headers=H, follow_redirects=False)
    assert r.status_code == 400 and BLOCKED in r.text and textarea(r.text) == q
    assert env.one("SELECT count(*) FROM runs") == 1 and env.gemini.sent == []


def test_run_again_of_a_blocked_question_shows_the_refusal(env):
    q = "Mrs. Lakshmi asks: is mesh better?"  # stored before a check: simulated
    rid = insert_run(env, question=q)
    r = env.c.post(f"/runs/{rid}/again", headers=H, follow_redirects=False)
    assert r.status_code == 400 and BLOCKED in r.text and textarea(r.text) == q
    assert env.one("SELECT count(*) FROM runs") == 1 and env.gemini.sent == []


def test_run_again_while_another_runs_is_409(held):
    rid = insert_run(held)
    held.ask(QUESTION)
    r = held.c.post(f"/runs/{rid}/again", headers=H, follow_redirects=False)
    assert r.status_code == 409 and ACTIVE in r.text


def test_run_again_without_a_key_is_503(no_key):
    rid = insert_run(no_key)
    r = no_key.c.post(f"/runs/{rid}/again", headers=H)
    assert r.status_code == 503 and NO_KEY in r.text
