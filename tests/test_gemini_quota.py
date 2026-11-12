import itertools
import re
import threading

import pytest
from lit_fakes import FakeClock

from pubmedgemini.gemini.client import Usage
from pubmedgemini.gemini.quota import (
    CALL_RESERVE,
    RUN_ATTEMPTS,
    RUN_CALLS,
    WRITER_CALLS,
    GeminiPacer,
    calls_today,
    estimate_tokens,
    finish_attempt,
    record_attempt,
    reset_time_ist,
    runs_left,
    runs_left_for,
)

# ---------- the pacer ----------


def test_pacer_spaces_starts_by_rpm():
    clock = FakeClock()
    p = GeminiPacer(10, 250_000, clock.now, clock.sleep)
    for _ in range(3):
        assert p.wait(100, deadline=clock.t + 600)
    assert clock.slept == pytest.approx([6.0, 6.0])


def test_pacer_does_not_wait_when_the_gap_has_passed():
    clock = FakeClock()
    p = GeminiPacer(10, 250_000, clock.now, clock.sleep)
    assert p.wait(100, clock.t + 600)
    clock.t += 7
    assert p.wait(100, clock.t + 600) and clock.slept == []


def test_pacer_holds_tokens_under_tpm():
    clock = FakeClock()
    p = GeminiPacer(1000, 10_000, clock.now, clock.sleep)
    assert p.wait(8_000, clock.t + 600) and p.wait(3_000, clock.t + 600)
    assert sum(clock.slept) == pytest.approx(60.0, abs=0.5)  # the first 8,000 must age out


def test_pacer_counts_only_the_last_60_seconds():
    clock = FakeClock()
    p = GeminiPacer(1000, 10_000, clock.now, clock.sleep)
    assert p.wait(4_000, clock.t + 600)
    clock.t += 30
    assert p.wait(4_000, clock.t + 600)  # 8,000 in the window: fits
    assert clock.slept == []
    assert p.wait(4_000, clock.t + 600)  # 12,000 would not: wait for the first to age out
    assert sum(clock.slept) == pytest.approx(30.0, abs=0.01)


def test_note_replaces_the_estimate_with_googles_count():
    clock = FakeClock()
    p = GeminiPacer(1000, 10_000, clock.now, clock.sleep)
    assert p.wait(9_000, clock.t + 600)
    p.note(1_000)
    clock.t += 1
    assert p.wait(8_000, clock.t + 600) and clock.slept == []


def test_an_estimate_over_tpm_waits_for_an_empty_window_then_goes():
    clock = FakeClock()
    p = GeminiPacer(1000, 10_000, clock.now, clock.sleep)
    assert p.wait(5_000, clock.t + 600)
    assert p.wait(50_000, clock.t + 600)
    assert sum(clock.slept) == pytest.approx(60.0, abs=0.01)


def test_pacer_refuses_a_wait_past_the_deadline():
    clock = FakeClock()
    p = GeminiPacer(10, 250_000, clock.now, clock.sleep)
    p.wait(1, clock.t + 600)
    assert p.wait(1, deadline=clock.t + 2) is False and clock.slept == []
    assert p.wait(1, deadline=clock.t + 600)  # a refused wait reserved nothing
    assert clock.slept == pytest.approx([6.0])


def test_pacer_refuses_a_token_wait_past_the_deadline():
    clock = FakeClock()
    p = GeminiPacer(1000, 10_000, clock.now, clock.sleep)
    p.wait(9_000, clock.t + 600)
    assert p.wait(2_000, deadline=clock.t + 30) is False and clock.slept == []


def test_pacer_is_shared_by_threads():
    """One lock: two threads never start inside the same gap."""
    import time

    starts: list[float] = []
    p = GeminiPacer(600, 250_000)  # 0.1 s apart, real clock

    def go():
        assert p.wait(10, time.monotonic() + 30)
        starts.append(time.monotonic())

    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    starts.sort()
    assert len(starts) == 3 and all(b - a >= 0.09 for a, b in itertools.pairwise(starts))


def test_estimate_tokens_is_three_bytes_a_token():
    body = {"x": "a" * 300}
    assert estimate_tokens(body) == len('{"x": "' + "a" * 300 + '"}') // 3
    assert estimate_tokens({"x": "é" * 3}) == len('{"x": "ééé"}'.encode()) // 3


# ---------- the day counter ----------


def add_calls(pool, model: str, n: int, days_ago: int = 0) -> None:
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO model_calls (model, called_at, step, attempt, status_kind) "
            "SELECT %s, now() - make_interval(days => %s), 'researcher', 1, 'ok' "
            "FROM generate_series(1, %s)",
            (model, days_ago, n),
        )


def test_run_calls_and_reserve():
    # F3 plan D1 option A (owner, 12 Nov 2026): the researcher's 11 and the writer's 10
    # (4 parts, 4 retries, the one-piece writer and its retry).
    assert (WRITER_CALLS, RUN_CALLS, CALL_RESERVE) == (10, 21, 3)


def test_a_run_may_send_its_calls_plus_the_reserve():  # owner decision, 11 Nov: cap per run
    assert RUN_ATTEMPTS == RUN_CALLS + CALL_RESERVE == 24


@pytest.mark.parametrize("rpd", [1, 13, 14, 15, 23, 24, 25, 44, 45, 46, 100, 500, 1000])
def test_a_run_admitted_with_one_left_cannot_pass_the_day_limit_at_its_cap(rpd):
    # Admission is `runs_left >= 1`; a run then sends at most RUN_ATTEMPTS attempts.
    for used in range(rpd + 30):
        if runs_left_for(rpd, used) >= 1:
            assert used + RUN_ATTEMPTS <= rpd, (rpd, used)
    if rpd >= RUN_ATTEMPTS:  # the last admitted run lands exactly on the limit
        assert runs_left_for(rpd, rpd - RUN_ATTEMPTS) == 1
        assert runs_left_for(rpd, rpd - RUN_ATTEMPTS + 1) == 0
    else:
        assert runs_left_for(rpd, 0) == 0


def test_runs_left_on_a_fresh_day():  # 23 under option A
    assert runs_left_for(500, 0) == 23 == (500 - 3) // 21
    assert [runs_left_for(500, u) for u in (455, 456, 476, 477, 500, 600)] == [2, 1, 1, 0, 0, 0]


def test_runs_left_under_option_a(migrated_pool):
    assert runs_left(migrated_pool, "m", 500) == 23
    add_calls(migrated_pool, "m", 476)
    assert runs_left(migrated_pool, "m", 500) == 1
    add_calls(migrated_pool, "m", 1)
    assert runs_left(migrated_pool, "m", 500) == 0


def test_runs_left_arithmetic(migrated_pool):
    assert runs_left(migrated_pool, "m", 500) == 23  # (500 - 3) // 21
    add_calls(migrated_pool, "m", 476)
    assert runs_left(migrated_pool, "m", 500) == 1
    add_calls(migrated_pool, "m", 1)
    assert runs_left(migrated_pool, "m", 500) == 0
    add_calls(migrated_pool, "other-model", 50)
    assert runs_left(migrated_pool, "m", 500) == 0  # per model
    assert runs_left(migrated_pool, "other-model", 500) == 21
    add_calls(migrated_pool, "m", 100)
    assert runs_left(migrated_pool, "m", 500) == 0  # never negative


def test_yesterdays_calls_do_not_count(migrated_pool):
    add_calls(migrated_pool, "m", 30, days_ago=1)
    assert calls_today(migrated_pool, "m") == 0
    add_calls(migrated_pool, "m", 2)
    assert calls_today(migrated_pool, "m") == 2


def test_record_then_finish_an_attempt(migrated_pool):
    call_id = record_attempt(migrated_pool, model="m", run_id=None, step="study_list", attempt=2)
    assert calls_today(migrated_pool, "m") == 1  # counted while still `sent`
    with migrated_pool.connection() as conn:
        row = conn.execute("SELECT status_kind, prompt_tokens FROM model_calls WHERE id = %s",
                           (call_id,)).fetchone()
    assert row == ("sent", None)
    finish_attempt(migrated_pool, call_id, status_kind="ok", usage=Usage(1200, 80, 3),
                   latency_ms=850)
    finish_attempt(migrated_pool, record_attempt(migrated_pool, model="m", run_id=None,
                                                 step="researcher", attempt=1),
                   status_kind="rate_limited", usage=None, latency_ms=40)
    with migrated_pool.connection() as conn:
        rows = conn.execute(
            "SELECT step, attempt, status_kind, prompt_tokens, output_tokens, thought_tokens,"
            " latency_ms, quota_day = (now() AT TIME ZONE 'America/Los_Angeles')::date"
            " FROM model_calls ORDER BY id").fetchall()
    assert rows == [("study_list", 2, "ok", 1200, 80, 3, 850, True),
                    ("researcher", 1, "rate_limited", None, None, None, 40, True)]
    assert calls_today(migrated_pool, "m") == 2  # every status counts


def test_reset_time_is_ist_hh_mm(migrated_pool):
    assert re.fullmatch(r"\d\d:\d\d", reset_time_ist(migrated_pool))
    assert reset_time_ist(migrated_pool) in ("13:30", "12:30")  # Pacific standard or daylight


def test_reset_time_is_the_next_pacific_midnight_in_ist(migrated_pool):
    from pubmedgemini.gemini.quota import _RESET_SQL

    with migrated_pool.connection() as conn:
        winter = conn.execute(_RESET_SQL.replace("now()", "timestamptz '2026-11-11 07:59+00'"))
        summer = conn.execute(_RESET_SQL.replace("now()", "timestamptz '2026-07-01 12:00+00'"))
        assert (winter.fetchone()[0], summer.fetchone()[0]) == ("13:30", "12:30")
