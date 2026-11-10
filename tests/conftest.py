import os

import psycopg
import pytest

from pubmedgemini.db import open_pool

SKIP_MESSAGE = "TEST_DATABASE_URL is not set; database tests need a Postgres server"


@pytest.fixture
def pool():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip(SKIP_MESSAGE)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
    p = open_pool(url)
    p.wait(timeout=10)
    try:
        yield p
    finally:
        p.close()
