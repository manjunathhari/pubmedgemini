"""F7 Task 1: the doctor's Gemini key, sealed into a cookie bound to their email.

The key is never stored by the server: it lives only in this cookie (AES-256-GCM, the email as
associated data) and, during a request or a run, in memory."""

import base64

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from pubmedgemini.keycookie import (
    COOKIE_NAME,
    MAX_AGE_SECONDS,
    clear_key_cookie,
    key_tail,
    normalise_key,
    open_,
    seal,
    set_key_cookie,
)

SECRET = bytes(range(32))
OTHER_SECRET = bytes(range(1, 33))
KEY = "AIzaTestKeyNotReal_0123456789-abcd"
EMAIL = "doc@example.com"


def test_round_trip():
    opened = open_(seal(KEY, EMAIL, SECRET), EMAIL, SECRET)
    assert opened is not None and opened.get_secret_value() == KEY


def test_email_is_normalised_on_both_sides():
    opened = open_(seal(KEY, " Doc@Example.com ", SECRET), EMAIL, SECRET)
    assert opened is not None and opened.get_secret_value() == KEY


def test_another_email_cannot_open_it():
    assert open_(seal(KEY, EMAIL, SECRET), "other@example.com", SECRET) is None


def test_another_secret_cannot_open_it():
    assert open_(seal(KEY, EMAIL, SECRET), EMAIL, OTHER_SECRET) is None


def test_a_changed_byte_is_refused():
    sealed = seal(KEY, EMAIL, SECRET)
    prefix, body = sealed.split(".", 1)
    raw = bytearray(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    raw[-1] ^= 1
    tampered = prefix + "." + base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    assert open_(tampered, EMAIL, SECRET) is None


@pytest.mark.parametrize("value", ["", "v1.", "v1.!!!", "v2." + "A" * 60, "garbage", "v1.AAAA"])
def test_junk_is_refused_quietly(value):
    assert open_(value, EMAIL, SECRET) is None


def test_two_seals_differ_and_hold_neither_key_nor_email():
    a, b = seal(KEY, EMAIL, SECRET), seal(KEY, EMAIL, SECRET)
    assert a != b  # a fresh nonce each time
    for s in (a, b):
        assert KEY not in s and EMAIL not in s and "AIza" not in s


def test_opened_key_does_not_show_in_repr():
    opened = open_(seal(KEY, EMAIL, SECRET), EMAIL, SECRET)
    assert KEY not in repr(opened) and KEY not in str(opened)


@pytest.mark.parametrize("raw,expected", [
    (KEY, KEY), (f"  {KEY}\n", KEY), (f"\t{KEY} \r\n", KEY),
    ("short", None), ("", None), ("   ", None), ("has space inside 0123456789xx", None),
    ("a" * 101, None), ("key/with/slash/0123456789", None), ("é" * 30, None),
])
def test_normalise_key(raw, expected):
    assert normalise_key(raw) == expected


def test_key_tail_is_last_four():
    assert key_tail(KEY) == "abcd"


def _cookie_app():
    app = FastAPI()

    @app.get("/set")
    def _set():
        r = Response("ok")
        set_key_cookie(r, "v1.sealedvalue")
        return r

    @app.get("/clear")
    def _clear():
        r = Response("ok")
        clear_key_cookie(r)
        return r

    return TestClient(app, base_url="https://testserver")


def test_cookie_flags():
    header = _cookie_app().get("/set").headers["set-cookie"]
    parts = [p.strip() for p in header.split(";")]
    assert parts[0] == f"{COOKIE_NAME}=v1.sealedvalue"
    lower = [p.lower() for p in parts[1:]]
    assert "httponly" in lower and "secure" in lower and "samesite=lax" in lower
    assert "path=/" in lower and f"max-age={MAX_AGE_SECONDS}" in lower
    assert MAX_AGE_SECONDS == 400 * 24 * 3600  # the most browsers keep a cookie


def test_clear_cookie_expires_it_with_the_same_flags():
    header = _cookie_app().get("/clear").headers["set-cookie"].lower()
    assert header.startswith(f"{COOKIE_NAME}=")
    assert "max-age=0" in header and "path=/" in header and "secure" in header
    assert "httponly" in header and "samesite=lax" in header


# ---------- review fixes: Lax, and the sealed issue time (renewal) ----------

def test_cookie_is_lax_so_a_signed_in_landing_page_sees_it():
    """Review Important 1: Strict hid the key on the page after every Google sign-in (the
    redirect chain starts on another site). Every POST keeps the cross-site guard."""
    lower = _cookie_app().get("/set").headers["set-cookie"].lower()
    assert "samesite=lax" in lower and "samesite=strict" not in lower


def test_the_seal_carries_when_it_was_made():
    from pubmedgemini.keycookie import open_with_age

    sealed = seal(KEY, EMAIL, SECRET, now=1_000_000)
    key, issued = open_with_age(sealed, EMAIL, SECRET)
    assert key.get_secret_value() == KEY and issued == 1_000_000
    assert open_with_age(sealed, "other@example.com", SECRET) is None
