"""Run the repo's shell scripts with the fake docker in tests/fakes first on PATH."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAKES = ROOT / "tests" / "fakes"


def run_script(script: str, *args: str, cwd: Path, state: Path, env: dict | None = None):
    state.mkdir(parents=True, exist_ok=True)
    full_env = os.environ | {
        "PATH": f"{FAKES}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(state / "calls.log"),
        "FAKE_DOCKER_STATE": str(state),
    } | (env or {})
    return subprocess.run(
        ["bash", str(ROOT / script), *args],
        check=False, cwd=cwd, env=full_env, capture_output=True, text=True, timeout=60,
    )


def docker_calls(state: Path) -> list[str]:
    log = state / "calls.log"
    return log.read_text().splitlines() if log.exists() else []
