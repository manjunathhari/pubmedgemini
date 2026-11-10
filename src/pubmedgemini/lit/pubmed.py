"""PubMed through NCBI E-utilities: esearch, esummary, efetch and elink (Incisive `lit/pubmed.rs`).

Every request carries `db=pubmed` and `tool=pubmedgemini`, plus `email` and `api_key` only
when set. Parameters go through httpx's encoding; no URL is joined by hand. Nothing here logs.
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pubmedgemini.lit.http import NCBI_HOST, LitClient, LitError
from pubmedgemini.lit.paper import Paper

EUTILS = f"https://{NCBI_HOST}/entrez/eutils/"
TOOL = "pubmedgemini"
RETRACTED_TYPE = "retracted publication"
SUMMARY_BATCH = 100  # ids per esummary request
FETCH_BATCH = 50  # ids per efetch request
MAX_HITS = 1000  # upper bound on esearch retmax

_DIGITS = frozenset("0123456789")


@dataclass(frozen=True)
class SearchHits:
    count: int  # total matches in PubMed (may exceed len(pmids))
    pmids: tuple[str, ...]


@dataclass(frozen=True)
class FetchedRecord:
    """What efetch tells us about one record."""

    pmid: str
    abstract: str | None  # sections as `LABEL: text`, a blank line between them
    pub_types: tuple[str, ...]
    retracted: bool  # pub type "Retracted Publication" or a RetractionIn comment


def _ascii_lower(s: str) -> str:
    return "".join(c.lower() if c.isascii() else c for c in s)


def _is_retracted_type(t: str) -> bool:
    return _ascii_lower(t.strip()) == RETRACTED_TYPE


def _chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class PubMed:
    def __init__(self, client: LitClient, api_key: str | None, email: str | None) -> None:
        self._client = client
        self._api_key = api_key if api_key and api_key.strip() else None
        self._email = email if email and email.strip() else None

    def __repr__(self) -> str:
        return f"PubMed(api_key={self._api_key is not None}, email={self._email is not None})"

    def _params(self, extra: Mapping[str, str]) -> dict[str, str]:
        params = {"db": "pubmed", **extra, "tool": TOOL}
        if self._email is not None:
            params["email"] = self._email
        if self._api_key is not None:
            params["api_key"] = self._api_key
        return params

    def search(self, query: str, max_hits: int) -> SearchHits:
        """esearch, relevance-sorted; `max_hits` clamped to 1..1000."""
        retmax = str(min(max(int(max_hits), 1), MAX_HITS))
        params = {"term": query, "retmax": retmax, "sort": "relevance", "retmode": "json"}
        return parse_esearch(self._client.get_json(EUTILS + "esearch.fcgi", self._params(params)))

    def summaries(self, pmids: Sequence[str]) -> list[Paper]:
        """esummary. Ids PubMed does not know are absent from the result."""
        out: list[Paper] = []
        for chunk in _chunks(list(pmids), SUMMARY_BATCH):
            params = self._params({"id": ",".join(chunk), "retmode": "json"})
            out.extend(parse_esummary(self._client.get_json(EUTILS + "esummary.fcgi", params), chunk))
        return out

    def details(self, pmids: Sequence[str]) -> dict[str, FetchedRecord]:
        """efetch details per PMID. Ids PubMed does not know are absent."""
        out: dict[str, FetchedRecord] = {}
        for chunk in _chunks(list(pmids), FETCH_BATCH):
            params = self._params({"id": ",".join(chunk), "rettype": "abstract", "retmode": "xml"})
            for rec in parse_efetch(self._client.get_text(EUTILS + "efetch.fcgi", params)):
                out[rec.pmid] = rec
        return out

    def related(self, pmid: str, max_ids: int) -> list[str]:
        """PubMed's related articles, best first, without `pmid`, at most `max_ids`."""
        params = self._params({
            "dbfrom": "pubmed",
            "id": pmid,
            "cmd": "neighbor_score",
            "linkname": "pubmed_pubmed",
            "retmode": "json",
        })
        return parse_elink(self._client.get_json(EUTILS + "elink.fcgi", params), pmid, max_ids)


# ---------- esearch / esummary ----------


def parse_esearch(body: Any) -> SearchHits:
    r = body.get("esearchresult") if isinstance(body, dict) else None
    if not isinstance(r, dict) or "ERROR" in r:
        raise LitError("parse") from None
    count = r.get("count")
    if not isinstance(count, str) or not count.strip() or not set(count.strip()) <= _DIGITS:
        raise LitError("parse") from None
    idlist = r.get("idlist")
    if idlist is None:
        idlist = []
    if not isinstance(idlist, list) or not all(isinstance(i, str) for i in idlist):
        raise LitError("parse") from None
    return SearchHits(count=int(count.strip()), pmids=tuple(idlist))


def _leading_year(s: str) -> int | None:
    digits = ""
    for c in s:
        if c not in _DIGITS:
            break
        digits += c
    return int(digits) if len(digits) == 4 else None


def _extract_pmcid(v: str) -> str | None:
    """"PMC123" or the older "pmc-id: PMC123;" form -> "PMC123"."""
    start = v.find("PMC")
    if start < 0:
        return None
    out = "PMC"
    for c in v[start + 3 :]:
        if c not in _DIGITS:
            break
        out += c
    return out if len(out) > 3 else None


def parse_esummary(body: Any, requested: Sequence[str]) -> list[Paper]:
    """Papers in the order NCBI listed them. Entries with an `error` field (unknown ids)
    or no usable title are skipped."""
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        raise LitError("parse") from None
    uids_raw = result.get("uids")
    if isinstance(uids_raw, list):
        uids = [u for u in uids_raw if isinstance(u, str)]
    else:
        uids = list(requested)  # some error replies have no uids
    papers: list[Paper] = []
    for uid in uids:
        rec = result.get(uid)
        if not isinstance(rec, dict) or "error" in rec:
            continue

        def text(key: str, rec: dict = rec) -> str | None:
            v = rec.get(key)
            return v.strip() or None if isinstance(v, str) else None

        def article_id(kind: str, rec: dict = rec) -> str | None:
            ids = rec.get("articleids")
            if not isinstance(ids, list):
                return None
            for a in ids:
                if isinstance(a, dict) and a.get("idtype") == kind:
                    v = a.get("value")
                    return v.strip() or None if isinstance(v, str) else None
            return None

        title = text("title")
        if title is None:
            continue
        pubtype = rec.get("pubtype")
        pub_types = [t for t in pubtype if isinstance(t, str)] if isinstance(pubtype, list) else []
        authors_raw = rec.get("authors")
        authors: list[str] = []
        if isinstance(authors_raw, list):
            for a in authors_raw:
                name = a.get("name") if isinstance(a, dict) else None
                if isinstance(name, str) and name.strip():
                    authors.append(name.strip())
        pubdate, epubdate = text("pubdate"), text("epubdate")
        year = _leading_year(pubdate) if pubdate else None
        if year is None and epubdate:
            year = _leading_year(epubdate)
        pmcid = None
        for v in (article_id("pmc"), article_id("pmcid")):
            if v is not None and (pmcid := _extract_pmcid(v)) is not None:
                break
        papers.append(
            Paper(
                pmid=uid,
                pmcid=pmcid,
                doi=article_id("doi"),
                title=title,
                authors=authors,
                journal=text("fulljournalname") or text("source"),
                year=year,
                pub_types=pub_types,
                abstract=None,
                retracted=any(_is_retracted_type(t) for t in pub_types),
                source="pubmed",
            )
        )
    return papers


# ---------- efetch XML ----------

# Any entity reference that is not a character reference.
_NAMED_ENTITY = re.compile(r"&([A-Za-z_][A-Za-z0-9._-]*);")
_XML_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})


def _neutralise_entities(xml: str) -> str:
    """PubMed's DTD defines entities ElementTree does not know, and it raises on them.
    Incisive keeps such a name visible (`&thinsp;` stays `&thinsp;`) and turns `&nbsp;`
    into a space; doing the same before parsing gives the same text."""

    def swap(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        if name == "nbsp":
            return " "
        return "&amp;" + name + ";"

    return _NAMED_ENTITY.sub(swap, xml)


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _collapse_ws(s: str) -> str:
    # ASCII whitespace only: thin and no-break spaces in the text are meaningful.
    return " ".join(w for w in re.split(r"[ \t\n\r]", s) if w)


def _attr(el: ET.Element, key: str) -> str | None:
    v = el.get(key)
    return v.strip() or None if v is not None else None


class _Building:
    def __init__(self) -> None:
        self.pmid: str | None = None
        self.sections: list[tuple[str | None, str]] = []
        self.pub_types: list[str] = []
        self.retraction_comment = False

    def visit(self, el: ET.Element, parent: str | None) -> None:
        name = _local(el.tag)
        if name == "PMID" and parent in ("MedlineCitation", "BookDocument"):
            value = _collapse_ws("".join(el.itertext()))
            if self.pmid is None and value:
                self.pmid = value
            return
        if name == "PublicationType" and parent == "PublicationTypeList":
            value = _collapse_ws("".join(el.itertext()))
            if value:
                self.pub_types.append(value)
            return
        if name == "AbstractText" and parent == "Abstract":
            # Inner elements (<i>, <sup>) give their text; their tags go.
            value = _collapse_ws("".join(el.itertext()))
            if value:
                self.sections.append((_attr(el, "Label"), value))
            return
        if name == "CommentsCorrections" and _attr(el, "RefType") == "RetractionIn":
            self.retraction_comment = True
        for child in el:
            self.visit(child, name)

    def finish(self) -> FetchedRecord | None:
        if self.pmid is None:
            return None
        abstract = None
        if self.sections:
            abstract = "\n\n".join(f"{lab}: {t}" if lab else t for lab, t in self.sections)
        retracted = self.retraction_comment or any(_is_retracted_type(t) for t in self.pub_types)
        return FetchedRecord(
            pmid=self.pmid, abstract=abstract, pub_types=tuple(self.pub_types), retracted=retracted
        )


def parse_efetch(xml: str) -> list[FetchedRecord]:
    """Records of an efetch `PubmedArticleSet`. Anything malformed is LitError("parse");
    records without a PMID are skipped."""
    try:
        root = ET.fromstring(_neutralise_entities(xml))
    except (ET.ParseError, ValueError):
        raise LitError("parse") from None
    records: list[FetchedRecord] = []
    for article_set in root.iter():
        if _local(article_set.tag) != "PubmedArticleSet":
            continue
        for article in article_set:
            if _local(article.tag) not in ("PubmedArticle", "PubmedBookArticle"):
                continue
            b = _Building()
            for child in article:
                b.visit(child, _local(article.tag))
            rec = b.finish()
            if rec is not None:
                records.append(rec)
    return records


# ---------- elink ----------


def _id_of(v: Any) -> str | None:
    """An id as a JSON string or a whole non-negative number."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return str(v)
    return None


def parse_elink(body: Any, pmid: str, max_ids: int) -> list[str]:
    """Neighbour ids from an elink JSON reply, in the order given (NCBI sorts by score).
    Only digit ids, never `pmid` itself, no repeats, at most `max_ids`."""
    if not isinstance(body, dict) or "ERROR" in body:
        raise LitError("parse") from None
    linksets = body.get("linksets")
    if not isinstance(linksets, list) or not linksets:
        raise LitError("parse") from None
    first = linksets[0]
    first = first if isinstance(first, dict) else {}
    if "ERROR" in first:
        raise LitError("parse") from None
    dbs = first.get("linksetdbs")
    dbs = dbs if isinstance(dbs, list) else []
    db = next(
        (d for d in dbs if isinstance(d, dict) and d.get("linkname") == "pubmed_pubmed"),
        dbs[0] if dbs else None,
    )
    links = db.get("links") if isinstance(db, dict) else None
    if not isinstance(links, list):
        return []
    out: list[str] = []
    for link in links:
        found = _id_of(link.get("id")) if isinstance(link, dict) else _id_of(link)
        if not found or not set(found) <= _DIGITS or found == pmid or found in out:
            continue
        out.append(found)
        if len(out) >= max_ids:
            break
    return out
