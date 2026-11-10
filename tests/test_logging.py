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
    with search_client(db_url, upstream, ncbi_api_key="key-q7",
                       ncbi_email="dev-q7@example.org") as c:
        r = c.post("/search", data={"q": "zq7 hernia mesh?"}, headers=H)
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
