from collections.abc import Mapping
from dataclasses import dataclass

from fastapi import Request
from psycopg_pool import ConnectionPool

GATE_EMAIL_HEADER = "x-forwarded-email"
DEV_EMAIL_HEADER = "x-dev-user-email"


class NotSignedIn(Exception):
    """The request carries no email."""


class NotSetUp(Exception):
    """The email is not in the users table."""


@dataclass(frozen=True)
class User:
    id: int
    email: str
    role: str


def normalise_email(raw: str) -> str:
    return raw.strip().lower()


def email_from_headers(headers: Mapping[str, str], app_env: str) -> str | None:
    """The gate header if present and non-empty; else, only in dev, the dev header."""
    gate = normalise_email(headers.get(GATE_EMAIL_HEADER, ""))
    if gate:
        return gate
    if app_env == "dev":
        dev = normalise_email(headers.get(DEV_EMAIL_HEADER, ""))
        if dev:
            return dev
    return None


def find_user(pool: ConnectionPool, email: str) -> User | None:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT id, email, role FROM users WHERE email = %s", (normalise_email(email),)
        ).fetchone()
    return User(id=row[0], email=row[1], role=row[2]) if row else None


def ensure_owner(pool: ConnectionPool, owner_email: str) -> None:
    """Insert the owner if no row has that email; change nothing otherwise."""
    with pool.connection() as conn:
        conn.execute(
            "INSERT INTO users (email, role) VALUES (%s, 'owner') ON CONFLICT (email) DO NOTHING",
            (normalise_email(owner_email),),
        )


def current_user(request: Request) -> User:
    """FastAPI dependency: who is asking."""
    email = email_from_headers(request.headers, request.app.state.settings.app_env)
    if email is None:
        raise NotSignedIn
    user = find_user(request.app.state.pool, email)
    if user is None:
        raise NotSetUp
    return user
