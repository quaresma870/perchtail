import logging

from app.config import get_settings
from app.logging_config import configure_logging


def test_configure_logging_writes_json_to_rotating_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    get_settings.cache_clear()

    configure_logging()

    logger = logging.getLogger()
    logger.info("test message")
    for handler in logger.handlers:
        handler.flush()

    log_file = tmp_path / "perchtail.log"
    assert log_file.exists()
    assert "test message" in log_file.read_text()

    get_settings.cache_clear()
