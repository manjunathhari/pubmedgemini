import logging
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI

from pubmedgemini import web
from pubmedgemini.config import Settings, load_settings
from pubmedgemini.db import open_pool
from pubmedgemini.identity import ensure_owner
from pubmedgemini.migrate import apply_migrations
from pubmedgemini.web import NotReady

log = logging.getLogger("pubmedgemini.main")

# After a failed start-up attempt, requests get 503 at once for this long instead of each
# waiting for the pool's connect timeout.
RETRY_BACKOFF_SECONDS = 5.0


def create_app(
    settings: Settings, clock: Callable[[], float] = time.monotonic
) -> FastAPI:
    state_lock = threading.Lock()

    def start_up_work(app: FastAPI) -> None:
        apply_migrations(app.state.pool, settings.migrations_dir)
        ensure_owner(app.state.pool, settings.owner_email)
        app.state.ready = True

    def try_start_up(app: FastAPI) -> bool:
        """Run the start-up work once. Logs a fixed kind on failure, never the exception text."""
        try:
            start_up_work(app)
        except psycopg.OperationalError:  # cannot connect (PoolTimeout is one of these)
            kind = "db_unreachable"
        except psycopg.Error:  # connected, but a migration or the owner insert failed
            kind = "migration_failed"
        else:
            app.state.failure = None
            return True
        log.warning("kind=%s", kind)
        app.state.failure = kind
        app.state.next_attempt_at = clock() + RETRY_BACKOFF_SECONDS
        return False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = open_pool(settings.database_url)
        app.state.ready = False
        try_start_up(app)
        try:
            yield
        finally:
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
    return app


app = create_app(load_settings())
