import datetime
import stat

from shell_helpers import run_script


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_backup_is_private_and_keeps_the_last_seven_dumps(tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o755)
    backups.chmod(0o755)
    old = [f"2020-01-0{d}.dump" for d in range(1, 10)]
    for name in old:
        (backups / name).write_text("old")
    r = run_script(
        "infra/backup.sh", cwd=tmp_path, state=tmp_path / "state",
        env={"COMPOSE_DIR": str(tmp_path), "BACKUP_DIR": str(backups)},
    )
    assert r.returncode == 0, r.stderr
    today = backups / f"{datetime.date.today():%Y-%m-%d}.dump"
    assert f"backup ok: {today}" in r.stdout
    assert _mode(backups) == 0o700
    assert _mode(today) == 0o600
    assert sorted(p.name for p in backups.glob("*.dump")) == old[3:] + [today.name]


def test_new_backup_dir_is_made_private(tmp_path):
    backups = tmp_path / "new" / "backups"
    r = run_script(
        "infra/backup.sh", cwd=tmp_path, state=tmp_path / "state",
        env={"COMPOSE_DIR": str(tmp_path), "BACKUP_DIR": str(backups)},
    )
    assert r.returncode == 0, r.stderr
    assert _mode(backups) == 0o700


def test_empty_dump_fails_and_leaves_no_file(tmp_path):
    backups = tmp_path / "backups"
    r = run_script(
        "infra/backup.sh", cwd=tmp_path, state=tmp_path / "state",
        env={"COMPOSE_DIR": str(tmp_path), "BACKUP_DIR": str(backups), "FAKE_DUMP_EMPTY": "1"},
    )
    assert r.returncode != 0 and "dump is empty" in r.stderr
    assert list(backups.iterdir()) == []
