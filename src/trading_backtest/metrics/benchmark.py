"""Buy & hold benchmark: the reference a strategy has to beat.

Why it matters
--------------
On a rising asset -- BTC gained roughly +471% between 2023 and 2025 -- *any*
long-only strategy looks profitable, so a raw total return is not evidence of
skill.  The honest question is "did the strategy beat doing nothing at all?", i.e.
buying the asset on the first candle of the backtest window and holding it until
the last one.  This module answers it on the exact same window, with the exact
same initial capital and the exact same costs as the backtest, so the comparison
is apples to apples.

Design rules:

* no metric maths is duplicated here: the benchmark equity curve is wrapped in a
  synthetic :class:`~trading_backtest.core.models.BacktestResult` and handed over
  to the frozen :func:`trading_backtest.metrics.performance.compute_metrics`;
* the entry rule mirrors the engine byte for byte -- ``size = balance / (fill *
  (1 + fee_rate))`` with ``fill = close[0] * (1 + slippage)`` -- and the exit
  fee/slippage are charged on the very last candle only (mark to market);
* :data:`BENCHMARK_METRIC_NAMES` is the frozen, ordered subset of the 23
  :data:`~trading_backtest.metrics.performance.METRIC_NAMES` that is meaningful
  for a passive curve (``METRIC_NAMES`` itself is never touched);
* ``variant="none"`` means "no benchmark at all": it yields ``None``, never a
  curve;
* the layer rule is unchanged -- this module imports ``core`` and its sibling
  metric modules only, never ``config``/``data``/``strategy``/``validation``/
  ``reporting``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, KeysView
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from trading_backtest.core.constants import (
    DEFAULT_INITIAL_BALANCE,
    DEFAULT_TIMEFRAME,
    OHLCV_INDEX_NAME,
    UTC,
)
from trading_backtest.core.errors import MetricsError
from trading_backtest.core.models import BacktestResult
from trading_backtest.metrics.performance import MetricSet, compute_metrics

__all__ = [
    "BENCHMARK_METRIC_NAMES",
    "BENCHMARK_VARIANTS",
    "DEFAULT_BENCHMARK_VARIANT",
    "MIN_RETURNS_FOR_BETA",
    "BenchmarkComparison",
    "BenchmarkResult",
    "BenchmarkVariant",
    "benchmark_alpha",
    "buy_and_hold_equity",
    "compare_benchmark",
    "compute_benchmark",
]

#: Supported benchmark variants, in the order they are rendered in error messages.
BENCHMARK_VARIANTS: tuple[str, ...] = ("buy_and_hold", "cash", "none")

#: Benchmark variant name.
BenchmarkVariant = Literal["buy_and_hold", "cash", "none"]

#: Variant used when the caller does not choose one.
DEFAULT_BENCHMARK_VARIANT: BenchmarkVariant = "buy_and_hold"

#: Ordered subset of ``METRIC_NAMES`` computed for a benchmark curve.
BENCHMARK_METRIC_NAMES: tuple[str, ...] = (
    "total_return",
    "cagr",
    "volatility",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "max_drawdown_duration",
    "final_balance",
)

#: Below this number of aligned returns beta/correlation carry no information.
MIN_RETURNS_FOR_BETA: int = 3

#: Label used for the strategy side of a comparison (it is not a benchmark variant).
_STRATEGY_LABEL = "strategy"


def _unknown_variant(variant: object) -> MetricsError:
    """Build the uniform "unknown variant" error listing the supported variants."""
    listing = ", ".join(BENCHMARK_VARIANTS)
    return MetricsError(f"unknown benchmark variant: {variant!r}; available variants: {listing}")


def _curve_variant_error(variant: str) -> MetricsError:
    """Error raised when a variant has no equity curve of its own."""
    listing = ", ".join(BENCHMARK_VARIANTS)
    return MetricsError(
        f"benchmark variant {variant!r} has no equity curve: it means 'no benchmark'; "
        f"available curve variants: {listing}"
    )


def _normalise_index(index: pd.Index) -> pd.DatetimeIndex:
    """Return ``index`` as a UTC, ascending, deduplicated ``DatetimeIndex``.

    Mirrors :func:`trading_backtest.core.models._normalise_equity` so that the
    benchmark curve is aligned with a real backtest equity curve whatever the
    timezone/resolution of the input frame.
    """
    out = pd.DatetimeIndex(index)
    out = out.tz_localize(UTC) if out.tz is None else out.tz_convert(UTC)
    out = out.as_unit("us")
    out.name = OHLCV_INDEX_NAME
    return out


def _benchmark_prices(data: pd.DataFrame) -> pd.Series:
    """Return the forward-filled ``close`` column of ``data`` on a normalised index.

    Raises
    ------
    MetricsError
        If ``data`` is not a frame, has no numeric ``close`` column, is empty or
        starts with a NaN/non-positive close.
    """
    if not isinstance(data, pd.DataFrame):  # pragma: no cover - defensive guard
        raise MetricsError(f"benchmark requires a pandas DataFrame, got {type(data).__name__}")
    if "close" not in data.columns:
        raise MetricsError("benchmark requires a 'close' column in the OHLCV frame")
    if len(data.index) == 0:
        raise MetricsError("benchmark requires at least one candle, got an empty frame")
    try:
        closes = data["close"].astype("float64")
    except (TypeError, ValueError) as exc:
        raise MetricsError(f"benchmark requires a numeric 'close' column: {exc}") from exc
    index = _normalise_index(closes.index)
    prices = pd.Series(closes.to_numpy(dtype="float64"), index=index, name="close")
    if prices.index.has_duplicates or not prices.index.is_monotonic_increasing:
        prices = prices[~prices.index.duplicated(keep="last")].sort_index()
    prices = prices.ffill()
    first = float(prices.iloc[0])
    if not math.isfinite(first) or first <= 0.0:
        raise MetricsError(f"benchmark requires a positive first close, got {first!r}")
    return prices


def buy_and_hold_equity(
    data: pd.DataFrame,
    *,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    fee_rate: float = 0.0,
    slippage: float = 0.0,
    variant: BenchmarkVariant = DEFAULT_BENCHMARK_VARIANT,
) -> pd.Series:
    """Return the equity curve of a passive ``variant`` portfolio.

    ``buy_and_hold`` buys at the first candle's close (with slippage) for exactly
    ``initial_balance / (entry_fill * (1 + fee_rate))`` units -- the engine's own
    sizing rule -- and marks the position to market on every candle.  Only the
    last candle bears the exit costs: ``equity[-1] = size * close[-1] * (1 -
    slippage) * (1 - fee_rate)``, which makes the curve comparable with a
    strategy that liquidates at the end of the backtest.  A single-candle frame is
    therefore also a "last candle" and gets the exit adjustment.

    ``cash`` returns a flat curve equal to ``initial_balance`` (a risk-free
    alternative that ignores fees and slippage); it is the natural yardstick for
    "not trading at all".

    Parameters
    ----------
    data:
        OHLCV frame; only ``close`` is read.
    initial_balance:
        Capital invested at the first candle.
    fee_rate:
        Per-side fee rate (same convention as the engine).
    slippage:
        Relative slippage, always working against the position.
    variant:
        ``"buy_and_hold"`` or ``"cash"``.

    Returns
    -------
    pandas.Series
        ``float64`` series named ``"equity"`` indexed by an ascending UTC
        ``DatetimeIndex`` named ``"timestamp"``.

    Raises
    ------
    MetricsError
        If ``variant`` is ``"none"``/unknown, or if ``data`` has no usable close.
    """
    if variant == "none":
        raise _curve_variant_error(variant)
    if variant not in BENCHMARK_VARIANTS:
        raise _unknown_variant(variant)
    prices = _benchmark_prices(data)
    balance = float(initial_balance)
    if variant == "cash":
        values = np.full(len(prices), balance, dtype="float64")
        return pd.Series(values, index=prices.index, name="equity", dtype="float64")
    fee = float(fee_rate)
    slip = float(slippage)
    closes = prices.to_numpy(dtype="float64")
    entry_fill = float(closes[0]) * (1.0 + slip)
    size = balance / (entry_fill * (1.0 + fee))
    values = size * closes
    values[-1] = size * float(closes[-1]) * (1.0 - slip) * (1.0 - fee)
    return pd.Series(values, index=prices.index, name="equity", dtype="float64")


def _select_metrics(metric_set: MetricSet) -> dict[str, float]:
    """Return the 8 :data:`BENCHMARK_METRIC_NAMES` taken from ``metric_set``."""
    return {name: float(metric_set[name]) for name in BENCHMARK_METRIC_NAMES}


@dataclass(frozen=True)
class BenchmarkResult:
    """Immutable benchmark outcome: the metrics of one passive equity curve.

    The mapping protocol mirrors
    :class:`~trading_backtest.metrics.performance.MetricSet`, so downstream
    layers can read a benchmark exactly like a metric set.
    """

    variant: str
    initial_balance: float
    final_balance: float
    n_periods: int
    timeframe: str
    metrics: dict[str, float]
    equity_curve: pd.Series

    def __getitem__(self, name: str) -> float:
        """Return the benchmark metric ``name``; raise :class:`MetricsError` if unknown."""
        try:
            return self.metrics[name]
        except KeyError:
            listing = ", ".join(BENCHMARK_METRIC_NAMES)
            raise MetricsError(
                f"unknown benchmark metric: {name!r}; available metrics: {listing}"
            ) from None

    def get(self, name: str, default: float | None = None) -> float | None:
        """Return the benchmark metric ``name`` or ``default`` when it is absent."""
        return self.metrics.get(name, default)

    def as_dict(self) -> dict[str, float]:
        """Return a mutable copy of the benchmark metrics."""
        return dict(self.metrics)

    def keys(self) -> KeysView[str]:
        """Benchmark metric names, in the frozen :data:`BENCHMARK_METRIC_NAMES` order."""
        return self.metrics.keys()

    def __len__(self) -> int:
        return len(self.metrics)

    def __iter__(self) -> Iterator[str]:
        return iter(self.metrics)

    def __contains__(self, name: object) -> bool:
        return name in self.metrics

    def to_dict(self) -> dict[str, Any]:
        """Return the scalar payload of the benchmark (equity curve excluded).

        The equity curve is deliberately left out so that markdown bullets and
        JSON reports stay compact and deterministic.
        """
        return {
            "variant": str(self.variant),
            "initial_balance": float(self.initial_balance),
            "final_balance": float(self.final_balance),
            "n_periods": int(self.n_periods),
            "timeframe": str(self.timeframe),
            "metrics": dict(self.metrics),
        }

    def __eq__(self, other: object) -> bool:
        """Compare scalars, metrics and the equity curve element-wise."""
        if not isinstance(other, BenchmarkResult):
            return NotImplemented
        scalars = (
            self.variant == other.variant
            and self.n_periods == other.n_periods
            and self.timeframe == other.timeframe
            and float(self.initial_balance) == float(other.initial_balance)
            and float(self.final_balance) == float(other.final_balance)
            and self.metrics == other.metrics
        )
        if not scalars:
            return False
        try:
            pd.testing.assert_series_equal(
                self.equity_curve,
                other.equity_curve,
                check_exact=True,
                check_names=True,
                check_freq=False,
            )
        except AssertionError:
            return False
        return True

    def __hash__(self) -> int:
        """Hash the scalar payload (the equity curve is intentionally excluded)."""
        return hash(
            (
                self.variant,
                float(self.initial_balance),
                float(self.final_balance),
                int(self.n_periods),
                self.timeframe,
                tuple(sorted(self.metrics.items())),
            )
        )


def _benchmark_result(
    data: pd.DataFrame,
    *,
    variant: BenchmarkVariant,
    initial_balance: float,
    fee_rate: float,
    slippage: float,
    timeframe: str,
    risk_free_rate: float,
    symbol: str,
) -> BenchmarkResult:
    """Build the :class:`BenchmarkResult` of ``variant`` over ``data``.

    The curve is wrapped in a synthetic
    :class:`~trading_backtest.core.models.BacktestResult` and measured with the
    frozen :func:`~trading_backtest.metrics.performance.compute_metrics`, so not a
    single metric formula is duplicated in this module.
    """
    equity = buy_and_hold_equity(
        data,
        initial_balance=initial_balance,
        fee_rate=fee_rate,
        slippage=slippage,
        variant=variant,
    )
    final_balance = float(equity.iloc[-1])
    synthetic = BacktestResult(
        strategy_name="benchmark",
        symbol=str(symbol),
        timeframe=str(timeframe),
        start=pd.Timestamp(equity.index[0]),
        end=pd.Timestamp(equity.index[-1]),
        initial_balance=float(initial_balance),
        final_balance=final_balance,
        trades=[],
        equity_curve=equity,
        params={},
        metadata={"variant": variant},
    )
    computed = compute_metrics(synthetic, timeframe=timeframe, risk_free_rate=risk_free_rate)
    return BenchmarkResult(
        variant=variant,
        initial_balance=float(initial_balance),
        final_balance=final_balance,
        n_periods=len(equity) - 1,
        timeframe=str(timeframe),
        metrics=_select_metrics(computed),
        equity_curve=equity,
    )


def compute_benchmark(
    data: pd.DataFrame,
    *,
    variant: BenchmarkVariant = DEFAULT_BENCHMARK_VARIANT,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    fee_rate: float = 0.0,
    slippage: float = 0.0,
    timeframe: str = DEFAULT_TIMEFRAME,
    risk_free_rate: float = 0.0,
    symbol: str = "",
) -> BenchmarkResult | None:
    """Measure the ``variant`` benchmark over ``data``.

    Parameters
    ----------
    data:
        OHLCV frame the strategy was backtested on (same window, same capital).
    variant:
        ``"buy_and_hold"``, ``"cash"`` or ``"none"``.
    initial_balance:
        Initial capital, identical to the backtest's.
    fee_rate, slippage:
        Costs applied exactly like the engine does.
    timeframe:
        Timeframe used to annualise ``cagr``/``volatility``/``sharpe_ratio``.
    risk_free_rate:
        Annualised risk-free rate forwarded to
        :func:`~trading_backtest.metrics.performance.compute_metrics`.
    symbol:
        Free-form symbol carried by the synthetic result.

    Returns
    -------
    BenchmarkResult | None
        ``None`` if and only if ``variant == "none"``.

    Raises
    ------
    MetricsError
        If ``variant`` is neither a known variant nor ``"none"``, or if ``data``
        has no usable close.
    ConfigError
        Propagated untouched from :func:`compute_metrics` for an unsupported
        ``timeframe``.
    """
    if variant == "none":
        return None
    if variant not in BENCHMARK_VARIANTS:
        raise _unknown_variant(variant)
    return _benchmark_result(
        data,
        variant=variant,
        initial_balance=float(initial_balance),
        fee_rate=float(fee_rate),
        slippage=float(slippage),
        timeframe=str(timeframe),
        risk_free_rate=float(risk_free_rate),
        symbol=str(symbol),
    )


def benchmark_alpha(strategy_total_return: float, benchmark_total_return: float) -> float:
    """Return ``strategy - benchmark`` total return, as a fraction.

    The value is expressed in the same unit as the ``total_return`` metric, so
    ``benchmark_alpha(...) * 100`` is an excess return in percentage points --
    negative means the strategy destroyed value against doing nothing.
    """
    return float(strategy_total_return) - float(benchmark_total_return)


def _aligned_returns(strategy_equity: pd.Series, benchmark_equity: pd.Series) -> pd.DataFrame:
    """Return the finite strategy/benchmark returns on their common timestamps."""
    with np.errstate(divide="ignore", invalid="ignore"):
        strategy_returns = strategy_equity.astype("float64").pct_change()
        benchmark_returns = benchmark_equity.astype("float64").pct_change()
    joined = pd.concat(
        [strategy_returns.rename(_STRATEGY_LABEL), benchmark_returns.rename("benchmark")],
        axis=1,
        join="inner",
    ).dropna()
    finite = np.isfinite(joined.to_numpy(dtype="float64")).all(axis=1)
    aligned: pd.DataFrame = joined[finite]
    return aligned


def _beta_correlation(returns: pd.DataFrame) -> tuple[float | None, float | None]:
    """Return ``(beta, correlation)`` of the strategy against the benchmark.

    Both values are plain ``float`` or ``None`` -- never numpy scalars, ``NaN`` or
    ``inf``.  ``beta`` is ``None`` when the benchmark variance is not positive
    (a flat ``cash`` curve carries no market exposure to measure), and both are
    ``None`` below :data:`MIN_RETURNS_FOR_BETA` aligned returns.
    """
    if len(returns) < MIN_RETURNS_FOR_BETA:
        return None, None
    strategy_values = returns[_STRATEGY_LABEL].to_numpy(dtype="float64")
    benchmark_values = returns["benchmark"].to_numpy(dtype="float64")
    benchmark_variance = float(np.var(benchmark_values, ddof=1))

    beta: float | None = None
    if math.isfinite(benchmark_variance) and benchmark_variance > 0.0:
        covariance = float(np.cov(strategy_values, benchmark_values, ddof=1)[0, 1])
        candidate = covariance / benchmark_variance
        if math.isfinite(candidate):
            beta = float(candidate)

    correlation: float | None = None
    strategy_std = float(np.std(strategy_values, ddof=1))
    benchmark_std = math.sqrt(benchmark_variance) if benchmark_variance > 0.0 else 0.0
    if strategy_std > 0.0 and benchmark_std > 0.0:
        candidate_corr = float(np.corrcoef(strategy_values, benchmark_values)[0, 1])
        if math.isfinite(candidate_corr):
            correlation = float(candidate_corr)
    return beta, correlation


@dataclass(frozen=True)
class BenchmarkComparison:
    """Side-by-side outcome of a strategy against its benchmark.

    ``strategy`` and ``benchmark`` are :class:`BenchmarkResult` objects, so both
    sides answer to the same 8 frozen metric names and the gap is a plain
    subtraction of like-for-like numbers.
    """

    variant: str
    timeframe: str
    initial_balance: float
    fee_rate: float
    slippage: float
    n_periods: int
    n_returns: int
    strategy: BenchmarkResult
    benchmark: BenchmarkResult
    alpha: float
    beta: float | None
    correlation: float | None

    def gap(self) -> dict[str, float]:
        """Return ``strategy - benchmark`` for each :data:`BENCHMARK_METRIC_NAMES`."""
        return {name: self.strategy[name] - self.benchmark[name] for name in BENCHMARK_METRIC_NAMES}

    def comparison_rows(self) -> list[dict[str, Any]]:
        """Return the 3 flat rows (strategy, benchmark, gap) of the side-by-side table.

        Every row is JSON-native and keyed ``variant`` first, then the 8 metric
        names in their frozen order, which is exactly what the markdown and JSON
        report writers need.
        """
        return [
            {"variant": _STRATEGY_LABEL, **self.strategy.metrics},
            {"variant": str(self.variant), **self.benchmark.metrics},
            {"variant": "gap", **self.gap()},
        ]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable payload of the comparison (no equity curves)."""
        return {
            "variant": str(self.variant),
            "timeframe": str(self.timeframe),
            "initial_balance": float(self.initial_balance),
            "fee_rate": float(self.fee_rate),
            "slippage": float(self.slippage),
            "n_periods": int(self.n_periods),
            "n_returns": int(self.n_returns),
            "alpha": float(self.alpha),
            "beta": None if self.beta is None else float(self.beta),
            "correlation": None if self.correlation is None else float(self.correlation),
            "strategy": self.strategy.as_dict(),
            "benchmark": self.benchmark.as_dict(),
            "gap": self.gap(),
        }


def compare_benchmark(
    strategy: BacktestResult,
    data: pd.DataFrame,
    *,
    variant: BenchmarkVariant = DEFAULT_BENCHMARK_VARIANT,
    initial_balance: float | None = None,
    fee_rate: float = 0.0,
    slippage: float = 0.0,
    risk_free_rate: float = 0.0,
    strategy_metrics: MetricSet | None = None,
) -> BenchmarkComparison | None:
    """Compare a completed backtest with its ``variant`` benchmark.

    The benchmark covers the very same window and initial capital as ``strategy``
    and is charged the same ``fee_rate``/``slippage``, so ``alpha`` is a fair
    excess return.  ``beta``/``correlation`` are measured on the returns aligned
    on the common timestamps and are ``None`` whenever they cannot be computed.

    Parameters
    ----------
    strategy:
        Completed backtest run.
    data:
        OHLCV frame the run was produced from.
    variant:
        ``"buy_and_hold"``, ``"cash"`` or ``"none"``.
    initial_balance:
        Capital both sides start with; ``None`` means "the strategy's own".
    fee_rate, slippage:
        Costs applied to the benchmark side.
    risk_free_rate:
        Annualised risk-free rate used for ``sharpe_ratio``/``sortino_ratio``.
    strategy_metrics:
        Pre-computed strategy metrics (avoids recomputing them); ``None`` means
        "compute them here".

    Returns
    -------
    BenchmarkComparison | None
        ``None`` if and only if ``variant == "none"``.

    Raises
    ------
    MetricsError
        If ``variant`` is neither a known variant nor ``"none"``.
    ConfigError
        Propagated untouched for an unsupported timeframe.
    """
    if variant == "none":
        return None
    if variant not in BENCHMARK_VARIANTS:
        raise _unknown_variant(variant)
    balance = float(strategy.initial_balance) if initial_balance is None else float(initial_balance)
    timeframe = str(strategy.timeframe)
    metric_set = (
        compute_metrics(strategy, timeframe=timeframe, risk_free_rate=risk_free_rate)
        if strategy_metrics is None
        else strategy_metrics
    )
    strategy_result = BenchmarkResult(
        variant=_STRATEGY_LABEL,
        initial_balance=float(strategy.initial_balance),
        final_balance=float(strategy.final_balance),
        n_periods=len(strategy.equity_curve) - 1,
        timeframe=timeframe,
        metrics=_select_metrics(metric_set),
        equity_curve=strategy.equity_curve,
    )
    benchmark_result = _benchmark_result(
        data,
        variant=variant,
        initial_balance=balance,
        fee_rate=float(fee_rate),
        slippage=float(slippage),
        timeframe=timeframe,
        risk_free_rate=float(risk_free_rate),
        symbol=str(strategy.symbol),
    )
    aligned = _aligned_returns(strategy.equity_curve, benchmark_result.equity_curve)
    beta, correlation = _beta_correlation(aligned)
    return BenchmarkComparison(
        variant=variant,
        timeframe=timeframe,
        initial_balance=balance,
        fee_rate=float(fee_rate),
        slippage=float(slippage),
        n_periods=benchmark_result.n_periods,
        n_returns=len(aligned),
        strategy=strategy_result,
        benchmark=benchmark_result,
        alpha=benchmark_alpha(strategy_result["total_return"], benchmark_result["total_return"]),
        beta=beta,
        correlation=correlation,
    )
