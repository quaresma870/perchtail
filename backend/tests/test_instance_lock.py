"""One PerchTail process per DATA_DIR, and no migrating a database a newer
version left behind (#334)."""

import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from app import instance_lock
from app.config import Settings
from app.instance_lock import InstanceLockHeldError
from app.schema_version import database_revision, is_known_revision
from app.version import APP_VERSION
from sqlalchemy import create_engine

BACKEND = Path(__file__).resolve().parents[1]
UNKNOWN_REVISION = "ffffffffffff"
# An older revision tests/test_migrations_keep_autoincrement.py also uses.
OLDER_REVISION = "c8e3f1a62d07"


def _settings(data_dir: Path) -> Settings:
    return Settings(data_dir=str(data_dir))


def _env(tmp_path: Path) -> dict[str, str]:
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path / "db"),
        "LOG_DIR": str(tmp_path / "logs"),
        "SCRATCH_DIR": str(tmp_path / "scratch"),
    }
    for name in ("DATABASE_URL", "CREDENTIAL_SALT_PATH", "SSH_KNOWN_HOSTS_PATH"):
        env.pop(name, None)
    return env


def _alembic(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND,
        env=_env(tmp_path),
        capture_output=True,
        text=True,
    )


def _db_revision(tmp_path: Path) -> str | None:
    with sqlite3.connect(tmp_path / "db" / "perchtail.db") as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def _set_db_revision(tmp_path: Path, revision: str) -> None:
    with sqlite3.connect(tmp_path / "db" / "perchtail.db") as conn:
        conn.execute("UPDATE alembic_version SET version_num = ?", (revision,))


def _events(output: str) -> list[dict]:
    events = []
    for line in output.splitlines():
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "event" in parsed:
            events.append(parsed)
    return events


def test_acquire_writes_who_holds_it_in_a_private_file(tmp_path):
    lock = instance_lock.acquire(_settings(tmp_path))
    try:
        path = tmp_path / ".perchtail.lock"
        assert lock.path == path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        details = json.loads(path.read_text())
        assert details["pid"] == os.getpid()
        assert details["version"] == APP_VERSION
        assert details["host"]
        assert details["started_at"]
    finally:
        lock.release()


def test_a_second_acquire_is_refused_with_the_first_ones_details(tmp_path):
    first = instance_lock.acquire(_settings(tmp_path))
    try:
        with pytest.raises(InstanceLockHeldError) as refused:
            instance_lock.acquire(_settings(tmp_path))
        assert refused.value.holder["pid"] == os.getpid()
        assert refused.value.holder["version"] == APP_VERSION
        assert "pid" in refused.value.content
    finally:
        first.release()


def test_release_lets_the_next_one_in_and_is_safe_twice(tmp_path):
    first = instance_lock.acquire(_settings(tmp_path))
    first.release()
    first.release()
    assert not first.held
    second = instance_lock.acquire(_settings(tmp_path))
    second.release()


def test_another_process_holding_it_is_named(tmp_path):
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from app import instance_lock\n"
            "from app.config import Settings\n"
            f"instance_lock.acquire(Settings(data_dir={str(tmp_path)!r}))\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n",
        ],
        cwd=BACKEND,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        with pytest.raises(InstanceLockHeldError) as refused:
            instance_lock.acquire(_settings(tmp_path))
        assert refused.value.holder["pid"] == holder.pid

        # SIGKILL: no chance to release, the operating system does it.
        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=10)
        lock = instance_lock.acquire(_settings(tmp_path))
        assert json.loads(lock.path.read_text())["pid"] == os.getpid()
        lock.release()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
        holder.stdout.close()


def test_alembic_upgrade_refuses_while_an_instance_runs(tmp_path):
    assert _alembic(tmp_path, "upgrade", OLDER_REVISION).returncode == 0
    lock = instance_lock.acquire(_settings(tmp_path / "db"))
    try:
        result = _alembic(tmp_path, "upgrade", "head")
    finally:
        lock.release()

    assert result.returncode != 0
    held = [e for e in _events(result.stdout) if e["event"] == "startup.instance_lock_held"]
    assert held and held[0]["level"] == "critical"
    assert held[0]["holder_pid"] == os.getpid()
    assert held[0]["holder_version"] == APP_VERSION
    assert held[0]["holder_host"] and held[0]["holder_started_at"]
    assert _db_revision(tmp_path) == OLDER_REVISION

    # And once it's gone, the upgrade goes through and releases the lock.
    assert _alembic(tmp_path, "upgrade", "head").returncode == 0
    assert _db_revision(tmp_path) != OLDER_REVISION
    instance_lock.acquire(_settings(tmp_path / "db")).release()


def test_alembic_upgrade_refuses_a_database_newer_than_the_app(tmp_path):
    assert _alembic(tmp_path, "upgrade", "head").returncode == 0
    _set_db_revision(tmp_path, UNKNOWN_REVISION)

    result = _alembic(tmp_path, "upgrade", "head")

    assert result.returncode != 0
    newer = [e for e in _events(result.stdout) if e["event"] == "startup.database_newer_than_app"]
    assert newer and newer[0]["level"] == "critical"
    assert newer[0]["database_revision"] == UNKNOWN_REVISION
    assert newer[0]["app_version"] == APP_VERSION
    assert "Can't locate revision" not in result.stderr
    assert _db_revision(tmp_path) == UNKNOWN_REVISION
    # The refusal released the lock on its way out.
    instance_lock.acquire(_settings(tmp_path / "db")).release()


def test_database_revision_and_is_known_revision(tmp_path):
    assert _alembic(tmp_path, "upgrade", OLDER_REVISION).returncode == 0
    engine = create_engine(f"sqlite:///{tmp_path / 'db' / 'perchtail.db'}")
    try:
        assert database_revision(engine) == OLDER_REVISION
        assert is_known_revision(OLDER_REVISION)
        _set_db_revision(tmp_path, UNKNOWN_REVISION)
        assert database_revision(engine) == UNKNOWN_REVISION
        assert not is_known_revision(UNKNOWN_REVISION)
    finally:
        engine.dispose()

    fresh = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        assert database_revision(fresh) is None
        assert is_known_revision(None)
    finally:
        fresh.dispose()
