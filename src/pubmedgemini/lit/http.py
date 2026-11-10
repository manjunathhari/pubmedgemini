"""The one outbound HTTP client for literature sources (Incisive `lit/http.rs`).

The allowlist is enforced on every hop, redirects included: https only, default port only,
no userinfo, host one of BASE_HOSTS. Every httpx error becomes a LitError holding a fixed
kind (and the HTTP status), never a URL, so no message or traceback can carry the search
text or the NCBI key. Nothing here logs.
"""

import json
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal

import httpx

BASE_HOSTS = ("eutils.ncbi.nlm.nih.gov", "www.ebi.ac.uk")
NCBI_HOST = BASE_HOSTS[0]

REQUEST_TIMEOUT_SECONDS = 20.0
# Retries after the first attempt, on 5xx, timeout or network error only (not 429).
MAX_RETRIES = 2
# Attempt n waits n x this before it is sent again.
RETRY_BACKOFF_SECONDS = 0.5
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 8 * 1024 * 1024
USER_AGENT = "pubmedgemini/0.1.0"

LitErrorKind = Literal[
    "disallowed", "too_large", "too_many_redirects", "http", "timeout", "parse", "network"
]


class LitError(Exception):
    """A literature request failed. `str()` is the kind, or `http <status>`; never a URL."""

    def __init__(self, kind: LitErrorKind, status: int | None = None) -> None:
        self.kind = kind
        self.status = status
        super().__init__(f"{kind} {status}" if status is not None else kind)

    @property
    def transient(self) -> bool:
        """Worth another attempt: a timeout, a network error or a 5xx (Incisive
        `is_transient_err`). A 429 is not retried."""
        if self.kind in ("timeout", "network"):
            return True
        return self.kind == "http" and self.status is not None and 500 <= self.status <= 599


def ncbi_min_interval(has_key: bool) -> float:
    """NCBI's limits: 3 requests a second without a key, 10 with one."""
    return 0.1 if has_key else 0.334


class NcbiPacer:
    """Spaces NCBI attempts at least `gap_seconds` apart. Callers queue on one lock, so
    every thread shares one schedule (Incisive `pubmed.rs` `Pacer::wait`)."""

    def __init__(
        self,
        gap_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.gap_seconds = gap_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = clock()

    def wait(self) -> None:
        if self.gap_seconds <= 0:
            return
        with self._lock:
            now = self._clock()
            if self._next > now:
                self._sleep(self._next - now)
            self._next = max(self._clock(), self._next) + self.gap_seconds


def _allowed(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.port is None  # httpx drops an explicit default port, as Incisive's url crate
        and not url.userinfo
        and url.host.lower() in BASE_HOSTS
    )


class LitClient:
    """GETs JSON or text from the allowlisted hosts, with retries, pacing and a body cap."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        pacer: NcbiPacer | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pacer = pacer
        self._sleep = sleep
        self._http = httpx.Client(
            transport=transport,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"user-agent": USER_AGENT},
            event_hooks={"request": [self._before_hop]},
        )

    def _before_hop(self, request: httpx.Request) -> None:
        """Runs before every hop and every retry: the allowlist, then the NCBI pacer."""
        if not _allowed(request.url):
            raise LitError("disallowed") from None
        if self._pacer is not None and request.url.host.lower() == NCBI_HOST:
            self._pacer.wait()

    def get_json(self, url: str, params: Mapping[str, str]) -> Any:
        body = self._get(url, params)
        try:
            return json.loads(body)
        except ValueError:
            raise LitError("parse") from None

    def get_text(self, url: str, params: Mapping[str, str]) -> str:
        return self._get(url, params).decode("utf-8", errors="replace")

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: Mapping[str, str]) -> bytes:
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, TypeError):
            raise LitError("disallowed") from None
        if not _allowed(parsed):
            raise LitError("disallowed") from None
        request = self._http.build_request("GET", parsed, params=dict(params))
        attempt = 0
        while True:
            try:
                return self._attempt(request)
            except LitError as exc:
                err = exc
            if not err.transient or attempt >= MAX_RETRIES:
                raise err from None
            attempt += 1
            self._sleep(RETRY_BACKOFF_SECONDS * attempt)

    def _attempt(self, request: httpx.Request) -> bytes:
        try:
            response = self._http.send(request, stream=True)
        except LitError:
            raise
        except httpx.TooManyRedirects:
            raise LitError("too_many_redirects") from None
        except httpx.TimeoutException:
            raise LitError("timeout") from None
        except httpx.InvalidURL:
            raise LitError("disallowed") from None
        except httpx.HTTPError:
            raise LitError("network") from None
        try:
            if not response.is_success:
                raise LitError("http", response.status_code) from None
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BODY_BYTES:
                raise LitError("too_large") from None
            body = bytearray()
            try:
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise LitError("too_large") from None
                    body += chunk
            except httpx.TimeoutException:
                raise LitError("timeout") from None
            except httpx.HTTPError:
                raise LitError("network") from None
            return bytes(body)
        finally:
            response.close()
