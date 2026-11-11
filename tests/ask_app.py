"""A question app over fakes for the real-Uvicorn leak tests in tests/test_logging.py: the
replayed literature fixtures and a scripted Gemini (no network). Runs start on the real
background thread. Not collected by pytest (no test_ prefix).

ASK_APP_MODE picks the Gemini script: `happy` (a full run with marked texts, see
`leak_script`, that writes an answer from `leak_parts`) or `crash` (the first Gemini request raises an unexpected error whose text
holds the whole request body, so the question)."""

import os

import httpx
from lit_fakes import FakeUpstream, europepmc_routes, pubmed_routes
from test_logging import leak_parts, leak_script
from writer_fakes import RunGemini

from pubmedgemini.config import load_settings
from pubmedgemini.main import create_app


def _explode(request: httpx.Request) -> httpx.Response:
    raise RuntimeError(f"unexpected failure for {request.content.decode()}")


def build():
    mode = os.environ.get("ASK_APP_MODE", "happy")
    script = leak_script() if mode == "happy" else [_explode]
    gemini = RunGemini(script, leak_parts())  # the writer's parts carry mq2
    upstream = FakeUpstream(pubmed_routes() | europepmc_routes())
    return create_app(load_settings(), lit_transport=upstream.transport, ncbi_gap_seconds=0,
                      gemini_transport=gemini.transport, gemini_sleep=lambda _s: None)
