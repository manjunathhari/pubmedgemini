import logging
import os
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
from pydantic import SecretStr

from pubmedgemini import web
from pubmedgemini.config import Settings, load_settings
from pubmedgemini.db import is_healthy, open_pool
from pubmedgemini.gemini.caller import ModelCaller
from pubmedgemini.gemini.client import GeminiClient, GeminiError
from pubmedgemini.gemini.quota import (
    PacerBook,
    estimate_tokens,
    finish_attempt,
    record_attempt,
)
from pubmedgemini.identity import ensure_owner
from pubmedgemini.lit.cache import prune as prune_paper_cache
from pubmedgemini.lit.europepmc import EuropePmc
from pubmedgemini.lit.http import LitClient, NcbiPacer, ncbi_min_interval
from pubmedgemini.lit.pubmed import PubMed
from pubmedgemini.lit.tools import LitTools
from pubmedgemini.logging_setup import configure_logging
from pubmedgemini.migrate import apply_migrations
from pubmedgemini.runner import RunKeys, RunStarter, ThreadStarter, run_one
from pubmedgemini.runs import finish_run, stop_unfinished
from pubmedgemini.web import NotReady

log = logging.getLogger("pubmedgemini.main")
lit_log = logging.getLogger("pubmedgemini.lit")
gemini_log = logging.getLogger("pubmedgemini.gemini")
runs_log = logging.getLogger("pubmedgemini.runs")

# After a failed start-up attempt, requests get 503 at once for this long instead of each
# waiting for the pool's connect timeout.
RETRY_BACKOFF_SECONDS = 5.0

STATIC_DIR = Path(__file__).parent / "static"

# F7 /key: the smallest request that proves a key works (about 5 output tokens).
KEY_CHECK_BODY = {
    "contents": [{"role": "user", "parts": [{"text": "Reply with the word OK."}]}],
    "generationConfig": {"maxOutputTokens": 5},
}
KEY_CHECK_TIMEOUT_SECONDS = 20.0
# Review Important 3 (18 Nov): a doctor may check at most this many keys in this many minutes.
KEY_CHECKS_PER_WINDOW = 5
KEY_CHECK_WINDOW_MINUTES = 10


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

    def open_gemini(app: FastAPI) -> None:
        """F7: no Gemini key on the server. Each doctor's own key comes from their cookie
        (keycookie.py), is handed to their run in memory (RunKeys), and a client, a caller and
        that doctor's pacer are put together for the run alone (`run_question`)."""
        gemini_log.info(
            "gemini own_keys=true model=%s rpm=%d tpm=%d rpd=%d max_active_runs=%d",
            settings.gemini_model, settings.gemini_rpm, settings.gemini_tpm, settings.gemini_rpd,
            settings.max_active_runs,
        )
        app.state.pacers = PacerBook(settings.gemini_rpm, settings.gemini_tpm, sleep=gemini_sleep)
        app.state.run_keys = RunKeys()
        secret = settings.key_cookie_secret_bytes()
        if secret is None:  # dev and tests only: compose makes it required in production
            secret = os.urandom(32)
        gemini_log.info("gemini key_cookie_secret=%s",
                        "set" if settings.key_cookie_secret is not None else "random")
        app.state.key_secret = secret

    def caller_for(user_id: int, key: SecretStr) -> tuple[ModelCaller, GeminiClient]:
        client = GeminiClient(key, settings.gemini_model, gemini_transport)
        caller = ModelCaller(client, app.state.pacers.get(user_id), app.state.pool,
                             settings.gemini_model, sleep=gemini_sleep, user_id=user_id)
        return caller, client

    def check_key(user_id: int, key: SecretStr) -> str:
        """F7 /key: one tiny call on the doctor's key before it is saved. Returns `ok` or the
        GeminiError kind. Counted on that doctor's day (step `key_check`). At most
        KEY_CHECKS_PER_WINDOW checks in KEY_CHECK_WINDOW_MINUTES per doctor (`too_many`, no call;
        review Important 3), and each waits on that doctor's pacer like a run's call."""
        with app.state.pool.connection() as conn:
            recent = conn.execute(
                "SELECT count(*) FROM model_calls WHERE user_id = %s AND step = 'key_check' "
                "AND called_at > now() - make_interval(mins => %s)",
                (user_id, KEY_CHECK_WINDOW_MINUTES)).fetchone()[0]
        if recent >= KEY_CHECKS_PER_WINDOW:
            return "too_many"
        if not app.state.pacers.get(user_id).wait(estimate_tokens(KEY_CHECK_BODY),
                                                  clock() + KEY_CHECK_TIMEOUT_SECONDS):
            return "pacer_busy"
        client = GeminiClient(key, settings.gemini_model, gemini_transport)
        call_id = record_attempt(app.state.pool, model=settings.gemini_model, run_id=None,
                                 step="key_check", attempt=1, user_id=user_id)
        started = clock()
        kind, usage = "ok", None
        try:
            usage = client.generate(KEY_CHECK_BODY, KEY_CHECK_TIMEOUT_SECONDS).usage
        except GeminiError as e:
            kind = e.kind
        finally:
            client.close()
        try:
            finish_attempt(app.state.pool, call_id, status_kind=kind, usage=usage,
                           latency_ms=int((clock() - started) * 1000))
        except psycopg.Error:
            gemini_log.warning("gemini kind=count_not_finished")
        return kind

    def run_question(run_id) -> None:
        handed = app.state.run_keys.take(run_id)
        if handed is None:  # the key went with a restart, or never came: nothing is sent
            if finish_run(app.state.pool, run_id, "failed", "key_missing", None, None):
                runs_log.info("run kind=finished run_id=%s status=failed error_kind=key_missing",
                              run_id)
            return
        user_id, key = handed
        try:
            caller, client = caller_for(user_id, key)
        except Exception as e:  # noqa: BLE001 - the run must not stay queued, holding a slot
            runs_log.error("run kind=internal error=%s", type(e).__name__)
            finish_run(app.state.pool, run_id, "failed", "internal", None, None)
            return
        try:
            run_one(run_id, pool=app.state.pool, tools=app.state.lit, caller=caller,
                    model=settings.gemini_model)
        finally:
            client.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = open_pool(settings.database_url)
        app.state.ready = False
        lit_client = open_literature(app)
        open_gemini(app)
        starter = run_starter if run_starter is not None else ThreadStarter()
        if getattr(starter, "run", None) is None:
            starter.run = run_question
        app.state.run_starter = starter
        try_start_up(app)
        try:
            yield
        finally:
            lit_client.close()
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
    app.state.caller_for = caller_for
    app.state.check_key = check_key
    app.state.run_question = run_question
    web.register(app)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app(load_settings())
