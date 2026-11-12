"""Test doubles for the writer's four parts on threads (F3a Task 6).

`RoutedFake` is an httpx.MockTransport for Gemini that four threads can share: every request is
routed by what it asks for, under one lock, to its own queue of replies:

* a request whose user text holds `SECTION TO WRITE: <NAME>` goes to that part's queue;
* one with the writer's system prompt and no part instruction goes to the one-piece queue;
* anything else is served from the F2 script, in order.

A step is a 200 body (dict), an httpx.Response, or a function of the request returning one (run
outside the lock, so it may block on a barrier or an event). An empty queue fails the test.
`LockedClock` is lit_fakes' FakeClock behind a lock: the pacer, the caller and the part threads
read and move it at once. Neither sleeps or reaches the network.

Task 7 adds a whole run's Gemini: `RunGemini` (F2's FakeGemini for the researcher, a RoutedFake
for the writer) and the writer replies it serves by default (`whole_answer`, `good_parts`).

F3c: a request with the Introduction's system prompt goes to RunGemini's own `intro` queue (by
default every Introduction gets `INTRO_REPLY`), so the researcher's script and the writer's
queues are served exactly as before. `inline_intro` writes the Introduction at once, inside
`run_one`, before the research (as InlineStarter does for runs), so a test's attempt counts
are fixed: the Introduction's attempt is always the run's first.
"""

import json
import threading
from collections.abc import Callable, Sequence

import httpx
from gemini_fakes import FakeGemini, reply

from pubmedgemini import intro
from pubmedgemini.gemini.client import GEMINI_HOST
from pubmedgemini.writer.prompts import SECTION_HEAD, SYSTEM, Section

Step = httpx.Response | dict | Callable[[httpx.Request], httpx.Response]
ONE_PIECE = "one_piece"
F2 = "f2"
INTRO = "intro"


def route_of(body: dict) -> str:
    """Which queue a request body belongs to: a part's name, ONE_PIECE or F2."""
    try:
        system = body["systemInstruction"]["parts"][0]["text"]
        user = body["contents"][0]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return F2
    if system.startswith(intro.PROMPT):
        return INTRO
    for s in Section.ALL:
        if f"{SECTION_HEAD}{s.name.upper()}\n" in user:
            return s.name
    if system.startswith(SYSTEM):
        return ONE_PIECE
    return F2


class RoutedFake:
    def __init__(self, parts: dict[str, Sequence[Step]] | None = None,
                 one_piece: Sequence[Step] = (), script: Sequence[Step] = ()) -> None:
        self._lock = threading.Lock()
        self.queues: dict[str, list[Step]] = {s.name: [] for s in Section.ALL}
        for name, steps in (parts or {}).items():
            if name not in self.queues:
                raise ValueError(f"no such part: {name}")
            self.queues[name] = list(steps)
        self.queues[ONE_PIECE] = list(one_piece)
        self.queues[F2] = list(script)
        self.sent: list[tuple[str, dict]] = []  # (route, body), in the order received
        self.transport = httpx.MockTransport(self._handle)

    def bodies(self, route: str | None = None) -> list[dict]:
        with self._lock:
            return [b for r, b in self.sent if route is None or r == route]

    def count(self, route: str | None = None) -> int:
        return len(self.bodies(route))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != GEMINI_HOST:
            return httpx.Response(404)
        body = json.loads(request.content)
        route = route_of(body)
        with self._lock:
            self.sent.append((route, body))
            queue = self.queues[route]
            if not queue:
                raise AssertionError(f"RoutedFake: no reply left for {route}")
            step = queue.pop(0)
        if callable(step) and not isinstance(step, httpx.Response):
            return step(request)
        if isinstance(step, dict):
            return httpx.Response(200, json=step)
        return httpx.Response(step.status_code, headers=step.headers, content=step.content)


class LockedClock:
    """A monotonic clock that moves only when sleep() is called or a test sets it."""

    def __init__(self, start: float = 1000.0) -> None:
        self._lock = threading.Lock()
        self._t = start
        self.slept: list[float] = []

    @property
    def t(self) -> float:
        with self._lock:
            return self._t

    @t.setter
    def t(self, value: float) -> None:  # `clock.t += 420` in a test, as lit_fakes' FakeClock
        with self._lock:
            self._t = value

    def set(self, t: float) -> None:
        with self._lock:
            self._t = t

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.slept.append(seconds)
            self._t += seconds


# ---------- a whole run: the researcher's script and the writer's parts (F3a Task 7) ----------

# The F1 fixtures' PMIDs, as tests/test_runs.py and the leak script select them.
RUN_IDS = ("33763744", "36262096")


def whole_answer(tag: str = "w", ids: Sequence[str] = RUN_IDS) -> dict:
    """A whole single-writer reply citing `ids`; every model-written text carries `tag`."""
    a, b = ids[0], ids[-1]
    return {
        "bottom_line": {"text": f"{tag} no clear benefit [{a}; {b}].",
                        "certainty": "Moderate", "certainty_reason": f"{tag} one trial, one review"},
        "outcomes": [{
            "outcome": f"{tag} chronic pain", "rct_evidence": f"1 RCT [{b}]",
            "effect_text": f"{tag} RR 0.76 [{b}]",
            "numbers": [{"label": "RR", "value": 0.76, "unit": None, "arm": None,
                         "ci_low": 0.49, "ci_high": 1.1, "p": None, "pmid": b}],
            "certainty": "Moderate", "why": f"{tag} small", "pmids": [a, b],
        }],
        "studies": [{"pmid": i, "label": f"{tag} study {n}", "design": "RCT", "n": 60,
                     "setting": "s", "comparison": "c", "key_results": f"k [{i}]",
                     "appraisal": f"{tag} appraisal"} for n, i in enumerate(ids, 1)],
        "conflicts": [],
        "applicability": {"case_mix": f"{tag} simple", "india_cost_context": "not known",
                          "training": "none"},
        "would_change": [f"{tag} a larger RCT"],
        "at_a_glance": f"{tag} no clear benefit.\nChronic pain: RR 0.76 [{b}]\nMain caveat: small",
        "clinical_practice": f"Default choice: {tag} lap\nCost per case: not known",
        "notable_findings": f"- {tag} withdrawals 2 vs 9 [{b}].",
    }


def part_reply(s: Section, w: dict) -> dict:
    """The 200 body of part `s` cut from the whole reply `w`."""
    return reply([{"text": json.dumps({f: w[f] for f in s.fields})}], out=321)


def piece_reply(w: dict) -> dict:
    return reply([{"text": json.dumps(w)}], out=654)


def good_parts(w: dict | None = None, copies: int = 1) -> dict[str, list[Step]]:
    """Every part answering at its first call with its share of `w`, `copies` times (one per
    run the fake serves)."""
    w = whole_answer() if w is None else w
    return {s.name: [part_reply(s, w)] * copies for s in Section.ALL}


INTRO_TEXT = "Made-up introduction, paragraph one.\n\nMade-up paragraph two."


def intro_reply(text: str = INTRO_TEXT, finish: str = "STOP") -> dict:
    return reply([{"text": text}], finish=finish, out=111)


def inline_intro(run_id, question, *, caller, pool, clock) -> threading.Thread:
    """`intro.start_intro` for tests: writes the Introduction at once, then returns a thread
    that has already ended."""
    intro.write_intro(run_id, question, caller=caller, pool=pool, clock=clock)
    t = threading.Thread(target=lambda: None, name="intro")
    t.start()
    t.join()
    return t


class RunGemini(FakeGemini):
    """A whole run's Gemini: F2's FakeGemini serves the researcher's calls (in order, every
    model turn sent back checked) and a RoutedFake the writer's parts and one piece, by route.
    `.sent`, `.bodies` and `.script` are the researcher's (as FakeGemini's); `.writer` holds
    the writer's queues and requests."""

    def __init__(self, script: Sequence[Step], parts: dict[str, Sequence[Step]] | None = None,
                 one_piece: Sequence[Step] = (), intro: Sequence[Step] | None = None) -> None:
        super().__init__(script)
        self.writer = RoutedFake(good_parts(copies=8) if parts is None else parts, one_piece)
        self._intro_lock = threading.Lock()
        self._intro_steps = None if intro is None else list(intro)
        self.intro_sent: list[dict] = []
        self.transport = httpx.MockTransport(self._route)

    def _intro(self, request: httpx.Request, body: dict) -> httpx.Response:
        with self._intro_lock:
            self.intro_sent.append(body)
            if self._intro_steps is None:
                step: Step = intro_reply()
            elif not self._intro_steps:
                raise AssertionError("RunGemini: no Introduction reply left")
            else:
                step = self._intro_steps.pop(0)
        if callable(step) and not isinstance(step, httpx.Response):
            return step(request)
        if isinstance(step, dict):
            return httpx.Response(200, json=step)
        return httpx.Response(step.status_code, headers=step.headers, content=step.content)

    def _route(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == GEMINI_HOST:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = None
            if isinstance(body, dict) and route_of(body) == INTRO:
                return self._intro(request, body)
            if isinstance(body, dict) and route_of(body) != F2:
                return self.writer._handle(request)
        return self._handle(request)
