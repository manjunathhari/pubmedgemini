"""A question app over fakes for the real-Uvicorn leak tests in tests/test_logging.py: the
replayed literature fixtures and a scripted Gemini (no network). Runs start on the real
background thread. Not collected by pytest (no test_ prefix).

ASK_APP_MODE picks the Gemini script: `happy` (a full run with marked texts, see
`leak_script`, that writes an answer from marked parts), `fallback` (a blocked part, then the
one piece), `db_refused` (an answer the database refuses; see `test_logging.scenario`) or `crash` (the first Gemini request raises an unexpected error whose text
holds the whole request body, so the question). F3c: the Introduction answers with marked text
(`test_logging.intro_steps`), and `intro_fails` is `happy` with an Introduction whose one
attempt fails with a marked Google message."""

import os

import httpx
from lit_fakes import FakeUpstream, europepmc_routes, pubmed_routes
from test_logging import intro_steps, marked_upstream, scenario, without_store_cleaning
from writer_fakes import RunGemini

from pubmedgemini.config import load_settings
from pubmedgemini.main import create_app


def _explode(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"unexpected failure for {request.content.decode()}")


def build():
    mode = os.environ.get("ASK_APP_MODE", "happy")
    if mode == "crash":
        script, parts, piece = [_explode], None, ()
    else:
        script, parts, piece = scenario(mode)
    if mode == "db_refused":
        without_store_cleaning()  # a NUL is cleaned before the INSERT otherwise
    gemini = RunGemini(script, parts, piece, intro_steps(mode))  # the replies carry markers
    upstream = marked_upstream() if mode != "crash" else FakeUpstream(
        pubmed_routes() | europepmc_routes())
    return create_app(load_settings(), lit_transport=upstream.transport, ncbi_gap_seconds=0,
                      gemini_transport=gemini.transport, gemini_sleep=lambda _s: None)
