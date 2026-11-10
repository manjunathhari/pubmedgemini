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
    assert "Search and answers arrive in the next stages." in r.text


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
    assert lines == ["kind=db_unreachable"]


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
