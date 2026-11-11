"""One model call: the pacer, the day's count, retries and the waiting line.

Retries follow Incisive's `retry.rs` `RetryPolicy::default` (3 attempts; 0.5 s x 2^(n-1),
jittered 0.8 to 1.2, at most 8 s) on a per-minute 429, an overloaded status, a timeout or a
network error. One difference (plan, "Differences from Incisive"): Google's asked delay is
waited however long it is, as long as it ends before the caller's deadline; free-tier delays
are tens of seconds. A wait that would end at or after the deadline raises DeadlineReached at
once, so the loop can wrap up instead of failing.

A run sends at most RUN_ATTEMPTS attempts in all (owner decision, 11 Nov 2026: cap calls per
run), counted here per run id over every step (turns, the list call, its nudge and the S3
retry) and every retry, as the day count counts them. At the cap nothing more is sent for that
run: RunCapReached, and the run ends failed `run_call_cap`.

Log lines hold the step, the attempt, the kind, the finish kind, token counts and latency
only: never the body, the reply, the key or Google's message.
"""

import logging
import random
import threading
import time
from collections.abc import Callable
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from pubmedgemini.gemini.client import (
    ATTEMPT_TIMEOUT_SECONDS,
    GeminiClient,
    GeminiError,
    Reply,
    finish_log_kind,
)
from pubmedgemini.gemini.quota import (
    RUN_ATTEMPTS,
    GeminiPacer,
    estimate_tokens,
    finish_attempt,
    record_attempt,
)

log = logging.getLogger("pubmedgemini.gemini")

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 8.0
# Before a wait this long or longer for a 429, the caller's `on_wait` writes the spec §8 line.
WAIT_LINE_MIN_SECONDS = 1.0

RETRYABLE_KINDS = frozenset({"rate_limited", "overloaded", "timeout", "network"})


class DeadlineReached(Exception):
    """The pacer, or a wait Google asked for, would end at or after the deadline; nothing
    more is sent. Not a GeminiError: the loop tells "out of room" from a failed call."""

    def __init__(self) -> None:
        super().__init__("deadline_reached")


class RunCapReached(Exception):
    """This run has sent RUN_ATTEMPTS attempts; nothing more is sent for it. Neither a
    GeminiError nor DeadlineReached, so no step of the loop takes it for a failed or late call:
    the runner ends the run failed `run_call_cap`."""

    def __init__(self) -> None:
        super().__init__("run_call_cap")


class ModelCaller:
    def __init__(
        self,
        client: GeminiClient,
        pacer: GeminiPacer,
        pool: ConnectionPool,
        model: str,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._pacer = pacer
        self._pool = pool
        self.model = model
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self.run_cap = RUN_ATTEMPTS
        self._cap_lock = threading.Lock()
        self._sent: dict[UUID | None, int] = {}  # attempts sent, per run id

    def __repr__(self) -> str:
        return "ModelCaller()"

    def _backoff(self, attempt: int) -> float:
        """Incisive `delay_for` without an asked delay; `attempt` is the one that failed."""
        unit = min(max(self._jitter(), 0.0), 1.0)
        raw = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
        return min(raw * (0.8 + 0.4 * unit), BACKOFF_MAX_SECONDS)

    def _at_cap(self, run_id: UUID | None) -> bool:
        with self._cap_lock:
            return self._sent.get(run_id, 0) >= self.run_cap

    def _take_attempt(self, run_id: UUID | None) -> bool:
        """Counts one attempt for `run_id`; False, counting nothing, at the cap."""
        with self._cap_lock:
            n = self._sent.get(run_id, 0)
            if n >= self.run_cap:
                return False
            self._sent[run_id] = n + 1
            return True

    def forget(self, run_id: UUID | None) -> None:
        """Drops the run's attempt count once the run has ended, so the map does not grow
        with every run. Nothing is sent for a run after this call ends it."""
        with self._cap_lock:
            self._sent.pop(run_id, None)

    def _cap_reached(self, step: str, run_id: UUID | None) -> RunCapReached:
        with self._cap_lock:
            n = self._sent.get(run_id, 0)
        log.info("gemini kind=run_call_cap step=%s attempts=%d cap=%d", step, n, self.run_cap)
        return RunCapReached()

    def _finish(self, call_id: int, kind: str, reply: Reply | None, latency_ms: int) -> None:
        """Best effort: the `sent` row already counts the attempt."""
        try:
            finish_attempt(
                self._pool, call_id, status_kind=kind,
                usage=reply.usage if reply else None, latency_ms=latency_ms,
            )
        except psycopg.Error:
            log.warning("gemini kind=count_not_finished")

    def call(
        self,
        body: dict,
        *,
        step: str,
        run_id: UUID | None,
        deadline: float,
        on_wait: Callable[[int], None],
        attempt_deadline: float | None = None,
    ) -> Reply:
        """The reply, after at most MAX_ATTEMPTS attempts. Raises RunCapReached when the next
        attempt would pass the run's cap, DeadlineReached when the pacer or a wait would pass
        `deadline`, else the last GeminiError.

        `deadline` decides whether an attempt starts; an attempt once sent is bounded by
        `attempt_deadline` (default `deadline`) and the 120 s attempt timeout. Incisive checks
        the wrap-up only before a turn and lets the turn in flight run to the hard stop."""
        limit = deadline if attempt_deadline is None else attempt_deadline
        estimate = estimate_tokens(body)
        attempt = 0
        while True:
            attempt += 1
            if self._at_cap(run_id):  # before the pacer, so a capped run waits for nothing
                raise self._cap_reached(step, run_id)
            if not self._pacer.wait(estimate, deadline):
                raise DeadlineReached()
            now = self._clock()
            if deadline - now <= 0 or limit - now <= 0:
                raise DeadlineReached()
            left = limit - now
            if not self._take_attempt(run_id):  # counted with the `sent` row, as the day's
                raise self._cap_reached(step, run_id)
            call_id = record_attempt(
                self._pool, model=self.model, run_id=run_id, step=step, attempt=attempt
            )
            started = self._clock()
            try:
                reply = self._client.generate(body, min(ATTEMPT_TIMEOUT_SECONDS, left))
            except GeminiError as e:
                err = e
            else:
                latency_ms = round((self._clock() - started) * 1000)
                if reply.usage.prompt > 0:
                    self._pacer.note(reply.usage.prompt)
                self._finish(call_id, "ok", reply, latency_ms)
                log.info(
                    "gemini step=%s attempt=%d status=ok finish=%s prompt_tokens=%d"
                    " output_tokens=%d thought_tokens=%d latency_ms=%d",
                    step, attempt, finish_log_kind(reply.finish), reply.usage.prompt,
                    reply.usage.output, reply.usage.thoughts, latency_ms,
                )
                return reply
            latency_ms = round((self._clock() - started) * 1000)
            self._finish(call_id, err.kind, None, latency_ms)
            log.info(
                "gemini step=%s attempt=%d status=%s finish=none prompt_tokens=0"
                " output_tokens=0 thought_tokens=0 latency_ms=%d",
                step, attempt, err.kind, latency_ms,
            )
            if err.kind not in RETRYABLE_KINDS or attempt >= MAX_ATTEMPTS:
                raise err
            wait = err.retry_after if err.retry_after is not None else self._backoff(attempt)
            if self._clock() + wait >= deadline:
                raise DeadlineReached()
            if err.kind == "rate_limited" and wait >= WAIT_LINE_MIN_SECONDS:
                on_wait(round(wait))
            self._sleep(wait)
