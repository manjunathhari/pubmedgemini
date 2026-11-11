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
"""

import json
import threading
from collections.abc import Callable, Sequence

import httpx

from pubmedgemini.gemini.client import GEMINI_HOST
from pubmedgemini.writer.prompts import SECTION_HEAD, SYSTEM, Section

Step = httpx.Response | dict | Callable[[httpx.Request], httpx.Response]
ONE_PIECE = "one_piece"
F2 = "f2"


def route_of(body: dict) -> str:
    """Which queue a request body belongs to: a part's name, ONE_PIECE or F2."""
    try:
        system = body["systemInstruction"]["parts"][0]["text"]
        user = body["contents"][0]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError):
        return F2
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
