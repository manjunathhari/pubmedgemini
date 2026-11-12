"""Run rows: one per question, with its step lines, search trail, seen papers and study list.

Plain SQL over the tables of `migrations/0004_runs.sql` (Incisive `migrations/0005_runs.sql`
`runs`, `run_steps`, plus the trail, the seen papers and the list). One active run for the whole
app, enforced by the database (`runs_one_active`). Writes during a run are made at once, so the
page shows each line as it happens.

From Incisive (commit 12630f7, `crates/incisive-app/src/`): `clip` and `MAX_STEP_LINE`
(`agent/mod.rs`), a failed step write logged and ignored (`add_line`), the end messages
(`agent/answer.rs` `NO_EVIDENCE`, `STOPPED_NO_ANSWER`, `EMPTY_SELECTION`; `agent/team.rs`
`UNFETCHED`, `WRITER_TRUNCATED`, `WRITER_UNREADABLE`, `WRITER_NO_TIME`).

The written answer (F3) is one JSON document in `answers.body` (Incisive `run_answers.body`,
`AnswerV3`), stored only while its run is still running, so a run a restart stopped keeps
none. A run finished before F3 has no row and reads exactly as before.

The patient-details check (`deid.check_question`, ported from Incisive) runs in
`create_run`, the one place a question becomes a run: a blocked question raises
`QuestionBlocked` before anything is stored or sent.

Logs hold run ids, counts and fixed kinds only; never the question or any text of a run.
"""

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from pubmedgemini.deid import BlockedIdentifiers, check_question
from pubmedgemini.lit.cache import get_for_display
from pubmedgemini.lit.paper import Paper
from pubmedgemini.lit.text import clean_text
from pubmedgemini.researcher.loop import SearchEntry, SeenPaper
from pubmedgemini.researcher.studylist import Validated
from pubmedgemini.writer.answer import AnswerBody, body_from_json, body_to_json

log = logging.getLogger("pubmedgemini.runs")

MAX_STEP_LINE = 160  # agent/mod.rs MAX_STEP_LINE

NO_EVIDENCE = "No relevant evidence found."
STOPPED_NO_ANSWER = (
    "No answer could be written: the search stopped early, before enough evidence was read and "
    "checked."
)
EMPTY_SELECTION = (
    "No studies could be selected from the papers found. Please run the question again."
)
HARD_STOP_MESSAGE = "This run ran out of time (7 minutes). Please run the question again."
UNFETCHED = (
    "The selected studies' records could not be fetched, so no answer could be written."
)
WRITER_TRUNCATED = (
    "The answer was longer than the writer's output limit and could not be finished. The "
    "studies found are listed below; please ask again."
)
WRITER_UNREADABLE = (
    "The writer's answer could not be read. The studies found are listed below; please ask "
    "again."
)
WRITER_NO_TIME = (
    "Not enough of the time limit was left to write the answer. The studies found are listed "
    "below; please ask again."
)
# F2 runs closed with "Study list ready"; F3 runs close as Incisive's do (team.rs `finish`).
ANSWER_READY = "Answer ready"
DOWN_LINES = {
    "europepmc": "Europe PMC could not be reached; searched PubMed only",
    "pubmed": "PubMed could not be reached; searched Europe PMC only",
}

# Unicode White_Space, as Rust's split_whitespace uses it.
_WHITE_SPACE = frozenset(
    "\t\n\x0b\x0c\r \x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def _is_control(ch: str) -> bool:
    """Rust `char::is_control`: the Cc category."""
    o = ord(ch)
    return o < 0x20 or 0x7F <= o <= 0x9F


def clip(s: str, max_chars: int) -> str:
    """agent/mod.rs `clip`: control characters as spaces, runs of whitespace collapsed,
    trimmed, and cut to at most `max_chars` characters (an ellipsis marks a cut)."""
    spaced = "".join(" " if _is_control(ch) else ch for ch in s)
    words: list[str] = []
    cur: list[str] = []
    for ch in spaced:
        if ch in _WHITE_SPACE:
            if cur:
                words.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        words.append("".join(cur))
    clean = clean_text(" ".join(words))
    if len(clean) <= max_chars:
        return clean
    out = "".join(clean[: max(0, max_chars - 1)]).rstrip("".join(_WHITE_SPACE))
    return out + "…"


class ActiveRunExists(Exception):
    """A run is queued or running (one for the whole app). `run_id` is that run when it could
    be read, `own` whether it is the asking user's."""

    def __init__(self, run_id: UUID | None, own: bool) -> None:
        super().__init__("active_run_exists")
        self.run_id = run_id
        self.own = own


class QuestionBlocked(Exception):
    """The question holds details that could identify a patient. `outcome` carries the
    category names, their counts and the message to show; never the matched text. No run was
    created, nothing was stored and nothing was sent anywhere."""

    def __init__(self, outcome: BlockedIdentifiers) -> None:
        super().__init__("blocked_identifiers")
        self.outcome = outcome


@dataclass(frozen=True)
class StepRow:
    kind: str
    line: str


@dataclass(frozen=True)
class SelectedView:
    pmid: str
    design: str  # the model's label: RCT, SR_MA or other
    why: str
    title: str | None  # the record's title, else the title seen in the run
    paper: Paper | None  # the cached record (abstract), None when not available


@dataclass(frozen=True)
class ConsideredView:
    pmid: str
    reason: str
    title: str | None


@dataclass(frozen=True)
class ContradictionView:
    pmid: str
    note: str
    title: str | None


@dataclass(frozen=True)
class RunView:
    id: UUID
    question: str
    status: str
    error_kind: str | None
    stop_reason: str | None
    rounds: int | None
    model: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    steps: list[StepRow]
    searches: list[SearchEntry]
    selected: list[SelectedView]
    considered: list[ConsideredView]
    contradictions: list[ContradictionView]
    has_answer: bool = False  # an `answers` row exists (F3 runs that reached the end)
    empty_reason: str | None = None  # the stored answer's end message, when it has one

    @property
    def lines(self) -> list[str]:
        return [s.line for s in self.steps]

    @property
    def down_line(self) -> str | None:
        """The source-down line, when the runner wrote one."""
        for s in self.steps:
            if s.line in DOWN_LINES.values():
                return s.line
        return None


@dataclass(frozen=True)
class RunRow:
    id: UUID
    question: str
    created_ist: str  # "11 Nov 2026, 14:05"
    status: str


def end_message(v: RunView) -> str | None:
    """The end message for a finished run (plan Task 8 outcome table), or None: a list was
    selected, the run is not finished, or it failed with a model kind (the page words those).
    An F3 run's stored answer gives it (`empty_reason`, Incisive's); a run without an answer
    row (every F2 run) is worded as F2 words it."""
    if v.status == "failed":
        return HARD_STOP_MESSAGE if v.error_kind == "hard_stop" else None
    if v.status not in ("done", "partial"):
        return None
    if v.empty_reason is not None:
        return v.empty_reason
    if v.error_kind == "researcher_incomplete":
        return STOPPED_NO_ANSWER
    if v.error_kind == "empty_selection":
        return EMPTY_SELECTION
    if not v.selected:
        return STOPPED_NO_ANSWER if v.stop_reason in ("turn_failed", "wrap_up") else NO_EVIDENCE
    return None


# ---------- writes ----------


def active_run(pool: ConnectionPool) -> tuple[UUID, int] | None:
    """The queued or running run and its user, if any."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, user_id FROM runs WHERE status IN ('queued', 'running') LIMIT 1"
        ).fetchone()
    return (row[0], int(row[1])) if row else None


def stored_question(question: str) -> str:
    """The question as stored and sent, for a question that passed the check: a form sends a
    line break as CR LF, which becomes LF, the line break a textarea's own value holds (and
    so the text Incisive's page sends). Nothing else changes: no trimming, no other
    character removed (Incisive stores the question as it came)."""
    return question.replace("\r\n", "\n")


def create_run(pool: ConnectionPool, user_id: int, question: str, model: str) -> UUID:
    """A queued run for `question`. Raises QuestionBlocked (checked first, on the question
    exactly as received, locally, before any database write or network call) when the
    patient-details check finds anything, and ActiveRunExists when a run is queued or
    running. A question that passes is stored as `stored_question` gives it.

    The check runs twice: on the text as received (so a NUL or another hidden character is
    seen) and on the stored form Gemini is sent (LF line breaks; the port's byte windows can
    read a CR LF differently). A finding in either blocks; the first form's outcome is shown
    when both find something."""
    blocked = check_question(question)
    if blocked is None:
        blocked = check_question(stored_question(question))
    if blocked is not None:
        log.info(
            "ask kind=blocked_identifiers categories=%d %s",
            len(blocked.categories),
            " ".join(f"{k}={n}" for k, n in blocked.categories),
        )
        raise QuestionBlocked(blocked)
    question = stored_question(question)
    for _ in range(2):
        run_id = uuid.uuid4()
        try:
            with pool.connection() as conn:
                conn.execute(
                    "INSERT INTO runs (id, user_id, question, model) VALUES (%s, %s, %s, %s)",
                    (run_id, user_id, question, model),
                )
        except psycopg.errors.UniqueViolation as e:
            if e.diag.constraint_name != "runs_one_active":
                continue  # a uuid4 collision: try another id
            active = active_run(pool)
            if active is None:
                continue  # it finished in between: try once more
            raise ActiveRunExists(active[0], active[1] == user_id) from None
        return run_id
    raise ActiveRunExists(None, False)


def set_running(pool: ConnectionPool, run_id: UUID) -> None:
    with pool.connection() as conn:
        conn.execute(
            "UPDATE runs SET status = 'running', started_at = now() "
            "WHERE id = %s AND status = 'queued'",
            (run_id,),
        )


def add_step(pool: ConnectionPool, run_id: UUID, kind: str, line: str) -> None:
    """Appends a step line (cut to MAX_STEP_LINE). A failed write is logged by kind only and
    otherwise ignored (Incisive `add_line`)."""
    text = clip(line, MAX_STEP_LINE)
    try:
        with pool.connection() as conn:
            conn.execute(
                "INSERT INTO run_steps (run_id, seq, kind, line) SELECT %s, "
                "COALESCE(MAX(seq), 0) + 1, %s, %s FROM run_steps WHERE run_id = %s",
                (run_id, kind, text, run_id),
            )
    except psycopg.Error:
        log.warning("run kind=step_not_stored")


def add_search(pool: ConnectionPool, run_id: UUID, entry: SearchEntry) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO run_searches (run_id, seq, source, query, hits) SELECT %s, "
            "COALESCE(MAX(seq), 0) + 1, %s, %s, %s FROM run_searches WHERE run_id = %s",
            (run_id, entry.source, clean_text(entry.query)[:300], max(0, entry.hits), run_id),
        )


def add_seen(pool: ConnectionPool, run_id: UUID, papers: Sequence[SeenPaper]) -> None:
    """The papers the model saw (a PMID already stored for the run is kept as it is)."""
    if not papers:
        return
    with pool.connection() as conn, conn.transaction():
        for p in papers:
            conn.execute(
                "INSERT INTO run_seen (run_id, pmid, title, journal, year) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (run_id, p.pmid, clean_text(p.title),
                 clean_text(p.journal) if p.journal is not None else None, p.year),
            )


def save_list(pool: ConnectionPool, run_id: UUID, validated: Validated) -> None:
    """The checked study list in one transaction, in the model's order (`rank` from 1). The
    design is stored for selected rows only. A contradiction noted twice on one PMID keeps the
    first note."""
    with pool.connection() as conn, conn.transaction():
        rows: list[tuple[str, int, str, str | None, str]] = []
        rows += [("selected", i, s.pmid, s.design, s.why)
                 for i, s in enumerate(validated.selected, 1)]
        rows += [("considered", i, c.pmid, None, c.reason)
                 for i, c in enumerate(validated.considered, 1)]
        rows += [("contradiction", i, c.pmid, None, c.note)
                 for i, c in enumerate(validated.contradictions, 1)]
        for role, rank, pmid, design, note in rows:
            conn.execute(
                "INSERT INTO run_papers (run_id, role, rank, pmid, design, note) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (run_id, role, rank, pmid, design, note),
            )


def finish_run(pool: ConnectionPool, run_id: UUID, status: str, error_kind: str | None,
               stop_reason: str | None, rounds: int | None) -> bool:
    """The run's outcome, written only while it is still `queued` or `running`, so a late
    write never overwrites `stopped` (a restart) or an earlier outcome. False, logged by a
    fixed kind, when no row was changed."""
    with pool.connection() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = %s, error_kind = %s, stop_reason = %s, rounds = %s, "
            "finished_at = now() WHERE id = %s AND status IN ('queued', 'running')",
            (status, error_kind, stop_reason, rounds, run_id),
        )
        applied = cur.rowcount == 1
    if not applied:
        log.info("run kind=finish_not_applied run_id=%s", run_id)
    return applied


def save_answer(pool: ConnectionPool, run_id: UUID, body: AnswerBody,
                written: str | None) -> int | None:
    """Stores the run's answer (one row per run; a second write replaces the first), only
    while the run is still running. Returns the document's size in UTF-8 bytes for the log,
    or None when nothing was stored (the run was stopped by a restart meanwhile)."""
    doc = body_to_json(body)
    size = len(json.dumps(doc, ensure_ascii=False).encode("utf-8", "surrogatepass"))
    with pool.connection() as conn:
        cur = conn.execute(
            "INSERT INTO answers (run_id, body, written) SELECT %s, %s, %s WHERE EXISTS "
            "(SELECT 1 FROM runs WHERE id = %s AND status = 'running') ON CONFLICT (run_id) "
            "DO UPDATE SET body = EXCLUDED.body, written = EXCLUDED.written",
            (run_id, Jsonb(doc), written, run_id),
        )
        return size if cur.rowcount == 1 else None


def stop_unfinished(pool: ConnectionPool) -> int:
    """At start-up: every queued or running run belonged to the previous process."""
    with pool.connection() as conn:
        cur = conn.execute(
            "UPDATE runs SET status = 'stopped', error_kind = 'restarted', finished_at = now() "
            "WHERE status IN ('queued', 'running')"
        )
        return cur.rowcount


# ---------- reads ----------


def question_of(pool: ConnectionPool, run_id: UUID) -> str | None:
    with pool.connection() as conn:
        row = conn.execute("SELECT question FROM runs WHERE id = %s", (run_id,)).fetchone()
    return row[0] if row else None


def get_run(pool: ConnectionPool, run_id: UUID, user_id: int) -> RunView | None:
    """The run with everything the page shows; None for a missing run or another user's."""
    with pool.connection() as conn:
        r = conn.execute(
            "SELECT id, question, status, error_kind, stop_reason, rounds, model, created_at, "
            "started_at, finished_at FROM runs WHERE id = %s AND user_id = %s",
            (run_id, user_id),
        ).fetchone()
        if r is None:
            return None
        steps = [StepRow(k, line) for k, line in conn.execute(
            "SELECT kind, line FROM run_steps WHERE run_id = %s ORDER BY seq", (run_id,))]
        searches = [SearchEntry(q, s, int(h)) for s, q, h in conn.execute(
            "SELECT source, query, hits FROM run_searches WHERE run_id = %s ORDER BY seq",
            (run_id,))]
        seen = {pmid: title for pmid, title in conn.execute(
            "SELECT pmid, title FROM run_seen WHERE run_id = %s", (run_id,))}
        papers = conn.execute(
            "SELECT role, pmid, design, note FROM run_papers WHERE run_id = %s "
            "ORDER BY role, rank", (run_id,)).fetchall()
        answer = conn.execute("SELECT body FROM answers WHERE run_id = %s",
                              (run_id,)).fetchone()
    chosen = [p for p in papers if p[0] == "selected"]
    records = get_for_display(pool, [p[1] for p in chosen]) if chosen else {}
    selected = []
    for _, pmid, design, note in chosen:
        rec = records.get(pmid)
        selected.append(SelectedView(pmid, design, note,
                                     rec.title if rec else seen.get(pmid), rec))
    considered = [ConsideredView(pmid, note, seen.get(pmid))
                  for role, pmid, _, note in papers if role == "considered"]
    contradictions = [ContradictionView(pmid, note, seen.get(pmid))
                      for role, pmid, _, note in papers if role == "contradiction"]
    body = body_from_json(answer[0]) if answer is not None else None
    return RunView(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], steps,
                   searches, selected, considered, contradictions, answer is not None,
                   body.empty_reason if body is not None else None)


def get_answer(pool: ConnectionPool, run_id: UUID, user_id: int) -> AnswerBody | None:
    """The run's stored answer; None for another user's run, a run without one, or a stored
    document that cannot be read (never raises for it)."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT a.body FROM answers a JOIN runs r ON r.id = a.run_id "
            "WHERE a.run_id = %s AND r.user_id = %s", (run_id, user_id)).fetchone()
    return body_from_json(row[0]) if row is not None else None


def list_runs(pool: ConnectionPool, user_id: int, limit: int = 20) -> list[RunRow]:
    """The user's runs, newest first, with the time in IST."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, question, to_char(created_at AT TIME ZONE 'Asia/Kolkata', "
            "'FMDD Mon YYYY, HH24:MI'), status FROM runs WHERE user_id = %s "
            "ORDER BY created_at DESC, id LIMIT %s",
            (user_id, limit),
        ).fetchall()
    return [RunRow(i, q, t, s) for i, q, t, s in rows]
