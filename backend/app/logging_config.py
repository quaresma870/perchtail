import logging
import os
import sys
from pathlib import Path

import structlog

from app.config import get_settings
from app.log_rotation import NOISY_THIRD_PARTY_LOGGERS, SizeAndTimeRotatingFileHandler


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
        # ExtraAdder: a stdlib logger's `extra={...}` become JSON fields, so
        # events logged without structlog (app.log_rotation, F-J) keep the
        # same `event` + fields shape as everything else.
        foreign_pre_chain=shared_processors + [structlog.stdlib.ExtraAdder()],
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    # Size or midnight, whichever comes first; retention by age and by total
    # size (#172, see app/log_rotation.py).
    file_handler = SizeAndTimeRotatingFileHandler(
        os.path.join(settings.log_dir, "perchtail.log"),
        max_bytes=int(settings.log_max_file_mb * 1024 * 1024),
        retention_days=settings.log_retention_days,
        max_total_bytes=int(settings.log_max_total_mb * 1024 * 1024),
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [stream_handler, file_handler]
    root_logger.setLevel(level)

    # Keeps the app's own DEBUG detail while capping protocol libraries'
    # packet dumps, which are most of DEBUG's volume.
    quiet = settings.log_quiet_third_party and level < logging.INFO
    for name in NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO if quiet else logging.NOTSET)
    file_handler.enforce_retention()


def get_logger(*args, **kwargs):
    return structlog.get_logger(*args, **kwargs)
