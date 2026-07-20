from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path


LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUP_COUNT = 3


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


def configure_logger(name: str, log_path: Path) -> logging.Logger:
    """Create a transition logger backed by journald or bounded files."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing_handler in logger.handlers:
        logger.removeHandler(existing_handler)
        existing_handler.close()

    if os.environ.get("AGENT_KEEPALIVE_LOG_DEST") == "journal":
        handler: logging.Handler = logging.StreamHandler()
    else:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = PrivateRotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger.addHandler(handler)
    return logger
