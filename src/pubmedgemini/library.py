"""F9: the Library like Incisive's (frozen mock v3 + PDF, owner, 21 Nov 2026).

The rules are Incisive's (`crates/incisive-app/src/library.rs`): a card per run, runs of the same
question (case and spaces ignored) grouped with the newest shown, groups placed under the IST day
of their newest run, a verdict (the at-a-glance verdict, else the bottom line's first sentence,
citations removed), the certainty as four pips and a four-segment spine, and a meta line.
Nothing here asks Gemini; every word is the stored answer or fixed text.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from pubmedgemini.answer_page import (
    answer_view,
    first_sentence,
    model_text_cleaned,
    outcome_cards,
    pieces_text,
    refs_of,
    strip_citations,
)
from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.writer.answer import AnswerInvalid, read_body

IST = ZoneInfo("Asia/Kolkata")
LEVELS = {"High": 4, "Moderate": 3, "Low": 2, "Very low": 1}
NAMES = {v: k for k, v in LEVELS.items()}
ACTIVE = ("queued", "running")


@dataclass(frozen=True)
class Card:
    id: UUID
    question: str
    status: str
    created_at: datetime
    duration_s: int | None
    verdict: str | None
    certainty: int | None  # 1 Very low … 4 High
    references: int | None

    @property
    def has_answer(self) -> bool:
        return self.verdict is not None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE


@dataclass
class Group:
    runs: list[Card] = field(default_factory=list)

    @property
    def newest(self) -> Card:
        return self.runs[0]


def certainty_level(c: Any) -> int | None:
    return LEVELS.get(rust_trim(str(c))) if c is not None else None


def certainty_name(level: int | None) -> str:
    return NAMES.get(level or 0, "")


def pips(level: int) -> str:
    """`●●●○` for a level out of 4."""
    return "".join("●" if i <= level else "○" for i in range(1, 5))


def spine(level: int | None) -> list[bool]:
    """The spine's four segments, top first: True where filled (filled from the bottom)."""
    lv = level or 0
    return [lv >= 4, lv >= 3, lv >= 2, lv >= 1]


def no_answer_text(status: str) -> str:
    return {
        "queued": "Waiting to start…",
        "running": "Working on it…",
        "failed": "Didn’t finish — no answer",
        "stopped": "Stopped — no answer",
    }.get(status, "Ended without an answer")


def short_duration(s: int) -> str:
    return f"{(s + 30) // 60} min" if s >= 60 else f"{s} s"


def time_ist(t: datetime) -> str:
    return t.astimezone(IST).strftime("%H:%M")


def ist_day(t: datetime) -> date:
    return t.astimezone(IST).date()


def meta_line(c: Card) -> str:
    parts = [time_ist(c.created_at)]
    if c.duration_s is not None:
        parts.append(short_duration(c.duration_s))
    if c.references is not None:
        parts.append(f"{c.references} {'reference' if c.references == 1 else 'references'}")
    return " · ".join(parts)


def day_heading(day: date, today: date) -> str:
    short = f"{day:%a} {day.day} {day:%b}" + ("" if day.year == today.year else f" {day.year}")
    if day == today:
        return f"Today · {short}"
    if day == today - timedelta(days=1):
        return f"Yesterday · {short}"
    return short


def day_summary(groups: Sequence[Group]) -> str:
    q = len(groups)
    r = sum(len(g.runs) for g in groups)
    return (f"{q} {'question' if q == 1 else 'questions'} · "
            f"{r} {'run' if r == 1 else 'runs'}")


def normalise_question(q: str) -> str:
    return " ".join(q.split()).casefold()


def _answer_parts(body: Any) -> tuple[str | None, int | None, int | None]:
    """(verdict, certainty level, reference count) of a stored body; Nones when unreadable or
    empty (`AnswerInvalid` is not raised to the Library: that card shows its status line)."""
    if body is None:
        return None, None, None
    try:
        b = model_text_cleaned(read_body(body))
    except (AnswerInvalid, ValueError):
        return None, None, None
    w = b.answer
    if w is None:
        return None, None, None
    verdict = None
    if w.at_a_glance is not None:
        verdict = strip_citations(w.at_a_glance.verdict) or None
    if verdict is None:
        verdict = first_sentence(w.bottom_line.text) or None
    if verdict is None:
        return None, None, None
    return verdict, certainty_level(w.bottom_line.certainty), len(b.references)


def card_from_row(row: Sequence[Any]) -> Card:
    """A row of `runs.list_library`: id, question, status, created, started, finished, body."""
    rid, question, status, created, started, finished, body = row
    duration = None
    if started is not None and finished is not None and status not in ACTIVE:
        duration = max(0, int((finished - started).total_seconds()))
    verdict, level, refs = _answer_parts(body)
    return Card(rid, question, status, created, duration, verdict, level, refs)


def group_runs(cards: Sequence[Card]) -> list[Group]:
    """Runs of the same question grouped, newest run first in each and groups in the order of
    their newest run."""
    ordered = sorted(cards, key=lambda c: (-c.created_at.timestamp(), str(c.id)))
    index: dict[str, Group] = {}
    groups: list[Group] = []
    for c in ordered:
        k = normalise_question(c.question)
        if k not in index:
            index[k] = Group()
            groups.append(index[k])
        index[k].runs.append(c)
    return groups


@dataclass
class Day:
    heading: str
    summary: str
    groups: list[Group]


def days_of(groups: Sequence[Group], now: datetime) -> list[Day]:
    today = ist_day(now)
    out: list[Day] = []
    for g in groups:
        d = ist_day(g.newest.created_at)
        h = day_heading(d, today)
        if not out or out[-1].heading != h:
            out.append(Day(h, "", []))
        out[-1].groups.append(g)
    for day in out:
        day.summary = day_summary(day.groups)
    return out


def thread_time(r: Card, newest: Card) -> str:
    d = ist_day(r.created_at)
    t = time_ist(r.created_at)
    return t if d == ist_day(newest.created_at) else f"{d.day} {d:%b} {t}"


def copy_text(question: str, body: Any) -> str | None:
    """The answer as plain text for Copy: question, verdict, bottom line with certainty,
    outcomes, clinical practice and references. None when there is no readable answer."""
    try:
        stored = read_body(body)
    except (AnswerInvalid, ValueError):
        return None
    view = answer_view(stored)
    if not view["answer"]:
        return None
    clean = model_text_cleaned(stored)
    cards = outcome_cards(clean.answer, refs_of(clean))
    out = [question.strip(), ""]
    g = view.get("glance")
    if g and g["verdict"]:
        out += [pieces_text(g["verdict"]), ""]
    b = view["bottom"]
    out.append(f"Bottom line ({b['certainty']} certainty)")
    out += [pieces_text(p) for p in b["paragraphs"]]
    out.append("")
    for c in cards:
        out.append(f"{c.title} ({c.certainty})")
        out += [pieces_text(p) for p in c.effect_paragraphs]
        out.append("")
    if view["practice"]:
        out.append("Clinical practice")
        out += [(f"{label}: " if label else "") + pieces_text(t) for label, t in view["practice"]]
        out.append("")
    if view["references"]:
        out.append("References")
        out += [f"{r.n}. {r.text} PMID {r.pmid}" for r in view["references"]]
        out.append("")
    out.append("PubMed Ai · decision support from abstracts; the clinician decides.")
    return "\n".join(out)
