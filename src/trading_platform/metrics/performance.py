"""Performance metrics of a completed backtest run.

The 23 metric names and their formulas are frozen by the architecture contract:
the validation layer imports :func:`metric_value` lazily and the reporting/CLI
layers render :data:`METRIC_NAMES` verbatim, so a rename would break three
packages at once.

Design rules:

* every metric is a plain ``float``; ``profit_factor`` is the only one allowed to
  be ``+inf`` (all winners, no loss at all);
* whenever a formula would produce ``NaN`` or ``inf`` the value falls back to
  ``0.0`` so that downstream consumers can always sum/format safely;
* trade dependent metrics return ``0.0`` on a run without any trade;
* nothing is rounded here: formatting belongs to the reporting layer.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, KeysView, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from trading_platform.core.constants import DEFAULT_TIMEFRAME, periods_per_year
from trading_platform.core.errors import MetricsError
from trading_platform.core.models import BacktestResult, TradeRecord
from trading_platform.metrics.drawdown import drawdown_duration, max_drawdown

__all__ = [
    "METRIC_NAMES",
    "MetricSet",
    "compute_metrics",
    "metric_value",
    "risk_free_rate_to_period",
]

#: The single source of truth for metric names -- order is part of the contract.
METRIC_NAMES: tuple[str, ...] = (
    "total_return",
    "cagr",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "max_drawdown_duration",
    "calmar_ratio",
    "volatility",
    "win_rate",
    "profit_factor",
    "expectancy",
    "avg_trade_pnl",
    "avg_win",
    "avg_loss",
    "largest_win",
    "largest_loss",
    "n_trades",
    "exposure",
    "best_trade_pct",
    "worst_trade_pct",
    "recovery_factor",
    "total_fees",
    "final_balance",
)

#: Metrics that are meaningful only when the run produced at least one trade.
TRADE_METRIC_NAMES: tuple[str, ...] = (
    "win_rate",
    "profit_factor",
    "expectancy",
    "avg_trade_pnl",
    "avg_win",
    "avg_loss",
    "largest_win",
    "largest_loss",
    "n_trades",
    "best_trade_pct",
    "worst_trade_pct",
    "total_fees",
)


def _finite(value: float) -> float:
    """Return ``value`` when it is finite, ``0.0`` otherwise (never NaN/inf)."""
    return value if math.isfinite(value) else 0.0


def _unknown_metric(name: str, available: tuple[str, ...] = METRIC_NAMES) -> MetricsError:
    """Build the uniform "unknown metric" error listing the available names."""
    listing = ", ".join(available)
    return MetricsError(f"unknown metric: {name!r}; available metrics: {listing}")


@dataclass(frozen=True)
class MetricSet:
    """Immutable bundle of computed metrics.

    The mapping protocol is intentionally read-only: ``values`` is exposed as a
    dictionary for interoperability, but the canonical accessors are
    :meth:`__getitem__` (which raises :class:`MetricsError` on an unknown name)
    and :meth:`get`.
    """

    values: dict[str, float]

    def __getitem__(self, name: str) -> float:
        """Return the metric ``name``; raise :class:`MetricsError` when unknown."""
        try:
            return self.values[name]
        except KeyError:
            raise _unknown_metric(name, tuple(sorted(self.values))) from None

    def get(self, name: str, default: float | None = None) -> float | None:
        """Return the metric ``name`` or ``default`` when it is not present."""
        return self.values.get(name, default)

    def as_dict(self) -> dict[str, float]:
        """Return a mutable copy of the metric values."""
        return dict(self.values)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable payload ``{"values": {...sorted...}}``."""
        return {"values": dict(sorted(self.values.items()))}

    def keys(self) -> KeysView[str]:
        """Metric names, in the order they were computed."""
        return self.values.keys()

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self) -> Iterator[str]:
        return iter(self.values)

    def __contains__(self, name: object) -> bool:
        return name in self.values


def risk_free_rate_to_period(annual_rate: float, timeframe: str) -> float:
    """Convert an annualised risk-free rate into a per-candle rate.

    Raises
    ------
    ConfigError
        If ``timeframe`` is not supported.
    """
    return float(annual_rate) / periods_per_year(timeframe)


def _cagr(initial_balance: float, final_balance: float, n_periods: int, ppy: float) -> float:
    """Compound annual growth rate over ``n_periods`` candles (locked formula)."""
    if n_periods >= 1 and final_balance > 0.0 and initial_balance > 0.0:
        try:
            compounded = float((final_balance / initial_balance) ** (ppy / n_periods))
        except OverflowError:
            compounded = float("inf")
        return _finite(compounded - 1.0)
    if final_balance <= 0.0:
        return -1.0
    return 0.0


def _exposure(trades: Sequence[TradeRecord], total_minutes: float) -> float:
    """Fraction of the backtested timeline spent in a position, clamped to [0, 1]."""
    if total_minutes <= 0.0:
        return 0.0
    invested = float(sum(float(trade.duration_minutes) for trade in trades))
    return min(max(invested / total_minutes, 0.0), 1.0)


def _timeline_minutes(equity: pd.Series) -> float:
    """Length of the equity curve in minutes (``0.0`` for fewer than two points)."""
    if len(equity) < 2:
        return 0.0
    span = pd.Timestamp(equity.index[-1]) - pd.Timestamp(equity.index[0])
    return float(span / pd.Timedelta(minutes=1))


def compute_metrics(
    result: BacktestResult,
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    risk_free_rate: float = 0.0,
) -> MetricSet:
    """Compute the full :data:`METRIC_NAMES` metric set for ``result``.

    Parameters
    ----------
    result:
        Completed backtest run (its equity curve is already normalised by
        :class:`~trading_platform.core.models.BacktestResult`).
    timeframe:
        Timeframe used to annualise return/volatility based metrics.
    risk_free_rate:
        Annualised risk-free rate subtracted from the annualised mean return in
        ``sharpe_ratio`` / ``sortino_ratio``.

    Raises
    ------
    ConfigError
        If ``timeframe`` is not supported.
    """
    ppy = periods_per_year(timeframe)
    rf = float(risk_free_rate)
    equity = result.equity_curve
    if not isinstance(equity, pd.Series):  # pragma: no cover - defensive guard
        equity = pd.Series(equity, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        returns = equity.pct_change().dropna()
    # A curve starting at (or crossing) zero produces infinite percentage changes;
    # they carry no information and would poison every aggregate below.
    returns = returns[np.isfinite(returns.to_numpy(dtype="float64"))]
    n_returns = len(returns)

    initial_balance = float(result.initial_balance)
    final_balance = _finite(float(result.final_balance))
    n_periods = len(equity) - 1

    total_return = _finite(final_balance / initial_balance - 1.0) if initial_balance else 0.0
    cagr = _cagr(initial_balance, final_balance, n_periods, ppy)

    std = float(returns.std(ddof=1)) if n_returns >= 2 else 0.0
    annual_volatility = std * math.sqrt(ppy)
    volatility = _finite(annual_volatility) if n_returns >= 2 else 0.0
    annual_mean = float(returns.mean()) * ppy if n_returns else 0.0
    sharpe_ratio = (
        _finite((annual_mean - rf) / annual_volatility)
        if n_returns >= 2 and annual_volatility > 0.0
        else 0.0
    )
    if n_returns >= 2:
        downside = np.minimum(returns.to_numpy(dtype="float64"), 0.0)
        downside_deviation = float(np.sqrt(np.mean(downside**2))) * math.sqrt(ppy)
    else:
        downside_deviation = 0.0
    sortino_ratio = (
        _finite((annual_mean - rf) / downside_deviation)
        if n_returns >= 2 and downside_deviation > 0.0
        else 0.0
    )

    max_dd = max_drawdown(equity)
    max_dd_duration = float(drawdown_duration(equity))
    calmar_ratio = _finite(cagr / abs(max_dd)) if abs(max_dd) > 0.0 else 0.0

    trades = list(result.trades)
    pnls = [float(trade.pnl) for trade in trades]
    pnl_pcts = [float(trade.pnl_pct) for trade in trades]
    winners = [pnl for pnl in pnls if pnl > 0.0]
    losers = [pnl for pnl in pnls if pnl < 0.0]
    n_trades = float(len(trades))
    gross_profit = float(sum(winners))
    gross_loss = float(sum(losers))

    win_rate = len(winners) / n_trades if n_trades else 0.0
    if gross_loss < 0.0:
        profit_factor = _finite(gross_profit / abs(gross_loss))
    elif gross_profit > 0.0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0
    expectancy = _finite(float(np.mean(pnls))) if pnls else 0.0
    avg_win = _finite(float(np.mean(winners))) if winners else 0.0
    avg_loss = _finite(float(np.mean(losers))) if losers else 0.0
    largest_win = _finite(max(pnls)) if pnls else 0.0
    largest_loss = _finite(min(pnls)) if pnls else 0.0
    best_trade_pct = _finite(max(pnl_pcts)) if pnl_pcts else 0.0
    worst_trade_pct = _finite(min(pnl_pcts)) if pnl_pcts else 0.0
    total_fees = _finite(float(sum(float(trade.fees) for trade in trades)))

    exposure = _exposure(trades, _timeline_minutes(equity))
    recovery_factor = (
        _finite((final_balance - initial_balance) / abs(max_dd * initial_balance))
        if max_dd < 0.0 and initial_balance
        else 0.0
    )

    values: dict[str, float] = {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "max_drawdown": max_dd,
        "max_drawdown_duration": max_dd_duration,
        "calmar_ratio": calmar_ratio,
        "volatility": volatility,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "avg_trade_pnl": expectancy,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "n_trades": n_trades,
        "exposure": exposure,
        "best_trade_pct": best_trade_pct,
        "worst_trade_pct": worst_trade_pct,
        "recovery_factor": recovery_factor,
        "total_fees": total_fees,
        "final_balance": final_balance,
    }
    return MetricSet(values=values)


def metric_value(
    result: BacktestResult,
    name: str,
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    risk_free_rate: float = 0.0,
) -> float:
    """Return a single metric by name.

    This helper is the frozen seam consumed by the validation layer (which
    imports it lazily) and keeps the name/signature stable.

    Raises
    ------
    MetricsError
        If ``name`` is not one of :data:`METRIC_NAMES`.
    ConfigError
        If ``timeframe`` is not supported.
    """
    if name not in METRIC_NAMES:
        raise _unknown_metric(name)
    return compute_metrics(result, timeframe=timeframe, risk_free_rate=risk_free_rate)[name]
