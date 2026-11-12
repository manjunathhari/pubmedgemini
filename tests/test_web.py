import logging

import psycopg
from fastapi.testclient import TestClient

from pubmedgemini.main import create_app

NOT_SET_UP = (
    "Your Google account is allowed in, but you haven't been set up yet. "
    "Ask the owner to add you."
)


def test_home_shows_signed_in_email(client_dev):
    r = client_dev.get("/", headers={"x-dev-user-email": "owner@example.com"})
    assert r.status_code == 200 and "Signed in as owner@example.com" in r.text
    assert "pubmedgemini" in r.text
    assert "Written answers arrive in the next stage." not in r.text  # F3: they are here


def test_home_matches_gate_email_in_any_case_and_spaces(client_prod):  # Review Focus 3
    r = client_prod.get("/", headers={"x-forwarded-email": " Owner@Example.com "})
    assert r.status_code == 200 and "Signed in as owner@example.com" in r.text


def test_no_header_is_401(client_prod):  # Review Focus 1
    r = client_prod.get("/")
    assert r.status_code == 401
    assert "You are not signed in." in r.text


def test_dev_header_ignored_in_prod(client_prod):  # Review Focus 2
    r = client_prod.get("/", headers={"x-dev-user-email": "owner@example.com"})
    assert r.status_code == 401


def test_unknown_user_is_403_with_exact_message(client_prod):
    r = client_prod.get("/", headers={"x-forwarded-email": "stranger@example.com"})
    assert r.status_code == 403
    assert NOT_SET_UP in r.text.replace("&#39;", "'")


def test_healthz_ok(client_prod):
    assert client_prod.get("/healthz").json() == {"status": "ok"}


def test_healthz_needs_no_sign_in(client_prod):
    assert client_prod.get("/healthz").status_code == 200


def test_database_down_is_503_without_trace(client_db_down):  # Review Focus 4
    r = client_db_down.get("/", headers={"x-forwarded-email": "owner@example.com"})
    assert r.status_code == 503 and "Traceback" not in r.text
    assert "The database could not be reached. Please try again in a minute." in r.text
    h = client_db_down.get("/healthz")
    assert h.status_code == 503 and h.json() == {"status": "database_unreachable"}


def test_start_up_with_database_down_logs_one_fixed_line(caplog):
    caplog.set_level(logging.INFO)
    from pubmedgemini.config import Settings

    settings = Settings(
        app_env="prod", database_url="postgresql://u:p@127.0.0.1:1/x", owner_email="o@example.com"
    )
    with TestClient(create_app(settings)):
        pass
    lines = [r.getMessage() for r in caplog.records if "db_unreachable" in r.getMessage()]
    # class name and sqlstate only: the message ("couldn't get a connection ...") is not logged
    assert lines == ["kind=db_unreachable error=PoolTimeout sqlstate=none"]
    assert "couldn't" not in caplog.text and "127.0.0.1" not in caplog.text


def test_start_up_work_is_retried_when_database_comes_back(settings_prod, db_url):
    app = create_app(settings_prod)
    with TestClient(app) as c:
        # simulate a start-up that failed: forget that it finished and drop the schema
        app.state.ready = False
        with psycopg.connect(db_url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        r = c.get("/", headers={"x-forwarded-email": "owner@example.com"})
        assert r.status_code == 200


def test_second_start_does_not_fail(settings_prod):  # Review Focus 5
    with TestClient(create_app(settings_prod)):
        pass
    with TestClient(create_app(settings_prod)) as c:
        assert c.get("/healthz").status_code == 200


def test_pages_are_plain_html(client_dev):
    r = client_dev.get("/", headers={"x-dev-user-email": "owner@example.com"})
    assert "<script" not in r.text and "http://" not in r.text and "https://" not in r.text
    assert r.text.count("<main") == 1
    assert "42rem" in r.text


def test_failed_migration_makes_healthz_not_ready(db_url, tmp_path, caplog):
    from pubmedgemini.config import Settings

    (tmp_path / "0001_bad.sql").write_text("CREATE TABLE a (id int); SELECT nope_column;")
    settings = Settings(
        app_env="prod", database_url=db_url, owner_email="o@example.com", migrations_dir=tmp_path
    )
    caplog.set_level(logging.INFO)
    with TestClient(create_app(settings)) as c:
        h = c.get("/healthz")
        assert h.status_code == 503 and h.json() == {"status": "not_ready"}
        r = c.get("/", headers={"x-forwarded-email": "o@example.com"})
        assert r.status_code == 503 and "Traceback" not in r.text
    messages = [r.getMessage() for r in caplog.records]
    assert "kind=migration_failed error=UndefinedColumn sqlstate=42703" in messages
    assert not any("db_unreachable" in m for m in messages)
    assert "nope_column" not in caplog.text and "syntax" not in caplog.text.lower()


def _app_with_failing_start_up(monkeypatch, clock, gate=None):
    from pubmedgemini import main
    from pubmedgemini.config import Settings

    calls = []

    def failing(pool, migrations_dir):
        calls.append(1)
        if gate is not None:
            gate.wait(timeout=5)
        raise psycopg.OperationalError("connection details that must not be logged")

    monkeypatch.setattr(main, "apply_migrations", failing)
    settings = Settings(
        app_env="prod", database_url="postgresql://u:p@127.0.0.1:1/x", owner_email="o@example.com"
    )
    return main, main.create_app(settings, clock=clock), calls


def test_requests_during_the_back_off_make_no_new_attempt(monkeypatch):
    now = [1000.0]
    main, app, calls = _app_with_failing_start_up(monkeypatch, lambda: now[0])
    with TestClient(app) as c:
        assert len(calls) == 1  # the start-up attempt
        for _ in range(8):
            assert c.get("/healthz").json() == {"status": "database_unreachable"}
            assert c.get("/", headers={"x-forwarded-email": "o@example.com"}).status_code == 503
        assert len(calls) == 1
        now[0] += main.RETRY_BACKOFF_SECONDS + 0.1
        assert c.get("/healthz").status_code == 503
        assert len(calls) == 2


def test_parallel_requests_do_not_queue_behind_a_running_attempt(monkeypatch):
    import threading
    import time

    from pubmedgemini.web import NotReady

    now = [1000.0]
    gate = threading.Event()
    gate.set()  # the start-up attempt fails at once
    main, app, calls = _app_with_failing_start_up(monkeypatch, lambda: now[0], gate)
    with TestClient(app):
        now[0] += main.RETRY_BACKOFF_SECONDS + 0.1
        gate.clear()  # the next attempt hangs until released
        first = threading.Thread(target=lambda: _swallow(app.state.ensure_ready, NotReady))
        first.start()
        while len(calls) < 2:
            time.sleep(0.01)
        started = time.monotonic()
        for _ in range(8):
            assert _swallow(app.state.ensure_ready, NotReady)
        assert time.monotonic() - started < 1
        gate.set()
        first.join(timeout=5)
        assert len(calls) == 2


def _swallow(fn, exc_type) -> bool:
    try:
        fn()
    except exc_type:
        return True
    return False


def test_cancelled_migration_is_migration_failed_not_db_unreachable(db_url, tmp_path, caplog):
    """A query-cancel is an OperationalError, but the database was reachable."""
    from pubmedgemini.config import Settings

    (tmp_path / "0001_slow.sql").write_text(
        "SET LOCAL statement_timeout = '50ms'; SELECT pg_sleep(2);"
    )
    settings = Settings(
        app_env="prod", database_url=db_url, owner_email="o@example.com", migrations_dir=tmp_path
    )
    caplog.set_level(logging.INFO)
    with TestClient(create_app(settings)) as c:
        h = c.get("/healthz")
        assert h.status_code == 503 and h.json() == {"status": "not_ready"}
    messages = [r.getMessage() for r in caplog.records if r.name == "pubmedgemini.main"]
    assert messages == ["kind=migration_failed error=QueryCanceled sqlstate=57014"]
    assert "statement timeout" not in caplog.text and "canceling" not in caplog.text
