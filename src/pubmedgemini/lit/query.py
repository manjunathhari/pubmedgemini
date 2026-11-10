"""The query rule: what search text may be sent to PubMed or Europe PMC.

A port of Incisive `agent/search.rs` `validate_query`. A refusal is one fixed kind, so it
can be logged and shown without repeating the text.
"""

from typing import Literal

MAX_QUERY_CHARS = 1000
# Incisive's allowed punctuation, copied exactly (`agent/search.rs` `validate_query`,
# lines 95 to 101), besides ASCII letters and digits.
QUERY_PUNCTUATION = " -\"()[]*/:,.'"
# A deliberate difference from Incisive (owner decision 6, 10 Nov 2026): Europe PMC's own
# field names (PUB_TYPE:, PUB_YEAR:) need the underscore, which Incisive's rule above
# (`agent/search.rs` lines 95 to 101) refuses. PubMed keeps Incisive's exact rule.
EUROPEPMC_EXTRA_PUNCTUATION = "_"

QuerySource = Literal["pubmed", "europepmc"]


def _balanced(q: str) -> bool:
    """Double quotes, parentheses and square brackets; brackets inside quotes do not count."""
    open_: list[str] = []
    quoted = False
    for c in q:
        if c == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif c in "([":
            open_.append(c)
        elif c in ")]":
            opener = "(" if c == ")" else "["
            if not open_ or open_.pop() != opener:
                return False
    return not quoted and not open_


def validate_query(q: str, source: QuerySource) -> str | None:
    """None if `q` may be sent to `source`; else one of empty, too_long, newline, url,
    character, unbalanced (checked in that order, as Incisive)."""
    if not q.strip():
        return "empty"
    if len(q) > MAX_QUERY_CHARS:
        return "too_long"
    if "\n" in q or "\r" in q:
        return "newline"
    lower = "".join(c.lower() if c.isascii() else c for c in q)  # Rust to_ascii_lowercase
    if "http" in lower or "www." in lower:
        return "url"
    allowed = QUERY_PUNCTUATION
    if source == "europepmc":
        allowed += EUROPEPMC_EXTRA_PUNCTUATION
    # str.isalnum() alone accepts letters such as "é"; the rule is ASCII only.
    if not all((c.isascii() and c.isalnum()) or c in allowed for c in q):
        return "character"
    if not _balanced(q):
        return "unbalanced"
    return None


def query_rejected_message(kind: str, source: QuerySource) -> str:
    """Incisive's text (`agent/tools/lit.rs` `check_query`), naming `_` for Europe PMC."""
    extra = ", _" if source == "europepmc" else ""
    return (
        f"query rejected ({kind}): use 1 to {MAX_QUERY_CHARS} characters of letters, digits, "
        f"spaces and - \" ( ) [ ] * / : , . '{extra} with balanced quotes and brackets, "
        "no URLs or newlines"
    )
