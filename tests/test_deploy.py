import stat

from shell_helpers import docker_calls, run_script

OLD = "ghcr.io/example/pubmedgemini:old"
NEW = "ghcr.io/example/pubmedgemini:new"
ENV = f"POSTGRES_PASSWORD=dummy\nAPP_IMAGE={OLD}\nAPP_HOST=app.example.org\n"
FAST = {"HEALTH_TRIES": "2", "HEALTH_INTERVAL": "0"}


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


def _deploy(app, state, *args, **env):
    return run_script(
        "infra/deploy.sh", *args, cwd=app, state=state, env={"APP_DIR": str(app)} | FAST | env
    )


def _running(state) -> str:
    return (state / "running_image").read_text().strip()


def _assert_restored(app, state):
    assert (app / ".env").read_text() == ENV
    assert stat.S_IMODE((app / ".env").stat().st_mode) == 0o600
    assert (app / "compose.yaml").read_text() == "old compose\n"
    assert not (app / ".env.previous").exists()
    assert not (app / "compose.yaml.previous").exists()


def test_success_switches_image_and_compose_file(tmp_path):
    app, state = _vps(tmp_path)
    r = _deploy(app, state, NEW)
    assert r.returncode == 0, r.stderr
    assert (app / ".env").read_text() == ENV.replace(OLD, NEW)
    assert stat.S_IMODE((app / ".env").stat().st_mode) == 0o600
    assert (app / "compose.yaml").read_text() == "new compose\n"
    assert sorted(p.name for p in app.iterdir()) == [".env", "compose.yaml"]
    assert _running(state) == NEW
    calls = docker_calls(state)
    assert "compose pull app gate" in calls and "compose up -d" in calls
    assert "deploy ok" in r.stdout


def test_failed_pull_restores_both_files_and_exits_non_zero(tmp_path):
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
    assert (app / ".env").read_text() == ENV
    assert (app / "compose.yaml").read_text() == "old compose\n"


def test_missing_app_image_line_changes_nothing(tmp_path):
    env_text = "POSTGRES_PASSWORD=dummy\n"
    app, state = _vps(tmp_path, env_text)
    r = _deploy(app, state, NEW)
    assert r.returncode != 0
    assert "no APP_IMAGE= line" in r.stderr
    assert (app / ".env").read_text() == env_text
    assert (app / "compose.yaml").read_text() == "old compose\n"
    assert sorted(p.name for p in app.iterdir()) == [".env", "compose.yaml", "compose.yaml.new"]
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
