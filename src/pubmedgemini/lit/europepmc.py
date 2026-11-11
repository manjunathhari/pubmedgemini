"""Europe PMC search (`resultType=core`) and its record mapping (Incisive `lit/europepmc.rs`).

Not paced (Incisive paces NCBI only). Nothing here logs.
"""

import re
from dataclasses import dataclass
from typing import Any

from pubmedgemini.lit.http import BASE_HOSTS, LitClient, LitError
from pubmedgemini.lit.paper import Paper

SEARCH_URL = f"https://{BASE_HOSTS[1]}/europepmc/webservices/rest/search"
MAX_PAGE = 100  # Europe PMC allows 1,000; a page of 100 is all we need
RETRACTED_TYPE = "retracted publication"
INLINE_TAGS = frozenset(
    {"i", "b", "u", "em", "strong", "sub", "sup", "italic", "bold", "span", "a"}
)


@dataclass(frozen=True)
class EpmcHits:
    count: int  # the API's hitCount; the page length when absent
    papers: tuple[Paper, ...]


class EuropePmc:
    def __init__(self, client: LitClient) -> None:
        self._client = client

    def __repr__(self) -> str:
        return "EuropePmc()"

    def search(self, query: str, max_hits: int) -> EpmcHits:
        """Core search, up to `max_hits` records (clamped to 1..100), with the total count.
        `query` must be Europe PMC syntax."""
        params = {
            "query": query,
            "resultType": "core",
            "format": "json",
            "pageSize": str(min(max(int(max_hits), 1), MAX_PAGE)),
        }
        body = self._client.get_json(SEARCH_URL, params)
        result_list = body.get("resultList") if isinstance(body, dict) else None
        if not isinstance(result_list, dict):
            raise LitError("parse") from None
        results = result_list.get("result", [])
        if not isinstance(results, list) or not all(isinstance(r, dict) for r in results):
            raise LitError("parse") from None
        papers = tuple(to_paper(r) for r in results)
        hit_count = body.get("hitCount")
        if isinstance(hit_count, int) and not isinstance(hit_count, bool) and hit_count >= 0:
            count = hit_count
        else:
            count = len(papers)
        return EpmcHits(count=count, papers=papers)


def _str(raw: dict, key: str) -> str | None:
    v = raw.get(key)
    return v if isinstance(v, str) else None


def _non_empty(s: str | None) -> str | None:
    return s.strip() or None if s is not None else None


def _dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _is_pmcid(s: str) -> bool:
    digits = s[3:]
    return s.startswith("PMC") and bool(digits) and all(c in "0123456789" for c in digits)


_I32 = re.compile(r"[+-]?[0-9]+")


def _parse_i32(s: str) -> int | None:
    """Rust `str::parse::<i32>`: an optional sign and ASCII digits, in range."""
    if not _I32.fullmatch(s):
        return None
    n = int(s)
    return n if -(2**31) <= n < 2**31 else None


def _year(raw: dict) -> int | None:
    pub_year = _str(raw, "pubYear")
    if pub_year is not None and (y := _parse_i32(pub_year.strip())) is not None:
        return y
    v = _dict(raw.get("journalInfo")).get("yearOfPublication")
    y = None
    if isinstance(v, int) and not isinstance(v, bool):
        y = v
    elif isinstance(v, str):
        y = _parse_i32(v.strip())
    if y is not None and 0 < y < 2**31:
        return y
    first = _str(raw, "firstPublicationDate")
    if first is not None and len(first) >= 4:
        return _parse_i32(first[:4])
    return None


def _authors(raw: dict) -> list[str]:
    listed = _dict(raw.get("authorList")).get("author")
    if isinstance(listed, list) and listed:
        names = (_non_empty(_str(_dict(a), "fullName")) for a in listed)
        return [n for n in names if n]
    author_string = _str(raw, "authorString")
    if author_string is None:
        return []
    return [a.strip() for a in author_string.rstrip(".").split(", ") if a.strip()]


def to_paper(raw: dict) -> Paper:
    """One Europe PMC core record as a Paper (Incisive `to_paper`, without the open-access link)."""
    # Records from the MEDLINE feed use the PMID as their id.
    pmid = _non_empty(_str(raw, "pmid"))
    if pmid is None and _str(raw, "source") == "MED":
        pmid = _non_empty(_str(raw, "id"))
    pmcid = _non_empty(_str(raw, "pmcid"))
    if pmcid is not None and not _is_pmcid(pmcid):
        pmcid = None
    types = _dict(raw.get("pubTypeList")).get("pubType")
    pub_types = [t for t in types if isinstance(t, str) and t.strip()] if isinstance(types, list) else []
    title = _str(raw, "title")
    abstract = _str(raw, "abstractText")
    abstract = strip_markup(abstract) if abstract is not None else None
    return Paper(
        pmid=pmid,
        pmcid=pmcid,
        doi=_non_empty(_str(raw, "doi")),
        title=(strip_markup(title) if title is not None else "").rstrip("."),
        authors=_authors(raw),
        journal=_non_empty(_str(_dict(_dict(raw.get("journalInfo")).get("journal")), "title")),
        year=_year(raw),
        pub_types=pub_types,
        abstract=abstract or None,
        retracted=any(_ascii_lower(t) == RETRACTED_TYPE for t in pub_types),
        source="europepmc",
    )


def _ascii_lower(s: str) -> str:
    return "".join(c.lower() if c.isascii() else c for c in s)


def strip_markup(s: str) -> str:
    """Removes markup from a title or abstract (Incisive `strip_markup`): inline tags vanish,
    other tags become a space, a `<` not starting a tag is kept, entities are decoded and
    whitespace is collapsed."""
    out: list[str] = []
    rest = s
    while (start := rest.find("<")) >= 0:
        out.append(rest[:start])
        after = rest[start + 1 :]
        end = after.find(">")
        if end < 0:
            out.append("<")
            rest = after
            continue
        tag = after[:end].lstrip("/")
        name = re.split(r"[\s/]", tag, maxsplit=1)[0]
        if name[:1].isascii() and name[:1].isalpha():
            if _ascii_lower(name) not in INLINE_TAGS:
                out.append(" ")
        else:
            # A stray "<" (as in "p < 0.05"): kept, with the text up to the next ">".
            out.append("<" + after[: end + 1])
        rest = after[end + 1 :]
    out.append(rest)
    return " ".join(_decode_entities("".join(out)).split())


_NAMED = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}
_HEX = re.compile(r"\+?[0-9A-Fa-f]+")
_DEC = re.compile(r"\+?[0-9]+")


def _char(n: int) -> str | None:
    """Rust `char::from_u32`: no surrogates, nothing past U+10FFFF (the entity is then kept as
    written). Unlike Incisive, code point 0 decodes to nothing: a NUL cannot be stored in
    Postgres, and this runs after the reply was cleaned (`text.clean_text`)."""
    if n > 0x10FFFF or 0xD800 <= n <= 0xDFFF:
        return None
    if n == 0:
        return ""
    return chr(n)


def _entity(name: str) -> str | None:
    if name in _NAMED:
        return _NAMED[name]
    if name.startswith(("#x", "#X")):
        digits = name[2:]
        return _char(int(digits, base=16)) if _HEX.fullmatch(digits) else None
    if name.startswith("#"):
        return _char(int(name[1:])) if _DEC.fullmatch(name[1:]) else None
    return None


def _decode_entities(s: str) -> str:
    out: list[str] = []
    rest = s
    while (i := rest.find("&")) >= 0:
        out.append(rest[:i])
        after = rest[i + 1 :]
        end = after.find(";")
        # Incisive looks at most 10 bytes ahead for the ";".
        decoded = _entity(after[:end]) if 0 <= end and len(after[:end].encode()) <= 10 else None
        if decoded is None:
            out.append("&")
            rest = after
        else:
            out.append(decoded)
            rest = after[end + 1 :]
    out.append(rest)
    return "".join(out)
