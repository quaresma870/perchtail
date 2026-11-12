"""Encrypted online backups, verification and restore (#334)."""

import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.script import ScriptDirectory
from app import backup, crypto, instance_lock, system_settings
from app.api.auth import get_current_active_user
from app.api.system_settings import router as system_settings_router
from app.auth.models import AuditLog, GlobalCapability, Role, User
from app.backup import BLOCK_BYTES, HEADER_BYTES, TAG_BYTES, BackupError
from app.config import get_settings
from app.db import configure_sqlite_connection, get_session
from app.models import Customer, Protocol, Source
from app.schema_version import MIGRATIONS_DIR
from app.version import APP_VERSION
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

KEY = "test-credential-key-for-backups"
BACKUP_KEY = "a-separate-backup-key"
MFA_SECRET = "JBSWY3DPEHPK3PXP"
CREDENTIAL = {"username": "svc", "password": "s3cret"}
ON_DISK_BLOCK = BLOCK_BYTES + TAG_BYTES
HEAD = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()


def _clear_caches() -> None:
    get_settings.cache_clear()
    crypto._fernet.cache_clear()
    crypto.audit_chain_key.cache_clear()


def _engine(url: str):
    engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(engine, "connect", configure_sqlite_connection)
    return engine


@pytest.fixture()
def env(monkeypatch, tmp_path):
    data = tmp_path / "db"
    backups = tmp_path / "backups"
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("BACKUP_DIR", str(backups))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", KEY)
    for name in (
        "DATABASE_URL",
        "CREDENTIAL_SALT_PATH",
        "SSH_KNOWN_HOSTS_PATH",
        "BACKUP_ENCRYPTION_KEY",
        "BACKUP_KEEP",
    ):
        monkeypatch.delenv(name, raising=False)
    _clear_caches()
    settings = get_settings()
    data.mkdir()
    engine = _engine(settings.database_url)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql(f"INSERT INTO alembic_version VALUES ('{HEAD}')")
    with Session(engine) as session:
        role = Role(name="r")
        session.add(role)
        session.commit()
        session.add(
            Source(
                name="app01",
                protocol=Protocol.ssh,
                host="app01.example.com",
                base_path="/var/log",
                credential_ref=crypto.encrypt_credential(CREDENTIAL),
            )
        )
        session.add(
            User(
                username="alice",
                role_id=role.id,
                mfa_secret_encrypted=crypto.encrypt_secret(MFA_SECRET),
            )
        )
        session.commit()
    Path(settings.ssh_known_hosts_path).write_text("app01.example.com ssh-ed25519 AAAA\n")
    monkeypatch.setattr("app.db.engine", engine)
    monkeypatch.setattr(backup, "_last_failed_attempt", None)
    yield SimpleNamespace(settings=settings, engine=engine, data=data, backups=backups)
    engine.dispose()
    _clear_caches()


def _reload(monkeypatch, **env) -> object:
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    _clear_caches()
    return get_settings()


def _add_filler(engine, size: int = 3 * BLOCK_BYTES) -> None:
    """Random bytes, so the archive spans several blocks."""
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE IF NOT EXISTS filler (b BLOB)")
        connection.exec_driver_sql("INSERT INTO filler VALUES (?)", (os.urandom(size),))


def _members(settings, path: Path) -> dict[str, bytes]:
    with open(path, "rb") as fh:
        decryptor = backup._Decryptor(fh, settings)
        members = {}
        with tarfile.open(fileobj=decryptor, mode="r|") as tar:
            for member in tar:
                members[member.name] = tar.extractfile(member).read()
        decryptor.drain()
    return members


def _craft(settings, path: Path, members: list[tuple[str, bytes]]) -> Path:
    with open(path, "wb") as out:
        encryptor = backup._Encryptor(out, settings)
        with tarfile.open(fileobj=encryptor, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        encryptor.finish()
    return path


def _recraft(settings, source: Path, dest: Path, *, manifest=None, files=None) -> Path:
    """A copy of a real backup with its manifest and files changed, and the
    manifest's hashes matching the files unless `manifest` says otherwise."""
    members = _members(settings, source)
    current = json.loads(members.pop("manifest.json"))
    members.update(files or {})
    current["files"] = {name: hashlib.sha256(data).hexdigest() for name, data in members.items()}
    current.update(manifest or {})
    payload = [("manifest.json", json.dumps(current).encode())] + list(members.items())
    return _craft(settings, dest, payload)


def _audit_actions(engine) -> list[tuple[str, dict]]:
    with Session(engine) as session:
        rows = session.exec(select(AuditLog).order_by(AuditLog.id)).all()
        return [(row.action, row.event_metadata or {}) for row in rows]


def _tree(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != instance_lock.LOCK_FILE_NAME
    }


# --- create ----------------------------------------------------------------


def test_create_while_writing_in_parallel_gives_a_backup_verify_accepts(env):
    stop = threading.Event()
    written = []

    def writer():
        while not stop.is_set():
            with Session(env.engine) as session:
                session.add(Customer(name=f"c-{uuid.uuid4().hex}"))
                session.commit()
            written.append(1)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        path = backup.create_backup("manual")
    finally:
        stop.set()
        thread.join()
    assert written

    assert path.parent == env.backups
    assert backup.parse_name(path.name)[0] == APP_VERSION
    assert backup.parse_name(path.name)[2] == "manual"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env.backups.stat().st_mode) == 0o700
    assert path.read_bytes().startswith(b"PTBK\x01\x00")

    manifest = backup.verify_backup(path)
    assert manifest["format"] == 1
    assert manifest["app_version"] == APP_VERSION
    assert manifest["alembic_revision"] == HEAD
    assert manifest["reason"] == "manual"

    members = _members(env.settings, path)
    salt = Path(env.settings.credential_salt_path).read_bytes()
    assert members["credential_salt"] == salt
    assert manifest["files"]["credential_salt"] == hashlib.sha256(salt).hexdigest()
    assert (
        manifest["files"]["ssh_known_hosts"]
        == hashlib.sha256(Path(env.settings.ssh_known_hosts_path).read_bytes()).hexdigest()
    )
    assert manifest["files"]["perchtail.db"] == hashlib.sha256(members["perchtail.db"]).hexdigest()

    copy = env.backups / "copy.db"
    copy.write_bytes(members["perchtail.db"])
    with sqlite3.connect(copy) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT count(*) FROM source").fetchone()[0] == 1


def test_create_leaves_no_temporary_files_and_never_writes_the_key(env):
    _add_filler(env.engine, BLOCK_BYTES // 2)
    path = backup.create_backup("manual")
    assert [p.name for p in env.backups.iterdir()] == [path.name]
    assert KEY.encode() not in path.read_bytes()


def test_create_without_known_hosts_still_works(env):
    Path(env.settings.ssh_known_hosts_path).unlink()
    manifest = backup.verify_backup(backup.create_backup("manual"))
    assert set(manifest["files"]) == {"perchtail.db", "credential_salt"}


def test_create_refuses_an_unknown_reason(env):
    with pytest.raises(BackupError, match="reason"):
        backup.create_backup("weekly")


# --- verify ----------------------------------------------------------------


@pytest.fixture()
def big_backup(env):
    _add_filler(env.engine)
    path = backup.create_backup("manual")
    assert path.stat().st_size > HEADER_BYTES + 3 * ON_DISK_BLOCK
    return path


def _altered(path: Path, offset: int) -> Path:
    data = bytearray(path.read_bytes())
    data[offset] ^= 0x01
    path.write_bytes(bytes(data))
    return path


@pytest.mark.parametrize("where", ["first", "second", "last"])
def test_verify_detects_a_changed_byte_in_any_block(env, big_backup, where):
    size = big_backup.stat().st_size
    offset = {
        "first": HEADER_BYTES + 100,
        "second": HEADER_BYTES + ON_DISK_BLOCK + 100,
        "last": size - 1,
    }[where]
    with pytest.raises(BackupError):
        backup.verify_backup(_altered(big_backup, offset))


def test_verify_detects_a_removed_block(env, big_backup):
    data = big_backup.read_bytes()
    start = HEADER_BYTES + ON_DISK_BLOCK
    big_backup.write_bytes(data[:start] + data[start + ON_DISK_BLOCK :])
    with pytest.raises(BackupError, match="block 1"):
        backup.verify_backup(big_backup)


def test_verify_detects_swapped_blocks(env, big_backup):
    data = big_backup.read_bytes()
    first = HEADER_BYTES
    second = first + ON_DISK_BLOCK
    third = second + ON_DISK_BLOCK
    swapped = data[:first] + data[second:third] + data[first:second] + data[third:]
    big_backup.write_bytes(swapped)
    with pytest.raises(BackupError):
        backup.verify_backup(big_backup)


def test_verify_detects_a_missing_last_block(env, big_backup):
    data = big_backup.read_bytes()
    blocks = (len(data) - HEADER_BYTES) // ON_DISK_BLOCK
    big_backup.write_bytes(data[: HEADER_BYTES + blocks * ON_DISK_BLOCK])
    if big_backup.stat().st_size == len(data):  # the last block happened to be full
        big_backup.write_bytes(data[: HEADER_BYTES + (blocks - 1) * ON_DISK_BLOCK])
    with pytest.raises(BackupError, match="truncated|altered"):
        backup.verify_backup(big_backup)


def test_verify_detects_a_truncated_end(env, big_backup):
    big_backup.write_bytes(big_backup.read_bytes()[:-10])
    with pytest.raises(BackupError):
        backup.verify_backup(big_backup)


@pytest.mark.parametrize(
    "offset, message",
    [
        (0, "not a PerchTail backup"),
        (4, "format version"),
        (5, "BACKUP_ENCRYPTION_KEY"),
        (10, "altered"),
        (25, "altered"),
    ],
)
def test_verify_detects_a_changed_header(env, offset, message):
    path = backup.create_backup("manual")
    with pytest.raises(BackupError, match=message):
        backup.verify_backup(_altered(path, offset))


def test_verify_detects_a_wrong_key(env, monkeypatch):
    path = backup.create_backup("manual")
    _reload(monkeypatch, CREDENTIAL_ENCRYPTION_KEY="not-the-key")
    message = "doesn't decrypt with the current CREDENTIAL_ENCRYPTION_KEY"
    with pytest.raises(BackupError, match=message):
        backup.verify_backup(path)


def test_verify_detects_an_unknown_manifest_format(env, tmp_path):
    path = backup.create_backup("manual")
    crafted = _recraft(env.settings, path, env.backups / "x.ptbackup", manifest={"format": 2})
    with pytest.raises(BackupError, match="unknown manifest format 2"):
        backup.verify_backup(crafted)


def test_verify_detects_a_hash_that_doesnt_match(env):
    path = backup.create_backup("manual")
    crafted = _recraft(
        env.settings,
        path,
        env.backups / "x.ptbackup",
        manifest={"files": {"perchtail.db": "0" * 64, "credential_salt": "0" * 64}},
    )
    with pytest.raises(BackupError, match="SHA-256|don't match"):
        backup.verify_backup(crafted)


def test_verify_detects_a_corrupted_database(env):
    path = backup.create_backup("manual")
    db = bytearray(_members(env.settings, path)["perchtail.db"])
    page = 4096
    db[2 * page : 2 * page + 200] = b"\xff" * 200  # a b-tree page's header
    dest = env.backups / "x.ptbackup"
    crafted = _recraft(env.settings, path, dest, files={"perchtail.db": bytes(db)})
    with pytest.raises(BackupError, match="damaged|quick_check"):
        backup.verify_backup(crafted)


def test_verify_refuses_unexpected_entries(env):
    path = backup.create_backup("manual")
    crafted = _recraft(env.settings, path, env.backups / "x.ptbackup", files={"../evil": b"x"})
    with pytest.raises(BackupError, match="unexpected entry"):
        backup.verify_backup(crafted)


# --- BACKUP_ENCRYPTION_KEY -------------------------------------------------


def test_backup_key_is_used_and_named_in_the_header(env, monkeypatch):
    _reload(monkeypatch, BACKUP_ENCRYPTION_KEY=BACKUP_KEY)
    path = backup.create_backup("manual")
    assert path.read_bytes()[5] == backup.KEY_BACKUP
    backup.verify_backup(path)

    _reload(monkeypatch, BACKUP_ENCRYPTION_KEY=None)
    with pytest.raises(BackupError, match="BACKUP_ENCRYPTION_KEY, which isn't set"):
        backup.verify_backup(path)

    _reload(monkeypatch, BACKUP_ENCRYPTION_KEY="another-key")
    with pytest.raises(BackupError, match="current BACKUP_ENCRYPTION_KEY"):
        backup.verify_backup(path)

    # Saying in the header that the credential key was used doesn't help:
    # the header is authenticated, and that key isn't the backup key.
    _reload(monkeypatch, BACKUP_ENCRYPTION_KEY=None, CREDENTIAL_ENCRYPTION_KEY=BACKUP_KEY)
    data = bytearray(path.read_bytes())
    data[5] = backup.KEY_CREDENTIAL
    path.write_bytes(bytes(data))
    with pytest.raises(BackupError, match="doesn't decrypt"):
        backup.verify_backup(path)


# --- restore ---------------------------------------------------------------


def test_restore_refuses_while_the_app_runs(env):
    path = backup.create_backup("manual")
    before = _tree(env.data)
    lock = instance_lock.acquire(env.settings)
    try:
        with pytest.raises(BackupError, match="PerchTail is running"):
            backup.restore_backup(path)
    finally:
        lock.release()
    assert _tree(env.data) == before
    assert not list(env.data.parent.glob("db.*"))


def test_restore_refuses_an_unknown_revision(env):
    path = backup.create_backup("manual")
    unknown = {"alembic_revision": "ffffffffffff"}
    crafted = _recraft(env.settings, path, env.backups / "x.ptbackup", manifest=unknown)
    env.engine.dispose()
    before = _tree(env.data)
    with pytest.raises(BackupError, match="revision ffffffffffff"):
        backup.restore_backup(crafted)
    assert _tree(env.data) == before
    assert not list(env.data.parent.glob("db.*"))


def test_restore_refuses_a_key_that_doesnt_decrypt_the_archive(env, monkeypatch):
    path = backup.create_backup("manual")
    env.engine.dispose()
    before = _tree(env.data)
    _reload(monkeypatch, CREDENTIAL_ENCRYPTION_KEY="not-the-key")
    with pytest.raises(BackupError, match="doesn't decrypt"):
        backup.restore_backup(path)
    assert _tree(env.data) == before
    assert not list(env.data.parent.glob("db.*"))


def test_restore_refuses_a_credential_key_that_doesnt_match_unless_forced(env, monkeypatch):
    _reload(monkeypatch, BACKUP_ENCRYPTION_KEY=BACKUP_KEY)
    path = backup.create_backup("manual")
    env.engine.dispose()
    before = _tree(env.data)
    _reload(monkeypatch, CREDENTIAL_ENCRYPTION_KEY="a-rotated-key")
    with pytest.raises(BackupError, match="--force"):
        backup.restore_backup(path)
    assert _tree(env.data) == before

    result = backup.restore_backup(path, force=True)
    assert result.key_check is False
    marker = json.loads((env.data / backup.RESTORE_MARKER).read_text())
    assert marker["result"] == "forced"


def test_restore_puts_the_backup_back_and_keeps_the_previous_data(env, monkeypatch):
    path = backup.create_backup("manual")
    salt = Path(env.settings.credential_salt_path).read_bytes()

    # Changes after the backup: a new source, and a salt that's been lost.
    with Session(env.engine) as session:
        session.add(Source(name="later", protocol=Protocol.ssh, host="h", base_path="/"))
        session.commit()
    env.engine.dispose()
    Path(env.settings.credential_salt_path).write_bytes(b"0" * 16)
    (env.data / "perchtail.db-wal").write_bytes(b"stale")
    (env.data / "perchtail.db-shm").write_bytes(b"stale")
    old_tree = _tree(env.data)

    result = backup.restore_backup(path)

    assert result.previous.name.startswith("db.before-restore-")
    assert _tree(result.previous) == old_tree
    assert not (env.data / "perchtail.db-wal").exists()
    assert not (env.data / "perchtail.db-shm").exists()
    assert Path(env.settings.credential_salt_path).read_bytes() == salt
    assert not list(env.data.parent.glob("db.restoring-*"))
    assert [p.name for p in env.backups.iterdir()] == [path.name]

    # The app starts on it: same key, the restored salt.
    _clear_caches()
    engine = _engine(get_settings().database_url)
    monkeypatch.setattr("app.db.engine", engine)
    try:
        with Session(engine) as session:
            sources = session.exec(select(Source)).all()
            assert [s.name for s in sources] == ["app01"]
            assert crypto.decrypt_credential(sources[0].credential_ref) == CREDENTIAL
            user = session.exec(select(User)).one()
            assert crypto.decrypt_secret(user.mfa_secret_encrypted) == MFA_SECRET

            assert backup.record_pending_restore(session)
            session.commit()
            assert not backup.record_pending_restore(session)
        actions = _audit_actions(engine)
        assert actions[-1][0] == "backup.restore"
        assert actions[-1][1]["file"] == path.name
        assert actions[-1][1]["result"] == "ok"
        assert actions[-1][1]["reason"] == "manual"
    finally:
        engine.dispose()


def test_restore_into_a_missing_data_dir(env):
    path = backup.create_backup("manual")
    env.engine.dispose()
    moved = env.data.with_name("elsewhere")
    env.data.rename(moved)
    backup.restore_backup(path)
    assert (env.data / "perchtail.db").is_file()
    assert (env.data / "credential_salt").is_file()


# --- retention -------------------------------------------------------------


def _touch(directory: Path, reason: str, when: datetime) -> str:
    name = f"perchtail-{APP_VERSION}-{when:%Y%m%dT%H%M%SZ}-{reason}.ptbackup"
    (directory / name).write_bytes(b"x")
    return name


def test_retention_deletes_only_the_oldest_of_the_same_kind(env, monkeypatch):
    settings = _reload(monkeypatch, BACKUP_KEEP="2")
    env.backups.mkdir()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    regular = [
        _touch(env.backups, "manual" if i % 2 else "scheduled", start + timedelta(hours=i))
        for i in range(4)
    ]
    upgrades = [_touch(env.backups, "pre-upgrade", start + timedelta(days=i)) for i in range(5)]
    (env.backups / "notes.txt").write_text("not a backup")

    deleted = backup.apply_retention(settings)

    assert sorted(deleted) == sorted(regular[:2] + upgrades[:2])
    left = {p.name for p in env.backups.iterdir()}
    assert left == set(regular[2:] + upgrades[2:] + ["notes.txt"])


def test_create_applies_retention(env, monkeypatch):
    _reload(monkeypatch, BACKUP_KEEP="1")
    env.backups.mkdir()
    old = _touch(env.backups, "manual", datetime(2020, 1, 1, tzinfo=UTC))
    upgrade = _touch(env.backups, "pre-upgrade", datetime(2020, 1, 1, tzinfo=UTC))
    path = backup.create_backup("scheduled")
    assert {p.name for p in env.backups.iterdir()} == {path.name, upgrade}
    assert old not in {p.name for p in env.backups.iterdir()}


# --- CLI -------------------------------------------------------------------


def test_cli_refuses_root(monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    assert backup.main(["list"]) == 2
    assert "run it as the perchtail user (docker compose exec -u perchtail ...)" in (
        capsys.readouterr().err
    )


def test_cli_create_verify_list_and_errors(env, monkeypatch, capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr("app.logging_config.configure_logging", lambda *a, **k: None)
    assert backup.main(["create", "--reason", "pre-upgrade"]) == 0
    path = next(env.backups.iterdir())
    assert path.name.endswith("-pre-upgrade.ptbackup")
    assert backup.main(["verify", str(path)]) == 0
    assert backup.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "OK:" in out and path.name in out

    assert backup.main(["verify", str(env.backups / "missing.ptbackup")]) == 1
    assert "ERROR:" in capsys.readouterr().err


# --- scheduled job ---------------------------------------------------------


def _set_interval(engine, hours: int) -> None:
    with Session(engine) as session:
        system_settings.set_int(session, "backup_interval_hours", hours)
        session.commit()


def test_scheduled_backup_is_off_with_zero(env):
    assert backup.run_scheduled_backup() is None
    assert not env.backups.exists() or not list(env.backups.iterdir())


def test_scheduled_backup_runs_once_per_interval(env):
    _set_interval(env.engine, 2)
    first = backup.run_scheduled_backup()
    assert first is not None and first.name.endswith("-scheduled.ptbackup")
    assert backup.run_scheduled_backup() is None

    def age(path: Path, delta: timedelta) -> Path:
        older = path.with_name(backup.backup_name("scheduled", datetime.now(UTC) - delta))
        path.rename(older)
        return older

    first = age(first, timedelta(hours=1))
    assert backup.run_scheduled_backup() is None
    first = age(first, timedelta(hours=2, seconds=1))
    second = backup.run_scheduled_backup()
    assert second is not None and second != first
    assert {p.name for p in env.backups.iterdir()} == {first.name, second.name}

    _set_interval(env.engine, 0)
    later = datetime.now(UTC) + timedelta(days=30)
    assert backup.run_scheduled_backup(now=lambda: later) is None


def test_scheduled_backup_waits_after_a_failed_attempt(env, monkeypatch):
    _set_interval(env.engine, 3)
    real_create = backup._create
    attempts: list[str] = []

    def failing(reason, settings):
        attempts.append(reason)
        raise OSError("disk full")

    monkeypatch.setattr(backup, "_create", failing)
    start = datetime.now(UTC)

    def tick(after: timedelta) -> Path | None:
        return backup.run_scheduled_backup(now=lambda: start + after)

    with pytest.raises(OSError):
        tick(timedelta())
    # The next tick doesn't try again.
    assert tick(backup.SCHEDULE_TICK) is None
    assert len(attempts) == 1
    assert tick(backup.RETRY_AFTER_FAILURE - timedelta(seconds=1)) is None
    assert len(attempts) == 1

    # An hour (less than the interval) after the failed attempt, it tries again.
    with pytest.raises(OSError):
        tick(backup.RETRY_AFTER_FAILURE)
    assert len(attempts) == 2
    assert tick(backup.RETRY_AFTER_FAILURE + backup.SCHEDULE_TICK) is None
    assert len(attempts) == 2

    # A success clears the wait, and the interval rule applies again.
    monkeypatch.setattr(backup, "_create", real_create)
    made = tick(2 * backup.RETRY_AFTER_FAILURE)
    assert made is not None and made.name.endswith("-scheduled.ptbackup")
    assert backup._last_failed_attempt is None
    assert tick(2 * backup.RETRY_AFTER_FAILURE + backup.SCHEDULE_TICK) is None
    assert [p.name for p in env.backups.iterdir() if p.suffix == ".ptbackup"] == [made.name]

    # Every attempt is still audited.
    creates = [m["result"] for a, m in _audit_actions(env.engine) if a == "backup.create"]
    assert creates == ["failed", "failed", "ok"]


def test_a_restart_tries_a_failed_scheduled_backup_straight_away(env, monkeypatch):
    _set_interval(env.engine, 3)
    monkeypatch.setattr(backup, "_last_failed_attempt", datetime.now(UTC))
    assert backup.run_scheduled_backup() is None
    # A new process starts with no failed attempt in memory.
    monkeypatch.setattr(backup, "_last_failed_attempt", None)
    assert backup.run_scheduled_backup() is not None


def test_scheduled_backup_is_registered_as_a_job():
    import inspect

    from app import main

    source = inspect.getsource(main._started)
    assert '"backup"' in source and "run_scheduled_backup" in source


# --- audit -----------------------------------------------------------------


def test_create_verify_and_refused_restore_are_audited(env, monkeypatch):
    path = backup.create_backup("manual")
    backup.verify_backup(path)
    _altered(path, HEADER_BYTES + 5)
    with pytest.raises(BackupError):
        backup.verify_backup(path)
    lock = instance_lock.acquire(env.settings)
    try:
        with pytest.raises(BackupError):
            backup.restore_backup(path)
    finally:
        lock.release()

    actions = _audit_actions(env.engine)
    # A refused restore changes nothing, the audit log included; a restore
    # that worked is audited by the first start (test above).
    assert [(a, m["result"]) for a, m in actions] == [
        ("backup.create", "ok"),
        ("backup.verify", "ok"),
        ("backup.verify", "failed"),
    ]
    for _action, metadata in actions:
        assert metadata["file"] == path.name
        assert metadata["reason"] == "manual"
        assert KEY not in json.dumps(metadata)


# --- Settings -> System list -----------------------------------------------


def _user(session, capabilities) -> User:
    role = Role(name=f"role-{uuid.uuid4().hex}", global_capabilities=capabilities)
    session.add(role)
    session.commit()
    user = User(username=f"u-{uuid.uuid4().hex}", role_id=role.id)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _client(session, user) -> TestClient:
    app = FastAPI()
    app.include_router(system_settings_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def test_backup_list_for_whoever_manages_system_settings(env):
    path = backup.create_backup("manual")
    with Session(env.engine) as session:
        admin = _user(session, [GlobalCapability.manage_system_settings])
        response = _client(session, admin).get("/system-settings/backups")
        assert response.status_code == 200
        [entry] = response.json()
        assert entry["name"] == path.name
        assert entry["reason"] == "manual"
        assert entry["size_bytes"] == path.stat().st_size
        assert entry["created_at"].startswith(str(datetime.now(UTC).year))

        plain = _user(session, [])
        assert _client(session, plain).get("/system-settings/backups").status_code == 403
