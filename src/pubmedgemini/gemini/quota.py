"""The free tier's limits: a pacer for requests and tokens a minute, and the day's count.

`GeminiPacer` is one per process: every Gemini attempt waits on it. The day's count lives in
`model_calls` (no text), one row per attempt, written as `sent` before the request goes out
so a crash still counts it. Google's free day resets at midnight Pacific (spec §3), so the
count is by `quota_day`, worked out in Postgres; so is the reset time shown in IST.
"""

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from uuid import UUID

from psycopg_pool import ConnectionPool

from pubmedgemini.gemini.client import Usage

WINDOW_SECONDS = 60.0
# Incisive estimates a prompt at 3 bytes a token (`agent/team.rs` module doc).
BYTES_PER_TOKEN = 3

# The most calls the researcher makes without transport retries: 8 turns, the study-list
# call, its one JSON nudge retry and the one S3 retry (Incisive `researcher.rs`, `llm.rs`
# `start_structured_from`).
RESEARCHER_CALLS = 11
# The most calls the writer makes (F3 plan D1 option A, owner decision of 12 Nov 2026:
# Incisive exactly): four parts, one retry each, the one-piece fallback and its retry
# (Incisive `writer_v3.rs` `section_reply`, `write_quick_parallel_with`, `write_counted`).
WRITER_CALLS = 10
# The most calls one run makes without transport retries. F4 adds number completion (one
# call, no retry).
RUN_CALLS = RESEARCHER_CALLS + WRITER_CALLS
# Room for transport retries.
CALL_RESERVE = 3
# The most attempts one run may send, every attempt counted as the day count counts it (owner
# decision, 11 Nov 2026: cap calls per run). `ModelCaller` enforces it per run id; `runs_left`
# admits a run only when the day still has room for all of them.
RUN_ATTEMPTS = RUN_CALLS + CALL_RESERVE


def estimate_tokens(body: dict) -> int:
    """The prompt's size in tokens at 3 bytes a token, as the client encodes it."""
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8", errors="replace")) // (
        BYTES_PER_TOKEN
    )


class GeminiPacer:
    """Starts attempts at least `60 / rpm` s apart and keeps the prompt tokens of the last
    60 s, plus the next estimate, at or under `tpm`. Callers queue on one lock, so every
    thread shares one schedule (as F1's `NcbiPacer`)."""

    def __init__(
        self,
        rpm: int,
        tpm: int,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.gap_seconds = WINDOW_SECONDS / rpm
        self.tpm = tpm
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._last_start: float | None = None
        self._window: deque[list] = deque()  # [start time, prompt tokens], oldest first

    def _start_time(self, now: float, estimated: int) -> float:
        """The earliest start at or after `now` within both limits."""
        t = now if self._last_start is None else max(now, self._last_start + self.gap_seconds)
        used = sum(n for _, n in self._window)
        for start, n in self._window:  # oldest first: each one ageing out frees its tokens
            if used + estimated <= self.tpm:
                break
            t = max(t, start + WINDOW_SECONDS)
            used -= n
        return t

    def wait(self, estimated_tokens: int, deadline: float) -> bool:
        """Sleeps until an attempt may start, then reserves it. Returns False, without
        sleeping or reserving, when the start would be at or after `deadline`. An estimate
        over `tpm` on its own goes once the window is empty."""
        with self._lock:
            now = self._clock()
            while self._window and self._window[0][0] <= now - WINDOW_SECONDS:
                self._window.popleft()
            start = self._start_time(now, estimated_tokens)
            if start >= deadline:
                return False
            if start > now:
                self._sleep(start - now)
            self._last_start = start
            self._window.append([start, estimated_tokens])
            return True

    def note(self, tokens: int) -> None:
        """Replaces the newest estimate with Google's `promptTokenCount`."""
        with self._lock:
            if self._window:
                self._window[-1][1] = max(0, int(tokens))


# ---------- the day's count (model_calls) ----------

_PACIFIC_DAY = "(now() AT TIME ZONE 'America/Los_Angeles')::date"

# The next Pacific midnight, shown as HH:MM in India time.
_RESET_SQL = (
    "SELECT to_char(((date_trunc('day', now() AT TIME ZONE 'America/Los_Angeles')"
    " + interval '1 day') AT TIME ZONE 'America/Los_Angeles') AT TIME ZONE 'Asia/Kolkata',"
    " 'HH24:MI')"
)


def record_attempt(
    pool: ConnectionPool, *, model: str, run_id: UUID | None, step: str, attempt: int
) -> int:
    """Inserts a `sent` row before the request goes out; returns its id."""
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO model_calls (model, run_id, step, attempt, status_kind)"
            " VALUES (%s, %s, %s, %s, 'sent') RETURNING id",
            (model, run_id, step, attempt),
        ).fetchone()
    return int(row[0])


def finish_attempt(
    pool: ConnectionPool,
    call_id: int,
    *,
    status_kind: str,
    usage: Usage | None,
    latency_ms: int,
) -> None:
    """`ok` or a GeminiError kind, Google's token counts when there was a reply, and the
    latency."""
    with pool.connection() as conn:
        conn.execute(
            "UPDATE model_calls SET status_kind = %s, prompt_tokens = %s, output_tokens = %s,"
            " thought_tokens = %s, latency_ms = %s WHERE id = %s",
            (
                status_kind,
                usage.prompt if usage else None,
                usage.output if usage else None,
                usage.thoughts if usage else None,
                latency_ms,
                call_id,
            ),
        )


def calls_today(pool: ConnectionPool, model: str) -> int:
    """Attempts on the current Pacific day for `model`, every status (429s included: whether
    Google counts refused requests is not known)."""
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT count(*) FROM model_calls WHERE model = %s AND quota_day = {_PACIFIC_DAY}",
            (model,),
        ).fetchone()
    return int(row[0])


def runs_left_for(rpd: int, used: int) -> int:
    """Whole runs the rest of the day allows when `used` attempts are counted. The next run
    needs room for its cap (RUN_ATTEMPTS); each one after it is counted at RUN_CALLS, a run
    without retries, so the number is "about". At least 1 exactly when
    `used + RUN_ATTEMPTS <= rpd`: a run admitted then cannot pass `rpd` even at its cap. Runs
    are admitted one at a time against the live count, so later runs are checked in turn. The
    same number as `(rpd - used - CALL_RESERVE) // RUN_CALLS` (23 on a fresh day of 500)."""
    room = rpd - used
    if room < RUN_ATTEMPTS:
        return 0
    return 1 + (room - RUN_ATTEMPTS) // RUN_CALLS


def runs_left(pool: ConnectionPool, model: str, rpd: int) -> int:
    """`runs_left_for` with today's count for `model`."""
    return runs_left_for(rpd, calls_today(pool, model))


def reset_time_ist(pool: ConnectionPool) -> str:
    """When the free day resets (the next Pacific midnight), in IST, as HH:MM."""
    with pool.connection() as conn:
        row = conn.execute(_RESET_SQL).fetchone()
    return str(row[0])
