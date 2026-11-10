"""The page's search: both sources side by side, merged, with abstracts (F1, no model).

The text is checked for each source first; a source that refuses it is not run. The two
searches run at once on two threads through the same checked tools F2's researcher uses.
Nothing here logs the text, a title or an abstract.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from pubmedgemini.lit.design import design_label
from pubmedgemini.lit.paper import Paper, merge_results
from pubmedgemini.lit.query import QuerySource, validate_query
from pubmedgemini.lit.tools import LitTools, ToolFailed, ToolInputError, valid_pmid

# Each source is asked for 20 and the page shows the first 20 merged, so one get_abstracts
# call, at its cap of 20, covers every shown paper with a PMID.
PAGE_RESULTS = 20


@dataclass(frozen=True)
class ShownPaper:
    pmid: str | None  # only a valid PMID (1 to 9 digits)
    doi: str | None
    title: str
    journal: str | None
    year: int | None
    pub_types: tuple[str, ...]
    design: str
    abstract: str | None
    retracted: bool

    @property
    def paragraphs(self) -> list[str]:
        """The abstract split on blank lines (structured abstracts: one per section)."""
        if not self.abstract:
            return []
        return [p.strip() for p in self.abstract.split("\n\n") if p.strip()]

    @property
    def facts(self) -> str:
        """"Journal · Year · design label", leaving out what is missing."""
        parts = [self.journal or "", str(self.year) if self.year is not None else "", self.design]
        return " · ".join(p for p in parts if p)


@dataclass(frozen=True)
class PageResult:
    papers: tuple[ShownPaper, ...]
    pubmed_count: int | None  # None when PubMed was not searched or not reached
    epmc_count: int | None
    not_reached: tuple[str, ...]  # "pubmed" / "europepmc": searched but unreachable
    not_run: tuple[str, ...]  # refused the text (only PubMed, for an underscore)
    abstracts_failed: bool  # a fetch failed and at least one shown paper has no abstract
    cached: int


class QueryRejected(Exception):
    """Both sources refuse the text. `kind` is the query rule's fixed kind for `source`."""

    def __init__(self, kind: str, source: QuerySource) -> None:
        super().__init__(kind)
        self.kind = kind
        self.source = source


class BothUnreachable(Exception):
    """No source that was run could be reached."""


def _search(tools: LitTools, source: QuerySource, text: str) -> tuple[int, list[Paper]] | None:
    """One source's search; None when it could not be reached."""
    run = tools.search_pubmed_papers if source == "pubmed" else tools.search_europepmc_papers
    try:
        return run(text, PAGE_RESULTS)
    except (ToolFailed, ToolInputError):
        return None


def _shown(p: Paper) -> ShownPaper:
    pmid = p.pmid.strip() if p.pmid else None
    return ShownPaper(
        pmid=pmid if pmid and valid_pmid(pmid) else None,
        doi=p.doi.strip() if p.doi and p.doi.strip() else None,
        title=p.title,
        journal=p.journal,
        year=p.year,
        pub_types=tuple(p.pub_types),
        design=design_label(p.title, p.pub_types),
        abstract=p.abstract,
        retracted=p.retracted,
    )


def run_page_search(tools: LitTools, text: str) -> PageResult:
    """Searches PubMed and Europe PMC for `text` and returns what the page shows."""
    text = text.strip()
    kinds: dict[QuerySource, str | None] = {
        "pubmed": validate_query(text, "pubmed"),
        "europepmc": validate_query(text, "europepmc"),
    }
    accepted = [s for s, kind in kinds.items() if kind is None]
    if not accepted:
        raise QueryRejected(kinds["pubmed"] or "character", "pubmed")
    not_run = tuple(s for s, kind in kinds.items() if kind is not None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {s: pool.submit(_search, tools, s, text) for s in accepted}
        found = {s: f.result() for s, f in futures.items()}
    not_reached = tuple(s for s in accepted if found[s] is None)
    if len(not_reached) == len(accepted):
        raise BothUnreachable()

    pubmed = found.get("pubmed")
    epmc = found.get("europepmc")
    merged = merge_results(pubmed[1] if pubmed else [], epmc[1] if epmc else [])[:PAGE_RESULTS]

    pmids: list[str] = []
    for p in merged:
        pmid = p.pmid.strip() if p.pmid else ""
        if valid_pmid(pmid) and pmid not in pmids:
            pmids.append(pmid)
    fetch_failed = False
    cached = 0
    if pmids:
        # PubMed was just tried with all its retries; asking it again for abstracts would
        # only wait through another round. The cache and Europe PMC's abstracts serve.
        pubmed_down = "pubmed" in not_reached
        try:
            result = tools.cached_abstracts(pmids) if pubmed_down else tools.get_abstracts(pmids)
        except (ToolFailed, ToolInputError):
            fetch_failed = True
        else:
            cached = result.cached
            fetch_failed = result.fetch_failed
            records = {r.pmid: r for r in result.papers}
            for p in merged:
                rec = records.get(p.pmid.strip()) if p.pmid else None
                if rec is None:
                    continue
                # PubMed's values win, as in the merge; gaps are filled.
                if rec.abstract:
                    p.abstract = rec.abstract
                p.doi = p.doi or rec.doi
                p.journal = p.journal or rec.journal
                p.year = p.year if p.year is not None else rec.year
                p.pub_types = p.pub_types + [t for t in rec.pub_types if t not in p.pub_types]
                p.retracted = p.retracted or rec.retracted

    shown = tuple(_shown(p) for p in merged)
    return PageResult(
        papers=shown,
        pubmed_count=pubmed[0] if pubmed else None,
        epmc_count=epmc[0] if epmc else None,
        not_reached=not_reached,
        not_run=not_run,
        # The "titles only" line: only when a fetch failed and a shown paper has no abstract.
        abstracts_failed=fetch_failed and any(not p.abstract for p in shown),
        cached=cached,
    )
