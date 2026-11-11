import re
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, ValidationError, ValidationInfo, field_validator
from pydantic_settings import BaseSettings

DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
# The model id goes into the request path, so a setting can never change the path.
MODEL_ID_PATTERN = r"[a-z0-9][a-z0-9.-]{0,63}"

# Defaults when a value is unset or blank: the free tier's limits for Flash-Lite (spec §3,
# from a third-party page; settings, not constants).
_GEMINI_DEFAULTS = {
    "gemini_model": DEFAULT_GEMINI_MODEL,
    "gemini_rpm": 10,
    "gemini_tpm": 250_000,
    "gemini_rpd": 500,
}
# Inclusive bounds for the limits.
_GEMINI_RANGES = {
    "gemini_rpm": (1, 1_000),
    "gemini_tpm": (1_000, 100_000_000),
    "gemini_rpd": (1, 1_000_000),
}


class SettingsError(Exception):
    """A required setting is missing or invalid. The message holds names only, never values."""


class Settings(BaseSettings):
    """Every environment value, in one place. No .env file is read here."""

    app_env: Literal["dev", "prod"]
    database_url: str
    owner_email: str
    migrations_dir: Path = Path("migrations")
    # Optional. NCBI allows 10 requests a second with a key, 3 without.
    ncbi_api_key: SecretStr | None = None
    ncbi_email: str | None = None
    # Optional: without a key the app, /healthz and the key-word search still work; only
    # questions need it.
    gemini_api_key: SecretStr | None = None
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_rpm: int = 10  # requests a minute
    gemini_tpm: int = 250_000  # prompt tokens a minute
    gemini_rpd: int = 500  # requests a day (the free day resets at midnight Pacific)

    @field_validator("ncbi_api_key", "ncbi_email", "gemini_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        """Compose passes an empty value when the name is unset in .env."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gemini_model", "gemini_rpm", "gemini_tpm", "gemini_rpd", mode="before")
    @classmethod
    def _blank_is_default(cls, value: object, info: ValidationInfo) -> object:
        """Compose passes an empty value when the name is unset in .env."""
        if isinstance(value, str) and not value.strip():
            return _GEMINI_DEFAULTS[info.field_name]
        return value

    @field_validator("gemini_model")
    @classmethod
    def _model_id(cls, value: str) -> str:
        if not re.fullmatch(MODEL_ID_PATTERN, value):
            raise ValueError("not a model id")
        return value

    @field_validator("gemini_rpm", "gemini_tpm", "gemini_rpd")
    @classmethod
    def _in_range(cls, value: int, info: ValidationInfo) -> int:
        low, high = _GEMINI_RANGES[info.field_name]
        if not low <= value <= high:
            raise ValueError("out of range")
        return value

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
