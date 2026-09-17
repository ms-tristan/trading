"""Parametric robustness analysis: does the edge survive small parameter moves?

A backtest result is only informative if it is *stable*: a strategy whose Sharpe
ratio collapses as soon as one parameter moves by one tick is curve-fitted to
its own history, not robust.

This module sweeps a parameter grid mechanically:

* the grid is expanded deterministically (keys sorted alphabetically, values in
  their declared order) so two runs always evaluate the same points in the same
  order;
* **every** point is evaluated with the *full* merged mapping
  (``{**base_params, **point}``) — never a partial override — so the runner
  never has to guess what "unset" means;
* a reference point is always evaluated with ``base_params`` alone, giving
  ``base_metric`` used for the stability ratios;
* the summary statistics are plain population statistics, documented to the
  last digit (see :class:`RobustnessResult`), so a report can quote them.

The runner is always injected and the metric function may be overridden: this
module never imports the engine nor the metrics layer.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.core.models import BacktestResult, RunnerFn
from trading_backtest.data.validation import ensure_ohlcv
from trading_backtest.validation.walk_forward import (
    MetricFn,
    _call_runner,
    _resolve_metric,
    _score_metric,
)

__all__ = [
    "DEFAULT_MAX_COMBINATIONS",
    "RobustnessPoint",
    "RobustnessResult",
    "expand_grid",
    "grid_size",
    "parameter_sweep",
]

#: Default ceiling on the number of evaluated parameter combinations.
DEFAULT_MAX_COMBINATIONS = 512

#: Share of points that must be positive for a grid to be "robust".
POSITIVE_RATIO_THRESHOLD = 0.7

#: Share of points that must stay above half of ``base_metric``.
ROBUST_RATIO_THRESHOLD = 0.5

#: Number of points returned by :meth:`RobustnessResult.best` by default.
DEFAULT_BEST_COUNT = 5


@dataclass(frozen=True)
class RobustnessPoint:
    """One evaluation of the parameter grid."""

    params: dict[str, float | int]
    metric: float
    n_trades: int
    total_return: float

    @property
    def key(self) -> str:
        """Canonical, deterministic identifier of the point: ``"a=1,b=2"``."""
        return ",".join(f"{name}={value}" for name, value in sorted(self.params.items()))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "params": dict(self.params),
            "metric": float(self.metric),
            "n_trades": int(self.n_trades),
            "total_return": float(self.total_return),
        }


@dataclass(frozen=True)
class RobustnessResult:
    """Summary of a parameter sweep.

    The derived fields are computed by :func:`parameter_sweep` with the
    following exact definitions:

    ``metric_mean`` / ``metric_std``
        mean and **population** standard deviation (``ddof=0``) of the point
        metrics; ``metric_std`` is ``0.0`` when fewer than two points exist;
    ``stability``
        ``metric_mean / metric_std``, or ``0.0`` when ``metric_std`` is 0;
    ``positive_ratio``
        share of points with ``metric > 0``;
    ``robust_ratio``
        share of points with ``metric >= 0.5 * base_metric`` when
        ``base_metric > 0``, otherwise share of points with
        ``metric >= base_metric``;
    ``worst_case_return``
        smallest ``total_return`` over the grid;
    ``is_robust``
        ``positive_ratio >= 0.7`` **and** ``robust_ratio >= 0.5``.
    """

    metric_name: str
    base_params: dict[str, float | int]
    base_metric: float
    points: list[RobustnessPoint]
    n_points: int = 0
    metric_mean: float = 0.0
    metric_std: float = 0.0
    metric_min: float = 0.0
    metric_max: float = 0.0
    stability: float = 0.0
    positive_ratio: float = 0.0
    robust_ratio: float = 0.0
    worst_case_return: float = 0.0
    best_params: dict[str, float | int] = field(default_factory=dict)
    is_robust: bool = False

    @property
    def metric_range(self) -> float:
        """Spread between the best and the worst metric of the grid."""
        return float(self.metric_max - self.metric_min)

    def best(self, n: int = DEFAULT_BEST_COUNT) -> list[RobustnessPoint]:
        """Return the ``n`` best points, descending by metric.

        Ties are broken by :attr:`RobustnessPoint.key` ascending, which makes the
        ordering fully deterministic.  ``n <= 0`` returns an empty list.
        """
        if n <= 0:
            return []
        return sorted(self.points, key=lambda point: (-point.metric, point.key))[:n]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping with one key per field."""
        return {
            "metric_name": str(self.metric_name),
            "base_params": dict(self.base_params),
            "base_metric": float(self.base_metric),
            "points": [point.to_dict() for point in self.points],
            "n_points": int(self.n_points),
            "metric_mean": float(self.metric_mean),
            "metric_std": float(self.metric_std),
            "metric_min": float(self.metric_min),
            "metric_max": float(self.metric_max),
            "stability": float(self.stability),
            "positive_ratio": float(self.positive_ratio),
            "robust_ratio": float(self.robust_ratio),
            "worst_case_return": float(self.worst_case_return),
            "best_params": dict(self.best_params),
            "is_robust": bool(self.is_robust),
        }


# ---------------------------------------------------------------------------
# grid helpers
# ---------------------------------------------------------------------------


def expand_grid(grid: Mapping[str, Sequence[float | int]]) -> list[dict[str, float | int]]:
    """Expand ``grid`` into the list of parameter combinations to evaluate.

    Keys are sorted alphabetically and each key keeps the order of its declared
    values, so the returned list is deterministic::

        >>> expand_grid({"a": [1, 2], "b": [3]})
        [{'a': 1, 'b': 3}, {'a': 2, 'b': 3}]

    An empty grid (or a grid holding an empty value list) yields ``[]``.
    """
    if not grid:
        return []
    names = sorted(grid)
    return [
        dict(zip(names, values, strict=True))
        for values in itertools.product(*(grid[name] for name in names))
    ]


def grid_size(grid: Mapping[str, Sequence[float | int]]) -> int:
    """Return the number of combinations ``grid`` expands to (0 when empty)."""
    if not grid:
        return 0
    return int(math.prod(len(values) for values in grid.values()))


def _total_return(result: BacktestResult, fallback_initial: float) -> float:
    """Return ``final_balance / initial_balance - 1``.

    The balance of the result itself is authoritative; ``fallback_initial`` (the
    sweep's ``initial_balance``) is only used when a result carries no positive
    balance at all, and ``0.0`` is returned when neither is usable.
    """
    initial = float(result.initial_balance)
    if initial <= 0.0:
        initial = float(fallback_initial)
    if initial <= 0.0:  # pragma: no cover - requires a non-positive fallback too
        return 0.0
    return float(result.final_balance) / initial - 1.0


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def parameter_sweep(
    runner: RunnerFn,
    data: pd.DataFrame,
    grid: Mapping[str, Sequence[float | int]],
    *,
    base_params: Mapping[str, Any] | None = None,
    metric: str = "sharpe_ratio",
    metric_fn: MetricFn | None = None,
    max_combinations: int = DEFAULT_MAX_COMBINATIONS,
    initial_balance: float = 10_000.0,
) -> RobustnessResult:
    """Evaluate every combination of ``grid`` and summarise its stability.

    Parameters
    ----------
    runner:
        A :data:`~trading_backtest.core.models.RunnerFn`.  It is called once with
        ``base_params or None`` and then once per grid point with the **full
        merged mapping** ``{**base_params, **point}``.
    data:
        Contract-conformant OHLCV frame, validated once and reused for every
        evaluation.
    grid:
        Mapping ``parameter -> values`` to sweep.  Every parameter of the grid
        is set explicitly on every call.
    base_params:
        Reference parameters, evaluated as-is to provide ``base_metric`` and
        merged into every grid point.
    metric:
        Metric name, resolved eagerly (before any runner call) when ``metric_fn``
        is not given.
    metric_fn:
        Explicit scoring function ``BacktestResult -> float``.
    max_combinations:
        Safety ceiling: a grid expanding to more combinations is refused instead
        of silently running for hours.
    initial_balance:
        Base balance recorded on the summary (the runner keeps ownership of the
        balance it actually used).

    Returns
    -------
    RobustnessResult

    Raises
    ------
    ValidationLayerError
        If the grid is empty, if it is larger than ``max_combinations``, if the
        metric cannot be resolved, or if the runner fails (the original exception
        is kept as ``__cause__``).
    DataValidationError
        If ``data`` does not satisfy the OHLCV contract.
    """
    if not grid:
        raise ValidationLayerError(
            "cannot sweep an empty parameter grid: at least one parameter must be varied"
        )
    size = grid_size(grid)
    if size < 1:
        raise ValidationLayerError(
            "cannot sweep a parameter grid that expands to 0 combinations: "
            "every parameter needs at least one value"
        )
    if size > max_combinations:
        raise ValidationLayerError(
            f"parameter grid has {size} combinations, which exceeds "
            f"max_combinations={max_combinations}"
        )

    scoring = _resolve_metric(metric, metric_fn)
    frame = ensure_ohlcv(data, name="data")
    base: dict[str, Any] = dict(base_params) if base_params else {}

    base_result = _call_runner(runner, frame, dict(base) or None, context="base parameters")
    base_metric = _score_metric(scoring, base_result)

    points: list[RobustnessPoint] = []
    for combination in expand_grid(grid):
        merged: dict[str, Any] = {**base, **combination}
        context = ", ".join(f"{name}={value}" for name, value in combination.items())
        result = _call_runner(runner, frame, merged, context=f"parameters {context}")
        points.append(
            RobustnessPoint(
                params=dict(merged),
                metric=_score_metric(scoring, result),
                n_trades=int(result.n_trades),
                total_return=_total_return(result, initial_balance),
            )
        )

    metrics = np.asarray([point.metric for point in points], dtype="float64")
    n_points = len(points)
    metric_mean = float(metrics.mean())
    metric_std = float(metrics.std()) if n_points >= 2 else 0.0
    metric_min = float(metrics.min())
    metric_max = float(metrics.max())
    positive_ratio = float(np.count_nonzero(metrics > 0.0) / n_points)
    threshold = 0.5 * base_metric if base_metric > 0.0 else base_metric
    robust_ratio = float(np.count_nonzero(metrics >= threshold) / n_points)
    ordered = sorted(points, key=lambda point: (-point.metric, point.key))

    return RobustnessResult(
        metric_name=str(metric),
        base_params=dict(base),
        base_metric=float(base_metric),
        points=points,
        n_points=n_points,
        metric_mean=metric_mean,
        metric_std=metric_std,
        metric_min=metric_min,
        metric_max=metric_max,
        stability=metric_mean / metric_std if metric_std > 0.0 else 0.0,
        positive_ratio=positive_ratio,
        robust_ratio=robust_ratio,
        worst_case_return=float(min(point.total_return for point in points)),
        best_params=dict(ordered[0].params),
        is_robust=bool(
            positive_ratio >= POSITIVE_RATIO_THRESHOLD and robust_ratio >= ROBUST_RATIO_THRESHOLD
        ),
    )
