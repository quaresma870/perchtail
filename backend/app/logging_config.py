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

    for name in NOISY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(third_party_level(name, level, settings))
    file_handler.enforce_retention()
    if level < logging.INFO and not settings.log_quiet_third_party:
        get_logger(__name__).warning(
            "logging.third_party_debug",
            message=THIRD_PARTY_DEBUG_WARNING,
        )


THIRD_PARTY_DEBUG_WARNING = (
    "the protocol libraries log at DEBUG: their messages can include what is read from "
    "sources, before redaction, and slow down reads; set LOG_QUIET_THIRD_PARTY=true unless "
    "you are debugging a connection"
)


# At INFO these log every SMB message and every SSH session they close, most
# of an INFO log's lines: kept at WARNING unless PerchTail logs at DEBUG.
QUIET_AT_INFO_LOGGERS = frozenset({"smbprotocol", "spnego", "paramiko"})


def third_party_level(name: str, level: int, settings) -> int:
    """The level of one of NOISY_THIRD_PARTY_LOGGERS for the app's `level`.
    At DEBUG, LOG_QUIET_THIRD_PARTY (true by default) keeps the app's own
    DEBUG detail while capping the protocol libraries at INFO: their DEBUG
    messages can carry what is read from sources, before redaction."""
    if level < logging.INFO:
        return logging.INFO if settings.log_quiet_third_party else logging.NOTSET
    if name in QUIET_AT_INFO_LOGGERS:
        return max(logging.WARNING, level)
    return logging.NOTSET


def get_logger(*args, **kwargs):
    return structlog.get_logger(*args, **kwargs)
