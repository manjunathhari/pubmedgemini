import logging
import threading
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


def create_app(settings: Settings) -> FastAPI:
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

    def ensure_ready() -> None:
        """Return if start-up has finished; else retry it once. Raises NotReady on failure."""
        if app.state.ready:
            return
        with state_lock:
            if app.state.ready:
                return
            if not try_start_up(app):
                raise NotReady(app.state.failure)

    app.state.ensure_ready = ensure_ready
    web.register(app)
    return app


app = create_app(load_settings())
