import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pubmedgemini import intro
from pubmedgemini.answer_page import answer_view, fmt_pct, pill_class
from pubmedgemini.db import is_healthy
from pubmedgemini.gemini.quota import reset_time_ist, runs_left
from pubmedgemini.identity import NotSetUp, NotSignedIn, User, current_user
from pubmedgemini.lit.query import query_rejected_message
from pubmedgemini.lit.text import clean_text, drop_invisible
from pubmedgemini.runs import (
    ActiveRunExists,
    QuestionBlocked,
    RunView,
    SelectedView,
    create_run,
    daily_question_counts,
    end_message,
    finish_run,
    get_run,
    list_runs,
    stored_question,
)
from pubmedgemini.search import BothUnreachable, PageResult, QueryRejected, run_page_search
from pubmedgemini.writer.answer import AnswerInvalid, read_body

log = logging.getLogger("pubmedgemini.web")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _pct(x: object) -> str:
    """A position computed by code (`answer_page`), as the number an SVG attribute takes: one
    decimal, a trailing ".0" dropped. Anything but a number is refused, so no model text can
    ever reach an attribute through this filter."""
    if isinstance(x, bool) or not isinstance(x, (float, int)):
        raise TypeError("pct takes a number")
    return fmt_pct(float(x))


templates.env.filters["pct"] = _pct
templates.env.filters["pill"] = pill_class  # a fixed class word for a certainty
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
    "run_call_cap": (
        "Gemini stayed busy, so this run used up its share of today's free calls. "
        "Please run the question again."
    ),
    # Owner's answer Q6 (F3a plan, 11 Nov 2026): the selected studies' records could not be
    # fetched (Incisive ends the run `failed` with `lit` and has no message of its own).
    "lit": (
        "PubMed could not be reached to fetch the selected studies, so no answer could be "
        "written. Please run the question again."
    ),
    **{k: GEMINI_UNAVAILABLE for k in ("overloaded", "timeout", "network", "http",
                                       "request_refused", "not_found", "too_large", "parse")},
}
# ---------- the Introduction and the early summary (F3c, mock f3c v1) ----------

INTRO_WORDS = {"heading": intro.HEADING, "tag_running": intro.TAG_RUNNING,
               "tag_finished": intro.TAG_FINISHED, "closing": intro.CLOSING,
               "background": intro.BACKGROUND}
EARLY_NOTE = (
    "The summary was written first. Its citations have been checked against the selected "
    "studies. The outcomes, the trials and the check that the parts agree are still being "
    "written, so this summary may still change or be withdrawn. The full answer replaces it on "
    "this page."
)
EARLY_KEYS_HELD = "The key numbers are shown with the full answer."  # O3 (b)
_STILL_ALWAYS = ("why this conclusion (every estimate with a 95% CI, outcome by outcome)",
                 "the counselling dashboard", "the trials", "conflicting evidence")
_STILL_CONTEXT = ("applicability", "what would change this answer")


def still_being_written(context: bool) -> str:
    """The muted line naming what the early summary does not hold yet."""
    items = [*_STILL_ALWAYS, *(() if context else _STILL_CONTEXT), "the references"]
    return "Still being written: " + ", ".join(items[:-1]) + ", and " + items[-1] + "."


STATUS_WORDS = {"queued": "Waiting", "running": "Running", "done": "Done", "partial": "Partial",
                "failed": "Failed", "stopped": "Stopped"}
LISTED_QUESTION_CHARS = 120
LIBRARY_LIMIT = 50
ACTIVITY_DAYS = 14
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


def _message(request: Request, text: str, status_code: int,
             user: User | None = None) -> HTMLResponse:
    """A message page. With a signed-in, set-up `user` the menu shows (no item current);
    without one (no sign-in, not set up, start-up or database failures) it does not."""
    context: dict = {"message": text}
    if user is not None:
        context["nav"] = ""
    return templates.TemplateResponse(request, "message.html", context, status_code=status_code)


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
    context = {"email": user.email, "nav": "ask", "ask_enabled": enabled, "question": question,
               "ask_message": ask_message, "ask_found": ask_found, "ask_link": ask_link}
    if enabled:
        left = runs_left(state.pool, settings.gemini_model, settings.gemini_rpd)
        context["runs_left"] = left
        context["reset_time"] = reset_time_ist(state.pool) if left < 1 else None
    return templates.TemplateResponse(request, "home.html", context, status_code=status_code)


def _shorten(question: str) -> str:
    if len(question) <= LISTED_QUESTION_CHARS:
        return question
    return question[:LISTED_QUESTION_CHARS] + "\u2026"


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_ready)])
def home(request: Request, user: Annotated[User, Depends(current_user)]):
    return _home(request, user)


@router.get("/library", response_class=HTMLResponse, dependencies=[Depends(require_ready)])
def library(request: Request, user: Annotated[User, Depends(current_user)]):
    """The signed-in user's own questions, newest first (up to LIBRARY_LIMIT)."""
    runs = [
        ListedRun(r.id, _shorten(r.question), r.created_ist, STATUS_WORDS.get(r.status, r.status))
        for r in list_runs(request.app.state.pool, user.id, LIBRARY_LIMIT)
    ]
    return templates.TemplateResponse(request, "library.html", {"nav": "library", "runs": runs})


def _day_label(d: date) -> str:
    """"Wed 11 Nov": the day number without a leading zero."""
    return f"{d:%a} {d.day} {d:%b}"


@router.get("/activity", response_class=HTMLResponse, dependencies=[Depends(require_ready)])
def activity(request: Request, user: Annotated[User, Depends(current_user)]):
    """The signed-in user's questions per India-time day, the last ACTIVITY_DAYS days."""
    counts = daily_question_counts(request.app.state.pool, user.id, datetime.now(UTC),
                                   ACTIVITY_DAYS)
    days = [(_day_label(d), n) for d, n in counts]
    return templates.TemplateResponse(request, "activity.html", {"nav": "activity", "days": days})


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
    context = {"email": user.email, "nav": "ask", "query": text}
    try:
        result = run_page_search(request.app.state.lit, text)
    except QueryRejected as exc:
        log.info("search kind=rejected reason=%s", exc.kind)
        context["rejected"] = query_rejected_message(exc.kind, exc.source)
        return templates.TemplateResponse(request, "results.html", context, status_code=400)
    except BothUnreachable:
        log.info("search kind=unavailable")
        return _message(request, BOTH_DOWN, 503, user)
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
        return _message(request, NO_KEY, 503, user)
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


def _answer_view(v: RunView) -> tuple[dict[str, Any] | None, bool]:
    """What the page draws of the stored answer (`answer_page.answer_view`), read only through
    `read_body`, and whether a stored answer could not be read. An answer that cannot be read
    is logged by its kind alone and the page says so; no body reaches the page or the log."""
    if v.answer is None:
        return None, False
    try:
        a = read_body(v.answer)
    except AnswerInvalid as exc:
        log.warning("run page answer kind=%s", exc.kind)
        return None, True
    return answer_view(a), False


def _intro_paragraphs(v: RunView) -> list[str]:
    """The Introduction's paragraphs (Incisive `paragraphs`), cleaned again of NUL, bidi and
    zero-width characters; none when it is not `done`."""
    if not v.intro:
        return []
    return intro.paragraphs(drop_invisible(clean_text(v.intro)))


def _early_view(v: RunView) -> dict[str, Any] | None:
    """The early summary, only while the run works, once the summary part is in, read only
    through `read_body` (as the stored answer). Unreadable: ignored, logged by kind alone."""
    e = v.early
    if not isinstance(e, dict) or e.get("summary") is not True:
        return None
    try:
        a = read_body(e.get("answer"))
        view = answer_view(a)
    except AnswerInvalid as exc:
        log.warning("run page early kind=%s", exc.kind)
        return None
    except Exception:  # noqa: BLE001 - nothing of the body reaches the log
        log.warning("run page early kind=internal")
        return None
    if not view.get("answer"):
        log.warning("run page early kind=invalid")
        return None
    context = e.get("context") is True
    return {"view": view, "context": context, "note": EARLY_NOTE, "keys_held": EARLY_KEYS_HELD,
            "still": still_being_written(context)}


# ---------- the completeness check box (F4a, mock f4a-completeness-v3) ----------

# The page keeps its 5 s refresh after the answer is stored only while the check is pending or
# running, and at most this long after the check row was created (the database's clock). Past
# it a row that is still pending or running is shown as a check that could not be completed.
CHECK_WAIT_SECONDS = 60
CHECK_NAME = "Completeness check"
CHECK_STEPS = ("Pending", "Running", "Done")
CHECK_NOT_COMPLETED = "The check could not be completed. The answer is unchanged."
_NOW, _WAS, _NOT_YET = "st now", "st was", "st"
_CHECK_CLASSES = {
    "pending": (_NOW, _NOT_YET, _NOT_YET),
    "running": (_WAS, _NOW, _NOT_YET),
    "done": (_WAS, _WAS, _NOW),
    "failed": (_WAS, _WAS, _NOT_YET),  # no dark step: it never claims Done
}


def _check_line(found: int, total: int, match: int, names: int) -> str:
    numbers = f"{found} of {total} numbers found" if total else "No numbers to check"
    trials = f"{match} of {names} trial names match" if names else "No trial names to check"
    return f"{numbers} · {trials}"


def _check_box(v: RunView) -> dict | None:
    """What the completeness check's box shows, or None for a run with no check row. Only
    words fixed here and the four stored counts: nothing of the answer. A pending or running
    row older than CHECK_WAIT_SECONDS, and anything not understood, is shown as failed; the
    row itself is never changed by a page view. `wait` is True only while the page should
    keep refreshing for the check."""
    c = v.check
    if c is None:
        return None
    counts = (c.numbers_found, c.numbers_total, c.names_match, c.names_total)
    shown = "failed"
    if c.status == "done" and all(isinstance(n, int) for n in counts):
        shown = "done"
    elif c.status in ("pending", "running") and c.age_s < CHECK_WAIT_SECONDS:
        shown = c.status
    line = None
    if shown == "done":
        line = _check_line(*counts)
    elif shown == "failed":
        line = CHECK_NOT_COMPLETED
    return {"name": CHECK_NAME, "steps": list(zip(CHECK_STEPS, _CHECK_CLASSES[shown],
                                                  strict=True)),
            "line": line, "wait": shown in ("pending", "running")}


@router.get("/runs/{run_id}", response_class=HTMLResponse, dependencies=[Depends(require_ready)])
def run_page(request: Request, run_id: str, user: Annotated[User, Depends(current_user)]):
    v = _own_run(request, user, run_id)
    if v is None:
        return _message(request, RUN_NOT_FOUND, 404, user)
    active = v.status in ("queued", "running")
    view, unreadable = _answer_view(v)
    wide = view is not None and not active
    check = _check_box(v) if wide else None  # F4a: on the answer page only
    context = {
        "nav": "library",
        "run": v,
        "active": active,
        "question_paragraphs": [x.strip() for x in v.question.split("\n") if x.strip()],
        "outcome": None if active else _outcome_line(request, v),
        "view": view,
        "unreadable": unreadable,
        # The answer layout (N1 A): a finished run with a readable stored answer. Every other
        # run page is as before.
        "wide": wide,
        # F4a: the completeness check's box, and the refresh: while the run works, or while
        # the check is pending or running and under its cap.
        "check": check,
        "refresh": active or bool(check and check["wait"]),
        "status_word": STATUS_WORDS.get(v.status, v.status),
        "selected": [_shown_study(s) for s in v.selected],
        "trail": [f"{e.source}: {e.query} ({e.hits:,} found)" for e in v.searches],
        # F3c: the Introduction (none for old runs, a failed or a pending one) and, while the
        # run works, the early summary.
        "intro_paragraphs": _intro_paragraphs(v),
        "intro_words": INTRO_WORDS,
        "early": _early_view(v) if active else None,
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
        return _message(request, RUN_NOT_FOUND, 404, user)
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
