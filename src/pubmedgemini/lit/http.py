"""The one outbound HTTP client for literature sources (Incisive `lit/http.rs`).

The allowlist is enforced on every hop, redirects included (they are followed by hand): https only, default port only,
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

# A total for each attempt (Incisive `REQUEST_TIMEOUT`); httpx's timeout also bounds each phase.
REQUEST_TIMEOUT_SECONDS = 20.0
# Retries after the first attempt, on 5xx, timeout or network error only (not 429).
MAX_RETRIES = 2
# Attempt n waits n x this before it is sent again.
RETRY_BACKOFF_SECONDS = 0.5
MAX_REDIRECTS = 3
# The NCBI key's parameter name: never sent to any other host.
NCBI_KEY_PARAM = "api_key"
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


class _Redirect(Exception):
    """A 301, 302, 303, 307 or 308 with a Location. Raised from the response hook, so httpx
    closes the response unread and never works out a redirect request of its own."""

    def __init__(self, location: str) -> None:
        super().__init__("redirect")
        self.location = location


def _on_response(response: httpx.Response) -> None:
    """Every 3xx stops here, before httpx reads its body or parses its Location (Incisive
    `attempt`): a redirect status with a Location is a hop, any other 3xx an HTTP error."""
    status = response.status_code
    if not 300 <= status <= 399:
        return
    location = response.headers.get("location")
    if status in (301, 302, 303, 307, 308) and location is not None:
        raise _Redirect(location)
    raise LitError("http", status)


def _next_hop(current: httpx.URL, location: str) -> httpx.URL:
    """The redirect target, resolved against `current` (Incisive `follow`). The request's
    parameters are not carried over, and an `api_key` is dropped on any host but NCBI's, so
    the key only ever goes to NCBI. An invalid or refused target is "disallowed"."""
    try:
        target = current.join(location)
    except (httpx.InvalidURL, TypeError, ValueError):
        raise LitError("disallowed") from None
    if not _allowed(target):
        raise LitError("disallowed") from None
    if target.host.lower() != NCBI_HOST and NCBI_KEY_PARAM in target.params:
        target = target.copy_remove_param(NCBI_KEY_PARAM)
    return target


class LitClient:
    """GETs JSON or text from the allowlisted hosts, with retries, pacing and a body cap.

    Redirects are followed by hand (Incisive `follow`): every hop is checked against the
    allowlist before it is sent, at most MAX_REDIRECTS are followed, a redirect's body is
    never read, and retries are per hop (Incisive `fetch_hop`)."""

    def __init__(
        self,
        transport: httpx.BaseTransport | None = None,
        pacer: NcbiPacer | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pacer = pacer
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"user-agent": USER_AGENT},
            event_hooks={"response": [_on_response]},
        )

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
        current = self._http.build_request("GET", parsed, params=dict(params)).url
        redirects = 0
        while True:
            hop = self._fetch_hop(current)
            if isinstance(hop, bytes):
                return hop
            if redirects >= MAX_REDIRECTS:
                raise LitError("too_many_redirects") from None
            redirects += 1
            current = _next_hop(current, hop.location)

    def _fetch_hop(self, url: httpx.URL) -> "bytes | _Redirect":
        """One allowed hop, retried on a transient error; the NCBI pacer runs before every
        attempt to NCBI (Incisive `fetch_hop` and its `before_attempt` hook)."""
        attempt = 0
        while True:
            if self._pacer is not None and url.host.lower() == NCBI_HOST:
                self._pacer.wait()
            try:
                return self._attempt(url)
            except LitError as exc:
                err = exc
            if not err.transient or attempt >= MAX_RETRIES:
                raise err from None
            attempt += 1
            self._sleep(RETRY_BACKOFF_SECONDS * attempt)

    def _attempt(self, url: httpx.URL) -> "bytes | _Redirect":
        """One request. REQUEST_TIMEOUT_SECONDS is a total for the attempt, as reqwest's
        timeout is in Incisive: httpx's own timeout bounds each connect and each read, so the
        total is checked once the reply's head is in and after every chunk of the body."""
        deadline = self._clock() + REQUEST_TIMEOUT_SECONDS
        request = self._http.build_request("GET", url)
        try:
            response = self._http.send(request, stream=True)
        except _Redirect as redirect:
            if self._clock() > deadline:
                raise LitError("timeout") from None
            return redirect
        except LitError:
            raise
        except httpx.TimeoutException:
            raise LitError("timeout") from None
        except httpx.InvalidURL:
            raise LitError("disallowed") from None
        except httpx.HTTPError:
            raise LitError("network") from None
        try:
            if self._clock() > deadline:
                raise LitError("timeout") from None
            if not response.is_success:
                raise LitError("http", response.status_code) from None
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_BODY_BYTES:
                raise LitError("too_large") from None
            body = bytearray()
            try:
                for chunk in response.iter_bytes():
                    if self._clock() > deadline:
                        raise LitError("timeout") from None
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
