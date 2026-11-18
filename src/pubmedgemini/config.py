import base64
import binascii
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


def _decode_secret(text: str) -> bytes | None:
    try:
        return base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None


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
    # F7 (owner decision O1, 18 Nov 2026): no Gemini key here. Each doctor's own key comes
    # from their browser cookie (keycookie.py); a GEMINI_API_KEY left in .env is not read.
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_rpm: int = 10  # requests a minute
    gemini_tpm: int = 250_000  # prompt tokens a minute
    gemini_rpd: int = 500  # requests a day (the free day resets at midnight Pacific)
    # F7: seals each doctor's own Gemini key into their cookie (keycookie.py). 32 bytes,
    # base64. Required in production by compose; unset (dev, tests) means a random secret per
    # process, so saved keys stop opening after a restart.
    key_cookie_secret: SecretStr | None = None
    # F7 (owner decision O2, 18 Nov 2026): questions running at once for the whole app.
    max_active_runs: int = 2

    @field_validator("ncbi_api_key", "ncbi_email", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        """Compose passes an empty value when the name is unset in .env."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("key_cookie_secret", mode="before")
    @classmethod
    def _blank_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("key_cookie_secret")
    @classmethod
    def _secret_is_32_bytes(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(_decode_secret(value.get_secret_value()) or b"") != 32:
            raise ValueError("not 32 bytes of base64")
        return value

    @field_validator("max_active_runs", mode="before")
    @classmethod
    def _blank_cap_is_default(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return 2
        return value

    @field_validator("max_active_runs")
    @classmethod
    def _cap_in_range(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError("out of range")
        return value

    def key_cookie_secret_bytes(self) -> bytes | None:
        if self.key_cookie_secret is None:
            return None
        return _decode_secret(self.key_cookie_secret.get_secret_value())

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
