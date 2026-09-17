"""Shared constants and small timeframe helpers (no heavy side effects)."""

from __future__ import annotations

import pandas as pd

from trading_backtest.core.errors import ConfigError

__all__ = [
    "DEFAULT_FEE_RATE",
    "DEFAULT_INITIAL_BALANCE",
    "DEFAULT_SLIPPAGE",
    "DEFAULT_STAKE_CURRENCY",
    "DEFAULT_TIMEFRAME",
    "OHLCV_INDEX_NAME",
    "REQUIRED_OHLCV_COLUMNS",
    "SIGNAL_COLUMNS",
    "SUPPORTED_TIMEFRAMES",
    "TIMEFRAME_MINUTES",
    "UTC",
    "candle_delta",
    "periods_per_year",
    "timeframe_minutes",
]

UTC = "UTC"
DEFAULT_TIMEFRAME = "1h"
DEFAULT_INITIAL_BALANCE = 10_000.0
DEFAULT_FEE_RATE = 0.001
DEFAULT_SLIPPAGE = 0.0
DEFAULT_STAKE_CURRENCY = "USDT"

REQUIRED_OHLCV_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
OHLCV_INDEX_NAME = "timestamp"
SIGNAL_COLUMNS: tuple[str, ...] = (
    "entry_long",
    "exit_long",
    "entry_short",
    "exit_short",
    "stop_loss",
)

#: Number of candles in a year per supported timeframe (365 days, 24/7 markets).
SUPPORTED_TIMEFRAMES: dict[str, float] = {
    "1m": 525600.0,
    "5m": 105120.0,
    "15m": 35040.0,
    "30m": 17520.0,
    "1h": 8760.0,
    "4h": 2190.0,
    "1d": 365.0,
}

#: Duration of a single candle, in minutes.
TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


def periods_per_year(timeframe: str) -> float:
    """Return the number of candles of ``timeframe`` in one year.

    Raises
    ------
    ConfigError
        If ``timeframe`` is not supported.
    """
    try:
        return SUPPORTED_TIMEFRAMES[timeframe]
    except (KeyError, TypeError):
        raise ConfigError(f"unsupported timeframe: {timeframe!r}") from None


def timeframe_minutes(timeframe: str) -> int:
    """Return the duration of one ``timeframe`` candle in minutes.

    Raises
    ------
    ConfigError
        If ``timeframe`` is not supported.
    """
    try:
        return TIMEFRAME_MINUTES[timeframe]
    except (KeyError, TypeError):
        raise ConfigError(f"unsupported timeframe: {timeframe!r}") from None


def candle_delta(timeframe: str) -> pd.Timedelta:
    """Return the duration of one ``timeframe`` candle as a :class:`pandas.Timedelta`.

    Raises
    ------
    ConfigError
        If ``timeframe`` is not supported.
    """
    return pd.Timedelta(minutes=timeframe_minutes(timeframe))
