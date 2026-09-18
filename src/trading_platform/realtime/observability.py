"""Observability of the realtime layer: structured logs, redaction and counters.

Three concerns live here, and only three (delivery brief D10):

structured JSON logs
    :class:`JsonLogFormatter` renders **one JSON object per line** -- ``ts``,
    ``level``, ``event``, ``profile_id``, every key of the ``context`` mapping the
    caller passed through :func:`log_event`, then ``message`` and ``logger``.  A
    log line is therefore parseable by ``jq`` and by any log shipper, without a
    third-party dependency.

secret redaction
    :class:`RedactionFilter` rewrites the record -- ``msg``, ``args``, the
    ``context`` mapping and every other string-valued extra -- replacing each
    non-empty secret of at least four characters with ``***``.  It *always* returns
    ``True``: a filter that dropped records would silently hide the very events an
    operator needs.  The names scanned in the environment are deliberately the same
    ones ``trading_platform.realtime.credentials`` reads; the eight lines are
    duplicated on purpose so that this module (layer 6, observability) creates no
    import edge towards the credential resolver -- both sides read the *same*
    documented name list.

in-process counters
    :class:`Counters` is a thread-safe bag over every field of
    :class:`~trading_platform.realtime.models.EngineCounters`, exposed through the
    health / snapshot read model.

Time never comes from :func:`datetime.datetime.now` here: the JSON timestamp is
derived from ``LogRecord.created`` (the value the :mod:`logging` machinery stamps
on the record) and :func:`log_path` reads the wall clock through
:class:`~trading_platform.realtime.clock.SystemClock`, which is the single owner of
"now" in this layer.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from trading_platform.realtime.clock import SystemClock
from trading_platform.realtime.models import EngineCounters

__all__ = [
    "LOGGER_NAME",
    "Counters",
    "JsonLogFormatter",
    "RedactionFilter",
    "configure_logging",
    "log_event",
    "log_path",
]

#: Name of the logger every realtime module logs through.
LOGGER_NAME = "trading_platform.realtime"

#: Replacement written in place of a secret.
REDACTED = "***"

#: Shortest value considered a secret; shorter values are too noisy to redact.
MIN_SECRET_LENGTH = 4

#: Attribute marking the handler this module installed (makes it idempotent).
_HANDLER_FLAG = "_trading_platform_realtime_handler"

#: Global credential variables (the same names ``realtime.credentials`` resolves).
_GLOBAL_SECRET_NAMES: tuple[str, ...] = (
    "TB_LIVE_API_KEY",
    "TB_LIVE_API_SECRET",
    "TB_LIVE_API_PASSWORD",
)

#: Suffixes of the per-profile credential variables ``TB_PROFILE_<ID>_<SUFFIX>``.
_PROFILE_SECRET_SUFFIXES: tuple[str, ...] = ("_API_KEY", "_API_SECRET", "_API_PASSWORD")

#: Prefix of the per-profile credential variables.
_PROFILE_SECRET_PREFIX = "TB_PROFILE_"

#: Every attribute :class:`logging.LogRecord` defines itself.
_STANDARD_RECORD_KEYS: frozenset[str] = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__
) | {"message", "asctime"}


# ---------------------------------------------------------------------------
# value helpers -- no NaN/Inf and no non-JSON object ever reaches the wire
# ---------------------------------------------------------------------------


def _finite(value: float) -> float | None:
    """Return ``value`` as a finite ``float``, or ``None``.

    ``NaN`` and the two infinities are mapped to ``None``: a JSON payload (and a
    JSON log line) must never carry a value the encoder refuses.
    """
    number = float(value)
    return number if math.isfinite(number) else None


def _json_value(value: Any) -> Any:
    """Return ``value`` as a JSON-native object (nested containers included)."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return _finite(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    return str(value)


def _environment_secrets(environ: Mapping[str, str]) -> tuple[str, ...]:
    """Collect the credential values present in ``environ``.

    The scan mirrors ``trading_platform.realtime.credentials``: the three global
    ``TB_LIVE_*`` variables plus every per-profile ``TB_PROFILE_<ID>_*`` one.  It is
    duplicated instead of imported so that the observability module stays free of
    an edge towards the credential resolver.
    """
    found: list[str] = []
    for name, value in environ.items():
        if not value:
            continue
        is_profile_secret = name.startswith(_PROFILE_SECRET_PREFIX) and name.endswith(
            _PROFILE_SECRET_SUFFIXES
        )
        if name in _GLOBAL_SECRET_NAMES or is_profile_secret:
            found.append(str(value))
    return tuple(found)


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


class RedactionFilter(logging.Filter):
    """Replace every known secret by ``***`` in a log record.

    Parameters
    ----------
    secrets:
        Values to redact.  An empty, ``None`` or shorter-than-four-character
        value is ignored (redacting ``"1"`` would mangle every message).
    """

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets: tuple[str, ...] = tuple(
            str(secret) for secret in secrets if secret and len(str(secret)) >= MIN_SECRET_LENGTH
        )

    @property
    def count(self) -> int:
        """Return how many values this filter redacts (never the values themselves)."""
        return len(self._secrets)

    def _redact(self, value: str) -> str:
        """Return ``value`` with every known secret replaced by ``***``."""
        redacted = value
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, REDACTED)
        return redacted

    def _redact_any(self, value: Any) -> Any:
        """Recursively redact a value: strings, mappings and sequences."""
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, Mapping):
            return {key: self._redact_any(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return tuple(self._redact_any(item) for item in value)
        if isinstance(value, list):
            return [self._redact_any(item) for item in value]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record in place and always return ``True``.

        The filter never suppresses a record: hiding an event is the opposite of
        what observability is for.
        """
        if not self._secrets:
            return True
        record.msg = self._redact_any(record.msg)
        if record.args:
            record.args = self._redact_any(record.args)
        context = record.__dict__.get("context")
        if isinstance(context, Mapping):
            record.__dict__["context"] = self._redact_any(context)
        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_RECORD_KEYS or key == "context":
                continue
            if isinstance(value, (str, Mapping, list, tuple)):
                record.__dict__[key] = self._redact_any(value)
        return True


# ---------------------------------------------------------------------------
# JSON formatting
# ---------------------------------------------------------------------------


class JsonLogFormatter(logging.Formatter):
    """Render one log record as a single line of JSON.

    Keys, in order: ``ts`` (UTC ISO-8601 derived from ``record.created``),
    ``level``, ``event`` (the ``event`` extra, falling back to the formatted
    message), ``profile_id``, every key of the ``context`` extra, then ``message``
    and ``logger``.  An attached exception is rendered under ``exception``.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as one JSON object on one line."""
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": str(record.levelname),
            "event": self._event(record),
            "profile_id": str(record.__dict__.get("profile_id") or ""),
        }
        context = record.__dict__.get("context")
        if isinstance(context, Mapping):
            for key, value in context.items():
                payload[str(key)] = _json_value(value)
        payload["message"] = self._message(record)
        payload["logger"] = str(record.name)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _event(record: logging.LogRecord) -> str:
        """Return the event name: the ``event`` extra, else the log message."""
        event = record.__dict__.get("event")
        return str(event) if event else JsonLogFormatter._message(record)

    @staticmethod
    def _message(record: logging.LogRecord) -> str:
        """Return the formatted message, never raising on a broken record."""
        try:
            return str(record.getMessage())
        except (TypeError, ValueError):  # pragma: no cover - defensive, a bad args tuple
            return str(record.msg)


# ---------------------------------------------------------------------------
# configuration, emission and log location
# ---------------------------------------------------------------------------


def configure_logging(
    *,
    level: str = "INFO",
    stream: TextIO | None = None,
    secrets: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    log_file: Path | None = None,
) -> logging.Logger:
    """Install the JSON handler on the realtime logger and return it.

    Idempotent: the handlers this function installed previously are removed before
    the new ones are added, so calling it twice never duplicates an output line and
    a later call can change the level, the stream, the secret list or the file.

    Parameters
    ----------
    level:
        Level name applied to the logger and to both handlers.
    stream:
        Destination of the console handler; defaults to ``sys.stderr``.
    secrets:
        Values to redact.  ``None`` collects them from ``environ`` (or the process
        environment) using the documented credential variable names.
    environ:
        Environment mapping used by the default secret scan.
    log_file:
        Optional durable sink (``realtime-YYYYMMDD.log``, see :func:`log_path`).
        Its parent directory is created.  The console handler alone is lost as soon
        as the process is supervised by a container runtime that rotates its
        output, which is exactly when an operator needs to read why a profile died.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            logger.removeHandler(handler)
            handler.close()
    resolved: Sequence[str]
    if secrets is None:
        resolved = _environment_secrets(os.environ if environ is None else environ)
    else:
        resolved = secrets
    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setLevel(level)
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(RedactionFilter(resolved))
    setattr(handler, _HANDLER_FLAG, True)
    logger.addHandler(handler)
    if log_file is not None:
        target = Path(log_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(target, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(JsonLogFormatter())
        file_handler.addFilter(RedactionFilter(resolved))
        setattr(file_handler, _HANDLER_FLAG, True)
        logger.addHandler(file_handler)
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    profile_id: str = "",
    **context: Any,
) -> None:
    """Emit one structured event on ``logger``.

    Parameters
    ----------
    logger:
        Target logger (usually ``logging.getLogger(LOGGER_NAME)``).
    event:
        Stable machine-readable event name, e.g. ``"candle_processed"``.
    level:
        Standard :mod:`logging` level of the record.
    profile_id:
        Profile the event belongs to (``""`` for a platform-wide event).
    **context:
        Extra fields merged into the JSON object.
    """
    logger.log(
        level,
        str(event),
        extra={
            "event": str(event),
            "profile_id": str(profile_id),
            "context": dict(context),
        },
    )


def log_path(logs_dir: Path, *, moment: datetime | None = None) -> Path:
    """Return the daily log file of a realtime run: ``realtime-YYYYMMDD.log``.

    Parameters
    ----------
    logs_dir:
        Directory holding the runtime logs.
    moment:
        Instant deciding the date; defaults to the current UTC wall clock, read
        through :class:`~trading_platform.realtime.clock.SystemClock`.
    """
    stamp = SystemClock().now() if moment is None else moment
    return Path(logs_dir) / f"realtime-{stamp.strftime('%Y%m%d')}.log"


# ---------------------------------------------------------------------------
# in-process counters
# ---------------------------------------------------------------------------


class Counters:
    """Thread-safe counters over every field of :class:`EngineCounters`.

    The object is mutable by design -- it is the running tally of one profile --
    while :meth:`snapshot` returns the frozen, JSON-ready view the health and
    snapshot read models publish.
    """

    #: Field names of the frozen counter model; an unknown name is a bug.
    FIELDS: frozenset[str] = frozenset(field.name for field in fields(EngineCounters))

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, int] = dict.fromkeys(self.FIELDS, 0)

    def __repr__(self) -> str:
        """Return a short representation of the current tallies."""
        return f"Counters({self.snapshot().to_dict()})"

    def increment(self, name: str, amount: int = 1) -> int:
        """Add ``amount`` to the counter ``name`` and return the new value.

        Raises
        ------
        KeyError
            If ``name`` is not a field of :class:`EngineCounters` (a typo must be
            loud, never a silently created counter).
        """
        if name not in self.FIELDS:
            known = ", ".join(sorted(self.FIELDS))
            raise KeyError(f"unknown counter: {name!r} (known: {known})")
        with self._lock:
            self._values[name] += int(amount)
            return self._values[name]

    def snapshot(self) -> EngineCounters:
        """Return the frozen counter model holding the current tallies."""
        with self._lock:
            return EngineCounters.from_dict(dict(self._values))

    def reset(self) -> None:
        """Set every counter back to zero."""
        with self._lock:
            self._values = dict.fromkeys(self.FIELDS, 0)
