"""F8: the stored answer as a PDF laid out as a short paper (frozen mock
`docs/mocks/f8-answer-pdf-v1.pdf`, owner, 20 Nov 2026).

Every word is the stored answer, already shaped by `answer_page` for the answer page, or fixed
text here and in `templates/paper.html`. Nothing is asked of Gemini and nothing is stored. Model
text reaches the paper only as escaped text (autoescape; no `|safe`), and positions reach SVG
attributes only through the `pct` filter (numbers only), as on the answer page.

Left out, by the owner's choice: the run's steps, the selected studies' abstracts, the studies
considered and not selected, possible contradictions and the completeness box. The paper ends
with the references and one closing line.
"""

import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pubmedgemini import intro
from pubmedgemini.answer_page import (
    answer_view,
    compare,
    fmt_pct,
    fmt_value,
    forest,
    model_text_cleaned,
    out_of_100,
    outcome_cards,
    pill_class,
    refs_of,
)
from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.lit.text import clean_text, drop_invisible
from pubmedgemini.runs import RunView
from pubmedgemini.writer.answer import read_body

HERE = Path(__file__).parent
STATIC = HERE / "static"

# The paper's sections in order; Introduction only when the run stored one, and the numbers
# close up when it did not.
SECTIONS = ("Summary", "Introduction", "Methods", "Results", "Discussion",
            "Conclusion and clinical practice", "References")
NUMBERED = ("Introduction", "Methods", "Results", "Discussion",
            "Conclusion and clinical practice")

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")


def _pct(x: object) -> str:
    """As `web._pct`: a position computed by code, never model text."""
    if isinstance(x, bool) or not isinstance(x, (float, int)):
        raise TypeError("pct takes a number")
    return fmt_pct(float(x))


_env = Environment(loader=FileSystemLoader(str(HERE / "templates")),
                   autoescape=select_autoescape(["html"], default_for_string=True,
                                                default=True))
_env.filters["pct"] = _pct
_env.filters["pill"] = pill_class


def long_date(d: datetime) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def pdf_name(question: str, day: str) -> str:
    """`PubMedAi-<day>-<first words of the question>.pdf`: ASCII letters, digits and hyphens
    only, at most six words, so the header can never carry a quote or a path."""
    plain = unicodedata.normalize("NFKD", question).encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", plain)[:6]
    slug = "-".join(words)[:60].strip("-") or "answer"
    return f"PubMedAi-{day}-{slug}.pdf"


def _section_numbers(has_intro: bool) -> dict[str, str]:
    names = [s for s in NUMBERED if has_intro or s != "Introduction"]
    out = {s: f"{i}. {s}" for i, s in enumerate(names, start=1)}
    return {s: out.get(s, s) for s in SECTIONS}


def paper_context(v: RunView) -> dict[str, Any] | None:
    """What the paper draws, or None when the run has no readable finished answer."""
    if v.status in ("queued", "running") or v.answer is None:
        return None
    body = read_body(v.answer)  # AnswerInvalid reaches the caller
    view = answer_view(body)
    if not view["answer"]:
        return None
    clean = model_text_cleaned(body)
    w, refs = clean.answer, refs_of(clean)
    f = forest(w, refs)
    intro_paras = intro.paragraphs(drop_invisible(clean_text(v.intro))) if v.intro else []
    when = v.finished_at or v.created_at
    return {
        "question_paragraphs": [x.strip() for x in v.question.split("\n") if x.strip()],
        "date": long_date(when),
        "model": v.model,
        "view": view,
        "intro_paragraphs": intro_paras,
        "h": _section_numbers(bool(intro_paras)),
        "searches": [(e.source, e.query, e.hits) for e in v.searches],
        "n_selected": len(v.selected),
        "n_considered": len(v.selected) + len(v.considered),
        "ratios": f.ratios,
        "ratio_axis": f.ratio_axis,
        "diffs": f.diffs,
        "studies": [{
            "ref": refs.get(rust_trim(s.pmid)),
            "label": rust_trim(s.label).rstrip("."),
            "design": rust_trim(s.design),
            "n": fmt_value(s.n) if s.n is not None and s.n > 0 else "–",
            "setting": rust_trim(s.setting),
            "comparison": rust_trim(s.comparison),
            "limits": rust_trim(s.appraisal).rstrip("."),
        } for s in w.studies],
        "outcomes": outcome_cards(w, refs),
        "out100": out_of_100(w, refs),
        "compare": compare(w, refs),
    }


def paper_html(v: RunView) -> str | None:
    ctx = paper_context(v)
    if ctx is None:
        return None
    return _env.get_template("paper.html").render(**ctx)


def render_pdf(html: str) -> bytes:
    """The paper as PDF bytes. Imported here so the app starts even before the library is
    needed; the fonts and stylesheet come from the app's own files (no outside fetch)."""
    import weasyprint

    def no_fetch(url: str, *args: Any, **kwargs: Any) -> dict:
        if not url.startswith(STATIC.resolve().as_uri() + "/"):
            raise ValueError("outside fetch refused")
        return weasyprint.default_url_fetcher(url, *args, **kwargs)

    css = weasyprint.CSS(filename=str(STATIC / "paper.css"), url_fetcher=no_fetch)
    return weasyprint.HTML(string=html, base_url=str(STATIC) + "/",
                           url_fetcher=no_fetch).write_pdf(stylesheets=[css])
