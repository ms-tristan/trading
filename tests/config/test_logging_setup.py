"""Tests for the logging configuration."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from trading_platform import config as platform_config
from trading_platform import logging_setup


def _installed_handlers() -> list[logging.Handler]:
    """The handlers ``configure_logging`` installed on the root logger."""
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_trading_platform_handler", False)
    ]


@pytest.fixture
def restore_root_logging():
    """Restore the root logger handlers and level after a test."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    package_logger = logging.getLogger(logging_setup.PACKAGE_LOGGER_NAME)
    original_package_level = package_logger.level
    yield
    for handler in list(root.handlers):
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in original_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(original_level)
    package_logger.setLevel(original_package_level)


def test_log_format_is_a_single_line_pattern() -> None:
    assert "%(asctime)s" in logging_setup.LOG_FORMAT
    assert "%(levelname)s" in logging_setup.LOG_FORMAT
    assert "%(name)s" in logging_setup.LOG_FORMAT
    assert "%(message)s" in logging_setup.LOG_FORMAT
    record = logging.LogRecord(
        "trading_platform.engine", logging.INFO, __file__, 1, "started", None, None
    )
    rendered = logging.Formatter(logging_setup.LOG_FORMAT).format(record)
    assert "INFO" in rendered
    assert "trading_platform.engine" in rendered
    assert rendered.endswith("started")


def test_get_logger_returns_the_named_logger() -> None:
    logger = logging_setup.get_logger("trading_platform.metrics")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "trading_platform.metrics"


def test_configure_logging_defaults_to_info(restore_root_logging) -> None:
    logger = logging_setup.configure_logging()
    assert logger.level == logging.INFO
    assert logger is logging.getLogger()
    assert logger.handlers


def test_configure_logging_uses_the_requested_level(restore_root_logging) -> None:
    logging_setup.configure_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG
    logging_setup.configure_logging("warning")
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_reads_the_environment(monkeypatch, restore_root_logging) -> None:
    monkeypatch.setenv(platform_config.ENV_LOG_LEVEL, "debug")
    logging_setup.configure_logging()
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger(logging_setup.PACKAGE_LOGGER_NAME).level == logging.DEBUG


def test_configure_logging_falls_back_on_an_unknown_level(
    monkeypatch, restore_root_logging
) -> None:
    monkeypatch.setenv(platform_config.ENV_LOG_LEVEL, "LOUD")
    logging_setup.configure_logging()
    assert logging.getLogger().level == logging.INFO


def test_configure_logging_is_idempotent(restore_root_logging) -> None:
    logging_setup.configure_logging("INFO")
    installed = len(_installed_handlers())
    assert installed == 1
    logging_setup.configure_logging("INFO")
    assert len(_installed_handlers()) == installed
    logging_setup.configure_logging("DEBUG")
    assert len(_installed_handlers()) == installed


def test_configure_logging_writes_to_the_standard_output(capsys, restore_root_logging) -> None:
    logging_setup.configure_logging("INFO")
    logging_setup.get_logger("trading_platform.engine").info("supervisor ready")
    assert "supervisor ready" in capsys.readouterr().out


def test_configure_logging_adds_a_rotating_file_handler(
    tmp_path: Path, restore_root_logging
) -> None:
    log_dir = tmp_path / "logs"
    logging_setup.configure_logging("INFO", log_dir)
    logging_setup.get_logger("trading_platform.cli").warning("disk check")
    log_file = log_dir / logging_setup.DEFAULT_LOG_FILENAME
    assert log_file.is_file()
    assert "disk check" in log_file.read_text(encoding="utf-8")


def test_configure_logging_accepts_a_custom_filename(tmp_path: Path, restore_root_logging) -> None:
    logging_setup.configure_logging("INFO", tmp_path, filename="worker.log")
    logging_setup.get_logger("trading_platform.cli").info("hello")
    assert (tmp_path / "worker.log").is_file()


def test_configure_logging_survives_an_unusable_log_directory(
    tmp_path: Path, restore_root_logging
) -> None:
    blocker = tmp_path / "realtime.log"
    blocker.write_text("not a directory", encoding="utf-8")
    logging_setup.configure_logging("INFO", blocker)
    logging_setup.get_logger("trading_platform.cli").info("still logging")
    assert len(_installed_handlers()) == 1
    assert blocker.is_file()
    assert blocker.read_text(encoding="utf-8") == "not a directory"


def test_configure_logging_replaces_the_previous_file_handler(
    tmp_path: Path, restore_root_logging
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    logging_setup.configure_logging("INFO", first)
    assert len(_installed_handlers()) == 2
    logging_setup.get_logger("trading_platform.cli").info("first pass")
    logging_setup.configure_logging("INFO", second)
    assert len(_installed_handlers()) == 2
    logging_setup.get_logger("trading_platform.cli").info("second pass")
    log_file = logging_setup.DEFAULT_LOG_FILENAME
    assert (second / log_file).is_file()
    assert "second pass" in (second / log_file).read_text(encoding="utf-8")
    assert "second pass" not in (first / log_file).read_text(encoding="utf-8")


def test_configure_logging_keeps_foreign_handlers(restore_root_logging) -> None:
    root = logging.getLogger()
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        logging_setup.configure_logging("INFO")
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)
