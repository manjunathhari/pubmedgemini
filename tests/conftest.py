import os

# main.py builds the module-level app at import time, so the environment must hold values
# (dummy ones; nothing connects until the app starts).
os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/x")
os.environ.setdefault("OWNER_EMAIL", "owner@example.com")

import psycopg
import pytest
from fastapi.testclient import TestClient

from pubmedgemini.config import Settings
from pubmedgemini.db import open_pool
from pubmedgemini.main import create_app

SKIP_MESSAGE = "TEST_DATABASE_URL is not set; database tests need a Postgres server"
UNREACHABLE_URL = "postgresql://u:p@127.0.0.1:1/x"
OWNER = "owner@example.com"


@pytest.fixture
def db_url():
    """The test database URL, with the schema dropped and recreated."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(SKIP_MESSAGE)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    return url


@pytest.fixture
def pool(db_url):
    p = open_pool(db_url)
    p.wait(timeout=10)
    try:
        yield p
    finally:
        p.close()


@pytest.fixture
def settings_prod(db_url):
    return Settings(app_env="prod", database_url=db_url, owner_email=OWNER)


@pytest.fixture
def client_prod(settings_prod):
    with TestClient(create_app(settings_prod)) as c:
        yield c


@pytest.fixture
def client_dev(db_url):
    settings = Settings(app_env="dev", database_url=db_url, owner_email=OWNER)
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def client_db_down():
    settings = Settings(app_env="prod", database_url=UNREACHABLE_URL, owner_email=OWNER)
    with TestClient(create_app(settings)) as c:
        yield c
