"""Task 6: the writer run in four parts on threads, with one retry per part, the merge, the
one-piece fallback and the part events (Incisive `agent/writer_v3.rs` `section_reply`
2075-2123, `write_quick_parallel_with` 2145-2310, `write_counted` 1614-1670; owner's answers Q2
and Q3).

Gemini is `RoutedFake` (each part's replies in its own queue, served under a lock); the clock
is a `LockedClock` moved only by the pacer's and the caller's injected sleeps or by a test. No
test sleeps or reaches the network; barriers and events only make threads meet, with a guard
timeout that is never reached when the code is right."""

import contextlib
import json
import logging
import threading
import traceback

import httpcore
import httpx
import pytest
from gemini_fakes import FAKE_KEY, QUOTA_FAILURE, RETRY_INFO, error, reply
from psycopg_pool import ConnectionPool
from pydantic import SecretStr
from writer_fakes import ONE_PIECE, LockedClock, RoutedFake

from pubmedgemini.gemini.caller import ModelCaller, RunCapReached
from pubmedgemini.gemini.client import GeminiClient, to_openapi
from pubmedgemini.gemini.quota import GeminiPacer
from pubmedgemini.researcher.loop import FatalModelError, HardStop, Timing
from pubmedgemini.researcher.prompts import SCHEMA_NUDGE, with_source_rule
from pubmedgemini.writer.parts import (
    ONE_PIECE_LINE,
    WriterFailed,
    write_one_piece,
    write_parts,
)
from pubmedgemini.writer.prompts import (
    SYSTEM,
    WRITER_MAX_TOKENS,
    Section,
    StudyInput,
    section_schema,
    section_user,
    writer_schema,
    writer_user,
)
from pubmedgemini.writer.read import as_whole, into_answer

MODEL = "gemini-3.5-flash-lite"
GUARD = 10  # seconds a barrier or an event may wait before the test fails (never reached)

# Private text: none of it may reach a log line or an exception message.
Q_SECRET = "QSECRETxanthelasma"
A_SECRET = "ASECRETpapillotomy"
M_SECRET = "MSECRETfundoplication"
SECRETS = (Q_SECRET, A_SECRET, M_SECRET)

T, O, C, S = Section.TRIALS, Section.OUTCOMES, Section.CONTEXT, Section.SUMMARY

STUDY = StudyInput(pmid="11111111", title="A trial", journal="J Surg", year=2024,
                   pub_types=("Randomized Controlled Trial",), doi=None, retracted=False,
                   abstract=f"Sixty patients. {A_SECRET} RR 0.76.", design="RCT",
                   why="The trial.")
USER = writer_user(f"Is laparoscopic repair better? {Q_SECRET}", [STUDY], [], [])


def whole(tag: str) -> dict:
    """A whole single-writer reply; every text carries `tag` and the model-text marker."""
    return {
        "bottom_line": {"text": f"{tag} no benefit {M_SECRET} [11111111].",
                        "certainty": "Moderate", "certainty_reason": f"{tag} two RCTs"},
        "outcomes": [{
            "outcome": f"{tag} chronic pain", "rct_evidence": "1 RCT [11111111]",
            "effect_text": f"{tag} RR 0.76 [11111111]",
            "numbers": [{"label": "RR", "value": 0.76, "unit": None, "arm": None,
                         "ci_low": 0.49, "ci_high": 1.1, "p": None, "pmid": "11111111"}],
            "certainty": "Low", "why": f"{tag} small", "pmids": ["11111111"],
        }],
        "studies": [{"pmid": "11111111", "label": f"{tag} A 2024", "design": "RCT", "n": 60,
                     "setting": "s", "comparison": "c", "key_results": "k [11111111]",
                     "appraisal": "a"}],
        "conflicts": [],
        "applicability": {"case_mix": f"{tag} simple", "india_cost_context": "not known",
                          "training": "none"},
        "would_change": [f"{tag} a larger RCT"],
        "at_a_glance": f"{tag} no benefit.\nChronic pain: RR 0.76 [11111111]\nMain caveat: small",
        "clinical_practice": f"Default choice: {tag} lap\nCost per case: not known",
        "notable_findings": f"- {tag} withdrawals 2 vs 9 [11111111].",
    }


PARTS = whole("parts")
PIECE = whole("piece")


def part_of(w: dict, s: Section) -> dict:
    return {f: w[f] for f in s.fields}


def ok(s: Section, w: dict = PARTS) -> dict:
    return reply([{"text": json.dumps(part_of(w, s))}], out=321)


def piece(w: dict = PIECE) -> dict:
    return reply([{"text": json.dumps(w)}], out=654)


UNREADABLE = reply([{"text": f"Sorry, {M_SECRET} is not JSON"}])
CUT = reply([{"text": '{"outcomes": [{"outcome": "' + M_SECRET}], finish="MAX_TOKENS")
BLOCKED = {"promptFeedback": {"blockReason": "SAFETY"}}
SAFETY = reply([{"text": json.dumps(part_of(PARTS, S))}], finish="SAFETY")
DAILY = error(429, "RESOURCE_EXHAUSTED",
              [QUOTA_FAILURE("GenerateRequestsPerDayPerProjectPerModel-FreeTier")],
              message=f"quota {M_SECRET}")
REFUSED_400 = error(400, "INVALID_ARGUMENT", message=f"bad {M_SECRET}")


def all_ok() -> dict:
    return {s.name: [ok(s)] for s in Section.ALL}


def merged_answer(w: dict = PARTS):
    return into_answer(as_whole(w))


class Lines:
    def __init__(self) -> None:
        self.got: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def line(self, kind: str, text: str) -> None:
        with self._lock:
            self.got.append((kind, text))


class Harness:
    def __init__(self, pool, parts, one_piece=(), left=200.0, rpm=1000, run_cap=None,
                 transport=None):
        self.pool = pool
        self.clock = LockedClock()
        self.fake = RoutedFake(parts, one_piece)
        client = GeminiClient(SecretStr(FAKE_KEY), MODEL, transport or self.fake.transport,
                              clock=self.clock.now)
        self.pacer = GeminiPacer(rpm, 250_000, self.clock.now, self.clock.sleep)
        self.caller = ModelCaller(client, self.pacer, pool, MODEL, sleep=self.clock.sleep,
                                  clock=self.clock.now, jitter=lambda: 0.5)
        if run_cap is not None:
            self.caller.run_cap = run_cap
        t = self.clock.t
        self.timing = Timing(started=t, wrap_up_at=t, hard_stop_at=t + left)
        self.lines = Lines()
        self.waits: list[int] = []
        self.events: list[tuple] = []

    def on_part(self, ev):
        self.events.append(ev)

    def run(self, on_part="record"):
        cb = self.on_part if on_part == "record" else on_part
        return write_parts(USER, caller=self.caller, run_id=None, timing=self.timing,
                           on_wait=self.waits.append, progress=self.lines, on_part=cb,
                           clock=self.clock.now)

    def steps(self) -> list[str]:
        with self.pool.connection() as conn:
            return [r[0] for r in conn.execute("SELECT step FROM model_calls ORDER BY id")]


@pytest.fixture
def harness(migrated_pool):
    def make(parts, **kw):
        return Harness(migrated_pool, parts, **kw)

    return make


@pytest.fixture(autouse=True)
def no_thread_left_and_no_thread_traceback(monkeypatch):
    """Every test: no writer thread outlives write_parts, and no thread dies with an
    unhandled exception (whose traceback threading would print)."""
    died: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", died.append)
    yield
    assert [t.name for t in threading.enumerate() if t.name.startswith("writer")] == []
    assert died == []


def leaks(text: str) -> list[str]:
    return [s for s in SECRETS if s in text]


def assert_private(exc: BaseException | None, caplog) -> None:
    assert leaks(caplog.text) == []
    for r in caplog.records:
        assert leaks(r.getMessage()) == []
    while exc is not None:
        assert leaks(str(exc)) == [] and leaks(repr(exc)) == []
        assert leaks("".join(traceback.format_exception(exc))) == []
        exc = exc.__cause__ or exc.__context__


# ---------- all four parts ----------


def test_four_parts_succeed_and_merge(harness, caplog):
    caplog.set_level(logging.DEBUG)
    h = harness(all_ok())
    answer, defaulted, merged = h.run()
    assert (answer, defaulted) == merged_answer()
    assert merged is True
    assert h.fake.count() == 4
    assert sorted(h.steps()) == sorted(f"writer_{s.name}" for s in Section.ALL)
    assert h.lines.got == []  # no one-piece line
    for s in Section.ALL:
        [body] = h.fake.bodies(s.name)
        assert body["systemInstruction"]["parts"][0]["text"] == with_source_rule(SYSTEM)
        assert body["contents"] == [{"role": "user",
                                     "parts": [{"text": section_user(USER, s)}]}]
        gc = body["generationConfig"]
        assert gc["responseSchema"] == to_openapi(section_schema(s))
        assert gc["maxOutputTokens"] == s.max_tokens(WRITER_MAX_TOKENS)
        assert gc["responseMimeType"] == "application/json"
    caps = {s.name: h.fake.bodies(s.name)[0]["generationConfig"]["maxOutputTokens"]
            for s in Section.ALL}
    assert caps == {"trials": 10_000, "outcomes": 12_000, "context": 5_000, "summary": 5_000}
    assert "writer kind=merged" in caplog.text
    assert_private(None, caplog)


def test_parts_start_through_the_pacer(harness):
    h = harness(all_ok(), rpm=10)
    t0 = h.clock.t
    h.run()
    starts = sorted(start - t0 for start, _ in h.pacer._window)
    assert starts == [0, 6, 12, 18]  # one schedule for the four threads, 60/rpm apart


def test_four_parts_are_in_flight_at_once_and_hold_no_database_connection(harness,
                                                                         monkeypatch):
    held = [0]
    taken: list[int] = []
    count_lock = threading.Lock()
    h = None

    def all_in():  # runs once, in one thread, while all four wait inside the call
        with count_lock:
            stats.append(held[0])

    stats: list[int] = []
    barrier = threading.Barrier(4, action=all_in, timeout=GUARD)

    def meet(s):
        def step(_request):
            barrier.wait()
            return httpx.Response(200, json=ok(s))
        return step

    h = harness({s.name: [meet(s)] for s in Section.ALL})
    real = h.pool.connection

    @contextlib.contextmanager
    def counted(*a, **kw):  # connections taken by the writer's code and not yet given back
        with real(*a, **kw) as conn:
            with count_lock:
                held[0] += 1
                taken.append(1)
            try:
                yield conn
            finally:
                with count_lock:
                    held[0] -= 1

    monkeypatch.setattr(h.pool, "connection", counted)
    assert h.run()[2] is True
    assert stats == [0], "no part holds a pool connection while its call is in flight"
    assert held == [0]
    assert len(taken) == 8  # a `sent` row and its finish for each part: the count is real


def test_one_pool_connection_is_enough_for_four_parts(migrated_pool, db_url):
    """Four parts in flight at once on a pool of one connection: each database write takes
    the connection briefly and gives it back before the call (a part holding it across its
    call would leave the others waiting and the barrier would break)."""
    barrier = threading.Barrier(4, timeout=GUARD)

    def meet(s):
        def step(_request):
            barrier.wait()
            return httpx.Response(200, json=ok(s))
        return step

    one = ConnectionPool(db_url, min_size=1, max_size=1, timeout=5, open=False)
    one.open(wait=True, timeout=GUARD)
    try:
        h = Harness(one, {s.name: [meet(s)] for s in Section.ALL})
        assert h.run()[2] is True
        assert len(h.steps()) == 4
    finally:
        one.close()


class _EchoStream(httpcore.NetworkStream):
    """One connection to a fake server: the reply is chosen from the request this stream
    received, so a crossed connection would give a part another part's reply."""

    def __init__(self, barrier: threading.Barrier) -> None:
        self._barrier = barrier
        self._sent = bytearray()
        self._out: list[bytes] | None = None

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._sent += buffer

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        if self._out is None:
            self._barrier.wait()  # the four connections are open at once
            text = bytes(self._sent).decode()
            name = next(s for s in Section.ALL if f"TO WRITE: {s.name.upper()}" in text)
            body = json.dumps(ok(name)).encode()
            head = (f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
            self._out = [head + body]
        return self._out.pop(0) if self._out else b""

    def close(self) -> None:
        pass

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self

    def get_extra_info(self, info):
        return None


class _EchoBackend(httpcore.NetworkBackend):
    def __init__(self) -> None:
        self.barrier = threading.Barrier(4, timeout=GUARD)
        self.connects = 0
        self._lock = threading.Lock()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        assert host == "generativelanguage.googleapis.com" and port == 443
        with self._lock:
            self.connects += 1
        return _EchoStream(self.barrier)


def test_one_httpx_client_serves_four_threads_through_the_real_pool(harness):
    """httpx documents `Client` as shareable between threads (`httpx/_client.py`, class
    docstring); its connection pool changes state under a thread lock
    (`httpcore/_sync/connection_pool.py`, `_optional_thread_lock`). Here the production
    transport (httpx.HTTPTransport and httpcore's pool) runs over a fake network backend:
    four connections open at once and each part gets its own reply."""
    backend = _EchoBackend()
    transport = httpx.HTTPTransport()
    transport._pool = httpcore.ConnectionPool(network_backend=backend)
    h = harness({}, transport=transport)
    answer, _, merged = h.run()
    assert merged is True and (answer, _) == merged_answer()
    assert backend.connects == 4


# ---------- one part's retry ----------


@pytest.mark.parametrize("first", [CUT, UNREADABLE, BLOCKED, SAFETY],
                         ids=["cut_off", "unreadable", "blocked", "refused_finish"])
def test_cut_off_part_gets_one_nudge_retry_only_for_that_part(harness, first, caplog):
    caplog.set_level(logging.DEBUG)
    parts = all_ok()
    parts["outcomes"] = [first, ok(O)]
    h = harness(parts)
    answer, _, merged = h.run()
    assert merged is True and answer == merged_answer()[0]
    assert h.fake.count() == 5
    a, b = h.fake.bodies("outcomes")
    user_b = b["contents"][0]["parts"][0]["text"]
    assert user_b == section_user(USER, O) + SCHEMA_NUDGE
    assert user_b.endswith(SCHEMA_NUDGE)
    # D7: the same body, the nudge line added; same schema and cap.
    assert b["generationConfig"] == a["generationConfig"]
    assert b["systemInstruction"] == a["systemInstruction"]
    for s in (T, C, S):
        assert h.fake.count(s.name) == 1
    assert h.steps().count("writer_outcomes_retry") == 1
    assert len(h.steps()) == 5
    assert h.lines.got == []
    assert_private(None, caplog)


def test_a_part_read_on_retry_is_reported(harness):
    parts = all_ok()
    parts["summary"] = [UNREADABLE, ok(S)]
    h = harness(parts)
    h.run()
    assert [e[0] for e in h.events if e[0] == "summary"] == ["summary"]
    assert ("withdraw", None) not in h.events


# ---------- a part fails for good: the one piece ----------


def test_one_part_fails_twice_all_four_discarded_and_one_piece_written(harness, caplog):
    caplog.set_level(logging.DEBUG)
    parts = all_ok()
    parts["trials"] = [UNREADABLE, UNREADABLE]
    h = harness(parts, one_piece=[piece()])
    answer, defaulted, merged = h.run()
    assert merged is False
    assert (answer, defaulted) == merged_answer(PIECE)
    dumped = answer.model_dump_json()
    assert "parts" not in dumped and "piece" in dumped  # nothing from the four parts
    line = "Writer: section trials failed; writing the answer in one piece"
    assert h.lines.got == [("write", line)]
    [body] = h.fake.bodies(ONE_PIECE)
    assert body["systemInstruction"]["parts"][0]["text"] == with_source_rule(SYSTEM)
    assert body["contents"][0]["parts"][0]["text"] == USER
    assert body["generationConfig"]["responseSchema"] == to_openapi(writer_schema())
    assert body["generationConfig"]["maxOutputTokens"] == 20_000
    assert h.fake.count() == 6  # 4 parts + the trials retry + the one piece
    assert h.steps().count("writer_one_piece") == 1
    assert h.events[-1] == ("withdraw", None)
    assert "writer kind=parts_failed" in caplog.text
    assert_private(None, caplog)


def test_one_piece_line_is_incisives():
    assert ONE_PIECE_LINE.format(name="outcomes") == (
        "Writer: section outcomes failed; writing the answer in one piece")  # run_tracker.rs


def test_one_piece_gets_one_nudge_retry(harness):
    parts = all_ok()
    parts["context"] = [CUT, CUT]
    h = harness(parts, one_piece=[UNREADABLE, piece()])
    answer, _, merged = h.run()
    assert merged is False and answer == merged_answer(PIECE)[0]
    a, b = h.fake.bodies(ONE_PIECE)
    assert b["contents"][0]["parts"][0]["text"] == USER + SCHEMA_NUDGE
    assert b["generationConfig"] == a["generationConfig"]
    assert h.steps().count("writer_one_piece_retry") == 1
    assert h.fake.count() == 7


def test_a_part_whose_call_fails_falls_back_to_one_piece(harness):
    """A non-fatal error after the caller's own attempts fails that part (Incisive
    `StepError::Llm`), with no nudge retry: the one piece is written."""
    parts = all_ok()
    parts["outcomes"] = [REFUSED_400]
    h = harness(parts, one_piece=[piece()])
    answer, _, merged = h.run()
    assert merged is False and answer == merged_answer(PIECE)[0]
    assert h.fake.count("outcomes") == 1
    assert h.fake.count() == 5  # 4 parts + the one piece
    assert h.lines.got == [("write", ONE_PIECE_LINE.format(name="outcomes"))]


def test_two_parts_fail_the_line_names_the_first_in_order(harness):
    parts = all_ok()
    parts["summary"] = [UNREADABLE, UNREADABLE]
    parts["outcomes"] = [CUT, CUT]
    h = harness(parts, one_piece=[piece()])
    h.run()
    assert h.lines.got == [("write", ONE_PIECE_LINE.format(name="outcomes"))]


class Arrivals:
    """Hooks in the fake's queues: `step(s, body)` serves `body` and marks part `s` as in (its
    request has passed the pacer, so it will not move the shared clock again); `wait(*parts)`
    returns once those parts are in. A test that sets the clock waits for every sibling first,
    so no sibling's pacer sleep can land after the set."""

    def __init__(self) -> None:
        self._in = {x.name: threading.Event() for x in Section.ALL}

    def step(self, s: Section, body):
        def serve(_request):
            self._in[s.name].set()
            return body if isinstance(body, httpx.Response) else httpx.Response(200, json=body)
        return serve

    def wait(self, *parts: Section) -> None:
        for x in parts:
            assert self._in[x.name].wait(GUARD), x.name


def _at_decision(h, left, arrivals, siblings, body=UNREADABLE):
    """A step that, once `siblings` are in, leaves `left` seconds to the hard stop."""
    def step(_request):
        arrivals.wait(*siblings)
        h.clock.set(h.timing.hard_stop_at - left)
        return httpx.Response(200, json=body) if isinstance(body, dict) else body
    return step


# The clock jumps inside a reply by less than the attempt's own limit (120 s, or the time left),
# so the client does not take the jump for a timed-out attempt.
@pytest.mark.parametrize("left,start", [(149.0, 200.0), (0.5, 100.0)])
def test_no_time_for_one_piece_raises_the_first_failure(harness, left, start, caplog):
    caplog.set_level(logging.DEBUG)
    h = None
    arr = Arrivals()
    parts = {
        "trials": [CUT, arr.step(T, CUT)],
        "outcomes": [UNREADABLE, lambda r: _at_decision(h, left, arr, (T, C, S))(r)],
        "context": [arr.step(C, ok(C))],
        "summary": [arr.step(S, ok(S))],
    }
    h = harness(parts, left=start)
    with pytest.raises(WriterFailed) as got:
        h.run()
    assert (got.value.failure, got.value.kind) == ("truncated", None)  # trials, first in order
    assert h.fake.count(ONE_PIECE) == 0
    assert h.lines.got == []
    assert "writer kind=no_time_for_one_piece" in caplog.text
    assert_private(got.value, caplog)


def test_150_seconds_left_is_time_for_one_piece(harness):
    h = None
    arr = Arrivals()
    parts = {s.name: [arr.step(s, ok(s))] for s in (O, C, S)}
    parts["trials"] = [UNREADABLE, lambda r: _at_decision(h, 150.0, arr, (O, C, S))(r)]
    h = harness(parts, one_piece=[piece()])
    assert h.run()[2] is False
    assert h.fake.count(ONE_PIECE) == 1


def test_no_time_after_a_failed_call_raises_its_kind(harness):
    h = None
    arr = Arrivals()
    parts = {s.name: [arr.step(s, ok(s))] for s in (T, O, S)}
    parts["context"] = [lambda r: _at_decision(h, 100.0, arr, (T, O, S), REFUSED_400)(r)]
    h = harness(parts)
    with pytest.raises(WriterFailed) as got:
        h.run()
    assert (got.value.failure, got.value.kind) == (None, "request_refused")


@pytest.mark.parametrize("script,failure,kind", [
    ([UNREADABLE, UNREADABLE], "unreadable", None),
    ([CUT, UNREADABLE], "truncated", None),
    ([UNREADABLE, CUT], "truncated", None),
    ([BLOCKED, UNREADABLE], "unreadable", None),
    ([UNREADABLE, REFUSED_400], "unreadable", None),
    ([REFUSED_400], None, "request_refused"),
], ids=["unreadable_twice", "cut_then_unreadable", "unreadable_then_cut", "blocked",
        "retry_call_failed", "first_call_failed"])
def test_one_piece_fails_raises(harness, script, failure, kind, caplog):
    caplog.set_level(logging.DEBUG)
    parts = all_ok()
    parts["trials"] = [UNREADABLE, UNREADABLE]
    h = harness(parts, one_piece=script)
    with pytest.raises(WriterFailed) as got:
        h.run()
    assert (got.value.failure, got.value.kind) == (failure, kind)
    assert h.fake.count(ONE_PIECE) == len(script)
    assert_private(got.value, caplog)


def test_write_one_piece_alone(harness):
    h = harness({}, one_piece=[piece()])
    answer, defaulted = write_one_piece(USER, caller=h.caller, run_id=None, timing=h.timing,
                                        on_wait=h.waits.append, clock=h.clock.now)
    assert (answer, defaulted) == merged_answer(PIECE)
    assert h.steps() == ["writer_one_piece"]


# ---------- fatal: the run ends at once (Q2) ----------


def test_cap_mid_writer_is_fatal(harness, caplog):
    caplog.set_level(logging.DEBUG)
    h = harness(all_ok(), run_cap=2)
    with pytest.raises(RunCapReached) as got:
        h.run()
    assert h.fake.count() == 2
    assert h.fake.count(ONE_PIECE) == 0
    assert len(h.steps()) == 2
    assert h.lines.got == []
    assert ("withdraw", None) in h.events
    assert_private(got.value, caplog)


@pytest.mark.parametrize("fatal,kind", [
    (DAILY, "daily_limit"),
    (error(403, "PERMISSION_DENIED"), "key_refused"),
    (error(402, "X"), "billing_refused"),
], ids=["daily_limit", "key_refused", "billing_refused"])
def test_daily_limit_in_a_part_is_fatal(harness, fatal, kind, caplog):
    caplog.set_level(logging.DEBUG)
    parts = all_ok()
    parts["context"] = [fatal]
    h = harness(parts, one_piece=[piece()])
    with pytest.raises(FatalModelError) as got:
        h.run()
    assert got.value.kind == kind
    assert h.fake.count(ONE_PIECE) == 0
    assert h.fake.count() == 4  # the other three parts' calls, already made
    assert h.lines.got == []
    assert got.value.__cause__ is None and got.value.__context__ is None
    assert_private(got.value, caplog)


def test_fatal_part_waits_for_the_others_then_raises(harness):
    """A fatal part never cancels the others: write_parts returns only when every part is in
    (here outcomes answers only after trials has failed and withdrawn)."""
    withdrawn = threading.Event()
    finished = []

    def outcomes(_request):
        assert withdrawn.wait(GUARD)
        finished.append("outcomes")
        return httpx.Response(200, json=ok(O))

    def on_part(ev):
        if ev[0] == "withdraw":
            withdrawn.set()

    parts = all_ok()
    parts["trials"] = [DAILY]
    parts["outcomes"] = [outcomes]
    h = harness(parts)
    with pytest.raises(FatalModelError):
        h.run(on_part=on_part)
    assert finished == ["outcomes"]
    assert h.fake.count(ONE_PIECE) == 0


def test_a_sibling_sends_no_retry_after_another_part_is_fatal(harness):
    """Owner Q2: once a part is fatal, a sibling whose reply was unreadable sends no retry
    (Incisive would send it: `llm.rs:1260` checks only the user's cancel)."""
    withdrawn = threading.Event()

    def trials_first(_request):
        assert withdrawn.wait(GUARD)  # arrives after context's fatal error
        return httpx.Response(200, json=UNREADABLE)

    def on_part(ev):
        if ev[0] == "withdraw":
            withdrawn.set()

    events = []
    parts = all_ok()
    parts["trials"] = [trials_first, ok(T)]  # the retry, never sent
    parts["context"] = [DAILY]
    h = harness(parts, one_piece=[piece()])
    with pytest.raises(FatalModelError) as got:
        h.run(on_part=lambda ev: (events.append(ev), on_part(ev)))
    assert got.value.kind == "daily_limit"
    assert h.fake.count() == 4
    assert h.fake.count("trials") == 1 and h.fake.count(ONE_PIECE) == 0
    assert "writer_trials_retry" not in h.steps()
    assert [e[0] for e in events].count("withdraw") == 1


def test_a_retry_already_sent_before_the_fatal_part_is_unaffected(harness):
    retry_sent = threading.Event()
    withdrawn = threading.Event()
    finished = []

    def trials_retry(_request):
        retry_sent.set()
        assert withdrawn.wait(GUARD)  # in flight while context is fatal
        finished.append("trials_retry")
        return httpx.Response(200, json=ok(T))

    def context_first(_request):
        assert retry_sent.wait(GUARD)
        return DAILY

    def on_part(ev):
        if ev[0] == "withdraw":
            withdrawn.set()

    parts = all_ok()
    parts["trials"] = [UNREADABLE, trials_retry]
    parts["context"] = [context_first]
    h = harness(parts, one_piece=[piece()])
    with pytest.raises(FatalModelError) as got:
        h.run(on_part=on_part)
    assert got.value.kind == "daily_limit"
    assert finished == ["trials_retry"]
    assert h.fake.count() == 5 and h.fake.count("trials") == 2
    assert h.fake.count(ONE_PIECE) == 0


def test_the_stop_flag_does_not_fire_for_a_non_fatal_failure(harness):
    withdrawn = threading.Event()

    def trials_first(_request):
        assert withdrawn.wait(GUARD)  # arrives after context failed (not fatally)
        return httpx.Response(200, json=UNREADABLE)

    def on_part(ev):
        if ev[0] == "withdraw":
            withdrawn.set()

    parts = all_ok()
    parts["trials"] = [trials_first, ok(T)]
    parts["context"] = [REFUSED_400]
    h = harness(parts, one_piece=[piece()])
    answer, _, merged = h.run(on_part=on_part)
    assert merged is False and answer == merged_answer(PIECE)[0]
    assert h.fake.count("trials") == 2  # its retry was sent
    assert h.fake.count() == 6 and h.fake.count(ONE_PIECE) == 1
    assert h.lines.got == [("write", ONE_PIECE_LINE.format(name="context"))]


def test_rate_limit_wait_past_the_hard_stop_is_a_hard_stop(harness, caplog):
    caplog.set_level(logging.DEBUG)
    parts = all_ok()
    parts["summary"] = [error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("500s")])]
    h = harness(parts, one_piece=[piece()])
    with pytest.raises(HardStop) as got:
        h.run()
    assert h.fake.count(ONE_PIECE) == 0
    assert ("withdraw", None) in h.events
    assert_private(got.value, caplog)


def test_a_reply_after_the_hard_stop_is_a_hard_stop(harness):
    h = None
    arr = Arrivals()
    parts = {s.name: [arr.step(s, ok(s))] for s in (O, C, S)}
    # At the hard stop exactly: within the attempt's limit, so the reply is read.
    parts["trials"] = [lambda r: _at_decision(h, 0.0, arr, (O, C, S), ok(T))(r)]
    h = harness(parts, one_piece=[piece()], left=100.0)
    with pytest.raises(HardStop):
        h.run()
    assert h.fake.count(ONE_PIECE) == 0


def test_rate_limit_waits_are_reported_from_the_part_threads(harness):
    parts = all_ok()
    parts["outcomes"] = [error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("12s")]), ok(O)]
    h = harness(parts)
    assert h.run()[2] is True
    assert h.waits == [12]
    assert h.steps().count("writer_outcomes") == 2  # the caller's own retry, same step


# ---------- events ----------


def test_summary_reported_early_then_withdrawn(harness):
    reported = threading.Event()
    h = None

    def trials_first(_request):
        assert reported.wait(GUARD)  # the summary is reported before trials is in
        return httpx.Response(200, json=UNREADABLE)

    def on_part(ev):
        h.events.append(ev)
        if ev[0] == "summary":
            reported.set()

    def context_first(_request):
        assert reported.wait(GUARD)
        return httpx.Response(200, json=CUT)

    parts = all_ok()
    parts["trials"] = [trials_first, UNREADABLE]
    parts["context"] = [context_first, CUT]  # fails too: still one withdraw
    h = harness(parts, one_piece=[piece()])
    h.run(on_part=on_part)
    kinds = [e[0] for e in h.events]
    assert kinds[0] == "summary"
    assert kinds.count("withdraw") == 1
    assert kinds[-1] == "withdraw"
    assert h.events[0][1] == part_of(PARTS, S)


def test_no_withdraw_after_report_order(harness):
    """Reports and the withdrawal go under one lock: a part read after a sibling failed for
    good is never reported, and a withdrawal comes after every report made before it."""
    withdrawn = threading.Event()
    summary_seen = threading.Event()
    h = None

    def trials_first(_request):
        assert summary_seen.wait(GUARD)
        return REFUSED_400

    def context_first(_request):
        assert withdrawn.wait(GUARD)  # read only after trials failed for good
        return httpx.Response(200, json=ok(C))

    def on_part(ev):
        h.events.append(ev)
        if ev[0] == "summary":
            summary_seen.set()
        if ev[0] == "withdraw":
            withdrawn.set()

    parts = all_ok()
    parts["trials"] = [trials_first]
    parts["context"] = [context_first]
    h = harness(parts, one_piece=[piece()])
    h.run(on_part=on_part)
    assert [e[0] for e in h.events] == ["summary", "withdraw"]


def test_context_reported_and_all_four_merged_without_withdraw(harness):
    h = harness(all_ok())
    h.run()
    assert sorted(e[0] for e in h.events) == ["context", "summary"]
    ctx = next(e[1] for e in h.events if e[0] == "context")
    assert ctx == part_of(PARTS, C)


def test_a_failing_callback_never_fails_the_writer(harness, caplog):
    caplog.set_level(logging.DEBUG)

    def bad(ev):
        raise RuntimeError(f"callback broke {M_SECRET}")

    h = harness(all_ok())
    answer, _, merged = h.run(on_part=bad)
    assert merged is True and answer == merged_answer()[0]
    assert "writer kind=callback_failed error=RuntimeError" in caplog.text
    assert_private(None, caplog)


def test_no_callback_is_fine(harness):
    h = harness(all_ok())
    assert h.run(on_part=None)[2] is True


# ---------- logs ----------


def test_part_log_lines_hold_names_kinds_and_counts_only(harness, caplog):
    caplog.set_level(logging.INFO, logger="pubmedgemini.writer")
    parts = all_ok()
    parts["outcomes"] = [CUT, ok(O)]
    parts["trials"] = [BLOCKED, UNREADABLE]
    h = harness(parts, one_piece=[piece()])
    h.run()
    lines = sorted(r.getMessage() for r in caplog.records if r.name == "pubmedgemini.writer")
    assert "writer part=outcomes attempt=first result=cut_off kind=truncated output_tokens=100" \
        in lines
    assert "writer part=outcomes attempt=retry result=ok kind=none output_tokens=321" in lines
    assert "writer part=trials attempt=first result=refused kind=block output_tokens=0" in lines
    assert "writer part=trials attempt=retry result=unreadable kind=syntax output_tokens=100" \
        in lines
    assert "writer part=one_piece attempt=first result=ok kind=none output_tokens=654" in lines
    assert "writer kind=parts_failed failed=1 first=trials" in lines
