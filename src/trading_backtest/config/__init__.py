"""Typed configuration layer.

Public API::

    from trading_backtest.config import AppConfig, default_config, load_config
"""

from __future__ import annotations

from trading_backtest.config.loader import (
    default_config,
    default_monitoring_config,
    default_realtime_config,
    dump_config,
    load_config,
    load_monitoring_config,
    load_profiles,
    load_realtime_config,
    override_params,
)
from trading_backtest.config.models import (
    AppConfig,
    BacktestConfig,
    BenchmarkConfig,
    DataConfig,
    ExchangeConfig,
    MonitoringConfig,
    ProfileConfig,
    RealtimeConfig,
    ReportingConfig,
    RiskLimitsConfig,
    StrategyConfig,
    ValidationConfig,
)

__all__ = [
    "AppConfig",
    "BacktestConfig",
    "BenchmarkConfig",
    "DataConfig",
    "ExchangeConfig",
    "MonitoringConfig",
    "ProfileConfig",
    "RealtimeConfig",
    "ReportingConfig",
    "RiskLimitsConfig",
    "StrategyConfig",
    "ValidationConfig",
    "default_config",
    "default_monitoring_config",
    "default_realtime_config",
    "dump_config",
    "load_config",
    "load_monitoring_config",
    "load_profiles",
    "load_realtime_config",
    "override_params",
]
