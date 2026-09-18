"""Contract tests for the shared exception hierarchy."""

from __future__ import annotations

import pytest

from trading_platform.core.errors import (
    ConfigError,
    DataDownloadError,
    DataError,
    DataValidationError,
    FreqtradeConfigError,
    InsufficientDataError,
    MetricsError,
    MonteCarloError,
    ReportingError,
    RobustnessError,
    StrategyError,
    TradingBacktestError,
    ValidationLayerError,
    WalkForwardError,
)

ALL_ERRORS = [
    ConfigError,
    DataError,
    DataValidationError,
    InsufficientDataError,
    DataDownloadError,
    StrategyError,
    ValidationLayerError,
    WalkForwardError,
    RobustnessError,
    MonteCarloError,
    MetricsError,
    ReportingError,
    FreqtradeConfigError,
]


@pytest.mark.parametrize("error_class", ALL_ERRORS)
def test_every_error_derives_from_the_base_class(error_class: type[Exception]) -> None:
    assert issubclass(error_class, TradingBacktestError)
    assert issubclass(error_class, Exception)
    error = error_class("boom")
    assert isinstance(error, TradingBacktestError)
    assert str(error) == "boom"
    assert error.issues == ()


@pytest.mark.parametrize(
    ("error_class", "parent"),
    [
        (DataError, TradingBacktestError),
        (DataValidationError, DataError),
        (InsufficientDataError, DataError),
        (DataDownloadError, DataError),
        (ValidationLayerError, TradingBacktestError),
        (WalkForwardError, ValidationLayerError),
        (RobustnessError, ValidationLayerError),
        (MonteCarloError, ValidationLayerError),
        (ConfigError, TradingBacktestError),
        (FreqtradeConfigError, ConfigError),
    ],
)
def test_hierarchy_chains(error_class: type[Exception], parent: type[Exception]) -> None:
    assert issubclass(error_class, parent)
    assert isinstance(error_class("x"), parent)


def test_unrelated_errors_are_not_confused() -> None:
    assert not issubclass(StrategyError, DataError)
    assert not issubclass(DataError, ConfigError)
    assert not issubclass(MetricsError, ValidationLayerError)


def test_issues_defaults_to_empty_tuple() -> None:
    assert TradingBacktestError("boom").issues == ()
    assert DataValidationError("boom").issues == ()


def test_issues_are_stored_and_exposed() -> None:
    error = DataValidationError("contract violated", ["missing close", "naive index"])
    assert error.issues == ("missing close", "naive index")
    assert isinstance(error.issues, tuple)
    assert error.message == "contract violated"


def test_data_validation_error_str_contains_message_and_issues() -> None:
    error = DataValidationError("contract violated", ["missing close", "naive index"])
    rendered = str(error)
    assert "contract violated" in rendered
    assert "missing close" in rendered
    assert "naive index" in rendered


def test_trading_platform_error_accepts_issues() -> None:
    error = TradingBacktestError("boom", ["first", "second"])
    assert error.issues == ("first", "second")
    assert "first" in str(error)


def test_errors_are_raisable_and_catchable_as_base() -> None:
    with pytest.raises(TradingBacktestError):
        raise InsufficientDataError("not enough candles")
    with pytest.raises(DataError):
        raise DataDownloadError("network down")
    with pytest.raises(ConfigError):
        raise FreqtradeConfigError("bad config.json")
    with pytest.raises(ValidationLayerError):
        raise WalkForwardError("no window")
