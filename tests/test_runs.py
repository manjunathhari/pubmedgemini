"""Task 8: runs on a background thread, one at a time, each step stored as it happens
(Incisive `runs/mod.rs`, `migrations/0005_runs.sql`, `agent/mod.rs` `add_line`, `clip`,
`agent/team.rs` `run` for the stop lines and outcomes)."""

import json
import logging
import threading
import uuid

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from gemini_fakes import FAKE_KEY, FakeGemini, call, error, reply
from lit_fakes import FakeClock
from pydantic import SecretStr

from pubmedgemini.config import Settings
from pubmedgemini.gemini.caller import ModelCaller
from pubmedgemini.gemini.client import GeminiClient
from pubmedgemini.gemini.quota import GeminiPacer
from pubmedgemini.identity import ensure_owner
from pubmedgemini.lit.tools import ToolFailed
from pubmedgemini.main import create_app
from pubmedgemini.researcher.loop import SearchEntry, SeenPaper
from pubmedgemini.researcher.studylist import Considered, Contradiction, Selected, Validated
from pubmedgemini.runner import DbProgress, InlineStarter, ThreadStarter, run_one
from pubmedgemini.runs import (
    EMPTY_SELECTION,
    HARD_STOP_MESSAGE,
    MAX_STEP_LINE,
    NO_EVIDENCE,
    STOPPED_NO_ANSWER,
    ActiveRunExists,
    active_run,
    add_search,
    add_seen,
    add_step,
    clip,
    create_run,
    end_message,
    finish_run,
    finish_with_answer,
    get_run,
    list_runs,
    save_list,
    set_running,
    stop_unfinished,
)

MODEL = "gemini-3.5-flash-lite"
QUESTION = "Does laparoscopic repair reduce chronic pain after inguinal hernia surgery"
ID1, ID2, ID3 = "36262096", "33763744", "31696731"
Q1 = {"query": "hernia[tiab]", "max_results": 3}
Q2 = {"query": "hernia", "max_results": 3}
OWNER = "owner@example.com"


def turn(*calls):
    return reply(list(calls))


def list_reply(selected=(), considered=(), contradictions=()):
    return reply([{"text": json.dumps({
        "searches": [], "selected": list(selected), "considered_not_selected": list(considered),
        "possible_contradictions": list(contradictions)})}])


LIST_OK = list_reply(
    selected=[{"pmid": ID2, "design": "SR_MA", "why": "The newest meta-analysis."},
              {"pmid": ID1, "design": "RCT", "why": "A large trial."}],
    considered=[{"pmid": ID3, "reason": "Older."}],
    contradictions=[{"pmid": ID3, "note": "Found more pain."}])
HAPPY_RUN = [turn(call("search_pubmed", Q1), call("search_europepmc", Q2))] + \
    [turn(call("search_pubmed", Q1))] * 7 + [LIST_OK]
EMPTY = list_reply()
END = reply([{"text": "Done."}])


class RunApp:
    def __init__(self, pool, upstream):
        from conftest import make_tools

        self.pool = pool
        self.upstream = upstream
        ensure_owner(pool, OWNER)
        with pool.connection() as conn:
            self.owner_id = conn.execute("SELECT id FROM users WHERE email = %s",
                                         (OWNER,)).fetchone()[0]
        self.tools = make_tools(pool, upstream)
        self.gemini = None

    def caller(self, script, clock):
        self.gemini = FakeGemini(script)
        client = GeminiClient(SecretStr(FAKE_KEY), MODEL, self.gemini.transport, clock=clock.now)
        pacer = GeminiPacer(1000, 250_000, clock.now, clock.sleep)
        self.model_caller = ModelCaller(client, pacer, self.pool, MODEL, sleep=clock.sleep,
                                        clock=clock.now, jitter=lambda: 0.5)
        return self.model_caller

    def runner(self, script, tools=None):
        clock = self.clock = FakeClock()
        caller = self.caller(script, clock)
        lit = tools or self.tools
        return lambda rid: run_one(rid, pool=self.pool, tools=lit, caller=caller, model=MODEL,
                                   clock=clock.now)

    def start(self, question=QUESTION, script=HAPPY_RUN, tools=None):
        rid = create_run(self.pool, self.owner_id, question, MODEL)
        InlineStarter(self.runner(script, tools)).start(rid)
        return rid

    def view(self, rid):
        return get_run(self.pool, rid, self.owner_id)


@pytest.fixture
def run_app(migrated_pool, upstream):
    return RunApp(migrated_pool, upstream)


def rows(pool, sql, params=()):
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


# ---------- one full run ----------

def test_a_full_run_writes_lines_trail_and_list(run_app):
    rid = run_app.start()
    v = run_app.view(rid)
    assert v.status == "done" and v.stop_reason == "turn_cap" and v.rounds == 8
    assert v.error_kind is None and v.model == MODEL and v.question == QUESTION
    assert v.lines[0] == "Researcher: searching PubMed and Europe PMC"
    assert v.lines[-3:] == ["Researcher: selected 2 studies (1 RCT, 1 meta-analysis)",
                            "Search stopped at the round cap after 8 rounds",
                            "Study list ready"]  # Incisive's order
    assert [s.kind for s in v.steps][-3:] == ["search", "note", "present"]
    assert [p.pmid for p in v.selected] == [ID2, ID1] and v.selected[0].design == "SR_MA"
    assert v.selected[0].why == "The newest meta-analysis."
    assert v.searches[0] == SearchEntry("hernia[tiab]", "PubMed", 5160)
    assert v.searches[1].source == "Europe PMC" and len(v.searches) == 9
    assert [(c.pmid, c.reason) for c in v.considered] == [(ID3, "Older.")]
    assert v.considered[0].title  # from run_seen
    assert [(c.pmid, c.note) for c in v.contradictions] == [(ID3, "Found more pain.")]
    assert v.selected[0].paper.abstract and v.selected[1].paper.abstract
    assert v.selected[0].title == v.selected[0].paper.title
    assert v.started_at is not None and v.finished_at is not None
    assert end_message(v) is None and v.down_line is None


def test_every_model_call_of_the_run_is_counted_against_it(run_app):
    rid = run_app.start()
    n = rows(run_app.pool, "SELECT count(*) FROM model_calls WHERE run_id = %s", (rid,))[0][0]
    assert n == 9


def test_lines_are_stored_as_the_run_goes(run_app):
    seen_at_turn_2 = []

    def second(request):
        seen_at_turn_2.extend(r[0] for r in rows(
            run_app.pool, "SELECT line FROM run_steps ORDER BY seq"))
        return httpx.Response(200, json=END)

    script = [turn(call("search_pubmed", Q1)), second, LIST_OK]
    run_app.start(script=script)
    assert "Researcher: 1 search so far (turn 1 of 8)" in seen_at_turn_2
    assert rows(run_app.pool, "SELECT count(*) FROM run_searches")[0][0] == 1
    assert rows(run_app.pool, "SELECT count(*) FROM run_seen")[0][0] == 3


def test_the_run_is_running_while_it_works(run_app):
    status = []

    def first(request):
        status.append(rows(run_app.pool, "SELECT status, started_at IS NOT NULL FROM runs")[0])
        return httpx.Response(200, json=END)

    run_app.start(script=[first, EMPTY])
    assert status == [("running", True)]


# ---------- one at a time ----------

def test_second_run_is_refused_while_one_runs(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    with pytest.raises(ActiveRunExists) as e:
        create_run(run_app.pool, run_app.owner_id, "another question", MODEL)
    assert (e.value.run_id, e.value.own) == (rid, True)
    assert active_run(run_app.pool) == (rid, run_app.owner_id)
    assert rows(run_app.pool, "SELECT count(*) FROM runs")[0][0] == 1


def test_another_users_active_run_is_not_own(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    with run_app.pool.connection() as conn:
        other = conn.execute("INSERT INTO users (email, role) VALUES ('b@example.com', 'surgeon')"
                             " RETURNING id").fetchone()[0]
    with pytest.raises(ActiveRunExists) as e:
        create_run(run_app.pool, other, QUESTION, MODEL)
    assert (e.value.run_id, e.value.own) == (rid, False)


def test_a_finished_run_frees_the_slot(run_app):
    run_app.start(script=[END, EMPTY])
    assert active_run(run_app.pool) is None
    create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)


def test_run_ids_are_random_uuid4(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    assert isinstance(rid, uuid.UUID) and rid.version == 4


# ---------- start-up ----------

def test_start_up_stops_unfinished_runs(db_url, migrated_pool, caplog):
    ensure_owner(migrated_pool, OWNER)
    with migrated_pool.connection() as conn:
        uid = conn.execute("SELECT id FROM users").fetchone()[0]
        running = uuid.uuid4()
        conn.execute("INSERT INTO runs (id, user_id, question, status, model) VALUES "
                     "(%s, %s, 'q', 'running', 'm')", (running, uid))
        done = uuid.uuid4()
        conn.execute("INSERT INTO runs (id, user_id, question, status, model) VALUES "
                     "(%s, %s, 'q', 'done', 'm')", (done, uid))
    caplog.set_level(logging.INFO)
    settings = Settings(app_env="dev", database_url=db_url, owner_email=OWNER)
    with TestClient(create_app(settings)):
        pass
    got = dict(rows(migrated_pool, "SELECT id, status || '/' || coalesce(error_kind, '') "
                                   "FROM runs"))
    assert got == {running: "stopped/restarted", done: "done/"}
    assert "runs stopped_at_start=1" in [r.getMessage() for r in caplog.records]
    assert rows(migrated_pool, "SELECT finished_at IS NOT NULL FROM runs WHERE id = %s",
                (running,))[0][0]


def test_stop_unfinished_counts_queued_and_running(run_app):
    create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    assert stop_unfinished(run_app.pool) == 1 and stop_unfinished(run_app.pool) == 0


def test_start_up_logs_whether_a_key_is_set(db_url, caplog):
    caplog.set_level(logging.INFO)
    with TestClient(create_app(Settings(app_env="dev", database_url=db_url, owner_email=OWNER,
                                        gemini_api_key="gk-q7"))) as c:
        assert c.app.state.gemini_caller is not None
    msgs = [r.getMessage() for r in caplog.records]
    assert "gemini api_key=true model=gemini-3.5-flash-lite rpm=10 tpm=250000 rpd=500" in msgs
    assert "gk-q7" not in caplog.text
    caplog.clear()
    with TestClient(create_app(Settings(app_env="dev", database_url=db_url,
                                        owner_email=OWNER))) as c:
        assert c.app.state.gemini_caller is None
    assert "gemini api_key=false model=gemini-3.5-flash-lite rpm=10 tpm=250000 rpd=500" in \
        [r.getMessage() for r in caplog.records]


def test_app_runs_a_question_with_the_inline_starter(db_url, upstream):
    settings = Settings(app_env="dev", database_url=db_url, owner_email=OWNER,
                        gemini_api_key=FAKE_KEY)
    g = FakeGemini([END, EMPTY])
    starter = InlineStarter()
    app = create_app(settings, lit_transport=upstream.transport, ncbi_gap_seconds=0,
                     gemini_transport=g.transport, gemini_sleep=lambda s: None,
                     run_starter=starter)
    with TestClient(app) as c:
        pool = c.app.state.pool
        with pool.connection() as conn:
            uid = conn.execute("SELECT id FROM users").fetchone()[0]
        rid = create_run(pool, uid, QUESTION, MODEL)
        c.app.state.run_starter.start(rid)
        v = get_run(pool, rid, uid)
    assert v.status == "done" and len(g.sent) == 2 and starter.started == [rid]
    assert g.sent[0].headers["x-goog-api-key"] == FAKE_KEY


# ---------- reading runs ----------

def test_another_users_run_is_not_found(run_app):
    rid = run_app.start(script=[END, EMPTY])
    assert get_run(run_app.pool, rid, run_app.owner_id + 999) is None
    assert get_run(run_app.pool, uuid.uuid4(), run_app.owner_id) is None


def test_list_runs_newest_first_with_ist_times(run_app):
    first = run_app.start(question="first q", script=[END, EMPTY])
    second = create_run(run_app.pool, run_app.owner_id, "second q", MODEL)
    got = list_runs(run_app.pool, run_app.owner_id)
    assert [(r.id, r.question, r.status) for r in got] == [(second, "second q", "queued"),
                                                         (first, "first q", "done")]
    import re
    assert re.fullmatch(r"\d{1,2} [A-Z][a-z]{2} \d{4}, \d\d:\d\d", got[0].created_ist)
    assert list_runs(run_app.pool, run_app.owner_id + 999) == []
    assert len(list_runs(run_app.pool, run_app.owner_id, limit=1)) == 1


# ---------- outcomes ----------

TURN1_402 = [error(402, "X")]
EMPTY_TWICE = [turn(call("search_pubmed", Q1)), END, EMPTY, EMPTY]
UNREADABLE_TWICE = [turn(call("search_pubmed", Q1)), END, reply([{"text": "no"}]),
                    reply([{"text": "no"}])]
NOTHING_SELECTED = [turn(call("search_pubmed", Q1)), END,
                    list_reply(considered=[{"pmid": ID1, "reason": "Off topic."}])]
TURN_FAILED_WITH_PICKS = [turn(call("search_pubmed", Q1)), error(400, "X"), LIST_OK]
TURN_FAILED_NOTHING = [turn(call("search_pubmed", Q1)), error(400, "X"),
                       list_reply(considered=[{"pmid": ID1, "reason": "r"}])]


@pytest.mark.parametrize("script,status,kind,message", [
    (TURN1_402, "failed", "billing_refused", None),
    (EMPTY_TWICE, "partial", "empty_selection", EMPTY_SELECTION),
    (UNREADABLE_TWICE, "partial", "researcher_incomplete", STOPPED_NO_ANSWER),
    (NOTHING_SELECTED, "done", None, NO_EVIDENCE),
    (TURN_FAILED_WITH_PICKS, "partial", None, None),
    (TURN_FAILED_NOTHING, "partial", None, STOPPED_NO_ANSWER),
])
def test_outcomes(run_app, script, status, kind, message):
    rid = run_app.start(script=script)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == (status, kind)
    assert end_message(v) == message
    assert v.finished_at is not None


def test_turn1_failure_writes_no_closing_line(run_app):
    v = run_app.view(run_app.start(script=TURN1_402))
    assert v.lines == ["Researcher: searching PubMed and Europe PMC"]
    assert v.stop_reason is None and v.rounds is None


def test_empty_selection_keeps_the_trail_and_closes(run_app):
    v = run_app.view(run_app.start(script=EMPTY_TWICE))
    assert v.searches and v.selected == [] and v.lines[-1] == "Study list ready"
    assert "Researcher: the study list came back empty; selecting again" in v.lines


def test_final_failed_has_no_stop_line(run_app):
    v = run_app.view(run_app.start(script=UNREADABLE_TWICE))
    assert v.lines[-2:] == ["Researcher: the study list could not be completed",
                            "Study list ready"]
    assert v.stop_reason == "other"


def test_wrap_up_line_and_turn_failed_line(run_app):
    v = run_app.view(run_app.start(script=TURN_FAILED_WITH_PICKS))
    assert "Research stopped early (a failed model call)" in v.lines
    assert v.stop_reason == "turn_failed" and v.rounds == 1


def test_hard_stop_fails_the_run(run_app, monkeypatch):
    import pubmedgemini.runner as runner_mod
    from pubmedgemini.researcher.loop import HardStop

    def boom(*a, **k):
        raise HardStop()

    monkeypatch.setattr(runner_mod, "research", boom)
    v = run_app.view(run_app.start(script=[]))
    assert (v.status, v.error_kind) == ("failed", "hard_stop")
    assert end_message(v) == HARD_STOP_MESSAGE == (
        "This run ran out of time (7 minutes). Please run the question again.")


def test_list_call_cut_at_the_hard_stop_fails_the_run(run_app):  # review M3
    def cut(request):
        run_app.clock.t += 420
        raise httpx.ReadTimeout("cut")

    v = run_app.view(run_app.start(script=[turn(call("search_pubmed", Q1)), END, cut]))
    assert (v.status, v.error_kind) == ("failed", "hard_stop")
    assert end_message(v) == HARD_STOP_MESSAGE and v.selected == []


def test_fetch_past_the_hard_stop_fails_the_run(run_app, migrated_pool, upstream):  # M3
    from conftest import make_tools

    tools = make_tools(migrated_pool, upstream)
    real = tools.fetch_papers

    def slow(pmids):
        out = real(pmids)
        run_app.clock.t += 420
        return out

    tools.fetch_papers = slow
    v = run_app.view(run_app.start(tools=tools))
    assert (v.status, v.error_kind) == ("failed", "hard_stop")
    assert end_message(v) == HARD_STOP_MESSAGE and "Study list ready" not in v.lines


def test_no_fetch_once_past_the_hard_stop(run_app, migrated_pool, upstream, monkeypatch):
    # research returns a list just as the clock passes 420 s: no fetch, no list, hard_stop.
    from conftest import make_tools

    import pubmedgemini.runner as runner_mod
    from pubmedgemini.researcher.loop import Research

    tools = make_tools(migrated_pool, upstream)
    fetched = []
    tools.fetch_papers = lambda pmids: fetched.append(pmids) or {}

    def late(question, *, timing, clock, **k):
        run_app.clock.t = timing.hard_stop_at
        v = Validated((Selected(ID1, "RCT", "A trial."),), (), (), 0, 0, 0)
        return Research(v, (), frozenset({ID1}), "end_turn", 1, False, False, ())

    monkeypatch.setattr(runner_mod, "research", late)
    v = run_app.view(run_app.start(script=[], tools=tools))
    assert (v.status, v.error_kind) == ("failed", "hard_stop") and fetched == []
    assert v.selected == []


def test_fetch_failure_keeps_the_list(run_app, migrated_pool, upstream):
    from conftest import make_tools

    tools = make_tools(migrated_pool, upstream)

    def fail(pmids):
        raise ToolFailed("the database could not be reached; try again later or use another "
                         "tool", "network")

    tools.fetch_papers = fail
    v = run_app.view(run_app.start(tools=tools))
    assert v.status == "done" and [p.pmid for p in v.selected] == [ID2, ID1]
    assert all(p.paper is None for p in v.selected)
    assert v.selected[0].title  # from run_seen


def test_source_down_line(run_app, migrated_pool):
    from conftest import make_tools
    from lit_fakes import EPMC_HOST, EPMC_SEARCH_PATH, FakeUpstream, europepmc_routes, pubmed_routes

    routes = pubmed_routes() | europepmc_routes()
    routes[(EPMC_HOST, EPMC_SEARCH_PATH)] = httpx.Response(503)
    tools = make_tools(migrated_pool, FakeUpstream(routes))
    v = run_app.view(run_app.start(script=[turn(call("search_pubmed", Q1),
                                                call("search_europepmc", Q2)), END, LIST_OK],
                                   tools=tools))
    assert v.lines[-2:] == ["Europe PMC could not be reached; searched PubMed only",
                            "Study list ready"]
    assert v.down_line == "Europe PMC could not be reached; searched PubMed only"


def test_an_unexpected_error_fails_the_run_with_a_fixed_kind(run_app, monkeypatch, caplog):
    import pubmedgemini.runner as runner_mod

    def boom(*a, **k):
        return 1 / 0

    monkeypatch.setattr(runner_mod, "research", boom)
    caplog.set_level(logging.INFO)
    v = run_app.view(run_app.start(script=[]))
    assert (v.status, v.error_kind) == ("failed", "internal")
    assert "run kind=internal error=ZeroDivisionError" in [r.getMessage() for r in caplog.records]
    assert "Traceback" not in caplog.text and "division" not in caplog.text


# ---------- the per-run cap (owner decision, 11 Nov 2026: cap calls per run) ----------

BUSY = [error(503, "UNAVAILABLE")] * 2
SEARCH = turn(call("search_pubmed", Q1))
# Gemini answers 503 twice per call: 3 attempts a call; the cap of 14 comes on turn 5.
BUSY_RUN = (BUSY + [SEARCH]) * 4 + BUSY + [SEARCH, LIST_OK]
# The most calls a run makes: 8 turns, the list call (unreadable), its nudge (empty) and S3.
ELEVEN = [SEARCH] * 8 + [reply([{"text": "no"}]), EMPTY, LIST_OK]


def calls_of(pool, rid):
    return rows(pool, "SELECT count(*) FROM model_calls WHERE run_id = %s", (rid,))[0][0]


def test_a_busy_gemini_reaches_the_cap_and_fails_the_run(run_app, caplog):
    caplog.set_level(logging.INFO)
    rid = run_app.start(script=BUSY_RUN)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("failed", "run_call_cap")
    assert v.stop_reason is None and v.rounds is None and v.finished_at is not None
    assert calls_of(run_app.pool, rid) == 14 and len(run_app.gemini.sent) == 14
    assert len(run_app.gemini.script) == 2  # nothing sent after the cap
    assert end_message(v) is None  # the page words it from the kind
    assert f"run kind=finished run_id={rid} status=failed error_kind=run_call_cap" in [
        r.getMessage() for r in caplog.records]


def test_the_runs_attempt_count_is_forgotten_when_the_run_ends(run_app, monkeypatch):
    # The count is kept while the run goes (the cap holds: 14 attempts, none after) and is
    # gone at its end, whether the run is done, capped or failed inside.
    import pubmedgemini.runner as runner_mod

    held = []
    real = ModelCaller.forget

    def spy(self, run_id):
        held.append(dict(self._sent))  # what the map held at the moment of the call
        real(self, run_id)

    monkeypatch.setattr(ModelCaller, "forget", spy)
    capped = run_app.start(script=BUSY_RUN)
    assert held == [{capped: 14}] and run_app.model_caller._sent == {}
    assert calls_of(run_app.pool, capped) == 14

    held.clear()
    done = run_app.start(script=ELEVEN)
    assert held == [{done: 11}] and run_app.model_caller._sent == {}

    monkeypatch.setattr(runner_mod, "research", lambda *a, **k: 1 / 0)
    run_app.start(script=[])
    assert run_app.model_caller._sent == {}


def test_a_normal_eleven_call_run_is_unaffected(run_app):
    rid = run_app.start(script=ELEVEN)
    v = run_app.view(rid)
    assert (v.status, v.error_kind, v.stop_reason) == ("done", None, "turn_cap")
    assert [p.pmid for p in v.selected] == [ID2, ID1]
    assert "Researcher: the study list came back empty; selecting again" in v.lines
    assert calls_of(run_app.pool, rid) == 11 and run_app.gemini.script == []


def test_eleven_calls_with_three_retries_still_finish(run_app):
    # Exactly the cap: three transport retries spread over the run.
    script = [BUSY[0]] + ELEVEN[:8] + [BUSY[0], ELEVEN[8], ELEVEN[9], BUSY[0], ELEVEN[10]]
    rid = run_app.start(script=script)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("done", None)
    assert calls_of(run_app.pool, rid) == 14


def test_the_s3_retry_counts_toward_the_cap(run_app):
    # 11 attempts in 7 turns, the list call and the nudge: S3's first attempt is the 14th,
    # its retry would be the 15th.
    script = (BUSY + [SEARCH]) * 2 + [SEARCH] * 4 + [END, reply([{"text": "no"}]), EMPTY,
                                                       BUSY[0], LIST_OK]
    rid = run_app.start(script=script)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("failed", "run_call_cap")
    assert calls_of(run_app.pool, rid) == 14 and run_app.gemini.script == [LIST_OK]


def test_the_nudge_counts_toward_the_cap(run_app):
    # 11 attempts in 7 turns, two for the list call: the nudge's retry would be the 15th.
    script = (BUSY + [SEARCH]) * 2 + [SEARCH] * 4 + [END, BUSY[0], reply([{"text": "no"}]),
                                                       BUSY[0], LIST_OK]
    rid = run_app.start(script=script)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("failed", "run_call_cap")
    assert calls_of(run_app.pool, rid) == 14 and run_app.gemini.script == [LIST_OK]


# ---------- the writers ----------

def test_clip_is_incisives():  # agent/mod.rs clip
    assert clip("  a\tb\n\nc\x00d  ", 160) == "a b c d"
    assert clip("x" * 200, 160) == "x" * 159 + "\u2026"
    assert clip("ab " * 100, 10) == "ab ab ab\u2026"
    assert MAX_STEP_LINE == 160


def test_add_step_cuts_and_numbers(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    add_step(run_app.pool, rid, "note", "y" * 400)
    add_step(run_app.pool, rid, "search", "two")
    got = rows(run_app.pool, "SELECT seq, kind, char_length(line) FROM run_steps ORDER BY seq")
    assert got == [(1, "note", 160), (2, "search", 3)]


def test_a_failed_step_write_is_logged_and_ignored(run_app, caplog):
    caplog.set_level(logging.WARNING)
    add_step(run_app.pool, uuid.uuid4(), "note", "zq7 orphan")  # no such run: FK violation
    msgs = [r.getMessage() for r in caplog.records]
    assert "run kind=step_not_stored" in msgs and "zq7" not in caplog.text


def test_save_list_stores_design_for_selected_only(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    v = Validated(selected=(Selected("1", "RCT", "w1"), Selected("2", "other", "w2")),
                  considered=(Considered("3", "r"),),
                  contradictions=(Contradiction("1", "n"), Contradiction("1", "again")),
                  dropped_unseen=0, duplicates=0, over_cap=0)
    save_list(run_app.pool, rid, v)
    got = rows(run_app.pool, "SELECT role, rank, pmid, design, note FROM run_papers "
                             "ORDER BY role, rank")
    assert got == [("considered", 1, "3", None, "r"), ("contradiction", 1, "1", None, "n"),
                   ("selected", 1, "1", "RCT", "w1"), ("selected", 2, "2", "other", "w2")]


def test_add_search_and_seen(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    add_search(run_app.pool, rid, SearchEntry("q1", "PubMed", 5))
    add_search(run_app.pool, rid, SearchEntry("q2", "PubMed related", 0))
    add_seen(run_app.pool, rid, [SeenPaper("1", "T\x00itle", "J", 2020)])
    add_seen(run_app.pool, rid, [SeenPaper("1", "Other", None, None), SeenPaper("2", "U", None,
                                                                              None)])
    assert rows(run_app.pool, "SELECT seq, query, hits FROM run_searches ORDER BY seq") == \
        [(1, "q1", 5), (2, "q2", 0)]
    assert rows(run_app.pool, "SELECT pmid, title FROM run_seen ORDER BY pmid") == \
        [("1", "Title"), ("2", "U")]


def test_db_progress_writes_at_once(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    p = DbProgress(run_app.pool, rid)
    p.line("note", "hello")
    assert rows(run_app.pool, "SELECT line FROM run_steps") == [("hello",)]


def test_set_running_and_finish_run(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    assert finish_run(run_app.pool, rid, "partial", "empty_selection", "turn_cap", 8) is True
    v = run_app.view(rid)
    assert (v.status, v.error_kind, v.stop_reason, v.rounds) == ("partial", "empty_selection",
                                                                 "turn_cap", 8)
    rid2 = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid2)
    with pytest.raises(psycopg.errors.CheckViolation):  # on a running row (review L5)
        finish_run(run_app.pool, rid2, "weird", None, None, None)


def test_finish_run_never_overwrites_a_stopped_run(run_app, caplog):  # review L5
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    assert stop_unfinished(run_app.pool) == 1  # a restart marked it stopped
    caplog.set_level(logging.DEBUG)
    assert finish_run(run_app.pool, rid, "done", None, "turn_cap", 8) is False
    v = run_app.view(rid)
    assert (v.status, v.error_kind, v.stop_reason, v.rounds) == ("stopped", "restarted", None,
                                                                 None)
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("pubmedgemini")]
    assert msgs == [f"run kind=finish_not_applied run_id={rid}"]


def test_finish_run_twice_keeps_the_first(run_app):  # review L5
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    assert finish_run(run_app.pool, rid, "failed", "hard_stop", None, None) is True  # queued
    assert finish_run(run_app.pool, rid, "done", None, "turn_cap", 8) is False
    assert (run_app.view(rid).status, run_app.view(rid).error_kind) == ("failed", "hard_stop")


# ---------- the background thread ----------

def test_thread_starter_runs_in_the_background(run_app):
    release = threading.Event()
    entered = threading.Event()

    def first(request):
        entered.set()
        assert release.wait(5), "the test never released the run"
        return httpx.Response(200, json=END)

    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    starter = ThreadStarter(run_app.runner([first, EMPTY]))
    starter.start(rid)  # returns at once: the run waits inside its first model call
    assert entered.wait(5)
    assert run_app.view(rid).status == "running"
    t = starter.threads[-1]
    assert t.daemon and t.name == "run" and t.is_alive()
    release.set()
    starter.join(timeout=10)
    assert not t.is_alive() and run_app.view(rid).status == "done"


# ---------- F3a Task 1: writer steps, step kinds, safe lines, the stored answer ----------

WRITER_STEPS = [f"writer_{p}{r}" for p in ("trials", "outcomes", "context", "summary")
                for r in ("", "_retry")] + ["writer_one_piece", "writer_one_piece_retry"]


@pytest.mark.parametrize("step", WRITER_STEPS)
def test_writer_steps_are_counted(run_app, step):
    from pubmedgemini.gemini.quota import calls_today, record_attempt

    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    record_attempt(run_app.pool, model=MODEL, run_id=rid, step=step, attempt=1)
    assert calls_today(run_app.pool, MODEL) == 1


def test_an_unknown_writer_step_is_refused(run_app):
    from pubmedgemini.gemini.quota import record_attempt

    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    for step in ("writer", "writer_outcomes_retry_retry", "writer_intro"):
        with pytest.raises(psycopg.errors.CheckViolation):
            record_attempt(run_app.pool, model=MODEL, run_id=rid, step=step, attempt=1)


def test_step_kinds_write_and_check_are_stored(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    for kind in ("search", "note", "write", "check", "present"):
        add_step(run_app.pool, rid, kind, f"line {kind}")
    assert rows(run_app.pool, "SELECT seq, kind FROM run_steps ORDER BY seq") == [
        (1, "search"), (2, "note"), (3, "write"), (4, "check"), (5, "present")]


def test_unknown_step_kind_is_refused(run_app, caplog):
    caplog.set_level(logging.WARNING)
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    add_step(run_app.pool, rid, "answer", "x")  # a CHECK violation: logged, ignored
    assert rows(run_app.pool, "SELECT count(*) FROM run_steps") == [(0,)]
    assert "run kind=step_not_stored" in [r.getMessage() for r in caplog.records]
    with run_app.pool.connection() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO run_steps (run_id, seq, kind, line) VALUES (%s, 1, 'x', 'l')",
                     (rid,))


def test_four_threads_store_every_line(run_app, caplog):
    caplog.set_level(logging.WARNING)
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    barrier = threading.Barrier(4)

    def work(n):
        barrier.wait(timeout=10)
        for i in range(50):
            add_step(run_app.pool, rid, "write", f"t{n} line {i}")

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    got = rows(run_app.pool, "SELECT seq FROM run_steps WHERE run_id = %s ORDER BY seq", (rid,))
    assert [r[0] for r in got] == list(range(1, 201))
    assert "step_not_stored" not in caplog.text


BODY = {"summary": {"text": "A finding."}, "references": []}


def test_finish_with_answer_stores_answer_and_status_together(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    assert finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, BODY) is None
    assert rows(run_app.pool, "SELECT version, body FROM answers WHERE run_id = %s", (rid,)) \
        == [(3, BODY)]
    v = run_app.view(rid)
    assert (v.status, v.stop_reason, v.rounds, v.answer) == ("done", "end_turn", 9, BODY)
    assert rows(run_app.pool, "SELECT finished_at IS NOT NULL FROM runs WHERE id = %s",
                (rid,)) == [(True,)]


def test_get_run_has_no_answer_when_none_was_stored(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    assert run_app.view(rid).answer is None


def test_answer_and_status_are_stored_together(run_app):
    """A body that cannot be stored (a NUL in a JSON string): no answer row and the status
    stays running, in one rollback."""
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    with pytest.raises(psycopg.Error):
        finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, {"summary": "a\x00b"})
    assert rows(run_app.pool, "SELECT count(*) FROM answers") == [(0,)]
    assert rows(run_app.pool, "SELECT status, finished_at FROM runs WHERE id = %s",
                (rid,)) == [("running", None)]


def test_a_late_answer_never_overwrites_a_stopped_run(run_app, caplog):
    caplog.set_level(logging.INFO)
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    stop_unfinished(run_app.pool)
    assert finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, BODY) is None
    assert rows(run_app.pool, "SELECT count(*) FROM answers") == [(0,)]
    assert rows(run_app.pool, "SELECT status FROM runs WHERE id = %s", (rid,)) == [("stopped",)]
    assert f"run kind=finish_not_applied run_id={rid}" in [
        r.getMessage() for r in caplog.records]
