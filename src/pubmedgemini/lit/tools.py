"""The four literature tools of Incisive Quick (Incisive `agent/tools/lit.rs` `LitTools`).

`search_pubmed`, `search_europepmc`, `get_abstracts` and `find_related`, each a plain method
with typed results, so F2's researcher can offer them to the model unchanged. Every input is
checked before any request is sent. Errors carry fixed texts, never the input or a URL. Log
lines hold the tool name, counts and fixed kinds only. The paper cache is best effort: a read
error is a miss and a write error is logged; neither fails a tool.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from pubmedgemini.lit.cache import get_many, put_many
from pubmedgemini.lit.europepmc import EuropePmc
from pubmedgemini.lit.http import LitError
from pubmedgemini.lit.paper import Paper
from pubmedgemini.lit.pubmed import PubMed
from pubmedgemini.lit.query import QuerySource, query_rejected_message, validate_query

log = logging.getLogger("pubmedgemini.lit.tools")

MAX_SEARCH_RESULTS = 50  # most results a search tool returns
MAX_ABSTRACTS = 20  # most PMIDs one get_abstracts call takes
MAX_RELATED = 30  # most neighbours find_related returns

NOTHING_USABLE = "the database returned nothing usable for this input"
UNREACHABLE = "the database could not be reached; try again later or use another tool"
BAD_PMIDS = f"`pmids` must be a list of 1 to {MAX_ABSTRACTS} PubMed ids (digits only)"
BAD_PMID = "`pmid` must be a PubMed id (1 to 9 digits)"


class ToolInputError(Exception):
    """The input was empty, out of range or malformed. The text never repeats the input."""

    kind = "input"


class ToolFailed(Exception):
    """The lookup failed. `error_kind` is the LitError kind, for logs; the text is fixed."""

    kind = "failed"

    def __init__(self, message: str, error_kind: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


def valid_pmid(s: str) -> bool:
    """1 to 9 ASCII digits (Incisive `valid_pmid`)."""
    return 1 <= len(s) <= 9 and all(c in "0123456789" for c in s)


# ---------- result shapes ----------


@dataclass(frozen=True)
class Item:
    """One search hit (Incisive `Item`)."""

    pmid: str | None
    title: str
    year: int | None
    journal: str | None
    pub_types: tuple[str, ...]
    pmcid: str | None = None
    doi: str | None = None
    retracted: bool = False

    @classmethod
    def of(cls, p: Paper, ids: bool) -> "Item":
        return cls(
            pmid=p.pmid,
            title=p.title,
            year=p.year,
            journal=p.journal,
            pub_types=tuple(p.pub_types),
            pmcid=p.pmcid if ids else None,
            doi=p.doi if ids else None,
            retracted=p.retracted,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "pmid": self.pmid,
            "title": self.title,
            "year": self.year,
            "journal": self.journal,
            "pub_types": list(self.pub_types),
        }
        if self.pmcid is not None:
            d["pmcid"] = self.pmcid
        if self.doi is not None:
            d["doi"] = self.doi
        if self.retracted:
            d["retracted"] = True
        return d


@dataclass(frozen=True)
class SearchResult:
    count: int  # the database's total for a search; the items returned for find_related
    items: tuple[Item, ...]

    def to_json(self) -> str:
        return json.dumps({"count": self.count, "items": [i.to_dict() for i in self.items]})


@dataclass(frozen=True)
class AbstractRecord:
    pmid: str
    title: str
    journal: str | None
    year: int | None
    abstract: str | None
    pub_types: tuple[str, ...]
    doi: str | None
    pmcid: str | None
    retracted: bool


@dataclass(frozen=True)
class AbstractsResult:
    papers: tuple[AbstractRecord, ...]  # in the order asked
    not_found: tuple[str, ...]
    cached: int  # how many came from the cache; for logs only, not in the JSON
    # esummary or efetch failed for some ids, so abstracts may be missing. For the page
    # and logs only, not in the JSON (a difference from Incisive, which only logs it).
    fetch_failed: bool = False

    def to_json(self) -> str:
        papers = []
        for p in self.papers:
            d = asdict(p)
            d["pub_types"] = list(p.pub_types)
            papers.append(d)
        return json.dumps({"papers": papers, "not_found": list(self.not_found)})


# ---------- input checks (Incisive `string_field`, `count_field`) ----------


def _string(value: object, key: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"`{key}` must be a non-empty string")
    return value.strip()


def _count(value: object, key: str, cap: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= cap:
        raise ToolInputError(f"`{key}` must be a whole number from 1 to {cap}")
    return value


def _query(value: object, max_results: object, cap: int, source: QuerySource) -> tuple[str, int]:
    """Incisive's order: the query is a string, the count is in range, then the query rule."""
    query = _string(value, "query")
    n = _count(max_results, "max_results", cap)
    kind = validate_query(query, source)
    if kind is not None:
        raise ToolInputError(query_rejected_message(kind, source))
    return query, n


def _pmids(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= MAX_ABSTRACTS:
        raise ToolInputError(BAD_PMIDS)
    out: list[str] = []
    for v in value:
        if not isinstance(v, str) or not valid_pmid(v.strip()):
            raise ToolInputError(BAD_PMIDS)
        if v.strip() not in out:
            out.append(v.strip())
    return out


# ---------- the tools ----------


class LitTools:
    """The four tools over the PubMed and Europe PMC clients and the paper cache."""

    def __init__(self, pubmed: PubMed, epmc: EuropePmc, pool: ConnectionPool) -> None:
        self._pubmed = pubmed
        self._epmc = epmc
        self._pool = pool

    def __repr__(self) -> str:
        return "LitTools()"

    def _failed(self, tool: str, e: LitError) -> ToolFailed:
        """Logs the kind and gives the fixed text (Incisive `LitTools::failed`)."""
        log.warning("lit tool=%s error_kind=%s", tool, e.kind)
        if (e.kind == "http" and e.status == 404) or e.kind == "parse":
            return ToolFailed(NOTHING_USABLE, e.kind)
        return ToolFailed(UNREACHABLE, e.kind)

    def _cached(self, pmids: Sequence[str]) -> list[Paper]:
        """Fresh cached papers; a cache error is a miss (Incisive `LitTools::cached`)."""
        try:
            return get_many(self._pool, pmids)
        except psycopg.Error:
            log.warning("lit kind=cache_read_failed")
            return []

    def _store(self, papers: Sequence[Paper]) -> None:
        """Writes papers with a valid PMID to the cache; any failure is logged only, with a
        fixed kind (the exception's text can hold a title)."""
        valid = [p for p in papers if p.pmid is not None and valid_pmid(p.pmid)]
        if not valid:
            return
        # Any failure is caught, so one odd paper never fails a tool; KeyboardInterrupt and
        # SystemExit are not Exceptions and still propagate. The text is not logged.
        try:
            put_many(self._pool, valid)
        except Exception:  # noqa: BLE001
            log.warning("lit kind=cache_write_failed")

    def _summaries(self, pmids: Sequence[str]) -> list[Paper]:
        """Summaries for `pmids`, cache first, in `pmids` order (Incisive `summaries`)."""
        found = {p.pmid: p for p in self._cached(pmids)}
        missing = [i for i in pmids if i not in found]
        if missing:
            for p in self._pubmed.summaries(missing):
                if p.pmid is not None:
                    found.setdefault(p.pmid, p)
        return [found[i] for i in pmids if i in found]

    # The page needs whole papers (DOIs to merge on, Europe PMC's abstracts); the model tools
    # below return the same search as Items. One checked path serves both.

    def search_pubmed_papers(self, query: object, max_results: object) -> tuple[int, list[Paper]]:
        """search_pubmed's work: the total count and the papers, in relevance order."""
        tool = "search_pubmed"
        q, n = _query(query, max_results, MAX_SEARCH_RESULTS, "pubmed")
        try:
            hits = self._pubmed.search(q, n)
            papers = self._summaries(list(hits.pmids))
        except LitError as e:
            raise self._failed(tool, e) from None
        log.info("lit tool=%s count=%d items=%d", tool, hits.count, len(papers))
        return hits.count, papers

    def search_europepmc_papers(
        self, query: object, max_results: object
    ) -> tuple[int, list[Paper]]:
        """search_europepmc's work: the total count and at most `max_results` papers."""
        tool = "search_europepmc"
        q, n = _query(query, max_results, MAX_SEARCH_RESULTS, "europepmc")
        try:
            hits = self._epmc.search(q, n)
        except LitError as e:
            raise self._failed(tool, e) from None
        log.info("lit tool=%s count=%d items=%d", tool, hits.count, len(hits.papers))
        return hits.count, list(hits.papers[:n])

    def search_pubmed(self, query: object, max_results: object) -> SearchResult:
        """PubMed search: the total count and items without pmcid or doi."""
        count, papers = self.search_pubmed_papers(query, max_results)
        return SearchResult(count=count, items=tuple(Item.of(p, ids=False) for p in papers))

    def search_europepmc(self, query: object, max_results: object) -> SearchResult:
        """Europe PMC search: the total count and items with pmcid and doi when present."""
        count, papers = self.search_europepmc_papers(query, max_results)
        return SearchResult(count=count, items=tuple(Item.of(p, ids=True) for p in papers))

    def _fetch_papers(
        self, pmids: list[str], tool: str
    ) -> tuple[dict[str, Paper], int, bool]:
        """Incisive `LitTools::fetch_papers`: the cache first, then esummary and efetch for
        the rest; what efetch returned is cached. Returns the papers by PMID, how many came
        from the cache, and whether a lookup failed. Raises LitError only when esummary
        failed and nothing was cached."""
        papers = {p.pmid: p for p in self._cached(pmids) if p.pmid in pmids}
        cached = len(papers)
        missing = [i for i in pmids if i not in papers]
        if not missing:
            return papers, cached, False
        try:
            summaries = self._pubmed.summaries(missing)
        except LitError as e:
            if not papers:
                raise
            self._failed(tool, e)
            return papers, cached, True
        # Only the ids asked for (a reply may carry others), once each.
        wanted: dict[str, Paper] = {}
        for p in summaries:
            if p.pmid in missing and p.pmid not in wanted:
                wanted[p.pmid] = p
        if not wanted:
            return papers, cached, False
        try:
            details = self._pubmed.details(list(wanted))
        except LitError as e:
            self._failed(tool, e)  # summaries without abstracts; not cached
            papers.update(wanted)
            return papers, cached, True
        fetched: list[Paper] = []
        for pmid, p in wanted.items():
            rec = details.get(pmid)
            if rec is not None:
                if p.abstract is None:
                    p.abstract = rec.abstract
                if not p.pub_types:
                    p.pub_types = list(rec.pub_types)
                p.retracted = p.retracted or rec.retracted
                fetched.append(p)
            papers[pmid] = p
        self._store(fetched)
        return papers, cached, False

    def get_abstracts(self, pmids: object) -> AbstractsResult:
        """Abstracts for 1 to 20 PMIDs, in the order asked; unknown ids in `not_found`."""
        tool = "get_abstracts"
        ids = _pmids(pmids)
        try:
            papers, cached, fetch_failed = self._fetch_papers(ids, tool)
        except LitError as e:
            raise self._failed(tool, e) from None
        out: list[AbstractRecord] = []
        not_found: list[str] = []
        for pmid in ids:
            p = papers.get(pmid)
            if p is None:
                not_found.append(pmid)
                continue
            out.append(
                AbstractRecord(
                    pmid=pmid,
                    title=p.title,
                    journal=p.journal,
                    year=p.year,
                    abstract=p.abstract,
                    pub_types=tuple(p.pub_types),
                    doi=p.doi,
                    pmcid=p.pmcid,
                    retracted=p.retracted,
                )
            )
        log.info("lit tool=%s asked=%d cached=%d returned=%d", tool, len(ids), cached, len(out))
        return AbstractsResult(
            papers=tuple(out), not_found=tuple(not_found), cached=cached, fetch_failed=fetch_failed
        )

    def find_related(self, pmid: object, max_results: object) -> SearchResult:
        """PubMed's related articles for one PMID, best first; `count` is the items returned."""
        tool = "find_related"
        p = _string(pmid, "pmid")
        if not valid_pmid(p):
            raise ToolInputError(BAD_PMID)
        n = _count(max_results, "max_results", MAX_RELATED)
        try:
            ids = self._pubmed.related(p, n)
            papers = self._summaries(ids) if ids else []
        except LitError as e:
            raise self._failed(tool, e) from None
        log.info("lit tool=%s count=%d items=%d", tool, len(papers), len(papers))
        items = tuple(Item.of(x, ids=False) for x in papers)
        return SearchResult(count=len(items), items=items)
