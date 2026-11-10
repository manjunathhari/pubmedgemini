"""One bibliographic record, and its identity across databases (Incisive `lit/mod.rs`)."""

from dataclasses import dataclass
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
