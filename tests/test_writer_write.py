"""Task 5 (F3): Incisive's four-part writer on Gemini, with its retries and the one-piece
fallback (Incisive `agent/writer_v3.rs` `section_reply`, `write_quick_parallel_with`,
`write_counted`, `fatal`).

Gemini is PartsGemini (replies served by the part a request writes); the clock is a FakeClock
shared with the pacer (10 requests a minute: starts 6 s apart) and the caller."""

import logging
import re
import threading

import pytest
from gemini_fakes import FAKE_KEY, QUOTA_FAILURE, RETRY_INFO, error
from lit_fakes import FakeClock
from pydantic import SecretStr
from writer_fakes import (
    ONE_PIECE,
    PartsGemini,
    cut_off,
    fixture_part,
    json_reply,
    parts_ok,
    section_word,
    text_reply,
    user_of,
    whole_ok,
)

from pubmedgemini.gemini.caller import ModelCaller, RunCapReached
from pubmedgemini.gemini.client import GeminiClient
from pubmedgemini.gemini.quota import GeminiPacer
from pubmedgemini.researcher.loop import FatalModelError, HardStop, Timing
from pubmedgemini.researcher.prompts import SCHEMA_NUDGE, SOURCE_RULE
from pubmedgemini.writer.prompts import (
    SYSTEM,
    WRITER_MAX_TOKENS,
    Section,
    StudyInput,
    section_user,
    writer_user,
)
from pubmedgemini.writer.write import WriteFailure, write_quick

MODEL = "gemini-3.5-flash-lite"
STUDY = StudyInput(pmid="90000001", title="A trial", journal="Hernia", year=2025,
                   pub_types=("Meta-Analysis",), doi=None, retracted=False,
                   abstract="Chronic pain RR 0.38.", design="SR_MA", why="the newest")
USER = writer_user("q?", [STUDY], [], [])
ONE_LINE = "Writer: section {} failed; writing the answer in one piece"


class LockedClock(FakeClock):
    """FakeClock whose sleeps may come from four threads."""

    def __init__(self, start: float = 1000.0) -> None:
        super().__init__(start)
        self._lock = threading.Lock()

    def sleep(self, seconds: float) -> None:
        with self._lock:
            super().sleep(seconds)

    def jump(self, seconds: float) -> None:
        with self._lock:
            self.t += seconds


class RecordingPacer(GeminiPacer):
    """The F2 pacer, recording the start it reserves for each attempt (under its lock: the
    shared fake clock moves on before a thread's request reaches the transport)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.starts: list[float] = []

    def _start_time(self, now, estimated):
        t = super()._start_time(now, estimated)
        self.starts.append(t)
        return t


class Progress:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def line(self, kind, text):
        with self._lock:
            self.lines.append((kind, text))

    def search(self, entry):
        raise AssertionError("the writer records no search")

    def seen(self, papers):
        raise AssertionError("the writer records no paper")


class Ctx:
    def __init__(self, g, clock, prog, caller, timing):
        self.g, self.clock, self.prog, self.caller, self.timing = g, clock, prog, caller, timing

    @property
    def texts(self):
        return [t for _, t in self.prog.lines]


@pytest.fixture
def write_with(migrated_pool):
    """`write_with(script, at=0, run_cap=None, on_request=None)`: the writer started `at`
    seconds into the run. Returns (Written, Ctx); a raised failure propagates, with the Ctx
    on `write_with.last`."""

    def run(script, at=0.0, run_cap=None, on_request=None):
        clock = LockedClock()
        g = PartsGemini(script, clock=clock,
                        on_request=(lambda w, b: on_request(w, b, clock)) if on_request else None)
        client = GeminiClient(SecretStr(FAKE_KEY), MODEL, g.transport, clock=clock.now)
        pacer = RecordingPacer(10, 250_000, clock.now, clock.sleep)
        caller = ModelCaller(client, pacer, migrated_pool, MODEL, sleep=clock.sleep,
                             clock=clock.now, jitter=lambda: 0.5)
        if run_cap is not None:
            caller.run_cap = run_cap
        timing = Timing.quick(clock.t - at)
        prog = Progress()
        ctx = Ctx(g, clock, prog, caller, timing)
        ctx.pacer = pacer
        run.last = ctx
        w = write_quick(USER, caller=caller, run_id=None, timing=timing, progress=prog,
                        clock=clock.now)
        return w, ctx

    return run


def steps_in_db(pool) -> list[str]:
    with pool.connection() as conn:
        return [r[0] for r in conn.execute("SELECT step FROM model_calls ORDER BY id")]


def test_four_parts_merge_and_each_has_its_schema(write_with, migrated_pool):
    w, c = write_with(parts_ok())
    assert w.merged and w.labels_defaulted == 0
    assert w.answer.outcomes[0].outcome == "Chronic postoperative groin pain"
    assert len(w.answer.studies) == 8 and w.answer.at_a_glance is not None
    assert sorted(c.g.words()) == ["CONTEXT", "OUTCOMES", "SUMMARY", "TRIALS"]
    for body in c.g.sent:
        s = Section[section_word(body)]
        gen = body["generationConfig"]
        assert set(gen["responseSchema"]["properties"]) == set(s.fields)
        assert gen["maxOutputTokens"] == s.max_tokens(WRITER_MAX_TOKENS)
        assert gen["responseMimeType"] == "application/json"
        assert body["systemInstruction"]["parts"][0]["text"] == f"{SYSTEM}\n\n{SOURCE_RULE}"
        assert user_of(body) == section_user(USER, s)
        assert "propertyOrdering" not in str(body)
    assert sorted(steps_in_db(migrated_pool)) == ["writer_context", "writer_outcomes",
                                                  "writer_summary", "writer_trials"]
    assert c.prog.lines == []


def test_four_parts_start_six_seconds_apart(write_with):
    _, c = write_with(parts_ok())
    first = min(c.pacer.starts)
    assert sorted(t - first for t in c.pacer.starts) == pytest.approx([0, 6, 12, 18], abs=0.01)
    assert len(c.g.sent) == 4


def test_a_part_cut_off_gets_one_retry_with_the_nudge(write_with, migrated_pool):
    w, c = write_with(parts_ok() | {"OUTCOMES": [cut_off(), json_reply(fixture_part("outcomes"))]})
    retry = [b for b in c.g.sent if section_word(b) == "OUTCOMES"][1]
    assert user_of(retry) == section_user(USER, Section.OUTCOMES) + SCHEMA_NUDGE
    assert w.merged and "writer_outcomes_retry" in steps_in_db(migrated_pool)


def test_an_unreadable_part_gets_one_retry(write_with):
    w, c = write_with(parts_ok() | {"SUMMARY": [text_reply("not json"),
                                                json_reply(fixture_part("summary"))]})
    assert w.merged and c.g.words().count("SUMMARY") == 2


def test_one_failed_part_waits_for_the_others_then_falls_back(write_with, migrated_pool):
    w, c = write_with(parts_ok() | {"TRIALS": [text_reply("no"), text_reply("no")],
                                    ONE_PIECE: [whole_ok()]})
    assert not w.merged and w.answer.outcomes[0].outcome == "Chronic postoperative groin pain"
    assert ("write", ONE_LINE.format("trials")) in c.prog.lines
    words = c.g.words()
    assert sum(1 for x in words if x in ("OUTCOMES", "CONTEXT", "SUMMARY")) == 3  # not re-called
    assert words[-1] == ONE_PIECE and words.count(ONE_PIECE) == 1
    one = c.g.sent[-1]
    assert user_of(one) == USER
    assert set(one["generationConfig"]["responseSchema"]["properties"]) == {
        f for s in Section for f in s.fields}
    assert one["generationConfig"]["maxOutputTokens"] == WRITER_MAX_TOKENS
    assert steps_in_db(migrated_pool)[-1] == "writer_one_piece"


def test_the_one_piece_writer_gets_one_retry(write_with, migrated_pool):
    w, c = write_with(parts_ok() | {"CONTEXT": [text_reply("no"), text_reply("no")],
                                    ONE_PIECE: [text_reply("no"), whole_ok()]})
    assert not w.merged
    assert user_of(c.g.sent[-1]) == USER + SCHEMA_NUDGE
    assert steps_in_db(migrated_pool)[-2:] == ["writer_one_piece", "writer_one_piece_retry"]
    assert ONE_LINE.format("context") in c.texts


def test_the_first_failed_part_names_the_line(write_with):
    _, c = write_with(parts_ok() | {"SUMMARY": [text_reply("no"), text_reply("no")],
                                    "OUTCOMES": [text_reply("no"), text_reply("no")],
                                    ONE_PIECE: [whole_ok()]})
    assert c.texts == [ONE_LINE.format("outcomes")]  # Section order, not finishing order


def test_no_fallback_with_less_than_150_s_left(write_with):
    with pytest.raises(WriteFailure) as e:
        write_with(parts_ok() | {"TRIALS": [text_reply("no"), text_reply("no")]}, at=280)
    assert (e.value.kind, e.value.error_kind, str(e.value)) == ("unreadable", None, "unreadable")
    assert ONE_PIECE not in write_with.last.g.words() and write_with.last.prog.lines == []


def test_truncated_twice_then_fallback_truncated_is_truncated(write_with):
    with pytest.raises(WriteFailure) as e:
        write_with(parts_ok() | {"TRIALS": [cut_off("trials"), cut_off("trials")],
                                 ONE_PIECE: [cut_off(ONE_PIECE), cut_off(ONE_PIECE)]})
    assert e.value.kind == "truncated"


def test_cut_off_then_unreadable_stays_truncated(write_with):
    with pytest.raises(WriteFailure) as e:
        write_with(parts_ok() | {"TRIALS": [cut_off("trials"), text_reply("no")]}, at=280)
    assert e.value.kind == "truncated"


def test_unreadable_then_cut_off_is_truncated(write_with):
    with pytest.raises(WriteFailure) as e:
        write_with(parts_ok() | {"TRIALS": [text_reply("no"), cut_off("trials")]}, at=280)
    assert e.value.kind == "truncated"


def test_first_call_error_is_a_call_failure_with_its_kind(write_with):
    busy = [error(503, "UNAVAILABLE")] * 3
    with pytest.raises(WriteFailure) as e:
        write_with(parts_ok() | {"SUMMARY": list(busy), ONE_PIECE: list(busy)})
    assert (e.value.kind, e.value.error_kind, str(e.value)) == ("call", "overloaded", "call")
    assert e.value.__context__ is None and e.value.__cause__ is None


def test_a_failed_retry_keeps_the_first_failure(write_with):
    with pytest.raises(WriteFailure) as e:
        write_with(parts_ok() | {"TRIALS": [cut_off("trials")] + [error(503, "UNAVAILABLE")] * 3},
                   at=280)
    assert e.value.kind == "truncated"


def test_a_refused_reply_is_a_failed_call_never_read_and_not_retried(write_with):
    blocked = {"promptFeedback": {"blockReason": "SAFETY"}}
    safety = json_reply(fixture_part("trials"), finish="SAFETY")
    with pytest.raises(WriteFailure) as e:
        write_with(parts_ok() | {"TRIALS": [safety], ONE_PIECE: [blocked]})
    assert (e.value.kind, e.value.error_kind) == ("call", "refused")
    assert write_with.last.g.words().count("TRIALS") == 1


def test_daily_limit_in_a_part_is_fatal_after_the_others(write_with):
    daily = error(429, "RESOURCE_EXHAUSTED", [QUOTA_FAILURE("GenerateRequestsPerDayPerProject")])
    with pytest.raises(FatalModelError) as e:
        write_with(parts_ok() | {"CONTEXT": [daily]})
    assert e.value.kind == "daily_limit" and e.value.__context__ is None
    assert sorted(write_with.last.g.words()) == ["CONTEXT", "OUTCOMES", "SUMMARY", "TRIALS"]


def test_a_fatal_error_on_the_retry_propagates(write_with):
    daily = error(429, "RESOURCE_EXHAUSTED", [QUOTA_FAILURE("GenerateRequestsPerDayPerProject")])
    with pytest.raises(FatalModelError):
        write_with(parts_ok() | {"TRIALS": [text_reply("no"), daily]})


def test_run_cap_reached_mid_writer_fails_the_run(write_with):
    with pytest.raises(RunCapReached):
        write_with(parts_ok(), run_cap=2)
    assert len(write_with.last.g.sent) == 2


def test_hard_stop_during_the_writer(write_with):
    sent = []

    def late(word, body, clock):  # the fourth request to arrive takes the clock past 420 s
        sent.append(word)
        if len(sent) == 4:
            clock.jump(500)

    with pytest.raises(HardStop):
        write_with(parts_ok(), on_request=late)
    assert len(write_with.last.g.sent) == 4  # the others were in flight; nothing is cancelled


def test_deadline_reached_in_a_part_is_no_time(write_with, caplog):
    caplog.set_level(logging.INFO, logger="pubmedgemini")
    wait = error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("300s")])
    with pytest.raises(WriteFailure) as e:
        write_with(parts_ok() | {"SUMMARY": [wait]}, at=280)
    assert e.value.kind == "no_time"  # 300 s asked with under 140 s left; no time to fall back
    assert any(m.getMessage().startswith("writer part=summary attempt=first outcome=no_time")
               for m in caplog.records)


def test_log_lines_hold_kinds_and_counts_only(write_with, caplog):
    caplog.set_level(logging.DEBUG, logger="pubmedgemini")
    write_with(parts_ok() | {"TRIALS": [text_reply("vq4 not json"), text_reply("vq4 no")],
                             ONE_PIECE: [whole_ok()]})
    msgs = [m.getMessage() for m in caplog.records if m.name.startswith("pubmedgemini.writer")]
    pattern = (r"writer part=(trials|outcomes|context|summary|one_piece) attempt=(first|retry) "
               r"outcome=(ok|truncated|unreadable|call_failed|no_time) "
               r"category=(syntax|shape|truncated|none) output_tokens=\d+"
               r"|writer parts_failed parts=[a-z,]+"
               r"|writer done merged=(true|false) outcomes=\d+ numbers=\d+ studies=\d+ "
               r"labels_defaulted=\d+")
    assert msgs and all(re.fullmatch(pattern, m) for m in msgs), msgs
    assert "writer parts_failed parts=trials" in msgs
    assert "writer done merged=false outcomes=6 numbers=16 studies=8 labels_defaulted=0" in msgs
    for leak in ("vq4", "groin", "Lichtenstein", "SECTION TO WRITE", "<source"):
        assert leak not in caplog.text
