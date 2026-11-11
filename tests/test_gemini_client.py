import json
import logging

import httpx
import pytest
from gemini_fakes import (
    ERROR_INFO,
    FAKE_KEY,
    QUOTA_FAILURE,
    RETRY_INFO,
    FakeGemini,
    call,
    error,
    error_body,
    fixture,
    reply,
)
from lit_fakes import FakeClock, FakeUpstream
from pydantic import SecretStr

from pubmedgemini.gemini.client import (
    API_ROOT,
    ATTEMPT_TIMEOUT_SECONDS,
    FATAL_KINDS,
    FINISH_LOG_KINDS,
    GEMINI_HOST,
    MAX_ERROR_BYTES,
    MAX_REPLY_BYTES,
    USER_AGENT,
    FunctionCall,
    GeminiClient,
    GeminiError,
    Reply,
    Usage,
    classify,
    finish_log_kind,
    function_declarations,
    function_response_turn,
    parse_reply,
    retry_delay_seconds,
    structured_body,
    to_openapi,
    turn_body,
    user_text,
)
from pubmedgemini.lit.http import LitClient, LitError

MODEL = "gemini-3.5-flash-lite"
URL = f"https://{GEMINI_HOST}/v1beta/models/{MODEL}:generateContent"
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"status": {"type": "string", "enum": ["ok"]}, "number": {"type": "integer"}},
    "required": ["status", "number"],
}


def client(g: FakeGemini, key: str = FAKE_KEY, clock=None) -> GeminiClient:
    kw = {"clock": clock} if clock is not None else {}
    return GeminiClient(SecretStr(key), MODEL, g.transport, **kw)


def test_constants():
    assert GEMINI_HOST == "generativelanguage.googleapis.com"
    assert API_ROOT == "https://generativelanguage.googleapis.com/v1beta"
    assert ATTEMPT_TIMEOUT_SECONDS == 120.0  # Incisive client.rs REQUEST_TIMEOUT
    assert (MAX_REPLY_BYTES, MAX_ERROR_BYTES) == (8 * 1024 * 1024, 64 * 1024)
    assert USER_AGENT == "pubmedgemini/0.1.0"
    assert FATAL_KINDS == frozenset({"daily_limit", "key_refused", "billing_refused"})


# ---------- request bodies (the probe's shapes, accepted live) ----------


def test_to_openapi_simplifications():
    s = to_openapi({
        "type": "object", "additionalProperties": False, "description": "d",
        "properties": {
            "a": {"type": ["number", "null"]},
            "b": {"type": "array", "items": {"type": "string", "enum": ["x", "y"]}},
            "c": {"type": "string", "format": "date"},
        },
        "required": ["a", "b"],
    })
    assert s == {
        "type": "OBJECT", "description": "d",
        "properties": {
            "a": {"type": "NUMBER", "nullable": True},
            "b": {"type": "ARRAY", "items": {"type": "STRING", "enum": ["x", "y"]}},
            "c": {"type": "STRING", "format": "date"},
        },
        "required": ["a", "b"],
    }
    assert "additionalProperties" not in json.dumps(s)


class _Spec:
    name = "get_probe_value"
    description = "Return the stored test value for a key."

    def __init__(self) -> None:
        self.schema = {"type": "object", "additionalProperties": False,
                       "properties": {"key": {"type": "string", "description": "The key."}},
                       "required": ["key"]}


def test_turn_body_shape():
    decls = function_declarations([_Spec()])
    body = turn_body("SYS", [user_text("hi")], decls, 4000)
    assert body == {
        "systemInstruction": {"parts": [{"text": "SYS"}]},
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "tools": [{"functionDeclarations": [{
            "name": "get_probe_value",
            "description": "Return the stored test value for a key.",
            "parameters": {"type": "OBJECT",
                           "properties": {"key": {"type": "STRING", "description": "The key."}},
                           "required": ["key"]},
        }]}],
        "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
        "generationConfig": {"maxOutputTokens": 4000},
    }


def test_structured_body_shape():
    assert structured_body("S", "U", SCHEMA, 1024) == {
        "systemInstruction": {"parts": [{"text": "S"}]},
        "contents": [{"role": "user", "parts": [{"text": "U"}]}],
        "generationConfig": {
            "maxOutputTokens": 1024,
            "responseMimeType": "application/json",
            "responseSchema": {"type": "OBJECT", "properties": {
                "status": {"type": "STRING", "enum": ["ok"]}, "number": {"type": "INTEGER"}},
                "required": ["status", "number"]},
        },
    }


def test_function_response_turn_shape():
    turn = function_response_turn(
        [("search_pubmed", None, True, "R1"), ("find_related", "call-2", False, "E2")]
    )
    assert turn == {"role": "user", "parts": [
        {"functionResponse": {"name": "search_pubmed", "response": {"output": "R1"}}},
        {"functionResponse": {"name": "find_related", "response": {"error": "E2"},
                              "id": "call-2"}},
    ]}


# ---------- reading a reply ----------


def test_parse_reply_shapes():  # the probe's unit-test shapes
    r = parse_reply(reply([{"text": "t", "thought": True}, {"text": "Searching."},
                           call("search_pubmed", {"query": "a", "max_results": 5}, id="x1"),
                           call("search_europepmc", {"query": "b", "max_results": 5})]))
    assert r.text == "Searching." and [c.name for c in r.calls] == ["search_pubmed",
                                                                    "search_europepmc"]
    assert r.calls[0].id == "x1" and r.calls[1].id is None
    assert r.calls[0].args == {"query": "a", "max_results": 5}
    assert r.content["parts"][2]["thoughtSignature"] == "c2ln"
    assert r.finish == "STOP" and r.usage == Usage(prompt=1000, output=100, thoughts=0)
    assert parse_reply({"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL"}]}).calls == ()
    blocked = parse_reply({"promptFeedback": {"blockReason": "SAFETY"}})
    assert blocked.block_reason == "SAFETY" and blocked.content is None and blocked.finish == ""
    assert parse_reply({"candidates": []}).content is None


def test_parse_reply_keeps_the_content_unchanged_to_echo():
    body = fixture("reply_two_calls.json")
    r = parse_reply(json.loads(json.dumps(body)))
    assert r.content == body["candidates"][0]["content"]
    assert r.text == "Searching." and r.usage == Usage(1200, 80, 0)


def test_parse_reply_keeps_args_that_are_not_an_object():
    """The loop answers them `input must be a JSON object`; a missing args is an empty
    object (Gemini leaves out an empty Struct)."""
    r = parse_reply(reply([call("search_pubmed", "not a dict"),
                           {"functionCall": {"name": "get_abstracts"}},
                           {"functionCall": {"args": {}}}]))
    assert [c.args for c in r.calls] == ["not a dict", {}, {}]
    assert [c.name for c in r.calls] == ["search_pubmed", "get_abstracts", ""]


@pytest.mark.parametrize("obj", [None, [], "x", 5, {"candidates": "x"}, {"candidates": [None]},
                                 {"candidates": [{"content": "x"}]},
                                 {"candidates": [{"content": {"parts": "x"}}]},
                                 {"usageMetadata": {"promptTokenCount": "many"}}])
def test_parse_reply_never_raises_on_odd_shapes(obj):
    r = parse_reply(obj)
    assert isinstance(r, Reply) and r.calls == () and r.content is None
    assert r.usage == Usage(0, 0, 0)


def test_finish_log_kinds():
    assert {"STOP", "MAX_TOKENS", "SAFETY", "RECITATION", "OTHER", "MALFORMED_FUNCTION_CALL",
            "MISSING_THOUGHT_SIGNATURE"} <= FINISH_LOG_KINDS
    assert finish_log_kind("STOP") == "stop"
    assert finish_log_kind("MALFORMED_FUNCTION_CALL") == "malformed_function_call"
    assert finish_log_kind("gm-q7 SOMETHING NEW") == "other" and finish_log_kind("") == "other"


def test_retry_delay_seconds():
    assert retry_delay_seconds({}, error_body(429, [RETRY_INFO("41.5s")])) == 41.5
    assert retry_delay_seconds({"retry-after": "7"}, error_body(429, [RETRY_INFO("41.5s")])) == 7.0
    assert retry_delay_seconds(httpx.Headers({"Retry-After": "3"}), {}) == 3.0
    assert retry_delay_seconds({}, error_body(503, [])) is None
    assert retry_delay_seconds({}, fixture("error_429_rate.json")) == 12.0
    for bad in ("soon", "inf", "nan", "-5"):
        got = retry_delay_seconds({"retry-after": bad}, {})
        assert got is None or got == 0.0, bad
    assert retry_delay_seconds({}, error_body(429, [RETRY_INFO("12")])) is None
    assert retry_delay_seconds({}, "not a dict") is None


@pytest.mark.parametrize("status,body,kind", [
    (429, fixture("error_429_rate.json"), "rate_limited"),
    (429, fixture("error_429_daily.json"), "daily_limit"),
    (429, {}, "rate_limited"),
    (400, fixture("error_400_key_invalid.json"), "key_refused"),
    (400, error_body(400, [ERROR_INFO("OTHER")]), "request_refused"),
    (500, {}, "overloaded"), (502, {}, "overloaded"), (504, {}, "overloaded"),
    (529, {}, "overloaded"), (301, {}, "http"), (409, {}, "http"), (501, {}, "http"),
])
def test_classify(status, body, kind):
    assert classify(status, body) == kind


def test_classify_never_reads_the_message():
    body = error_body(400, [], message="API_KEY_INVALID PerDay")
    assert classify(400, body) == "request_refused"
    assert classify(429, error_body(429, [], message="PerDay")) == "rate_limited"


# ---------- one attempt over the fake transport ----------


def test_key_only_in_header_never_in_url_or_error():  # Review Focus 4
    g = FakeGemini([error(503, "UNAVAILABLE", message="gm-q7 echo gk-q7")])
    c = client(g, key="gk-q7")
    with pytest.raises(GeminiError) as e:
        c.generate(structured_body("s", "u", SCHEMA, 10), 60)
    r = g.sent[0]
    assert r.headers["x-goog-api-key"] == "gk-q7" and "gk-q7" not in str(r.url)
    assert b"gk-q7" not in r.content
    assert str(r.url) == URL and r.method == "POST"
    assert r.headers["user-agent"] == USER_AGENT and r.headers["content-type"] == "application/json"
    text = str(e.value) + repr(e.value) + repr(c) + repr(e.value.args)
    assert "gk-q7" not in text and "gm-q7" not in text and GEMINI_HOST not in text
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_error_and_client_carry_no_url_or_body():
    e = GeminiError("http", 418)
    assert (str(e), e.kind, e.status, e.retry_after) == ("http 418", "http", 418, None)
    assert str(GeminiError("timeout")) == "timeout"
    assert not any(hasattr(e, a) for a in ("url", "body", "message", "request", "response"))


def test_success_returns_the_parsed_reply():
    g = FakeGemini([fixture("reply_two_calls.json")])
    r = client(g).generate({"contents": []}, 60)
    assert [c.name for c in r.calls] == ["search_pubmed", "search_europepmc"]
    assert r.calls[0] == FunctionCall("search_pubmed", {"query": "alpha[tiab]", "max_results": 5},
                                      "x1")
    assert json.loads(g.sent[0].content) == {"contents": []}


@pytest.mark.parametrize("status,details,kind", [
    (429, [RETRY_INFO("12s")], "rate_limited"),
    (429, [QUOTA_FAILURE("GenerateRequestsPerDayPerProjectPerModel-FreeTier")], "daily_limit"),
    (503, [], "overloaded"), (402, [], "billing_refused"), (401, [], "key_refused"),
    (403, [], "key_refused"),
    (400, [ERROR_INFO("API_KEY_INVALID")], "key_refused"), (400, [], "request_refused"),
    (404, [], "not_found"), (418, [], "http"),
])
def test_error_statuses_map_to_fixed_kinds(status, details, kind):  # Review Focus 3
    g = FakeGemini([error(status, "X", details, message="gm-q7 secret text")])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert (e.value.kind, e.value.status) == (kind, status) and "gm-q7" not in str(e.value)
    assert "gm-q7" not in repr(e.value)


def test_429_carries_the_asked_delay():
    g = FakeGemini([error(429, "RESOURCE_EXHAUSTED", [RETRY_INFO("41.5s")])])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert e.value.retry_after == 41.5
    g = FakeGemini([error(503, "UNAVAILABLE", headers={"retry-after": "2"})])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert (e.value.kind, e.value.retry_after) == ("overloaded", 2.0)


def test_error_body_that_is_not_json_is_still_classified():
    g = FakeGemini([httpx.Response(503, content=b"<html>busy</html>")])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert (e.value.kind, e.value.status) == ("overloaded", 503)


def test_error_body_is_read_only_up_to_its_cap():
    body = error_body(400, [ERROR_INFO("API_KEY_INVALID")], message="m" * MAX_ERROR_BYTES)
    assert list(body["error"]) == ["code", "message", "status", "details"]  # cut in the message
    g = FakeGemini([httpx.Response(400, content=json.dumps(body).encode())])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert e.value.kind == "request_refused"  # cut, so unreadable: the plain 400 kind


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308, 304])
def test_redirect_is_not_followed(status):
    g = FakeGemini([httpx.Response(status, headers={"location": "https://evil.example/"})])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert (e.value.kind, e.value.status, len(g.sent)) == ("http", status, 1)
    assert "evil" not in str(e.value) + repr(e.value)


def test_body_over_cap_is_too_large():
    g = FakeGemini([httpx.Response(200, content=b" " * (MAX_REPLY_BYTES + 1))])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert e.value.kind == "too_large"


def test_declared_length_over_cap_is_too_large():
    g = FakeGemini([httpx.Response(200, content=b"{}",
                                   headers={"content-length": str(MAX_REPLY_BYTES + 1)})])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert e.value.kind == "too_large"


@pytest.mark.parametrize("content", [b"<html>", b"[1, 2]", b'"x"', b"\xff\xfe", b"{" * 100_000])
def test_non_json_200_is_parse(content):
    g = FakeGemini([httpx.Response(200, content=content)])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 60)
    assert e.value.kind == "parse"


def test_nul_and_lone_surrogates_in_a_reply_are_cleaned():
    g = FakeGemini([httpx.Response(200, content=json.dumps(reply([{"text": "a\u0000b\ud800c"}]))
                                   .encode())])
    assert client(g).generate({}, 60).text == "ab�c"


def test_timeout_is_timeout():
    def slow(request):
        raise httpx.ReadTimeout("gm-q7 timed out", request=request)

    with pytest.raises(GeminiError) as e:
        client(FakeGemini([slow])).generate({}, 60)
    assert e.value.kind == "timeout" and "gm-q7" not in repr(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_connect_error_is_network():
    def down(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(GeminiError) as e:
        client(FakeGemini([down])).generate({}, 60)
    assert e.value.kind == "network" and GEMINI_HOST not in repr(e.value)


# Review L4: on the network and timeout paths the GeminiError carries no cause and no context,
# so the httpx error (and the request with the key header) can never ride along on it.

def _no_chain(err):
    assert err.__cause__ is None and err.__suppress_context__ and err.__context__ is None


def _body_then(exc):
    def handler(request):
        def chunks():
            yield b'{"candidates": ['
            raise exc(f"gk-q7 {request.url}", request=request)
        return httpx.Response(200, content=chunks())
    return handler


@pytest.mark.parametrize("exc,kind", [
    (httpx.ConnectError, "network"), (httpx.RemoteProtocolError, "network"),
    (httpx.ReadTimeout, "timeout"), (httpx.ConnectTimeout, "timeout")])
def test_send_errors_carry_no_cause_or_context(exc, kind):
    def fail(request):
        raise exc(f"gk-q7 {request.url}", request=request)

    with pytest.raises(GeminiError) as e:
        client(FakeGemini([fail]), key="gk-q7").generate({}, 60)
    assert e.value.kind == kind
    _no_chain(e.value)


@pytest.mark.parametrize("exc,kind", [
    (httpx.ReadError, "network"), (httpx.RemoteProtocolError, "network"),
    (httpx.ReadTimeout, "timeout")])
def test_read_errors_carry_no_cause_or_context(exc, kind):
    with pytest.raises(GeminiError) as e:
        client(FakeGemini([_body_then(exc)]), key="gk-q7").generate({}, 60)
    assert e.value.kind == kind
    _no_chain(e.value)


def test_error_body_read_errors_carry_no_cause_or_context():
    def handler(request):
        def chunks():
            yield b'{"error": '
            raise httpx.ReadError("gk-q7", request=request)
        return httpx.Response(503, content=chunks())

    with pytest.raises(GeminiError) as e:
        client(FakeGemini([handler]), key="gk-q7").generate({}, 60)
    assert e.value.kind == "network"
    _no_chain(e.value)


# The same for the parse and request paths: a JSONDecodeError holds the whole reply text
# (`.doc`), a TypeError or ValueError from encoding can name the body; neither may ride along.

@pytest.mark.parametrize("raw", [
    b'{"candidates": [{"content": {"parts": [{"text": "zq7 reply text',  # cut off
    b"<html>zq7 reply text</html>",  # not JSON
    b"[" * 100_000,  # too deep: RecursionError
    b'["zq7 reply text"]',  # JSON, not an object
])
def test_parse_errors_carry_no_cause_or_context(raw):
    with pytest.raises(GeminiError) as e:
        client(FakeGemini([httpx.Response(200, content=raw)])).generate({}, 60)
    assert e.value.kind == "parse"
    _no_chain(e.value)
    assert "zq7" not in repr(e.value) and "zq7" not in str(e.value)


def _circular():
    d = {"zq7": "question text"}
    d["self"] = d
    return d


@pytest.mark.parametrize("body", [{"zq7": object()}, _circular()])
def test_request_refused_carries_no_cause_or_context(body):
    g = FakeGemini([])
    with pytest.raises(GeminiError) as e:
        client(g).generate(body, 60)
    assert e.value.kind == "request_refused" and g.sent == []
    _no_chain(e.value)


def test_total_deadline_is_checked_while_reading():
    clock = FakeClock()

    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"candidates": '
            clock.t += 61
            yield b"[]}"

    g = FakeGemini([lambda request: httpx.Response(200, stream=SlowStream())])
    with pytest.raises(GeminiError) as e:
        client(g, clock=clock.now).generate({}, 60)
    assert e.value.kind == "timeout"


def test_the_attempt_timeout_is_cut_to_the_time_given():
    seen = []

    def capture(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=reply([{"text": "x"}]))

    g = FakeGemini([capture, capture])
    c = client(g)
    c.generate({}, 30)
    c.generate({}, 500)
    assert seen[0]["read"] == 30 and seen[1]["read"] == ATTEMPT_TIMEOUT_SECONDS


def test_no_time_left_sends_nothing():
    g = FakeGemini([reply([{"text": "x"}])])
    with pytest.raises(GeminiError) as e:
        client(g).generate({}, 0)
    assert e.value.kind == "timeout" and g.sent == []


@pytest.mark.parametrize("model", ["../x", "Gemini", "a/b", "x:generateContent", "", "a" * 65])
def test_client_refuses_a_model_id_that_could_change_the_path(model):
    with pytest.raises(ValueError) as e:
        GeminiClient(SecretStr(FAKE_KEY), model, FakeGemini([]).transport)
    assert str(e.value) == "invalid model id"


def test_lit_client_still_refuses_the_gemini_host():  # hosts stay apart
    with pytest.raises(LitError) as e:
        LitClient(FakeUpstream({}).transport).get_json(f"https://{GEMINI_HOST}/x", {})
    assert e.value.kind == "disallowed"


def test_the_client_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    g = FakeGemini([fixture("reply_two_calls.json"), error(402, "X", message="gm-q7")])
    c = client(g, key="gk-q7")
    c.generate(structured_body("zq7 system", "zq7 user", SCHEMA, 10), 60)
    with pytest.raises(GeminiError):
        c.generate({}, 60)
    assert [r for r in caplog.records if r.name.startswith("pubmedgemini")] == []


# ---------- the fake transport itself (Task 7 relies on its checks) ----------


def test_fake_checks_model_turns_are_sent_back_unchanged():
    served = reply([call("search_pubmed", {"query": "a", "max_results": 5}, sig="c2lnQQ==")])
    g = FakeGemini([served, reply([{"text": "x"}]), reply([{"text": "y"}])])
    c = client(g)
    first = c.generate({"contents": [user_text("u")]}, 60)
    c.generate({"contents": [user_text("u"), first.content]}, 60)
    changed = json.loads(json.dumps(first.content))
    changed["parts"][0]["thoughtSignature"] = "other"
    with pytest.raises(AssertionError, match="model turn sent back was changed"):
        c.generate({"contents": [user_text("u"), changed]}, 60)


def test_fake_answers_404_for_any_other_host_and_fails_when_the_script_ends():
    g = FakeGemini([])
    assert httpx.Client(transport=g.transport).get("https://www.ebi.ac.uk/x").status_code == 404
    with pytest.raises(AssertionError, match="script exhausted"):
        client(g).generate({}, 60)
    assert len(g.sent) == 2
