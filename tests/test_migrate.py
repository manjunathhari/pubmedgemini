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


def test_real_migrations_create_papers(pool):
    apply_migrations(pool, Path("migrations"))
    assert table_exists(pool, "papers")
    with pool.connection() as conn:
        cols = conn.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'papers' ORDER BY ordinal_position"
        ).fetchall()
    assert cols == [
        ("pmid", "text", "NO"), ("pmcid", "text", "YES"), ("doi", "text", "YES"),
        ("title", "text", "NO"), ("authors", "ARRAY", "NO"), ("journal", "text", "YES"),
        ("year", "integer", "YES"), ("pub_types", "ARRAY", "NO"), ("design", "text", "NO"),
        ("abstract", "text", "YES"), ("retracted", "boolean", "NO"),
        ("fetched_at", "timestamp with time zone", "NO"),
    ]


def test_users_email_must_be_lower_and_trimmed(pool):
    apply_migrations(pool, Path("migrations"))
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO users (email, role) VALUES (' A@x.com', 'owner')")


def test_users_role_must_be_known(pool):
    apply_migrations(pool, Path("migrations"))
    with pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO users (email, role) VALUES ('a@example.com', 'admin')")


def test_two_pools_applying_at_once_apply_each_file_once(db_url, tmp_path):
    import threading

    from pubmedgemini.db import open_pool

    for i in range(1, 6):
        # pg_sleep widens the window in which the two callers overlap
        (tmp_path / f"000{i}_t{i}.sql").write_text(f"SELECT pg_sleep(0.05); CREATE TABLE t{i} (id int);")
    pools = [open_pool(db_url), open_pool(db_url)]
    for p in pools:
        p.wait(timeout=10)
    barrier = threading.Barrier(2)
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def run(p):
        try:
            barrier.wait(timeout=10)
            results.append(apply_migrations(p, tmp_path))
        except BaseException as exc:  # noqa: BLE001 - reported by the assert below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(p,)) for p in pools]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        names = sorted(n for r in results for n in r)
        assert names == [f"000{i}_t{i}.sql" for i in range(1, 6)]
        with pools[0].connection() as conn:
            assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 5
    finally:
        for p in pools:
            p.close()
