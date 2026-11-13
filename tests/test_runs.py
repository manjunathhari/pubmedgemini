"""Task 8: runs on a background thread, one at a time, each step stored as it happens
(Incisive `runs/mod.rs`, `migrations/0005_runs.sql`, `agent/mod.rs` `add_line`, `clip`,
`agent/team.rs` `run` for the stop lines and outcomes). F3a Task 7: the run writes, checks and
stores the answer (`team.rs:343-772`); Gemini is `RunGemini`, so a run that reaches the writer
gets its four parts."""

import json
import logging
import threading
import uuid

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from gemini_fakes import FAKE_KEY, call, error, reply
from pydantic import SecretStr
from writer_fakes import LockedClock, RunGemini, inline_intro

from pubmedgemini.config import Settings
from pubmedgemini.gemini.caller import ModelCaller
from pubmedgemini.gemini.client import GeminiClient
from pubmedgemini.gemini.quota import RUN_ATTEMPTS, GeminiPacer
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
    check_begin,
    check_finish,
    clear_early,
    clip,
    create_run,
    end_message,
    finish_run,
    finish_with_answer,
    get_run,
    intro_finish,
    intro_start,
    list_runs,
    save_list,
    set_running,
    stop_unfinished,
    store_early,
)
from pubmedgemini.writer.prompts import Section

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

    def caller(self, script, clock, parts=None, one_piece=(), intro=None):
        # The researcher's calls in order (F2's FakeGemini); the writer's parts and one piece
        # by route, on its four threads (RoutedFake); by default every part answers at once.
        # F3c: the Introduction by its own route; by default it answers at once.
        self.gemini = RunGemini(script, parts, one_piece, intro)
        client = GeminiClient(SecretStr(FAKE_KEY), MODEL, self.gemini.transport, clock=clock.now)
        pacer = GeminiPacer(1000, 250_000, clock.now, clock.sleep)
        self.model_caller = ModelCaller(client, pacer, self.pool, MODEL, sleep=clock.sleep,
                                        clock=clock.now, jitter=lambda: 0.5)
        return self.model_caller

    def runner(self, script, tools=None, parts=None, one_piece=(), intro=None,
               start_intro=inline_intro):
        clock = self.clock = LockedClock()  # read and moved by the writer's four threads too
        caller = self.caller(script, clock, parts, one_piece, intro)
        lit = tools or self.tools
        # F3c: by default the Introduction is written at once, before the research (so its
        # attempt is always the run's first); a test may pass the real thread starter.
        return lambda rid: run_one(rid, pool=self.pool, tools=lit, caller=caller, model=MODEL,
                                   clock=clock.now, start_intro=start_intro)

    def start(self, question=QUESTION, script=HAPPY_RUN, tools=None, parts=None, one_piece=(),
              intro=None, start_intro=inline_intro):
        rid = create_run(self.pool, self.owner_id, question, MODEL)
        InlineStarter(self.runner(script, tools, parts, one_piece, intro, start_intro)).start(rid)
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
    assert v.lines[-5:-3] == ["Researcher: selected 2 studies (1 RCT, 1 meta-analysis)",
                              "Search stopped at the round cap after 8 rounds"]  # Incisive's
    assert v.lines[-1] == "Answer ready"  # F3a: the run now writes the answer (Task 7)
    assert [s.kind for s in v.steps][-5:-3] == ["search", "note"]
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
    # The Introduction (F3c), 8 turns and the list (9), then the writer's four parts (F3a)
    assert n == 14


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
    g = RunGemini([END, EMPTY])  # F3c: the Introduction, on its own thread, by its route
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
    assert len(g.intro_sent) == 1 and v.intro is not None  # the real thread, joined


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
    assert v.searches and v.selected == [] and v.lines[-1] == "Answer ready"
    assert "Researcher: the study list came back empty; selecting again" in v.lines


def test_final_failed_has_no_stop_line(run_app):
    v = run_app.view(run_app.start(script=UNREADABLE_TWICE))
    assert v.lines[-2:] == ["Researcher: the study list could not be completed",
                            "Answer ready"]
    assert v.stop_reason == "other"


def test_wrap_up_line_and_turn_failed_line(run_app):
    v = run_app.view(run_app.start(script=TURN_FAILED_WITH_PICKS))
    # Incisive's full line (team.rs:353-359), replacing F2's short one (F3a plan Task 7).
    assert ("Research stopped early (a failed model call); writing the answer from what was "
            "found") in v.lines
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
    assert end_message(v) == HARD_STOP_MESSAGE and "Answer ready" not in v.lines
    assert v.answer is None and run_app.gemini.writer.count() == 0


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


def test_source_down_line(run_app, migrated_pool):
    from conftest import make_tools
    from lit_fakes import EPMC_HOST, EPMC_SEARCH_PATH, FakeUpstream, europepmc_routes, pubmed_routes

    routes = pubmed_routes() | europepmc_routes()
    routes[(EPMC_HOST, EPMC_SEARCH_PATH)] = httpx.Response(503)
    tools = make_tools(migrated_pool, FakeUpstream(routes))
    v = run_app.view(run_app.start(script=[turn(call("search_pubmed", Q1),
                                                call("search_europepmc", Q2)), END, LIST_OK],
                                   tools=tools))
    # F3a: the source-down line now follows the stop line and the stored list, before the
    # records fetch and the writer's lines.
    i = v.lines.index("Europe PMC could not be reached; searched PubMed only")
    assert v.lines[i + 1].startswith("Writer: writing the answer from ")
    assert v.lines[-1] == "Answer ready"
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
# Gemini answers 503 twice per call: 3 attempts a call, so the 8 turns use 24 attempts and the
# study-list call hits the cap of 26 on its second.
BUSY_RUN = (BUSY + [SEARCH]) * 8 + BUSY + [LIST_OK]
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
    assert calls_of(run_app.pool, rid) == RUN_ATTEMPTS == 26
    # F3c: the Introduction's attempt is one of the 26; the researcher sent the other 25.
    assert len(run_app.gemini.sent) == RUN_ATTEMPTS - 1 and len(run_app.gemini.intro_sent) == 1
    assert len(run_app.gemini.script) == 2  # nothing sent after the cap
    assert end_message(v) is None  # the page words it from the kind
    assert f"run kind=finished run_id={rid} status=failed error_kind=run_call_cap" in [
        r.getMessage() for r in caplog.records]


def test_the_runs_attempt_count_is_forgotten_when_the_run_ends(run_app, monkeypatch):
    # The count is kept while the run goes (the cap holds: 26 attempts, none after) and is
    # gone at its end, whether the run is done, capped or failed inside.
    import pubmedgemini.runner as runner_mod

    held = []
    real = ModelCaller.forget

    def spy(self, run_id):
        held.append(dict(self._sent))  # what the map held at the moment of the call
        real(self, run_id)

    monkeypatch.setattr(ModelCaller, "forget", spy)
    capped = run_app.start(script=BUSY_RUN)
    assert held == [{capped: RUN_ATTEMPTS}] and run_app.model_caller._sent == {}
    assert calls_of(run_app.pool, capped) == RUN_ATTEMPTS

    held.clear()
    done = run_app.start(script=ELEVEN)
    # the Introduction (F3c), 11 and the 4 parts
    assert held == [{done: 16}] and run_app.model_caller._sent == {}

    monkeypatch.setattr(runner_mod, "research", lambda *a, **k: 1 / 0)
    run_app.start(script=[])
    assert run_app.model_caller._sent == {}


def test_the_researchers_most_calls_and_the_writer_are_unaffected(run_app):
    # F3a: the researcher's eleven calls, then the writer's four parts: 15; with the
    # Introduction (F3c) 16.
    rid = run_app.start(script=ELEVEN)
    v = run_app.view(rid)
    assert (v.status, v.error_kind, v.stop_reason) == ("done", None, "turn_cap")
    assert [p.pmid for p in v.selected] == [ID2, ID1]
    assert "Researcher: the study list came back empty; selecting again" in v.lines
    assert calls_of(run_app.pool, rid) == 16 and run_app.gemini.script == []
    assert run_app.gemini.writer.count() == 4


def test_sixteen_calls_with_ten_retries_still_finish(run_app):
    # Exactly the cap: the Introduction (F3c), the researcher's eleven calls, 10 transport
    # retries spread over them (two before each of the first five calls), and the writer's
    # four. With 11 retries the cap is reached: test_the_intro_is_one_of_the_26_and_the_cap_holds.
    script = []
    for n, item in zip([2] * 5 + [0] * 6, ELEVEN, strict=True):
        script += [BUSY[0]] * n + [item]
    rid = run_app.start(script=script)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("done", None) and v.answer is not None
    assert calls_of(run_app.pool, rid) == RUN_ATTEMPTS


def test_the_s3_retry_counts_toward_the_cap(run_app):
    # The Introduction (F3c), 20 attempts in 7 turns, the list call (2) and the nudge (2):
    # S3's first attempt is the 26th, its retry would be the 27th.
    script = (BUSY + [SEARCH]) * 6 + [BUSY[0], END] + [BUSY[0], reply([{"text": "no"}]),
                                                      BUSY[0], EMPTY, BUSY[0], LIST_OK]
    rid = run_app.start(script=script)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("failed", "run_call_cap")
    assert calls_of(run_app.pool, rid) == RUN_ATTEMPTS and run_app.gemini.script == [LIST_OK]


def test_the_nudge_counts_toward_the_cap(run_app):
    # The Introduction (F3c), 20 attempts in 7 turns, three for the list call: the nudge's
    # first attempt is the 25th and its second the 26th; its third would be the 27th.
    script = (BUSY + [SEARCH]) * 6 + [BUSY[0], END] + BUSY + [reply([{"text": "no"}])] + BUSY + [
        LIST_OK]
    rid = run_app.start(script=script)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("failed", "run_call_cap")
    assert calls_of(run_app.pool, rid) == RUN_ATTEMPTS and run_app.gemini.script == [LIST_OK]


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
    assert finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, BODY) is True
    assert rows(run_app.pool, "SELECT version, body FROM answers WHERE run_id = %s", (rid,)) \
        == [(3, BODY)]
    v = run_app.view(rid)
    assert (v.status, v.stop_reason, v.rounds, v.answer) == ("done", "end_turn", 9, BODY)
    assert rows(run_app.pool, "SELECT finished_at IS NOT NULL FROM runs WHERE id = %s",
                (rid,)) == [(True,)]


DIRTY = "a\x00b\u202ec\u200bd"  # NUL, a bidi control, a zero-width space


def test_finish_with_answer_stores_the_body_cleaned(run_app):
    """NUL, bidi controls and zero-width characters are cleaned from every string of the
    answer (keys too) before the INSERT: no UntranslatableCharacter, nothing for Postgres to
    write into its server log."""
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    body = {"summary": {"text": DIRTY, "k\x00\u200b": [DIRTY, 1, None]}, "references": []}
    assert finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, body) is True
    assert rows(run_app.pool, "SELECT body FROM answers WHERE run_id = %s", (rid,)) == [
        ({"summary": {"text": "abcd", "k": ["abcd", 1, None]}, "references": []},)]
    assert body["summary"]["text"] == DIRTY  # the caller's dict is not changed
    assert run_app.view(rid).status == "done"


def test_get_run_has_no_answer_when_none_was_stored(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    assert run_app.view(rid).answer is None


# A body Postgres refuses: NaN is not JSON to Postgres (a NUL no longer is one: it is cleaned
# before the INSERT).
REFUSED = {"summary": float("nan")}


def test_answer_and_status_are_stored_together(run_app):
    """A body that cannot be stored (NaN in the JSON): no answer row and the status stays
    running, in one rollback."""
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    with pytest.raises(psycopg.Error):
        finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, REFUSED)
    assert rows(run_app.pool, "SELECT count(*) FROM answers") == [(0,)]
    assert rows(run_app.pool, "SELECT status, finished_at FROM runs WHERE id = %s",
                (rid,)) == [("running", None)]


def test_a_late_answer_never_overwrites_a_stopped_run(run_app, caplog):
    caplog.set_level(logging.INFO)
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    stop_unfinished(run_app.pool)
    assert finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, BODY) is False
    assert rows(run_app.pool, "SELECT count(*) FROM answers") == [(0,)]
    assert rows(run_app.pool, "SELECT status FROM runs WHERE id = %s", (rid,)) == [("stopped",)]
    assert f"run kind=finish_not_applied run_id={rid}" in [
        r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("status", ["queued", "running", "interrupted"])
def test_finish_with_answer_refuses_a_non_final_status(run_app, status):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    with pytest.raises(ValueError):
        finish_with_answer(run_app.pool, rid, status, None, "end_turn", 9, BODY)
    assert rows(run_app.pool, "SELECT count(*) FROM answers") == [(0,)]
    assert rows(run_app.pool, "SELECT status, finished_at FROM runs WHERE id = %s",
                (rid,)) == [("running", None)]


@pytest.mark.parametrize("status", ["done", "partial", "failed", "stopped"])
def test_finish_with_answer_accepts_each_final_status(run_app, status):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    assert finish_with_answer(run_app.pool, rid, status, None, None, None, BODY) is True


# ---------- F3a Task 7: the run writes, checks and stores the answer ----------
# Every step line below is typed from Incisive's source (`agent/team.rs`, commit 12630f7) at
# the line given; none is built from the code under test.

WRITER_LINE_2 = "Writer: writing the answer from 2 studies (2 with an abstract)"  # t.rs:536-544
CITATIONS_OK = "Citation check: every citation is one of the selected studies"  # t.rs:653
READY = "Answer ready"  # t.rs:230
NO_TIME = "Not enough time was left to write the answer"  # t.rs:519-524
CUT_OFF = "The writer's answer was cut off by its output limit"  # t.rs:761-766
NOT_READ = "The writer's answer could not be read"  # t.rs:770
GAP_1 = "1 selected study could not be fetched from PubMed and was left out"  # t.rs:428-447
GAP_2 = "2 selected studies could not be fetched from PubMed and were left out"
EARLY = ("Research stopped early (a failed model call); writing the answer from what was "
         "found")  # t.rs:353-359
HARD_STOP_AT = 1000.0 + 420  # LockedClock starts at 1000; Timing.quick: 360 + 60 s

LIST3 = list_reply(
    selected=[{"pmid": ID2, "design": "SR_MA", "why": "The newest meta-analysis."},
              {"pmid": ID1, "design": "RCT", "why": "A large trial."},
              {"pmid": ID3, "design": "other", "why": "A cohort."}],
    contradictions=[{"pmid": ID3, "note": "Found more pain."}])
RUN3 = HAPPY_RUN[:-1] + [LIST3]


def answer_of(v):
    from pubmedgemini.writer.answer import read_body

    assert v.answer is not None, "no answer row"
    return read_body(v.answer)


def answers(pool):
    return rows(pool, "SELECT count(*) FROM answers")[0][0]


def tools_with(pool, upstream, fetch=None):
    """LitTools over the fixtures, with `fetch(real, pmids)` in place of fetch_papers."""
    from conftest import make_tools

    tools = make_tools(pool, upstream)
    if fetch is not None:
        real = tools.fetch_papers
        tools.fetch_papers = lambda pmids: fetch(real, pmids)
    return tools


def test_a_full_run_writes_and_stores_a_valid_answer(run_app):
    from pubmedgemini.writer.answer import EvidenceType

    rid = run_app.start()
    v = run_app.view(rid)
    assert (v.status, v.error_kind, v.stop_reason, v.rounds) == ("done", None, "turn_cap", 8)
    assert v.lines[-4:] == ["Search stopped at the round cap after 8 rounds", WRITER_LINE_2,
                            CITATIONS_OK, READY]
    assert [s.kind for s in v.steps][-3:] == ["write", "check", "present"]
    a = answer_of(v)  # re-validates as AnswerV3
    assert a.answer is not None and a.empty_reason is None
    assert a.question == QUESTION and a.mode == "quick" and a.version == 3
    assert [(r.n, r.pmid) for r in a.references] == [(1, ID2), (2, ID1)]  # selection order
    assert [r.title for r in a.references] == [s.paper.title for s in v.selected]
    assert a.searches == v.searches and a.stopped_early is None
    assert v.answer["integrity"] == {  # serde's form: numbers_completed 0 is left out
        "dropped_unseen": 0, "dropped_unfetched": 0, "removed_citations": 0,
        "dropped_numbers": 0, "dropped_studies": 0, "numbers_checked": 0,
        "numbers_untraced": 0, "numbers_unverified": 0, "outcomes_unverified": 0,
        "labels_defaulted": 0}
    assert a.notes == []  # merged, and the merge checks find nothing
    # the evidence label from the cited studies' designs: SR_MA and RCT
    assert a.answer.outcomes[0].evidence_type == EvidenceType.RCTS_AND_META_ANALYSES
    assert a.answer.bottom_line.text == f"w no clear benefit [{ID2}; {ID1}]."
    assert a.to_body() == v.answer and a.elapsed_s >= 0 and a.cost_paise == 0
    assert run_app.gemini.writer.count() == 4 and run_app.gemini.writer.count("one_piece") == 0


def test_a_normal_run_makes_fourteen_model_calls(run_app):  # F3c: with the Introduction
    rid = run_app.start()
    steps = rows(run_app.pool, "SELECT step, count(*) FROM model_calls WHERE run_id = %s "
                               "GROUP BY step ORDER BY step", (rid,))
    assert steps == [("intro", 1), ("researcher", 8), ("study_list", 1), ("writer_context", 1),
                     ("writer_outcomes", 1), ("writer_summary", 1), ("writer_trials", 1)]
    assert calls_of(run_app.pool, rid) == 14


def test_the_writer_is_given_the_fetched_records_and_the_notes(run_app):
    rid = run_app.start(script=RUN3)
    v = run_app.view(rid)
    user = run_app.gemini.writer.bodies("trials")[0]["contents"][0]["parts"][0]["text"]
    assert (user.index(f"Study 1 - PMID {ID2}") < user.index(f"Study 2 - PMID {ID1}")
            < user.index(f"Study 3 - PMID {ID3}"))
    assert "Researcher's reason for selecting it:" in user and "A large trial." in user
    assert v.selected[0].paper.abstract[:60] in user  # the fetched record's abstract
    assert f"- [{ID3}] Found more pain." in user  # the researcher's contradiction note
    assert "- PubMed: hernia[tiab] (5160 hits)" in user  # the search log
    assert QUESTION in user


def test_citation_check_uses_fetched_studies_only(run_app, migrated_pool, upstream):
    # ID1 is selected and cited by the writer, but its record is not fetched: every citation
    # of it is removed and counted, its number and its trial row dropped (Q8: as Incisive).
    def without_id1(real, pmids):
        return {k: p for k, p in real(pmids).items() if k != ID1}

    v = run_app.view(run_app.start(tools=tools_with(migrated_pool, upstream, without_id1)))
    a = answer_of(v)
    assert v.status == "done" and [p.pmid for p in v.selected] == [ID2, ID1]
    assert GAP_1 in v.lines
    assert "Writer: writing the answer from 1 study (1 with an abstract)" in v.lines
    assert [r.pmid for r in a.references] == [ID2]
    i = a.integrity
    assert i.dropped_unfetched == 1 and i.dropped_numbers == 1 and i.dropped_studies == 1
    # ID1 is cited in the bottom line, the outcome's evidence and effect texts and its pmids,
    # the second trial row's results, the key number and the notable finding (text and pmids).
    assert i.removed_citations == 8
    # t.rs:656-668, singular "number" and "trial row"
    assert v.lines[-2] == ("Citation check: removed 8 unknown citations, dropped 1 number and 1 "
                           "trial row")
    assert ID1 not in json.dumps(a.answer.model_dump()) and ID2 in a.answer.bottom_line.text
    assert [s.pmid for s in a.answer.studies] == [ID2]


def test_two_missing_records_are_counted_in_the_plural(run_app, migrated_pool, upstream):
    def only_id3(real, pmids):
        return {k: p for k, p in real(pmids).items() if k == ID3}

    v = run_app.view(run_app.start(script=RUN3,
                                   tools=tools_with(migrated_pool, upstream, only_id3)))
    assert GAP_2 in v.lines and v.steps[v.lines.index(GAP_2)].kind == "note"
    assert "Writer: writing the answer from 1 study (1 with an abstract)" in v.lines
    a = answer_of(v)
    assert a.integrity.dropped_unfetched == 2 and [r.pmid for r in a.references] == [ID3]


def test_no_record_fetched_ends_partial_unfetched(run_app, migrated_pool, upstream):
    from pubmedgemini.runs import UNFETCHED

    v = run_app.view(run_app.start(tools=tools_with(migrated_pool, upstream,
                                                    lambda real, pmids: {})))
    assert (v.status, v.error_kind) == ("partial", None)
    assert v.lines[-2:] == [GAP_2, READY]
    a = answer_of(v)
    assert a.answer is None and a.empty_reason == UNFETCHED == (
        "The selected studies' records could not be fetched, so no answer could be written.")
    assert a.references == [] and a.integrity.dropped_unfetched == 2
    assert run_app.gemini.writer.count() == 0


def test_fetch_failure_fails_the_run_lit(run_app, migrated_pool, upstream):
    def fail(real, pmids):
        raise ToolFailed("the database could not be reached; try again later or use another "
                         "tool", "network")

    v = run_app.view(run_app.start(tools=tools_with(migrated_pool, upstream, fail)))
    assert (v.status, v.error_kind) == ("failed", "lit") and v.finished_at is not None
    assert v.answer is None and answers(run_app.pool) == 0
    assert [p.pmid for p in v.selected] == [ID2, ID1]  # the study list is still shown
    assert all(p.paper is None for p in v.selected) and v.selected[0].title  # run_seen's
    assert READY not in v.lines and run_app.gemini.writer.count() == 0


@pytest.mark.parametrize("left,writes", [(149.0, False), (150.0, True)])
def test_no_time_for_the_writer_ends_partial_with_references(run_app, migrated_pool, upstream,
                                                             left, writes):
    from pubmedgemini.runs import WRITER_NO_TIME

    def late(real, pmids):
        out = real(pmids)
        run_app.clock.t = HARD_STOP_AT - left
        return out

    v = run_app.view(run_app.start(tools=tools_with(migrated_pool, upstream, late)))
    a = answer_of(v)
    assert [r.pmid for r in a.references] == [ID2, ID1]
    if writes:
        assert v.status == "done" and a.answer is not None
        assert NO_TIME not in v.lines and run_app.gemini.writer.count() == 4
        return
    assert (v.status, v.error_kind) == ("partial", "writer_time")
    assert v.lines[-2:] == [NO_TIME, READY] and v.steps[-2].kind == "note"
    assert a.answer is None and a.empty_reason == WRITER_NO_TIME == (
        "Not enough of the time limit was left to write the answer. The studies found are "
        "listed below; please ask again.")
    assert run_app.gemini.writer.count() == 0


def test_cap_in_the_writer_fails_the_run_and_keeps_the_study_list(run_app):
    # The Introduction (F3c), then 22 attempts in research: six turns after two 503s each
    # (18), one after one 503 (2), the eighth and the list: 3 left for the writer's four parts.
    script = (BUSY + [SEARCH]) * 6 + [BUSY[0], SEARCH] + [SEARCH, LIST_OK]
    rid = run_app.start(script=script)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("failed", "run_call_cap")
    assert calls_of(run_app.pool, rid) == RUN_ATTEMPTS and run_app.gemini.writer.count() == 3
    assert WRITER_LINE_2 in v.lines and READY not in v.lines
    assert v.answer is None and answers(run_app.pool) == 0
    assert [p.pmid for p in v.selected] == [ID2, ID1] and v.selected[0].paper is not None


def _parts(**over):
    from writer_fakes import good_parts

    parts = good_parts()
    parts.update(over)
    return parts


def test_writer_failure_stores_no_written_answer(run_app):
    from pubmedgemini.runs import WRITER_TRUNCATED

    cut = reply([{"text": '{"studies": [{"pmid": "'}], finish="MAX_TOKENS")

    def gate_passed(real, pmids):
        out = real(pmids)
        run_app.clock.t = HARD_STOP_AT - 155  # the writer starts (150 s or more left)
        return out

    def cut_late(request):
        run_app.clock.t += 10  # 145 s left once the parts are in: no time for the one piece
        return httpx.Response(200, json=cut)

    v = run_app.view(run_app.start(parts=_parts(trials=[cut, cut_late]),
                                   tools=tools_with(run_app.pool, run_app.upstream,
                                                    gate_passed)))
    assert (v.status, v.error_kind) == ("partial", "writer_truncated")
    assert v.lines[-2:] == [CUT_OFF, READY] and v.steps[-2].kind == "note"
    a = answer_of(v)
    assert a.answer is None and a.empty_reason == WRITER_TRUNCATED == (
        "The answer was longer than the writer's output limit and could not be finished. The "
        "studies found are listed below; please ask again.")
    assert [r.pmid for r in a.references] == [ID2, ID1]
    assert run_app.gemini.writer.count("one_piece") == 0
    assert not any(line.startswith("Citation check") for line in v.lines)


def test_the_one_piece_failing_too_ends_partial_unreadable(run_app):
    from pubmedgemini.runs import WRITER_UNREADABLE

    bad = reply([{"text": "not json"}])
    v = run_app.view(run_app.start(parts=_parts(trials=[bad, bad]), one_piece=[bad, bad]))
    assert (v.status, v.error_kind) == ("partial", "writer_unreadable")
    assert "Writer: section trials failed; writing the answer in one piece" in v.lines
    assert v.lines[-2:] == [NOT_READ, READY]
    a = answer_of(v)
    assert a.answer is None and a.empty_reason == WRITER_UNREADABLE == (
        "The writer's answer could not be read. The studies found are listed below; please "
        "ask again.")
    assert run_app.gemini.writer.count("one_piece") == 2


def test_the_one_piece_answer_is_stored_and_gets_no_merge_notes(run_app):
    from writer_fakes import piece_reply, whole_answer

    high = whole_answer("p")
    high["bottom_line"]["certainty"] = "High"  # above every outcome's: a merge note if merged
    bad = reply([{"text": "not json"}])
    v = run_app.view(run_app.start(parts=_parts(trials=[bad, bad]),
                                   one_piece=[piece_reply(high)]))
    assert v.status == "done" and v.lines[-2:] == [CITATIONS_OK, READY]
    a = answer_of(v)
    assert a.answer.bottom_line.text.startswith("p ") and a.notes == []


def test_a_merged_answer_gets_the_merge_notes(run_app):
    from writer_fakes import good_parts, whole_answer

    high = whole_answer("m")
    high["bottom_line"]["certainty"] = "High"
    v = run_app.view(run_app.start(parts=good_parts(high)))
    a = answer_of(v)
    assert v.status == "done" and len(a.notes) == 1
    assert a.notes[0].startswith("Consistency check (answer written in four parts): the "
                                 "bottom line's certainty (High)")


@pytest.mark.parametrize("err,kind", [
    (error(429, "RESOURCE_EXHAUSTED",
           [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]),
     "daily_limit"),
    (error(401, "X"), "key_refused"),
    (error(402, "X"), "billing_refused"),
])
def test_a_fatal_error_in_the_writer_fails_the_run(run_app, err, kind):
    v = run_app.view(run_app.start(parts=_parts(outcomes=[err])))
    assert (v.status, v.error_kind) == ("failed", kind)
    assert v.answer is None and answers(run_app.pool) == 0 and READY not in v.lines
    assert run_app.gemini.writer.count("one_piece") == 0  # Q2: no one piece
    assert [p.pmid for p in v.selected] == [ID2, ID1]


def test_a_failed_writer_call_fails_the_run_with_its_kind(run_app):
    # `WriteFailure::Step(e)` (t.rs:787): a call that failed (not cut off, not unreadable),
    # here Google refusing the request in a part and in the one piece.
    refused = error(400, "INVALID_ARGUMENT")
    v = run_app.view(run_app.start(parts=_parts(trials=[refused]), one_piece=[refused]))
    assert (v.status, v.error_kind) == ("failed", "request_refused")
    assert v.answer is None and answers(run_app.pool) == 0 and READY not in v.lines


def test_the_hard_stop_in_the_writer_fails_the_run(run_app):
    from writer_fakes import part_reply, whole_answer

    def at_hard_stop(request):
        run_app.clock.t = HARD_STOP_AT
        return httpx.Response(200, json=part_reply(Section.OUTCOMES, whole_answer()))

    v = run_app.view(run_app.start(parts=_parts(outcomes=[at_hard_stop])))
    assert (v.status, v.error_kind) == ("failed", "hard_stop")
    assert end_message(v) == HARD_STOP_MESSAGE
    assert v.answer is None and answers(run_app.pool) == 0 and READY not in v.lines


def test_a_restart_while_the_writer_works_keeps_the_run_stopped(run_app, caplog):
    from writer_fakes import part_reply, whole_answer

    def restarted(request):
        stop_unfinished(run_app.pool)  # a new process marked the run stopped
        return httpx.Response(200, json=part_reply(Section.OUTCOMES, whole_answer()))

    caplog.set_level(logging.INFO)
    rid = run_app.start(parts=_parts(outcomes=[restarted]))
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("stopped", "restarted")
    assert v.answer is None and answers(run_app.pool) == 0 and READY not in v.lines
    msgs = [r.getMessage() for r in caplog.records]
    assert f"run kind=finish_not_applied run_id={rid}" in msgs
    assert not any(m.startswith(f"run kind=finished run_id={rid}") for m in msgs)


def test_the_database_refusing_the_answer_fails_the_run(run_app, monkeypatch, caplog):
    from pubmedgemini.writer.answer import AnswerV3

    real = AnswerV3.to_body

    def refused(self):
        body = real(self)
        body["question"] = "zq7 private"
        body["elapsed_s"] = float("nan")  # Postgres refuses NaN in JSON
        return body

    monkeypatch.setattr(AnswerV3, "to_body", refused)
    caplog.set_level(logging.DEBUG)
    rid = run_app.start()
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("failed", "internal") and v.finished_at is not None
    assert v.answer is None and answers(run_app.pool) == 0
    assert READY not in v.lines  # stored with the answer, so rolled back with it
    msgs = [r.getMessage() for r in caplog.records]
    assert "run kind=answer_not_stored error=InvalidTextRepresentation" in msgs
    assert f"run kind=finished run_id={rid} status=failed error_kind=internal" in msgs
    assert "zq7" not in caplog.text and "u0000" not in caplog.text


def test_an_unexpected_error_after_research_fails_the_run_kind_only(run_app, monkeypatch,
                                                                    caplog):
    import pubmedgemini.runner as runner_mod

    def boom(*a, **k):
        raise KeyError("zq7 private model text")

    monkeypatch.setattr(runner_mod, "check_citations", boom)
    caplog.set_level(logging.DEBUG)
    v = run_app.view(run_app.start())
    assert (v.status, v.error_kind) == ("failed", "internal")
    assert v.answer is None and answers(run_app.pool) == 0 and READY not in v.lines
    assert "run kind=internal error=KeyError" in [r.getMessage() for r in caplog.records]
    assert "zq7" not in caplog.text and "Traceback" not in caplog.text


@pytest.mark.parametrize("script,status,kind,reason,early", [
    (EMPTY_TWICE, "partial", "empty_selection", EMPTY_SELECTION, None),
    (UNREADABLE_TWICE, "partial", "researcher_incomplete", STOPPED_NO_ANSWER, None),
    (NOTHING_SELECTED, "done", None, NO_EVIDENCE, None),
    (TURN_FAILED_NOTHING, "partial", None, STOPPED_NO_ANSWER, "a failed model call"),
])
def test_the_ends_before_fetching_store_an_answer(run_app, script, status, kind, reason, early):
    v = run_app.view(run_app.start(script=script))
    assert (v.status, v.error_kind) == (status, kind)
    assert v.lines[-1] == READY and v.steps[-1].kind == "present"
    a = answer_of(v)
    assert a.answer is None and a.empty_reason == reason and a.references == []
    assert a.stopped_early == early and a.searches == v.searches
    assert run_app.gemini.writer.count() == 0


def test_wrap_up_with_nothing_selected_is_partial_with_the_time_limit(run_app, monkeypatch):
    import pubmedgemini.runner as runner_mod
    from pubmedgemini.researcher.loop import Research

    def wrapped(question, **k):
        v = Validated((), (Considered(ID1, "Off topic."),), (), 0, 0, 0)
        return Research(v, (), frozenset({ID1}), "wrap_up", 3, False, False, ())

    monkeypatch.setattr(runner_mod, "research", wrapped)
    v = run_app.view(run_app.start(script=[]))
    assert (v.status, v.error_kind) == ("partial", None)
    assert v.lines == ["Search stopped at the Quick limit after 3 rounds", READY]  # t.rs:349
    a = answer_of(v)
    assert a.empty_reason == STOPPED_NO_ANSWER and a.stopped_early == "time limit"


def test_a_failed_turn_with_picks_writes_a_partial_answer(run_app):
    v = run_app.view(run_app.start(script=TURN_FAILED_WITH_PICKS))
    assert (v.status, v.error_kind) == ("partial", None)
    a = answer_of(v)
    assert a.answer is not None and a.stopped_early == "a failed model call"
    i = v.lines.index(EARLY)
    assert v.lines[i + 1:] == [WRITER_LINE_2, CITATIONS_OK, READY]
    assert v.steps[i].kind == "note"


def test_dropped_unseen_is_carried_into_the_answer(run_app):
    unseen = list_reply(selected=[{"pmid": ID2, "design": "SR_MA", "why": "w"},
                                  {"pmid": "99999999", "design": "RCT", "why": "x"}])
    v = run_app.view(run_app.start(script=HAPPY_RUN[:-1] + [unseen]))
    assert answer_of(v).integrity.dropped_unseen == 1 and v.status == "done"


# ---------- nothing private reaches a log line, on any ending ----------

Q_SECRET = "zq7xanthelasma"
M_SECRET = "mq1fundoplication"


def _secret_parts():
    from writer_fakes import good_parts, whole_answer

    return good_parts(whole_answer(M_SECRET))


def _leak_cases(run_app, migrated_pool, upstream):
    from writer_fakes import piece_reply, whole_answer

    bad = reply([{"text": f"{M_SECRET} not json"}])
    cut = reply([{"text": '{"studies": [{"pmid": "' + M_SECRET}], finish="MAX_TOKENS")

    def late(real, pmids):
        out = real(pmids)
        run_app.clock.t = HARD_STOP_AT - 10
        return out

    def fail(real, pmids):
        raise ToolFailed(f"{M_SECRET} unreachable", "network")

    secret_err = error(402, "X", message=f"{M_SECRET} billing")
    return {
        "written": {"parts": _secret_parts()},
        "one_piece": {"parts": _parts(trials=[bad, bad]),
                          "one_piece": [piece_reply(whole_answer(M_SECRET))]},
        "unreadable": {"parts": _parts(trials=[bad, bad]), "one_piece": [bad, bad]},
        "truncated": {"parts": _parts(trials=[cut, cut]), "one_piece": [cut, cut]},
        "no_time": {"tools": tools_with(migrated_pool, upstream, late)},
        "lit": {"tools": tools_with(migrated_pool, upstream, fail)},
        "fatal": {"parts": _parts(outcomes=[secret_err])},
    }


LEAK_ENDINGS = {
    "written": ("done", None), "one_piece": ("done", None),
    "unreadable": ("partial", "writer_unreadable"), "truncated": ("partial", "writer_truncated"),
    "no_time": ("partial", "writer_time"), "lit": ("failed", "lit"),
    "fatal": ("failed", "billing_refused"),
}


@pytest.mark.parametrize("case", list(LEAK_ENDINGS))
def test_nothing_private_is_logged_on_any_ending(run_app, migrated_pool, upstream, caplog,
                                                 case):
    from test_logging import assert_app_lines_fixed

    kw = _leak_cases(run_app, migrated_pool, upstream)[case]
    caplog.set_level(logging.DEBUG)
    rid = run_app.start(question=f"{QUESTION} {Q_SECRET}", **kw)
    v = run_app.view(rid)
    assert v.finished_at is not None and (v.status, v.error_kind) == LEAK_ENDINGS[case]
    if v.answer is not None:  # the private texts really were in the run
        assert Q_SECRET in json.dumps(v.answer)
    if case in ("written", "one_piece"):
        assert M_SECRET in json.dumps(v.answer)
    records = [s.paper for s in v.selected if s.paper is not None]
    private = [Q_SECRET, M_SECRET, "laparoscopic", "Found more pain", "newest meta-analysis"]
    private += [p.title[:40] for p in records] + [p.abstract[:40] for p in records if p.abstract]
    for text in private:
        assert text not in caplog.text, text
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("pubmedgemini")]
    assert_app_lines_fixed(msgs)
    assert sum(m.startswith(f"run kind=finished run_id={rid} ") for m in msgs) == 1


# ---------- F3c Task 1: the Introduction's row and the early summary's row ----------

EARLY_BODY = {"summary": True, "context": False, "answer": {"made": "up"}}


def running_run(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    set_running(run_app.pool, rid)
    return rid


def test_intro_row_moves_pending_to_done_once(run_app):
    rid = running_run(run_app)
    assert run_app.view(rid).intro is None  # no row
    assert intro_start(run_app.pool, rid) is True
    assert intro_start(run_app.pool, rid) is False  # one row a run
    assert run_app.view(rid).intro is None  # pending shows nothing
    assert intro_finish(run_app.pool, rid, "done", None, "Example paragraph.") is True
    assert intro_finish(run_app.pool, rid, "failed", "api", None) is False  # only once
    assert run_app.view(rid).intro == "Example paragraph."
    assert rows(run_app.pool, "SELECT status, reason, finished_at IS NOT NULL FROM run_intros"
                ) == [("done", None, True)]


def test_intro_start_for_a_missing_run_is_false(run_app):
    assert intro_start(run_app.pool, uuid.uuid4()) is False
    assert rows(run_app.pool, "SELECT count(*) FROM run_intros") == [(0,)]


def test_failed_intro_shows_nothing(run_app):
    rid = running_run(run_app)
    intro_start(run_app.pool, rid)
    assert intro_finish(run_app.pool, rid, "failed", "api", "ignored text") is True
    assert run_app.view(rid).intro is None
    assert rows(run_app.pool, "SELECT status, reason, text FROM run_intros") == [
        ("failed", "api", None)]  # a failed row never keeps text


def test_intro_text_is_stored_cleaned(run_app):
    rid = running_run(run_app)
    intro_start(run_app.pool, rid)
    intro_finish(run_app.pool, rid, "done", None, "A\x00b\u202ec\u200bd")
    assert run_app.view(rid).intro == "Abcd"


def test_intro_text_with_every_hidden_character_is_stored_cleaned(run_app):
    rid = running_run(run_app)
    intro_start(run_app.pool, rid)
    assert intro_finish(run_app.pool, rid, "done", None, DIRTY) is True
    assert rows(run_app.pool, "SELECT text FROM run_intros") == [("abcd",)]
    assert run_app.view(rid).status == "running"


def test_store_early_stores_the_body_cleaned(run_app):
    rid = running_run(run_app)
    body = {"summary": True, "context": False, "answer": {"text": DIRTY, "list": [DIRTY]}}
    assert store_early(run_app.pool, rid, body) is True  # no UntranslatableCharacter
    assert rows(run_app.pool, "SELECT body FROM run_early") == [
        ({"summary": True, "context": False, "answer": {"text": "abcd", "list": ["abcd"]}},)]
    assert body["answer"]["text"] == DIRTY
    assert run_app.view(rid).status == "running"


def test_store_early_only_while_running(run_app):
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    assert store_early(run_app.pool, rid, EARLY_BODY) is False  # queued: not yet
    set_running(run_app.pool, rid)
    assert store_early(run_app.pool, rid, EARLY_BODY) is True
    assert run_app.view(rid).early == EARLY_BODY
    assert store_early(run_app.pool, rid, EARLY_BODY | {"context": True}) is True  # an upsert
    assert run_app.view(rid).early["context"] is True
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(1,)]
    assert finish_run(run_app.pool, rid, "done", None, None, None) is True
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]  # cleared with it
    assert store_early(run_app.pool, rid, EARLY_BODY) is False
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]
    assert run_app.view(rid).early is None


@pytest.mark.parametrize("status", ["partial", "failed", "stopped"])
def test_finish_run_clears_the_early_row_for_every_end(run_app, status):
    rid = running_run(run_app)
    store_early(run_app.pool, rid, EARLY_BODY)
    finish_run(run_app.pool, rid, status, None, None, None)
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]


def test_store_early_refused_once_an_answer_is_stored(run_app):
    rid = running_run(run_app)
    with run_app.pool.connection() as conn:  # an answer row while still running (by hand)
        conn.execute("INSERT INTO answers (run_id, version, body) VALUES (%s, 3, '{}')", (rid,))
    assert store_early(run_app.pool, rid, EARLY_BODY) is False
    assert run_app.view(rid).early is None


def test_finish_clears_the_early_row_in_the_same_transaction(run_app):
    rid = running_run(run_app)
    store_early(run_app.pool, rid, EARLY_BODY)
    # A body the database refuses: the answer, the status and the early row all roll back.
    with pytest.raises(psycopg.Error):
        finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, REFUSED)
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(1,)]
    assert run_app.view(rid).status == "running"
    assert finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, BODY) is True
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]
    assert run_app.view(rid).answer == BODY and run_app.view(rid).early is None


def test_clear_early_deletes_the_row(run_app):
    rid = running_run(run_app)
    store_early(run_app.pool, rid, EARLY_BODY)
    clear_early(run_app.pool, rid)
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]
    clear_early(run_app.pool, rid)  # nothing to delete: no error


def test_a_late_early_row_is_never_shown_for_a_finished_run(run_app):
    rid = running_run(run_app)
    finish_run(run_app.pool, rid, "done", None, None, None)
    with run_app.pool.connection() as conn:  # left over (inserted by hand)
        conn.execute("INSERT INTO run_early (run_id, body) VALUES (%s, %s)",
                     (rid, json.dumps(EARLY_BODY)))
    assert run_app.view(rid).early is None


def test_start_up_clears_early_and_fails_pending_intros(run_app):
    a, b = running_run(run_app), None
    intro_start(run_app.pool, a)
    store_early(run_app.pool, a, EARLY_BODY)
    finish_run(run_app.pool, a, "done", None, None, None)
    b = running_run(run_app)
    intro_start(run_app.pool, b)
    intro_finish(run_app.pool, b, "done", None, "Kept.")
    with run_app.pool.connection() as conn:
        conn.execute("INSERT INTO run_early (run_id, body) VALUES (%s, %s)",
                     (b, json.dumps(EARLY_BODY)))
    assert stop_unfinished(run_app.pool) == 1
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]
    assert dict(rows(run_app.pool, "SELECT run_id, status || '/' || coalesce(reason, '') "
                                   "FROM run_intros")) == {a: "failed/stale", b: "done/"}
    assert run_app.view(b).intro == "Kept." and run_app.view(b).early is None


# ---------- F4a Task 1: the completeness check's row (migration 0007) ----------


def check_rows(pool):
    return {r[0]: r[1:] for r in rows(
        pool, "SELECT run_id, status, reason, numbers_found, numbers_total, names_match, "
              "names_total FROM run_checks")}


def test_answer_with_check_stores_a_pending_row_together(run_app):
    a = running_run(run_app)
    assert finish_with_answer(run_app.pool, a, "done", None, "end_turn", 9, BODY,
                              check=True) is True
    assert check_rows(run_app.pool) == {a: ("pending", None, None, None, None, None)}
    v = run_app.view(a)
    assert v.answer == BODY and v.check is not None
    assert (v.check.status, v.check.numbers_found, v.check.numbers_total, v.check.names_match,
            v.check.names_total) == ("pending", None, None, None, None)
    assert 0 <= v.check.age_s < 30
    b = running_run(run_app)  # the default: no check row
    assert finish_with_answer(run_app.pool, b, "done", None, "end_turn", 9, BODY) is True
    assert run_app.view(b).check is None and set(check_rows(run_app.pool)) == {a}
    c = running_run(run_app)  # a run no longer running: neither the answer nor the row
    stop_unfinished(run_app.pool)
    assert finish_with_answer(run_app.pool, c, "done", None, "end_turn", 9, BODY,
                              check=True) is False
    assert run_app.view(c).answer is None and c not in check_rows(run_app.pool)


def test_a_refused_check_row_still_stores_the_answer(run_app, caplog):
    rid = running_run(run_app)
    with run_app.pool.connection() as conn:  # the insert will hit the primary key
        conn.execute("INSERT INTO run_checks (run_id, status, reason) "
                     "VALUES (%s, 'failed', 'stale')", (rid,))
    caplog.set_level(logging.INFO)
    assert finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, BODY,
                              closing_line=("present", "Answer ready"), check=True) is True
    v = run_app.view(rid)
    assert (v.status, v.answer, v.lines[-1]) == ("done", BODY, "Answer ready")
    assert check_rows(run_app.pool) == {rid: ("failed", "stale", None, None, None, None)}
    lines = [r.getMessage() for r in caplog.records if "check_not_started" in r.getMessage()]
    assert lines == [f"run kind=check_not_started run_id={rid} error=UniqueViolation"]


def test_an_answer_the_database_refuses_leaves_no_check_row(run_app):
    rid = running_run(run_app)
    with pytest.raises(psycopg.Error):
        finish_with_answer(run_app.pool, rid, "done", None, "end_turn", 9, REFUSED, check=True)
    assert check_rows(run_app.pool) == {} and answers(run_app.pool) == 0
    assert run_app.view(rid).status == "running"


def test_check_moves_pending_running_done_once(run_app):
    rid = running_run(run_app)
    assert check_begin(run_app.pool, rid) is False  # no row yet
    finish_with_answer(run_app.pool, rid, "done", None, None, None, BODY, check=True)
    assert check_begin(run_app.pool, rid) is True
    assert rows(run_app.pool, "SELECT status, started_at IS NOT NULL, finished_at FROM "
                              "run_checks") == [("running", True, None)]
    assert check_begin(run_app.pool, rid) is False
    assert check_finish(run_app.pool, rid, counts=(16, 16, 4, 4), reason=None) is True
    assert check_finish(run_app.pool, rid, counts=(1, 2, 3, 4), reason=None) is False
    assert check_finish(run_app.pool, rid, counts=None, reason="internal") is False
    assert check_rows(run_app.pool) == {rid: ("done", None, 16, 16, 4, 4)}
    assert rows(run_app.pool, "SELECT finished_at IS NOT NULL FROM run_checks") == [(True,)]
    c = run_app.view(rid).check
    assert (c.status, c.numbers_found, c.numbers_total, c.names_match, c.names_total) == (
        "done", 16, 16, 4, 4)
    assert run_app.view(rid).answer == BODY  # untouched


def test_check_finish_failed_keeps_no_counts(run_app):
    rid = running_run(run_app)
    finish_with_answer(run_app.pool, rid, "done", None, None, None, BODY, check=True)
    assert check_finish(run_app.pool, rid, counts=None, reason="internal") is True  # from pending
    assert check_rows(run_app.pool) == {rid: ("failed", "internal", None, None, None, None)}
    assert rows(run_app.pool, "SELECT finished_at IS NOT NULL FROM run_checks") == [(True,)]
    c = run_app.view(rid).check
    assert (c.status, c.numbers_found, c.names_total) == ("failed", None, None)
    assert check_begin(run_app.pool, rid) is False


@pytest.mark.parametrize("counts,reason", [(None, None), ((1, 1, 1, 1), "internal")])
def test_check_finish_needs_counts_or_reason(run_app, counts, reason):
    rid = running_run(run_app)
    finish_with_answer(run_app.pool, rid, "done", None, None, None, BODY, check=True)
    with pytest.raises(ValueError):
        check_finish(run_app.pool, rid, counts=counts, reason=reason)
    assert check_rows(run_app.pool) == {rid: ("pending", None, None, None, None, None)}


def test_start_up_fails_unfinished_checks(run_app):
    ids = []
    for _ in range(3):
        rid = running_run(run_app)
        finish_with_answer(run_app.pool, rid, "done", None, None, None, BODY, check=True)
        ids.append(rid)
    pending, running, done = ids
    check_begin(run_app.pool, running)
    check_begin(run_app.pool, done)
    check_finish(run_app.pool, done, counts=(2, 3, 1, 2), reason=None)
    assert stop_unfinished(run_app.pool) == 0
    assert check_rows(run_app.pool) == {
        pending: ("failed", "stale", None, None, None, None),
        running: ("failed", "stale", None, None, None, None),
        done: ("done", None, 2, 3, 1, 2)}
    assert rows(run_app.pool, "SELECT count(*) FROM run_checks WHERE finished_at IS NULL") == [
        (0,)]
    assert [run_app.view(r).status for r in ids] == ["done"] * 3  # the runs are untouched


def test_a_deleted_run_takes_its_check_row(run_app):
    rid = running_run(run_app)
    finish_with_answer(run_app.pool, rid, "done", None, None, None, BODY, check=True)
    with run_app.pool.connection() as conn:
        conn.execute("DELETE FROM runs WHERE id = %s", (rid,))
    assert check_rows(run_app.pool) == {}


# ---------- F3c Task 3: the run writes the Introduction and stores the early summary ----------


def intro_row(pool, rid):
    got = rows(pool, "SELECT status, reason, text FROM run_intros WHERE run_id = %s", (rid,))
    return got[0] if got else None


def steps_of(pool, rid):
    return [s for (s,) in rows(pool, "SELECT step FROM model_calls WHERE run_id = %s "
                                     "ORDER BY id", (rid,))]


def wait_for(cond, seconds=10.0):
    import time

    end = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_full_run_has_an_introduction_and_14_attempts(run_app):
    from writer_fakes import INTRO_TEXT

    rid = run_app.start()
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("done", None) and v.answer is not None
    assert intro_row(run_app.pool, rid) == ("done", None, INTRO_TEXT)
    assert v.intro == INTRO_TEXT
    steps = steps_of(run_app.pool, rid)
    assert len(steps) == 14 and steps.count("intro") == 1 and steps[0] == "intro"
    # the model saw the question only, as data (no studies, no search results)
    (body,) = run_app.gemini.intro_sent
    assert body["contents"] == [{"role": "user", "parts": [
        {"text": f'<source id="question">\n{QUESTION}\n</source>'}]}]


def test_the_intro_runs_on_its_own_thread_and_the_run_never_waits_for_it(run_app):
    """The real starter: the Introduction's reply is held until the run has stored its answer;
    the run is done by then, and the Introduction still ends `done` (the join)."""
    from writer_fakes import intro_reply

    seen = []

    def held(request):
        wait_for(lambda: rows(run_app.pool, "SELECT count(*) FROM answers")[0][0] == 1)
        seen.append(rows(run_app.pool, "SELECT status FROM runs")[0][0])
        return httpx.Response(200, json=intro_reply())

    from pubmedgemini.intro import start_intro

    rid = run_app.start(intro=[held], start_intro=start_intro)
    assert seen == ["done"]
    assert intro_row(run_app.pool, rid)[0] == "done"
    assert run_app.model_caller._sent == {}  # forgotten only after the Introduction ended
    assert len(steps_of(run_app.pool, rid)) == 14


def _blocked():
    return {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {}}


@pytest.mark.parametrize("case,reason", [
    ("503", "api"), ("429_long_wait", "api"), ("max_tokens", "truncated"),
    ("blocked", "refused"), ("db_refuses", "store"), ("raises", "internal"),
])
def test_intro_failure_never_fails_the_run(run_app, monkeypatch, case, reason):
    from gemini_fakes import RETRY_INFO
    from writer_fakes import intro_reply

    import pubmedgemini.intro as intro_mod

    steps = {
        "503": [error(503, "UNAVAILABLE")],
        "429_long_wait": [error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("300s")])],
        "max_tokens": [intro_reply(finish="MAX_TOKENS")],
        "blocked": [_blocked()],
        "db_refuses": [intro_reply("x" * 20000)],  # over the column's CHECK
        "raises": [intro_reply()],
    }[case]
    if case == "raises":
        def boom(*a, **k):
            raise RuntimeError("made-up")
        monkeypatch.setattr(intro_mod, "_call", boom)
    rid = run_app.start(intro=steps)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("done", None) and v.answer is not None
    assert v.lines[-1] == "Answer ready" and v.intro is None
    row = intro_row(run_app.pool, rid)
    if case == "db_refuses":
        assert row == ("pending", None, None)  # refused; start-up fails it `stale`
    else:
        assert row == ("failed", reason, None)
    assert len(run_app.gemini.intro_sent) == (0 if case == "raises" else 1)
    assert 300.0 not in run_app.clock.slept  # a long asked wait is never waited


def test_a_failing_intro_starter_never_fails_the_run(run_app, caplog):
    def broken(*a, **k):
        raise RuntimeError("made-up")

    caplog.set_level(logging.INFO)
    rid = run_app.start(start_intro=broken)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("done", None) and v.answer is not None
    assert f"run kind=intro_not_started run_id={rid} error=RuntimeError" in [
        r.getMessage() for r in caplog.records]


def test_intro_cap_reached_leaves_the_run_alone(run_app):
    """The run uses all 26 attempts (the eleven calls, 11 transport retries, the four parts:
    done without an Introduction). The Introduction, held until then, is the one refused: it
    sends nothing, ends `failed` `cap`, and the run's outcome is as without it."""
    from pubmedgemini.intro import start_intro

    script = []
    for n, item in zip([2] * 5 + [1] + [0] * 5, ELEVEN, strict=True):
        script += [BUSY[0]] * n + [item]

    def late(run_id, question, *, caller, pool, clock):
        def go():
            wait_for(lambda: caller._sent.get(run_id, 0) >= RUN_ATTEMPTS)
            from pubmedgemini.intro import write_intro
            write_intro(run_id, question, caller=caller, pool=pool, clock=clock)

        t = threading.Thread(target=go, daemon=True, name="intro")
        t.start()
        return t

    assert start_intro  # the real one is the default in run_one; this test holds it back
    rid = run_app.start(script=script, start_intro=late)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("done", None) and v.answer is not None
    assert intro_row(run_app.pool, rid) == ("failed", "cap", None)
    assert run_app.gemini.intro_sent == [] and calls_of(run_app.pool, rid) == RUN_ATTEMPTS
    assert "intro" not in steps_of(run_app.pool, rid)


def test_the_intro_is_one_of_the_26_and_the_cap_holds(run_app):
    """With the Introduction first, a run of the eleven calls and the four parts has ten
    transport retries to spare (26 = 1 + 11 + 4 + 10); an eleventh retry reaches the cap."""
    def script_with(retries):
        out = []
        for n, item in zip([2] * 5 + [retries - 10] + [0] * 5, ELEVEN, strict=True):
            out += [BUSY[0]] * n + [item]
        return out

    done = run_app.start(script=script_with(10))
    assert (run_app.view(done).status, calls_of(run_app.pool, done)) == ("done", RUN_ATTEMPTS)
    capped = run_app.start(script=script_with(11))
    v = run_app.view(capped)
    assert (v.status, v.error_kind) == ("failed", "run_call_cap")
    assert calls_of(run_app.pool, capped) == RUN_ATTEMPTS
    assert steps_of(run_app.pool, capped).count("intro") == 1


def test_a_run_admitted_with_one_run_left_never_passes_the_day(run_app):
    """The free runs' arithmetic (Review Focus 4): a run is admitted only with room for
    RUN_ATTEMPTS; with the Introduction it still sends at most RUN_ATTEMPTS, so the day's
    count never passes GEMINI_RPD. 21 on a fresh day of 500 is unchanged."""
    from pubmedgemini.gemini.quota import calls_today, runs_left_for

    rpd = 500
    with run_app.pool.connection() as conn:
        conn.execute("INSERT INTO model_calls (model, step, attempt, status_kind) "
                     "SELECT %s, 'researcher', 1, 'ok' FROM generate_series(1, %s)",
                     (MODEL, rpd - RUN_ATTEMPTS))
    assert runs_left_for(rpd, calls_today(run_app.pool, MODEL)) == 1
    rid = run_app.start(script=BUSY_RUN)  # every attempt it can make
    assert steps_of(run_app.pool, rid)[0] == "intro"
    assert calls_of(run_app.pool, rid) == RUN_ATTEMPTS
    assert calls_today(run_app.pool, MODEL) == rpd
    assert runs_left_for(rpd, rpd) == 0 and runs_left_for(500, 0) == 21


def test_the_intro_keeps_the_shared_pacing(run_app, monkeypatch):
    """At 10 a minute every attempt of the run, the Introduction's on its own thread
    included, starts at least 6 s after the one before."""
    from pubmedgemini.intro import start_intro

    starts = []
    real = GeminiPacer.wait

    def spy(self, estimated, deadline):
        ok = real(self, estimated, deadline)
        if ok:
            starts.append(self._last_start)
        return ok

    monkeypatch.setattr(GeminiPacer, "wait", spy)
    run_app_runner = run_app.runner(HAPPY_RUN, start_intro=start_intro)
    run_app.model_caller._pacer = GeminiPacer(10, 250_000, run_app.clock.now,
                                              run_app.clock.sleep)
    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)
    InlineStarter(run_app_runner).start(rid)
    assert run_app.view(rid).status == "done"
    assert len(starts) == 14
    gaps = [b - a for a, b in zip(sorted(starts), sorted(starts)[1:])]
    assert min(gaps) >= 6.0 - 1e-9, gaps


def _summary_citing(ids):
    from writer_fakes import good_parts, part_reply, whole_answer

    parts = good_parts()
    parts["summary"] = [part_reply(Section.SUMMARY, whole_answer("w", ids))]
    return parts


def _spy_early(monkeypatch):
    import pubmedgemini.runner as runner_mod

    stored = []
    real = runner_mod.store_early

    def spy(pool, run_id, body):
        stored.append(json.loads(json.dumps(body)))
        return real(pool, run_id, body)

    monkeypatch.setattr(runner_mod, "store_early", spy)
    return stored


def test_early_summary_is_citation_checked(run_app, monkeypatch):
    from pubmedgemini.writer.answer import read_body

    stored = _spy_early(monkeypatch)
    rid = run_app.start(parts=_summary_citing((ID2, "99999999")))
    v = run_app.view(rid)
    assert v.status == "done"
    early = [b for b in stored if b["summary"]]
    assert early, "no early summary was stored"
    text = read_body(early[-1]["answer"]).answer.bottom_line.text
    assert ID2 in text and "99999999" not in text
    assert text == answer_of(v).answer.bottom_line.text  # the final answer's, same reply
    # no outcomes or trials early, and the key numbers are held back (O3)
    a = read_body(early[-1]["answer"]).answer
    assert a.outcomes == [] and a.studies == [] and a.at_a_glance.key_numbers == []
    assert a.at_a_glance.verdict and answer_of(v).answer.at_a_glance.key_numbers


def test_early_summary_stored_then_cleared_at_the_end(run_app):
    from writer_fakes import good_parts

    parts = good_parts()
    outcomes = parts["outcomes"][0]
    seen = []

    def held(request):  # the outcomes part waits until the summary is on the page
        wait_for(lambda: rows(run_app.pool, "SELECT count(*) FROM run_early")[0][0] == 1)
        rid = rows(run_app.pool, "SELECT run_id FROM run_early")[0][0]
        seen.append(run_app.view(rid).early)
        return httpx.Response(200, json=outcomes)

    parts["outcomes"] = [held]
    rid = run_app.start(parts=parts)
    assert seen and seen[0]["summary"] is True and seen[0]["answer"]["answer"]
    assert run_app.view(rid).status == "done"
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]
    assert run_app.view(rid).early is None


def test_withdraw_deletes_the_early_summary(run_app, caplog):
    from writer_fakes import good_parts, piece_reply, whole_answer

    parts = good_parts()
    stored_first = []

    def unreadable_after_the_summary(request):
        wait_for(lambda: rows(run_app.pool, "SELECT count(*) FROM run_early")[0][0] == 1)
        stored_first.append(True)
        return httpx.Response(200, json=reply([{"text": "not json"}]))

    parts["trials"] = [reply([{"text": "not json"}]), unreadable_after_the_summary]
    at_one_piece = []

    def one_piece(request):
        at_one_piece.append(rows(run_app.pool, "SELECT count(*) FROM run_early")[0][0])
        return httpx.Response(200, json=piece_reply(whole_answer()))

    caplog.set_level(logging.INFO)
    rid = run_app.start(parts=parts, one_piece=[one_piece])
    v = run_app.view(rid)
    assert stored_first == [True] and at_one_piece == [0]
    assert v.status == "done" and v.answer is not None and v.early is None
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]
    assert f"run kind=early_withdrawn run_id={rid}" in [r.getMessage() for r in caplog.records]


def test_a_failing_early_store_never_fails_the_run(run_app, monkeypatch, caplog):
    import pubmedgemini.runner as runner_mod

    def boom(*a, **k):
        raise psycopg.OperationalError("made-up")

    monkeypatch.setattr(runner_mod, "store_early", boom)
    caplog.set_level(logging.INFO)
    rid = run_app.start()
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("done", None) and v.answer is not None
    assert f"run kind=early_not_stored run_id={rid} error=OperationalError" in [
        r.getMessage() for r in caplog.records]


def test_an_early_body_that_cannot_be_built_never_fails_the_run(run_app, monkeypatch, caplog):
    import pubmedgemini.runner as runner_mod

    def boom(*a, **k):
        raise ValueError("made-up")

    monkeypatch.setattr(runner_mod, "early_answer", boom)
    caplog.set_level(logging.INFO)
    rid = run_app.start()
    assert run_app.view(rid).status == "done"
    assert f"run kind=early_not_stored run_id={rid} error=ValueError" in [
        r.getMessage() for r in caplog.records]


def test_hidden_characters_in_the_early_part_and_the_answer_are_cleaned(run_app, monkeypatch,
                                                                         caplog):
    """The model text reaches the early summary and the answer with a NUL, U+202E and U+200B
    (put in after the client, which already removes a NUL): both are stored cleaned, and the
    run ends `done` as it would without them."""
    from writer_fakes import good_parts

    import pubmedgemini.runner as runner_mod
    from pubmedgemini.writer.answer import AnswerV3

    real_early = runner_mod.early_answer

    def dirty_early(summary, context):
        w = real_early(summary, context)
        w.bottom_line.text += DIRTY
        return w

    real_body = AnswerV3.to_body

    def dirty_body(self):
        body = real_body(self)
        body["question"] = "q" + DIRTY
        return body

    monkeypatch.setattr(runner_mod, "early_answer", dirty_early)
    monkeypatch.setattr(AnswerV3, "to_body", dirty_body)
    parts = good_parts()
    outcomes = parts["outcomes"][0]
    seen = []

    def held(request):  # the outcomes part waits until the summary is stored
        wait_for(lambda: rows(run_app.pool, "SELECT count(*) FROM run_early")[0][0] == 1)
        seen.append(rows(run_app.pool, "SELECT body::text FROM run_early")[0][0])
        return httpx.Response(200, json=outcomes)

    parts["outcomes"] = [held]
    caplog.set_level(logging.INFO)
    rid = run_app.start(parts=parts)
    v = run_app.view(rid)
    assert (v.status, v.error_kind) == ("done", None)
    assert seen and "abcd" in seen[0]
    assert "\\u0000" not in seen[0] and "\u202e" not in seen[0] and "\u200b" not in seen[0]
    assert v.answer["question"] == "qabcd"
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("early_not_stored" in m or "answer_not_stored" in m for m in msgs)


def test_an_early_store_for_a_run_not_running_logs_its_run_id(run_app, caplog):
    from writer_fakes import whole_answer

    from pubmedgemini.runner import EarlyStore, _Answer
    from pubmedgemini.writer.answer import IntegrityV3
    from pubmedgemini.writer.read import as_summary

    rid = create_run(run_app.pool, run_app.owner_id, QUESTION, MODEL)  # queued, not running
    w = whole_answer("ex", (ID2,))
    es = EarlyStore(run_app.pool, rid, _Answer("q", [], None, IntegrityV3()), [ID2])
    caplog.set_level(logging.INFO)
    es(("summary", as_summary({f: w[f] for f in Section.SUMMARY.fields})))
    assert f"run kind=early_not_stored run_id={rid} error=NotRunning" in [
        r.getMessage() for r in caplog.records]
    assert rows(run_app.pool, "SELECT count(*) FROM run_early") == [(0,)]
