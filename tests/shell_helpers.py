"""Run the repo's shell scripts with the fake docker in tests/fakes first on PATH."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"


def _env(state: Path, env: dict | None) -> dict:
    state.mkdir(parents=True, exist_ok=True)
    return os.environ | {
        "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(state / "calls.log"),
        "FAKE_DOCKER_STATE": str(state),
    } | (env or {})


def start_script(script: str, *args: str, cwd: Path, state: Path, env: dict | None = None,
                 **popen):
    """Start the script in its own process group, so a test can signal it like an SSH drop."""
    return subprocess.Popen(
        ["bash", str(ROOT / script), *args], cwd=cwd, env=_env(state, env),
        stdin=subprocess.DEVNULL, start_new_session=True, **popen,
    )


def run_script(script: str, *args: str, cwd: Path, state: Path, env: dict | None = None):
    full_env = _env(state, env)
    return subprocess.run(
        ["bash", str(ROOT / script), *args],
        check=False, cwd=cwd, env=full_env, capture_output=True, text=True, timeout=60,
    )


def docker_calls(state: Path) -> list[str]:
    log = state / "calls.log"
    return log.read_text().splitlines() if log.exists() else []
