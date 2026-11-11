import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import psycopg
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool, PoolTimeout

from pubmedgemini import web
from pubmedgemini.config import Settings, load_settings
from pubmedgemini.db import is_healthy, open_pool
from pubmedgemini.gemini.caller import ModelCaller
from pubmedgemini.gemini.client import GeminiClient
from pubmedgemini.gemini.quota import GeminiPacer
from pubmedgemini.identity import ensure_owner
from pubmedgemini.lit.cache import prune as prune_paper_cache
from pubmedgemini.lit.europepmc import EuropePmc
from pubmedgemini.lit.http import LitClient, NcbiPacer, ncbi_min_interval
from pubmedgemini.lit.pubmed import PubMed
from pubmedgemini.lit.tools import LitTools
from pubmedgemini.logging_setup import configure_logging
from pubmedgemini.migrate import apply_migrations
from pubmedgemini.runner import RunStarter, ThreadStarter, run_one
from pubmedgemini.runs import stop_unfinished
from pubmedgemini.web import NotReady

log = logging.getLogger("pubmedgemini.main")
lit_log = logging.getLogger("pubmedgemini.lit")
gemini_log = logging.getLogger("pubmedgemini.gemini")
runs_log = logging.getLogger("pubmedgemini.runs")

# After a failed start-up attempt, requests get 503 at once for this long instead of each
# waiting for the pool's connect timeout.
RETRY_BACKOFF_SECONDS = 5.0

STATIC_DIR = Path(__file__).parent / "static"


def _failure_kind(pool: ConnectionPool, exc: psycopg.Error) -> str:
    """db_unreachable if no connection can be opened; else migration_failed.

    Decided by trying a connection, not by exception class: a cancelled query, a lock
    timeout or a serialization failure is an OperationalError on a reachable database.
    """
    if isinstance(exc, PoolTimeout) or not is_healthy(pool):
        return "db_unreachable"
    return "migration_failed"


def create_app(
    settings: Settings,
    clock: Callable[[], float] = time.monotonic,
    lit_transport: httpx.BaseTransport | None = None,
    ncbi_gap_seconds: float | None = None,
    gemini_transport: httpx.BaseTransport | None = None,
    gemini_sleep: Callable[[float], None] = time.sleep,
    run_starter: RunStarter | None = None,
) -> FastAPI:
    """The app. Tests pass fake transports (no network), an NCBI gap of 0, a sleep that does
    not wait, and an InlineStarter so a run happens inside the request."""
    configure_logging()
    state_lock = threading.Lock()

    def start_up_work(app: FastAPI) -> None:
        apply_migrations(app.state.pool, settings.migrations_dir)
        ensure_owner(app.state.pool, settings.owner_email)
        prune_once(app)
        # A run still queued or running belonged to the previous process (one process).
        runs_log.info("runs stopped_at_start=%d", stop_unfinished(app.state.pool))
        app.state.ready = True

    def prune_once(app: FastAPI) -> None:
        """Old cached papers go at each start (the app restarts on every deploy). A failed
        prune is logged and start-up carries on: the cache is not needed to serve."""
        try:
            pruned = prune_paper_cache(app.state.pool)
        except psycopg.Error as exc:
            sqlstate = getattr(exc, "sqlstate", None) or "none"
            log.warning(
                "paper_cache kind=prune_failed error=%s sqlstate=%s", type(exc).__name__, sqlstate
            )
        else:
            log.info("paper_cache pruned=%d", pruned)

    def try_start_up(app: FastAPI) -> bool:
        """Run the start-up work once. Logs a fixed kind on failure, never the exception text."""
        try:
            start_up_work(app)
        except psycopg.Error as exc:
            kind = _failure_kind(app.state.pool, exc)
            # Safe fields only: the class name and SQLSTATE are fixed codes; the message is
            # libpq or server text and can hold the host, role or SQL.
            sqlstate = getattr(exc, "sqlstate", None) or "none"
            log.warning("kind=%s error=%s sqlstate=%s", kind, type(exc).__name__, sqlstate)
        else:
            app.state.failure = None
            return True
        app.state.failure = kind
        app.state.next_attempt_at = clock() + RETRY_BACKOFF_SECONDS
        return False

    def open_literature(app: FastAPI) -> LitClient:
        """One NCBI pacer and one client for the whole process (one Uvicorn worker)."""
        key = settings.ncbi_api_key.get_secret_value() if settings.ncbi_api_key else None
        gap = ncbi_gap_seconds if ncbi_gap_seconds is not None else ncbi_min_interval(bool(key))
        client = LitClient(lit_transport, pacer=NcbiPacer(gap))
        pubmed = PubMed(client, key, settings.ncbi_email)
        app.state.lit = LitTools(pubmed, EuropePmc(client), app.state.pool)
        # Whether a key is set, never the key or the email (Incisive `main.rs`).
        lit_log.info("literature ncbi_api_key=%s", "true" if key else "false")
        return client

    def open_gemini(app: FastAPI) -> GeminiClient | None:
        """With a key: one client, one pacer and one caller for the whole process. Without
        one, questions are off and the rest of the app works."""
        key = settings.gemini_api_key
        gemini_log.info(
            "gemini api_key=%s model=%s rpm=%d tpm=%d rpd=%d", "true" if key else "false",
            settings.gemini_model, settings.gemini_rpm, settings.gemini_tpm, settings.gemini_rpd,
        )
        if key is None:
            app.state.gemini_caller = None
            return None
        client = GeminiClient(key, settings.gemini_model, gemini_transport)
        pacer = GeminiPacer(settings.gemini_rpm, settings.gemini_tpm, sleep=gemini_sleep)
        app.state.gemini_caller = ModelCaller(
            client, pacer, app.state.pool, settings.gemini_model, sleep=gemini_sleep
        )
        return client

    def run_question(run_id) -> None:
        run_one(run_id, pool=app.state.pool, tools=app.state.lit,
                caller=app.state.gemini_caller, model=settings.gemini_model)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = open_pool(settings.database_url)
        app.state.ready = False
        lit_client = open_literature(app)
        gemini_client = open_gemini(app)
        starter = run_starter if run_starter is not None else ThreadStarter()
        if getattr(starter, "run", None) is None:
            starter.run = run_question
        app.state.run_starter = starter
        try_start_up(app)
        try:
            yield
        finally:
            lit_client.close()
            if gemini_client is not None:
                gemini_client.close()
            app.state.pool.close()

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.ready = False
    app.state.failure = None
    app.state.next_attempt_at = 0.0

    def ensure_ready() -> None:
        """Return if start-up has finished; else retry it, at most once per back-off window.

        Raises NotReady at once during the window, or while another request's attempt runs.
        """
        if app.state.ready:
            return
        if clock() < app.state.next_attempt_at:
            raise NotReady(app.state.failure)
        if not state_lock.acquire(blocking=False):
            raise NotReady(app.state.failure)
        try:
            if app.state.ready:
                return
            if clock() < app.state.next_attempt_at:
                raise NotReady(app.state.failure)
            if not try_start_up(app):
                raise NotReady(app.state.failure)
        finally:
            state_lock.release()

    app.state.ensure_ready = ensure_ready
    web.register(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app(load_settings())
