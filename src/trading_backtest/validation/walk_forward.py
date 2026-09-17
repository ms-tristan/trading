"""Walk-forward analysis: run, score and stitch a series of IS/OOS windows.

The walk-forward test is the backbone of the statistical validation layer.  It
answers a single question: *does the strategy keep performing on data it has
never seen, repeatedly, over time?*

For every window produced by
:func:`trading_backtest.validation.split.make_windows` this module:

1. runs the injected ``runner`` on the in-sample slice and on the
   out-of-sample slice (the runner is always injected — this layer never
   imports the strategy or the engine);
2. scores both results with a single metric function;
3. stitches the out-of-sample equity curves into one continuous curve, rebased
   so that it starts at the first window's initial balance.

Two rules are deliberately mechanical:

* the metric function is resolved **eagerly**, before the first runner call, so
  that an unknown metric name fails fast without wasting a backtest;
* nothing is ever re-optimised here: parameter optimisation belongs to the
  caller, this module only measures the IS/OOS gap and the consistency of the
  out-of-sample result.

The metrics layer (``trading_backtest.metrics``) is imported **lazily** inside
:func:`_resolve_metric`: every scoring entry point accepts an explicit
``metric_fn`` override, which keeps this module usable (and testable) without
that package.
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd

from trading_backtest.core.constants import OHLCV_INDEX_NAME, UTC
from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.core.models import BacktestResult, RunnerFn
from trading_backtest.data.validation import ensure_ohlcv
from trading_backtest.validation.split import Window, _check_mode, make_windows

__all__ = [
    "CONSISTENCY_THRESHOLD",
    "WalkForwardResult",
    "WalkForwardWindowResult",
    "walk_forward",
]

#: Share of windows whose out-of-sample metric must be positive for the run to
#: be flagged as ``consistent``.
CONSISTENCY_THRESHOLD = 0.6

#: A scoring function: ``BacktestResult -> float``.
MetricFn = Callable[[BacktestResult], float]

#: How many parameters ``metric_value`` is expected to take when inspected.
_NAME_FIRST = "name_first"
_RESULT_FIRST = "result_first"
_FACTORY = "factory"


# ---------------------------------------------------------------------------
# metric resolution (lazy import of the metrics layer)
# ---------------------------------------------------------------------------


def _metric_call_order(metric_value: Callable[..., Any]) -> str:
    """Return how ``metric_value`` expects its arguments.

    ``trading_backtest.metrics`` is written by another package, and its
    ``metric_value`` helper may be declared as ``metric_value(result, name)``,
    as ``metric_value(name, result)`` or as a one-argument factory returning a
    ``Callable[[BacktestResult], float]``.  The signature is therefore inspected
    once, at metric-resolution time, instead of assuming one call style.
    """
    try:
        parameters = list(inspect.signature(metric_value).parameters)
    except (TypeError, ValueError):  # builtins / C callables
        return _RESULT_FIRST
    if len(parameters) == 1:
        return _FACTORY
    first = parameters[0].lower()
    second = parameters[1].lower()
    if "result" in first or "run" in first or "backtest" in first:
        return _RESULT_FIRST
    if "result" in second or "run" in second or "backtest" in second:
        return _NAME_FIRST
    if first in {"name", "metric", "metric_name", "key", "metric_key"}:
        return _NAME_FIRST
    return _RESULT_FIRST


def _resolve_metric(metric: str, metric_fn: MetricFn | None) -> MetricFn:
    """Return the scoring function to use, importing the metrics layer lazily.

    ``metric_fn`` always wins.  Otherwise ``METRIC_NAMES`` / ``metric_value``
    are imported from :mod:`trading_backtest.metrics` *inside this function* so
    that the validation layer stays importable while that module is unavailable.

    Raises
    ------
    ValidationLayerError
        If ``metric`` is not a known metric name, or if the metrics layer cannot
        be imported.
    """
    if metric_fn is not None:
        return metric_fn

    try:  # lazy on purpose: the metrics layer is a downstream package
        from trading_backtest.metrics import METRIC_NAMES, metric_value
    except ImportError as exc:
        raise ValidationLayerError(
            f"cannot resolve metric {metric!r}: the metrics layer "
            f"(trading_backtest.metrics) is not importable ({exc})"
        ) from exc

    available = tuple(sorted(str(name) for name in METRIC_NAMES))
    if metric not in available:
        raise ValidationLayerError(
            f"unknown metric: {metric!r} (available: {', '.join(available)})"
        )

    # ``metric_value`` belongs to another package: it is dispatched dynamically
    # (see :func:`_metric_call_order`) so that its exact signature is not part of
    # this layer's contract.
    dynamic_metric_value: Callable[..., Any] = metric_value
    order = _metric_call_order(dynamic_metric_value)
    if order == _NAME_FIRST:

        def _scorer(result: BacktestResult) -> float:
            return float(dynamic_metric_value(metric, result))
    elif order == _FACTORY:
        factory: Callable[..., Any] = dynamic_metric_value(metric)

        def _scorer(result: BacktestResult) -> float:
            return float(factory(result))
    else:

        def _scorer(result: BacktestResult) -> float:
            return float(dynamic_metric_value(result, metric))

    return _scorer


def _score_metric(metric_fn: MetricFn, result: BacktestResult) -> float:
    """Score ``result``, treating a trade-less result as non-contributing (0.0).

    A window that produced no trade carries no performance evidence at all;
    scoring functions built on trade statistics legitimately raise in that case.
    Rather than failing the whole walk-forward run, such a window contributes
    ``0.0`` — which is also what a metrics layer computing "no trades -> 0.0"
    would return.  Every other scoring error propagates unchanged.
    """
    try:
        return float(metric_fn(result))
    except Exception:
        if not result.is_empty:
            raise
        return 0.0


def _call_runner(
    runner: RunnerFn,
    data: pd.DataFrame,
    params: Mapping[str, Any] | None,
    *,
    context: str,
) -> BacktestResult:
    """Call ``runner`` and normalise its failures.

    Any exception raised by the runner is wrapped in
    :class:`~trading_backtest.core.errors.ValidationLayerError` with the
    original exception as ``__cause__``; a runner returning something else than
    a :class:`BacktestResult` is reported the same way.
    """
    try:
        result = runner(data, params)
    except Exception as exc:
        raise ValidationLayerError(f"runner failed on {context}: {exc}") from exc
    if not isinstance(result, BacktestResult):
        raise ValidationLayerError(
            f"runner returned {type(result).__name__} on {context}, expected BacktestResult"
        )
    return result


# ---------------------------------------------------------------------------
# slicing and equity stitching
# ---------------------------------------------------------------------------


def _slice_window(frame: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Return the rows of ``frame`` between two inclusive timestamps.

    Row positions are looked up with ``searchsorted`` so the slice is exactly
    the contiguous row range delimited by the two timestamps, whatever the
    index resolution.
    """
    index = pd.DatetimeIndex(frame.index)
    left = int(index.searchsorted(pd.Timestamp(start), side="left"))
    right = int(index.searchsorted(pd.Timestamp(end), side="right"))
    return frame.iloc[left:right]


def _extract_equity(result: BacktestResult, *, balance: float) -> pd.Series:
    """Return ``result``'s equity curve rescaled to start from ``balance``.

    The scaling factor is ``balance / result.initial_balance``: the shape of the
    curve is untouched, only its level is rebased onto the running balance of
    the stitched curve.
    """
    curve = result.equity_curve.astype("float64")
    initial = float(result.initial_balance)
    if initial == 0.0:  # pragma: no cover - a backtest always starts with a balance
        return curve
    return curve * (float(balance) / initial)


def _concat_equity(curves: Sequence[pd.Series]) -> pd.Series:
    """Concatenate equity curves, preserving duplicates and index order."""
    non_empty = [curve for curve in curves if len(curve)]
    if not non_empty:
        empty_index = pd.DatetimeIndex([], tz=UTC, name=OHLCV_INDEX_NAME)
        return pd.Series([], index=empty_index, name="equity", dtype="float64")
    stitched = pd.concat(non_empty)
    index = pd.DatetimeIndex(stitched.index)
    index = index.tz_localize(UTC) if index.tz is None else index.tz_convert(UTC)
    index.name = OHLCV_INDEX_NAME
    return pd.Series(
        stitched.to_numpy(dtype="float64"), index=index, name="equity", dtype="float64"
    )


def _rebase_curves(results: Sequence[BacktestResult], base: float) -> list[pd.Series]:
    """Return ``results``' equity curves rebased on ``base``, one curve each.

    The first curve is scaled by ``base / results[0].initial_balance`` and every
    following curve by ``running_balance / its own initial_balance``, where
    ``running_balance`` is the last point of the previous rebased curve.  This
    keeps the concatenation continuous and starting at ``base``.
    """
    curves: list[pd.Series] = []
    running = float(base)
    for result in results:
        curve = _extract_equity(result, balance=running)
        curves.append(curve)
        if len(curve):
            running = float(curve.iloc[-1])
    return curves


def _stitch_equity(results: Sequence[BacktestResult]) -> pd.Series:
    """Stitch the equity curves of ``results`` into one continuous curve.

    The stitched curve starts at the first result's initial balance and is
    rebased on the running balance at every window boundary (see
    :func:`_rebase_curves`).  The result is a plain concatenation: timestamps are
    ascending and duplicates are kept (no reindexing, no deduplication).
    """
    if not results:
        return _concat_equity([])
    return _concat_equity(_rebase_curves(results, float(results[0].initial_balance)))


def _aggregate(
    results: Sequence[BacktestResult],
    *,
    initial_balance: float | None = None,
) -> BacktestResult:
    """Concatenate ``results`` into a single synthetic :class:`BacktestResult`.

    Documented internal helper used to score a whole walk-forward run with a
    single metric call:

    * ``trades`` is the concatenation of every result's trades, in order;
    * ``equity_curve`` is the concatenation of every result's equity curve, each
      curve rebased on the running balance so that the stitched curve starts at
      ``initial_balance`` (the first result's own initial balance by default)
      and stays continuous across window boundaries;
    * ``final_balance`` is the last point of that stitched curve;
    * ``strategy_name`` / ``symbol`` / ``timeframe`` are inherited from the first
      result, ``params`` is empty and ``metadata`` marks the object as an
      aggregate (``{"aggregated": True, "n_results": ...}``).

    Raises
    ------
    ValidationLayerError
        If ``results`` is empty.
    """
    if not results:
        raise ValidationLayerError("cannot aggregate an empty sequence of BacktestResults")

    base = float(initial_balance if initial_balance is not None else results[0].initial_balance)
    trades = [trade for result in results for trade in result.trades]
    curves = _rebase_curves(results, base)
    equity = _concat_equity(curves)
    running = base
    for curve in curves:
        if len(curve):
            running = float(curve.iloc[-1])
    first = results[0]
    return BacktestResult(
        strategy_name=first.strategy_name,
        symbol=first.symbol,
        timeframe=first.timeframe,
        start=first.start,
        end=results[-1].end,
        initial_balance=base,
        final_balance=running,
        trades=trades,
        equity_curve=equity,
        params={},
        metadata={"aggregated": True, "n_results": len(results), "aggregate": "walk_forward"},
    )


# ---------------------------------------------------------------------------
# result models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkForwardWindowResult:
    """Metrics and raw results of a single walk-forward window."""

    window: Window
    is_result: BacktestResult
    oos_result: BacktestResult
    is_metric: float
    oos_metric: float
    n_oos_trades: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping (nested objects use ``to_dict``)."""
        return {
            "window": self.window.to_dict(),
            "is_result": self.is_result.to_dict(),
            "oos_result": self.oos_result.to_dict(),
            "is_metric": float(self.is_metric),
            "oos_metric": float(self.oos_metric),
            "n_oos_trades": int(self.n_oos_trades),
        }


@dataclass(frozen=True)
class WalkForwardResult:
    """Full outcome of a walk-forward analysis.

    ``aggregate_*_metric`` values are computed on the *concatenation* of every
    window (see :func:`_aggregate`): they answer "what would a single run over
    all out-of-sample data have produced?", while the per-window metrics answer
    "how stable is that performance over time?".
    """

    windows: list[WalkForwardWindowResult]
    mode: str
    in_sample_ratio: float
    metric_name: str
    stitched_oos_equity: pd.Series
    aggregate_oos_metric: float
    aggregate_is_metric: float
    efficiency: float
    is_consistent: bool
    n_windows: int
    n_oos_trades: int

    @property
    def mean_oos_metric(self) -> float:
        """Mean out-of-sample metric over the windows (0.0 when there is none)."""
        if not self.windows:
            return 0.0
        return float(sum(window.oos_metric for window in self.windows) / len(self.windows))

    @property
    def mean_is_metric(self) -> float:
        """Mean in-sample metric over the windows (0.0 when there is none)."""
        if not self.windows:
            return 0.0
        return float(sum(window.is_metric for window in self.windows) / len(self.windows))

    @property
    def consistency_ratio(self) -> float:
        """Share of windows whose out-of-sample metric is strictly positive."""
        if not self.windows:
            return 0.0
        positive = sum(1 for window in self.windows if window.oos_metric > 0.0)
        return float(positive / len(self.windows))

    def to_dict(self) -> dict[str, Any]:
        """Return a fully JSON-serialisable mapping of the analysis."""
        return {
            "windows": [window.to_dict() for window in self.windows],
            "mode": str(self.mode),
            "in_sample_ratio": float(self.in_sample_ratio),
            "metric_name": str(self.metric_name),
            "aggregate_oos_metric": float(self.aggregate_oos_metric),
            "aggregate_is_metric": float(self.aggregate_is_metric),
            "efficiency": float(self.efficiency),
            "is_consistent": bool(self.is_consistent),
            "consistency_ratio": float(self.consistency_ratio),
            "n_windows": int(self.n_windows),
            "n_oos_trades": int(self.n_oos_trades),
            "mean_oos_metric": float(self.mean_oos_metric),
            "mean_is_metric": float(self.mean_is_metric),
            "stitched_oos_equity": {
                "timestamps": [
                    pd.Timestamp(timestamp).isoformat()
                    for timestamp in self.stitched_oos_equity.index
                ],
                "values": [
                    float(value) for value in self.stitched_oos_equity.to_numpy(dtype="float64")
                ],
            },
        }


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def walk_forward(
    runner: RunnerFn,
    data: pd.DataFrame,
    *,
    metric: str = "sharpe_ratio",
    metric_fn: MetricFn | None = None,
    n_windows: int = 5,
    in_sample_ratio: float = 0.7,
    mode: str = "rolling",
    purge_candles: int = 0,
    initial_balance: float = 10_000.0,
) -> WalkForwardResult:
    """Run a walk-forward analysis of ``runner`` over ``data``.

    Parameters
    ----------
    runner:
        A :data:`~trading_backtest.core.models.RunnerFn` — ``runner(frame,
        params) -> BacktestResult``.  It is called as ``runner(slice, None)``
        (the strategy defaults are used: walk-forward does not optimise).
    data:
        Contract-conformant OHLCV frame.
    metric:
        Name of the metric to score the windows with (resolved eagerly, before
        any runner call).  Ignored when ``metric_fn`` is given.
    metric_fn:
        Explicit scoring function ``BacktestResult -> float``; overrides
        ``metric`` and removes any dependency on the metrics layer.
    n_windows:
        Number of walk-forward windows.
    in_sample_ratio:
        Share of each block used for the in-sample slice.
    mode:
        ``"rolling"`` or ``"anchored"``.
    purge_candles:
        Rows dropped between in-sample and out-of-sample.
    initial_balance:
        Base balance used to rebase the aggregate equity curves.

    Returns
    -------
    WalkForwardResult
        Per-window results plus the stitched out-of-sample equity curve, the
        aggregate IS/OOS metrics, ``efficiency``
        (``aggregate_oos / aggregate_is``) and ``is_consistent``
        (at least 60% of the windows show a positive out-of-sample metric).

    Raises
    ------
    ValidationLayerError
        If the metric cannot be resolved, if the window parameters are invalid,
        or if the runner fails (the original exception is kept as ``__cause__``).
    DataValidationError
        If ``data`` does not satisfy the OHLCV contract.
    InsufficientDataError
        If ``data`` cannot hold ``n_windows`` windows.
    """
    scoring = _resolve_metric(metric, metric_fn)
    window_mode = _check_mode(mode)

    frame = ensure_ohlcv(data, name="data")
    windows = make_windows(
        frame,
        n_windows=n_windows,
        in_sample_ratio=in_sample_ratio,
        mode=window_mode,
        purge_candles=purge_candles,
    )

    window_results: list[WalkForwardWindowResult] = []
    for window in windows:
        is_slice = _slice_window(frame, window.is_start, window.is_end)
        oos_slice = _slice_window(frame, window.oos_start, window.oos_end)
        is_result = _call_runner(runner, is_slice, None, context=f"window {window.index} in-sample")
        oos_result = _call_runner(
            runner, oos_slice, None, context=f"window {window.index} out-of-sample"
        )
        window_results.append(
            WalkForwardWindowResult(
                window=window,
                is_result=is_result,
                oos_result=oos_result,
                is_metric=_score_metric(scoring, is_result),
                oos_metric=_score_metric(scoring, oos_result),
                n_oos_trades=int(oos_result.n_trades),
            )
        )

    oos_curves = [window.oos_result for window in window_results]
    is_runs = [window.is_result for window in window_results]
    stitched = _stitch_equity(oos_curves)
    aggregate_oos_metric = _score_metric(
        scoring, _aggregate(oos_curves, initial_balance=initial_balance)
    )
    aggregate_is_metric = _score_metric(
        scoring, _aggregate(is_runs, initial_balance=initial_balance)
    )
    efficiency = (
        aggregate_oos_metric / aggregate_is_metric
        if math.isfinite(aggregate_is_metric) and aggregate_is_metric != 0.0
        else 0.0
    )
    n_oos_trades = int(sum(window.n_oos_trades for window in window_results))
    positive = sum(1 for window in window_results if window.oos_metric > 0.0)
    consistency = float(positive / len(window_results)) if window_results else 0.0

    return WalkForwardResult(
        windows=window_results,
        mode=str(mode),
        in_sample_ratio=float(in_sample_ratio),
        metric_name=str(metric),
        stitched_oos_equity=stitched,
        aggregate_oos_metric=float(aggregate_oos_metric),
        aggregate_is_metric=float(aggregate_is_metric),
        efficiency=float(efficiency),
        is_consistent=consistency >= CONSISTENCY_THRESHOLD,
        n_windows=len(window_results),
        n_oos_trades=n_oos_trades,
    )
