"""Process-wide logging configuration.

``configure_logging`` is idempotent: calling it again replaces the handlers the
platform installed instead of stacking a second copy of every line. It also
never fails because of the log directory -- an unwritable filesystem only costs
the rotating file handler, the stdout handler always stays.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import ENV_LOG_LEVEL

__all__ = ["LOG_FORMAT", "configure_logging", "get_logger"]

#: Single line format of every platform log record.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FILENAME = "realtime.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUP_COUNT = 3

#: Marks the handlers this module installed, so a later call can replace them.
_HANDLER_MARKER = "_trading_platform_handler"

#: Logger the platform owns; its records never propagate twice.
PACKAGE_LOGGER_NAME = "trading_platform"


def get_logger(name: str) -> logging.Logger:
    """Return the named logger (module convention: ``get_logger(__name__)``)."""
    return logging.getLogger(name)


def _resolve_level(level: str | None) -> int:
    """Resolve the effective level: argument, then ``TB_LOG_LEVEL``, then INFO."""
    candidate = level if level is not None else os.environ.get(ENV_LOG_LEVEL, DEFAULT_LOG_LEVEL)
    try:
        resolved = logging.getLevelNamesMapping().get(str(candidate).strip().upper())
    except ValueError:  # pragma: no cover - defensive, getLevelNamesMapping never raises
        resolved = None
    return resolved if isinstance(resolved, int) else logging.INFO


def _file_handler(log_dir: Path | str, filename: str) -> RotatingFileHandler | None:
    """Open the rotating file handler, or return ``None`` when impossible."""
    try:
        directory = Path(log_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        return RotatingFileHandler(
            directory / filename,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
    except (OSError, ValueError):
        return None


def configure_logging(
    level: str | None = None,
    log_dir: Path | None = None,
    *,
    filename: str = DEFAULT_LOG_FILENAME,
) -> logging.Logger:
    """Configure platform logging and return the configured logger.

    A stdout handler is always installed; when ``log_dir`` is given a rotating
    file handler is added next to it. The level comes from ``level``, then from
    ``TB_LOG_LEVEL``, then from ``INFO``. Handlers installed by a previous call
    are removed first, so repeated calls never duplicate a log line, and a
    directory that cannot be written only disables the file handler.
    """
    root = logging.getLogger()
    resolved = _resolve_level(level)
    root.setLevel(resolved)
    package_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    package_logger.setLevel(resolved)

    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    handlers: list[logging.Handler] = [stream_handler]
    if log_dir is not None:
        rotating = _file_handler(log_dir, filename)
        if rotating is not None:
            rotating.setFormatter(formatter)
            handlers.append(rotating)
    for handler in handlers:
        setattr(handler, _HANDLER_MARKER, True)
        root.addHandler(handler)
    return root
