"""Test doubles for the literature clients: no test reaches the network."""

from collections.abc import Callable
from pathlib import Path

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "lit"

Route = Callable[[httpx.Request], httpx.Response] | httpx.Response


def fixture(name: str) -> bytes:
    """The bytes of tests/fixtures/lit/<name>."""
    return (FIXTURES / name).read_bytes()


class FakeUpstream:
    """An httpx.MockTransport keyed by (host, path) that records every request in `.sent`.

    A route is a fixed httpx.Response (a fresh copy is served each time) or a function of
    the request that returns one or raises an httpx exception. Unknown routes answer 404.
    """

    def __init__(self, routes: dict[tuple[str, str], Route]) -> None:
        self.routes = dict(routes)
        self.sent: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        route = self.routes.get((request.url.host, request.url.path))
        if route is None:
            return httpx.Response(404)
        if isinstance(route, httpx.Response):
            return httpx.Response(
                route.status_code, headers=route.headers, content=route.content
            )
        return route(request)


class FakeClock:
    """A monotonic clock that only moves when sleep() is called; records each sleep."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


EUTILS_HOST = "eutils.ncbi.nlm.nih.gov"
EUTILS_PATH = "/entrez/eutils/"
EPMC_HOST = "www.ebi.ac.uk"
EPMC_SEARCH_PATH = "/europepmc/webservices/rest/search"


def pubmed_routes() -> dict[tuple[str, str], Route]:
    """The four E-utilities endpoints, each answering with its recorded fixture."""

    def reply(name: str, kind: str) -> httpx.Response:
        return httpx.Response(200, content=fixture(name), headers={"content-type": kind})

    return {
        (EUTILS_HOST, EUTILS_PATH + "esearch.fcgi"): reply("esearch.json", "application/json"),
        (EUTILS_HOST, EUTILS_PATH + "esummary.fcgi"): reply("esummary.json", "application/json"),
        (EUTILS_HOST, EUTILS_PATH + "efetch.fcgi"): reply("efetch.xml", "text/xml"),
        (EUTILS_HOST, EUTILS_PATH + "elink.fcgi"): reply("elink.json", "application/json"),
    }


def europepmc_routes() -> dict[tuple[str, str], Route]:
    """Europe PMC search, answering with its recorded fixture."""
    body = fixture("europepmc.json")
    return {
        (EPMC_HOST, EPMC_SEARCH_PATH): httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )
    }
