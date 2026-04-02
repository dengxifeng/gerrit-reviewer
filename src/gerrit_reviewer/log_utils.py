"""Shared logging setup for bridge daemons."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path.home() / ".gerrit-reviewer" / "logs"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

# 10 MB per file, keep 3 backups (.log.1, .log.2, .log.3)
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 3


def setup_logging(log_name: str) -> logging.Logger:
    """Setup logger with rotating file + stderr output.

    Args:
        log_name: Log file name, e.g. "gerrit-event.log".

    Returns:
        Configured logger instance.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / log_name

    logger = logging.getLogger(log_name.removesuffix(".log"))
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    return logger
