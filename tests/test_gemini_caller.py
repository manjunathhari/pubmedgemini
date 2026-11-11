import logging
import re
import uuid

import httpx
import pytest
from gemini_fakes import FAKE_KEY, QUOTA_FAILURE, RETRY_INFO, FakeGemini, error, reply
from lit_fakes import FakeClock
from pydantic import SecretStr

from pubmedgemini.gemini.caller import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_MAX_SECONDS,
    MAX_ATTEMPTS,
    DeadlineReached,
    ModelCaller,
)
from pubmedgemini.gemini.client import GeminiClient, GeminiError
from pubmedgemini.gemini.quota import GeminiPacer

MODEL = "gemini-3.5-flash-lite"
RUN = None  # model_calls.run_id is a foreign key; these tests have no run row


@pytest.fixture
def caller_with(migrated_pool):
    """`caller_with(script, rpm=1000, jitter=0.5)` -> (caller, fake, clock, waits)."""

    def make(script, rpm=1000, jitter=0.5):
        clock = FakeClock()
        g = FakeGemini(script)
        client = GeminiClient(SecretStr(FAKE_KEY), MODEL, g.transport, clock=clock.now)
        pacer = GeminiPacer(rpm, 250_000, clock.now, clock.sleep)
        c = ModelCaller(client, pacer, migrated_pool, MODEL, sleep=clock.sleep, clock=clock.now,
                        jitter=lambda: jitter)
        return c, g, clock, []

    return make


def rows(pool):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT step, attempt, status_kind, prompt_tokens, output_tokens FROM model_calls "
            "ORDER BY id").fetchall()


def test_constants_match_incisive():  # retry.rs RetryPolicy::default
    assert (MAX_ATTEMPTS, BACKOFF_BASE_SECONDS, BACKOFF_MAX_SECONDS) == (3, 0.5, 8.0)


def test_ok_reply_is_returned_and_counted(caller_with, migrated_pool):
    c, g, clock, waits = caller_with([reply([{"text": "x"}], prompt=1200, out=80)])
    r = c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 200, on_wait=waits.append)
    assert r.text == "x" and len(g.sent) == 1 and waits == []
    assert rows(migrated_pool) == [("researcher", 1, "ok", 1200, 80)]


def test_429_waits_the_asked_delay_within_the_deadline(caller_with):  # Review Focus 3
    c, g, clock, waits = caller_with(
        [error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("12s")]), reply([{"text": "x"}])])
    c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 200, on_wait=waits.append)
    assert (len(g.sent), waits, 12.0 in clock.slept) == (2, [12], True)


def test_asked_delay_past_the_deadline_stops_at_once(caller_with):
    c, g, clock, waits = caller_with([error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("41s")])])
    with pytest.raises(DeadlineReached):
        c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 30, on_wait=waits.append)
    assert len(g.sent) == 1 and 41.0 not in clock.slept and waits == []


def test_a_wait_ending_exactly_at_the_deadline_stops(caller_with):
    c, g, clock, _ = caller_with([error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("30s")])])
    with pytest.raises(DeadlineReached):
        c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 30, on_wait=print)
    assert len(g.sent) == 1


def test_deadline_reached_is_not_a_gemini_error():
    assert not issubclass(DeadlineReached, GeminiError)


def test_pacer_past_the_deadline_sends_nothing(caller_with, migrated_pool):
    c, g, clock, _ = caller_with([reply([{"text": "x"}]), reply([{"text": "y"}])], rpm=10)
    c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 600, on_wait=print)
    with pytest.raises(DeadlineReached):
        c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 2, on_wait=print)
    assert len(g.sent) == 1 and len(rows(migrated_pool)) == 1


def test_three_attempts_then_the_error(caller_with, migrated_pool):
    c, g, clock, waits = caller_with([error(503, "UNAVAILABLE")] * 3, jitter=0.0)
    with pytest.raises(GeminiError) as e:
        c.call({}, step="study_list", run_id=RUN, deadline=clock.t + 200, on_wait=waits.append)
    assert (e.value.kind, len(g.sent)) == ("overloaded", 3)
    assert clock.slept == pytest.approx([0.4, 0.8]) and waits == []  # on_wait is for 429 only
    assert [r[1:3] for r in rows(migrated_pool)] == [(1, "overloaded"), (2, "overloaded"),
                                                    (3, "overloaded")]


@pytest.mark.parametrize("jitter,expected", [(0.0, [0.4, 0.8]), (0.5, [0.5, 1.0]),
                                             (0.999999, [0.6, 1.2])])
def test_backoff_is_jittered_0_8_to_1_2(caller_with, jitter, expected):
    c, _g, clock, _ = caller_with([error(500, "X")] * 3, jitter=jitter)
    with pytest.raises(GeminiError):
        c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 200, on_wait=print)
    assert clock.slept == pytest.approx(expected, abs=1e-4)


def test_timeouts_and_network_errors_are_retried(caller_with):
    def timeout(request):
        raise httpx.ReadTimeout("t", request=request)

    def down(request):
        raise httpx.ConnectError("c", request=request)

    c, g, clock, _ = caller_with([timeout, down, reply([{"text": "x"}])])
    assert c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 200,
                  on_wait=print).text == "x"
    assert len(g.sent) == 3


def test_a_short_asked_wait_writes_no_line(caller_with):
    c, _g, clock, waits = caller_with(
        [error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("0.5s")]), reply([{"text": "x"}])])
    c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 200, on_wait=waits.append)
    assert waits == [] and 0.5 in clock.slept


def test_429_without_a_delay_backs_off(caller_with):
    c, _g, clock, waits = caller_with([error(429, "RESOURCE_EXHAUSTED"), reply([{"text": "x"}])])
    c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 200, on_wait=waits.append)
    assert clock.slept == pytest.approx([0.5]) and waits == []


def test_daily_quota_is_never_retried(caller_with):
    c, g, _clock, _ = caller_with(
        [error(429, "RESOURCE_EXHAUSTED",
               [QUOTA_FAILURE("GenerateRequestsPerDayPerProjectPerModel-FreeTier")])])
    with pytest.raises(GeminiError) as e:
        c.call({}, step="researcher", run_id=RUN, deadline=10**9, on_wait=print)
    assert (e.value.kind, len(g.sent)) == ("daily_limit", 1)


@pytest.mark.parametrize("code", [402, 401, 403, 400, 404, 418])
def test_refusals_are_not_retried(caller_with, code):
    c, g, clock, _ = caller_with([error(code, "X")])
    with pytest.raises(GeminiError):
        c.call({}, step="researcher", run_id=RUN, deadline=10**9, on_wait=print)
    assert len(g.sent) == 1 and clock.slept == []


@pytest.mark.parametrize("response", [httpx.Response(200, content=b"<html>"),
                                      httpx.Response(200, content=b" " * (8 * 1024 * 1024 + 1)),
                                      httpx.Response(302, headers={"location": "https://x.example/"})])
def test_parse_too_large_and_http_are_not_retried(caller_with, response):
    c, g, _clock, _ = caller_with([response])
    with pytest.raises(GeminiError):
        c.call({}, step="researcher", run_id=RUN, deadline=10**9, on_wait=print)
    assert len(g.sent) == 1


def test_every_attempt_is_counted_with_its_kind(caller_with, migrated_pool):
    c, _g, clock, _ = caller_with(
        [error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("2s")]), reply([{"text": "x"}], prompt=900)])
    c.call({}, step="study_list_nudge", run_id=RUN, deadline=clock.t + 200, on_wait=print)
    assert rows(migrated_pool) == [("study_list_nudge", 1, "rate_limited", None, None),
                                   ("study_list_nudge", 2, "ok", 900, 100)]
    with migrated_pool.connection() as conn:
        days = conn.execute("SELECT count(*) FROM model_calls WHERE quota_day IS NOT NULL")
        assert days.fetchone()[0] == 2


def test_the_run_id_is_stored(caller_with, migrated_pool):
    with migrated_pool.connection() as conn:
        uid = conn.execute("INSERT INTO users (email, role) VALUES ('o@example.com', 'owner') "
                           "RETURNING id").fetchone()[0]
        run_id = uuid.uuid4()
        conn.execute("INSERT INTO runs (id, user_id, question, model) VALUES (%s, %s, 'q', 'm')",
                     (run_id, uid))
    c, _g, clock, _ = caller_with([reply([{"text": "x"}])])
    c.call({}, step="researcher", run_id=run_id, deadline=clock.t + 200, on_wait=print)
    with migrated_pool.connection() as conn:
        assert conn.execute("SELECT run_id FROM model_calls").fetchone()[0] == run_id


def test_the_attempt_timeout_is_cut_to_the_deadline(caller_with):
    seen = []

    def capture(request):
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=reply([{"text": "x"}]))

    c, _g, clock, _ = caller_with([capture, capture])
    c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 45, on_wait=print)
    c.call({}, step="researcher", run_id=RUN, deadline=clock.t + 4000, on_wait=print)
    assert seen[0] == pytest.approx(45, abs=0.1) and seen[1] == 120.0


def test_googles_token_count_replaces_the_estimate(caller_with):
    c, _g, clock, _ = caller_with([reply([{"text": "x"}], prompt=7)])
    body = {"x": "a" * 3000}
    c.call(body, step="researcher", run_id=RUN, deadline=clock.t + 200, on_wait=print)
    assert c._pacer._window[-1][1] == 7


def test_log_lines_hold_counts_and_kinds_only(caller_with, caplog):  # Review Focus 4
    caplog.set_level(logging.DEBUG)
    c, _g, clock, _ = caller_with(
        [error(503, "UNAVAILABLE", message="gm-q7 google says"),
         reply([{"text": "yq8 my reasoning"}], prompt=1200, out=80)])
    c.call({"contents": [{"role": "user", "parts": [{"text": "zq7 question"}]}]},
           step="researcher", run_id=RUN, deadline=clock.t + 200, on_wait=print)
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("pubmedgemini")]
    assert len(msgs) == 2
    assert re.fullmatch(r"gemini step=researcher attempt=1 status=overloaded finish=none "
                        r"prompt_tokens=0 output_tokens=0 thought_tokens=0 latency_ms=\d+", msgs[0])
    assert re.fullmatch(r"gemini step=researcher attempt=2 status=ok finish=stop "
                        r"prompt_tokens=1200 output_tokens=80 thought_tokens=0 latency_ms=\d+",
                        msgs[1])
    for leak in ("zq7", "yq8", "gm-q7", FAKE_KEY, "generativelanguage", "https://"):
        assert leak not in caplog.text, leak
