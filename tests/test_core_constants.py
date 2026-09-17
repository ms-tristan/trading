"""Contract tests for the shared constants and timeframe helpers."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pandas as pd
import pytest

import trading_backtest
from trading_backtest.core.constants import (
    DEFAULT_FEE_RATE,
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_SLIPPAGE,
    DEFAULT_STAKE_CURRENCY,
    DEFAULT_TIMEFRAME,
    OHLCV_INDEX_NAME,
    REQUIRED_OHLCV_COLUMNS,
    SIGNAL_COLUMNS,
    SUPPORTED_TIMEFRAMES,
    TIMEFRAME_MINUTES,
    UTC,
    candle_delta,
    periods_per_year,
    timeframe_minutes,
)
from trading_backtest.core.errors import ConfigError

TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")


def test_scalar_constants_match_the_contract() -> None:
    assert UTC == "UTC"
    assert DEFAULT_TIMEFRAME == "1h"
    assert DEFAULT_INITIAL_BALANCE == 10_000.0
    assert DEFAULT_FEE_RATE == 0.001
    assert DEFAULT_SLIPPAGE == 0.0
    assert DEFAULT_STAKE_CURRENCY == "USDT"
    assert OHLCV_INDEX_NAME == "timestamp"


def test_column_constants_match_the_contract() -> None:
    assert REQUIRED_OHLCV_COLUMNS == ("open", "high", "low", "close", "volume")
    assert SIGNAL_COLUMNS == ("entry_long", "exit_long", "entry_short", "exit_short", "stop_loss")
    assert isinstance(REQUIRED_OHLCV_COLUMNS, tuple)
    assert isinstance(SIGNAL_COLUMNS, tuple)


def test_supported_timeframe_tables_match_the_contract() -> None:
    assert SUPPORTED_TIMEFRAMES == {
        "1m": 525600.0,
        "5m": 105120.0,
        "15m": 35040.0,
        "30m": 17520.0,
        "1h": 8760.0,
        "4h": 2190.0,
        "1d": 365.0,
    }
    assert TIMEFRAME_MINUTES == {
        "1m": 1,
        "5m": 5,
        "15m": 15,
        "30m": 30,
        "1h": 60,
        "4h": 240,
        "1d": 1440,
    }
    assert tuple(SUPPORTED_TIMEFRAMES) == TIMEFRAMES


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_periods_per_year_for_every_supported_timeframe(timeframe: str) -> None:
    expected = SUPPORTED_TIMEFRAMES[timeframe]
    assert periods_per_year(timeframe) == expected
    assert isinstance(periods_per_year(timeframe), float)
    # 365 days * 24h * 60min / minutes-per-candle
    assert expected == pytest.approx(365 * 24 * 60 / TIMEFRAME_MINUTES[timeframe])


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_timeframe_minutes_for_every_supported_timeframe(timeframe: str) -> None:
    assert timeframe_minutes(timeframe) == TIMEFRAME_MINUTES[timeframe]
    assert isinstance(timeframe_minutes(timeframe), int)


@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_candle_delta_for_every_supported_timeframe(timeframe: str) -> None:
    delta = candle_delta(timeframe)
    assert isinstance(delta, pd.Timedelta)
    assert delta == pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    assert 365 * 24 * 60 * 60 / delta.total_seconds() == pytest.approx(periods_per_year(timeframe))


@pytest.mark.parametrize("timeframe", ["2h", "3d", "", "1H", "hourly", None, "1m "])
@pytest.mark.parametrize(
    "function", [periods_per_year, timeframe_minutes, candle_delta], ids=["ppy", "minutes", "delta"]
)
def test_unknown_timeframe_raises_config_error(function: object, timeframe: object) -> None:
    with pytest.raises(ConfigError) as excinfo:
        function(timeframe)  # type: ignore[operator]
    assert "unsupported timeframe" in str(excinfo.value)
    assert repr(timeframe) in str(excinfo.value)


def test_package_version_matches_pyproject() -> None:
    """``__version__`` must equal ``[project].version`` of pyproject.toml."""
    assert trading_backtest.__all__ == ["__version__"]
    assert isinstance(trading_backtest.__version__, str)
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert trading_backtest.__version__ == declared
