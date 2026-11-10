import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


def test_only_gate_faces_traefik_and_nothing_publishes_ports():
    c = yaml.safe_load(Path("compose.yaml").read_text())["services"]
    assert all("ports" not in s for s in c.values())
    assert [
        n
        for n, s in c.items()
        if any("traefik.enable=true" in label for label in s.get("labels", []))
    ] == ["gate"]
    assert list(c["app"]["networks"]) == ["internal"]
    assert list(c["db"]["networks"]) == ["internal"]


def test_env_example_has_names_but_no_values():
    for line in Path(".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("=")


# CVE-2026-40575 (GHSA-7x63-xv5r-3p2x) was fixed in 7.15.2, but the guard was inert under the
# default trusted-proxy set until 7.15.5 (CVE-2026-76835, affects 7.15.2 to 7.15.4).
GATE_MIN_VERSION = (7, 15, 5)


def _gate():
    return yaml.safe_load(Path("compose.yaml").read_text())["services"]["gate"]


# Exact allow-lists: a new flag or OAUTH2_PROXY_* variable (skip-auth-route, skip-auth-regex,
# skip-auth-preflight, trusted-ip, ...) fails these tests until it is reviewed and listed.
GATE_FLAGS = [
    "--provider",
    "--upstream",
    "--http-address",
    "--authenticated-emails-file",
    "--pass-user-headers",
    "--cookie-secure",
    "--reverse-proxy",
    "--trusted-proxy-ip",
    "--redirect-url",
]
GATE_ENV_KEYS = {
    "OAUTH2_PROXY_CLIENT_ID",
    "OAUTH2_PROXY_CLIENT_SECRET",
    "OAUTH2_PROXY_COOKIE_SECRET",
}


def test_gate_command_flags_are_exactly_the_reviewed_ones():
    command = _gate()["command"]
    assert all(arg.startswith("--") and "=" in arg for arg in command), command
    assert [arg.split("=", 1)[0] for arg in command] == GATE_FLAGS


def test_gate_environment_keys_are_exactly_the_reviewed_ones():
    gate = _gate()
    assert "env_file" not in gate and "entrypoint" not in gate
    assert set(gate["environment"]) == GATE_ENV_KEYS


def test_gate_trusts_forwarded_headers_only_from_the_traefik_subnet():
    command = _gate()["command"]
    assert "--reverse-proxy=true" in command
    assert "--trusted-proxy-ip=${TRAEFIK_SUBNET:?set TRAEFIK_SUBNET in .env}" in command
    names = [line.split("=", 1)[0] for line in Path(".env.example").read_text().splitlines()]
    assert "TRAEFIK_SUBNET" in names


def test_gate_has_no_skip_auth_or_trusted_ip_setting():
    gate = _gate()
    text = " ".join(gate["command"]) + " " + " ".join(gate["environment"])
    for banned in ("skip-auth", "skip_auth", "SKIP_AUTH", "trusted-ip=", "TRUSTED_IPS"):
        assert banned not in text, banned


def test_gate_image_is_at_or_above_the_patched_version():
    image = _gate()["image"]
    repo, tag = image.rsplit(":", 1)
    assert repo == "quay.io/oauth2-proxy/oauth2-proxy"
    assert tag.startswith("v")
    version = tuple(int(part) for part in tag[1:].split("-", 1)[0].split("."))
    assert version >= GATE_MIN_VERSION


def test_app_and_db_tell_traefik_to_ignore_them():
    c = yaml.safe_load(Path("compose.yaml").read_text())["services"]
    for name in ("app", "db"):
        assert "traefik.enable=false" in c[name].get("labels", []), name


# ---- every variable is required: compose refuses to start with one empty or unset ----

REQUIRED = [
    "APP_IMAGE",
    "APP_HOST",
    "OWNER_EMAIL",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "OAUTH2_PROXY_CLIENT_ID",
    "OAUTH2_PROXY_CLIENT_SECRET",
    "OAUTH2_PROXY_COOKIE_SECRET",
    "TRAEFIK_NETWORK",
    "TRAEFIK_SUBNET",
    "TRAEFIK_ENTRYPOINT",
    "TRAEFIK_CERTRESOLVER",
]
# Dummy values only: example names, a loopback range.
DUMMY = {name: "dummy" for name in REQUIRED} | {
    "APP_IMAGE": "ghcr.io/example/pubmedgemini:dummy",
    "APP_HOST": "app.example.org",
    "OWNER_EMAIL": "owner@example.org",
    "DATABASE_URL": "postgresql://postgres:dummy@db:5432/postgres",
    "TRAEFIK_SUBNET": "127.0.0.0/8",
}


def test_every_variable_uses_the_required_form_with_a_message_naming_it():
    text = Path("compose.yaml").read_text()
    uses = re.findall(r"\$\{([^}]*)\}", text)
    assert uses
    for use in uses:
        name = re.match(r"[A-Z0-9_]+", use).group(0)
        assert use == f"{name}:?set {name} in .env", use
    assert sorted({u.split(":", 1)[0] for u in uses}) == sorted(REQUIRED)
    names = [line.split("=", 1)[0] for line in Path(".env.example").read_text().splitlines()
             if line and not line.startswith("#")]
    assert sorted(names) == sorted(REQUIRED)


def _compose_cli_missing() -> bool:
    if shutil.which("docker") is None:
        return True
    r = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    return r.returncode != 0


needs_compose = pytest.mark.skipif(
    _compose_cli_missing(), reason="the docker CLI with the compose plugin is not installed"
)


def _compose_config(tmp_path, values: dict):
    env_file = tmp_path / "test.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    # A clean environment: a variable set in the shell would override the file.
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    return subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "--env-file", str(env_file),
         "config", "--quiet"],
        capture_output=True, text=True, check=False, env=env, timeout=60,
    )


@needs_compose
def test_a_complete_env_file_validates(tmp_path):
    r = _compose_config(tmp_path, DUMMY)
    assert r.returncode == 0, r.stderr


@needs_compose
@pytest.mark.parametrize("name", REQUIRED)
def test_an_empty_required_variable_stops_compose_and_names_it(tmp_path, name):
    r = _compose_config(tmp_path, DUMMY | {name: ""})
    assert r.returncode != 0
    assert f"required variable {name} is missing a value" in r.stderr, r.stderr
    for value in DUMMY.values():
        assert value not in r.stderr  # the message never shows a value


@needs_compose
@pytest.mark.parametrize("name", ["TRAEFIK_SUBNET", "OAUTH2_PROXY_COOKIE_SECRET"])
def test_an_unset_required_variable_stops_compose(tmp_path, name):
    r = _compose_config(tmp_path, {k: v for k, v in DUMMY.items() if k != name})
    assert r.returncode != 0
    assert f"required variable {name} is missing a value" in r.stderr, r.stderr
