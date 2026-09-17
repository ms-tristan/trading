"""Typed configuration layer.

Public API::

    from trading_backtest.config import AppConfig, default_config, load_config
"""

from __future__ import annotations

from trading_backtest.config.loader import (
    default_config,
    dump_config,
    load_config,
    override_params,
)
from trading_backtest.config.models import (
    AppConfig,
    BacktestConfig,
    DataConfig,
    ExchangeConfig,
    ReportingConfig,
    StrategyConfig,
    ValidationConfig,
)

__all__ = [
    "AppConfig",
    "BacktestConfig",
    "DataConfig",
    "ExchangeConfig",
    "ReportingConfig",
    "StrategyConfig",
    "ValidationConfig",
    "default_config",
    "dump_config",
    "load_config",
    "override_params",
]
