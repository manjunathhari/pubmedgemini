"""One run end to end, on a background thread.

`run_one` reads the stored question, marks the run running, runs the researcher with a
progress writer that stores every line, search and seen paper at once, then follows Incisive
Quick's sequence after research (`agent/team.rs` `run`, `t.rs:343-772`, commit 12630f7): the
stop line; the study list stored (so it stays on the page whatever follows) and the
source-down line; the ends before any record is fetched; the selected studies' records
fetched and kept; the missing-record line; the references from the records; the time gate;
the writer in four parts (`writer.parts.write_parts`); the citation check; the answer stored
with the run's final status and the closing line `Answer ready`, in one transaction.

| Ending | Status | error_kind | Answer row |
|---|---|---|---|
| FatalModelError (researcher or writer) | failed | its kind | no |
| HardStop, or the hard stop passed before or after the records fetch | failed | hard_stop | no |
| RunCapReached (the run's RUN_ATTEMPTS attempts are used) | failed | run_call_cap | no |
| final_failed | partial | researcher_incomplete | yes, STOPPED_NO_ANSWER |
| empty_selection | partial | empty_selection | yes, EMPTY_SELECTION |
| nothing selected | done (partial after turn_failed or wrap_up) | none | yes, NO_EVIDENCE (STOPPED_NO_ANSWER when stopped early) |
| the records fetch failed (ToolFailed) | failed | lit | no |
| no record fetched | partial | none | yes, UNFETCHED |
| under 150 s left before the writer | partial | writer_time | yes, WRITER_NO_TIME |
| the writer cut off | partial | writer_truncated | yes, WRITER_TRUNCATED |
| the writer unreadable | partial | writer_unreadable | yes, WRITER_UNREADABLE |
| the writer's call failed (a GeminiError kind) | failed | that kind | no |
| an answer written | done (partial after turn_failed) | none | yes, the answer |
| the database refused the answer, or anything else raised | failed | internal | no |

A run that a restart marked `stopped` meanwhile keeps that status: nothing is stored over it.

F3c: right after `set_running` the run starts its Introduction (`intro.start_intro`) on its own
daemon thread and never waits for it while it works; it can neither fail nor change the run
(every failure ends its own row `failed`). Its one attempt counts in the run's RUN_ATTEMPTS.
`run_one` joins it, at most TIME_LIMIT_SECONDS + 5 s, after the run's status is stored and
before the run's attempt count is forgotten, so nothing is sent for the run after `forget`.
The writer's `on_part` is an `EarlyStore`: the summary and context parts, citation-checked as
the final answer is, stored while the run works (`runs.store_early`), deleted on a withdraw;
`finish_run` and `finish_with_answer` delete it when the run ends.

F4a: a written answer is stored together with its `pending` completeness-check row, and right
after that, on this same thread, `completeness.run_check` counts the answer's numbers and
trial names against the records the run holds (code only: no model call, no network call, no
step line). The run is already final by then; the check never raises and can neither fail,
delay nor change the run or its stored answer.

A run is a plain function on one daemon thread (the writer adds four part threads of its
own): every client below is synchronous, and the POST that starts it never waits for it. Logs
hold run ids, counts and fixed kinds only; an exception is logged by its class name only.
"""

import copy
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from psycopg_pool import ConnectionPool

from pubmedgemini.completeness import run_check
from pubmedgemini.gemini.caller import ModelCaller, RunCapReached
from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.intro import TIME_LIMIT_SECONDS as INTRO_TIME_LIMIT
from pubmedgemini.intro import start_intro as start_intro_thread
from pubmedgemini.lit.tools import LitTools, ToolFailed
from pubmedgemini.researcher.loop import (
    WAITING_LINE,
    WRITER_MIN_SECONDS,
    FatalModelError,
    HardStop,
    Research,
    SearchEntry,
    SeenPaper,
    Timing,
    research,
)
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
    clear_early,
    finish_run,
    finish_with_answer,
    question_of,
    save_list,
    set_running,
    store_early,
)
from pubmedgemini.writer.answer import (
    AnswerV3,
    IntegrityV3,
    ReferenceV3,
    WrittenAnswer,
    check_citations,
    citation_check_line,
    plural,
    references_from,
)
from pubmedgemini.writer.parts import PartEvent, WriterFailed, write_parts
from pubmedgemini.writer.prompts import StudyInput, writer_user
from pubmedgemini.writer.read import design_class, early_answer, evidence_types, merge_checks

log = logging.getLogger("pubmedgemini.runner")

# team.rs:353-359 `Research stopped early ({reason}); writing the answer from what was found`,
# with researcher.rs:552-559 `stopped_early` for a failed turn.
STOPPED_EARLY_LINE = (
    "Research stopped early (a failed model call); writing the answer from what was found"
)
NO_TIME_LINE = "Not enough time was left to write the answer"  # team.rs:519-524
TRUNCATED_LINE = "The writer's answer was cut off by its output limit"  # team.rs:761-766
UNREADABLE_LINE = "The writer's answer could not be read"  # team.rs:770


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


def _stop_line(r: Research) -> str | None:
    """Incisive `team.rs:343-360` with `Research::stopped_at` and `stopped_early`."""
    if r.stop == "turn_cap":
        return f"Search stopped at the round cap after {plural(r.rounds, 'round', 'rounds')}"
    if r.stop == "wrap_up":
        return f"Search stopped at the Quick limit after {plural(r.rounds, 'round', 'rounds')}"
    if r.stop == "turn_failed":
        return STOPPED_EARLY_LINE
    return None


def _stopped_early(r: Research) -> str | None:
    """`Research::stopped_early` (researcher.rs:552-559): the Quick time limit, or a failed
    research turn (Incisive's "cost limit" has no counterpart: no money here)."""
    if r.stop == "wrap_up":
        return "time limit"
    if r.stop == "turn_failed":
        return "a failed model call"
    return None


class _Stop(Exception):
    """The run ends `failed` with `kind` and no answer row (a fixed kind, nothing else)."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


@dataclass
class _Answer:
    """The answer being built (Incisive `base`, `t.rs:203-226`)."""

    question: str
    searches: list[SearchEntry]
    stopped_early: str | None
    integrity: IntegrityV3
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    references: list[ReferenceV3] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class EarlyStore:
    """The writer's `on_part` (F3c): the early summary (Incisive `team.rs:798-870`
    `early_body`, `store_early`). On `summary` or `context` the part is kept and the answer
    holding the parts so far (`early_answer`) gets the same citation check, against the same
    fetched studies, as the final answer; the key numbers of At a glance are held back (O3),
    and the body is stored while the run works. On `withdraw` (a part failed for good) the row
    is deleted and nothing more is stored. Called under the writer's lock, so a delete is never
    overtaken by an earlier store. Every error is caught and logged by its class; the run goes
    on. Nothing of the text is logged."""

    def __init__(self, pool: ConnectionPool, run_id: UUID, a: "_Answer",
                 selected: list[str]) -> None:
        self._pool = pool
        self._run_id = run_id
        self._a = a
        self._selected = list(selected)
        self._parts: dict[str, dict] = {}
        self._withdrawn = False

    def body(self) -> dict:
        written = early_answer(copy.deepcopy(self._parts.get("summary")),
                               copy.deepcopy(self._parts.get("context")))
        check_citations(written, self._selected, IntegrityV3())
        if written.at_a_glance is not None:
            written.at_a_glance.key_numbers = []  # O3 (b): shown with the full answer only
        a = self._a
        answer = AnswerV3(question=a.question, created_at=a.created_at, answer=written,
                          empty_reason=None, references=a.references, searches=a.searches,
                          integrity=IntegrityV3(), stopped_early=a.stopped_early, elapsed_s=0,
                          notes=[]).to_body()
        return {"summary": "summary" in self._parts, "context": "context" in self._parts,
                "answer": answer}

    def __call__(self, ev: PartEvent) -> None:
        kind, part = ev
        try:
            if kind == "withdraw":
                self._withdrawn = True
                clear_early(self._pool, self._run_id)
                log.info("run kind=early_withdrawn run_id=%s", self._run_id)
                return
            if self._withdrawn or kind not in ("summary", "context") or part is None:
                return
            self._parts[kind] = part
            body = self.body()
            size = len(json.dumps(body, ensure_ascii=False).encode("utf-8", errors="replace"))
            if store_early(self._pool, self._run_id, body):
                log.info("run kind=early_stored run_id=%s summary=%s context=%s bytes=%d",
                         self._run_id, str(body["summary"]).lower(),
                         str(body["context"]).lower(), size)
            else:
                log.info("run kind=early_not_stored run_id=%s error=NotRunning", self._run_id)
        except Exception as e:  # noqa: BLE001 - the early summary never fails a run
            log.warning("run kind=early_not_stored run_id=%s error=%s", self._run_id,
                        type(e).__name__)


class _Run:
    """One run after `set_running`: the researcher, then Incisive Quick's sequence. Every
    ending either stores an answer with its final status (`end`) or raises one of HardStop,
    FatalModelError, RunCapReached and _Stop for `run_one` to fail the run."""

    def __init__(self, run_id: UUID, question: str, pool: ConnectionPool, tools: LitTools,
                 caller: ModelCaller, clock: Callable[[], float]) -> None:
        self.run_id = run_id
        self.question = question
        self.pool = pool
        self.tools = tools
        self.caller = caller
        self.clock = clock
        self.progress = DbProgress(pool, run_id)
        self.timing = Timing.quick(clock())
        self.r: Research | None = None

    def on_wait(self, seconds: int) -> None:
        self.progress.line("note", WAITING_LINE.format(n=seconds))

    def past_hard_stop(self) -> bool:
        return self.clock() >= self.timing.hard_stop_at

    def end(self, a: _Answer, written: WrittenAnswer | None, empty_reason: str | None,
            status: str, kind: str | None, check: bool = False) -> bool:
        """`finish` (`t.rs:229-243`): the time taken, the answer stored with the final status
        and the line `Answer ready`, together (`finish_with_answer`). `check` (F4a) stores the
        `pending` completeness-check row with them. True when the answer was stored."""
        r = self.r
        elapsed = max(0, int(self.clock() - self.timing.started))
        failed = None
        try:
            body = AnswerV3(
                question=a.question, created_at=a.created_at, answer=written,
                empty_reason=empty_reason, references=a.references, searches=a.searches,
                integrity=a.integrity, stopped_early=a.stopped_early, elapsed_s=elapsed,
                notes=a.notes,
            ).to_body()
            applied = finish_with_answer(self.pool, self.run_id, status, kind, r.stop,
                                         r.rounds, body, closing_line=("present", ANSWER_READY),
                                         check=check)
        except Exception as e:  # noqa: BLE001 - psycopg's text can hold a piece of the body
            failed = type(e).__name__
        if failed is not None:
            log.error("run kind=answer_not_stored error=%s", failed)
            raise _Stop("internal")
        i = a.integrity
        log.info("run kind=answer run_id=%s written=%s references=%d dropped_unseen=%d "
                 "dropped_unfetched=%d removed_citations=%d dropped_numbers=%d "
                 "dropped_studies=%d", self.run_id, str(written is not None).lower(),
                 len(a.references), i.dropped_unseen, i.dropped_unfetched,
                 i.removed_citations, i.dropped_numbers, i.dropped_studies)
        if applied:
            log.info("run kind=finished run_id=%s status=%s error_kind=%s", self.run_id,
                     status, kind or "none")
        return applied

    def go(self) -> None:
        r = self.r = research(self.question, tools=self.tools, caller=self.caller,
                              run_id=self.run_id, timing=self.timing, progress=self.progress,
                              clock=self.clock)
        line = _stop_line(r)
        if line is not None:
            self.progress.line("note", line)
        v = r.validated
        selected = v.selected if v is not None else ()
        # A literature call cannot be cut once started (plan: Hard stop), so the hard stop is
        # checked before and after the records fetch (Incisive `gate`, `t.rs:388, 512`).
        if selected and self.past_hard_stop():
            raise HardStop()
        if v is not None:
            save_list(self.pool, self.run_id, v)  # stays on the page whatever follows
        for source in r.down:
            self.progress.line("note", DOWN_LINES[source])
        a = _Answer(self.question, list(r.searches), _stopped_early(r),
                    IntegrityV3(dropped_unseen=v.dropped_unseen if v is not None else 0))
        if r.final_failed:
            return self.end(a, None, STOPPED_NO_ANSWER, "partial", "researcher_incomplete")
        if r.empty_selection:
            return self.end(a, None, EMPTY_SELECTION, "partial", "empty_selection")
        if not selected:
            stopped = a.stopped_early is not None
            return self.end(a, None, STOPPED_NO_ANSWER if stopped else NO_EVIDENCE,
                            "partial" if stopped else "done", None)

        # The selected studies' records and abstracts: cache, then esummary and efetch.
        try:
            papers = self.tools.fetch_papers([s.pmid for s in selected])
        except ToolFailed as e:
            log.warning("run kind=records_not_fetched error_kind=%s", e.error_kind)
            raise _Stop("lit") from None  # D5: as Incisive, the run fails
        if self.past_hard_stop():
            raise HardStop()
        studies: list[StudyInput] = []
        for s in selected:
            p = papers.get(s.pmid)
            if p is None:
                a.integrity.dropped_unfetched += 1
                continue
            studies.append(StudyInput(
                pmid=s.pmid, title=p.title, journal=p.journal, year=p.year,
                pub_types=tuple(p.pub_types), doi=p.doi, retracted=p.retracted,
                abstract=p.abstract, design=s.design, why=s.why))
        n = a.integrity.dropped_unfetched
        if n > 0:
            self.progress.line("note", f"{plural(n, 'selected study', 'selected studies')} "
                                       f"could not be fetched from PubMed and "
                                       f"{'was' if n == 1 else 'were'} left out")
        a.references = references_from(studies)  # code's, in selection order
        if not studies:
            return self.end(a, None, UNFETCHED, "partial", None)

        # The writer: only with time for it before the hard stop (`t.rs:510-533`).
        left = self.timing.hard_stop_at - self.clock()
        if left < WRITER_MIN_SECONDS:
            log.info("run kind=no_time_for_writer left_s=%d", max(0, int(left)))
            self.progress.line("note", NO_TIME_LINE)
            return self.end(a, None, WRITER_NO_TIME, "partial", "writer_time")
        with_abstract = sum(1 for s in studies if s.abstract is not None and rust_trim(s.abstract))
        self.progress.line("write", f"Writer: writing the answer from "
                                    f"{plural(len(studies), 'study', 'studies')} "
                                    f"({with_abstract} with an abstract)")
        user = writer_user(self.question, studies, v.contradictions, r.searches)
        try:
            written, defaulted, merged = write_parts(
                user, caller=self.caller, run_id=self.run_id, timing=self.timing,
                on_wait=self.on_wait, progress=self.progress,
                on_part=EarlyStore(self.pool, self.run_id, a, [s.pmid for s in studies]),
                clock=self.clock)
        except WriterFailed as e:
            failure, kind = e.failure, e.kind
        else:
            failure = kind = None
        if failure == "truncated":
            self.progress.line("note", TRUNCATED_LINE)
            return self.end(a, None, WRITER_TRUNCATED, "partial", "writer_truncated")
        if failure == "unreadable":
            self.progress.line("note", UNREADABLE_LINE)
            return self.end(a, None, WRITER_UNREADABLE, "partial", "writer_unreadable")
        if kind is not None:
            raise _Stop(kind)  # `WriteFailure::Step(e)`: the run fails with the call's kind
        a.integrity.labels_defaulted = defaulted
        check_citations(written, [s.pmid for s in studies], a.integrity)
        if merged:
            a.notes.extend(merge_checks(written))
        designs = [(s.pmid, s.design) for s in studies]
        evidence_types(written, lambda p: next(
            (design_class(d) for i, d in designs if i == p), None))
        self.progress.line("check", citation_check_line(a.integrity))
        # R-S2b: an answer after research stopped by a limit is done; a failed turn: partial.
        if self.end(a, written, None, "partial" if r.stop == "turn_failed" else "done",
                    None, check=True):
            # F4a: the answer is stored and the run is final; the check counts, code only.
            run_check(self.pool, self.run_id, written, papers)


def _failed(pool: ConnectionPool, run_id: UUID, kind: str) -> None:
    if finish_run(pool, run_id, "failed", kind, None, None):
        log.info("run kind=finished run_id=%s status=failed error_kind=%s", run_id, kind)


def run_one(
    run_id: UUID,
    *,
    pool: ConnectionPool,
    tools: LitTools,
    caller: ModelCaller,
    model: str,
    clock: Callable[[], float] = time.monotonic,
    start_intro: Callable[..., threading.Thread] = start_intro_thread,
) -> None:
    """Runs one queued run to its end (see the module doc). Never raises. `start_intro`
    starts the Introduction (tests pass one that writes it at once)."""
    intro: threading.Thread | None = None
    try:
        question = question_of(pool, run_id)
        if question is None:
            log.warning("run kind=not_found run_id=%s", run_id)
            return
        set_running(pool, run_id)
        log.info("run kind=running run_id=%s model=%s", run_id, model)
        run = _Run(run_id, question, pool, tools, caller, clock)
        try:
            intro = start_intro(run_id, question, caller=caller, pool=pool, clock=clock)
        except Exception as e:  # noqa: BLE001 - no Introduction; the run goes on
            log.warning("run kind=intro_not_started run_id=%s error=%s", run_id,
                        type(e).__name__)
        try:
            run.go()
        except FatalModelError as e:
            _failed(pool, run_id, e.kind)
        except HardStop:
            _failed(pool, run_id, "hard_stop")
        except RunCapReached:
            _failed(pool, run_id, "run_call_cap")
        except _Stop as e:
            _failed(pool, run_id, e.kind)
    except Exception as e:  # noqa: BLE001 - a run never takes the app down; kind only
        log.error("run kind=internal error=%s", type(e).__name__)
        try:
            finish_run(pool, run_id, "failed", "internal", None, None)
        except Exception:  # noqa: BLE001
            log.error("run kind=not_finished run_id=%s", run_id)
    finally:
        if intro is not None:
            # Waits at most INTRO_TIME_LIMIT + 5 s. A reply body that stalls can outlive this
            # join: the thread is then still reading its one request, and nothing more is sent
            # for the run (one attempt only). Its row is finished when that read ends, or, if
            # the process stops first, left `pending` for `stop_unfinished` at the next start.
            intro.join(INTRO_TIME_LIMIT + 5)
        caller.forget(run_id)  # the run's attempt count is no longer needed
