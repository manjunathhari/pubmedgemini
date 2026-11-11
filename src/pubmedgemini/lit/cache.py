"""Postgres cache of public bibliographic data (`papers`; Incisive `lit/cache.rs`).

No user linkage. Rows are served for 30 days and pruned after 90. The table has no source
column, so a cached paper always comes back with source "pubmed".
"""

from collections.abc import Sequence

from psycopg_pool import ConnectionPool

from pubmedgemini.lit.design import design_label
from pubmedgemini.lit.paper import Paper
from pubmedgemini.lit.text import clean_text

FRESH_DAYS = 30  # rows older than this are not served
PRUNE_DAYS = 90  # rows older than this are deleted by prune()

def _clean(s: str | None) -> str | None:
    return clean_text(s) if s is not None else None


_COLUMNS = "pmid, pmcid, doi, title, authors, journal, year, pub_types, abstract, retracted"


def _papers(rows) -> list[Paper]:
    return [
        Paper(
            pmid=pmid,
            pmcid=pmcid,
            doi=doi,
            title=title,
            authors=list(authors),
            journal=journal,
            year=year,
            pub_types=list(pub_types),
            abstract=abstract,
            retracted=retracted,
            source="pubmed",
        )
        for pmid, pmcid, doi, title, authors, journal, year, pub_types, abstract, retracted in rows
    ]


def get_many(pool: ConnectionPool, pmids: Sequence[str]) -> list[Paper]:
    """Fresh cached papers for the given PMIDs. Unknown and stale ids are absent."""
    if not pmids:
        return []
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM papers "
            "WHERE pmid = ANY(%s) AND fetched_at > now() - make_interval(days => %s)",
            (list(pmids), FRESH_DAYS),
        ).fetchall()
    return _papers(rows)


def get_for_display(pool: ConnectionPool, pmids: Sequence[str]) -> dict[str, Paper]:
    """Cached papers for the given PMIDs at any age, by PMID: the run page of an old run.
    Rows are pruned after PRUNE_DAYS; a pruned or unknown id is absent."""
    if not pmids:
        return {}
    with pool.connection() as conn:
        rows = conn.execute(
            f"SELECT {_COLUMNS} FROM papers WHERE pmid = ANY(%s)", (list(pmids),)
        ).fetchall()
    return {p.pmid: p for p in _papers(rows) if p.pmid is not None}


def put_many(pool: ConnectionPool, papers: Sequence[Paper]) -> None:
    """Upserts papers in one transaction and stamps them as fetched now. A refresh never
    erases: a missing doi, pmcid or abstract keeps the stored value, and `retracted` is
    sticky once set. Papers without a PMID have no key and are skipped. Every text is
    cleaned first (`text.clean_text`: no NUL, no lone surrogate), so one odd character
    cannot fail the whole batch."""
    with pool.connection() as conn, conn.transaction():
        for p in papers:
            pmid = p.pmid.strip() if p.pmid else ""
            if not pmid:
                continue
            conn.execute(
                "INSERT INTO papers (pmid, pmcid, doi, title, authors, journal, year, pub_types,"
                " design, abstract, retracted, fetched_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())"
                " ON CONFLICT (pmid) DO UPDATE SET"
                " pmcid = COALESCE(EXCLUDED.pmcid, papers.pmcid),"
                " doi = COALESCE(EXCLUDED.doi, papers.doi),"
                " title = EXCLUDED.title, authors = EXCLUDED.authors,"
                " journal = EXCLUDED.journal, year = EXCLUDED.year,"
                " pub_types = EXCLUDED.pub_types, design = EXCLUDED.design,"
                " abstract = COALESCE(EXCLUDED.abstract, papers.abstract),"
                " retracted = EXCLUDED.retracted OR papers.retracted,"
                " fetched_at = now()",
                (
                    pmid,
                    _clean(p.pmcid),
                    _clean(p.doi),
                    clean_text(p.title),
                    [clean_text(a) for a in p.authors],
                    _clean(p.journal),
                    p.year,
                    [clean_text(t) for t in p.pub_types],
                    design_label(p.title, p.pub_types),
                    _clean(p.abstract),
                    p.retracted,
                ),
            )


def prune(pool: ConnectionPool) -> int:
    """Deletes rows fetched more than 90 days ago. Returns how many."""
    with pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM papers WHERE fetched_at < now() - make_interval(days => %s)",
            (PRUNE_DAYS,),
        )
        return cur.rowcount
