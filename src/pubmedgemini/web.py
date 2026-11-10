import logging
from pathlib import Path
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from pubmedgemini.db import is_healthy
from pubmedgemini.identity import NotSetUp, NotSignedIn, User, current_user

log = logging.getLogger("pubmedgemini.web")

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
router = APIRouter()

NOT_SIGNED_IN = "You are not signed in."
NOT_SET_UP = (
    "Your Google account is allowed in, but you haven't been set up yet. "
    "Ask the owner to add you."
)
DB_DOWN = "The database could not be reached. Please try again in a minute."


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


@router.get("/healthz")
def healthz(request: Request):
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

    @app.exception_handler(psycopg.Error)
    def database_error(request: Request, exc: psycopg.Error):
        # Fixed kind only: the exception text can hold connection details.
        log.warning("kind=db_error")
        return _message(request, DB_DOWN, 503)

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        log.info("request method=%s route=%s status=%s", request.method, route, response.status_code)
        return response
