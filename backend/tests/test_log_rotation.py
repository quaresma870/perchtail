"""Log rotation by size or day, retention by age and total size (#172)."""

import gzip
import logging
from datetime import datetime

import pytest
from app import log_rotation
from app.log_rotation import SizeAndTimeRotatingFileHandler


class Clock:
    def __init__(self, when: datetime):
        self.now = when.timestamp()

    def __call__(self) -> float:
        return self.now

    def set(self, when: datetime):
        self.now = when.timestamp()


@pytest.fixture(autouse=True)
def _reset():
    log_rotation.reset_retention_pressure()
    yield
    log_rotation.reset_retention_pressure()


def _handler(tmp_path, clock, **kwargs):
    defaults = dict(max_bytes=200, retention_days=30, max_total_bytes=0)
    defaults.update(kwargs)
    handler = SizeAndTimeRotatingFileHandler(
        str(tmp_path / "perchtail.log"), clock=clock, **defaults
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _emit(handler, message: str):
    handler.handle(logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None))


def _names(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir())


def test_rotates_on_size_with_unique_names_per_day(tmp_path):
    clock = Clock(datetime(2026, 11, 3, 10, 0))
    handler = _handler(tmp_path, clock)
    for i in range(12):
        _emit(handler, f"line {i:02d} " + "x" * 50)
    handler.close()

    archives = [n for n in _names(tmp_path) if n.endswith(".gz")]
    assert archives == [f"perchtail.log.2026-11-03.{n}.gz" for n in range(1, len(archives) + 1)]
    assert len(archives) == 2  # 4 lines (~236 bytes) per file at max_bytes=200
    text = "".join(gzip.open(tmp_path / a, "rt").read() for a in archives)
    text += (tmp_path / "perchtail.log").read_text()
    assert [line.split()[1] for line in text.splitlines()] == [f"{i:02d}" for i in range(12)]


def test_rotates_at_midnight_under_the_day_the_lines_were_written(tmp_path):
    clock = Clock(datetime(2026, 11, 3, 23, 59))
    handler = _handler(tmp_path, clock, max_bytes=0)
    _emit(handler, "late on the 3rd")
    clock.set(datetime(2026, 11, 4, 0, 1))
    _emit(handler, "early on the 4th")
    handler.close()

    assert _names(tmp_path) == ["perchtail.log", "perchtail.log.2026-11-03.1.gz"]
    assert gzip.open(tmp_path / "perchtail.log.2026-11-03.1.gz", "rt").read() == "late on the 3rd\n"
    assert (tmp_path / "perchtail.log").read_text() == "early on the 4th\n"


def test_day_retention_also_expires_archives_from_earlier_versions(tmp_path):
    for name in (
        "perchtail.log.2026-09-01.gz",
        "perchtail.log.2026-10-30.gz",
        "perchtail.log.2026-09-02.4.gz",
    ):
        with gzip.open(tmp_path / name, "wt") as fh:
            fh.write("old\n")
    (tmp_path / "unrelated.gz").write_bytes(b"keep me")
    clock = Clock(datetime(2026, 11, 3, 12, 0))
    handler = _handler(tmp_path, clock, retention_days=30)
    handler.enforce_retention()
    handler.close()

    assert _names(tmp_path) == ["perchtail.log", "perchtail.log.2026-10-30.gz", "unrelated.gz"]
    assert log_rotation.retention_pressure().deleted_files == 0


def test_total_size_limit_deletes_oldest_first_and_reports_pressure(tmp_path, caplog):
    for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
        (tmp_path / f"perchtail.log.{day}.1.gz").write_bytes(b"x" * 1000)
    clock = Clock(datetime(2026, 11, 3, 12, 0))
    handler = _handler(tmp_path, clock, retention_days=30, max_total_bytes=2100)
    with caplog.at_level(logging.WARNING, logger="app.log_rotation"):
        handler.enforce_retention()
    handler.close()

    assert _names(tmp_path) == [
        "perchtail.log",
        "perchtail.log.2026-11-02.1.gz",
        "perchtail.log.2026-11-03.1.gz",
    ]
    pressure = log_rotation.retention_pressure()
    assert pressure.deleted_files == 1
    assert pressure.newest_deleted_day == "2026-11-01"
    # F-J: the event name alone, with the numbers as fields.
    record = next(r for r in caplog.records if r.name == "app.log_rotation")
    assert record.getMessage() == "logs.retention.size_limit"
    assert record.deleted_files == 1
    assert record.newest_deleted_day == "2026-11-01"
    assert "LOG_MAX_TOTAL_MB" in record.hint


def test_a_leftover_file_is_archived_under_its_own_day(tmp_path):
    import os

    leftover = tmp_path / "perchtail.log"
    leftover.write_text("from yesterday\n")
    yesterday = datetime(2026, 11, 2, 18, 0).timestamp()
    os.utime(leftover, (yesterday, yesterday))
    clock = Clock(datetime(2026, 11, 3, 9, 0))
    handler = _handler(tmp_path, clock, max_bytes=0)
    _emit(handler, "today")
    handler.close()

    assert "perchtail.log.2026-11-02.1.gz" in _names(tmp_path)
    assert (tmp_path / "perchtail.log").read_text() == "today\n"


def test_quiet_third_party_caps_protocol_loggers(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.logging_config import configure_logging

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_QUIET_THIRD_PARTY", "true")
    get_settings.cache_clear()
    try:
        configure_logging()
        assert logging.getLogger("smbprotocol").level == logging.INFO
        assert logging.getLogger("paramiko").level == logging.INFO
        assert logging.getLogger().level == logging.DEBUG
        monkeypatch.setenv("LOG_QUIET_THIRD_PARTY", "false")
        get_settings.cache_clear()
        configure_logging()
        assert logging.getLogger("smbprotocol").level == logging.NOTSET
    finally:
        monkeypatch.delenv("LOG_LEVEL")
        monkeypatch.delenv("LOG_QUIET_THIRD_PARTY")
        get_settings.cache_clear()
        configure_logging()


def _third_party_debug_warnings(log_dir) -> list[dict]:
    import json

    path = log_dir / "perchtail.log"
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in events if e.get("event") == "logging.third_party_debug"]


def test_quiet_third_party_is_on_by_default(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("LOG_QUIET_THIRD_PARTY", raising=False)
    assert Settings(_env_file=None).log_quiet_third_party is True
    monkeypatch.setenv("LOG_QUIET_THIRD_PARTY", "false")
    assert Settings(_env_file=None).log_quiet_third_party is False
    monkeypatch.setenv("LOG_QUIET_THIRD_PARTY", "true")
    assert Settings(_env_file=None).log_quiet_third_party is True


def test_protocol_loggers_stay_at_info_at_debug_by_default(tmp_path, monkeypatch):
    # At DEBUG, smbprotocol logs every SMB read with its buffer and pypsrp
    # every envelope: what is read from sources, before redaction.
    from app.config import Settings, get_settings
    from app.log_rotation import NOISY_THIRD_PARTY_LOGGERS
    from app.logging_config import configure_logging

    for name in ("pypsrp", "httpx", "httpcore", "winrm", "smbprotocol", "paramiko"):
        assert name in NOISY_THIRD_PARTY_LOGGERS
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("LOG_QUIET_THIRD_PARTY", raising=False)
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.logging_config.get_settings", lambda: settings)
    try:
        configure_logging()
        assert logging.getLogger().level == logging.DEBUG
        for name in NOISY_THIRD_PARTY_LOGGERS:
            assert logging.getLogger(name).level == logging.INFO, name
            assert not logging.getLogger(f"{name}.child").isEnabledFor(logging.DEBUG), name
        assert _third_party_debug_warnings(tmp_path) == []
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        configure_logging()


def test_protocol_loggers_at_debug_only_when_asked_and_warned_once(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.log_rotation import NOISY_THIRD_PARTY_LOGGERS
    from app.logging_config import configure_logging

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("LOG_QUIET_THIRD_PARTY", "false")
    get_settings.cache_clear()
    try:
        configure_logging()
        for name in NOISY_THIRD_PARTY_LOGGERS:
            assert logging.getLogger(name).level == logging.NOTSET, name
            assert logging.getLogger(name).isEnabledFor(logging.DEBUG), name
        warnings = _third_party_debug_warnings(tmp_path)
        assert len(warnings) == 1
        assert warnings[0]["level"] == "warning"
        assert "before redaction" in warnings[0]["message"]
        assert "LOG_QUIET_THIRD_PARTY=true" in warnings[0]["message"]

        # No warning at INFO, even with the switch off.
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        get_settings.cache_clear()
        configure_logging()
        assert len(_third_party_debug_warnings(tmp_path)) == 1
    finally:
        monkeypatch.delenv("LOG_LEVEL")
        monkeypatch.delenv("LOG_QUIET_THIRD_PARTY")
        get_settings.cache_clear()
        configure_logging()


def test_smb_and_ssh_libraries_log_warnings_only_at_info(tmp_path, monkeypatch):
    # At INFO, smbprotocol logs every SMB2 message and paramiko every closed
    # session: they stay at WARNING unless PerchTail logs at DEBUG.
    from app.config import get_settings
    from app.logging_config import configure_logging

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("LOG_QUIET_THIRD_PARTY", "false")
    get_settings.cache_clear()
    try:
        configure_logging()
        for name in ("smbprotocol", "spnego", "paramiko"):
            assert logging.getLogger(name).level == logging.WARNING
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)
        assert logging.getLogger("urllib3").level == logging.NOTSET
        assert logging.getLogger("app").isEnabledFor(logging.INFO)

        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        get_settings.cache_clear()
        configure_logging()
        assert not logging.getLogger("paramiko").isEnabledFor(logging.WARNING)

        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        get_settings.cache_clear()
        configure_logging()
        assert logging.getLogger("smbprotocol").isEnabledFor(logging.DEBUG)
    finally:
        monkeypatch.delenv("LOG_LEVEL")
        monkeypatch.delenv("LOG_QUIET_THIRD_PARTY")
        get_settings.cache_clear()
        configure_logging()
