"""One bibliographic record, and its identity across databases (Incisive `lit/mod.rs`)."""

from dataclasses import dataclass, replace
from typing import Literal

PaperSource = Literal["pubmed", "europepmc", "both"]


@dataclass
class Paper:
    """Public bibliographic data only; abstracts, never full text."""

    pmid: str | None
    pmcid: str | None
    doi: str | None
    title: str
    authors: list[str]
    journal: str | None
    year: int | None
    pub_types: list[str]
    abstract: str | None
    retracted: bool
    source: PaperSource


def paper_key(p: Paper) -> str:
    """The PMID, else the lower-case DOI, else `title:<lower-case title>` (Incisive
    `PaperId::key`), so a record with neither id still de-duplicates against itself."""
    if p.pmid and p.pmid.strip():
        return p.pmid.strip()
    if p.doi and p.doi.strip():
        return p.doi.strip().lower()
    return "title:" + p.title.strip().lower()


def _clean(s: str | None) -> str | None:
    return s.strip() or None if s is not None else None


def _doi_key(p: Paper) -> str | None:
    doi = _clean(p.doi)
    return doi.lower() if doi is not None else None


def _merge_into(base: Paper, other: Paper) -> None:
    """Fills `base`'s missing fields from `other`; `base` wins when both have a value
    (Incisive `merge_into`)."""

    def fill(a: str | None, b: str | None) -> str | None:
        if _clean(a) is None:
            return b if b is not None and b.strip() else None
        return a

    base.pmid = fill(base.pmid, other.pmid)
    base.pmcid = fill(base.pmcid, other.pmcid)
    base.doi = fill(base.doi, other.doi)
    base.journal = fill(base.journal, other.journal)
    base.abstract = fill(base.abstract, other.abstract)
    if not base.title.strip():
        base.title = other.title
    if not base.authors:
        base.authors = list(other.authors)
    if base.year is None:
        base.year = other.year
    for t in other.pub_types:
        if t not in base.pub_types:
            base.pub_types.append(t)
    base.retracted = base.retracted or other.retracted
    if base.source != other.source:
        base.source = "both"


class _Index:
    def __init__(self) -> None:
        self.pmid: dict[str, int] = {}
        self.doi: dict[str, int] = {}
        self.pmcid: dict[str, int] = {}

    def add(self, i: int, p: Paper) -> None:
        if (k := _clean(p.pmid)) is not None:
            self.pmid.setdefault(k, i)
        if (k := _doi_key(p)) is not None:
            self.doi.setdefault(k, i)
        if (k := _clean(p.pmcid)) is not None:
            self.pmcid.setdefault(k, i)

    def find(self, p: Paper, out: list[Paper]) -> int | None:
        """The record `p` duplicates: by PMID, then lower-case DOI, then PMCID. Two records
        that both have a PMID and differ are never the same (Incisive `Index::find`)."""
        pmid = _clean(p.pmid)
        if pmid is not None and pmid in self.pmid:
            return self.pmid[pmid]
        doi = _doi_key(p)
        candidate = self.doi.get(doi) if doi is not None else None
        if candidate is None and (pmcid := _clean(p.pmcid)) is not None:
            candidate = self.pmcid.get(pmcid)
        if candidate is None:
            return None
        theirs = _clean(out[candidate].pmid)
        if pmid is not None and theirs is not None and pmid != theirs:
            return None
        return candidate


def merge_results(pubmed: list[Paper], epmc: list[Paper]) -> list[Paper]:
    """PubMed and Europe PMC records, de-duplicated (Incisive `merge_results`). A merged
    record keeps PubMed's values, takes Europe PMC's where PubMed has none, unites the
    publication types, ORs `retracted` and has source "both". PubMed's order, then the
    Europe-PMC-only records in theirs. The inputs are not changed."""
    out: list[Paper] = []
    index = _Index()
    for p in [*pubmed, *epmc]:
        i = index.find(p, out)
        if i is None:
            index.add(len(out), p)
            out.append(replace(p, authors=list(p.authors), pub_types=list(p.pub_types)))
        else:
            _merge_into(out[i], p)
            index.add(i, out[i])
    return out
