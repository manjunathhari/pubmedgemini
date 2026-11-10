import threading

import httpx
import pytest
from lit_fakes import FakeClock, FakeUpstream

from pubmedgemini.lit.http import (
    BASE_HOSTS,
    MAX_BODY_BYTES,
    NCBI_HOST,
    REQUEST_TIMEOUT_SECONDS,
    USER_AGENT,
    LitClient,
    LitError,
    NcbiPacer,
    ncbi_min_interval,
)

EPMC_HOST = "www.ebi.ac.uk"
NCBI_URL = f"https://{NCBI_HOST}/x"


def test_constants_match_incisive():
    assert BASE_HOSTS == ("eutils.ncbi.nlm.nih.gov", "www.ebi.ac.uk")
    assert USER_AGENT == "pubmedgemini/0.1.0"


def test_other_host_is_refused_before_sending():  # Review Focus 4
    up = FakeUpstream({})
    c = LitClient(up.transport)
    with pytest.raises(LitError) as e:
        c.get_json("https://evil.example/x", {})
    assert e.value.kind == "disallowed" and up.sent == []


@pytest.mark.parametrize(
    "url",
    [
        f"http://{NCBI_HOST}/x",
        f"https://{NCBI_HOST}:8443/x",
        f"https://user:pw@{NCBI_HOST}/x",
        f"https://{NCBI_HOST}.evil.example/x",
        "https://127.0.0.1/x",
        "not a url",
        "",
    ],
)
def test_plain_http_ports_userinfo_and_lookalikes_are_refused(url):
    up = FakeUpstream({})
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_text(url, {})
    assert e.value.kind == "disallowed" and up.sent == []


def test_host_match_ignores_case_and_an_explicit_default_port():
    # As Incisive: the url crate (like httpx) drops an explicit :443.
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(200, json={"ok": 1})})
    assert LitClient(up.transport).get_json("https://EUTILS.ncbi.nlm.nih.gov/x", {}) == {"ok": 1}
    assert LitClient(up.transport).get_json(f"https://{NCBI_HOST}:443/x", {}) == {"ok": 1}


def test_user_agent_and_params_are_sent():
    up = FakeUpstream({(EPMC_HOST, "/y"): httpx.Response(200, text="hello")})
    assert LitClient(up.transport).get_text(f"https://{EPMC_HOST}/y", {"a": "1 2"}) == "hello"
    r = up.sent[0]
    assert r.headers["user-agent"] == USER_AGENT and r.url.params["a"] == "1 2"


def test_redirect_to_other_host_is_refused():
    up = FakeUpstream(
        {(NCBI_HOST, "/x"): httpx.Response(302, headers={"location": "https://evil.example/"})}
    )
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_json(NCBI_URL, {})
    assert e.value.kind == "disallowed" and len(up.sent) == 1
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_redirect_to_plain_http_is_refused():
    up = FakeUpstream(
        {(NCBI_HOST, "/x"): httpx.Response(301, headers={"location": f"http://{NCBI_HOST}/y"})}
    )
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_json(NCBI_URL, {})
    assert e.value.kind == "disallowed" and len(up.sent) == 1


def test_redirect_on_allowed_hosts_is_followed():
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(302, headers={"location": f"https://{EPMC_HOST}/z"}),
            (EPMC_HOST, "/z"): httpx.Response(200, json=[1]),
        }
    )
    assert LitClient(up.transport).get_json(NCBI_URL, {}) == [1]
    assert [r.url.host for r in up.sent] == [NCBI_HOST, EPMC_HOST]


def test_three_redirects_are_followed_and_a_fourth_is_refused():
    def hop(n):
        return httpx.Response(302, headers={"location": f"https://{NCBI_HOST}/r{n}"})

    routes = {(NCBI_HOST, "/x"): hop(1), (NCBI_HOST, "/r1"): hop(2), (NCBI_HOST, "/r2"): hop(3)}
    up = FakeUpstream(routes | {(NCBI_HOST, "/r3"): httpx.Response(200, json={})})
    assert LitClient(up.transport).get_json(NCBI_URL, {}) == {}
    assert len(up.sent) == 4

    up = FakeUpstream(routes | {(NCBI_HOST, "/r3"): hop(4)})
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_json(NCBI_URL, {})
    assert e.value.kind == "too_many_redirects" and len(up.sent) == 4


def test_5xx_is_retried_twice_then_fails():  # Review Focus 4
    sleeps: list[float] = []
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(503)})
    c = LitClient(up.transport, sleep=sleeps.append)
    with pytest.raises(LitError) as e:
        c.get_json(NCBI_URL, {})
    assert (e.value.kind, e.value.status, len(up.sent), sleeps) == ("http", 503, 3, [0.5, 1.0])


def test_5xx_then_success_returns_the_body():
    replies = iter([httpx.Response(500), httpx.Response(200, json={"n": 2})])
    sleeps: list[float] = []
    up = FakeUpstream({(NCBI_HOST, "/x"): lambda req: next(replies)})
    assert LitClient(up.transport, sleep=sleeps.append).get_json(NCBI_URL, {}) == {"n": 2}
    assert sleeps == [0.5]


def test_429_is_not_retried():  # Review Focus 4
    sleeps: list[float] = []
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(429)})
    with pytest.raises(LitError) as e:
        LitClient(up.transport, sleep=sleeps.append).get_json(NCBI_URL, {})
    assert (e.value.kind, e.value.status, len(up.sent), sleeps) == ("http", 429, 1, [])


def test_404_is_not_retried():
    up = FakeUpstream({})
    with pytest.raises(LitError) as e:
        LitClient(up.transport, sleep=lambda s: None).get_json(NCBI_URL, {})
    assert (e.value.kind, e.value.status, len(up.sent)) == ("http", 404, 1)
    assert str(e.value) == "http 404"


def test_timeout_is_retried_then_timeout():
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    sleeps: list[float] = []
    up = FakeUpstream({(NCBI_HOST, "/x"): slow})
    with pytest.raises(LitError) as e:
        LitClient(up.transport, sleep=sleeps.append).get_json(NCBI_URL, {})
    assert (e.value.kind, e.value.status, len(up.sent), sleeps) == ("timeout", None, 3, [0.5, 1.0])
    assert str(e.value) == "timeout"


def test_network_error_is_retried_then_network():
    def down(request):
        raise httpx.ConnectError("refused", request=request)

    up = FakeUpstream({(EPMC_HOST, "/x"): down})
    with pytest.raises(LitError) as e:
        LitClient(up.transport, sleep=lambda s: None).get_text(f"https://{EPMC_HOST}/x", {})
    assert (e.value.kind, len(up.sent)) == ("network", 3)


def test_body_over_cap_is_too_large():
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(200, content=b"a" * (MAX_BODY_BYTES + 1))})
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_text(NCBI_URL, {})
    assert e.value.kind == "too_large" and len(up.sent) == 1


def test_body_over_cap_without_length_header_is_too_large():
    def chunks():
        for _ in range(9):
            yield b"a" * (1024 * 1024)

    up = FakeUpstream({(NCBI_HOST, "/x"): lambda req: httpx.Response(200, content=chunks())})
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_text(NCBI_URL, {})
    assert e.value.kind == "too_large"


def test_body_at_cap_is_read():
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(200, content=b"a" * MAX_BODY_BYTES)})
    assert len(LitClient(up.transport).get_text(NCBI_URL, {})) == MAX_BODY_BYTES


def test_bad_json_is_parse():
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(200, text="<html>")})
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_json(NCBI_URL, {})
    assert e.value.kind == "parse"


def test_error_text_holds_no_url_or_params():  # Review Focus 2
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(503)})
    with pytest.raises(LitError) as e:
        LitClient(up.transport, sleep=lambda s: None).get_json(
            NCBI_URL, {"term": "zq7", "api_key": "k-q7"}
        )
    text = str(e.value) + repr(e.value) + repr(e.value.args)
    assert "zq7" not in text and "k-q7" not in text and NCBI_HOST not in text
    assert str(e.value) == "http 503"
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_transport_error_text_holds_no_url():
    def down(request):
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    up = FakeUpstream({(NCBI_HOST, "/x"): down})
    with pytest.raises(LitError) as e:
        LitClient(up.transport, sleep=lambda s: None).get_json(NCBI_URL, {"term": "zq7"})
    assert "zq7" not in str(e.value) + repr(e.value) and NCBI_HOST not in repr(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_refused_host_text_holds_no_url():
    with pytest.raises(LitError) as e:
        LitClient(FakeUpstream({}).transport).get_json("https://evil.example/zq7", {})
    assert str(e.value) == "disallowed" and "evil" not in repr(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_pacer_spaces_ncbi_attempts_only():
    clock = FakeClock()
    pacer = NcbiPacer(0.334, clock=clock.now, sleep=clock.sleep)
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(200, json={}),
            (EPMC_HOST, "/y"): httpx.Response(200, json={}),
        }
    )
    c = LitClient(up.transport, pacer=pacer)
    for _ in range(3):
        c.get_json(NCBI_URL, {})
    c.get_json(f"https://{EPMC_HOST}/y", {})
    assert clock.slept == pytest.approx([0.334, 0.334])


def test_retries_are_paced_too():
    clock = FakeClock()
    pacer = NcbiPacer(0.334, clock=clock.now, sleep=clock.sleep)
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(503)})
    c = LitClient(up.transport, pacer=pacer, sleep=clock.sleep)
    with pytest.raises(LitError):
        c.get_json(NCBI_URL, {})
    # The backoff sleeps (0.5, 1.0) already exceed the gap, so the pacer adds nothing more.
    assert clock.slept == pytest.approx([0.5, 1.0]) and len(up.sent) == 3


def test_pacer_with_zero_gap_never_sleeps():
    clock = FakeClock()
    pacer = NcbiPacer(0, clock=clock.now, sleep=clock.sleep)
    for _ in range(3):
        pacer.wait()
    assert clock.slept == []


def test_pacer_shares_one_schedule_across_threads():
    clock = FakeClock()
    lock = threading.Lock()

    def sleep(s):
        with lock:
            clock.sleep(s)

    pacer = NcbiPacer(0.334, clock=clock.now, sleep=sleep)
    threads = [threading.Thread(target=pacer.wait) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(clock.slept) == pytest.approx(3 * 0.334)


def test_ncbi_min_interval():
    assert (ncbi_min_interval(False), ncbi_min_interval(True)) == (0.334, 0.1)


# ---------- redirects by hand (Incisive `follow`, `fetch_hop`, `attempt`) ----------


def test_key_is_not_sent_to_the_other_allowed_host_after_a_redirect():
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(302, headers={"location": f"https://{EPMC_HOST}/z"}),
            (EPMC_HOST, "/z"): httpx.Response(200, json=[1]),
        }
    )
    LitClient(up.transport).get_json(NCBI_URL, {"api_key": "k-q7", "term": "zq7"})
    first, second = up.sent
    assert first.url.params["api_key"] == "k-q7"
    assert "api_key" not in second.url.params and "term" not in second.url.params
    assert "k-q7" not in str(second.url) and "zq7" not in str(second.url)


def test_key_echoed_in_a_location_to_the_other_host_is_dropped():
    loc = f"https://{EPMC_HOST}/z?api_key=k-q7&keep=1"
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(302, headers={"location": loc}),
            (EPMC_HOST, "/z"): httpx.Response(200, json=[1]),
        }
    )
    LitClient(up.transport).get_json(NCBI_URL, {"api_key": "k-q7"})
    assert "k-q7" not in str(up.sent[1].url) and up.sent[1].url.params["keep"] == "1"


def test_relative_location_is_resolved_against_the_current_url():
    up = FakeUpstream(
        {
            (NCBI_HOST, "/a/x"): httpx.Response(302, headers={"location": "y?n=1"}),
            (NCBI_HOST, "/a/y"): httpx.Response(200, json={"ok": 1}),
        }
    )
    assert LitClient(up.transport).get_json(f"https://{NCBI_HOST}/a/x", {"t": "1"}) == {"ok": 1}
    assert str(up.sent[1].url) == f"https://{NCBI_HOST}/a/y?n=1"


def test_a_redirect_body_is_never_read():
    pulled: list[int] = []

    def huge():
        for i in range(100):
            pulled.append(i)
            yield b"a" * (1024 * 1024)

    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): lambda req: httpx.Response(
                302, headers={"location": f"https://{NCBI_HOST}/y"}, content=huge()
            ),
            (NCBI_HOST, "/y"): httpx.Response(200, json={}),
        }
    )
    assert LitClient(up.transport).get_json(NCBI_URL, {}) == {}
    assert pulled == []


def test_redirect_to_a_disallowed_host_is_refused_before_any_request_to_it():
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(
                307, headers={"location": f"https://{NCBI_HOST}:8443/y"}
            ),
        }
    )
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_json(NCBI_URL, {})
    assert e.value.kind == "disallowed" and [r.url.path for r in up.sent] == ["/x"]


def test_invalid_location_is_disallowed_as_in_incisive():
    up = FakeUpstream(
        {(NCBI_HOST, "/x"): httpx.Response(302, headers={"location": "https://[::1/"})}
    )
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_json(NCBI_URL, {})
    assert e.value.kind == "disallowed" and len(up.sent) == 1


@pytest.mark.parametrize("status", [300, 304, 305])
def test_other_3xx_is_an_http_error(status):
    up = FakeUpstream(
        {(NCBI_HOST, "/x"): httpx.Response(status, headers={"location": f"https://{NCBI_HOST}/y"})}
    )
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_json(NCBI_URL, {})
    assert (e.value.kind, e.value.status, len(up.sent)) == ("http", status, 1)


def test_redirect_without_location_is_an_http_error():
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(302)})
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_json(NCBI_URL, {})
    assert (e.value.kind, e.value.status) == ("http", 302)


def test_a_transient_error_on_the_second_hop_retries_only_that_hop():
    replies = iter([httpx.Response(503), httpx.Response(200, json={"ok": 2})])
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(302, headers={"location": f"https://{NCBI_HOST}/y"}),
            (NCBI_HOST, "/y"): lambda req: next(replies),
        }
    )
    sleeps: list[float] = []
    assert LitClient(up.transport, sleep=sleeps.append).get_json(NCBI_URL, {}) == {"ok": 2}
    assert [r.url.path for r in up.sent] == ["/x", "/y", "/y"] and sleeps == [0.5]


def test_every_hop_and_retry_to_ncbi_is_paced():
    clock = FakeClock()
    pacer = NcbiPacer(0.334, clock=clock.now, sleep=clock.sleep)
    waits: list[str] = []
    real_wait = pacer.wait

    def counting_wait():
        waits.append("w")
        real_wait()

    pacer.wait = counting_wait
    replies = iter([httpx.Response(503), httpx.Response(200, json={})])
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(302, headers={"location": f"https://{NCBI_HOST}/y"}),
            (NCBI_HOST, "/y"): lambda req: next(replies),
        }
    )
    LitClient(up.transport, pacer=pacer, sleep=lambda s: None).get_json(NCBI_URL, {})
    assert len(waits) == len(up.sent) == 3


def test_a_hop_to_europepmc_is_not_paced():
    waits: list[str] = []
    pacer = NcbiPacer(0)
    pacer.wait = lambda: waits.append("w")
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(302, headers={"location": f"https://{EPMC_HOST}/z"}),
            (EPMC_HOST, "/z"): httpx.Response(200, json=[1]),
        }
    )
    LitClient(up.transport, pacer=pacer).get_json(NCBI_URL, {})
    assert waits == ["w"]


def test_body_cap_applies_to_the_final_response_after_a_redirect():
    up = FakeUpstream(
        {
            (NCBI_HOST, "/x"): httpx.Response(302, headers={"location": f"https://{EPMC_HOST}/z"}),
            (EPMC_HOST, "/z"): httpx.Response(200, content=b"a" * (MAX_BODY_BYTES + 1)),
        }
    )
    with pytest.raises(LitError) as e:
        LitClient(up.transport).get_text(NCBI_URL, {})
    assert e.value.kind == "too_large"


# ---------- a total deadline per attempt (Incisive `REQUEST_TIMEOUT`, a whole-request limit) ----------


def _trickle(clock: FakeClock, chunks: int, seconds_each: float):
    def reply(request):
        def body():
            for _ in range(chunks):
                clock.t += seconds_each
                yield b"a"

        return httpx.Response(200, content=body())

    return reply


def test_a_trickle_past_the_total_deadline_is_a_timeout_and_is_retried():
    clock = FakeClock()
    up = FakeUpstream({(NCBI_HOST, "/x"): _trickle(clock, chunks=5, seconds_each=6.0)})
    c = LitClient(up.transport, sleep=clock.sleep, clock=clock.now)
    with pytest.raises(LitError) as e:
        c.get_text(NCBI_URL, {})
    assert (e.value.kind, e.value.transient, len(up.sent)) == ("timeout", True, 3)
    assert clock.slept == [0.5, 1.0]
    assert str(e.value) == "timeout" and e.value.__cause__ is None


def test_a_body_inside_the_total_deadline_is_read():
    clock = FakeClock()
    up = FakeUpstream({(NCBI_HOST, "/x"): _trickle(clock, chunks=3, seconds_each=6.0)})
    assert LitClient(up.transport, clock=clock.now).get_text(NCBI_URL, {}) == "aaa"


def test_a_slow_reply_head_past_the_total_deadline_is_a_timeout():
    clock = FakeClock()

    def slow_head(request):
        clock.t += REQUEST_TIMEOUT_SECONDS + 1
        return httpx.Response(200, json={})

    up = FakeUpstream({(NCBI_HOST, "/x"): slow_head})
    with pytest.raises(LitError) as e:
        LitClient(up.transport, sleep=lambda s: None, clock=clock.now).get_json(NCBI_URL, {})
    assert (e.value.kind, len(up.sent)) == ("timeout", 3)


def test_the_deadline_is_per_attempt_so_a_retry_starts_afresh():
    clock = FakeClock()
    replies = iter([
        _trickle(clock, chunks=5, seconds_each=6.0),
        _trickle(clock, chunks=2, seconds_each=6.0),
    ])
    up = FakeUpstream({(NCBI_HOST, "/x"): lambda req: next(replies)(req)})
    c = LitClient(up.transport, sleep=clock.sleep, clock=clock.now)
    assert c.get_text(NCBI_URL, {}) == "aa" and len(up.sent) == 2
