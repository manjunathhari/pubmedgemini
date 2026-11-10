from pathlib import Path
from typing import Literal

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings


class SettingsError(Exception):
    """A required setting is missing or invalid. The message holds names only, never values."""


class Settings(BaseSettings):
    """Every environment value, in one place. No .env file is read here."""

    app_env: Literal["dev", "prod"]
    database_url: str
    owner_email: str
    migrations_dir: Path = Path("migrations")

    @field_validator("database_url", "owner_email")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


def load_settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # pydantic's own message repeats every input value, DATABASE_URL's password included.
        names = sorted({str(err["loc"][0]).upper() for err in exc.errors() if err["loc"]})
        raise SettingsError(
            "missing or invalid settings: " + ", ".join(names or ["(unknown)"])
        ) from None
