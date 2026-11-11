"""One run end to end, on a background thread.

`run_one` reads the stored question, marks the run running, runs the researcher with a
progress writer that stores every line, search and seen paper at once, then writes the stop
line (Incisive `agent/team.rs` `run`: `Search stopped at the round cap / the Quick limit after
N rounds`, or `Research stopped early (a failed model call)`), fetches the selected studies'
records into the cache (a failure is logged; the list stands), stores the list, the source-down
line, the closing line and the outcome:

| Researcher result | Status | error_kind |
|---|---|---|
| FatalModelError | failed | its kind |
| HardStop, or the hard stop passed before or after the records fetch | failed | hard_stop |
| final_failed | partial | researcher_incomplete |
| empty_selection | partial | empty_selection |
| nothing selected | done (partial after turn_failed or wrap_up) | none |
| studies selected | done (partial after turn_failed) | none |
| anything else raised | failed | internal |

A run is a plain function on one daemon thread: every client below is synchronous, and the
POST that starts it never waits for it. Logs hold run ids and fixed kinds only.
"""

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol
from uuid import UUID

from psycopg_pool import ConnectionPool

from pubmedgemini.gemini.caller import ModelCaller
from pubmedgemini.lit.tools import LitTools, ToolFailed
from pubmedgemini.researcher.loop import (
    FatalModelError,
    HardStop,
    Research,
    SearchEntry,
    SeenPaper,
    Timing,
    research,
)
from pubmedgemini.runs import (
    DOWN_LINES,
    STUDY_LIST_READY,
    add_search,
    add_seen,
    add_step,
    finish_run,
    question_of,
    save_list,
    set_running,
)

log = logging.getLogger("pubmedgemini.runner")

STOPPED_EARLY_LINE = "Research stopped early (a failed model call)"


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


def _outcome(r: Research) -> tuple[str, str | None]:
    if r.final_failed:
        return "partial", "researcher_incomplete"
    if r.empty_selection:
        return "partial", "empty_selection"
    selected = r.validated.selected if r.validated is not None else ()
    if not selected:
        return ("partial" if r.stop in ("turn_failed", "wrap_up") else "done"), None
    return ("partial" if r.stop == "turn_failed" else "done"), None


def _hard_stop(pool: ConnectionPool, run_id: UUID) -> None:
    finish_run(pool, run_id, "failed", "hard_stop", None, None)
    log.info("run kind=finished run_id=%s status=failed error_kind=hard_stop", run_id)


def run_one(
    run_id: UUID,
    *,
    pool: ConnectionPool,
    tools: LitTools,
    caller: ModelCaller,
    model: str,
    clock: Callable[[], float] = time.monotonic,
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
            finish_run(pool, run_id, "failed", e.kind, None, None)
            log.info("run kind=finished run_id=%s status=failed error_kind=%s", run_id, e.kind)
            return
        except HardStop:
            _hard_stop(pool, run_id)
            return
        line = _stop_line(r)
        if line is not None:
            progress.line("note", line)
        if r.validated is not None and r.validated.selected:
            # A literature call cannot be cut once started (plan: Hard stop), so the hard
            # stop is checked before and after it.
            if clock() >= timing.hard_stop_at:
                _hard_stop(pool, run_id)
                return
            try:
                tools.fetch_papers([s.pmid for s in r.validated.selected])
            except ToolFailed as e:
                log.warning("run kind=records_not_fetched error_kind=%s", e.error_kind)
            if clock() >= timing.hard_stop_at:
                _hard_stop(pool, run_id)
                return
        if r.validated is not None:
            save_list(pool, run_id, r.validated)
        for source in r.down:
            progress.line("note", DOWN_LINES[source])
        progress.line("present", STUDY_LIST_READY)
        status, kind = _outcome(r)
        finish_run(pool, run_id, status, kind, r.stop, r.rounds)
        log.info("run kind=finished run_id=%s status=%s error_kind=%s", run_id, status,
                 kind or "none")
    except Exception as e:  # noqa: BLE001 - a run never takes the app down; kind only
        log.error("run kind=internal error=%s", type(e).__name__)
        try:
            finish_run(pool, run_id, "failed", "internal", None, None)
        except Exception:  # noqa: BLE001
            log.error("run kind=not_finished run_id=%s", run_id)
