"""Tests of the realtime observability module: JSON logs, redaction, counters.

Everything here is offline and deterministic: no network, no optional extra, no
wall-clock dependency (the only timestamps read are the ones the :mod:`logging`
machinery stamps on a record, and the date handed to :func:`log_path`).  The last
test of the file is the mechanical *import policy* proof of the delivery brief:
importing the realtime package in a fresh interpreter must pull in neither ``ccxt``
nor ``freqtrade``, and no source file of the layer may read the clock outside
``clock.py``.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trading_backtest.realtime.models import EngineCounters
from trading_backtest.realtime.observability import (
    LOGGER_NAME,
    Counters,
    JsonLogFormatter,
    RedactionFilter,
    configure_logging,
    log_event,
    log_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
REALTIME_DIR = SRC_ROOT / "trading_backtest" / "realtime"
WEB_DIR = SRC_ROOT / "trading_backtest" / "web"

#: A value that must never survive redaction.
SENTINEL = "s3cr3t-API-KEY-0123456789"

#: A second, per-profile credential value.
PROFILE_SENTINEL = "profile-secret-value-abcdef"


class Capture(logging.Handler):
    """Minimal in-memory handler (propagate-independent, unlike ``caplog``)."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        self.records.append(record)


@pytest.fixture
def capture() -> object:
    """Attach a capturing handler to the realtime logger for one test."""
    logger = logging.getLogger(LOGGER_NAME)
    handler = Capture()
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


@pytest.fixture(autouse=True)
def _restore_logger() -> object:
    """Restore the realtime logger after every test (configure_logging mutates it)."""
    logger = logging.getLogger(LOGGER_NAME)
    saved_level = logger.level
    saved_propagate = logger.propagate
    saved_handlers = list(logger.handlers)
    try:
        yield None
    finally:
        for handler in list(logger.handlers):
            if handler not in saved_handlers:
                logger.removeHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def _records(capture: object) -> list[logging.LogRecord]:
    """Return the records collected by the fixture handler."""
    return list(capture.records)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 14. one JSON object per line, never a NaN/Inf
# ---------------------------------------------------------------------------


def test_json_formatter_emits_one_line_per_record(capture: object) -> None:
    """Every record becomes one line of valid JSON carrying the documented keys."""
    logger = logging.getLogger(LOGGER_NAME)
    assert str(logger.name) == "trading_backtest.realtime"
    log_event(
        logger,
        "candle_processed",
        profile_id="btc-paper",
        timestamp="2024-01-01T00:00:00+00:00",
        equity=1000.5,
        action="hold",
    )
    record = _records(capture)[0]
    formatter = JsonLogFormatter()
    line = formatter.format(record)
    assert "\n" not in line
    payload = json.loads(line)
    assert payload["event"] == "candle_processed"
    assert payload["profile_id"] == "btc-paper"
    assert payload["level"] == "INFO"
    assert payload["logger"] == LOGGER_NAME
    assert payload["timestamp"] == "2024-01-01T00:00:00+00:00"
    assert payload["equity"] == 1000.5
    assert payload["action"] == "hold"
    assert payload["message"] == "candle_processed"
    stamp = datetime.fromisoformat(payload["ts"])
    assert stamp.tzinfo is not None
    assert stamp.utcoffset() is not None
    assert stamp.utcoffset().total_seconds() == 0.0


def test_json_formatter_never_emits_nan_or_inf(capture: object) -> None:
    """Non-finite floats become ``null`` and exotic values are stringified."""
    logger = logging.getLogger(LOGGER_NAME)
    log_event(
        logger,
        "weird",
        profile_id="p1",
        price=float("nan"),
        ratio=float("inf"),
        count=3,
        flag=True,
        nothing=None,
        nested={"a": float("-inf"), "b": [1.0, float("nan")]},
        object_=object(),
    )
    payload = json.loads(JsonLogFormatter().format(_records(capture)[0]))
    assert payload["price"] is None
    assert payload["ratio"] is None
    assert payload["count"] == 3
    assert payload["flag"] is True
    assert payload["nothing"] is None
    assert payload["nested"] == {"a": None, "b": [1.0, None]}
    assert isinstance(payload["object_"], str)
    assert "NaN" not in json.dumps(payload)
    assert "Infinity" not in json.dumps(payload)


def test_json_formatter_accepts_a_plain_log_record() -> None:
    """A record without the structured extras still renders as valid JSON."""
    record = logging.LogRecord("other", logging.WARNING, "f.py", 3, "hello %s", ("world",), None)
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["event"] == "hello world"
    assert payload["message"] == "hello world"
    assert payload["profile_id"] == ""
    assert payload["level"] == "WARNING"


def test_json_formatter_renders_a_traceback() -> None:
    """An attached exception is rendered under ``exception``."""
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("other", logging.ERROR, "f.py", 3, "failed", (), sys.exc_info())
    payload = json.loads(JsonLogFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]


# ---------------------------------------------------------------------------
# 15. redaction -- the mechanical proof that a secret never reaches a log
# ---------------------------------------------------------------------------


def test_redaction_filter_hides_a_sentinel_everywhere(tmp_path: Path) -> None:
    """Neither the formatted line, the record nor the log file holds the secret."""
    log_file = tmp_path / "realtime.log"
    with log_file.open("w", encoding="utf-8") as stream:
        logger = configure_logging(
            level="DEBUG", stream=stream, secrets=[SENTINEL, PROFILE_SENTINEL]
        )
        log_event(
            logger,
            "order_submitted",
            profile_id="btc-live",
            api_key=SENTINEL,
            nested={"secret": PROFILE_SENTINEL, "list": [SENTINEL]},
            message_text=f"using {SENTINEL} now",
        )
        log_event(
            logger,
            "profile_starting",
            profile_id="btc-live",
            detail="no secret here",
        )
        stream.flush()
    text = log_file.read_text(encoding="utf-8")
    assert SENTINEL not in text
    assert PROFILE_SENTINEL not in text
    assert "***" in text
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 2
    payload = json.loads(lines[0])
    assert payload["event"] == "order_submitted"
    assert payload["profile_id"] == "btc-live"
    assert payload["api_key"] == "***"
    assert payload["nested"] == {"secret": "***", "list": ["***"]}
    assert payload["message_text"] == "using *** now"
    assert json.loads(lines[1])["event"] == "profile_starting"


def test_redaction_filter_rewrites_msg_and_args(capture: object) -> None:
    """``record.msg`` and ``record.args`` are redacted too, never dropped."""
    handler_filter = RedactionFilter([SENTINEL])
    record = logging.LogRecord(
        LOGGER_NAME,
        logging.INFO,
        "f.py",
        1,
        f"key={SENTINEL} value=%s",
        (SENTINEL,),
        None,
    )
    assert handler_filter.filter(record) is True
    assert SENTINEL not in record.getMessage()
    assert "***" in record.getMessage()


def test_redaction_filter_ignores_short_and_empty_secrets() -> None:
    """A one-character "secret" would mangle every message: it is not used."""
    handler_filter = RedactionFilter(["", "ab", "abc", None])  # type: ignore[list-item]
    assert handler_filter.count == 0
    logger = logging.getLogger(LOGGER_NAME)
    log_event(logger, "plain", profile_id="p", note="ab is a common substring")
    logger.info("ab")
    assert handler_filter.count == 0


def test_redaction_filter_walks_a_mapping_of_secrets(capture: object) -> None:
    """The ``context`` mapping is redacted recursively, keys included."""
    logger = logging.getLogger(LOGGER_NAME)
    log_event(logger, "event", profile_id="p", payload={"api_key": SENTINEL, "n": 1})
    record = _records(capture)[0]
    assert RedactionFilter([SENTINEL]).filter(record) is True
    assert record.context["payload"]["api_key"] == "***"
    assert record.context["payload"]["n"] == 1


def test_redaction_filter_never_suppresses_a_record(capture: object) -> None:
    """A filter that returned ``False`` would hide the events an operator needs."""
    logger = logging.getLogger(LOGGER_NAME)
    log_event(logger, "kept", profile_id="p", note=SENTINEL)
    assert RedactionFilter([SENTINEL]).filter(_records(capture)[0]) is True
    assert RedactionFilter([]).filter(_records(capture)[0]) is True


def test_configure_logging_can_read_the_secrets_from_the_environment(tmp_path: Path) -> None:
    """Without an explicit list, the credential variables of the environment are used."""
    environ = {
        "TB_LIVE_API_KEY": SENTINEL,
        "TB_PROFILE_BTC_PAPER_API_SECRET": PROFILE_SENTINEL,
        "TB_PROFILE_BTC_PAPER_API_KEY": SENTINEL,
        "UNRELATED": "keep-me",
    }
    log_file = tmp_path / "env.log"
    with log_file.open("w", encoding="utf-8") as stream:
        logger = configure_logging(level="INFO", stream=stream, environ=environ)
        log_event(logger, "boot", profile_id="btc-paper", key=SENTINEL, secret=PROFILE_SENTINEL)
        stream.flush()
    text = log_file.read_text(encoding="utf-8")
    assert SENTINEL not in text
    assert PROFILE_SENTINEL not in text
    assert json.loads(text.splitlines()[0])["event"] == "boot"


def test_no_secret_is_exposed_by_the_filter_or_the_logger(tmp_path: Path) -> None:
    """Repr, attributes and payloads of the redaction machinery stay secret-free."""
    handler_filter = RedactionFilter([SENTINEL])
    assert SENTINEL not in repr(handler_filter)
    assert handler_filter.count == 1
    logger = configure_logging(level="INFO", stream=io.StringIO(), secrets=[SENTINEL])
    assert SENTINEL not in repr(logger)
    assert all(SENTINEL not in repr(getattr(handler, "filters", [])) for handler in logger.handlers)


# ---------------------------------------------------------------------------
# 16. counters
# ---------------------------------------------------------------------------


def test_counters_are_thread_safe() -> None:
    """100 increments per thread from 4 threads land exactly once each."""
    counters = Counters()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(100):
                counters.increment("orders_submitted")
        except BaseException as exc:  # pragma: no cover - a failure surfaces below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    snapshot = counters.snapshot()
    assert isinstance(snapshot, EngineCounters)
    assert snapshot.orders_submitted == 400
    assert snapshot.candles_processed == 0


def test_counters_reject_unknown_names_and_reset() -> None:
    """A typo is loud, and ``reset`` zeroes every field of the model."""
    counters = Counters()
    with pytest.raises(KeyError, match="unknown counter"):
        counters.increment("not_a_counter")
    counters.increment("candles_processed", 5)
    counters.increment("errors")
    assert counters.snapshot().candles_processed == 5
    counters.reset()
    assert counters.snapshot() == EngineCounters()
    assert set(counters.FIELDS) == set(EngineCounters().to_dict())


def test_counters_repr_is_readable() -> None:
    """The representation shows the tallies and never raises."""
    counters = Counters()
    counters.increment("orders_filled", 2)
    assert "orders_filled" in repr(counters)


# ---------------------------------------------------------------------------
# 17. logging configuration and log location
# ---------------------------------------------------------------------------


def test_configure_logging_is_idempotent() -> None:
    """Two calls leave exactly one handler on the realtime logger."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    first = configure_logging(level="DEBUG", stream=io.StringIO(), secrets=[])
    assert len(first.handlers) == 1
    second = configure_logging(level="INFO", stream=io.StringIO(), secrets=["abcd"])
    assert second is first
    assert len(second.handlers) == 1
    assert second.level == logging.INFO
    assert second.propagate is False
    assert [type(item).__name__ for item in second.handlers[0].filters] == ["RedactionFilter"]


def test_configure_logging_uses_stderr_by_default() -> None:
    """The default stream is the process error stream."""
    logger = configure_logging(level="INFO", secrets=[])
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr


def test_log_path_is_deterministic_for_a_date(tmp_path: Path) -> None:
    """``realtime-YYYYMMDD.log`` depends only on the moment it is given."""
    moment = datetime(2024, 3, 7, 23, 59, tzinfo=UTC)
    first = log_path(tmp_path, moment=moment)
    assert first == log_path(tmp_path, moment=moment)
    assert first.name == "realtime-20240307.log"
    assert first.parent == tmp_path
    assert log_path(tmp_path).name.startswith("realtime-")
    assert len(log_path(tmp_path).stem) == len("realtime-20240307")


# ---------------------------------------------------------------------------
# 18. import policy and clock policy
# ---------------------------------------------------------------------------

_IMPORT_PROBE = """
import sys
import trading_backtest.realtime.orchestrator
import trading_backtest.realtime.runner
import trading_backtest.realtime.observability
import trading_backtest.realtime.strategies
forbidden = [name for name in ("ccxt", "freqtrade") if name in sys.modules]
print("FORBIDDEN:" + ",".join(forbidden))
"""

#: ``<module>.now()``-style calls that must go through the ``Clock`` seam instead.
_DIRECT_CLOCK_CALLS = frozenset({"now", "utcnow", "time", "monotonic"})

#: Names a direct clock read is reported under (``dt`` is the usual alias).
_CLOCK_MODULES = frozenset({"datetime", "dt", "time", "time_module"})


def _direct_clock_calls(path: Path) -> list[tuple[int, str]]:
    """Return the real ``datetime.now()``/``time.time()`` calls of one module.

    Docstring mentions are ignored because the scan walks the parsed AST, and a
    call through the injected clock (``self._clock.now()``) is ignored because its
    base is not one of :data:`_CLOCK_MODULES`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _DIRECT_CLOCK_CALLS:
            continue
        base = node.func.value
        if isinstance(base, ast.Name):
            owner = base.id
        elif isinstance(base, ast.Attribute):
            owner = base.attr
        else:
            owner = ""
        if owner in _CLOCK_MODULES:
            found.append((int(node.lineno), f"{owner}.{node.func.attr}("))
    return found


def test_importing_the_realtime_package_pulls_no_optional_extra() -> None:
    """A fresh interpreter imports the layer with the dev extra only."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(SRC_ROOT)
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=environment,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    marker = [line for line in completed.stdout.splitlines() if line.startswith("FORBIDDEN:")]
    assert marker == ["FORBIDDEN:"], completed.stdout


def test_no_realtime_module_reads_the_clock_outside_clock_py() -> None:
    """``Clock`` is the single owner of time in the realtime and web layers.

    The check is syntactic (an AST walk over the real calls), so a docstring that
    *mentions* ``datetime.now()`` is not mistaken for a violation, while a real
    ``self._clock.now()`` -- the documented seam -- is correctly ignored.
    """
    packages = [REALTIME_DIR] + ([WEB_DIR] if WEB_DIR.is_dir() else [])
    offenders: list[str] = []
    checked = 0
    for package in packages:
        for path in sorted(package.rglob("*.py")):
            if path.name == "clock.py":
                continue
            checked += 1
            for line, call in _direct_clock_calls(path):
                offenders.append(f"{path.relative_to(SRC_ROOT)}:{line}: {call}")
    assert checked >= 4
    assert offenders == []


def test_the_clock_scan_detects_a_real_violation(tmp_path: Path) -> None:
    """The scan is not vacuous: it finds a real call and ignores a docstring."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""A docstring mentioning datetime.now() and time.time()."""\n'
        "import time\n"
        "from datetime import datetime\n"
        "\n"
        "def bad() -> float:\n"
        "    stamp = datetime.now()\n"
        "    return time.time()\n"
        "\n"
        "def fine(clock: object) -> object:\n"
        "    return clock.now()  # noqa: the injected seam\n",
        encoding="utf-8",
    )
    found = _direct_clock_calls(sample)
    assert [call for _line, call in found] == ["datetime.now(", "time.time("]
    assert [line for line, _call in found] == [6, 7]
