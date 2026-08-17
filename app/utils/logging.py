"""Application and security logging.

Two channels:

``app.logger``            general application events
``logging.getLogger("quickjunction.security")``
                          authentication, authorisation and abuse events,
                          written to a separate file so it can be shipped to
                          a SIEM or alerted on independently.

Nothing logged here may contain a secret, a password, a session token or a
full database URI. Log identifiers, never credentials.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import Flask

SECURITY_LOGGER_NAME = "quickjunction.security"

_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5


def get_security_logger() -> logging.Logger:
    """Logger for security-relevant events."""
    return logging.getLogger(SECURITY_LOGGER_NAME)


def _file_handler(path: Path, level: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler.setLevel(level)
    return handler


def configure_logging(app: Flask) -> None:
    """Attach handlers to the app logger and the security logger."""
    level = logging.getLevelName(str(app.config.get("LOG_LEVEL", "INFO")).upper())
    if not isinstance(level, int):
        level = logging.INFO

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(_FORMAT))
    stream.setLevel(level)

    security = get_security_logger()
    security.propagate = False

    for logger in (app.logger, security):
        logger.handlers.clear()
        logger.setLevel(level)
        logger.addHandler(stream)

    if app.config.get("LOG_TO_FILE"):
        log_dir = Path(app.config["LOG_DIR"])
        log_dir.mkdir(parents=True, exist_ok=True)
        app.logger.addHandler(_file_handler(log_dir / "app.log", level))
        security.addHandler(_file_handler(log_dir / "security.log", logging.INFO))

    app.logger.info("Logging configured for %s environment", app.config["ENV_NAME"])
