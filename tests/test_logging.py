import logging


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


def test_configuring_logging_twice_adds_one_handler(settings_prod):
    import logging as std_logging

    from pubmedgemini.main import create_app

    create_app(settings_prod)
    create_app(settings_prod)
    app_log = std_logging.getLogger("pubmedgemini")
    assert app_log.level == std_logging.INFO and len(app_log.handlers) == 1
    for name in ("psycopg", "psycopg.pool"):
        assert std_logging.getLogger(name).getEffectiveLevel() > std_logging.CRITICAL
