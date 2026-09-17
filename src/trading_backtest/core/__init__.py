"""Core layer: domain models, error hierarchy and shared constants.

This is the only place where the cross-package domain objects live; every other
layer imports them from here and never redefines them.
"""

from __future__ import annotations

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
from trading_backtest.core.errors import (
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
from trading_backtest.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    RunnerFn,
    TradeRecord,
)

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
    "BacktestResult",
    "ConfigError",
    "DataDownloadError",
    "DataError",
    "DataValidationError",
    "Direction",
    "ExitReason",
    "FreqtradeConfigError",
    "InsufficientDataError",
    "MetricsError",
    "MonteCarloError",
    "ReportingError",
    "RobustnessError",
    "RunnerFn",
    "StrategyError",
    "TradeRecord",
    "TradingBacktestError",
    "ValidationLayerError",
    "WalkForwardError",
    "candle_delta",
    "periods_per_year",
    "timeframe_minutes",
]
