import json
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
    "--cookie-samesite",
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


def test_gate_cookie_is_samesite_lax():
    # oauth2-proxy v7.15.6: flag `cookie-samesite` (pkg/apis/options/cookie.go), "lax" parsed
    # by pkg/cookies/cookies.go ParseSameSite; the CSRF cookie inherits it (pkg/cookies/csrf.go
    # getCSRFSameSite). Lax still goes with Google's callback, a top-level GET.
    assert "--cookie-samesite=lax" in _gate()["command"]


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


# ---- every variable is required (compose refuses to start with one empty or unset), except
# the two reviewed optional ones, which only the app reads and which may be left out ----

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
# Optional on purpose: the app runs without them (Settings turns "" into None, or into the
# default). F1: the NCBI pair. F2: the Gemini key (questions are off without it) and the
# model and free-tier limits (blank means the default).
GEMINI_OPTIONAL = ["GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_RPM", "GEMINI_TPM", "GEMINI_RPD"]
OPTIONAL = ["NCBI_API_KEY", "NCBI_EMAIL", *GEMINI_OPTIONAL]
APP_ENV_KEYS = {"APP_ENV", "DATABASE_URL", "OWNER_EMAIL", "NCBI_API_KEY", "NCBI_EMAIL",
                *GEMINI_OPTIONAL}
# Dummy values only: example names, a loopback range.
DUMMY = {name: "dummy" for name in REQUIRED} | {
    "APP_IMAGE": "ghcr.io/example/pubmedgemini:dummy",
    "APP_HOST": "app.example.org",
    "OWNER_EMAIL": "owner@example.org",
    "DATABASE_URL": "postgresql://postgres:dummy@db:5432/postgres",
    "TRAEFIK_SUBNET": "127.0.0.0/8",
}


def _uses() -> list[str]:
    return re.findall(r"\$\{([^}]*)\}", Path("compose.yaml").read_text())


def test_every_variable_uses_the_required_form_except_the_reviewed_optional_ones():
    uses = _uses()
    assert uses
    for use in uses:
        name = re.match(r"[A-Z0-9_]+", use).group(0)
        if name in OPTIONAL:
            assert use == f"{name}:-", use  # empty when unset; nothing else allowed
        else:
            assert name in REQUIRED, use
            assert use == f"{name}:?set {name} in .env", use
    assert sorted({re.match(r"[A-Z0-9_]+", u).group(0) for u in uses}) == sorted(
        REQUIRED + OPTIONAL
    )
    names = [line.split("=", 1)[0] for line in Path(".env.example").read_text().splitlines()
             if line and not line.startswith("#")]
    assert sorted(names) == sorted(REQUIRED + OPTIONAL)
    assert names[-len(OPTIONAL):] == OPTIONAL  # listed after the required names


def test_required_and_optional_names_do_not_overlap():
    assert not set(REQUIRED) & set(OPTIONAL)


def test_app_environment_keys_are_exactly_the_reviewed_ones():
    app = yaml.safe_load(Path("compose.yaml").read_text())["services"]["app"]
    assert "env_file" not in app
    assert set(app["environment"]) == APP_ENV_KEYS
    for name in OPTIONAL:
        assert app["environment"][name] == f"${{{name}:-}}"


def test_optional_variables_are_used_by_app_only():
    services = yaml.safe_load(Path("compose.yaml").read_text())["services"]
    for service_name, service in services.items():
        if service_name == "app":
            continue
        text = yaml.safe_dump(service)
        for name in OPTIONAL:
            assert name not in text, (service_name, name)
    body = Path("compose.yaml").read_text().split("services:", 1)[1]
    for name in OPTIONAL:
        assert body.count(name) == 2, name  # app's key and its ${NAME:-} value, nothing else


def test_compose_header_names_the_optional_ones():
    header = Path("compose.yaml").read_text().split("services:", 1)[0]
    assert all(name in header for name in OPTIONAL)


def _compose_cli_missing() -> bool:
    if shutil.which("docker") is None:
        return True
    r = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    return r.returncode != 0


needs_compose = pytest.mark.skipif(
    _compose_cli_missing(), reason="the docker CLI with the compose plugin is not installed"
)


def _compose_config(tmp_path, values: dict, *args: str):
    env_file = tmp_path / "test.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    # A clean environment: a variable set in the shell would override the file.
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    return subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "--env-file", str(env_file),
         "config", *(args or ("--quiet",))],
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


@needs_compose
def test_optional_variables_may_be_unset(tmp_path):
    assert not set(DUMMY) & set(OPTIONAL)  # DUMMY holds the required names only
    assert _compose_config(tmp_path, DUMMY).returncode == 0
    r = _compose_config(tmp_path, DUMMY, "--format", "json")
    assert r.returncode == 0, r.stderr
    env = json.loads(r.stdout)["services"]["app"]["environment"]
    assert (env["NCBI_API_KEY"], env["NCBI_EMAIL"]) == ("", "")
    assert env["GEMINI_API_KEY"] == ""  # no key: questions are off, the rest works
    assert all(env[name] == "" for name in GEMINI_OPTIONAL)


@needs_compose
def test_optional_variables_reach_the_app_when_set(tmp_path):
    values = DUMMY | {"NCBI_API_KEY": "dummy-key", "NCBI_EMAIL": "dev@example.org",
                      "GEMINI_API_KEY": "dummy-gemini-key", "GEMINI_MODEL": "dummy-model",
                      "GEMINI_RPM": "9", "GEMINI_TPM": "200000", "GEMINI_RPD": "400"}
    r = _compose_config(tmp_path, values, "--format", "json")
    assert r.returncode == 0, r.stderr
    env = json.loads(r.stdout)["services"]["app"]["environment"]
    assert (env["NCBI_API_KEY"], env["NCBI_EMAIL"]) == ("dummy-key", "dev@example.org")
    assert [env[n] for n in GEMINI_OPTIONAL] == ["dummy-gemini-key", "dummy-model", "9",
                                                  "200000", "400"]
    services = json.loads(r.stdout)["services"]
    for name in ("db", "gate"):
        assert "dummy-gemini-key" not in json.dumps(services[name]), name


@needs_compose
def test_empty_optional_variables_validate(tmp_path):
    r = _compose_config(tmp_path, DUMMY | {name: "" for name in OPTIONAL})
    assert r.returncode == 0, r.stderr


@needs_compose
def test_the_servers_env_without_the_gemini_names_passes_the_deploy_check(tmp_path):
    """The server's .env as it is before the runbook's Gemini step (F1's names, none of
    GEMINI_*), checked exactly as infra/deploy.sh checks compose.yaml.new before it switches
    anything (with APP_IMAGE set by the deploy)."""
    values = (DUMMY | {"APP_IMAGE": "", "NCBI_API_KEY": "dummy-key",
                       "NCBI_EMAIL": "dev@example.org"})
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    assert "GEMINI" not in env_file.read_text()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "APP_IMAGE": "ghcr.io/example/pubmedgemini:dummy"}
    r = subprocess.run(
        ["docker", "compose", "-f", str(Path("compose.yaml").resolve()), "--env-file",
         str(env_file), "config", "--quiet"],
        capture_output=True, text=True, check=False, env=env, timeout=60, cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    deploy = Path("infra/deploy.sh").read_text()
    assert ('APP_IMAGE="$new_image" docker compose -f compose.yaml.new --env-file .env \\'
            in deploy)
