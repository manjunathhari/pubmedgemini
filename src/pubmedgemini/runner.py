"""One run end to end, on a background thread.

`run_one` reads the stored question, marks the run running, runs the researcher with a
progress writer that stores every line, search and seen paper at once, then, in Incisive's
`agent/team.rs` `run` order: the stop line (`Search stopped at the round cap / the Quick limit
after N rounds`, or `Research stopped early (a failed model call); writing the answer from
what was found`), the selected studies' records (cache, then efetch), the study list, the
source-down line, then the writer (F3): references from the fetched records in selection
order, the writer's start gate, the four parts (`writer.write.write_quick`), the evidence
labels and the merge notes, the stored answer and `Answer ready`.

| Result | Status | error_kind | Answer stored |
|---|---|---|---|
| FatalModelError (researcher or writer) | failed | its kind | no |
| HardStop, or the hard stop passed before or after the records fetch | failed | hard_stop | no |
| RunCapReached (the run's RUN_ATTEMPTS attempts are used) | failed | run_call_cap | no |
| final_failed | partial | researcher_incomplete | empty, STOPPED_NO_ANSWER |
| empty_selection | partial | empty_selection | empty, EMPTY_SELECTION |
| nothing selected | done (partial after turn_failed or wrap_up) | none | empty, NO_EVIDENCE or STOPPED_NO_ANSWER |
| the records fetch failed | failed | lit | no |
| no selected record returned | partial | none | empty, UNFETCHED |
| under 150 s left before the hard stop, or a wait that would pass it (X10) | partial | writer_time | empty, WRITER_NO_TIME |
| the writer cut off | partial | writer_truncated | empty, WRITER_TRUNCATED |
| the writer unreadable | partial | writer_unreadable | empty, WRITER_UNREADABLE |
| a writer call failed (not fatal) | failed | its kind (`refused` for a blocked reply) | no |
| an answer written | done (partial after turn_failed) | none | the answer |
| anything else raised | failed | internal | no |

A run is a plain function on one daemon thread: every client below is synchronous, and the
POST that starts it never waits for it. Logs hold run ids, counts and fixed kinds only.
"""

import logging
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from psycopg_pool import ConnectionPool

from pubmedgemini.gemini.caller import ModelCaller, RunCapReached
from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.lit.paper import Paper
from pubmedgemini.lit.tools import LitTools, ToolFailed
from pubmedgemini.researcher.loop import (
    WRITER_MIN_SECONDS,
    FatalModelError,
    HardStop,
    Research,
    SearchEntry,
    SeenPaper,
    Timing,
    research,
)
from pubmedgemini.researcher.studylist import Validated
from pubmedgemini.runs import (
    ANSWER_READY,
    DOWN_LINES,
    EMPTY_SELECTION,
    NO_EVIDENCE,
    STOPPED_NO_ANSWER,
    UNFETCHED,
    WRITER_NO_TIME,
    WRITER_TRUNCATED,
    WRITER_UNREADABLE,
    add_search,
    add_seen,
    add_step,
    finish_run,
    question_of,
    save_answer,
    save_list,
    set_running,
)
from pubmedgemini.writer.answer import (
    AnswerBody,
    Integrity,
    Reference,
    design_class,
    evidence_types,
    merge_checks,
)
from pubmedgemini.writer.prompts import study_input, writer_user
from pubmedgemini.writer.write import WriteFailure, Written, write_quick

log = logging.getLogger("pubmedgemini.runner")

# Incisive `team.rs` `run`: "Research stopped early ({reason}); writing the answer from what
# was found", with `Research::stopped_early`'s reason.
STOPPED_EARLY_LINE = ("Research stopped early (a failed model call); writing the answer from "
                      "what was found")
NO_TIME_LINE = "Not enough time was left to write the answer"  # team.rs run
TRUNCATED_LINE = "The writer's answer was cut off by its output limit"  # team.rs run
UNREADABLE_LINE = "The writer's answer could not be read"  # team.rs run
# The writer's failures that end the run partial: (line, end message, error kind).
WRITE_PARTIAL = {
    "truncated": (TRUNCATED_LINE, WRITER_TRUNCATED, "writer_truncated"),
    "unreadable": (UNREADABLE_LINE, WRITER_UNREADABLE, "writer_unreadable"),
    "no_time": (NO_TIME_LINE, WRITER_NO_TIME, "writer_time"),
}

Writer = Callable[..., Written]


class RunStarter(Protocol):
    def start(self, run_id: UUID) -> None: ...


class ThreadStarter:
    """Starts each run on its own daemon thread named `run`. `run` is the function to call
    with the run id; the app sets it at start-up when it is not given."""

    def __init__(self, run: Callable[[UUID], None] | None = None) -> None:
        self.run = run
        self.threads: list[threading.Thread] = []

    def start(self, run_id: UUID) -> None:
        if self.run is None:
            raise RuntimeError("no run function")
        t = threading.Thread(target=self.run, args=(run_id,), daemon=True, name="run")
        self.threads.append(t)
        t.start()

    def join(self, timeout: float | None = None) -> None:
        for t in list(self.threads):
            t.join(timeout)


class InlineStarter:
    """Tests: runs the run at once, inside `start`."""

    def __init__(self, run: Callable[[UUID], None] | None = None) -> None:
        self.run = run
        self.started: list[UUID] = []

    def start(self, run_id: UUID) -> None:
        if self.run is None:
            raise RuntimeError("no run function")
        self.started.append(run_id)
        self.run(run_id)


class DbProgress:
    """The researcher's progress, stored at once. A failed write is logged by kind and the run
    carries on (Incisive `add_line`)."""

    def __init__(self, pool: ConnectionPool, run_id: UUID) -> None:
        self._pool = pool
        self._run_id = run_id

    def line(self, kind: str, text: str) -> None:
        add_step(self._pool, self._run_id, kind, text)

    def search(self, entry: SearchEntry) -> None:
        try:
            add_search(self._pool, self._run_id, entry)
        except Exception as e:  # noqa: BLE001 - the trail never fails a run
            log.warning("run kind=search_not_stored error=%s", type(e).__name__)

    def seen(self, papers: Sequence[SeenPaper]) -> None:
        try:
            add_seen(self._pool, self._run_id, papers)
        except Exception as e:  # noqa: BLE001
            log.warning("run kind=seen_not_stored error=%s", type(e).__name__)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _stop_line(r: Research) -> str | None:
    """Incisive `team.rs` `run` with `Research::stopped_at` and `stopped_early`."""
    if r.stop == "turn_cap":
        return f"Search stopped at the round cap after {_plural(r.rounds, 'round', 'rounds')}"
    if r.stop == "wrap_up":
        return f"Search stopped at the Quick limit after {_plural(r.rounds, 'round', 'rounds')}"
    if r.stop == "turn_failed":
        return STOPPED_EARLY_LINE
    return None


def _stopped_early(r: Research) -> str | None:
    """Incisive `Research::stopped_early`: the reason research was cut short, if it was."""
    return {"wrap_up": "time limit", "turn_failed": "a failed model call"}.get(r.stop)


def _unwritten(r: Research) -> tuple[str, str | None, str] | None:
    """The outcome of a run that ends before the writer (status, kind, end message), or
    None when studies were selected."""
    if r.final_failed:
        return "partial", "researcher_incomplete", STOPPED_NO_ANSWER
    if r.empty_selection:
        return "partial", "empty_selection", EMPTY_SELECTION
    selected = r.validated.selected if r.validated is not None else ()
    if not selected:
        if r.stop in ("turn_failed", "wrap_up"):
            return "partial", None, STOPPED_NO_ANSWER
        return "done", None, NO_EVIDENCE
    return None


def _hard_stop(pool: ConnectionPool, run_id: UUID) -> None:
    _failed(pool, run_id, "hard_stop")


def _failed(pool: ConnectionPool, run_id: UUID, kind: str) -> None:
    finish_run(pool, run_id, "failed", kind, None, None)
    log.info("run kind=finished run_id=%s status=failed error_kind=%s", run_id, kind)


class _Finish:
    """The end of a run that reached the writer's side: the stored answer, `Answer ready`,
    the outcome (Incisive `finish`, `unwritten`)."""

    def __init__(self, pool: ConnectionPool, run_id: UUID, progress: "DbProgress", r: Research,
                 body: AnswerBody, started: float, clock: Callable[[], float]) -> None:
        self.pool, self.run_id, self.progress, self.r = pool, run_id, progress, r
        self.body, self.started, self.clock = body, started, clock

    def __call__(self, status: str, kind: str | None, written: str | None = None,
                 empty_reason: str | None = None) -> None:
        if empty_reason is not None:
            self.body.empty_reason = empty_reason
        self.body.elapsed_s = max(0, int(self.clock() - self.started))
        size = save_answer(self.pool, self.run_id, self.body, written)
        if size is None:
            log.info("answer kind=not_stored run_id=%s", self.run_id)
        else:
            log.info("answer stored run_id=%s bytes=%d written=%s", self.run_id, size,
                     written or "none")
        self.progress.line("present", ANSWER_READY)
        finish_run(self.pool, self.run_id, status, kind, self.r.stop, self.r.rounds)
        log.info("run kind=finished run_id=%s status=%s error_kind=%s", self.run_id, status,
                 kind or "none")


def _with_abstract(p: Paper) -> bool:
    return p.abstract is not None and bool(rust_trim(p.abstract))


def _write(question: str, validated: Validated, r: Research, papers: dict[str, Paper],
           body: AnswerBody, finish: _Finish, *, caller: ModelCaller, run_id: UUID,
           timing: Timing, progress: DbProgress, clock: Callable[[], float],
           writer: Writer) -> None:
    """From the fetched records to the end of the run (Incisive `team.rs` `run`, from the
    records to `finish`). Raises the writer's HardStop, FatalModelError and RunCapReached."""
    chosen = [(s, papers[s.pmid]) for s in validated.selected if s.pmid in papers]
    unfetched = len(validated.selected) - len(chosen)
    body.integrity.dropped_unfetched = unfetched
    if unfetched > 0:
        progress.line("note", f"{_plural(unfetched, 'selected study', 'selected studies')} "
                              f"could not be fetched from PubMed and "
                              f"{'was' if unfetched == 1 else 'were'} left out")
    # References are code's, from the fetched records, in selection order.
    body.references = [Reference(n=i, pmid=s.pmid, title=p.title, journal=p.journal,
                                 year=p.year, doi=p.doi, retracted=p.retracted)
                       for i, (s, p) in enumerate(chosen, start=1)]
    if not chosen:
        finish("partial", None, empty_reason=UNFETCHED)
        return
    left = timing.hard_stop_at - clock()
    if left < WRITER_MIN_SECONDS:
        log.info("run kind=writer_no_time run_id=%s left_s=%d", run_id, int(left))
        progress.line("note", NO_TIME_LINE)
        finish("partial", "writer_time", empty_reason=WRITER_NO_TIME)
        return
    with_abstract = sum(1 for _, p in chosen if _with_abstract(p))
    progress.line("write", f"Writer: writing the answer from "
                           f"{_plural(len(chosen), 'study', 'studies')} "
                           f"({with_abstract} with an abstract)")
    studies = [study_input(s, p) for s, p in chosen]
    user = writer_user(question, studies, validated.contradictions, r.searches)
    outcome: Written | WriteFailure
    try:
        outcome = writer(user, caller=caller, run_id=run_id, timing=timing, progress=progress,
                         clock=clock)
    except WriteFailure as e:
        outcome = e
    if isinstance(outcome, WriteFailure):
        if outcome.kind == "call":
            _failed(finish.pool, run_id, outcome.error_kind or "internal")
            return
        line, message, kind = WRITE_PARTIAL[outcome.kind]
        progress.line("note", line)
        finish("partial", kind, empty_reason=message)
        return
    designs = {s.pmid: s.design for s, _ in chosen}
    evidence_types(outcome.answer,
                   lambda p: design_class(designs[p]) if p in designs else None)
    body.integrity.labels_defaulted = outcome.labels_defaulted
    if outcome.merged:
        body.notes = merge_checks(outcome.answer)
    body.answer = outcome.answer
    finish("partial" if r.stop == "turn_failed" else "done", None,
           written="parts" if outcome.merged else "one_piece")


def run_one(
    run_id: UUID,
    *,
    pool: ConnectionPool,
    tools: LitTools,
    caller: ModelCaller,
    model: str,
    clock: Callable[[], float] = time.monotonic,
    writer: Writer = write_quick,
) -> None:
    """Runs one queued run to its end (see the module doc). Never raises."""
    try:
        question = question_of(pool, run_id)
        if question is None:
            log.warning("run kind=not_found run_id=%s", run_id)
            return
        set_running(pool, run_id)
        log.info("run kind=running run_id=%s model=%s", run_id, model)
        progress = DbProgress(pool, run_id)
        timing = Timing.quick(clock())
        try:
            r = research(question, tools=tools, caller=caller, run_id=run_id,
                         timing=timing, progress=progress, clock=clock)
        except FatalModelError as e:
            _failed(pool, run_id, e.kind)
            return
        except HardStop:
            _hard_stop(pool, run_id)
            return
        except RunCapReached:
            _failed(pool, run_id, "run_call_cap")
            return
        line = _stop_line(r)
        if line is not None:
            progress.line("note", line)
        selected = r.validated.selected if r.validated is not None else ()
        papers: dict[str, Paper] = {}
        fetch_failed = False
        if selected:
            # A literature call cannot be cut once started (plan: Hard stop), so the hard
            # stop is checked before and after it.
            if clock() >= timing.hard_stop_at:
                _hard_stop(pool, run_id)
                return
            try:
                papers = tools.fetch_papers([s.pmid for s in selected])
            except ToolFailed as e:
                log.warning("run kind=records_not_fetched error_kind=%s", e.error_kind)
                fetch_failed = True
            if clock() >= timing.hard_stop_at:
                _hard_stop(pool, run_id)
                return
        if r.validated is not None:
            save_list(pool, run_id, r.validated)
        for source in r.down:
            progress.line("note", DOWN_LINES[source])
        body = AnswerBody(
            created_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            answer=None, empty_reason=None, references=[], searches=list(r.searches),
            integrity=Integrity(dropped_unseen=(r.validated.dropped_unseen
                                                if r.validated is not None else 0)),
            stopped_early=_stopped_early(r), elapsed_s=0, notes=[])
        finish = _Finish(pool, run_id, progress, r, body, timing.started, clock)
        before = _unwritten(r)
        if before is not None:
            status, kind, message = before
            finish(status, kind, empty_reason=message)
            return
        if fetch_failed:  # the writer needs the abstracts (Incisive: `failed` `lit`)
            _failed(pool, run_id, "lit")
            return
        try:
            _write(question, r.validated, r, papers, body, finish, caller=caller,
                   run_id=run_id, timing=timing, progress=progress, clock=clock, writer=writer)
        except FatalModelError as e:
            _failed(pool, run_id, e.kind)
        except HardStop:
            _hard_stop(pool, run_id)
        except RunCapReached:
            _failed(pool, run_id, "run_call_cap")
    except Exception as e:  # noqa: BLE001 - a run never takes the app down; kind only
        log.error("run kind=internal error=%s", type(e).__name__)
        try:
            finish_run(pool, run_id, "failed", "internal", None, None)
        except Exception:  # noqa: BLE001
            log.error("run kind=not_finished run_id=%s", run_id)
    finally:
        caller.forget(run_id)  # the run's attempt count is no longer needed
