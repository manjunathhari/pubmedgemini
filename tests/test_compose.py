from pathlib import Path

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


def test_gate_has_no_skip_auth_rule():
    flags = " ".join(_gate()["command"])
    assert "skip-auth" not in flags and "skip_auth" not in flags


def test_gate_image_is_at_or_above_the_patched_version():
    image = _gate()["image"]
    repo, tag = image.rsplit(":", 1)
    assert repo == "quay.io/oauth2-proxy/oauth2-proxy"
    assert tag.startswith("v")
    version = tuple(int(part) for part in tag[1:].split("-", 1)[0].split("."))
    assert version >= GATE_MIN_VERSION
