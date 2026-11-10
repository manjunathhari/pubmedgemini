import re
import tomllib
from pathlib import Path

PIN = re.compile(r"^([A-Za-z0-9_.-]+)==\S+")


def _name(requirement: str) -> str:
    return re.split(r"[\[<>=!~; ]", requirement, maxsplit=1)[0].lower().replace("_", "-")


def _pins() -> dict[str, str]:
    text = Path("requirements.lock").read_text()
    entries = re.split(r"\n(?=[A-Za-z0-9])", text)
    pins = {}
    for entry in entries:
        m = PIN.match(entry)
        if m:
            pins[m.group(1).lower()] = entry
    return pins


def test_lock_pins_every_runtime_dependency_with_hashes():
    deps = tomllib.loads(Path("pyproject.toml").read_text())["project"]["dependencies"]
    pins = _pins()
    for dep in deps:
        assert _name(dep) in pins, dep
    assert all("--hash=sha256:" in entry for entry in pins.values())


def test_image_and_ci_install_from_the_lock():
    for path in ("Dockerfile", ".github/workflows/ci.yml"):
        lines = Path(path).read_text().splitlines()
        assert any(
            "pip install" in line and "--require-hashes" in line and "-r requirements.lock" in line
            for line in lines
        ), path
