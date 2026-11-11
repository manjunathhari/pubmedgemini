"""The Gemini REST client: one `generateContent` attempt (no retries; see `caller.py`).

Request and reply shapes are the Flash-Lite probe's (`probes/flash_lite_probe.py`), which
worked against the live API on 10 Nov 2026. The outbound discipline is F1's `lit/http.py`:

* One allowed host, `generativelanguage.googleapis.com`, https, default port, no userinfo.
  The model id is checked against MODEL_ID_PATTERN, so it can never change the path.
* The key goes only in the `x-goog-api-key` header: never in the URL, the body, an
  exception or a log line.
* No redirect is followed: any 3xx is an `http` error.
* A total deadline per attempt (ATTEMPT_TIMEOUT_SECONDS, Incisive `client.rs`
  `REQUEST_TIMEOUT`, or less when the caller has less), checked once the head is in and after
  every chunk; a reply body is capped at MAX_REPLY_BYTES, an error body is read only up to
  MAX_ERROR_BYTES.
* Every failure is a GeminiError holding a fixed kind and the status: never a URL, a body or
  Google's error `message`, and raised `from None`. `classify` reads only `error.status`,
  `error.details[].@type`, `.reason`, `.retryDelay` and `QuotaFailure.violations[].quotaId`.

Nothing here logs; the caller logs counts and kinds.
"""

import json
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
from pydantic import SecretStr

from pubmedgemini.config import MODEL_ID_PATTERN
from pubmedgemini.lit.text import clean_json

GEMINI_HOST = "generativelanguage.googleapis.com"
API_ROOT = f"https://{GEMINI_HOST}/v1beta"
ATTEMPT_TIMEOUT_SECONDS = 120.0  # Incisive claude-client `client.rs` REQUEST_TIMEOUT
MAX_REPLY_BYTES = 8 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
USER_AGENT = "pubmedgemini/0.1.0"

GeminiErrorKind = Literal[
    "rate_limited",
    "daily_limit",
    "overloaded",
    "key_refused",
    "billing_refused",
    "request_refused",
    "not_found",
    "http",
    "timeout",
    "network",
    "too_large",
    "parse",
]

# A run cannot carry on after one of these: the day's quota is used, or the key is refused.
FATAL_KINDS = frozenset({"daily_limit", "key_refused", "billing_refused"})

# The finish reasons Google lists for a candidate; anything else is logged as `other`.
FINISH_LOG_KINDS = frozenset({
    "FINISH_REASON_UNSPECIFIED",
    "STOP",
    "MAX_TOKENS",
    "SAFETY",
    "RECITATION",
    "LANGUAGE",
    "OTHER",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "MALFORMED_FUNCTION_CALL",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_OTHER",
    "NO_IMAGE",
    "IMAGE_RECITATION",
    "UNEXPECTED_TOOL_CALL",
    "TOO_MANY_TOOL_CALLS",
    "MISSING_THOUGHT_SIGNATURE",
})

_OVERLOADED = (500, 502, 503, 504, 529)  # Incisive retry.rs RETRYABLE_STATUSES, less 429
_DURATION = re.compile(r"\s*(\d+(?:\.\d+)?)s\s*")


class GeminiError(Exception):
    """A Gemini attempt failed. `str()` is the kind, or `kind status`; never a URL, a body,
    Google's message or the key."""

    def __init__(
        self, kind: GeminiErrorKind, status: int | None = None, retry_after: float | None = None
    ) -> None:
        self.kind = kind
        self.status = status
        self.retry_after = retry_after  # seconds Google asked to wait, when it said
        super().__init__(f"{kind} {status}" if status is not None else kind)


@dataclass(frozen=True)
class Usage:
    prompt: int
    output: int
    thoughts: int


@dataclass(frozen=True)
class FunctionCall:
    name: str
    args: object  # as sent: the loop refuses anything but a dict
    id: str | None


@dataclass(frozen=True)
class Reply:
    finish: str  # the first candidate's finishReason, "" when none
    block_reason: str  # promptFeedback.blockReason, "" when none
    content: dict | None  # the candidate's content, unchanged, to send back; None if no parts
    text: str  # the non-thought text parts joined
    calls: tuple[FunctionCall, ...]
    usage: Usage


class ToolSpecLike(Protocol):
    name: str
    description: str
    schema: dict


def finish_log_kind(finish: str) -> str:
    """The finish reason as a log word: lower case if Google lists it, else `other`."""
    return finish.lower() if finish in FINISH_LOG_KINDS else "other"


# ---------- request bodies (pure) ----------


def to_openapi(schema: dict) -> dict:
    """JSON Schema (Incisive's strict style) to Gemini's `parameters` / `responseSchema` form
    (the probe's `to_openapi`): `additionalProperties` dropped, type names upper case,
    `["T", "null"]` as `T` plus `nullable`; descriptions, enums, formats and `required` kept."""
    out: dict[str, Any] = {}
    t = schema.get("type")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        out["type"] = (non_null[0] if non_null else "string").upper()
        if "null" in t:
            out["nullable"] = True
    elif isinstance(t, str):
        out["type"] = t.upper()
    for k in ("description", "enum", "format"):
        if k in schema:
            out[k] = schema[k]
    if "properties" in schema:
        out["properties"] = {k: to_openapi(v) for k, v in schema["properties"].items()}
    if "required" in schema:
        out["required"] = list(schema["required"])
    if "items" in schema:
        out["items"] = to_openapi(schema["items"])
    return out


def function_declarations(tools: Sequence[ToolSpecLike]) -> list[dict]:
    return [
        {"name": t.name, "description": t.description, "parameters": to_openapi(t.schema)}
        for t in tools
    ]


def user_text(text: str) -> dict:
    return {"role": "user", "parts": [{"text": text}]}


def turn_body(system: str, contents: list[dict], declarations: list[dict], max_tokens: int) -> dict:
    """One tool-loop turn (the probe's `build_turn_body`, mode AUTO)."""
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "tools": [{"functionDeclarations": declarations}],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {"maxOutputTokens": int(max_tokens)},
    }


def structured_body(system: str, user: str, schema: dict, max_tokens: int) -> dict:
    """One JSON reply against `schema`, no tools (the probe's `build_structured_body`)."""
    return {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [user_text(user)],
        "generationConfig": {
            "maxOutputTokens": int(max_tokens),
            "responseMimeType": "application/json",
            "responseSchema": to_openapi(schema),
        },
    }


def function_response_turn(replies: Sequence[tuple[str, str | None, bool, str]]) -> dict:
    """`replies` is `(name, call id or None, ok, text)` in call order: one user turn holding
    every functionResponse part, `{"output": text}` or `{"error": text}`; the id is echoed
    when Gemini sent one (the probe's `function_response_turn`)."""
    parts = []
    for name, call_id, ok, text in replies:
        fr: dict[str, Any] = {"name": name, "response": {"output": text} if ok else {"error": text}}
        if call_id:
            fr["id"] = call_id
        parts.append({"functionResponse": fr})
    return {"role": "user", "parts": parts}


# ---------- reading replies (pure) ----------


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def parse_reply(obj: object) -> Reply:
    """The first candidate: text joins the non-`thought` text parts; calls in order; the
    content kept unchanged so it can be sent back (thought signatures intact). Never raises."""
    if not isinstance(obj, dict):
        return Reply("", "", None, "", (), Usage(0, 0, 0))
    u = obj.get("usageMetadata")
    u = u if isinstance(u, dict) else {}
    usage = Usage(
        _count(u.get("promptTokenCount")),
        _count(u.get("candidatesTokenCount")),
        _count(u.get("thoughtsTokenCount")),
    )
    pf = obj.get("promptFeedback")
    block = pf.get("blockReason") if isinstance(pf, dict) else None
    block_reason = block if isinstance(block, str) else ""
    cands = obj.get("candidates")
    cand = cands[0] if isinstance(cands, list) and cands else None
    if not isinstance(cand, dict):
        return Reply("", block_reason, None, "", (), usage)
    fin = cand.get("finishReason")
    finish = fin if isinstance(fin, str) else ""
    content = cand.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts:
        return Reply(finish, block_reason, None, "", (), usage)
    texts: list[str] = []
    calls: list[FunctionCall] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        fc = p.get("functionCall")
        if isinstance(fc, dict):
            name = fc.get("name")
            call_id = fc.get("id")
            calls.append(
                FunctionCall(
                    name=name if isinstance(name, str) else "",
                    args=fc.get("args", {}),
                    id=call_id if isinstance(call_id, str) and call_id else None,
                )
            )
        elif isinstance(p.get("text"), str) and not p.get("thought"):
            texts.append(p["text"])
    return Reply(finish, block_reason, content, "".join(texts), tuple(calls), usage)


def _seconds(value: str) -> float | None:
    try:
        s = float(value)
    except ValueError:
        return None
    return max(0.0, s) if math.isfinite(s) else None


def _details(body: object) -> list[dict]:
    err = body.get("error") if isinstance(body, dict) else None
    details = err.get("details") if isinstance(err, dict) else None
    return [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []


def _type_is(detail: dict, name: str) -> bool:
    t = detail.get("@type")
    return isinstance(t, str) and t.endswith("google.rpc." + name)


def retry_delay_seconds(headers: Mapping[str, str], body: object) -> float | None:
    """The wait Google asked for: `Retry-After` in seconds, else `RetryInfo.retryDelay`
    (such as `"41.5s"`). None when neither is given or readable."""
    value = None
    for k, v in headers.items():
        if k.lower() == "retry-after":
            value = v
            break
    if value:
        s = _seconds(value.strip())
        if s is not None:
            return s
    for d in _details(body):
        if _type_is(d, "RetryInfo"):
            delay = d.get("retryDelay")
            m = _DURATION.fullmatch(delay) if isinstance(delay, str) else None
            if m:
                return float(m.group(1))
    return None


def _quota_ids(body: object) -> list[str]:
    ids: list[str] = []
    for d in _details(body):
        if _type_is(d, "QuotaFailure") and isinstance(d.get("violations"), list):
            for v in d["violations"]:
                q = v.get("quotaId") if isinstance(v, dict) else None
                if isinstance(q, str):
                    ids.append(q)
    return ids


def _reasons(body: object) -> list[str]:
    return [
        d["reason"]
        for d in _details(body)
        if _type_is(d, "ErrorInfo") and isinstance(d.get("reason"), str)
    ]


def classify(status: int, body: object) -> GeminiErrorKind:
    """A fixed kind for a non-2xx reply; Google's `message` is never read."""
    if status == 429:
        return "daily_limit" if any("PerDay" in q for q in _quota_ids(body)) else "rate_limited"
    if status in _OVERLOADED:
        return "overloaded"
    if status in (401, 403):
        return "key_refused"
    if status == 400:
        return "key_refused" if "API_KEY_INVALID" in _reasons(body) else "request_refused"
    if status == 402:
        return "billing_refused"
    if status == 404:
        return "not_found"
    return "http"


# ---------- the client ----------


def _allowed(url: httpx.URL) -> bool:
    return (
        url.scheme == "https"
        and url.port is None
        and not url.userinfo
        and url.host.lower() == GEMINI_HOST
    )


class GeminiClient:
    """One POST to `{API_ROOT}/models/{model}:generateContent` per `generate` call."""

    def __init__(
        self,
        api_key: SecretStr,
        model: str,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(model, str) or not re.fullmatch(MODEL_ID_PATTERN, model):
            raise ValueError("invalid model id")
        self._key = api_key
        self.model = model
        self._url = httpx.URL(f"{API_ROOT}/models/{model}:generateContent")
        if not _allowed(self._url):  # cannot happen with the pattern above; kept as a guard
            raise ValueError("invalid model id")
        self._clock = clock
        self._http = httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=ATTEMPT_TIMEOUT_SECONDS,
            headers={"user-agent": USER_AGENT},
        )

    def __repr__(self) -> str:
        return "GeminiClient()"

    def close(self) -> None:
        self._http.close()

    def generate(self, body: dict, timeout: float) -> Reply:
        """One attempt, within `min(timeout, ATTEMPT_TIMEOUT_SECONDS)` in all. Raises
        GeminiError on any failure; a 200 that is not a JSON object is `parse`."""
        limit = min(float(timeout), ATTEMPT_TIMEOUT_SECONDS)
        if not limit > 0:
            raise GeminiError("timeout")
        deadline = self._clock() + limit
        try:
            content = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            raise GeminiError("request_refused") from None
        request = self._http.build_request(
            "POST",
            self._url,
            content=content,
            headers={
                "content-type": "application/json",
                "x-goog-api-key": self._key.get_secret_value(),
            },
            timeout=limit,
        )
        failed: str | None = None
        try:
            response = self._http.send(request, stream=True)
        except httpx.HTTPError as e:
            failed = _transport_kind(e)
        if failed is not None:
            # Raised outside the handler: no __context__, so the httpx error and its request
            # (with the key header) are not reachable from the GeminiError.
            raise GeminiError(failed) from None
        try:
            if self._clock() > deadline:
                raise GeminiError("timeout") from None
            status = response.status_code
            if 300 <= status <= 399:
                raise GeminiError("http", status) from None
            if not response.is_success:
                raw = self._read(response, deadline, MAX_ERROR_BYTES, cut=True)
                try:
                    err_body: object = json.loads(raw)
                except (ValueError, RecursionError):
                    err_body = {}
                raise GeminiError(
                    classify(status, err_body), status, retry_delay_seconds(response.headers,
                                                                            err_body)
                ) from None
            length = response.headers.get("content-length", "")
            if length.isdigit() and int(length) > MAX_REPLY_BYTES:
                raise GeminiError("too_large") from None
            raw = self._read(response, deadline, MAX_REPLY_BYTES, cut=False)
        finally:
            response.close()
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):
            raise GeminiError("parse") from None
        if not isinstance(obj, dict):
            raise GeminiError("parse") from None
        return parse_reply(clean_json(obj))

    def _read(self, response: httpx.Response, deadline: float, cap: int, cut: bool) -> bytes:
        """The body up to `cap` bytes: past it, `too_large`, or (for an error body, `cut`)
        the first `cap` bytes. The deadline is checked after every chunk."""
        body = bytearray()
        failed: str | None = None
        try:
            for chunk in response.iter_bytes():
                if self._clock() > deadline:
                    raise GeminiError("timeout") from None
                if len(body) + len(chunk) > cap:
                    if cut:
                        body += chunk[: cap - len(body)]
                        break
                    raise GeminiError("too_large") from None
                body += chunk
        except httpx.HTTPError as e:
            failed = _transport_kind(e)
        if failed is not None:
            raise GeminiError(failed) from None  # outside the handler: no __context__
        return bytes(body)


def _transport_kind(e: httpx.HTTPError) -> str:
    """`timeout` for an httpx timeout, else `network`. Only the kind leaves the handler."""
    return "timeout" if isinstance(e, httpx.TimeoutException) else "network"
