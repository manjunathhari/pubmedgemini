import os
import signal
import stat
import subprocess
import time

import pytest
from shell_helpers import docker_calls, run_script, start_script

OLD = "ghcr.io/example/pubmedgemini:old"
NEW = "ghcr.io/example/pubmedgemini:new"
ENV = f"POSTGRES_PASSWORD=dummy\nAPP_IMAGE={OLD}\nAPP_HOST=app.example.org\n"
FAST = {"HEALTH_TRIES": "2", "HEALTH_INTERVAL": "0"}
CLEAN = [".env", "compose.yaml"]


def _vps(tmp_path, env_text=ENV):
    """An app folder as the workflow leaves it: old files plus compose.yaml.new."""
    app = tmp_path / "app"
    app.mkdir()
    (app / ".env").write_text(env_text)
    (app / ".env").chmod(0o600)
    (app / "compose.yaml").write_text("old compose\n")
    (app / "compose.yaml.new").write_text("new compose\n")
    state = tmp_path / "state"
    state.mkdir()
    (state / "running_image").write_text(OLD + "\n")
    return app, state


def _env(app, env):
    return {"APP_DIR": str(app)} | FAST | env


def _deploy(app, state, *args, **env):
    return run_script("infra/deploy.sh", *args, cwd=app, state=state, env=_env(app, env))


def _running(state) -> str:
    return (state / "running_image").read_text().strip()


def _names(app) -> list[str]:
    return sorted(p.name for p in app.iterdir())


def _assert_restored(app, state, env_text=ENV):
    assert (app / ".env").read_bytes() == env_text.encode()  # bytes: keeps a CR visible
    assert stat.S_IMODE((app / ".env").stat().st_mode) == 0o600
    assert (app / "compose.yaml").read_text() == "old compose\n"
    assert _names(app) == CLEAN  # no .previous, .new or temporary files left


def test_success_switches_image_and_compose_file(tmp_path):
    app, state = _vps(tmp_path)
    r = _deploy(app, state, NEW)
    assert r.returncode == 0, r.stderr
    assert (app / ".env").read_text() == ENV.replace(OLD, NEW)
    assert stat.S_IMODE((app / ".env").stat().st_mode) == 0o600
    assert (app / "compose.yaml").read_text() == "new compose\n"
    assert _names(app) == CLEAN
    assert _running(state) == NEW
    calls = docker_calls(state)
    assert "compose pull app gate" in calls and "compose up -d" in calls
    assert "deploy ok" in r.stdout


def test_env_is_replaced_not_edited_in_place_and_keeps_its_mode(tmp_path):
    app, state = _vps(tmp_path)
    (app / ".env").chmod(0o640)
    before = (app / ".env").stat().st_ino
    r = _deploy(app, state, NEW)
    assert r.returncode == 0, r.stderr
    after = (app / ".env").stat()
    assert after.st_ino != before  # a new file moved into place
    assert stat.S_IMODE(after.st_mode) == 0o640


def test_env_previous_is_private_even_when_env_is_not(tmp_path):
    app, state = _vps(tmp_path)
    (app / ".env").chmod(0o644)
    r = _deploy(app, state, NEW, FAKE_FAIL_PULL="1")
    assert r.returncode != 0
    assert (state / "previous_mode").read_text().strip() == "600"
    assert stat.S_IMODE((app / ".env").stat().st_mode) == 0o644


def test_failed_pull_restores_all_files_and_exits_non_zero(tmp_path):
    app, state = _vps(tmp_path)
    r = _deploy(app, state, NEW, FAKE_FAIL_PULL="1")
    assert r.returncode != 0
    assert "pull failed" in r.stderr
    _assert_restored(app, state)
    assert _running(state) == OLD


def test_unhealthy_new_app_rolls_back_and_exits_non_zero(tmp_path):
    app, state = _vps(tmp_path)
    r = _deploy(app, state, NEW, FAKE_UNHEALTHY_IMAGE=NEW)
    assert r.returncode != 0
    assert "rolling back" in r.stderr
    assert "rolled back: the previous version is healthy" in r.stderr
    _assert_restored(app, state)
    assert docker_calls(state).count("compose up -d") == 2  # the new version, then the old one
    assert _running(state) == OLD


def test_gate_not_running_rolls_back(tmp_path):
    app, state = _vps(tmp_path)
    r = _deploy(app, state, NEW, FAKE_GATE_DOWN="1")
    assert r.returncode != 0
    _assert_restored(app, state)


def test_failed_up_rolls_back(tmp_path):
    app, state = _vps(tmp_path)
    r = _deploy(app, state, NEW, FAKE_FAIL_UP="1")
    assert r.returncode != 0
    _assert_restored(app, state)


def test_missing_app_image_line_changes_nothing(tmp_path):
    env_text = "POSTGRES_PASSWORD=dummy\n"
    app, state = _vps(tmp_path, env_text)
    r = _deploy(app, state, NEW)
    assert r.returncode != 0
    assert "no APP_IMAGE= line" in r.stderr
    assert (app / ".env").read_text() == env_text
    assert (app / "compose.yaml").read_text() == "old compose\n"
    assert _names(app) == sorted(CLEAN + ["compose.yaml.new"])
    assert docker_calls(state) == []


def test_missing_env_file_changes_nothing(tmp_path):
    app, state = _vps(tmp_path)
    (app / ".env").unlink()
    r = _deploy(app, state, NEW)
    assert r.returncode != 0 and ".env is missing" in r.stderr
    assert docker_calls(state) == []


def test_bad_image_name_is_refused(tmp_path):
    app, state = _vps(tmp_path)
    r = _deploy(app, state, "ghcr.io/x|y")
    assert r.returncode != 0
    assert (app / ".env").read_text() == ENV and docker_calls(state) == []


def test_first_deploy_with_empty_app_image(tmp_path):
    app, state = _vps(tmp_path, "APP_IMAGE=\n")
    (app / "compose.yaml").unlink()
    (state / "running_image").write_text("")
    r = _deploy(app, state, NEW)
    assert r.returncode == 0, r.stderr
    assert (app / ".env").read_text() == f"APP_IMAGE={NEW}\n"


# ---- interrupted deploys ----


def _wait_for(path, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.02)


def _interrupt(app, state, sig, block_on, **popen):
    env = _env(app, {"FAKE_BLOCK_ON": block_on})
    popen = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True} | popen
    proc = start_script("infra/deploy.sh", NEW, cwd=app, state=state, env=env, **popen)
    try:
        _wait_for(state / "blocked")
        os.killpg(proc.pid, sig)  # the whole group, as an SSH drop or a cancelled job does
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    return proc.returncode, out, err


@pytest.mark.parametrize("block_on", ["pull", "health"])
@pytest.mark.parametrize("sig", [signal.SIGHUP, signal.SIGTERM], ids=["HUP", "TERM"])
def test_signal_mid_deploy_restores_the_previous_version(tmp_path, sig, block_on):
    app, state = _vps(tmp_path)
    code, _, err = _interrupt(app, state, sig, block_on)
    assert code != 0
    _assert_restored(app, state)
    assert _running(state) == OLD
    assert docker_calls(state)[-1].startswith("inspect")  # brought back up and checked
    assert "compose up -d" in docker_calls(state)
    if sig == signal.SIGTERM:
        assert "interrupted by SIGTERM" in err and "rolled back" in err


def test_hangup_with_output_gone_still_rolls_back(tmp_path):
    """SSH dropped: stdout and stderr are pipes nobody reads, so log writes fail."""
    app, state = _vps(tmp_path)
    r_out, w_out = os.pipe()
    os.close(r_out)  # writes to w_out now fail with EPIPE / SIGPIPE
    try:
        code, _, _ = _interrupt(app, state, signal.SIGHUP, "pull",
                                stdout=w_out, stderr=w_out, text=False)
    finally:
        os.close(w_out)
    assert code != 0
    _assert_restored(app, state)
    assert _running(state) == OLD


def _half_switched(tmp_path):
    """What a deploy killed outright (no handler ran) leaves: new files plus the copies."""
    app, state = _vps(tmp_path)
    half = "ghcr.io/example/pubmedgemini:half"
    (app / ".env.previous").write_text(ENV)
    (app / ".env.previous").chmod(0o600)
    (app / "compose.yaml.previous").write_text("old compose\n")
    (app / ".env").write_text(ENV.replace(OLD, half))
    (app / "compose.yaml").write_text("half compose\n")
    (app / ".deploy-tmp.abc123").write_text("partial\n")
    (state / "running_image").write_text(half + "\n")
    return app, state


def test_leftover_state_is_restored_before_a_failing_deploy_rolls_back(tmp_path):
    app, state = _half_switched(tmp_path)
    r = _deploy(app, state, NEW, FAKE_UNHEALTHY_IMAGE=NEW)
    assert r.returncode != 0
    assert "interrupted earlier deploy" in r.stderr
    _assert_restored(app, state)  # the true previous version, not the half-switched one
    assert _running(state) == OLD


def test_leftover_state_is_restored_before_a_good_deploy(tmp_path):
    app, state = _half_switched(tmp_path)
    r = _deploy(app, state, NEW)
    assert r.returncode == 0, r.stderr
    assert (app / ".env").read_text() == ENV.replace(OLD, NEW)
    assert _names(app) == CLEAN


def test_stray_compose_copy_without_env_copy_is_discarded(tmp_path):
    """Interrupted while saving: nothing had changed yet, so the copy is not used."""
    app, state = _vps(tmp_path)
    (app / "compose.yaml.previous").write_text("stale compose\n")
    r = _deploy(app, state, NEW, FAKE_UNHEALTHY_IMAGE=NEW)
    assert r.returncode != 0
    _assert_restored(app, state)

