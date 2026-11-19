"""The doctor's own Gemini key, kept only in their browser (F7, owner decision 1A, 18 Nov 2026).

The key is sealed with AES-256-GCM under the server's KEY_COOKIE_SECRET, with the signed-in
email as associated data, so a cookie made under one Google account opens under no other (a
shared hospital computer). The server never writes the key to a table, a log line or a file:
it exists only in this cookie and, during a request or a run, in memory as a SecretStr.

The cookie lasts 400 days, the most browsers keep one. The seal carries the time it was made;
a signed-in page renews a seal older than RENEW_AFTER_SECONDS, so the key stays until the
doctor removes it (owner decision O3, 18 Nov 2026) while a Remove or a new key is not undone by
a page reload already on its way (review Important 2, 18 Nov).

SameSite=Lax, not Strict (review Important 1): the page a doctor lands on after Google sign-in,
or from a link in a message, must see the key. Lax is still not sent with another site's form
post, and every POST also has the app's cross-site guard.
"""

import base64
import binascii
import os
import re
import struct
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr
from starlette.responses import Response

from pubmedgemini.identity import normalise_email

COOKIE_NAME = "pmai_gk"
MAX_AGE_SECONDS = 400 * 24 * 3600
SECRET_BYTES = 32
_VERSION = "v1"
_AAD_PREFIX = b"pmai_gk/v1|"
_NONCE_BYTES = 12
_TIME = struct.Struct(">Q")  # seconds since 1970, before the key in the sealed text
RENEW_AFTER_SECONDS = 30 * 24 * 3600
# Deliberately loose: letters, digits, dot, underscore and hyphen. The older AI Studio keys
# start "AIza"; Google's newer style starts "AQ." and has dots (owner's live key, 19 Nov 2026).
KEY_PATTERN = re.compile(r"[A-Za-z0-9._-]{20,200}")


def normalise_key(raw: str) -> str | None:
    """The key with whitespace trimmed from both ends, or None if it is not key-shaped."""
    key = raw.strip()
    return key if KEY_PATTERN.fullmatch(key) else None


def key_tail(key: str) -> str:
    """The last four characters: the only part of a key ever shown."""
    return key[-4:]


def _aad(email: str) -> bytes:
    return _AAD_PREFIX + normalise_email(email).encode("utf-8")


def seal(key: str, email: str, secret: bytes, now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    nonce = os.urandom(_NONCE_BYTES)
    plain = _TIME.pack(issued) + key.encode("utf-8")
    box = AESGCM(secret).encrypt(nonce, plain, _aad(email))
    return _VERSION + "." + base64.urlsafe_b64encode(nonce + box).decode("ascii").rstrip("=")


def open_(value: str, email: str, secret: bytes) -> SecretStr | None:
    """The key, or None for anything that does not open: another email, another secret, a
    changed byte, junk. Never raises, and never says why."""
    opened = open_with_age(value, email, secret)
    return None if opened is None else opened[0]


def open_with_age(value: str, email: str, secret: bytes) -> tuple[SecretStr, int] | None:
    """The key and the time its seal was made, or None (as `open_`)."""
    version, _, body = value.partition(".")
    if version != _VERSION or not body:
        return None
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError):
        return None
    if len(raw) <= _NONCE_BYTES:
        return None
    try:
        plain = AESGCM(secret).decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _aad(email))
        if len(plain) <= _TIME.size:
            return None
        (issued,) = _TIME.unpack(plain[:_TIME.size])
        key = plain[_TIME.size:].decode("utf-8")
    except (InvalidTag, ValueError, struct.error):
        return None
    return (SecretStr(key), issued) if normalise_key(key) == key else None


def set_key_cookie(response: Response, sealed: str) -> None:
    response.set_cookie(COOKIE_NAME, sealed, max_age=MAX_AGE_SECONDS, path="/", secure=True,
                        httponly=True, samesite="lax")


def clear_key_cookie(response: Response) -> None:
    response.set_cookie(COOKIE_NAME, "", max_age=0, path="/", secure=True, httponly=True,
                        samesite="lax")
