"""Test doubles for Gemini: hand-written replies served by a mock transport. No test reaches
the network, and no live reply is recorded (see tests/fixtures/gemini/README.md).

The shapes follow the Flash-Lite probe, which worked against the live API on 10 Nov 2026
(`probes/flash_lite_probe.py` `parse_reply`, `retry_delay_s`; `test_flash_lite_probe.py`
`gemini_resp` and the client tests' error bodies).
"""

import copy
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from pubmedgemini.gemini.client import GEMINI_HOST

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "gemini"
FAKE_KEY = "test-key-not-real"

Step = httpx.Response | dict | Callable[[httpx.Request], httpx.Response]


def fixture(name: str) -> dict:
    """The parsed JSON of tests/fixtures/gemini/<name>."""
    return json.loads((FIXTURES / name).read_text())


# ---------- reply and error shapes ----------


def reply(
    parts: Sequence[dict], finish: str = "STOP", prompt: int = 1000, out: int = 100,
    thoughts: int = 0,
) -> dict:
    """A 200 generateContent body with one candidate (probe `gemini_resp`)."""
    return {
        "candidates": [
            {"content": {"role": "model", "parts": list(parts)}, "finishReason": finish}
        ],
        "usageMetadata": {
            "promptTokenCount": prompt,
            "candidatesTokenCount": out,
            "thoughtsTokenCount": thoughts,
        },
    }


def call(name: str, args: Any, id: str | None = None, sig: str | None = "c2ln") -> dict:
    """One functionCall part, with a thought signature beside it as Gemini sends."""
    fc: dict[str, Any] = {"name": name, "args": args}
    if id is not None:
        fc["id"] = id
    part: dict[str, Any] = {"functionCall": fc}
    if sig is not None:
        part["thoughtSignature"] = sig
    return part


def RETRY_INFO(delay: str) -> dict:
    return {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}


def QUOTA_FAILURE(quota_id: str) -> dict:
    return {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [{"quotaMetric": "made-up/metric", "quotaId": quota_id}],
    }


def ERROR_INFO(reason: str) -> dict:
    return {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": reason,
        "domain": "googleapis.com",
    }


def error_body(code: int, details: Sequence[dict] = (), status: str = "X",
               message: str = "made-up message") -> dict:
    """Google's error body (`google.rpc.Status` in JSON)."""
    return {"error": {"code": code, "message": message, "status": status,
                      "details": list(details)}}


def error(code: int, status: str = "X", details: Sequence[dict] = (),
          message: str = "made-up message", headers: dict | None = None) -> httpx.Response:
    """An error reply with Google's body."""
    return httpx.Response(
        code, json=error_body(code, details, status, message), headers=headers or {}
    )


# ---------- the transport ----------


class FakeGemini:
    """An httpx.MockTransport for GEMINI_HOST that serves `script` in order and records every
    request in `.sent`. A step is a 200 body (dict), an httpx.Response, or a function of the
    request that returns one or raises an httpx exception. Any other host answers 404.

    Every `role: model` turn sent back must equal a candidate content this fake served
    (thought signatures intact); anything else fails the test with an AssertionError."""

    def __init__(self, script: Sequence[Step]) -> None:
        self.script = list(script)
        self.sent: list[httpx.Request] = []
        self.served: list[dict] = []
        self.transport = httpx.MockTransport(self._handle)

    @property
    def bodies(self) -> list[dict]:
        """The JSON body of each request sent to Gemini."""
        return [json.loads(r.content) for r in self.sent if r.url.host == GEMINI_HOST]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if request.url.host != GEMINI_HOST:
            return httpx.Response(404)
        self._check_echoes(request)
        if not self.script:
            raise AssertionError("FakeGemini: script exhausted")
        step = self.script.pop(0)
        if callable(step) and not isinstance(step, httpx.Response):
            return step(request)
        if isinstance(step, dict):
            self._remember(step)
            return httpx.Response(200, json=step)
        return httpx.Response(step.status_code, headers=step.headers, content=step.content)

    def _remember(self, body: dict) -> None:
        for cand in body.get("candidates") or []:
            content = (cand or {}).get("content")
            if isinstance(content, dict):
                self.served.append(copy.deepcopy(content))

    def _check_echoes(self, request: httpx.Request) -> None:
        try:
            body = json.loads(request.content)
        except ValueError:
            return
        for turn in body.get("contents") or [] if isinstance(body, dict) else []:
            if (
                isinstance(turn, dict)
                and turn.get("role") == "model"
                and not any(_same_turn(turn, s) for s in self.served)
            ):
                raise AssertionError("FakeGemini: a model turn sent back was changed")


def _same_turn(sent: dict, served: dict) -> bool:
    """Equal, allowing only `role: model` added where Google sent none."""
    if "role" not in served:
        served = {"role": "model", **served}
    return sent == served
