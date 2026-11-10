from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Every environment value, in one place. No .env file is read here."""

    app_env: Literal["dev", "prod"]
    database_url: str
    owner_email: str
    migrations_dir: Path = Path("migrations")


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
