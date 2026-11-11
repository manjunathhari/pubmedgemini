import logging
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from pubmedgemini.lit.cache import FRESH_DAYS, PRUNE_DAYS, get_many, prune, put_many
from pubmedgemini.lit.paper import Paper
from pubmedgemini.migrate import apply_migrations


@pytest.fixture
def cache_pool(pool):
    apply_migrations(pool, Path("migrations"))
    return pool


def pool_exec(pool, sql: str, params=()) -> list[tuple]:
    with pool.connection() as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def paper(**kw) -> Paper:
    base = {"pmid": "123", "pmcid": "PMC9", "doi": "10.1/x", "title": "Mesh repair",
            "authors": ["Smith J", "Lee K"], "journal": "Hernia", "year": 2024,
            "pub_types": ["Journal Article", "Randomized Controlled Trial"],
            "abstract": "BACKGROUND: a\n\nMETHODS: b", "retracted": False, "source": "both"}
    return Paper(**(base | kw))


def age(pool, pmid: str, days: int) -> None:
    pool_exec(pool, "UPDATE papers SET fetched_at = now() - make_interval(days => %s) WHERE pmid = %s",
              (days, pmid))


def test_days_match_incisive():
    assert (FRESH_DAYS, PRUNE_DAYS) == (30, 90)


def test_put_then_get_round_trip(cache_pool):
    p = paper()
    put_many(cache_pool, [p])
    [got] = get_many(cache_pool, ["123"])
    # The table has no source column: a cached paper comes back as "pubmed" (Incisive).
    assert got == paper(source="pubmed")
    assert isinstance(got.authors, list) and isinstance(got.pub_types, list)


def test_get_many_returns_only_known_ids(cache_pool):
    put_many(cache_pool, [paper(pmid="1"), paper(pmid="2")])
    assert sorted(p.pmid for p in get_many(cache_pool, ["2", "1", "7"])) == ["1", "2"]
    assert get_many(cache_pool, []) == []


def test_nulls_and_empty_lists_round_trip(cache_pool):
    p = paper(pmcid=None, doi=None, journal=None, year=None, abstract=None, authors=[], pub_types=[])
    put_many(cache_pool, [p])
    assert get_many(cache_pool, ["123"]) == [paper(pmcid=None, doi=None, journal=None, year=None,
                                                   abstract=None, authors=[], pub_types=[],
                                                   source="pubmed")]


def test_stale_rows_are_not_served(cache_pool):
    p = paper()
    put_many(cache_pool, [p])
    pool_exec(cache_pool, "UPDATE papers SET fetched_at = now() - interval '31 days'")
    assert get_many(cache_pool, [p.pmid]) == []


def test_rows_inside_30_days_are_served(cache_pool):
    put_many(cache_pool, [paper()])
    age(cache_pool, "123", 29)
    assert len(get_many(cache_pool, ["123"])) == 1


def test_refresh_never_erases(cache_pool):
    put_many(cache_pool, [paper(retracted=True)])
    age(cache_pool, "123", 40)
    put_many(cache_pool, [paper(abstract=None, doi=None, pmcid=None, retracted=False,
                                title="New title", journal=None, pub_types=["Letter"])])
    [got] = get_many(cache_pool, ["123"])  # served again: fetched_at is now
    assert (got.abstract, got.doi, got.pmcid, got.retracted) == (
        "BACKGROUND: a\n\nMETHODS: b", "10.1/x", "PMC9", True)
    # The rest is replaced.
    assert (got.title, got.journal, got.pub_types) == ("New title", None, ["Letter"])


def test_refresh_replaces_values_when_new_ones_are_given(cache_pool):
    put_many(cache_pool, [paper()])
    put_many(cache_pool, [paper(abstract="new", doi="10.2/y", pmcid="PMC1")])
    [got] = get_many(cache_pool, ["123"])
    assert (got.abstract, got.doi, got.pmcid) == ("new", "10.2/y", "PMC1")


def test_paper_without_pmid_is_skipped(cache_pool):
    put_many(cache_pool, [paper(pmid=None), paper(pmid="  "), paper(pmid=" 77 ")])
    assert pool_exec(cache_pool, "SELECT pmid FROM papers") == [("77",)]


def test_design_is_stored_from_the_rule(cache_pool):
    put_many(cache_pool, [paper(pmid="1", pub_types=["Randomized Controlled Trial"]),
                          paper(pmid="2", pub_types=[], title="A meta-analysis of mesh"),
                          paper(pmid="3", pub_types=["Letter", "Randomized Controlled Trial"])])
    rows = dict(pool_exec(cache_pool, "SELECT pmid, design FROM papers"))
    assert rows == {"1": "RCT", "2": "meta-analysis", "3": "other"}
    put_many(cache_pool, [paper(pmid="1", pub_types=["Systematic Review"])])  # recomputed
    assert pool_exec(cache_pool, "SELECT design FROM papers WHERE pmid = '1'") == [
        ("systematic review",)]


def test_put_many_is_one_transaction(cache_pool):
    with pytest.raises(psycopg.errors.CheckViolation):
        put_many(cache_pool, [paper(pmid="1"), paper(pmid="1234567890")])
    assert pool_exec(cache_pool, "SELECT count(*) FROM papers") == [(0,)]


def test_prune_deletes_after_90_days(cache_pool):
    put_many(cache_pool, [paper(pmid="1"), paper(pmid="2"), paper(pmid="3")])
    age(cache_pool, "1", 91)
    age(cache_pool, "2", 89)
    assert prune(cache_pool) == 1
    assert sorted(r[0] for r in pool_exec(cache_pool, "SELECT pmid FROM papers")) == ["2", "3"]
    assert prune(cache_pool) == 0


def test_pmid_must_be_digits(cache_pool):
    with cache_pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO papers (pmid, title, design) VALUES ('abc', 't', 'other')")


def test_design_must_be_a_known_label(cache_pool):
    with cache_pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO papers (pmid, title, design) VALUES ('1', 't', 'cohort')")


def test_start_up_prunes_once_and_logs_the_count(settings_prod, caplog):
    from pubmedgemini.main import create_app

    app = create_app(settings_prod)
    with TestClient(app):  # the first start creates the table
        put_many(app.state.pool, [paper(pmid="5"), paper(pmid="6")])
        age(app.state.pool, "5", 100)
    caplog.set_level(logging.INFO, logger="pubmedgemini.main")
    caplog.clear()
    app = create_app(settings_prod)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        assert [r[0] for r in pool_exec(app.state.pool, "SELECT pmid FROM papers")] == ["6"]
    lines = [r.getMessage() for r in caplog.records if r.name == "pubmedgemini.main"]
    assert lines == ["paper_cache pruned=1"]


def test_start_up_carries_on_when_the_prune_fails(settings_prod, monkeypatch, caplog):
    from pubmedgemini import main

    def failing(pool):
        raise psycopg.errors.InsufficientPrivilege("details that must not be logged")

    monkeypatch.setattr(main, "prune_paper_cache", failing)
    caplog.set_level(logging.INFO, logger="pubmedgemini.main")
    with TestClient(main.create_app(settings_prod)) as c:
        assert c.get("/healthz").status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "pubmedgemini.main"]
    assert lines == ["paper_cache kind=prune_failed error=InsufficientPrivilege sqlstate=42501"]
    assert "details" not in caplog.text


def test_nul_and_lone_surrogates_are_cleaned_so_the_whole_batch_is_cached(cache_pool):
    bad = paper(pmid="1", title="Mesh\x00 repair", abstract="a\x00b", journal="J\ud800",
                authors=["Smith\x00 J"], pub_types=["Review\x00"], doi="10.1/\x00x",
                pmcid="PMC\x009")
    put_many(cache_pool, [paper(pmid="2"), bad, paper(pmid="3")])
    got = {p.pmid: p for p in get_many(cache_pool, ["1", "2", "3"])}
    assert sorted(got) == ["1", "2", "3"]
    one = got["1"]
    assert (one.title, one.abstract, one.journal) == ("Mesh repair", "ab", "J\ufffd")
    assert (one.authors, one.pub_types, one.doi, one.pmcid) == (
        ["Smith J"], ["Review"], "10.1/x", "PMC9")


def test_get_for_display_ignores_freshness(cache_pool):
    from pubmedgemini.lit.cache import get_for_display

    put_many(cache_pool, [paper(pmid="1", abstract="A"), paper(pmid="2")])
    age(cache_pool, "1", 60)
    assert get_many(cache_pool, ["1"]) == []  # stale for the tools
    got = get_for_display(cache_pool, ["1", "2", "3"])
    assert set(got) == {"1", "2"} and got["1"].abstract == "A" and got["1"].pmid == "1"
    assert get_for_display(cache_pool, []) == {}
