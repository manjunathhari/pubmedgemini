import logging

import pytest


def test_no_email_in_logs(client_prod, caplog):
    caplog.set_level(logging.DEBUG)
    client_prod.get("/", headers={"x-forwarded-email": "stranger@example.com"})
    client_prod.get("/", headers={"x-forwarded-email": "owner@example.com"})
    assert "stranger@example.com" not in caplog.text and "owner@example.com" not in caplog.text


def test_one_request_line_has_method_route_and_status_only(client_prod, caplog):
    caplog.set_level(logging.INFO, logger="pubmedgemini.web")
    client_prod.get("/", headers={"x-forwarded-email": "owner@example.com"})
    lines = [r.getMessage() for r in caplog.records if r.name == "pubmedgemini.web"]
    assert lines == ["request method=GET route=/ status=200"]


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_production_stderr_has_request_lines_and_no_connection_details():
    """Run the real server as in the container: only the app's own fixed lines reach stderr."""
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx

    root = Path(__file__).resolve().parents[1]
    port = _free_port()
    env = os.environ | {
        "APP_ENV": "prod",
        # unreachable on purpose: a made-up role on a loopback address with nothing listening
        "DATABASE_URL": "postgresql://madeuprole_q7:pw@127.0.0.2:59999/x",
        "OWNER_EMAIL": "owner-q7@example.com",
        "PYTHONPATH": str(root / "src"),
    }
    cmd = [sys.executable, "-m", "uvicorn", "pubmedgemini.main:app",
           "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
    proc = subprocess.Popen(
        cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                health = httpx.get(f"{base}/healthz", timeout=10)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.2)
        page = httpx.get(f"{base}/", headers={"x-forwarded-email": "visitor-q7@example.com"})
        assert (health.status_code, page.status_code) == (503, 503)
        time.sleep(1)  # let the pool's background reconnect run once more
    finally:
        proc.terminate()
        _, err = proc.communicate(timeout=10)
    assert "request method=GET route=/healthz status=503" in err
    assert "request method=GET route=/ status=503" in err
    assert "kind=db_unreachable" in err
    leaks = ("madeuprole_q7", "127.0.0.2", "59999", "@example.com",
             "error connecting", "connection to server", "Traceback")
    for leak in leaks:
        assert leak not in err, leak


def test_unexpected_error_under_uvicorn_prints_no_traceback_or_search_text(db_url):
    """A route that fails unexpectedly, under the real server: one fixed line, and neither
    the app nor Uvicorn's error logger prints a traceback or the search text."""
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx

    root = Path(__file__).resolve().parents[1]
    port = _free_port()
    env = os.environ | {
        "APP_ENV": "dev",
        "DATABASE_URL": db_url,
        "OWNER_EMAIL": "owner@example.com",
        "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")]),
    }
    cmd = [sys.executable, "-m", "uvicorn", "--factory", "crash_app:build",
           "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
    proc = subprocess.Popen(
        cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                health = httpx.get(f"{base}/healthz", timeout=10)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.2)
        r = httpx.post(f"{base}/search", data={"q": "zq7 hernia"},
                       headers={"x-dev-user-email": "owner@example.com"}, timeout=30)
        assert (health.status_code, r.status_code) == (200, 500)
        assert "Something went wrong. Please try again in a minute." in r.text
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
    assert err.count("kind=internal error=RuntimeError") == 1, err
    assert "request method=POST route=/search status=500" in err
    for leak in ("zq7", "hernia", "Traceback", "eutils", "ebi.ac.uk", "unexpected failure",
                 "Exception in ASGI"):
        assert leak not in err + out, leak


def test_configuring_logging_twice_adds_one_handler(settings_prod):
    import logging as std_logging

    from pubmedgemini.main import create_app

    create_app(settings_prod)
    create_app(settings_prod)
    app_log = std_logging.getLogger("pubmedgemini")
    assert app_log.level == std_logging.INFO and len(app_log.handlers) == 1
    for name in ("psycopg", "psycopg.pool"):
        assert std_logging.getLogger(name).getEffectiveLevel() > std_logging.CRITICAL


def test_httpx_loggers_are_silenced(settings_prod):  # Review Focus 2
    from pubmedgemini.main import create_app

    create_app(settings_prod)
    # httpx logs every request URL (with its query string, the NCBI key included) at INFO.
    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        assert logging.getLogger(name).getEffectiveLevel() > logging.CRITICAL, name
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).propagate is False, name


H = {"x-dev-user-email": "owner@example.com"}


def test_search_text_and_key_never_logged(db_url, upstream, caplog):  # Review Focus 2
    import re

    from conftest import search_client

    caplog.set_level(logging.DEBUG)  # root: every logger that propagates
    client = search_client(db_url, upstream, ncbi_api_key="key-q7",
                           ncbi_email="dev-q7@example.org")
    # create_app pins the app's logger at INFO; open it to DEBUG so a debug-level leak in
    # the app's own code is caught too (review finding H).
    app_log = logging.getLogger("pubmedgemini")
    pinned = app_log.level
    app_log.setLevel(logging.DEBUG)
    try:
        with client as c:
            r = c.post("/search", data={"q": "zq7 hernia mesh?"}, headers=H)
    finally:
        app_log.setLevel(pinned)
    assert r.status_code == 200
    assert upstream.sent[0].url.params["api_key"] == "key-q7"  # the key is used, never shown
    for leak in ("zq7", "key-q7", "dev-q7", "Mesh repair", "hernia", "eutils", "ebi.ac.uk",
                 "https://", "BACKGROUND"):
        assert leak not in caplog.text, leak
    messages = [r.getMessage() for r in caplog.records]
    assert "literature ncbi_api_key=true" in messages
    lines = [m for m in messages if m.startswith(("search ", "request "))]
    assert lines[-1] == "request method=POST route=/search status=200"
    assert re.fullmatch(
        r"search kind=ok pubmed=\d+ europepmc=\d+ shown=\d+ abstracts=\d+ cached=\d+", lines[-2]
    ), lines[-2]


def test_start_up_says_no_key_when_unset(db_url, caplog):
    from conftest import search_client
    from lit_fakes import FakeUpstream

    caplog.set_level(logging.INFO)
    with search_client(db_url, FakeUpstream({})):
        pass
    assert "literature ncbi_api_key=false" in [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize(
    ("override", "line"),
    [
        ({"epmc": 503}, r"search kind=partial down=europepmc pubmed=\d+ shown=\d+ abstracts=\d+ cached=\d+"),
        ({"esearch": 503}, r"search kind=partial down=pubmed europepmc=\d+ shown=\d+ abstracts=\d+ cached=\d+"),
        ({"epmc": 503, "esearch": 503}, r"search kind=unavailable"),
        ({"q": 'PUB_TYPE:"review"'}, r"search kind=partial not_run=pubmed europepmc=\d+ shown=\d+ abstracts=\d+ cached=\d+"),
        ({"q": "hernia; zq7"}, r"search kind=rejected reason=character"),
        ({"empty": True}, r"search kind=none pubmed=0 europepmc=0"),
    ],
)
def test_each_search_logs_one_fixed_line(db_url, caplog, override, line):
    import re

    import httpx
    from conftest import search_client
    from lit_fakes import (
        EPMC_HOST,
        EPMC_SEARCH_PATH,
        EUTILS_HOST,
        EUTILS_PATH,
        FakeUpstream,
        europepmc_routes,
        pubmed_routes,
    )

    routes = pubmed_routes() | europepmc_routes()
    if "epmc" in override:
        routes[(EPMC_HOST, EPMC_SEARCH_PATH)] = httpx.Response(override["epmc"])
    if "esearch" in override:
        routes[(EUTILS_HOST, EUTILS_PATH + "esearch.fcgi")] = httpx.Response(override["esearch"])
    if "empty" in override:
        routes[(EUTILS_HOST, EUTILS_PATH + "esearch.fcgi")] = httpx.Response(
            200, json={"esearchresult": {"count": "0", "idlist": []}})
        routes[(EPMC_HOST, EPMC_SEARCH_PATH)] = httpx.Response(
            200, json={"hitCount": 0, "resultList": {"result": []}})
    caplog.set_level(logging.INFO)
    with search_client(db_url, FakeUpstream(routes)) as c:
        c.post("/search", data={"q": override.get("q", "mesh")}, headers=H)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("search ")]
    assert len(lines) == 1 and re.fullmatch(line, lines[0]), lines
    assert "zq7" not in caplog.text


# ---------- questions (F2) ----------

# The fixed-kind `ask` lines and their exact form: names, counts and ids only. Task 10 adds
# the other `ask kind=` lines here and calls `assert_ask_lines_fixed` after each scenario.
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ASK_LINE_PATTERNS = {
    "blocked_identifiers": r"ask kind=blocked_identifiers categories=[1-9] [a-z_]+=[0-9]+"
                           r"(?: [a-z_]+=[0-9]+)*",
    "started": rf"ask kind=started run_id={UUID}",
    "rejected": r"ask kind=rejected reason=(?:empty|too_long)",
    "refused": r"ask kind=refused reason=(?:no_key|active|daily_limit)",
    "refused_cross_site": r"ask kind=refused_cross_site",
    "start_failed": r"ask kind=start_failed error=[A-Za-z_]+",
}


def _ask_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("ask ")]


def assert_ask_lines_fixed(caplog) -> None:
    """Every `ask ` line logged so far has a known kind and that kind's exact form."""
    import re

    for line in _ask_lines(caplog):
        kind = line.split()[1].removeprefix("kind=")
        assert kind in ASK_LINE_PATTERNS and re.fullmatch(ASK_LINE_PATTERNS[kind], line), line


def test_a_blocked_question_logs_categories_and_counts_only(migrated_pool, caplog):
    import re

    from pubmedgemini.identity import ensure_owner
    from pubmedgemini.runs import QuestionBlocked, create_run

    ensure_owner(migrated_pool, "owner@example.com")
    with migrated_pool.connection() as conn:
        uid = conn.execute("SELECT id FROM users").fetchone()[0]
    caplog.set_level(logging.DEBUG)
    app_log = logging.getLogger("pubmedgemini")
    pinned = app_log.level
    app_log.setLevel(logging.DEBUG)
    try:
        with pytest.raises(QuestionBlocked):
            create_run(migrated_pool, uid,
                       "zq7 Mrs. Lakshmi ph 9845012345 and 9845012346 hernia?", "m")
    finally:
        app_log.setLevel(pinned)
    assert _ask_lines(caplog) == [
        "ask kind=blocked_identifiers categories=2 phone=2 person_name=1"]
    assert re.fullmatch(ASK_LINE_PATTERNS["blocked_identifiers"], _ask_lines(caplog)[0])
    assert_ask_lines_fixed(caplog)
    for leak in ("zq7", "Lakshmi", "9845012345", "9845012346", "hernia", "Mrs"):
        assert leak not in caplog.text, leak


# ---------- a whole question, through the real routes (Task 10) ----------

LEAK_QUESTION = "zq7 does mesh reduce recurrence after inguinal hernia repair?"
LEAK_ID1, LEAK_ID2, LEAK_ID3 = "36262096", "33763744", "31696731"


def leak_script() -> list:
    """A full run (8 turns at the cap, then the list) whose every model-written text carries a
    marker: the model's note (yq8), a query (xq9), a refused query (vq3), the why (wq6), a
    reason (vq5), a contradiction note (uq4), and Google's error message (gm-q7) on a 503
    that is retried. Tool results carry the fixtures' titles and abstracts."""
    import json

    from gemini_fakes import call, error, reply

    def turn(*parts):
        return reply(list(parts))

    q = {"query": "xq9[tiab]", "max_results": 3}
    first = turn({"text": "yq8 my reasoning"}, call("search_pubmed", q),
                 call("search_europepmc", {"query": "xq9 hernia", "max_results": 3}),
                 call("search_pubmed", {"query": "vq3 hernia?", "max_results": 3}))
    second = turn(call("get_abstracts", {"pmids": [LEAK_ID1, LEAK_ID2]}),
                  call("find_related", {"pmid": LEAK_ID1, "max_results": 3}))
    listed = reply([{"text": json.dumps({
        "searches": [{"query": "tq2 model reported", "source": "PubMed", "hits": 1}],
        "selected": [{"pmid": LEAK_ID2, "design": "SR_MA", "why": "wq6 because"},
                     {"pmid": LEAK_ID1, "design": "RCT", "why": "wq6 trial"}],
        "considered_not_selected": [{"pmid": LEAK_ID3, "reason": "vq5 older"}],
        "possible_contradictions": [{"pmid": LEAK_ID3, "note": "uq4 contra"}]})}])
    return ([first, error(503, "UNAVAILABLE", message="gm-q7 secret text"), second]
            + [turn(call("search_pubmed", q))] * 6 + [listed])


# Every line the app's own loggers may write: fixed words, names, counts, ids and fixed
# kinds. A line that does not match one of these (free text of any kind) fails the tests.
K = r"[a-z_]+"
N = r"-?\d+"
APP_LINE_PATTERNS = [
    *ASK_LINE_PATTERNS.values(),
    r"literature ncbi_api_key=(?:true|false)",
    r"gemini api_key=(?:true|false) model=[a-z0-9.-]+ rpm=\d+ tpm=\d+ rpd=\d+",
    r"paper_cache pruned=\d+",
    r"paper_cache kind=prune_failed error=\w+ sqlstate=\w+",
    r"runs stopped_at_start=\d+",
    r"kind=(?:db_unreachable|migration_failed) error=\w+ sqlstate=\w+",
    r"kind=db_error",
    r"kind=internal error=\w+",
    r"request method=[A-Z]+ route=(?:[/a-z_{}]+|unmatched) status=\d{3}",
    rf"search kind={K}(?: {K}={K})?(?: {K}=\d+)*",
    rf"run kind=running run_id={UUID} model=[a-z0-9.-]+",
    rf"run kind=finished run_id={UUID} status={K} error_kind={K}",
    r"run kind=(?:internal|search_not_stored|seen_not_stored) error=\w+",
    rf"run kind=(?:not_found|not_finished) run_id={UUID}",
    r"run kind=step_not_stored",
    rf"run kind=records_not_fetched error_kind={K}",
    (rf"gemini step={K} attempt=[1-3] status={K} finish={K} prompt_tokens=\d+ "
    r"output_tokens=\d+ thought_tokens=\d+ latency_ms=\d+"),
    r"gemini kind=count_not_finished",
    rf"lit tool={K} (?:count=\d+ items=\d+|asked=\d+ cached=\d+ returned=\d+|error_kind={K})",
    r"lit kind=cache_(?:read|write)_failed",
    rf"researcher turn=\d+ tool={K} status={K} chars=\d+",
    rf"researcher turn=\d+ finish={K} calls=\d+ run=\d+ errors=\d+ cut=\d+ withheld=\d+",
    rf"researcher loop_done stop={K} turns=\d+ tool_calls=\d+ result_chars=\d+",
    (rf"researcher done stop={K} rounds=\d+ searches=\d+ seen=\d+ reported=\d+ kept=\d+ "
    r"rcts=\d+ meta_analyses=\d+ dropped_unseen=\d+ duplicates=\d+ over_cap=\d+ considered=\d+"),
    r"researcher kind=hard_stop_between_turns turns=\d+",
    r"researcher kind=hard_stop_between_tool_calls turn=\d+",
    rf"researcher {K} kind=refused finish={K}",
    rf"researcher study_list unreadable category={K} step={K}",
    rf"researcher study_list kind=not_completed error={K}",
    rf"researcher s3 kind=not_made_near_hard_stop left_s={N}",
    rf"researcher s3 kind=failed error={K}",
    r"researcher kind=final_failed rounds=\d+",
    r"researcher s3 kind=empty_list seen=\d+ reported=\d+ considered=\d+ rounds=\d+",
    r"researcher s3 outcome=usable reported=\d+ considered=\d+",
    r"researcher s3 outcome=(?:empty|not_made) seen=\d+ rounds=\d+",
    # F3: the writer and the stored answer
    (r"writer part=(?:trials|outcomes|context|summary|one_piece) attempt=(?:first|retry) "
     r"outcome=(?:ok|truncated|unreadable|call_failed|no_time) "
     r"category=(?:syntax|shape|truncated|none) output_tokens=\d+"),
    r"writer parts_failed parts=(?:trials|outcomes|context|summary)(?:,[a-z]+)*",
    (r"writer done merged=(?:true|false) outcomes=\d+ numbers=\d+ studies=\d+ "
     r"labels_defaulted=\d+"),
    rf"answer stored run_id={UUID} bytes=\d+ written=(?:parts|one_piece|none)",
    rf"answer kind=not_stored run_id={UUID}",
    rf"answer kind=unreadable run_id={UUID}",
    rf"run kind=writer_no_time run_id={UUID} left_s={N}",
]

# What must never reach a log line: the question (zq7 and its words), the model's note (yq8),
# its queries (xq9, and the refused vq3), the why (wq6), a reason (vq5), a contradiction note
# (uq4), the searches the model reported (tq2), Google's error message (gm-q7), the Gemini key
# (gk-q7), the NCBI key and email (nk-q7, nq7), tool results (fixture titles and abstracts,
# the source wrapper, tool error texts) and any URL or host.
LEAKS = ("zq7", "recurrence", "inguinal", "yq8", "xq9", "vq3", "wq6", "vq5", "uq4", "tq2",
         "gm-q7", "secret text", "gk-q7", "nk-q7", "nq7", "Mesh repair", "<script",
         "BACKGROUND", "<source", "source id", "query rejected", "Error (input)", "https://",
         "generativelanguage", "googleapis", "eutils", "ebi.ac.uk")


def assert_app_lines_fixed(messages) -> None:
    import re

    for m in messages:
        assert any(re.fullmatch(p, m) for p in APP_LINE_PATTERNS), m


def _app_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name.startswith("pubmedgemini")]


@pytest.fixture
def app_debug():
    """create_app pins the app's logger at INFO; tests call this after it to open DEBUG."""
    app_log = logging.getLogger("pubmedgemini")
    pinned = app_log.level

    def open_debug():
        app_log.setLevel(logging.DEBUG)

    yield open_debug
    app_log.setLevel(pinned)


def test_nothing_secret_or_private_is_logged_in_a_full_run(db_url, caplog, app_debug):
    import re

    from test_ask_page import H, ask_env

    caplog.set_level(logging.DEBUG)  # root: every logger that propagates
    with ask_env(db_url, None, leak_script(), key="gk-q7", ncbi_api_key="nk-q7",
                 ncbi_email="nq7@example.org") as env:
        app_debug()
        r = env.ask(LEAK_QUESTION)
        page = env.run_page(r.headers["location"]).text
        home = env.c.get("/", headers=H).text
        # the other ways a question is refused, each with its fixed line
        env.ask("   ")
        env.ask("zq7 " * 1001)
        env.ask("zq7 Mrs. Lakshmi ph 9845012345 inguinal?")
        env.ask("zq7 inguinal?", headers=H | {"sec-fetch-site": "cross-site"})
        env.run_page("/runs/zq7-inguinal")
        status = env.one("SELECT status FROM runs")
        sent = env.gemini.bodies
    # The marked texts were really in the app: Google got the question, the page shows them.
    assert status == "done" and "zq7" in sent[0]["contents"][0]["parts"][0]["text"]
    for shown in ("Answer ready", "wq6 because", "vq5 older", "uq4 contra", "xq9[tiab]"):
        assert shown in page, shown
    assert "zq7 does mesh" in home
    for leak in LEAKS:
        assert leak not in caplog.text, leak
    msgs = _app_messages(caplog)
    assert_app_lines_fixed(msgs)
    assert_ask_lines_fixed(caplog)
    assert any(re.fullmatch(r"gemini step=researcher attempt=1 status=ok finish=stop "
                            r"prompt_tokens=\d+ output_tokens=\d+ thought_tokens=\d+ "
                            r"latency_ms=\d+", m) for m in msgs)
    assert any(m.startswith("gemini step=researcher attempt=1 status=overloaded ") for m in msgs)
    assert any(m.startswith("researcher done stop=turn_cap rounds=8 ") for m in msgs)
    assert "gemini api_key=true model=gemini-3.5-flash-lite rpm=1000 tpm=250000 rpd=500" in msgs
    kinds = [m.split()[1] for m in msgs if m.startswith("ask ")]
    assert kinds == ["kind=started", "kind=rejected", "kind=rejected",
                     "kind=blocked_identifiers", "kind=refused_cross_site"]
    assert sum(m.startswith("run kind=finished ") for m in msgs) == 1


def test_failed_run_logs_kinds_only(db_url, caplog, app_debug):
    from gemini_fakes import error
    from test_ask_page import ask_env

    caplog.set_level(logging.DEBUG)
    script = [error(402, "X", message="gm-q7 secret text prepayment credits are depleted")]
    with ask_env(db_url, None, script, key="gk-q7") as env:
        app_debug()
        r = env.ask(LEAK_QUESTION)
        page = env.run_page(r.headers["location"]).text
    assert "linked to billing" in page
    msgs = _app_messages(caplog)
    assert_app_lines_fixed(msgs)
    assert any(" status=billing_refused " in m for m in msgs)
    assert any(m.endswith(" status=failed error_kind=billing_refused") for m in msgs)
    for leak in (*LEAKS, "prepayment", "depleted"):
        assert leak not in caplog.text, leak


def test_question_refusals_log_fixed_lines(db_url, caplog, app_debug):
    from test_ask_page import FailingStarter, HoldStarter, ask_env

    caplog.set_level(logging.DEBUG)
    with ask_env(db_url, None, key=None) as env:
        app_debug()
        env.ask(LEAK_QUESTION)
    with ask_env(db_url, None, starter=HoldStarter()) as env:
        app_debug()
        env.ask(LEAK_QUESTION)
        env.ask(LEAK_QUESTION)
    with ask_env(db_url, None, starter=FailingStarter()) as env:
        with env.pool.connection() as conn:
            conn.execute("UPDATE runs SET status = 'done' WHERE status = 'queued'")
        app_debug()
        env.ask(LEAK_QUESTION)
    with ask_env(db_url, None, gemini_rpd=23) as env:  # under a run's cap of 24: none left
        app_debug()
        env.ask(LEAK_QUESTION)
    assert_ask_lines_fixed(caplog)
    assert_app_lines_fixed(_app_messages(caplog))
    lines = [m for m in _app_messages(caplog) if m.startswith("ask ")]
    assert [m.split(" run_id=")[0] for m in lines] == [
        "ask kind=refused reason=no_key", "ask kind=started", "ask kind=refused reason=active",
        "ask kind=started", "ask kind=start_failed error=RuntimeError",
        "ask kind=refused reason=daily_limit"]
    for leak in LEAKS:
        assert leak not in caplog.text, leak


def test_no_model_text_in_any_table_but_the_reviewed_ones(migrated_pool):
    """Every text column in the schema, reviewed: model_calls holds none of the model's
    text; the question lives only in runs.question; the model's notes only in the clipped
    run_papers.note and the step lines; queries only in run_searches; the written answer only
    in answers.body (F3; `answers.written` is a fixed word)."""
    with migrated_pool.connection() as conn:
        cols = {(t, c) for t, c in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND data_type IN ('text', 'character varying', "
            "'jsonb', 'json')")}
    assert {c for t, c in cols if t == "model_calls"} == {"model", "step", "status_kind"}
    assert cols == {
        ("model_calls", "model"), ("model_calls", "step"), ("model_calls", "status_kind"),
        ("users", "email"), ("users", "role"),
        ("schema_migrations", "name"),
        ("papers", "pmid"), ("papers", "pmcid"), ("papers", "doi"), ("papers", "title"),
        ("papers", "journal"), ("papers", "design"), ("papers", "abstract"),
        ("runs", "question"), ("runs", "mode"), ("runs", "status"), ("runs", "error_kind"),
        ("runs", "stop_reason"), ("runs", "model"),
        ("run_steps", "kind"), ("run_steps", "line"),
        ("run_searches", "source"), ("run_searches", "query"),
        ("run_seen", "pmid"), ("run_seen", "title"), ("run_seen", "journal"),
        ("run_papers", "role"), ("run_papers", "pmid"), ("run_papers", "design"),
        ("run_papers", "note"),
        ("answers", "body"), ("answers", "written"),
    }, sorted(cols)


# ---------- the same, under a real Uvicorn (as the container runs it) ----------

UVICORN_LINES = [
    r"INFO:     Started server process \[\d+\]",
    r"INFO:     Waiting for application startup\.",
    r"INFO:     Application startup complete\.",
    r"INFO:     Uvicorn running on http://127\.0\.0\.1:\d+ \(Press CTRL\+C to quit\)",
    r"INFO:     Shutting down",
    r"INFO:     Waiting for application shutdown\.",
    r"INFO:     Application shutdown complete\.",
    r"INFO:     Finished server process \[\d+\]",
]
APP_LINE = (r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (?:INFO|WARNING|ERROR) "
            r"pubmedgemini(?:\.[a-z_.]+)? (?P<msg>.*)")


def _run_question_under_uvicorn(db_url: str, mode: str) -> tuple[str, str, str]:
    """Starts `ask_app:build` under Uvicorn, asks LEAK_QUESTION, waits for the run page to
    stop refreshing, opens the home page, stops the server. Returns (run page, home, stderr)."""
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    import httpx

    root = Path(__file__).resolve().parents[1]
    port = _free_port()
    env = os.environ | {
        "APP_ENV": "dev",
        "DATABASE_URL": db_url,
        "OWNER_EMAIL": "owner@example.com",
        "GEMINI_API_KEY": "gk-q7",
        "GEMINI_RPM": "1000",
        "NCBI_API_KEY": "nk-q7",
        "NCBI_EMAIL": "nq7@example.org",
        "ASK_APP_MODE": mode,
        "PYTHONPATH": os.pathsep.join([str(root / "src"), str(root / "tests")]),
    }
    cmd = [sys.executable, "-m", "uvicorn", "--factory", "ask_app:build",
           "--host", "127.0.0.1", "--port", str(port), "--no-access-log"]
    proc = subprocess.Popen(
        cmd, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    base = f"http://127.0.0.1:{port}"
    h = {"x-dev-user-email": "owner@example.com"}
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                health = httpx.get(f"{base}/healthz", timeout=10)
                break
            except httpx.TransportError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.2)
        assert health.status_code == 200
        r = httpx.post(f"{base}/ask", data={"question": LEAK_QUESTION}, headers=h, timeout=30)
        assert r.status_code == 303, r.status_code
        page_url = base + r.headers["location"]
        deadline = time.monotonic() + 30
        while True:
            page = httpx.get(page_url, headers=h, timeout=10).text
            if 'http-equiv="refresh"' not in page:
                break
            assert time.monotonic() < deadline, "the run did not finish"
            time.sleep(0.2)
        home = httpx.get(f"{base}/", headers=h, timeout=10).text
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
    return page, home, err + out


def _assert_stderr_fixed(err: str) -> list[str]:
    """Every line is one of Uvicorn's own start/stop lines or an app line of a fixed form;
    returns the app messages."""
    import re

    msgs = []
    for line in err.splitlines():
        if any(re.fullmatch(p, line) for p in UVICORN_LINES):
            continue
        m = re.fullmatch(APP_LINE, line)
        assert m, line
        msgs.append(m.group("msg"))
    assert_app_lines_fixed(msgs)
    return msgs


def test_a_full_run_under_uvicorn_logs_fixed_lines_only(db_url):
    page, home, err = _run_question_under_uvicorn(db_url, "happy")
    assert "Answer ready" in page and "wq6 because" in page and "zq7 does mesh" in home
    msgs = _assert_stderr_fixed(err)
    for leak in (*LEAKS, "Traceback", "Exception in"):
        assert leak not in err, leak
    assert sum(m.startswith("run kind=finished ") and m.endswith(" status=done error_kind=none")
               for m in msgs) == 1
    assert sum(m.startswith("ask kind=started run_id=") for m in msgs) == 1
    assert "request method=POST route=/ask status=303" in msgs
    assert "request method=GET route=/runs/{run_id} status=200" in msgs


def test_unexpected_error_in_a_run_under_uvicorn_is_one_fixed_line(db_url):
    """The first Gemini request raises an unexpected error whose text holds the request body
    (so the question): one fixed line, the run failed with the plain message, no traceback."""
    page, _home, err = _run_question_under_uvicorn(db_url, "crash")
    assert "Something went wrong. Please try again in a minute." in page
    msgs = _assert_stderr_fixed(err)
    assert err.count("run kind=internal error=RuntimeError") == 1, err
    assert sum(m.endswith(" status=done error_kind=none") for m in msgs) == 0
    for leak in (*LEAKS, "Traceback", "Exception in", "unexpected failure", "contents"):
        assert leak not in err, leak
