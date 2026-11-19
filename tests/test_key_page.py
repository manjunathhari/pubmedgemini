"""F7 Task 5: the "Your Gemini key" page (/key), built to the frozen mock v1 (18 Nov 2026).

The doctor pastes their own free key; one tiny check call to Google decides whether it is
saved, in their browser only (keycookie.py)."""

import logging
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from gemini_fakes import QUOTA_FAILURE, error, reply
from test_ask_page import OWNER, TEST_SECRET_B64, H, ask_env

from pubmedgemini.keycookie import COOKIE_NAME, open_, seal

KEY = "AIzaPastedByTheDoctor_0123456789wxyz"
OK = reply([{"text": "OK"}], out=1)

NO_KEY_SAVED = "No key saved in this browser yet."
SAVED = "Key saved in this browser."
REFUSED = "Not saved. Google did not accept this key. Copy it again from Google AI Studio."
BILLING = ("Not saved. This key&#39;s project has billing turned on. Make a key in a project "
           "with billing turned off, then paste it again.")
NOT_KEY = "Not saved. That does not look like a Gemini key. Copy it again from Google AI Studio."
UNREACHABLE = "Not saved. Google could not be reached. Please try again in a minute."
LIMIT = "Key saved. It has used today&#39;s free limit and will work again after "
REMOVED = "Key removed from this browser."


@pytest.fixture
def https_env(db_url, upstream):
    """An env whose client talks https, so the Secure cookie the page sets comes back."""
    with ask_env(db_url, upstream, key=None) as env:
        env.c = TestClient(env.c.app, base_url="https://testserver")
        env.c.__enter__()
        try:
            yield env
        finally:
            env.c.__exit__(None, None, None)


def save(env, key=KEY, headers=H):
    return env.c.post("/key", data={"key": key}, headers=headers, follow_redirects=True)


def script(env, *steps):
    env.gemini.script.extend(steps)


# ---------- the page ----------

def test_page_with_no_key(https_env):
    r = https_env.c.get("/key", headers=H)
    assert r.status_code == 200
    t = r.text
    assert "Run questions on your own free key" in t and NO_KEY_SAVED in t
    assert 'type="password"' in t and 'name="key"' in t and 'autocomplete="off"' in t
    assert '<form method="post" action="/key" id="key-form">' in t and "Save key" in t
    assert "billing turned off" in t and "Where your key is kept" in t
    assert "It stays until you remove it." in t
    assert 'href="https://ai.google.dev/gemini-api/terms"' in t
    assert 'href="/key" class="current" aria-current="page">Your key</a>' in t
    assert "Remove key from this browser" not in t


def test_save_checks_the_key_once_and_keeps_it_only_in_the_cookie(https_env):
    script(https_env, OK)
    r = save(https_env)
    assert r.status_code == 200 and SAVED in r.text
    assert len(https_env.gemini.sent) == 1
    sent = https_env.gemini.sent[0]
    assert sent.headers["x-goog-api-key"] == KEY
    assert KEY not in sent.url.query.decode() and KEY.encode() not in sent.content
    sealed = https_env.c.cookies.get(COOKIE_NAME)
    opened = open_(sealed, OWNER, https_env.secret)
    assert opened is not None and opened.get_secret_value() == KEY
    assert "A key is saved in this browser (ends ••••wxyz)." in r.text
    assert KEY not in r.text and KEY[:-4] not in r.text
    row = https_env.rows("SELECT step, status_kind, user_id FROM model_calls")
    assert row == [("key_check", "ok", https_env.user_id())]


def test_a_saved_key_page_shows_runs_left_and_remove(https_env):
    script(https_env, OK)
    t = save(https_env).text
    assert "Free runs left today on your key: about 21" in t  # 500 - 1 key check
    assert "Remove key from this browser" in t and "Change key" in t


def test_whitespace_around_a_pasted_key_is_trimmed(https_env):
    script(https_env, OK)
    assert SAVED in save(https_env, key=f"  {KEY}\n").text
    assert open_(https_env.c.cookies.get(COOKIE_NAME), OWNER,
                 https_env.secret).get_secret_value() == KEY


@pytest.mark.parametrize("bad", ["", "   ", "short", "not a key at all with spaces 12345",
                                 "x" * 201])
def test_something_not_key_shaped_is_refused_without_a_call(https_env, bad):
    r = save(https_env, key=bad)
    assert r.status_code == 400 and NOT_KEY in r.text
    assert https_env.gemini.sent == [] and https_env.c.cookies.get(COOKIE_NAME) is None


@pytest.mark.parametrize("step,text", [
    (error(400, "INVALID_ARGUMENT", details=[{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                              "reason": "API_KEY_INVALID"}]), REFUSED),
    (error(403, "PERMISSION_DENIED"), REFUSED),
    (error(401, "UNAUTHENTICATED"), REFUSED),
    (error(402, "X"), BILLING),
    (error(503, "UNAVAILABLE"), UNREACHABLE),
    (error(404, "NOT_FOUND"), UNREACHABLE),
])
def test_a_refused_key_is_not_saved(https_env, step, text):
    script(https_env, step)
    r = save(https_env)
    assert text in r.text and r.status_code == 400
    assert https_env.c.cookies.get(COOKIE_NAME) is None
    assert NO_KEY_SAVED in r.text


def test_google_unreachable_is_not_saved(https_env):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    script(https_env, down)
    r = save(https_env)
    assert UNREACHABLE in r.text and https_env.c.cookies.get(COOKIE_NAME) is None


def test_a_key_at_its_daily_limit_is_saved_with_the_reset_time(https_env):
    script(https_env, error(429, "X", [QUOTA_FAILURE("GenerateRequestsPerDay-FreeTier")]))
    r = save(https_env)
    assert LIMIT in r.text and " IST." in r.text
    assert https_env.c.cookies.get(COOKIE_NAME) is not None


def test_a_busy_minute_still_saves_the_key(https_env):
    script(https_env, error(429, "X"))
    assert SAVED in save(https_env).text
    assert https_env.c.cookies.get(COOKIE_NAME) is not None


def test_remove_clears_the_cookie(https_env):
    script(https_env, OK)
    save(https_env)
    r = https_env.c.post("/key/remove", headers=H, follow_redirects=True)
    assert REMOVED in r.text and NO_KEY_SAVED in r.text
    assert https_env.c.cookies.get(COOKIE_NAME) is None


def test_posts_from_another_site_are_refused(https_env):
    cross = H | {"sec-fetch-site": "cross-site"}
    assert https_env.c.post("/key", data={"key": KEY}, headers=cross).status_code == 403
    assert https_env.c.post("/key/remove", headers=cross).status_code == 403
    assert https_env.gemini.sent == []


def test_get_with_a_key_in_the_query_never_saves(https_env):
    r = https_env.c.get(f"/key?key={KEY}", headers=H)
    assert r.status_code == 200 and https_env.c.cookies.get(COOKIE_NAME) is None
    assert KEY not in r.text


def test_the_key_never_reaches_a_log_or_the_database(https_env, caplog):
    caplog.set_level(logging.DEBUG)
    script(https_env, OK)
    save(https_env)
    script(https_env, error(403, "PERMISSION_DENIED", message=f"echo {KEY}"))
    save(https_env, key="AIzaSecondKeyPasted_000000000000")
    dump = https_env.rows("SELECT string_agg(row_to_json(m)::text, ' ') FROM model_calls m")[0][0]
    for text in (caplog.text, dump):
        assert KEY not in text and "AIzaSecondKeyPasted" not in text
    key_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("key ")]
    assert key_lines == ["key kind=saved check=ok", "key kind=not_saved check=key_refused"]


def test_the_home_page_links_to_the_key_page_in_the_menu(https_env):
    t = https_env.c.get("/", headers=H).text
    assert '<a href="/key">Your key</a>' in t


def test_a_saved_key_lets_the_doctor_ask(https_env):
    from test_runs import HAPPY_RUN

    script(https_env, OK, *HAPPY_RUN)
    save(https_env)
    r = https_env.c.post("/ask", data={"question": "Is mesh better than suture?"}, headers=H,
                         follow_redirects=False)
    assert r.status_code == 303


def test_a_cookie_sealed_with_the_tests_secret_opens(https_env):
    """The fixture's secret is the one the app uses (sanity for the other tests)."""
    import base64

    assert https_env.secret == base64.b64decode(TEST_SECRET_B64)
    assert open_(seal(KEY, OWNER, https_env.secret), OWNER, https_env.secret) is not None


# ---------- review Important 3: a limit on key checks ----------

TOO_MANY = "Not saved. Too many keys tried. Please wait 10 minutes and try again."


def test_key_checks_are_limited_per_doctor(https_env):
    script(https_env, *[error(403, "PERMISSION_DENIED")] * 5)
    for i in range(5):
        assert REFUSED in save(https_env, key=f"AIzaTryNumber{i}_0000000000000000").text
    r = save(https_env, key="AIzaTryNumber6_0000000000000000")
    assert r.status_code == 429 and TOO_MANY in r.text
    assert len(https_env.gemini.sent) == 5  # the sixth made no call
    assert https_env.one("SELECT count(*) FROM model_calls WHERE step = 'key_check'") == 5


def test_older_checks_do_not_count_toward_the_limit(https_env):
    uid = https_env.user_id()
    with https_env.pool.connection() as conn:
        conn.execute("INSERT INTO model_calls (model, step, attempt, status_kind, user_id, "
                     "called_at) SELECT 'gemini-3.5-flash-lite', 'key_check', 1, 'ok', %s, "
                     "now() - interval '11 minutes' FROM generate_series(1, 5)", (uid,))
    script(https_env, OK)
    assert SAVED in save(https_env).text


def test_the_key_check_waits_on_the_doctors_pacer(https_env):
    pacer = https_env.c.app.state.pacers.get(https_env.user_id())
    waits = []
    real = pacer.wait

    def spy(estimated, deadline):
        waits.append(estimated)
        return real(estimated, deadline)

    pacer.wait = spy
    script(https_env, OK)
    save(https_env)
    assert len(waits) == 1



# ---------- the guided walkthrough (frozen mock v2, 19 Nov 2026) ----------

AI_STUDIO_FORM = ('<form method="get" action="https://aistudio.google.com/apikey" '
                  'target="_blank" rel="noopener noreferrer" class="wt-open">')


def test_the_walkthrough_has_three_steps(https_env):
    t = https_env.c.get("/key", headers=H).text
    assert "Get your free key in 3 steps" in t
    assert t.count('<span class="wt-num">') == 3
    assert "Open Google AI Studio and tap <b>Create API key</b>" in t
    assert "Tap <b>Copy</b> next to your new key" in t
    assert "Come back here and tap <b>Paste</b>" in t
    assert "The key starts with <code>AQ.</code> or <code>AIza</code>." in t


def test_open_ai_studio_is_a_button_in_a_new_tab(https_env):
    t = https_env.c.get("/key", headers=H).text
    assert AI_STUDIO_FORM in t
    assert '<button type="submit">Open Google AI Studio</button>' in t


def test_two_sketches_drawn_on_the_page(https_env):
    t = https_env.c.get("/key", headers=H).text
    sketches = re.findall(r'<svg class="wt-pic"[^>]*>', t)
    assert len(sketches) == 2 and all('role="img"' in s and "aria-label=" in s for s in sketches)
    assert "<image" not in t and "googleusercontent" not in t  # drawings, not copies


def test_paste_button_waits_for_the_script(https_env):
    """Without JavaScript the Paste button stays hidden and Save key does the job."""
    t = https_env.c.get("/key", headers=H).text
    assert '<button type="button" id="key-paste" hidden>Paste</button>' in t
    assert '<button type="submit" class="quiet">Save key</button>' in t
    assert re.findall(r"<script\b[^>]*>", t).count("<script>") == 3  # theme x2 + this page's
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", t)  # no inline event attributes


def test_common_fixes_are_folded_until_a_key_is_refused(https_env):
    t = https_env.c.get("/key", headers=H).text
    assert '<details class="help">' in t and "Key not accepted? Common fixes" in t
    script(https_env, error(402, "X"))
    r = save(https_env)
    assert '<details class="help" open>' in r.text and BILLING in r.text


def test_a_saved_key_shows_no_walkthrough(https_env):
    script(https_env, OK)
    t = save(https_env).text
    assert "Get your free key in 3 steps" not in t and 'id="key-paste"' not in t
    assert t.count("<script>") == 2  # only the day/night switch's two; no page script
