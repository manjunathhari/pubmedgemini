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


# ---------- F2 tables ----------


def pool_rows(pool, sql: str, params=()) -> list[tuple]:
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def pool_one(pool, sql: str, params=()):
    return pool_rows(pool, sql, params)[0][0]


def pool_exec(pool, sql: str, params=()) -> None:
    with pool.connection() as conn:
        conn.execute(sql, params)


@pytest.fixture
def owner_id(migrated_pool) -> int:
    return pool_one(
        migrated_pool,
        "INSERT INTO users (email, role) VALUES ('owner@example.com', 'owner') RETURNING id",
    )


def insert_run(pool, user_id: int, status: str = "queued", question: str = "q"):
    import uuid

    run_id = uuid.uuid4()
    pool_exec(
        pool,
        "INSERT INTO runs (id, user_id, question, status, model) VALUES (%s, %s, %s, %s, 'm')",
        (run_id, user_id, question, status),
    )
    return run_id


def test_real_migrations_create_the_f2_tables(migrated_pool):
    names = {r[0] for r in pool_rows(
        migrated_pool, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"model_calls", "runs", "run_steps", "run_searches", "run_seen", "run_papers"} <= names


def test_model_calls_has_no_text_column(migrated_pool):
    cols = dict(pool_rows(
        migrated_pool,
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'model_calls'"))
    assert {k for k, v in cols.items() if v == "text"} == {"model", "step", "status_kind"}
    assert cols["run_id"] == "uuid"


def test_quota_day_is_the_pacific_day(migrated_pool):
    """07:59 UTC on 11 Nov is still 10 Nov in Los Angeles."""
    day = pool_one(
        migrated_pool,
        "SELECT (timestamptz '2026-11-11 07:59:00+00' AT TIME ZONE 'America/Los_Angeles')::date")
    assert str(day) == "2026-11-10"


def test_model_calls_days_follow_called_at(migrated_pool):
    pool_exec(
        migrated_pool,
        "INSERT INTO model_calls (model, called_at, step, attempt, status_kind) "
        "VALUES ('m', timestamptz '2026-11-11 07:59:00+00', 'researcher', 1, 'sent')")
    utc_day, quota_day = pool_rows(migrated_pool, "SELECT utc_day, quota_day FROM model_calls")[0]
    assert (str(utc_day), str(quota_day)) == ("2026-11-11", "2026-11-10")


@pytest.mark.parametrize("sql", [
    "INSERT INTO model_calls (model, step, attempt, status_kind) VALUES ('M', 'researcher', 1, 'ok')",
    "INSERT INTO model_calls (model, step, attempt, status_kind) VALUES ('m', 'writer', 1, 'ok')",
    "INSERT INTO model_calls (model, step, attempt, status_kind) VALUES ('m', 'researcher', 4, 'ok')",
    "INSERT INTO model_calls (model, step, attempt, status_kind) VALUES ('m', 'researcher', 1, 'Ok')",
])
def test_model_calls_checks_refuse_bad_rows(migrated_pool, sql):
    with pytest.raises(psycopg.errors.CheckViolation):
        pool_exec(migrated_pool, sql)


def test_second_active_run_is_refused_by_the_database(migrated_pool, owner_id):
    insert_run(migrated_pool, owner_id, status="running")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_run(migrated_pool, owner_id, status="queued")
    insert_run(migrated_pool, owner_id, status="done")  # finished runs are not limited


def test_one_active_run_for_the_whole_app(migrated_pool, owner_id):
    other = pool_one(
        migrated_pool,
        "INSERT INTO users (email, role) VALUES ('other@example.com', 'fellow') RETURNING id")
    insert_run(migrated_pool, owner_id, status="queued")
    with pytest.raises(psycopg.errors.UniqueViolation):
        insert_run(migrated_pool, other, status="running")


def _bad_rows(run_id) -> dict[str, tuple[str, tuple]]:
    paper = ("INSERT INTO run_papers (run_id, role, rank, pmid, design, note) "
             "VALUES (%s, %s, 1, %s, %s, %s)")
    search = "INSERT INTO run_searches (run_id, seq, source, query, hits) VALUES (%s, 1, %s, %s, 1)"
    return {
        "pmid": (paper, (run_id, "selected", "PMID 1", "RCT", "w")),
        "pmid_10_digits": (paper, (run_id, "selected", "1234567890", "RCT", "w")),
        "design": (paper, (run_id, "selected", "1", "cohort", "w")),
        "design_on_considered": (paper, (run_id, "considered", "1", "RCT", "w")),
        "no_design_on_selected": (paper, (run_id, "selected", "1", None, "w")),
        "role": (paper, (run_id, "maybe", "1", None, "w")),
        "note": (paper, (run_id, "selected", "1", "RCT", "n" * 501)),
        "line": ("INSERT INTO run_steps (run_id, seq, kind, line) VALUES (%s, 1, 'note', %s)",
                 (run_id, "x" * 161)),
        "step_kind": ("INSERT INTO run_steps (run_id, seq, kind, line) VALUES (%s, 1, 'x', 'l')",
                      (run_id,)),
        "source": (search, (run_id, "Google", "q")),
        "query": (search, (run_id, "PubMed", "q" * 301)),
        "seen_pmid": ("INSERT INTO run_seen (run_id, pmid, title) VALUES (%s, 'x1', 't')",
                      (run_id,)),
    }


@pytest.mark.parametrize("case", list(_bad_rows(None)))
def test_checks_refuse_bad_rows(migrated_pool, owner_id, case):
    run_id = insert_run(migrated_pool, owner_id, status="done")
    sql, params = _bad_rows(run_id)[case]
    with pytest.raises(psycopg.errors.CheckViolation):
        pool_exec(migrated_pool, sql, params)


RUN_SQL = (
    "INSERT INTO runs (id, user_id, question, model, mode, status, error_kind, stop_reason) "
    "VALUES (gen_random_uuid(), %s, %s, 'm', %s, %s, %s, %s)"
)
GOOD_RUN = {"question": "q", "mode": "quick", "status": "done", "error_kind": None,
            "stop_reason": None}


@pytest.mark.parametrize("bad", [
    {"question": ""}, {"question": "a" * 4001}, {"mode": "full"}, {"status": "interrupted"},
    {"error_kind": "Bad Kind"}, {"stop_reason": "budget"},
])
def test_run_checks_refuse_bad_rows(migrated_pool, owner_id, bad):
    row = GOOD_RUN | bad
    with pytest.raises(psycopg.errors.CheckViolation):
        pool_exec(migrated_pool, RUN_SQL, (owner_id, *row.values()))


def test_a_good_run_row_is_accepted(migrated_pool, owner_id):
    pool_exec(migrated_pool, RUN_SQL, (owner_id, *GOOD_RUN.values()))


def test_good_rows_are_accepted_and_cascade_on_delete(migrated_pool, owner_id):
    run_id = insert_run(migrated_pool, owner_id, status="done", question="a" * 4000)
    with migrated_pool.connection() as conn:
        conn.execute("INSERT INTO run_steps (run_id, seq, kind, line) VALUES (%s, 1, 'search', %s)",
                     (run_id, "x" * 160))
        conn.execute("INSERT INTO run_searches (run_id, seq, source, query, hits) "
                     "VALUES (%s, 1, 'PubMed related', %s, 0)", (run_id, "q" * 300))
        conn.execute("INSERT INTO run_seen (run_id, pmid, title, journal, year) "
                     "VALUES (%s, '123456789', 't', NULL, NULL)", (run_id,))
        conn.execute("INSERT INTO run_papers (run_id, role, rank, pmid, design, note) VALUES "
                     "(%s, 'selected', 1, '1', 'SR_MA', %s), (%s, 'considered', 1, '1', NULL, ''),"
                     " (%s, 'contradiction', 1, '1', NULL, 'n')",
                     (run_id, "w" * 500, run_id, run_id))
        call_id = conn.execute(
            "INSERT INTO model_calls (model, step, attempt, status_kind, run_id) "
            "VALUES ('gemini-3.5-flash-lite', 'study_list_retry', 3, 'rate_limited', %s) "
            "RETURNING id", (run_id,)).fetchone()[0]
        conn.execute("DELETE FROM runs WHERE id = %s", (run_id,))
        left = [conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("run_steps", "run_searches", "run_seen", "run_papers")]
        kept = conn.execute("SELECT run_id FROM model_calls WHERE id = %s", (call_id,)).fetchone()
    assert left == [0, 0, 0, 0] and kept == (None,)  # the day count keeps the call


# ---------- F3a: answers, write and check steps, writer steps (migration 0005) ----------


def test_0005_applies_after_0004_and_is_recorded(pool):
    applied = apply_migrations(pool, Path("migrations"))
    assert applied.index("0005_answers.sql") == applied.index("0004_runs.sql") + 1
    assert [r[0] for r in pool_rows(
        pool, "SELECT name FROM schema_migrations WHERE name = '0005_answers.sql'")] == [
        "0005_answers.sql"]
    assert apply_migrations(pool, Path("migrations")) == []


def test_answers_columns(migrated_pool):
    cols = pool_rows(
        migrated_pool,
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'answers' ORDER BY ordinal_position")
    assert cols == [("run_id", "uuid", "NO"), ("version", "smallint", "NO"),
                    ("body", "jsonb", "NO"), ("created_at", "timestamp with time zone", "NO")]


def test_answers_refuse_other_versions(migrated_pool, owner_id):
    run_id = insert_run(migrated_pool, owner_id, status="done")
    with pytest.raises(psycopg.errors.CheckViolation):
        pool_exec(migrated_pool,
                  "INSERT INTO answers (run_id, version, body) VALUES (%s, 2, '{}')", (run_id,))
    pool_exec(migrated_pool,
              "INSERT INTO answers (run_id, version, body) VALUES (%s, 3, '{}')", (run_id,))


def test_answers_are_removed_with_their_run(migrated_pool, owner_id):
    run_id = insert_run(migrated_pool, owner_id, status="done")
    pool_exec(migrated_pool,
              "INSERT INTO answers (run_id, version, body) VALUES (%s, 3, '{}')", (run_id,))
    pool_exec(migrated_pool, "DELETE FROM runs WHERE id = %s", (run_id,))
    assert pool_one(migrated_pool, "SELECT count(*) FROM answers") == 0


def test_the_constraints_replaced_are_found_by_name_in_the_catalog(migrated_pool):
    """0005 drops and re-adds the CHECKs of 0003 and 0004 by their catalog names; the new
    definitions list the writer steps and the five step kinds."""
    defs = dict(pool_rows(
        migrated_pool,
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = 'c' "
        "AND conrelid::regclass::text IN ('model_calls', 'run_steps')"))
    for step in ("writer_trials", "writer_outcomes_retry", "writer_one_piece_retry",
                 "study_list_nudge"):
        assert step in defs["model_calls_step_check"]
    for kind in ("search", "note", "write", "check", "present"):
        assert f"'{kind}'" in defs["run_steps_kind_check"]


def test_0005_applies_over_existing_rows(pool, tmp_path):
    """Migrate to 0004 only (a temporary directory holding the real files up to 0004), store
    rows of every old step and kind, then apply the real 0005 over them."""
    import shutil

    for name in ("0001_users.sql", "0002_papers.sql", "0003_model_calls.sql", "0004_runs.sql"):
        shutil.copy(Path("migrations") / name, tmp_path / name)
    assert len(apply_migrations(pool, tmp_path)) == 4
    user_id = pool_one(
        pool, "INSERT INTO users (email, role) VALUES ('o@example.com', 'owner') RETURNING id")
    run_id = insert_run(pool, user_id, status="done")
    for seq, kind in enumerate(("search", "note", "present"), 1):
        pool_exec(pool, "INSERT INTO run_steps (run_id, seq, kind, line) VALUES (%s, %s, %s, 'l')",
                  (run_id, seq, kind))
    for step in ("researcher", "study_list", "study_list_nudge", "study_list_retry"):
        pool_exec(pool, "INSERT INTO model_calls (model, run_id, step, attempt, status_kind) "
                        "VALUES ('m', %s, %s, 1, 'ok')", (run_id, step))
    assert apply_migrations(pool, Path("migrations"))[0] == "0005_answers.sql"  # F3c: 0006 next
    assert pool_one(pool, "SELECT count(*) FROM users") == 1
    assert pool_one(pool, "SELECT count(*) FROM runs") == 1
    assert pool_one(pool, "SELECT count(*) FROM run_steps") == 3
    assert pool_one(pool, "SELECT count(*) FROM model_calls") == 4
    assert table_exists(pool, "answers")


# ---------- F3c: the Introduction and the early summary (migration 0006) ----------


def test_migration_0006_adds_tables_and_the_intro_step(migrated_pool, owner_id):
    assert table_exists(migrated_pool, "run_intros")
    assert table_exists(migrated_pool, "run_early")
    run_id = insert_run(migrated_pool, owner_id, status="running")
    pool_exec(migrated_pool, "INSERT INTO model_calls (model, run_id, step, attempt, status_kind) "
                             "VALUES ('m', %s, 'intro', 1, 'ok')", (run_id,))
    with pytest.raises(psycopg.errors.CheckViolation):
        pool_exec(migrated_pool, "INSERT INTO model_calls (model, step, attempt, status_kind) "
                                 "VALUES ('m', 'introx', 1, 'ok')")
    for step in ("researcher", "study_list_retry", "writer_one_piece_retry"):  # 0005's kept
        pool_exec(migrated_pool, "INSERT INTO model_calls (model, step, attempt, status_kind) "
                                 "VALUES ('m', %s, 1, 'ok')", (step,))


@pytest.mark.parametrize("sql", [
    "INSERT INTO run_intros (run_id, status) VALUES (%s, 'skipped')",
    "INSERT INTO run_intros (run_id, status, reason) VALUES (%s, 'failed', 'Free text!')",
    "INSERT INTO run_intros (run_id, status, text) VALUES (%s, 'failed', 'x')",
    "INSERT INTO run_intros (run_id, status) VALUES (%s, 'done')",
])
def test_run_intros_checks_refuse_bad_rows(migrated_pool, owner_id, sql):
    run_id = insert_run(migrated_pool, owner_id, status="running")
    with pytest.raises(psycopg.errors.CheckViolation):
        pool_exec(migrated_pool, sql, (run_id,))


def test_intro_and_early_rows_go_with_their_run(migrated_pool, owner_id):
    run_id = insert_run(migrated_pool, owner_id, status="running")
    pool_exec(migrated_pool, "INSERT INTO run_intros (run_id) VALUES (%s)", (run_id,))
    pool_exec(migrated_pool, "INSERT INTO run_early (run_id, body) VALUES (%s, '{}')", (run_id,))
    pool_exec(migrated_pool, "DELETE FROM runs WHERE id = %s", (run_id,))
    assert pool_one(migrated_pool, "SELECT count(*) FROM run_intros") == 0
    assert pool_one(migrated_pool, "SELECT count(*) FROM run_early") == 0


# ---------- F4a: the completeness check (migration 0007) ----------

CHECK_DONE = ("INSERT INTO run_checks (run_id, status, numbers_found, numbers_total, "
              "names_match, names_total) VALUES (%s, 'done', 2, 3, 1, 2)")


def test_migration_0007_adds_run_checks(migrated_pool, owner_id):
    assert table_exists(migrated_pool, "run_checks")
    a, b, c = (insert_run(migrated_pool, owner_id, status="done") for _ in range(3))
    pool_exec(migrated_pool, "INSERT INTO run_checks (run_id) VALUES (%s)", (a,))
    assert pool_rows(migrated_pool, "SELECT status, reason, numbers_found, started_at, "
                                    "finished_at FROM run_checks WHERE run_id = %s", (a,)) == [
        ("pending", None, None, None, None)]
    pool_exec(migrated_pool, CHECK_DONE, (b,))
    pool_exec(migrated_pool, "INSERT INTO run_checks (run_id, status, reason) "
                             "VALUES (%s, 'failed', 'stale')", (c,))


CHECK_COUNTS = ("INSERT INTO run_checks (run_id, status, numbers_found, numbers_total, "
                "names_match, names_total) VALUES (%s, ")


@pytest.mark.parametrize("sql", [
    "INSERT INTO run_checks (run_id, status) VALUES (%s, 'done')",  # done without counts
    CHECK_COUNTS + "'done', 1, 1, 1, NULL)",  # one count missing
    CHECK_COUNTS + "'done', 4, 3, 1, 2)",  # found > total
    CHECK_COUNTS + "'done', 2, 3, 3, 2)",  # match > total
    CHECK_COUNTS + "'done', -1, 3, 1, 2)",  # below zero
    CHECK_COUNTS + "'running', 2, 3, 1, 2)",  # counts before done
    "INSERT INTO run_checks (run_id, status) VALUES (%s, 'failed')",  # failed without a reason
    "INSERT INTO run_checks (run_id, status, reason) VALUES (%s, 'pending', 'stale')",
    "INSERT INTO run_checks (run_id, status, reason) VALUES (%s, 'failed', 'Free text!')",
    "INSERT INTO run_checks (run_id, status) VALUES (%s, 'checked')",  # not a status
])
def test_run_checks_table_refuses_bad_rows(migrated_pool, owner_id, sql):
    run_id = insert_run(migrated_pool, owner_id, status="done")
    with pytest.raises(psycopg.errors.CheckViolation):
        pool_exec(migrated_pool, sql, (run_id,))


def test_run_checks_has_no_free_text_column(migrated_pool):
    cols = pool_rows(
        migrated_pool,
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'run_checks' ORDER BY ordinal_position")
    assert cols == [("run_id", "uuid"), ("status", "text"), ("reason", "text"),
                    ("numbers_found", "integer"), ("numbers_total", "integer"),
                    ("names_match", "integer"), ("names_total", "integer"),
                    ("created_at", "timestamp with time zone"),
                    ("started_at", "timestamp with time zone"),
                    ("finished_at", "timestamp with time zone")]
