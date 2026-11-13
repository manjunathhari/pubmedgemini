import logging
from contextlib import contextmanager

import httpx
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


def leak_parts() -> dict:
    """The writer's four parts for the leak runs: every model-written text carries mq2."""
    from writer_fakes import good_parts, whole_answer

    return good_parts(whole_answer("mq2 written"), copies=2)


# Every line the app's own loggers may write: fixed words, names, counts, ids and fixed
# kinds. A line that does not match one of these (free text of any kind) fails the tests.
K = r"[a-z_]+"
N = r"-?\d+"
# F3a: the writer's parts (writer/parts.py), the cap, and the runner after the writer.
WRITER_LINE_PATTERNS = [
    (r"writer part=(?:trials|outcomes|context|summary|one_piece) attempt=(?:first|retry) "
     rf"result=(?:ok|cut_off|unreadable|refused|error|skipped) kind={K} output_tokens=\d+"),
    r"writer kind=merged labels_defaulted=\d+",
    rf"writer kind=parts_failed failed=\d+ first={K}",
    r"writer kind=no_time_for_one_piece left_s=\d+",
    r"writer kind=callback_failed error=\w+",
    rf"gemini kind=run_call_cap step={K} attempts=\d+ cap=\d+",
    (rf"run kind=answer run_id={UUID} written=(?:true|false) references=\d+ "
     r"dropped_unseen=\d+ dropped_unfetched=\d+ removed_citations=\d+ dropped_numbers=\d+ "
     r"dropped_studies=\d+"),
    r"run kind=no_time_for_writer left_s=\d+",
    r"run kind=answer_not_stored error=\w+",
    rf"run kind=finish_not_applied run_id={UUID}",
]

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
    *WRITER_LINE_PATTERNS,
    r"answer_page kind=row_not_derived rows=\d+",  # F3b: the answer page's one line
    # F3c: the Introduction and the early summary (fixed words, ids, kinds and counts).
    rf"run kind=intro run_id={UUID} status=(?:done|failed) reason={K} output_tokens=\d+",
    rf"run kind=intro_not_started run_id={UUID} error=\w+",
    (rf"run kind=early_stored run_id={UUID} summary=(?:true|false) context=(?:true|false) "
     r"bytes=\d+"),
    rf"run kind=early_withdrawn run_id={UUID}",
    rf"run kind=early_not_stored run_id={UUID} error=\w+",
    r"run page early kind=(?:not_quick|invalid|unserialisable|internal)",
    r"run page answer kind=(?:not_quick|invalid|unserialisable)",
]

# What must never reach a log line: the question (zq7 and its words), the model's note (yq8),
# its queries (xq9, and the refused vq3), the why (wq6), a reason (vq5), a contradiction note
# (uq4), the searches the model reported (tq2), Google's error message (gm-q7), the Gemini key
# (gk-q7), the NCBI key and email (nk-q7, nq7), tool results (fixture titles and abstracts,
# the source wrapper, tool error texts) and any URL or host.
LEAKS = ("zq7", "recurrence", "inguinal", "yq8", "xq9", "vq3", "wq6", "vq5", "uq4", "tq2", "mq2",
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
                 ncbi_email="nq7@example.org", parts=leak_parts()) as env:
        app_debug()
        r = env.ask(LEAK_QUESTION)
        page = env.run_page(r.headers["location"]).text
        home = env.c.get("/library", headers=H).text  # the list of questions is on Library
        # the other ways a question is refused, each with its fixed line
        env.ask("   ")
        env.ask("zq7 " * 1001)
        env.ask("zq7 Mrs. Lakshmi ph 9845012345 inguinal?")
        env.ask("zq7 inguinal?", headers=H | {"sec-fetch-site": "cross-site"})
        env.run_page("/runs/zq7-inguinal")
        status = env.one("SELECT status FROM runs")
        sent = env.gemini.bodies
        stored = env.one("SELECT body::text FROM answers")
        writer_sent = env.gemini.writer.bodies()
    # The marked texts were really in the app: Google got the question, the page shows them,
    # the writer got the question and the abstracts, and the stored answer holds its text.
    assert status == "done" and "zq7" in sent[0]["contents"][0]["parts"][0]["text"]
    assert len(writer_sent) == 4 and all(
        "zq7" in b["contents"][0]["parts"][0]["text"] for b in writer_sent)
    assert "mq2 written" in stored and "zq7" in stored
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
    assert any(re.fullmatch(r"writer part=outcomes attempt=first result=ok kind=none "
                            r"output_tokens=\d+", m) for m in msgs)
    assert any(m.startswith("run kind=answer run_id=") and " written=true " in m for m in msgs)
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
    with ask_env(db_url, None, gemini_rpd=13) as env:  # 13 < RUN_ATTEMPTS (26): 0 runs left
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
    run_papers.note and the step lines; queries only in run_searches; the written answer
    only in answers.body; the Introduction only in run_intros.text and the early summary
    only in run_early.body (spec section 4: Postgres only, never a log line)."""
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
        ("answers", "body"),
        # F3c: the Introduction's fixed status and reason, its text, and the early summary.
        ("run_intros", "status"), ("run_intros", "reason"), ("run_intros", "text"),
        ("run_early", "body"),
        # F4a: the completeness check's fixed status and reason (counts only, no text).
        ("run_checks", "status"), ("run_checks", "reason"),
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
    stop refreshing, opens the Library page, stops the server. Returns (run page, home, stderr)."""
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
        home = httpx.get(f"{base}/library", headers=h, timeout=10).text
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
    assert "writer kind=merged labels_defaulted=0" in msgs  # the run wrote its answer
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


# ---------- the writer's texts: every one marked, none may reach a log (F3a Task 9) ----------

# One marker per kind of writer text. None is a hex string, so none can sit inside a run id.
PART_TAGS = {"trials": "pt1", "outcomes": "po2", "context": "pc3", "summary": "ps4"}
PIECE_TAG = "pw5"      # the one-piece reply's text
BLOCK_NOTE = "bm6"     # the message inside a reply Gemini blocked
ABSTRACT_TAG = "qa7"   # put into the abstract of a selected study
REFUSED_TAG = "zd8"    # in an answer the database refuses (with a NUL, which JSONB cannot hold)
WRITER_LEAKS = (*PART_TAGS.values(), PIECE_TAG, BLOCK_NOTE, ABSTRACT_TAG, REFUSED_TAG)
# F3c: the Introduction's text, Google's message on its failed call, and a blocked reply's note.
INTRO_TAG = "iq3"
INTRO_ERROR_TAG = "gi4"
INTRO_BLOCK_TAG = "bi5"
INTRO_LEAKS = (INTRO_TAG, INTRO_ERROR_TAG, INTRO_BLOCK_TAG)
ABSTRACT_SENTENCE = b"There was no difference between the two methods."


def marked_upstream():
    """The F1 fixtures with the marker in one abstract of a selected study."""
    from lit_fakes import (
        EUTILS_HOST,
        EUTILS_PATH,
        FakeUpstream,
        europepmc_routes,
        fixture,
        pubmed_routes,
    )

    efetch = fixture("efetch.xml")
    assert ABSTRACT_SENTENCE in efetch
    routes = pubmed_routes() | europepmc_routes()
    routes[(EUTILS_HOST, EUTILS_PATH + "efetch.fcgi")] = httpx.Response(
        200, content=efetch.replace(ABSTRACT_SENTENCE, ABSTRACT_TAG.encode() + b" abstract text"),
        headers={"content-type": "text/xml"})
    return FakeUpstream(routes)


def marked_parts() -> dict:
    """Each part answers with its own marker in its text."""
    from writer_fakes import part_reply, whole_answer

    from pubmedgemini.writer.prompts import Section

    return {s.name: [part_reply(s, whole_answer(PART_TAGS[s.name]))] * 2 for s in Section.ALL}


def blocked_reply() -> dict:
    return {"promptFeedback": {"blockReason": "SAFETY", "blockReasonMessage": BLOCK_NOTE}}


def piece_after_a_blocked_part() -> tuple[dict, list]:
    """The trials part is blocked twice (the message carries a marker): the one piece is
    written, and its text carries another."""
    from writer_fakes import piece_reply, whole_answer

    parts = marked_parts()
    parts["trials"] = [blocked_reply(), blocked_reply()]
    return parts, [piece_reply(whole_answer(PIECE_TAG))]


def refused_by_the_database() -> dict:
    """The summary part's text holds a NUL: the whole answer is valid to read and to check, and
    the database refuses it (JSONB has no NUL), with an error text that holds the body. Since
    the F3c review the body is cleaned of NUL before its INSERT (`runs.scrub_json`), so the
    refusal is reached only with that cleaning turned off (`without_store_cleaning`): these
    tests keep checking that a refused answer logs nothing of its text."""
    from writer_fakes import part_reply, whole_answer

    from pubmedgemini.writer.prompts import Section

    parts = marked_parts()
    parts["summary"] = [part_reply(Section.SUMMARY, whole_answer(f"{REFUSED_TAG}\u0000"))]
    return parts


def without_store_cleaning(monkeypatch=None) -> None:
    """Turns off the cleaning of stored bodies (`runs.scrub_json`), so a NUL in model text
    reaches Postgres and is refused (the `db_refused` scenario)."""
    import pubmedgemini.runs as runs_mod

    if monkeypatch is None:  # tests/ask_app.py: a process of its own
        runs_mod.scrub_json = lambda v: v
    else:
        monkeypatch.setattr(runs_mod, "scrub_json", lambda v: v)


def marked_intro() -> list:
    from writer_fakes import intro_reply

    return [intro_reply(f"{INTRO_TAG} what the question asks.\n\n{INTRO_TAG} the disease.")]


def failing_intro() -> list:
    """The Introduction's one attempt fails with Google's message carrying a marker."""
    from gemini_fakes import error

    return [error(503, "UNAVAILABLE", message=f"{INTRO_ERROR_TAG} secret intro text")]


def intro_steps(mode: str) -> list:
    """The Introduction's replies for `tests/ask_app.py` (marked, or one that fails)."""
    return failing_intro() if mode == "intro_fails" else marked_intro()


def scenario(mode: str) -> tuple[list, dict, list]:
    """(researcher script, parts, one piece) of a whole run for `tests/ask_app.py`."""
    if mode == "fallback":
        parts, piece = piece_after_a_blocked_part()
        return leak_script(), parts, piece
    if mode == "db_refused":
        return leak_script(), refused_by_the_database(), []
    return leak_script(), marked_parts(), []


def assert_no_private_text(caplog, extra=()) -> None:
    """No captured line, formatted record, exception text or traceback holds a marker."""
    for leak in (*LEAKS, *WRITER_LEAKS, *INTRO_LEAKS, *extra):
        assert leak not in caplog.text, leak
    for r in caplog.records:
        assert r.exc_info is None and r.exc_text is None, (r.name, r.getMessage())
        for leak in (*LEAKS, *WRITER_LEAKS, *INTRO_LEAKS, *extra):
            assert leak not in r.getMessage() and leak not in str(r.args), leak


def test_nothing_private_is_logged_in_a_full_written_run(db_url, caplog, app_debug):
    import re

    caplog.set_level(logging.DEBUG)  # root: every logger that propagates
    with ask_env_marked(db_url, marked_parts()) as env:
        app_debug()
        r = env.ask(LEAK_QUESTION)
        page = env.run_page(r.headers["location"]).text
        status = env.one("SELECT status FROM runs")
        stored = env.one("SELECT body::text FROM answers")
        writer_sent = env.gemini.writer.bodies()
    # The markers were really in the app: the writer got the question and the abstract, the
    # stored answer holds the text of every part, and the page shows it.
    assert status == "done" and len(writer_sent) == 4
    for b in writer_sent:
        text = b["contents"][0]["parts"][0]["text"]
        assert "zq7" in text and ABSTRACT_TAG in text
    for tag in PART_TAGS.values():
        assert tag in stored, tag
    assert "Answer ready" in page and PART_TAGS["outcomes"] in page
    assert_no_private_text(caplog)
    msgs = _app_messages(caplog)
    assert_app_lines_fixed(msgs)
    # The pattern the plan names, and each part's first call.
    assert any(re.fullmatch(r"writer part=outcomes attempt=first result=ok kind=none "
                            r"output_tokens=\d+", m) for m in msgs)
    for name in PART_TAGS:
        assert any(m.startswith(f"writer part={name} attempt=first result=ok ") for m in msgs)
    assert "writer kind=merged labels_defaulted=0" in msgs


def test_failed_parts_log_kinds_only(db_url, caplog, app_debug, monkeypatch):
    """A blocked part (with a marked message), the one piece (marked text), and an answer the
    database refuses (marked body): fixed kinds and class names only, no exception text."""
    import re

    caplog.set_level(logging.DEBUG)
    parts, piece = piece_after_a_blocked_part()
    with ask_env_marked(db_url, parts, piece) as env:
        app_debug()
        env.ask(LEAK_QUESTION)
        stored = env.one("SELECT body::text FROM answers")
        status = env.one("SELECT status FROM runs")
    assert status == "done" and PIECE_TAG in stored
    msgs = _app_messages(caplog)
    assert "writer part=trials attempt=first result=refused kind=block output_tokens=0" in msgs
    assert "writer part=trials attempt=retry result=refused kind=block output_tokens=0" in msgs
    assert "writer kind=parts_failed failed=1 first=trials" in msgs
    assert any(m.startswith("writer part=one_piece attempt=first result=ok ") for m in msgs)
    assert not any(m.startswith("writer kind=merged") for m in msgs)
    assert_no_private_text(caplog, extra=(BLOCK_NOTE,))
    assert_app_lines_fixed(msgs)

    caplog.clear()
    without_store_cleaning(monkeypatch)
    with ask_env_marked(db_url, refused_by_the_database()) as env:
        app_debug()
        r = env.ask(LEAK_QUESTION)
        page = env.run_page(r.headers["location"]).text
        row = env.rows("SELECT status, error_kind FROM runs ORDER BY created_at DESC")[0]
        answers = env.one("SELECT count(*) FROM answers WHERE run_id = (SELECT id FROM runs "
                          "ORDER BY created_at DESC LIMIT 1)")
    assert tuple(row) == ("failed", "internal") and answers == 0  # nothing half-stored
    assert "Something went wrong. Please try again in a minute." in page
    assert REFUSED_TAG not in page
    msgs = _app_messages(caplog)
    assert any(re.fullmatch(r"run kind=answer_not_stored error=\w+", m) for m in msgs)
    assert_no_private_text(caplog)
    assert_app_lines_fixed(msgs)


def _writer_scenarios():
    """(name, parts, one piece, researcher script) for the allowlist test: every way a part
    can end that the writer logs a line for."""
    from gemini_fakes import error
    from writer_fakes import part_reply, piece_reply, whole_answer

    from pubmedgemini.writer.prompts import Section

    def ok(name, tag="w"):
        return part_reply({s.name: s for s in Section.ALL}[name], whole_answer(tag))

    cut = reply_text('{"studies": [{"pmid": "', finish="MAX_TOKENS")
    bad = reply_text(f"Sorry, {PART_TAGS['trials']} is not JSON")
    one = [piece_reply(whole_answer(PIECE_TAG))]
    yield "all parts at once", marked_parts(), [], leak_script()
    yield ("retries: cut off, unreadable, blocked, then read",
           {"trials": [bad, ok("trials")], "outcomes": [cut, ok("outcomes")],
            "context": [blocked_reply(), ok("context")], "summary": [ok("summary")]},
           [], leak_script())
    yield ("a part fails twice: one piece",
           {"trials": [bad, bad], "outcomes": [ok("outcomes")], "context": [ok("context")],
            "summary": [ok("summary")]}, one, leak_script())
    yield ("one piece fails too",
           {"trials": [bad, bad], "outcomes": [ok("outcomes")], "context": [ok("context")],
            "summary": [ok("summary")]}, [bad, bad], leak_script())
    yield ("billing refused in a part",
           {"trials": [ok("trials")], "outcomes": [ok("outcomes")], "context": [ok("context")],
            "summary": [error(402, "X", message="gm-q7 secret text")]}, [], leak_script())


def reply_text(text: str, finish: str = "STOP") -> dict:
    from gemini_fakes import reply

    return reply([{"text": text}], finish=finish)


def test_every_writer_log_line_matches_the_allowlist(db_url, caplog, app_debug):
    """Every line a written run emits (the writer's own and the runner's) is one of the fixed
    shapes of WRITER_LINE_PATTERNS or APP_LINE_PATTERNS; a new shape fails here. The check is
    shown to bite: a line of an unknown shape, or with free text, is refused."""
    import re

    def allowed(m: str) -> bool:
        return any(re.fullmatch(p, m) for p in WRITER_LINE_PATTERNS)

    for odd in ("writer part=outcomes attempt=first result=ok kind=none output_tokens=5 text=x",
                "writer part=outcomes said hello", "writer kind=new_line",
                "writer part=poem attempt=first result=ok kind=none output_tokens=1",
                "run kind=answer run_id=1 written=true"):
        assert not allowed(odd) and not any(re.fullmatch(p, odd) for p in APP_LINE_PATTERNS), odd

    seen_writer = set()
    for name, parts, one_piece, script in _writer_scenarios():
        caplog.clear()
        caplog.set_level(logging.DEBUG)
        with ask_env_marked(db_url, parts, one_piece, script=script) as env:
            app_debug()
            env.ask(LEAK_QUESTION)
            assert env.one("SELECT count(*) FROM runs") >= 1, name
        records = [r for r in caplog.records if r.name.startswith("pubmedgemini")]
        writer = [r.getMessage() for r in records if r.name == "pubmedgemini.writer"]
        assert writer, name
        for m in writer:
            assert allowed(m), (name, m)
            seen_writer.add(m.split()[1].split("=")[0] + ":" + m.split()[1])
        for r in records:  # the runner's lines after the writer, and every other app line
            assert any(re.fullmatch(p, r.getMessage()) for p in APP_LINE_PATTERNS), (
                name, r.getMessage())
        assert_no_private_text(caplog)
    results = {m for m in seen_writer}
    assert {"part=trials", "part=outcomes", "part=context", "part=summary",
            "part=one_piece", "kind=merged", "kind=parts_failed"} <= {
        r.split(":")[1] for r in results}, sorted(results)


def test_a_written_run_under_uvicorn_logs_no_writer_text(db_url):
    """The same full run under the real server: stderr holds fixed lines and none of the
    writer's markers (the parts', the abstract's), nor the Introduction's or the summary
    part's, which the early summary stored (F3c)."""
    import re

    page, _home, err = _run_question_under_uvicorn(db_url, "happy")
    msgs = _assert_stderr_fixed(err)
    assert PART_TAGS["outcomes"] in page and "Answer ready" in page
    assert f"<p>{INTRO_TAG} what the question asks.</p>" in page  # the Introduction, folded
    for leak in (*WRITER_LEAKS, *INTRO_LEAKS, "Traceback"):
        assert leak not in err, leak
    assert "writer part=outcomes attempt=first result=ok kind=none output_tokens=321" in msgs
    assert any(re.fullmatch(rf"run kind=intro run_id={UUID} status=done reason=none "
                            r"output_tokens=111", m) for m in msgs), msgs
    assert any(m.startswith("run kind=early_stored ") and " summary=true " in m for m in msgs)


def test_a_failing_introduction_under_uvicorn_leaves_the_run_done(db_url):
    """The Introduction's one attempt fails (Google's message marked): the run still ends
    done with its answer, the page shows no Introduction, and stderr holds fixed lines only."""
    import re

    page, _home, err = _run_question_under_uvicorn(db_url, "intro_fails")
    msgs = _assert_stderr_fixed(err)
    assert "Answer ready" in page and "Introduction" not in page
    assert sum(m.endswith(" status=done error_kind=none") for m in msgs) == 1
    assert any(re.fullmatch(rf"run kind=intro run_id={UUID} status=failed reason=api "
                            r"output_tokens=0", m) for m in msgs), msgs
    assert any(m.startswith("gemini step=intro attempt=1 status=overloaded ") for m in msgs)
    for leak in (*WRITER_LEAKS, *INTRO_LEAKS, *LEAKS, "Traceback", "Exception in"):
        assert leak not in err, leak


@pytest.mark.parametrize("case", ["marked", "fails", "blocked", "withdrawn"])
def test_nothing_private_is_logged_with_intro_and_early(db_url, caplog, app_debug, case):
    """Markers in the question (zq7), the Introduction's reply, Google's message on its failed
    call, a blocked reply's note and the summary part's text (ps4, stored in the early
    summary) never reach a log line; every new line matches the allow-list."""
    from writer_fakes import intro_reply

    intro = {"marked": marked_intro(), "fails": failing_intro(),
             "blocked": [{"promptFeedback": {"blockReason": "SAFETY",
                                             "blockReasonMessage": INTRO_BLOCK_TAG},
                          "usageMetadata": {}}],
             "withdrawn": [intro_reply(f"{INTRO_TAG} x")]}[case]
    parts, piece = (piece_after_a_blocked_part() if case == "withdrawn"
                    else (marked_parts(), []))
    caplog.set_level(logging.DEBUG)
    with ask_env_marked(db_url, parts, piece) as env:
        env.gemini._intro_steps = list(intro)
        app_debug()
        r = env.ask(LEAK_QUESTION)
        page = env.run_page(r.headers["location"]).text
        status = env.one("SELECT status FROM runs")
        intro_status = env.one("SELECT status FROM run_intros")
        sent = env.gemini.intro_sent
    assert status == "done" and "Answer ready" in page
    assert len(sent) == 1 and "zq7" in sent[0]["contents"][0]["parts"][0]["text"]
    assert intro_status == ("done" if case in ("marked", "withdrawn") else "failed")
    assert (INTRO_TAG in page) == (case in ("marked", "withdrawn"))
    assert_no_private_text(caplog)
    msgs = _app_messages(caplog)
    assert_app_lines_fixed(msgs)
    assert sum(m.startswith("run kind=intro run_id=") for m in msgs) == 1
    if case == "withdrawn":
        assert any(m.startswith("run kind=early_withdrawn run_id=") for m in msgs)
    else:
        assert any(m.startswith("run kind=early_stored ") and " summary=true " in m
                   for m in msgs)


def test_an_unreadable_early_summary_logs_its_fixed_kind_only(db_url, caplog, app_debug):
    from psycopg.types.json import Jsonb
    from test_ask_page import H, ask_env, insert_run

    caplog.set_level(logging.DEBUG)
    with ask_env(db_url) as env:
        app_debug()
        rid = insert_run(env, status="running", error_kind=None)
        with env.pool.connection() as conn:
            conn.execute("INSERT INTO run_early (run_id, body) VALUES (%s, %s)",
                         (rid, Jsonb({"summary": True, "answer": {"ps4": f"{PART_TAGS['summary']}"}})))
        page = env.c.get(f"/runs/{rid}", headers=H).text
    assert "Early" not in page
    msgs = _app_messages(caplog)
    assert_app_lines_fixed(msgs)
    assert "run page early kind=invalid" in msgs
    assert_no_private_text(caplog)


def test_the_one_piece_and_a_refused_answer_under_uvicorn_log_no_writer_text(db_url):
    page, _home, err = _run_question_under_uvicorn(db_url, "fallback")
    msgs = _assert_stderr_fixed(err)
    assert PIECE_TAG in page and "writer kind=parts_failed failed=1 first=trials" in msgs
    for leak in (*WRITER_LEAKS, *LEAKS, "Traceback", "Exception in"):
        assert leak not in err, leak
    page, _home, err = _run_question_under_uvicorn(db_url, "db_refused")
    msgs = _assert_stderr_fixed(err)
    assert "Something went wrong. Please try again in a minute." in page
    assert any(m.startswith("run kind=answer_not_stored error=") for m in msgs)
    for leak in (*WRITER_LEAKS, *LEAKS, "Traceback", "Exception in", "Unicode", "unsupported"):
        assert leak not in err, leak


@contextmanager
def ask_env_marked(db_url, parts, one_piece=(), script=None):
    """A whole-run app over the marked abstract, the leak script and the given writer replies."""
    from test_ask_page import ask_env

    with ask_env(db_url, marked_upstream(), leak_script() if script is None else script,
                 key="gk-q7", parts=parts, one_piece=one_piece) as env:
        yield env


# ---------- the answer page (F3b Task 5) ----------

def _marked_answer():
    """The hernia answer with a marker in every kind of model field the page draws."""
    from test_answer_page import hernia

    b = hernia()
    w = b.answer
    o = w.outcomes[0]
    o.outcome = "mkq-outcome"
    o.effect_text = "mkq-effect [90000001]"
    o.why = "mkq-why"
    for n in o.numbers:
        n.label += " mkq-label"
        if n.arm:
            n.arm += " mkq-arm"
    w.studies[0].comparison = "mkq-cmp vs other"
    w.studies[0].label = "mkq-study"
    w.bottom_line.text = "mkq-bottom"
    b.references[0].title = "mkq-title"
    b.notes.append("mkq-note")
    return b


def test_answer_page_logs_nothing_of_the_answer(db_url, upstream, caplog, app_debug,
                                                monkeypatch):
    from test_ask_page import HAPPY_RUN, H, ask_env
    from test_run_answer_layout import store

    import pubmedgemini.answer_page as ap

    with ask_env(db_url, upstream, HAPPY_RUN) as env:
        app_debug()
        url = store(env, _marked_answer())
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            r = env.c.get(url, headers=H)
        assert r.status_code == 200 and "mkq-outcome" in r.text and "mkq-arm" in r.text
        assert "mkq" not in caplog.text
        assert_app_lines_fixed(_app_messages(caplog))
        assert not [m for m in _app_messages(caplog) if m.startswith("answer_page")]

        # a row whose derivation fails: listed on the page, one fixed line with a count only
        def boom(*_a):
            raise OverflowError("mkq-exception")

        monkeypatch.setattr(ap, "_signed", boom)  # the difference axis labels
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            r = env.c.get(url, headers=H)
        assert r.status_code == 200
        assert "Not drawn: the numbers could not be placed on a scale." in r.text
        assert "mkq" not in caplog.text
        lines = [m for m in _app_messages(caplog) if m.startswith("answer_page")]
        assert lines == ["answer_page kind=row_not_derived rows=1"]
        assert_app_lines_fixed(_app_messages(caplog))


def test_an_unreadable_answer_logs_its_fixed_kind_only(db_url, upstream, caplog, app_debug):
    # Review leak gap: the run page's unreadable-answer line (main's own) must carry the fixed
    # kind alone: nothing of the stored body, nothing of the exception (no repr, no text, no
    # traceback).
    from psycopg.types.json import Jsonb
    from test_ask_page import HAPPY_RUN, H, ask_env, insert_run

    body = _marked_answer().to_body()
    body["answer"]["outcomes"][0]["numbers"] = "mkq-numbers-not-a-list"  # cannot be read
    body["answer"]["bottom_line"]["certainty"] = "mkq-certainty"
    with ask_env(db_url, upstream, HAPPY_RUN) as env:
        app_debug()
        rid = insert_run(env, status="done", error_kind=None)
        with env.pool.connection() as conn:
            conn.execute("INSERT INTO answers (run_id, version, body) VALUES (%s, 3, %s)",
                         (rid, Jsonb(body)))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            r = env.c.get(f"/runs/{rid}", headers=H)
        assert r.status_code == 200 and "The answer could not be shown." in r.text
        assert "mkq" not in r.text
        lines = [m for m in _app_messages(caplog) if m.startswith("run page answer")]
        assert lines == ["run page answer kind=invalid"]
        assert_app_lines_fixed(_app_messages(caplog))
        assert "mkq" not in caplog.text
        for word in ("AnswerInvalid", "ValidationError", "Traceback", "invalid'", "('"):
            assert word not in caplog.text, word
        assert all(rec.exc_info is None and rec.exc_text is None for rec in caplog.records)
