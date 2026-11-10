from pathlib import Path

from pubmedgemini.identity import (
    email_from_headers,
    ensure_owner,
    find_user,
    normalise_email,
)
from pubmedgemini.migrate import apply_migrations


def test_gate_header_wins():
    headers = {"x-forwarded-email": "A@x.com", "x-dev-user-email": "b@x.com"}
    assert email_from_headers(headers, "dev") == "a@x.com"


def test_dev_header_only_in_dev():  # Review Focus 2
    h = {"x-dev-user-email": "b@x.com"}
    assert email_from_headers(h, "dev") == "b@x.com"
    assert email_from_headers(h, "prod") is None


def test_no_header_is_none():  # Review Focus 1
    assert email_from_headers({}, "prod") is None
    assert email_from_headers({"x-forwarded-email": "  "}, "prod") is None


def test_blank_gate_header_falls_back_to_dev_header_only_in_dev():
    h = {"x-forwarded-email": " ", "x-dev-user-email": "b@x.com"}
    assert email_from_headers(h, "dev") == "b@x.com"
    assert email_from_headers(h, "prod") is None


def test_normalise_email_strips_and_lowers():
    assert normalise_email("  Owner@Gmail.com ") == "owner@gmail.com"


def test_case_and_spaces_match(pool):  # Review Focus 3
    apply_migrations(pool, Path("migrations"))
    ensure_owner(pool, " Owner@Gmail.com ")
    assert find_user(pool, normalise_email("OWNER@gmail.com")).role == "owner"


def test_ensure_owner_is_idempotent(pool):
    apply_migrations(pool, Path("migrations"))
    ensure_owner(pool, "owner@example.com")
    ensure_owner(pool, "owner@example.com")
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_ensure_owner_changes_nothing_when_email_exists(pool):
    apply_migrations(pool, Path("migrations"))
    with pool.connection() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES ('owner@example.com', 'surgeon')")
    ensure_owner(pool, "owner@example.com")
    assert find_user(pool, "owner@example.com").role == "surgeon"


def test_unknown_email_is_none(pool):
    apply_migrations(pool, Path("migrations"))
    assert find_user(pool, "stranger@example.com") is None


def test_find_user_gives_up_quickly_when_the_database_is_down():
    import time

    import psycopg
    import pytest

    from pubmedgemini.db import QUICK_CHECK_TIMEOUT_SECONDS, open_pool

    assert QUICK_CHECK_TIMEOUT_SECONDS == 2
    p = open_pool("postgresql://u:p@127.0.0.1:1/x")
    try:
        started = time.monotonic()
        with pytest.raises(psycopg.OperationalError):
            find_user(p, "owner@example.com")
        assert time.monotonic() - started < 3
    finally:
        p.close()


def test_home_answers_503_quickly_when_the_database_goes_down_after_start_up(monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from pubmedgemini import main
    from pubmedgemini.config import Settings

    # start-up "succeeds" without a database, as if it went down right after
    monkeypatch.setattr(main, "apply_migrations", lambda pool, d: [])
    monkeypatch.setattr(main, "ensure_owner", lambda pool, e: None)
    settings = Settings(
        app_env="prod", database_url="postgresql://u:p@127.0.0.1:1/x", owner_email="o@example.com"
    )
    with TestClient(main.create_app(settings)) as c:
        started = time.monotonic()
        r = c.get("/", headers={"x-forwarded-email": "o@example.com"})
        assert time.monotonic() - started < 3
    assert r.status_code == 503 and "Traceback" not in r.text
