import gzip
import logging
import logging.handlers
import os
import shutil
import sys
from pathlib import Path

import structlog

from app.config import get_settings


def _gzip_rotator(source: str, dest: str) -> None:
    with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(source)


def _gzip_namer(name: str) -> str:
    return name + ".gz"


def configure_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    Path(settings.log_dir).mkdir(parents=True, exist_ok=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=os.path.join(settings.log_dir, "perchtail.log"),
        when="midnight",
        backupCount=settings.log_retention_days,
        encoding="utf-8",
    )
    file_handler.rotator = _gzip_rotator
    file_handler.namer = _gzip_namer
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [stream_handler, file_handler]
    root_logger.setLevel(level)


def get_logger(*args, **kwargs):
    return structlog.get_logger(*args, **kwargs)
