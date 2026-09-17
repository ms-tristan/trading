"""Metrics layer: drawdown statistics and the locked performance metric set.

Public surface (frozen contract)::

    from trading_backtest.metrics import (
        METRIC_NAMES, MetricSet, compute_metrics, metric_value,
        risk_free_rate_to_period, drawdown_series, max_drawdown,
        drawdown_duration, drawdown_table,
    )

This module only depends on :mod:`trading_backtest.core`; it never imports the
validation or reporting layers, which is what lets the four work packages be
built concurrently.
"""

from __future__ import annotations

from trading_backtest.core.errors import MetricsError
from trading_backtest.metrics.drawdown import (
    drawdown_duration,
    drawdown_series,
    drawdown_table,
    max_drawdown,
)
from trading_backtest.metrics.performance import (
    METRIC_NAMES,
    TRADE_METRIC_NAMES,
    MetricSet,
    compute_metrics,
    metric_value,
    risk_free_rate_to_period,
)

__all__ = [
    "METRIC_NAMES",
    "TRADE_METRIC_NAMES",
    "MetricSet",
    "MetricsError",
    "compute_metrics",
    "drawdown_duration",
    "drawdown_series",
    "drawdown_table",
    "max_drawdown",
    "metric_value",
    "risk_free_rate_to_period",
]
