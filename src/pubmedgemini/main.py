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

log = logging.getLogger("pubmedgemini.main")


def create_app(settings: Settings) -> FastAPI:
    state_lock = threading.Lock()

    def start_up_work(app: FastAPI) -> None:
        apply_migrations(app.state.pool, settings.migrations_dir)
        ensure_owner(app.state.pool, settings.owner_email)
        app.state.ready = True

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.pool = open_pool(settings.database_url)
        app.state.ready = False
        try:
            start_up_work(app)
        except psycopg.Error:
            log.warning("kind=db_unreachable")
        try:
            yield
        finally:
            app.state.pool.close()

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.ready = False

    def ensure_ready() -> None:
        if app.state.ready:
            return
        with state_lock:
            if not app.state.ready:
                start_up_work(app)

    app.state.ensure_ready = ensure_ready
    web.register(app)
    return app


app = create_app(load_settings())
