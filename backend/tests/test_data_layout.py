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


def test_data_dir_is_readable_by_the_app_user_only(tmp_path):
    """#193: the salt, database and known_hosts were 0644 in a 0755
    directory, readable by any local user on a host install."""
    import os
    import stat

    from app.data_layout import secure_data_dir

    data_dir = tmp_path / "db"
    data_dir.mkdir(mode=0o755)
    (data_dir / "winrm_ca").mkdir(mode=0o755)
    for name in ("perchtail.db", "perchtail.db-wal", "credential_salt", "ssh_known_hosts"):
        (data_dir / name).write_bytes(b"x")
        os.chmod(data_dir / name, 0o644)
    (data_dir / "unrelated.txt").write_bytes(b"x")
    os.chmod(data_dir / "unrelated.txt", 0o644)

    assert secure_data_dir(_settings(data_dir)) == []

    def mode(name):
        return stat.S_IMODE((data_dir / name).stat().st_mode)

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((data_dir / "winrm_ca").stat().st_mode) == 0o700
    for name in ("perchtail.db", "perchtail.db-wal", "credential_salt", "ssh_known_hosts"):
        assert mode(name) == 0o600, name
    assert mode("unrelated.txt") == 0o644


def test_report_logs_moves_as_structured_events(tmp_path, monkeypatch):
    """F-A: in Docker the entrypoint moved the files first and only printed
    plain lines, so `startup.data_layout.moved` was never logged."""
    import app.data_layout as data_layout

    events = []

    class _Logger:
        def info(self, event, **kw):
            events.append((event, kw))

        def warning(self, event, **kw):
            events.append((event, kw))

    monkeypatch.setattr("app.logging_config.get_logger", lambda *a, **kw: _Logger())
    _legacy(tmp_path)
    data_layout.report(_settings(tmp_path / "db"))

    moved = [kw["dest"] for event, kw in events if event == "startup.data_layout.moved"]
    assert any(dest.endswith("perchtail.db") for dest in moved)
