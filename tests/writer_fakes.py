"""Writer replies for tests: the hand-written fixtures in tests/fixtures/writer (the frozen
mock's hernia numbers; no live reply) and small builders that change one field of them."""

import copy
import json
import re
import threading
from pathlib import Path
from typing import Any

import httpx
from gemini_fakes import reply

from pubmedgemini.gemini.client import GEMINI_HOST

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "writer"
PART_FILES = {"trials": "trials.json", "outcomes": "outcomes.json", "context": "context.json",
              "summary": "summary.json"}


def fixture(name: str) -> dict:
    """The parsed JSON of tests/fixtures/writer/<name>."""
    return json.loads((FIXTURES / name).read_text())


def fixture_part(word: str) -> dict:
    """One part's reply, by its word (`trials`, `outcomes`, `context`, `summary`)."""
    return fixture(PART_FILES[word])


def whole(bottom_certainty: str | None = None, outcome_certainty: str | None = None,
          number: dict | None = None) -> dict:
    """`whole.json` with the bottom line's certainty, the first outcome's certainty or the
    first outcome's first number changed."""
    w = fixture("whole.json")
    if bottom_certainty is not None:
        w["bottom_line"]["certainty"] = bottom_certainty
    if outcome_certainty is not None:
        w["outcomes"][0]["certainty"] = outcome_certainty
    if number is not None:
        w["outcomes"][0]["numbers"][0].update(number)
    return w


def outcomes_part(number_overrides: dict | None = None, extra_key: str | None = None) -> dict:
    """`outcomes.json` with the first number changed, or an extra key on the first number."""
    p = fixture_part("outcomes")
    n = p["outcomes"][0]["numbers"][0]
    if number_overrides is not None:
        n.update(number_overrides)
    if extra_key is not None:
        n[extra_key] = "extra"
    return p


def json_reply(value: Any, finish: str = "STOP", out: int = 100) -> dict:
    """A Gemini 200 body whose text is `value` as JSON."""
    return reply([{"text": json.dumps(value, ensure_ascii=False)}], finish=finish, out=out)


def text_reply(text: str, finish: str = "STOP") -> dict:
    return reply([{"text": text}], finish=finish)


def deep(value: Any) -> Any:
    return copy.deepcopy(value)


# ---------- a Gemini that serves the writer's parts by their section word ----------

_SECTION = re.compile(r"SECTION TO WRITE: ([A-Z]+)")
ONE_PIECE = "ONE_PIECE"


def section_word(body: dict) -> str:
    """The part a request writes (`TRIALS` …), or ONE_PIECE for the one-piece writer."""
    m = _SECTION.search(body["contents"][0]["parts"][0]["text"])
    return m.group(1) if m else ONE_PIECE


def user_of(body: dict) -> str:
    return body["contents"][0]["parts"][0]["text"]


class PartsGemini:
    """An httpx.MockTransport for GEMINI_HOST that serves each part from its own script
    (`{"TRIALS": [step, ...], ..., "ONE_PIECE": [...]}`), since the four threads may send in
    any order after pacing. A step is a 200 body (dict), an httpx.Response, or a function of
    the request returning one. Records every request with the clock time it arrived.
    Thread-safe."""

    def __init__(self, script: dict, clock=None, on_request=None) -> None:
        self.script = {k: list(v) for k, v in script.items()}
        self.clock = clock
        self.on_request = on_request
        self.sent: list[dict] = []
        self.starts: list[float] = []
        self._lock = threading.Lock()
        self.transport = httpx.MockTransport(self._handle)

    def words(self) -> list[str]:
        return [section_word(b) for b in self.sent]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != GEMINI_HOST:
            return httpx.Response(404)
        body = json.loads(request.content)
        word = section_word(body)
        with self._lock:
            self.sent.append(body)
            if self.clock is not None:
                self.starts.append(self.clock.now())
            steps = self.script.get(word) or []
            if not steps:
                raise AssertionError(f"PartsGemini: no reply left for {word}")
            step = steps.pop(0)
        if self.on_request is not None:
            self.on_request(word, body)
        if callable(step) and not isinstance(step, httpx.Response):
            return step(request)
        if isinstance(step, dict):
            return httpx.Response(200, json=step)
        return httpx.Response(step.status_code, headers=step.headers, content=step.content)


def parts_ok() -> dict:
    """Every part's first reply readable; nothing for the one-piece writer."""
    return {w.upper(): [json_reply(fixture_part(w))] for w in PART_FILES} | {ONE_PIECE: []}


def whole_ok() -> dict:
    return json_reply(fixture("whole.json"))


# The fixtures' made-up PMIDs as the test runs' selected studies: the meta-analysis
# (90000001) as the run's SR_MA, the Swedish trial (90000004) as its RCT. The rest stay
# PMIDs that are not in the run's study list.
RUN_PMIDS = {"90000001": "33763744", "90000004": "36262096"}


def remap(value: Any, pmids: dict[str, str] | None = None) -> Any:
    """`value` with the fixtures' PMIDs replaced (RUN_PMIDS by default)."""
    s = json.dumps(value, ensure_ascii=False)
    for a, b in (RUN_PMIDS if pmids is None else pmids).items():
        s = s.replace(a, b)
    return json.loads(s)


def run_parts(pmids: dict[str, str] | None = None) -> dict:
    """`parts_ok` for a test run: each part's reply with the run's PMIDs."""
    return {w.upper(): [json_reply(remap(fixture_part(w), pmids))] for w in PART_FILES} | {
        ONE_PIECE: []}


def run_writer(script: dict | None = None) -> "PartsGemini":
    """The writer of a test run (FakeGemini's `writer`): `run_parts` unless given."""
    return PartsGemini(run_parts() if script is None else script)


def cut_off(word: str = "outcomes") -> dict:
    """A reply cut off by its output limit (a readable part, but MAX_TOKENS: never read)."""
    return json_reply(fixture_part(word) if word != ONE_PIECE else fixture("whole.json"),
                      finish="MAX_TOKENS", out=12_000)
