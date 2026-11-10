import os
import subprocess
import sys
from pathlib import Path

import pytest

from pubmedgemini.config import SettingsError, load_settings

ROOT = Path(__file__).resolve().parents[1]


def _set(monkeypatch, **values):
    for name in ("APP_ENV", "DATABASE_URL", "OWNER_EMAIL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_settings_read_from_environment(monkeypatch):
    _set(monkeypatch, APP_ENV="prod", DATABASE_URL="postgresql://u:p@db/x",
         OWNER_EMAIL="owner@example.com")
    s = load_settings()
    assert (s.app_env, s.owner_email) == ("prod", "owner@example.com")


def test_missing_database_url_is_an_error(monkeypatch):
    _set(monkeypatch, APP_ENV="prod", OWNER_EMAIL="owner@example.com")
    with pytest.raises(SettingsError, match="DATABASE_URL"):
        load_settings()


def test_unknown_app_env_is_an_error(monkeypatch):
    _set(monkeypatch, APP_ENV="staging", DATABASE_URL="postgresql://u:p@db/x",
         OWNER_EMAIL="owner@example.com")
    with pytest.raises(SettingsError, match="APP_ENV") as exc:
        load_settings()
    assert "staging" not in str(exc.value)


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_owner_email_is_an_error(monkeypatch, blank):
    _set(monkeypatch, APP_ENV="prod", DATABASE_URL="postgresql://u:p@db/x", OWNER_EMAIL=blank)
    with pytest.raises(SettingsError, match="OWNER_EMAIL"):
        load_settings()


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_database_url_is_an_error(monkeypatch, blank):
    _set(monkeypatch, APP_ENV="prod", DATABASE_URL=blank, OWNER_EMAIL="owner@example.com")
    with pytest.raises(SettingsError, match="DATABASE_URL"):
        load_settings()


def test_error_message_names_settings_but_shows_no_values(monkeypatch):
    _set(monkeypatch, APP_ENV="staging", DATABASE_URL="postgresql://u:Sekr3tPw_q9@db/x")
    with pytest.raises(SettingsError) as exc:
        load_settings()
    text = str(exc.value)
    assert "APP_ENV" in text and "OWNER_EMAIL" in text
    assert "Sekr3tPw_q9" not in text and "staging" not in text
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_import_with_owner_email_unset_does_not_print_the_password():
    """Start-up as in the container: importing main must not echo DATABASE_URL."""
    env = {k: v for k, v in os.environ.items() if k not in ("OWNER_EMAIL", "DATABASE_URL")}
    env |= {
        "APP_ENV": "prod",
        "DATABASE_URL": "postgresql://madeuprole_q9:Sekr3tPw_q9@127.0.0.2:59999/x",
        "PYTHONPATH": str(ROOT / "src"),
    }
    r = subprocess.run(
        [sys.executable, "-c", "import pubmedgemini.main"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "OWNER_EMAIL" in out
    for leak in ("Sekr3tPw_q9", "madeuprole_q9", "input_value"):
        assert leak not in out, leak
