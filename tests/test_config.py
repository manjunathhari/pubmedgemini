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


def test_ncbi_settings_are_optional(monkeypatch):
    monkeypatch.delenv("NCBI_API_KEY", raising=False)
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    s = load_settings()
    assert (s.ncbi_api_key, s.ncbi_email) == (None, None)


def test_blank_ncbi_settings_are_none(monkeypatch):
    """What compose passes when the names are unset in .env."""
    monkeypatch.setenv("NCBI_API_KEY", "")
    monkeypatch.setenv("NCBI_EMAIL", "  ")
    s = load_settings()
    assert (s.ncbi_api_key, s.ncbi_email) == (None, None)


def test_ncbi_key_is_never_shown(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "k-q7-secret")
    s = load_settings()
    assert "k-q7-secret" not in repr(s) and "k-q7-secret" not in str(s)
    assert s.ncbi_api_key.get_secret_value() == "k-q7-secret"


GEMINI_NAMES = ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_RPM", "GEMINI_TPM", "GEMINI_RPD")


def test_gemini_settings_default(monkeypatch):
    for name in GEMINI_NAMES:
        monkeypatch.delenv(name, raising=False)
    s = load_settings()
    assert (s.gemini_api_key, s.gemini_model, s.gemini_rpm, s.gemini_tpm, s.gemini_rpd) == (
        None, "gemini-3.5-flash-lite", 10, 250_000, 500
    )


def test_blank_gemini_settings_take_defaults(monkeypatch):
    """What compose passes when the names are unset in .env."""
    for name in GEMINI_NAMES:
        monkeypatch.setenv(name, " ")
    s = load_settings()
    assert (s.gemini_api_key, s.gemini_model, s.gemini_rpm, s.gemini_tpm, s.gemini_rpd) == (
        None, "gemini-3.5-flash-lite", 10, 250_000, 500
    )


def test_empty_gemini_settings_take_defaults(monkeypatch):
    for name in GEMINI_NAMES:
        monkeypatch.setenv(name, "")
    s = load_settings()
    assert (s.gemini_api_key, s.gemini_model, s.gemini_rpm) == (None, "gemini-3.5-flash-lite", 10)


def test_gemini_key_is_never_shown(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-q7-secret")
    s = load_settings()
    assert "gk-q7-secret" not in repr(s) and "gk-q7-secret" not in str(s)
    assert s.gemini_api_key.get_secret_value() == "gk-q7-secret"


def test_gemini_settings_are_read(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-9.0-x")
    monkeypatch.setenv("GEMINI_RPM", "15")
    monkeypatch.setenv("GEMINI_TPM", "1000")
    monkeypatch.setenv("GEMINI_RPD", "1000000")
    s = load_settings()
    assert (s.gemini_model, s.gemini_rpm, s.gemini_tpm, s.gemini_rpd) == (
        "gemini-9.0-x", 15, 1000, 1_000_000
    )


@pytest.mark.parametrize(
    "model", ["../x", "gemini 3", "Gemini", "a/b", "x:generateContent", "a" * 65, "-x", "x?y", "é"]
)
def test_model_id_that_could_change_the_url_is_refused(monkeypatch, model):
    monkeypatch.setenv("GEMINI_MODEL", model)
    with pytest.raises(SettingsError) as e:
        load_settings()
    assert str(e.value) == "missing or invalid settings: GEMINI_MODEL" and model not in str(e.value)


def test_model_id_of_64_characters_is_accepted(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "a" * 64)
    assert load_settings().gemini_model == "a" * 64


@pytest.mark.parametrize(
    "name,value",
    [
        ("GEMINI_RPM", "0"), ("GEMINI_RPM", "1001"), ("GEMINI_RPM", "ten"),
        ("GEMINI_TPM", "10"), ("GEMINI_TPM", "999"), ("GEMINI_TPM", "100000001"),
        ("GEMINI_RPD", "-1"), ("GEMINI_RPD", "0"), ("GEMINI_RPD", "1000001"),
    ],
)
def test_limits_out_of_range_are_refused(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError) as e:
        load_settings()
    assert str(e.value) == f"missing or invalid settings: {name}"


@pytest.mark.parametrize(
    "name,value", [("GEMINI_RPM", "1"), ("GEMINI_RPM", "1000"), ("GEMINI_TPM", "1000"),
                   ("GEMINI_TPM", "100000000"), ("GEMINI_RPD", "1")]
)
def test_limit_bounds_are_accepted(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert getattr(load_settings(), name.lower()) == int(value)
