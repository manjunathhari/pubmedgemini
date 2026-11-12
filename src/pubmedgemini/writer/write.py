"""The writer's calls: four parts on four threads, one retry per part, and the one-piece
fallback.

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/agent/writer_v3.rs`):

| Here | Incisive |
|---|---|
| `section_reply` | `section_reply` (first call, then one retry with SCHEMA_NUDGE when the reply is cut off or unreadable) |
| `one_piece` | `write_counted` (the same, on the whole schema) |
| `write_quick` | `write_quick_parallel_with` (a failed part never cancels the others; the decision is made once all four are in) |
| `WriteFailure` | `WriteFailure` (`Truncated`, `Unreadable`, `Step`) |
| fatal errors | `fatal` (here: a fatal GeminiError kind, the run's attempt cap, the hard stop) |

On Gemini (F3 plan, "Differences from Incisive"): the four threads share the F2 pacer, so
their starts are spaced 6 s apart at 10 requests a minute (X2); the order in which they
reach it is not fixed. The retry is sent at the model's default effort (X3). A wait for the
free limit that would pass the hard stop is `no_time` (X10). No money holds (X7), no
streaming (X8), no early summary (X9, F4). A refused reply (a Gemini block) is a failed call,
never read and not retried, as Incisive's `refusal` (`llm.rs` `reply`: `StepError::Llm`).

Every call goes through the run's ModelCaller (pacing, the day's count, the per-run attempt
cap). Log lines hold the part, the attempt, fixed outcome words, categories and token counts
only: never the prompt, the reply or any answer text. Errors are raised outside `except`
blocks, so they carry no context.
"""

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pubmedgemini.gemini.caller import DeadlineReached, ModelCaller, RunCapReached
from pubmedgemini.gemini.client import FATAL_KINDS, GeminiError, Reply, structured_body
from pubmedgemini.gemini.structured import Unreadable, read_structured
from pubmedgemini.researcher.loop import (
    REFUSAL_FINISHES,
    WAITING_LINE,
    WRITER_MIN_SECONDS,
    FatalModelError,
    HardStop,
    Progress,
    Timing,
)
from pubmedgemini.researcher.prompts import SCHEMA_NUDGE, with_source_rule
from pubmedgemini.writer.answer import (
    WrittenAnswer,
    into_answer,
    merge_sections,
    typed_part,
    typed_whole,
)
from pubmedgemini.writer.prompts import (
    SYSTEM,
    WRITER_MAX_TOKENS,
    Section,
    section_schema,
    section_user,
    writer_schema,
)

log = logging.getLogger("pubmedgemini.writer")

# Incisive `run_tracker.rs` `one_piece_line`.
ONE_PIECE_LINE = "Writer: section {} failed; writing the answer in one piece"

FailureKind = Literal["truncated", "unreadable", "call", "no_time"]


class WriteFailure(Exception):
    """The writer produced no answer. `kind`: cut off (`truncated`), unreadable, a failed
    call (`call`, with its GeminiError kind or `refused` in `error_kind`), or a wait that
    would pass the hard stop (`no_time`). `str()` is the kind only."""

    def __init__(self, kind: FailureKind, error_kind: str | None = None) -> None:
        super().__init__(kind)
        self.kind = kind
        self.error_kind = error_kind


@dataclass(frozen=True)
class Written:
    answer: WrittenAnswer
    labels_defaulted: int
    merged: bool  # False: written in one piece after a part failed


_FATAL = (HardStop, FatalModelError, RunCapReached)


def _check_hard_stop(timing: Timing, clock: Callable[[], float]) -> None:
    if clock() >= timing.hard_stop_at:
        raise HardStop()


def _refused(reply: Reply) -> bool:
    return bool(reply.block_reason) or reply.finish in REFUSAL_FINISHES


def _log(word: str, attempt: str, outcome: str, category: str, output_tokens: int) -> None:
    log.info("writer part=%s attempt=%s outcome=%s category=%s output_tokens=%d",
             word, attempt, outcome, category, output_tokens)


def _call(body: dict, step: str, *, caller: ModelCaller, run_id: UUID | None, timing: Timing,
          clock: Callable[[], float], on_wait: Callable[[int], None]) -> Reply | WriteFailure:
    """One call: the reply, or the failure it is (never raised here). Raises HardStop when
    the clock is past the hard stop before or after the call, FatalModelError for a fatal
    kind, and the caller's RunCapReached."""
    _check_hard_stop(timing, clock)
    kind: str | None = None
    late = False
    try:
        reply = caller.call(body, step=step, run_id=run_id, deadline=timing.hard_stop_at,
                            attempt_deadline=timing.hard_stop_at, on_wait=on_wait)
    except GeminiError as e:
        kind = e.kind
    except DeadlineReached:
        late = True
    _check_hard_stop(timing, clock)
    if kind in FATAL_KINDS:
        raise FatalModelError(kind)
    if kind is not None:
        return WriteFailure("call", kind)
    if late:
        return WriteFailure("no_time")
    if _refused(reply):
        return WriteFailure("call", "refused")
    return reply


def _read(reply: Reply, typed: Callable[[Any], dict | None]) -> tuple[dict | None, str]:
    try:
        return read_structured(reply, typed), "none"
    except Unreadable as e:
        return None, e.category


def _written(word: str, attempt: str, got: Reply | WriteFailure,
             typed: Callable[[Any], dict | None]) -> tuple[dict | None, str]:
    """Reads one attempt's reply and logs it: (the typed value or None, the category)."""
    if isinstance(got, WriteFailure):
        _log(word, attempt, "no_time" if got.kind == "no_time" else "call_failed", "none", 0)
        return None, "none"
    value, category = _read(got, typed)
    outcome = "ok" if value is not None else (
        "truncated" if category == "truncated" else "unreadable")
    _log(word, attempt, outcome, category, got.usage.output)
    return value, category


def _two_tries(word: str, body: dict, retry_body: dict, steps: tuple[str, str],
               typed: Callable[[Any], dict | None], **call: Any) -> dict:
    """The first call and, when its reply is cut off or unreadable, one retry with the
    nudge. Raises WriteFailure: a failed first call as its failure; a cut-off reply as
    `truncated` (also when the retry is not made, fails or is unreadable); an unreadable one
    as `unreadable` unless the retry is cut off."""
    first = _call(body, steps[0], **call)
    value, _ = _written(word, "first", first, typed)
    if value is not None:
        return value
    if isinstance(first, WriteFailure):
        raise first
    fail: FailureKind = "truncated" if first.finish == "MAX_TOKENS" else "unreadable"
    second = _call(retry_body, steps[1], **call)
    value, _ = _written(word, "retry", second, typed)
    if value is not None:
        return value
    if isinstance(second, Reply) and second.finish == "MAX_TOKENS":
        fail = "truncated"
    raise WriteFailure(fail)


def _no_wait_line(_seconds: int) -> None:
    pass


def section_reply(section: Section, user: str, *, caller: ModelCaller, run_id: UUID | None,
                  timing: Timing, clock: Callable[[], float] = time.monotonic,
                  on_wait: Callable[[int], None] = _no_wait_line) -> dict:
    """One part: its typed wire dict. Raises WriteFailure, HardStop, FatalModelError,
    RunCapReached."""
    system = with_source_rule(SYSTEM)
    text = section_user(user, section)
    schema = section_schema(section)
    max_tokens = section.max_tokens(WRITER_MAX_TOKENS)
    return _two_tries(
        section.word,
        structured_body(system, text, schema, max_tokens),
        structured_body(system, text + SCHEMA_NUDGE, schema, max_tokens),
        (f"writer_{section.word}", f"writer_{section.word}_retry"),
        typed_part(section),
        caller=caller, run_id=run_id, timing=timing, clock=clock, on_wait=on_wait,
    )


def one_piece(user: str, *, caller: ModelCaller, run_id: UUID | None, timing: Timing,
              clock: Callable[[], float] = time.monotonic,
              on_wait: Callable[[int], None] = _no_wait_line) -> dict:
    """The whole answer in one call (and one retry): its typed wire dict."""
    system = with_source_rule(SYSTEM)
    schema = writer_schema()
    return _two_tries(
        "one_piece",
        structured_body(system, user, schema, WRITER_MAX_TOKENS),
        structured_body(system, user + SCHEMA_NUDGE, schema, WRITER_MAX_TOKENS),
        ("writer_one_piece", "writer_one_piece_retry"),
        typed_whole,
        caller=caller, run_id=run_id, timing=timing, clock=clock, on_wait=on_wait,
    )


def _guarded(section: Section, user: str, **kw: Any) -> dict | BaseException:
    """A part's value, or its failure as a value (so every part finishes before any
    decision, and nothing is raised across the thread)."""
    try:
        return section_reply(section, user, **kw)
    except (WriteFailure, *_FATAL) as e:
        return e


def _done(a: WrittenAnswer, defaulted: int, merged: bool) -> Written:
    log.info("writer done merged=%s outcomes=%d numbers=%d studies=%d labels_defaulted=%d",
             "true" if merged else "false", len(a.outcomes),
             sum(len(o.numbers) for o in a.outcomes), len(a.studies), defaulted)
    return Written(a, defaulted, merged)


def write_quick(
    user: str,
    *,
    caller: ModelCaller,
    run_id: UUID | None,
    timing: Timing,
    progress: Progress,
    clock: Callable[[], float] = time.monotonic,
    executor_factory: Callable[[], ThreadPoolExecutor] = lambda: ThreadPoolExecutor(
        4, thread_name_prefix="writer"),
) -> Written:
    """The Quick answer in four parts, else in one piece. Raises WriteFailure, HardStop,
    FatalModelError (a kind in FATAL_KINDS) and RunCapReached."""

    def on_wait(seconds: int) -> None:
        progress.line("note", WAITING_LINE.format(n=seconds))

    kw = {"caller": caller, "run_id": run_id, "timing": timing, "clock": clock,
          "on_wait": on_wait}
    with executor_factory() as ex:
        futures = [ex.submit(_guarded, s, user, **kw) for s in Section]
        wait(futures)
    results = {s: f.result() for s, f in zip(Section, futures, strict=True)}
    fatal = next((r for r in results.values() if isinstance(r, _FATAL)), None)
    if fatal is not None:
        raise fatal
    failed = [(s, r) for s, r in results.items() if isinstance(r, WriteFailure)]
    if not failed:
        a, defaulted = into_answer(merge_sections(results))
        return _done(a, defaulted, True)
    log.info("writer parts_failed parts=%s", ",".join(s.word for s, _ in failed))
    section, failure = failed[0]
    if timing.hard_stop_at - clock() < WRITER_MIN_SECONDS:
        raise failure
    progress.line("write", ONE_PIECE_LINE.format(section.word))
    a, defaulted = into_answer(one_piece(user, **kw))
    return _done(a, defaulted, False)
