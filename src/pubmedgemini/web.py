import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pubmedgemini.db import is_healthy
from pubmedgemini.gemini.quota import reset_time_ist, runs_left
from pubmedgemini.identity import NotSetUp, NotSignedIn, User, current_user
from pubmedgemini.lit.query import query_rejected_message
from pubmedgemini.runs import (
    ActiveRunExists,
    QuestionBlocked,
    RunView,
    SelectedView,
    create_run,
    end_message,
    finish_run,
    get_run,
    list_runs,
    stored_question,
)
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

# ---------- questions (F2, plan Task 9) ----------

MAX_QUESTION_CHARS = 4000  # Incisive ask.rs MAX_QUESTION_CHARS
EMPTY_QUESTION = "Please type your clinical question."  # Incisive ask.rs EMPTY_MESSAGE
TOO_LONG = "That question is too long (4,000 characters max). Please shorten it."
NO_KEY = "Questions are not set up yet: the Gemini key is missing on the server."
ACTIVE_RUN = "A question is already running. Please wait for it to finish, then ask again."
RUN_NOT_FOUND = "That question wasn't found. It may have been deleted."  # runs/mod.rs
DAILY_LIMIT = "Daily free limit reached. Resets at {reset} IST."  # spec section 8
STOPPED = "This run was stopped because the app restarted."
GEMINI_UNAVAILABLE = "Gemini could not be reached or refused the request. Please try again in a minute."
# The plain message for each fixed kind a failed run can end with (plan Task 9).
KIND_MESSAGES = {
    "key_refused": "Google refused the Gemini key. The owner needs to check it on the server.",
    "billing_refused": (
        "Google refused the Gemini key because its Google project is linked to billing. "
        "Make a key in a project with billing turned off."
    ),
    "rate_limited": (
        "The free Gemini limit stayed busy, so the run could not finish. "
        "Please try again in a minute."
    ),
    **{k: GEMINI_UNAVAILABLE for k in ("overloaded", "timeout", "network", "http",
                                       "request_refused", "not_found", "too_large", "parse")},
}
STATUS_WORDS = {"queued": "Waiting", "running": "Running", "done": "Done", "partial": "Partial",
                "failed": "Failed", "stopped": "Stopped"}
LISTED_QUESTION_CHARS = 120
# A run id in a URL: the canonical lower-case form only, so one run has one address.
RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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


def _refuse_cross_site(request: Request, area: str) -> None:
    if is_cross_site(request.headers):
        log.info("%s kind=refused_cross_site", area)  # a fixed word; no header values
        raise CrossSite


def refuse_cross_site(request: Request) -> None:
    """FastAPI dependency, run before anything else on the search form's post."""
    _refuse_cross_site(request, "search")


def refuse_cross_site_ask(request: Request) -> None:
    """The same guard on the question posts (`/ask`, Run again)."""
    _refuse_cross_site(request, "ask")


def _message(request: Request, text: str, status_code: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "message.html", {"message": text}, status_code=status_code
    )


def require_ready(request: Request) -> None:
    """Retry the start-up work if the database was unreachable at start-up."""
    request.app.state.ensure_ready()


@dataclass(frozen=True)
class ListedRun:
    id: uuid.UUID
    short: str
    created_ist: str
    status_word: str


def _home(request: Request, user: User, status_code: int = 200, *, question: str = "",
          ask_message: str | None = None, ask_found=None, ask_link: str | None = None):
    """The home page. `question` refills the box; it is only ever in this response body."""
    state = request.app.state
    settings = state.settings
    enabled = state.gemini_caller is not None
    context = {"email": user.email, "ask_enabled": enabled, "question": question,
               "ask_message": ask_message, "ask_found": ask_found, "ask_link": ask_link}
    if enabled:
        left = runs_left(state.pool, settings.gemini_model, settings.gemini_rpd)
        context["runs_left"] = left
        context["reset_time"] = reset_time_ist(state.pool) if left < 1 else None
    context["runs"] = [
        ListedRun(r.id, _shorten(r.question), r.created_ist, STATUS_WORDS.get(r.status, r.status))
        for r in list_runs(state.pool, user.id)
    ]
    return templates.TemplateResponse(request, "home.html", context, status_code=status_code)


def _shorten(question: str) -> str:
    if len(question) <= LISTED_QUESTION_CHARS:
        return question
    return question[:LISTED_QUESTION_CHARS] + "\u2026"


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_ready)])
def home(request: Request, user: Annotated[User, Depends(current_user)]):
    return _home(request, user)


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


def _start_question(request: Request, user: User, question: str):
    """Incisive's order (`ask.rs` `run_ask`): the input limits, then the patient-details
    check (inside `create_run`, on the question exactly as received), then the run. The
    question is only in the POST body and, when the page is shown again, in that response's
    question box: never in a URL, a redirect, a log line, or storage unless it passed."""
    state = request.app.state
    settings = state.settings
    if state.gemini_caller is None:
        log.info("ask kind=refused reason=no_key")
        return _message(request, NO_KEY, 503)
    if not question.strip():
        log.info("ask kind=rejected reason=empty")
        return _home(request, user, 400, ask_message=EMPTY_QUESTION)
    # Counted as Incisive counts it (characters as typed), with a form's CR LF line break as
    # the one character a textarea's maxlength counts.
    if len(stored_question(question)) > MAX_QUESTION_CHARS:
        log.info("ask kind=rejected reason=too_long")
        return _home(request, user, 400, question=question, ask_message=TOO_LONG)
    if runs_left(state.pool, settings.gemini_model, settings.gemini_rpd) < 1:
        log.info("ask kind=refused reason=daily_limit")
        return _home(request, user, 429, question=question,
                     ask_message=DAILY_LIMIT.format(reset=reset_time_ist(state.pool)))
    try:
        run_id = create_run(state.pool, user.id, question, settings.gemini_model)
    except QuestionBlocked as exc:  # logged by create_run: categories and counts only
        found = [(label, n) for label, (_, n) in zip(exc.outcome.labels, exc.outcome.categories,
                                                       strict=True)]
        return _home(request, user, 400, question=question, ask_message=exc.outcome.message,
                     ask_found=found)
    except ActiveRunExists as exc:
        log.info("ask kind=refused reason=active")
        link = f"/runs/{exc.run_id}" if exc.own and exc.run_id is not None else None
        return _home(request, user, 409, question=question, ask_message=ACTIVE_RUN,
                     ask_link=link)
    log.info("ask kind=started run_id=%s", run_id)
    try:
        state.run_starter.start(run_id)
    except Exception as exc:  # noqa: BLE001 - a run that cannot start must not stay active
        log.error("ask kind=start_failed error=%s", type(exc).__name__)
        finish_run(state.pool, run_id, "failed", "internal", None, None)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@router.post(
    "/ask",
    response_class=HTMLResponse,
    dependencies=[Depends(refuse_cross_site_ask), Depends(require_ready)],
)
def ask(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    question: Annotated[str, Form()] = "",
):
    return _start_question(request, user, question)


@router.get("/ask", dependencies=[Depends(require_ready), Depends(current_user)])
def ask_get():
    """Back to the question box (as `GET /search`). A question in the URL is never read."""
    return RedirectResponse("/", status_code=303)


def _own_run(request: Request, user: User, run_id: str) -> RunView | None:
    """The run when `run_id` is a well-formed id of one of this user's runs; else None, the
    same for a malformed id, an unknown one and another user's (nothing tells them apart)."""
    if not RUN_ID.fullmatch(run_id):
        return None
    return get_run(request.app.state.pool, uuid.UUID(run_id), user.id)


@dataclass(frozen=True)
class ShownStudy:
    pmid: str
    title: str
    facts: str
    why: str
    pub_types: tuple[str, ...]
    retracted: bool
    paragraphs: list[str]


def _shown_study(s: SelectedView) -> ShownStudy:
    """"Journal · Year · label" (the model's design label), and the abstract as paragraphs."""
    p = s.paper
    parts = [p.journal or "" if p else "", str(p.year) if p and p.year is not None else "",
             s.design]
    abstract = p.abstract if p and p.abstract else ""
    return ShownStudy(
        pmid=s.pmid,
        title=s.title or f"PMID {s.pmid}",
        facts=" · ".join(x for x in parts if x),
        why=s.why,
        pub_types=tuple(p.pub_types) if p else (),
        retracted=bool(p and p.retracted),
        paragraphs=[x.strip() for x in abstract.split("\n\n") if x.strip()],
    )


def _outcome_line(request: Request, v: RunView) -> str | None:
    if v.status == "stopped":
        return STOPPED
    message = end_message(v)
    if message is not None or v.status != "failed":
        return message
    if v.error_kind == "daily_limit":
        return DAILY_LIMIT.format(reset=reset_time_ist(request.app.state.pool))
    return KIND_MESSAGES.get(v.error_kind or "", INTERNAL_ERROR)


@router.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=[Depends(require_ready)])
def run_page(request: Request, run_id: str, user: Annotated[User, Depends(current_user)]):
    v = _own_run(request, user, run_id)
    if v is None:
        return _message(request, RUN_NOT_FOUND, 404)
    active = v.status in ("queued", "running")
    context = {
        "run": v,
        "active": active,
        "question_paragraphs": [x.strip() for x in v.question.split("\n") if x.strip()],
        "outcome": None if active else _outcome_line(request, v),
        "selected": [_shown_study(s) for s in v.selected],
        "trail": [f"{e.source}: {e.query} ({e.hits:,} found)" for e in v.searches],
    }
    return templates.TemplateResponse(request, "run.html", context)


@router.post(
    "/runs/{run_id}/again",
    response_class=HTMLResponse,
    dependencies=[Depends(refuse_cross_site_ask), Depends(require_ready)],
)
def run_again(request: Request, run_id: str, user: Annotated[User, Depends(current_user)]):
    """A new run of the stored question: it never goes through the browser again."""
    v = _own_run(request, user, run_id)
    if v is None:
        return _message(request, RUN_NOT_FOUND, 404)
    return _start_question(request, user, v.question)


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
