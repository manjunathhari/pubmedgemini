"""Run rows: one per question, with its step lines, search trail, seen papers and study list.

Plain SQL over the tables of `migrations/0004_runs.sql` (Incisive `migrations/0005_runs.sql`
`runs`, `run_steps`, plus the trail, the seen papers and the list). One active run for the whole
app, enforced by the database (`runs_one_active`). Writes during a run are made at once, so the
page shows each line as it happens.

From Incisive (commit 12630f7, `crates/incisive-app/src/`): `clip` and `MAX_STEP_LINE`
(`agent/mod.rs`), a failed step write logged and ignored (`add_line`), the end messages
(`agent/answer.rs` `NO_EVIDENCE`, `STOPPED_NO_ANSWER`, `EMPTY_SELECTION`; `agent/team.rs`
`UNFETCHED`, `WRITER_TRUNCATED`, `WRITER_UNREADABLE`, `WRITER_NO_TIME`).

The patient-details check (`deid.check_question`, ported from Incisive) runs in
`create_run`, the one place a question becomes a run: a blocked question raises
`QuestionBlocked` before anything is stored or sent.

Logs hold run ids, counts and fixed kinds only; never the question or any text of a run.
"""

import logging
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from pubmedgemini.deid import BlockedIdentifiers, check_question
from pubmedgemini.lit.cache import get_for_display
from pubmedgemini.lit.paper import Paper
from pubmedgemini.lit.text import clean_text, drop_invisible, scrub_json
from pubmedgemini.researcher.loop import SearchEntry, SeenPaper
from pubmedgemini.researcher.studylist import Validated

log = logging.getLogger("pubmedgemini.runs")

MAX_STEP_LINE = 160  # agent/mod.rs MAX_STEP_LINE

# Step lines are written from the run's thread and from the writer's four part threads. The
# next `seq` is MAX(seq)+1, which two writers at once would both read, so one writer inserts at a
# time. One process runs the app (one active run for the whole app), so a lock in the process is
# enough.
_STEP_LOCK = threading.Lock()

# India has no daylight saving: a fixed +05:30 offset is Asia/Kolkata, with no tz database needed.
IST = timezone(timedelta(hours=5, minutes=30))

# The run statuses that end a run (every status of `runs` but queued and running).
FINAL_STATUSES = frozenset({"done", "partial", "failed", "stopped"})

NO_EVIDENCE = "No relevant evidence found."
STOPPED_NO_ANSWER = (
    "No answer could be written: the search stopped early, before enough evidence was read and "
    "checked."
)
EMPTY_SELECTION = (
    "No studies could be selected from the papers found. Please run the question again."
)
HARD_STOP_MESSAGE = "This run ran out of time (7 minutes). Please run the question again."
# The writer's end messages, the answer's `empty_reason` (Incisive `agent/team.rs:137-145`).
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
ANSWER_READY = "Answer ready"  # team.rs `finish`, kind `present`
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
class CheckRow:
    """The run's completeness check (F4a), as stored: a fixed status and, when `done`, four
    counts. `age_s` is the seconds since the row was created, by the database's clock."""

    status: str  # pending, running, done or failed
    numbers_found: int | None
    numbers_total: int | None
    names_match: int | None
    names_total: int | None
    age_s: float


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
    answer: dict | None = None  # the stored AnswerV3 body, None when no answer was stored
    # F3c: the Introduction's text, only when its row is `done`; the early summary's body,
    # only while the run is queued or running and has no answer.
    intro: str | None = None
    early: dict | None = None
    # F4a: the completeness check's row; None for a run without one (every run before F4a,
    # and every run with no written answer).
    check: CheckRow | None = None

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
    selected, the run is not finished, or it failed with a model kind (the page words those)."""
    if v.status == "failed":
        return HARD_STOP_MESSAGE if v.error_kind == "hard_stop" else None
    if v.status not in ("done", "partial"):
        return None
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


_INSERT_STEP = (
    "INSERT INTO run_steps (run_id, seq, kind, line) SELECT %s, "
    "COALESCE(MAX(seq), 0) + 1, %s, %s FROM run_steps WHERE run_id = %s"
)


def add_step(pool: ConnectionPool, run_id: UUID, kind: str, line: str) -> None:
    """Appends a step line (cut to MAX_STEP_LINE). A failed write is logged by kind only and
    otherwise ignored (Incisive `add_line`)."""
    text = clip(line, MAX_STEP_LINE)
    try:
        with _STEP_LOCK, pool.connection() as conn:
            conn.execute(_INSERT_STEP, (run_id, kind, text, run_id))
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
    write never overwrites `stopped` (a restart) or an earlier outcome. The run's early
    summary is deleted in the same transaction. False, logged by a fixed kind, when no row was
    changed."""
    with pool.connection() as conn, conn.transaction():
        cur = conn.execute(
            "UPDATE runs SET status = %s, error_kind = %s, stop_reason = %s, rounds = %s, "
            "finished_at = now() WHERE id = %s AND status IN ('queued', 'running')",
            (status, error_kind, stop_reason, rounds, run_id),
        )
        applied = cur.rowcount == 1
        if applied:  # the early summary never outlives the run (F3c)
            conn.execute(_DELETE_EARLY, (run_id,))
    if not applied:
        log.info("run kind=finish_not_applied run_id=%s", run_id)
    return applied


def finish_with_answer(pool: ConnectionPool, run_id: UUID, status: str,
                       error_kind: str | None, stop_reason: str | None, rounds: int | None,
                       body: dict, closing_line: tuple[str, str] | None = None,
                       check: bool = False) -> bool:
    """The run's outcome and its written answer, in one transaction: both are stored or
    neither. As `finish_run`, the outcome is written only while the run is still `queued` or
    `running`; when it is not (a restart stopped it), nothing is stored and the case is logged
    by a fixed kind. A body the database refuses raises the database error (the status stays
    as it was); the caller decides what to do with it. Nothing of the body is logged. The
    body is stored cleaned of NUL, lone surrogates, bidi controls and zero-width characters
    (`scrub_json`), so a NUL in model text never makes Postgres refuse it.

    The run's early summary is deleted in the same transaction (F3c), so the page never shows
    it next to the answer, nor loses it while the answer is not stored.

    `closing_line` (kind, text), when given, is the run's closing step line ("Answer ready"),
    stored in the same transaction (cut as `add_step` cuts), so the page never shows it for an
    answer that was not stored, nor a stored answer without it.

    `check` (F4a), when true, also inserts the run's `pending` completeness-check row in the
    same transaction, inside a savepoint: the first page that shows the answer shows the row.
    If that insert alone is refused, it is rolled back on its own, one line is logged by the
    error's class, and the answer is stored all the same: the check never keeps an answer
    from being stored.

    True only when the outcome and the answer were both stored; False when the run was no
    longer queued or running. A `status` that is not final raises ValueError before anything
    is written."""
    if status not in FINAL_STATUSES:
        raise ValueError("status must be final")
    check_error: str | None = None
    with _STEP_LOCK, pool.connection() as conn, conn.transaction():
        cur = conn.execute(
            "UPDATE runs SET status = %s, error_kind = %s, stop_reason = %s, rounds = %s, "
            "finished_at = now() WHERE id = %s AND status IN ('queued', 'running')",
            (status, error_kind, stop_reason, rounds, run_id),
        )
        applied = cur.rowcount == 1
        if applied:
            conn.execute(
                "INSERT INTO answers (run_id, version, body) VALUES (%s, 3, %s)",
                (run_id, Jsonb(scrub_json(body))),
            )
            if closing_line is not None:
                kind, line = closing_line
                conn.execute(_INSERT_STEP, (run_id, kind, clip(line, MAX_STEP_LINE), run_id))
            conn.execute(_DELETE_EARLY, (run_id,))
            if check:
                try:
                    with conn.transaction():  # a savepoint: rolled back alone when refused
                        conn.execute("INSERT INTO run_checks (run_id) VALUES (%s)", (run_id,))
                except psycopg.Error as e:
                    check_error = type(e).__name__
    if check_error is not None:
        log.warning("run kind=check_not_started run_id=%s error=%s", run_id, check_error)
    if not applied:
        log.info("run kind=finish_not_applied run_id=%s", run_id)
    return applied


def stop_unfinished(pool: ConnectionPool) -> int:
    """At start-up: every queued or running run belonged to the previous process. In the same
    transaction every early summary is deleted (no run is running now), every pending
    Introduction fails `stale` (its thread died with the process) and every pending or running
    completeness check fails `stale` (F4a: it ran on the run's thread; it is not run again).
    Returns the runs stopped."""
    with pool.connection() as conn, conn.transaction():
        cur = conn.execute(
            "UPDATE runs SET status = 'stopped', error_kind = 'restarted', finished_at = now() "
            "WHERE status IN ('queued', 'running')"
        )
        stopped = cur.rowcount
        conn.execute("DELETE FROM run_early")
        conn.execute("UPDATE run_intros SET status = 'failed', reason = 'stale', "
                     "finished_at = now() WHERE status = 'pending'")
        conn.execute("UPDATE run_checks SET status = 'failed', reason = 'stale', "
                     "finished_at = now() WHERE status IN ('pending', 'running')")
    return stopped


# ---------- the Introduction and the early summary (F3c) ----------

_DELETE_EARLY = "DELETE FROM run_early WHERE run_id = %s"
# A write of the early summary waits at most this long (Incisive `EARLY_WRITE_LIMIT`, 2 s).
EARLY_WRITE_TIMEOUT = "2s"
INTRO_STATUSES = ("done", "failed")


def intro_start(pool: ConnectionPool, run_id: UUID) -> bool:
    """The run's `pending` Introduction row (Incisive `insert_pending`). False when a row
    already exists or the run is gone."""
    with pool.connection() as conn:
        cur = conn.execute(
            "INSERT INTO run_intros (run_id) SELECT id FROM runs WHERE id = %s "
            "ON CONFLICT (run_id) DO NOTHING",
            (run_id,),
        )
        return cur.rowcount == 1


def intro_finish(pool: ConnectionPool, run_id: UUID, status: str, reason: str | None,
                 text: str | None) -> bool:
    """Ends the Introduction row, only while it is `pending` (Incisive `finish`). `text` is
    kept only for `done`, cleaned of NUL, lone surrogates, bidi controls and zero-width
    characters; `reason` is a fixed kind. False when the row is gone or already ended."""
    if status not in INTRO_STATUSES:
        raise ValueError("status must be done or failed")
    kept = drop_invisible(clean_text(text)) if status == "done" and text is not None else None
    with pool.connection() as conn:
        cur = conn.execute(
            "UPDATE run_intros SET status = %s, reason = %s, text = %s, finished_at = now() "
            "WHERE run_id = %s AND status = 'pending'",
            (status, reason, kept, run_id),
        )
        return cur.rowcount == 1


def store_early(pool: ConnectionPool, run_id: UUID, body: dict) -> bool:
    """Stores (or replaces) the run's early summary, only while the run is `running` and has
    no answer row. The run's row is locked first, so a store and the run's end never cross:
    a store after the end finds the run ended, and an end after a store deletes it. The
    transaction waits at most EARLY_WRITE_TIMEOUT. The body is stored cleaned as the answer
    is (`scrub_json`). A body the database refuses raises; the caller logs its class. Nothing
    of the body is logged."""
    with pool.connection() as conn, conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = '{EARLY_WRITE_TIMEOUT}'")
        row = conn.execute(
            "SELECT status, EXISTS (SELECT 1 FROM answers WHERE run_id = runs.id) "
            "FROM runs WHERE id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if row is None or row[0] != "running" or row[1]:
            return False
        conn.execute(
            "INSERT INTO run_early (run_id, body) VALUES (%s, %s) ON CONFLICT (run_id) "
            "DO UPDATE SET body = EXCLUDED.body, updated_at = now()",
            (run_id, Jsonb(scrub_json(body))),
        )
        return True


def clear_early(pool: ConnectionPool, run_id: UUID) -> None:
    """Deletes the run's early summary (a writer part failed for good: withdraw)."""
    with pool.connection() as conn, conn.transaction():
        conn.execute(f"SET LOCAL statement_timeout = '{EARLY_WRITE_TIMEOUT}'")
        conn.execute(_DELETE_EARLY, (run_id,))


# ---------- the completeness check (F4a) ----------


def check_begin(pool: ConnectionPool, run_id: UUID) -> bool:
    """The run's check row from `pending` to `running`. False when there is no pending row
    (the answer was not stored with one, or a restart ended it first)."""
    with pool.connection() as conn:
        cur = conn.execute(
            "UPDATE run_checks SET status = 'running', started_at = now() "
            "WHERE run_id = %s AND status = 'pending'",
            (run_id,),
        )
        return cur.rowcount == 1


def check_finish(pool: ConnectionPool, run_id: UUID, *,
                 counts: tuple[int, int, int, int] | None, reason: str | None) -> bool:
    """Ends the run's check row, only while it is `pending` or `running`: `counts` (numbers
    found, numbers in all, names matching, names in all) make it `done`; else `reason`, a
    fixed kind, makes it `failed` with no counts. Exactly one of the two must be given, else
    ValueError before any write. False when the row is gone or already ended."""
    if (counts is None) == (reason is None):
        raise ValueError("counts or reason, one of the two")
    with pool.connection() as conn:
        if counts is not None:
            found, total, match, names = counts
            cur = conn.execute(
                "UPDATE run_checks SET status = 'done', numbers_found = %s, "
                "numbers_total = %s, names_match = %s, names_total = %s, finished_at = now() "
                "WHERE run_id = %s AND status IN ('pending', 'running')",
                (found, total, match, names, run_id),
            )
        else:
            cur = conn.execute(
                "UPDATE run_checks SET status = 'failed', reason = %s, finished_at = now() "
                "WHERE run_id = %s AND status IN ('pending', 'running')",
                (reason, run_id),
            )
        return cur.rowcount == 1


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
        answer_row = conn.execute(
            "SELECT body FROM answers WHERE run_id = %s", (run_id,)).fetchone()
        intro_row = conn.execute(
            "SELECT text FROM run_intros WHERE run_id = %s AND status = 'done'",
            (run_id,)).fetchone()
        early_row = None
        if answer_row is None and r[2] in ("queued", "running"):
            early_row = conn.execute(
                "SELECT body FROM run_early WHERE run_id = %s", (run_id,)).fetchone()
        check_row = conn.execute(
            "SELECT status, numbers_found, numbers_total, names_match, names_total, "
            "EXTRACT(EPOCH FROM now() - created_at) FROM run_checks WHERE run_id = %s",
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
    return RunView(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], steps,
                   searches, selected, considered, contradictions,
                   answer_row[0] if answer_row else None,
                   intro=intro_row[0] if intro_row else None,
                   early=early_row[0] if early_row else None,
                   check=CheckRow(check_row[0], check_row[1], check_row[2], check_row[3],
                                  check_row[4], float(check_row[5])) if check_row else None)


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


def daily_question_counts(
    pool: ConnectionPool, user_id: int, now: datetime, days: int = 14
) -> list[tuple[date, int]]:
    """How many questions the user asked on each of the last `days` India-time days, newest
    first, ending on the India day of `now` (an aware time), zeros included. A question counts
    on the India day its run row was created, whatever its status. The days are cut in Python
    from the rows in the window, so the result does not depend on the session's time zone."""
    today = now.astimezone(IST).date()
    first = today - timedelta(days=days - 1)
    start = datetime(first.year, first.month, first.day, tzinfo=IST)
    end = datetime(today.year, today.month, today.day, tzinfo=IST) + timedelta(days=1)
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT created_at FROM runs WHERE user_id = %s AND created_at >= %s "
            "AND created_at < %s",
            (user_id, start, end),
        ).fetchall()
    counts = {today - timedelta(days=i): 0 for i in range(days)}
    for (created_at,) in rows:
        counts[created_at.astimezone(IST).date()] += 1
    return list(counts.items())
