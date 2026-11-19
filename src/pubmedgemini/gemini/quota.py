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

# The most calls one run makes without transport retries (owner decision, 11 Nov 2026, D2):
# researcher 8 + study list 1 + its JSON nudge 1 + S3 retry 1 + writer parts 4 + part retries 4
# + one-piece fallback 1 + its retry 1 + Introduction 1 (F3c) + number completion 1 (F4) = 23.
# F3a made at most 21 of them; with the Introduction (F3c, one attempt) at most 22.
RUN_CALLS = 23
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
    pool: ConnectionPool, *, model: str, run_id: UUID | None, step: str, attempt: int,
    user_id: int | None = None,
) -> int:
    """Inserts a `sent` row before the request goes out; returns its id. F7: `user_id` is the
    doctor whose key the attempt uses (each key has its own free day)."""
    with pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO model_calls (model, run_id, step, attempt, status_kind, user_id)"
            " VALUES (%s, %s, %s, %s, 'sent', %s) RETURNING id",
            (model, run_id, step, attempt, user_id),
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


def calls_today(pool: ConnectionPool, model: str, user_id: int | None = None) -> int:
    """Attempts on the current Pacific day for `model`, every status (429s included: whether
    Google counts refused requests is not known). F7: with `user_id`, that doctor's attempts
    only (their own key's day); without it, every attempt (the whole app's)."""
    where = "" if user_id is None else " AND user_id = %s"
    params = (model,) if user_id is None else (model, user_id)
    with pool.connection() as conn:
        row = conn.execute(
            f"SELECT count(*) FROM model_calls WHERE model = %s AND quota_day = {_PACIFIC_DAY}"
            + where,
            params,
        ).fetchone()
    return int(row[0])


def runs_left_for(rpd: int, used: int) -> int:
    """Whole runs the rest of the day allows when `used` attempts are counted. The next run
    needs room for its cap (RUN_ATTEMPTS); each one after it is counted at RUN_CALLS, a run
    without retries, so the number is "about". At least 1 exactly when
    `used + RUN_ATTEMPTS <= rpd`: a run admitted then cannot pass `rpd` even at its cap. Runs
    are admitted one at a time against the live count, so later runs are checked in turn. The
    same number as `(rpd - used - CALL_RESERVE) // RUN_CALLS` (21 on a fresh day of 500, as the
    owner decided: worst-case counting)"""
    room = rpd - used
    if room < RUN_ATTEMPTS:
        return 0
    return 1 + (room - RUN_ATTEMPTS) // RUN_CALLS


def runs_left(pool: ConnectionPool, model: str, rpd: int, user_id: int | None = None) -> int:
    """`runs_left_for` with today's count for `model` (F7: on `user_id`'s own key)."""
    return runs_left_for(rpd, calls_today(pool, model, user_id))


class PacerBook:
    """F7: one GeminiPacer per doctor, since each key has its own per-minute limits. A pacer
    not used for `idle_seconds` is dropped, so the book does not grow with every doctor."""

    def __init__(
        self,
        rpm: int,
        tpm: int,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = 600.0,
    ) -> None:
        self._rpm, self._tpm, self._sleep, self._clock = rpm, tpm, sleep, clock
        self._idle = idle_seconds
        self._lock = threading.Lock()
        self._book: dict[int, tuple[GeminiPacer, float]] = {}

    def get(self, user_id: int) -> GeminiPacer:
        with self._lock:
            now = self._clock()
            for uid in [u for u, (_, t) in self._book.items() if now - t > self._idle]:
                del self._book[uid]
            pacer = self._book.get(user_id, (None, 0.0))[0]
            if pacer is None:
                pacer = GeminiPacer(self._rpm, self._tpm, clock=self._clock, sleep=self._sleep)
            self._book[user_id] = (pacer, now)
            return pacer

    def __len__(self) -> int:
        with self._lock:
            return len(self._book)


def reset_time_ist(pool: ConnectionPool) -> str:
    """When the free day resets (the next Pacific midnight), in IST, as HH:MM."""
    with pool.connection() as conn:
        row = conn.execute(_RESET_SQL).fetchone()
    return str(row[0])
