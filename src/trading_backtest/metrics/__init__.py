"""Metrics layer: drawdown statistics, the locked performance metric set and the
buy & hold benchmark.

Public surface (frozen contract)::

    from trading_backtest.metrics import (
        METRIC_NAMES, MetricSet, compute_metrics, metric_value,
        risk_free_rate_to_period, drawdown_series, max_drawdown,
        drawdown_duration, drawdown_table,
        BENCHMARK_VARIANTS, BENCHMARK_METRIC_NAMES, DEFAULT_BENCHMARK_VARIANT,
        MIN_RETURNS_FOR_BETA, BenchmarkResult, BenchmarkComparison,
        BenchmarkVariant, benchmark_alpha, buy_and_hold_equity,
        compare_benchmark, compute_benchmark,
    )

This module only depends on :mod:`trading_backtest.core`; it never imports the
validation or reporting layers, which is what lets the four work packages be
built concurrently.
"""

from __future__ import annotations

from trading_backtest.core.errors import MetricsError
from trading_backtest.metrics.benchmark import (
    BENCHMARK_METRIC_NAMES,
    BENCHMARK_VARIANTS,
    DEFAULT_BENCHMARK_VARIANT,
    MIN_RETURNS_FOR_BETA,
    BenchmarkComparison,
    BenchmarkResult,
    BenchmarkVariant,
    benchmark_alpha,
    buy_and_hold_equity,
    compare_benchmark,
    compute_benchmark,
)
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
    "BENCHMARK_METRIC_NAMES",
    "BENCHMARK_VARIANTS",
    "DEFAULT_BENCHMARK_VARIANT",
    "METRIC_NAMES",
    "MIN_RETURNS_FOR_BETA",
    "TRADE_METRIC_NAMES",
    "BenchmarkComparison",
    "BenchmarkResult",
    "BenchmarkVariant",
    "MetricSet",
    "MetricsError",
    "benchmark_alpha",
    "buy_and_hold_equity",
    "compare_benchmark",
    "compute_benchmark",
    "compute_metrics",
    "drawdown_duration",
    "drawdown_series",
    "drawdown_table",
    "max_drawdown",
    "metric_value",
    "risk_free_rate_to_period",
]
