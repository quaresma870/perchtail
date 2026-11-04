"""Moving the pre-0.5.0 /data layout into DATA_DIR (#172)."""

import sqlite3

from app.config import Settings
from app.data_layout import leftover_legacy_database, legacy_moves, migrate_legacy_layout


def _settings(data_dir, **overrides):
    values = dict(
        data_dir=str(data_dir),
        database_url="",
        credential_salt_path="",
        ssh_known_hosts_path="",
    )
    values.update(overrides)
    return Settings(**values)


def _legacy(root):
    db = root / "perchtail.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (x)")
        conn.execute("INSERT INTO t VALUES (42)")
    (root / "credential_salt").write_bytes(b"salt")
    (root / "ssh_known_hosts").write_text("host ssh-ed25519 AAAA\n")
    (root / "logs").mkdir()


def test_moves_salt_first_and_database_last(tmp_path):
    _legacy(tmp_path)
    settings = _settings(tmp_path / "db")

    planned = [(a.name, b.parent.name) for a, b in legacy_moves(settings)]
    assert planned[0] == ("credential_salt", "db")
    assert planned[-1] == ("perchtail.db", "db")

    migrate_legacy_layout(settings)

    assert not (tmp_path / "perchtail.db").exists()
    assert (tmp_path / "db" / "credential_salt").read_bytes() == b"salt"
    assert (tmp_path / "db" / "ssh_known_hosts").exists()
    with sqlite3.connect(tmp_path / "db" / "perchtail.db") as conn:
        assert conn.execute("SELECT x FROM t").fetchone() == (42,)
    assert (tmp_path / "logs").is_dir()
    assert migrate_legacy_layout(settings) == []


def test_an_interrupted_move_finishes_on_the_next_start(tmp_path):
    _legacy(tmp_path)
    settings = _settings(tmp_path / "db")
    (tmp_path / "db").mkdir()
    (tmp_path / "credential_salt").rename(tmp_path / "db" / "credential_salt")

    moved = [a.name for a, _ in migrate_legacy_layout(settings)]

    assert moved == ["ssh_known_hosts", "perchtail.db"]
    assert (tmp_path / "db" / "perchtail.db").exists()


def test_explicit_paths_are_never_moved(tmp_path):
    _legacy(tmp_path)
    settings = _settings(
        tmp_path / "db",
        database_url=f"sqlite:///{tmp_path / 'perchtail.db'}",
        credential_salt_path=str(tmp_path / "credential_salt"),
    )
    moved = [a.name for a, _ in migrate_legacy_layout(settings)]
    assert moved == ["ssh_known_hosts"]
    assert (tmp_path / "perchtail.db").exists()
    assert (tmp_path / "credential_salt").exists()


def test_an_existing_target_is_never_overwritten_and_is_reported(tmp_path):
    _legacy(tmp_path)
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "perchtail.db").write_bytes(b"")
    settings = _settings(tmp_path / "db")

    moved = [a.name for a, _ in migrate_legacy_layout(settings)]

    assert "perchtail.db" not in moved
    assert (tmp_path / "db" / "perchtail.db").read_bytes() == b""
    assert leftover_legacy_database(settings) == tmp_path / "perchtail.db"


def test_wal_files_move_with_the_database(tmp_path):
    _legacy(tmp_path)
    (tmp_path / "perchtail.db-wal").write_bytes(b"wal")
    settings = _settings(tmp_path / "db")
    migrate_legacy_layout(settings)
    assert (tmp_path / "db" / "perchtail.db-wal").read_bytes() == b"wal"
