"""An app whose literature transport raises an unexpected error (not an httpx one) whose
text holds the request URL, so the search text. Used under a real Uvicorn by
tests/test_logging.py. Not collected by pytest (no test_ prefix)."""

import httpx

from pubmedgemini.config import load_settings
from pubmedgemini.main import create_app


def _explode(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"unexpected failure for {request.url}")


def build():
    return create_app(
        load_settings(), lit_transport=httpx.MockTransport(_explode), ncbi_gap_seconds=0
    )
