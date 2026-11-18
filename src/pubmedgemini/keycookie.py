"""The doctor's own Gemini key, kept only in their browser (F7, owner decision 1A, 18 Nov 2026).

The key is sealed with AES-256-GCM under the server's KEY_COOKIE_SECRET, with the signed-in
email as associated data, so a cookie made under one Google account opens under no other (a
shared hospital computer). The server never writes the key to a table, a log line or a file:
it exists only in this cookie and, during a request or a run, in memory as a SecretStr.

The cookie lasts 400 days, the most browsers keep one, and is renewed on every signed-in page
load, so the key stays until the doctor removes it (owner decision O3, 18 Nov 2026).
"""

import base64
import binascii
import os
import re

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
# Deliberately loose: the exact shape of AI Studio keys is not checked (plan, Not checked).
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,100}")


def normalise_key(raw: str) -> str | None:
    """The key with whitespace trimmed from both ends, or None if it is not key-shaped."""
    key = raw.strip()
    return key if KEY_PATTERN.fullmatch(key) else None


def key_tail(key: str) -> str:
    """The last four characters: the only part of a key ever shown."""
    return key[-4:]


def _aad(email: str) -> bytes:
    return _AAD_PREFIX + normalise_email(email).encode("utf-8")


def seal(key: str, email: str, secret: bytes) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    box = AESGCM(secret).encrypt(nonce, key.encode("utf-8"), _aad(email))
    return _VERSION + "." + base64.urlsafe_b64encode(nonce + box).decode("ascii").rstrip("=")


def open_(value: str, email: str, secret: bytes) -> SecretStr | None:
    """The key, or None for anything that does not open: another email, another secret, a
    changed byte, junk. Never raises, and never says why."""
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
        key = plain.decode("utf-8")
    except (InvalidTag, ValueError):
        return None
    return SecretStr(key) if normalise_key(key) == key else None


def set_key_cookie(response: Response, sealed: str) -> None:
    response.set_cookie(COOKIE_NAME, sealed, max_age=MAX_AGE_SECONDS, path="/", secure=True,
                        httponly=True, samesite="strict")


def clear_key_cookie(response: Response) -> None:
    response.set_cookie(COOKIE_NAME, "", max_age=0, path="/", secure=True, httponly=True,
                        samesite="strict")
