from pathlib import Path

import psycopg
import pytest

from pubmedgemini.migrate import apply_migrations


def table_exists(pool, name: str) -> bool:
    with pool.connection() as conn:
        row = conn.execute("SELECT to_regclass(%s)", (f"public.{name}",)).fetchone()
    return row[0] is not None


def test_applies_files_in_order_once(pool, tmp_path):
    (tmp_path / "0002_b.sql").write_text("CREATE TABLE b (id int);")
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (id int);")
    assert apply_migrations(pool, tmp_path) == ["0001_a.sql", "0002_b.sql"]
    assert apply_migrations(pool, tmp_path) == []  # Review Focus 5


def test_a_failing_file_leaves_no_ledger_row(pool, tmp_path):
    (tmp_path / "0001_bad.sql").write_text("CREATE TABLE a (id int); SELECT nope;")
    with pytest.raises(psycopg.Error):
        apply_migrations(pool, tmp_path)
    assert table_exists(pool, "a") is False
    with pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0


def test_files_not_matching_the_pattern_are_ignored(pool, tmp_path):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (id int);")
    (tmp_path / "notes.sql").write_text("this is not sql")
    (tmp_path / "README.md").write_text("hello")
    assert apply_migrations(pool, tmp_path) == ["0001_a.sql"]


def test_real_migrations_create_users(pool):
    apply_migrations(pool, Path("migrations"))
    assert table_exists(pool, "users")


def test_users_email_must_be_lower_and_trimmed(pool):
    apply_migrations(pool, Path("migrations"))
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO users (email, role) VALUES (' A@x.com', 'owner')")


def test_users_role_must_be_known(pool):
    apply_migrations(pool, Path("migrations"))
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO users (email, role) VALUES ('a@example.com', 'admin')")
