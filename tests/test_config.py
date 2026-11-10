import pytest
from pydantic import ValidationError

from pubmedgemini.config import load_settings


def test_settings_read_from_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    s = load_settings()
    assert (s.app_env, s.owner_email) == ("prod", "owner@example.com")


def test_missing_database_url_is_an_error(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        load_settings()


def test_unknown_app_env_is_an_error(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    with pytest.raises(ValidationError):
        load_settings()
