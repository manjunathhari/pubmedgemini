import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pubmedgemini.db import is_healthy
from pubmedgemini.identity import NotSetUp, NotSignedIn, User, current_user
from pubmedgemini.lit.query import query_rejected_message
from pubmedgemini.search import BothUnreachable, PageResult, QueryRejected, run_page_search

log = logging.getLogger("pubmedgemini.web")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()

NOT_SIGNED_IN = "You are not signed in."
NOT_SET_UP = (
    "Your Google account is allowed in, but you haven't been set up yet. "
    "Ask the owner to add you."
)
DB_DOWN = "The database could not be reached. Please try again in a minute."
NOT_READY = "The app could not finish starting. Please try again in a minute."
BOTH_DOWN = "PubMed and Europe PMC could not be reached. Please try again in a minute."
CROSS_SITE = "This request did not come from this page. Open the page and search again."
INTERNAL_ERROR = "Something went wrong. Please try again in a minute."
SOURCE_NAMES = {"pubmed": "PubMed", "europepmc": "Europe PMC"}


class NotReady(Exception):
    """Start-up work has not completed. `kind` is a fixed word, never exception text."""

    def __init__(self, kind: str | None):
        super().__init__(kind)
        self.kind = kind


class CrossSite(Exception):
    """A form post another site made the browser send."""


def _host_key(host: str) -> str:
    """A host for comparing: lower case, without a default port (80 or 443)."""
    h = host.strip().lower()
    for port in (":443", ":80"):
        if h.endswith(port):
            return h[: -len(port)]
    return h


def is_cross_site(headers: Mapping[str, str]) -> bool:
    """True when another site sent this request. `Sec-Fetch-Site` decides when present: only
    "same-origin" and "none" are fine; "same-site" (a sibling subdomain), "cross-site" and
    any other value are refused. Without it, an `Origin` whose host differs from the
    request's own (`X-Forwarded-Host`, else `Host`). Neither header (old browsers, curl), an
    Origin with no host ("null"), or no own host: allowed."""
    site = headers.get("sec-fetch-site")
    if site is not None:
        return site.strip().lower() not in ("same-origin", "none")
    origin = headers.get("origin")
    if origin is None:
        return False
    try:
        origin_host = urlsplit(origin.strip()).netloc
    except ValueError:
        return False
    own = (headers.get("x-forwarded-host") or "").split(",")[0] or headers.get("host") or ""
    if not origin_host or not own.strip():
        return False
    return _host_key(origin_host) != _host_key(own)


def refuse_cross_site(request: Request) -> None:
    """FastAPI dependency, run before anything else on a form post."""
    if is_cross_site(request.headers):
        log.info("search kind=refused_cross_site")  # no header values
        raise CrossSite


def _message(request: Request, text: str, status_code: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "message.html", {"message": text}, status_code=status_code
    )


def require_ready(request: Request) -> None:
    """Retry the start-up work if the database was unreachable at start-up."""
    request.app.state.ensure_ready()


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_ready)])
def home(request: Request, user: Annotated[User, Depends(current_user)]):
    return templates.TemplateResponse(request, "home.html", {"email": user.email})


def drop_trailing_question_marks(text: str) -> str:
    """Owner decision (10 Nov 2026), for the search page only: a typed question often ends
    with "?", which the query rule refuses. Trailing question marks and the whitespace
    around them are dropped quietly; a "?" anywhere else is left for the rule to refuse.
    The tools and the rule are unchanged."""
    trimmed = text.rstrip()
    if not trimmed.endswith("?"):
        return text
    while trimmed.endswith("?"):
        trimmed = trimmed[:-1].rstrip()
    return trimmed


def _status_line(result: PageResult) -> str:
    """One line naming what was searched (spec §8)."""
    if "pubmed" in result.not_run:
        return "PubMed does not take the underscore; searched Europe PMC only"
    if result.not_reached:
        down = result.not_reached[0]
        other = "europepmc" if down == "pubmed" else "pubmed"
        return f"{SOURCE_NAMES[down]} could not be reached; searched {SOURCE_NAMES[other]} only"
    return "Searched PubMed and Europe PMC (free sources)"


def _counts_line(result: PageResult) -> str:
    found = []
    if result.pubmed_count is not None:
        found.append(f"PubMed found {result.pubmed_count}")
    if result.epmc_count is not None:
        found.append(f"Europe PMC found {result.epmc_count}")
    line = "; ".join(found) + "."
    if result.papers:
        line += f" Showing the first {len(result.papers)}."
    return line


def _log_search(result: PageResult) -> None:
    """One line per search: fixed kinds and counts only."""
    counts = []
    if result.pubmed_count is not None:
        counts.append(f"pubmed={result.pubmed_count}")
    if result.epmc_count is not None:
        counts.append(f"europepmc={result.epmc_count}")
    shown = len(result.papers)
    with_abstract = sum(1 for p in result.papers if p.abstract)
    if not result.papers:
        log.info("search kind=none %s", " ".join(counts))
        return
    if result.not_run:
        kind = f"partial not_run={result.not_run[0]}"
    elif result.not_reached:
        kind = f"partial down={result.not_reached[0]}"
    else:
        kind = "ok"
    log.info(
        "search kind=%s %s shown=%d abstracts=%d cached=%d",
        kind, " ".join(counts), shown, with_abstract, result.cached,
    )


@router.post(
    "/search",
    response_class=HTMLResponse,
    dependencies=[Depends(refuse_cross_site), Depends(require_ready)],
)
def search(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    q: Annotated[str, Form()] = "",
):
    """The text arrives in the POST body only; the results come back in this response."""
    text = drop_trailing_question_marks(q).strip()
    context = {"email": user.email, "query": text}
    try:
        result = run_page_search(request.app.state.lit, text)
    except QueryRejected as exc:
        log.info("search kind=rejected reason=%s", exc.kind)
        context["rejected"] = query_rejected_message(exc.kind, exc.source)
        return templates.TemplateResponse(request, "results.html", context, status_code=400)
    except BothUnreachable:
        log.info("search kind=unavailable")
        return _message(request, BOTH_DOWN, 503)
    _log_search(result)
    context |= {
        "result": result,
        "status_line": _status_line(result),
        "counts_line": _counts_line(result),
    }
    return templates.TemplateResponse(request, "results.html", context)


@router.get("/search", dependencies=[Depends(require_ready), Depends(current_user)])
def search_get():
    """Where the gate lands the browser after a sign-in that expired mid-search: back to the
    search box, with the sign-in checks of `/`. The typed text is lost (it was in the body)."""
    return RedirectResponse("/", status_code=303)


@router.get("/healthz")
def healthz(request: Request):
    try:
        request.app.state.ensure_ready()
    except NotReady as exc:
        status = "database_unreachable" if exc.kind == "db_unreachable" else "not_ready"
        return JSONResponse({"status": status}, status_code=503)
    if is_healthy(request.app.state.pool):
        return {"status": "ok"}
    return JSONResponse({"status": "database_unreachable"}, status_code=503)


def register(app: FastAPI) -> None:
    app.include_router(router)

    @app.exception_handler(NotSignedIn)
    def not_signed_in(request: Request, exc: NotSignedIn):
        return _message(request, NOT_SIGNED_IN, 401)

    @app.exception_handler(NotSetUp)
    def not_set_up(request: Request, exc: NotSetUp):
        return _message(request, NOT_SET_UP, 403)

    @app.exception_handler(CrossSite)
    def cross_site(request: Request, exc: CrossSite):
        return _message(request, CROSS_SITE, 403)

    @app.exception_handler(NotReady)
    def not_ready(request: Request, exc: NotReady):
        return _message(request, DB_DOWN if exc.kind == "db_unreachable" else NOT_READY, 503)

    @app.exception_handler(psycopg.Error)
    def database_error(request: Request, exc: psycopg.Error):
        # Fixed kind only: the exception text can hold connection details.
        log.warning("kind=db_error")
        return _message(request, DB_DOWN, 503)

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            # Anything no handler above took. Answered here, not re-raised, so neither this
            # app nor Uvicorn prints a traceback: its text can hold the search text or a URL.
            # The class name is a fixed word; the message is never logged.
            log.error("kind=internal error=%s", type(exc).__name__)
            response = _message(request, INTERNAL_ERROR, 500)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        log.info("request method=%s route=%s status=%s", request.method, route, response.status_code)
        return response
