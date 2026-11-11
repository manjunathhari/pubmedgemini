"""The writer run: four parts at once on threads, one retry per part, the merge, the one-piece
fallback, and the part events for an early summary.

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/agent/writer_v3.rs`, `w.rs:N`):

| Here | Incisive |
|---|---|
| `write_parts` | `w.rs:2145-2310` `write_quick_parallel_with` |
| `_write` for a part | `w.rs:2075-2123` `section_reply` |
| `write_one_piece`, `_write` for the whole | `w.rs:1614-1670` `write_counted` |
| `WriteFailure`, `WriterFailed` | `w.rs:1574-1586` `WriteFailure` |
| `PartEvent` | `w.rs:2023-2036` `PartEvent` |
| `ONE_PIECE_LINE` | `run_tracker.rs:161-166` `one_piece_line` |
| `WRITER_MIN_SECONDS` (150 s) | `agent/team.rs:152` `WRITER_MIN_TIME_QUICK` (via `researcher/loop.py`) |

Rules, as Incisive's:

* A part's first call fails (a GeminiError the caller did not retry away): the part failed,
  with that kind; no nudge retry (`StepError::Llm`).
* A part's reply cut off (`MAX_TOKENS`) or unreadable: ONE retry, the same body with
  SCHEMA_NUDGE after the part's user text (D7: Incisive's `effort: low` is dropped). The
  failure is `truncated` if either reply was cut off, else `unreadable`; a retry call that
  fails keeps the first reply's failure.
* All four are sent at once and waited for; a failure never cancels the others.
* All four read: merged (`merge_sections`, `into_answer`). Else, with less than 150 s left to
  the hard stop, the first failed part's failure (in Section order) is raised; otherwise the
  step line, and the whole answer is written in one piece by the same rules.
* Events: the context and summary parts are reported when read; the first part to fail for
  good sends `withdraw`; under one lock, nothing is reported after it.

Differences (plan, owner's answers): a Gemini block (`blockReason`, or a refusal finish) counts
as unreadable and gets the one retry (Q3; Incisive fails the call). The daily limit, a refused
key or billing (`FATAL_KINDS`), the run's cap (`RunCapReached`) and the hard stop end the run at
once, with no one-piece fallback, once all four parts are in (Q2; Incisive fails the part and
tries the one piece). The fatal kinds raise `FatalModelError`, as the researcher's do. The hard
stop is any call's DeadlineReached (the caller's deadline is the hard stop) or the clock at or
past the hard stop after a call (Incisive's worker drops the run then).

Threads: every part runs on its own thread of one ThreadPoolExecutor, left only when all four
are done. They share the ModelCaller (its per-run count is under a lock), the pacer (one lock,
one schedule), one httpx.Client (documented as shareable between threads), the database pool
(each write takes a connection for one statement) and `on_wait` (the runner's step writer
serialises its lines). An exception in a part is caught on its thread and raised here, so no
thread dies with a traceback.

Log lines hold part names, fixed kinds and counts only: never the question, the studies, an
abstract or the model's text.
"""

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from pubmedgemini.gemini.caller import DeadlineReached, ModelCaller, RunCapReached
from pubmedgemini.gemini.client import (
    FATAL_KINDS,
    GeminiError,
    Reply,
    finish_log_kind,
    structured_body,
)
from pubmedgemini.gemini.structured import Unreadable, read_structured
from pubmedgemini.researcher.loop import (
    REFUSAL_FINISHES,
    WRITER_MIN_SECONDS,
    FatalModelError,
    HardStop,
    Timing,
)
from pubmedgemini.researcher.prompts import SCHEMA_NUDGE, with_source_rule
from pubmedgemini.writer.answer import WrittenAnswer
from pubmedgemini.writer.prompts import (
    SYSTEM,
    WRITER_MAX_TOKENS,
    Section,
    section_schema,
    section_user,
    writer_schema,
)
from pubmedgemini.writer.read import (
    as_context,
    as_outcomes,
    as_summary,
    as_trials,
    as_whole,
    into_answer,
    merge_sections,
)

__all__ = [
    "ONE_PIECE_LINE", "PartEvent", "WriteFailure", "WriterFailed", "write_one_piece",
    "write_parts",
]

log = logging.getLogger("pubmedgemini.writer")

WriteFailure = Literal["truncated", "unreadable"]

# run_tracker.rs:161-166: `Writer: section {name}` + ONE_PIECE.
ONE_PIECE_LINE = "Writer: section {name} failed; writing the answer in one piece"

PartEvent = (
    tuple[Literal["summary"], dict]
    | tuple[Literal["context"], dict]
    | tuple[Literal["withdraw"], None]
)

_READERS: dict[str, Callable[[Any], dict | None]] = {
    "trials": as_trials,
    "outcomes": as_outcomes,
    "context": as_context,
    "summary": as_summary,
}


class WriterFailed(Exception):
    """The writer produced no answer. `failure` is `truncated` or `unreadable` (a reply and its
    retry); else `kind` is the GeminiError kind of a call that failed (Incisive
    `WriteFailure::Step`). Holds nothing else."""

    def __init__(self, failure: WriteFailure | None = None, kind: str | None = None) -> None:
        super().__init__(failure or kind or "writer_failed")
        self.failure = failure
        self.kind = kind


class Lines(Protocol):
    def line(self, kind: str, text: str) -> None: ...


@dataclass(frozen=True)
class _Failed:
    """A part (or the one piece) failed, not fatally."""

    failure: WriteFailure | None
    kind: str | None


@dataclass(frozen=True)
class _Raised:
    """A part ended with an exception (fatal, or a bug), to be raised on the calling thread."""

    error: BaseException


def _refused(reply: Reply) -> bool:
    return bool(reply.block_reason) or reply.finish in REFUSAL_FINISHES


def _log(part: str, attempt: str, result: str, kind: str, output_tokens: int) -> None:
    log.info("writer part=%s attempt=%s result=%s kind=%s output_tokens=%d", part, attempt,
             result, kind, output_tokens)


class _Writer:
    """One structured write (a part or the one piece): its calls, its reading, its retry."""

    def __init__(self, caller: ModelCaller, run_id: UUID | None, timing: Timing,
                 on_wait: Callable[[int], None], clock: Callable[[], float]) -> None:
        self.caller = caller
        self.run_id = run_id
        self.timing = timing
        self.on_wait = on_wait
        self.clock = clock

    def call(self, body: dict, step: str, part: str, attempt: str) -> Reply | str:
        """The reply, or the kind of the non-fatal GeminiError the call ended with (after
        the caller's own attempts). Raises HardStop, FatalModelError and RunCapReached."""
        kind: str | None = None
        late = False
        try:
            reply = self.caller.call(body, step=step, run_id=self.run_id,
                                     deadline=self.timing.hard_stop_at, on_wait=self.on_wait,
                                     attempt_deadline=self.timing.hard_stop_at)
        except DeadlineReached:
            late = True
        except GeminiError as e:
            kind = e.kind
        except RunCapReached:
            _log(part, attempt, "error", "run_call_cap", 0)
            raise
        # Raised outside the handlers: nothing that leaves here has a __context__.
        if late or self.clock() >= self.timing.hard_stop_at:
            _log(part, attempt, "error", "hard_stop", 0)
            raise HardStop()
        if kind is not None:
            _log(part, attempt, "error", kind, 0)
            if kind in FATAL_KINDS:
                raise FatalModelError(kind)
            return kind
        return reply

    def read(self, reply: Reply, reader: Callable[[Any], Any], part: str,
             attempt: str) -> Any | None:
        """The typed reply, or None (cut off, unreadable or refused), logged by kind."""
        out = reply.usage.output
        if _refused(reply):
            kind = "block" if reply.block_reason else finish_log_kind(reply.finish)
            _log(part, attempt, "refused", kind, out)
            return None
        category: str | None = None
        try:
            got = read_structured(reply, reader, rules="serde")
        except Unreadable as e:
            category = e.category
        if category is not None:
            _log(part, attempt, "cut_off" if category == "truncated" else "unreadable",
                 category, out)
            return None
        _log(part, attempt, "ok", "none", out)
        return got

    def write(self, system: str, user: str, schema: dict, max_tokens: int,
              reader: Callable[[Any], Any], step: str, part: str) -> Any:
        """`section_reply` / `write_counted`: the typed reply, or _Failed."""
        first = self.call(structured_body(system, user, schema, max_tokens), step, part, "first")
        if isinstance(first, str):
            return _Failed(None, first)
        got = self.read(first, reader, part, "first")
        if got is not None:
            return got
        fail: WriteFailure = "truncated" if first.finish == "MAX_TOKENS" else "unreadable"
        retry = self.call(structured_body(system, user + SCHEMA_NUDGE, schema, max_tokens),
                          f"{step}_retry", part, "retry")
        if isinstance(retry, str):
            return _Failed(fail, None)
        got = self.read(retry, reader, part, "retry")
        if got is not None:
            return got
        if retry.finish == "MAX_TOKENS":
            fail = "truncated"
        return _Failed(fail, None)


def write_one_piece(
    user: str,
    *,
    caller: ModelCaller,
    run_id: UUID | None,
    timing: Timing,
    on_wait: Callable[[int], None],
    clock: Callable[[], float] = time.monotonic,
) -> tuple[WrittenAnswer, int]:
    """The whole answer in one call (the live schema, WRITER_MAX_TOKENS), with one nudge
    retry: the answer and the labels set to their default. Raises WriterFailed, HardStop,
    FatalModelError and RunCapReached."""
    w = _Writer(caller, run_id, timing, on_wait, clock)
    got = w.write(with_source_rule(SYSTEM), user, writer_schema(), WRITER_MAX_TOKENS, as_whole,
                  "writer_one_piece", "one_piece")
    if isinstance(got, _Failed):
        raise WriterFailed(got.failure, got.kind)
    return into_answer(got)


def _safe(on_part: Callable[[PartEvent], None], ev: PartEvent) -> None:
    try:
        on_part(ev)
    except Exception as e:  # noqa: BLE001 - reporting never fails the writer
        log.warning("writer kind=callback_failed error=%s", type(e).__name__)


def write_parts(
    user: str,
    *,
    caller: ModelCaller,
    run_id: UUID | None,
    timing: Timing,
    on_wait: Callable[[int], None],
    progress: Lines,
    on_part: Callable[[PartEvent], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[WrittenAnswer, int, bool]:
    """The Quick answer in four parts (see the module doc): the answer, the labels set to
    their default, and whether it was merged from the four parts (False: written in one
    piece after a part failed). `progress.line` takes the one-piece step line. Raises
    WriterFailed, HardStop, FatalModelError and RunCapReached; never before every part's
    thread has ended."""
    lock = threading.Lock()
    withdrawn = False

    def report(ev: PartEvent) -> None:
        with lock:
            if on_part is not None and not withdrawn:
                _safe(on_part, ev)

    def withdraw() -> None:
        nonlocal withdrawn
        with lock:
            if not withdrawn:
                withdrawn = True
                if on_part is not None:
                    _safe(on_part, ("withdraw", None))

    system = with_source_rule(SYSTEM)

    def run(s: Section) -> dict | _Failed | _Raised:
        w = _Writer(caller, run_id, timing, on_wait, clock)
        try:
            got = w.write(system, section_user(user, s), section_schema(s),
                          s.max_tokens(WRITER_MAX_TOKENS), _READERS[s.name], f"writer_{s.name}",
                          s.name)
        except Exception as e:  # noqa: BLE001 - raised on the calling thread, below
            withdraw()
            return _Raised(e)
        if isinstance(got, _Failed):
            withdraw()
        elif s is Section.CONTEXT:
            report(("context", got))
        elif s is Section.SUMMARY:
            report(("summary", got))
        return got

    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="writer") as pool:
        futures = [pool.submit(run, s) for s in Section.ALL]
        results = [f.result() for f in futures]  # every part, in Section order

    for r in results:
        if isinstance(r, _Raised):
            raise r.error
    failed = [(s, r) for s, r in zip(Section.ALL, results, strict=True)
              if isinstance(r, _Failed)]
    if not failed:
        t, o, c, s_ = results
        answer, defaulted = into_answer(merge_sections(t, o, c, s_))
        log.info("writer kind=merged labels_defaulted=%d", defaulted)
        return answer, defaulted, True
    section, failure = failed[0]
    log.warning("writer kind=parts_failed failed=%d first=%s", len(failed), section.name)
    left = timing.hard_stop_at - clock()
    if left < WRITER_MIN_SECONDS:
        log.info("writer kind=no_time_for_one_piece left_s=%d", max(0, int(left)))
        raise WriterFailed(failure.failure, failure.kind)
    progress.line("write", ONE_PIECE_LINE.format(name=section.name))
    answer, defaulted = write_one_piece(user, caller=caller, run_id=run_id, timing=timing,
                                        on_wait=on_wait, clock=clock)
    return answer, defaulted, False
