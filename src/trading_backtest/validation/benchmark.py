"""Benchmark gate: did the strategy actually beat doing nothing?

A backtest that ends with a positive ``total_return`` proves nothing on a rising
asset -- on BTC between 2023 and 2025 *any* long-only run looks profitable.  The
only honest question is whether the strategy beat buying the asset on the first
candle and holding it until the last one.  This module turns the passive
comparison produced by :mod:`trading_backtest.metrics.benchmark` into a single
boolean verdict, surfaced exactly like the other validation gates and never woven
into them:

===============  ==========================================================
gate             question
===============  ==========================================================
``is_robust``    does the edge survive a parameter sweep?
``is_consistent``does the edge survive out of sample?
``strategy_beats_benchmark``  does the edge survive *inaction*?
===============  ==========================================================

Two rules are deliberate:

* the verdict is a **strict** inequality -- ``alpha > MIN_ALPHA`` with
  :data:`MIN_ALPHA` = ``0.0`` -- so a tie is not a victory: a strategy that
  reproduces buy & hold exactly has merely paid fees and slippage for nothing;
* ``alpha`` is a **fraction**, in the very same unit as ``total_return``
  (``alpha * 100`` is an excess return in percentage points).

The metrics layer (``trading_backtest.metrics``) is imported **lazily** inside
:func:`_resolve_comparison_fn` and every entry point accepts an explicit
``comparison_fn`` override, so this module stays importable *and* testable while
that package is unavailable -- the same contract as
:func:`trading_backtest.validation.walk_forward.walk_forward`.  ``variant ==
"none"`` short-circuits before any import at all: a disabled benchmark never
needs the metrics layer.

Dependency direction: ``core`` / ``config`` / ``data`` -> ``validation``; the
metrics layer is referenced under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.core.models import BacktestResult

if TYPE_CHECKING:  # pragma: no cover - types only, keeps this module importable
    from trading_backtest.metrics.benchmark import BenchmarkComparison

__all__ = [
    "MIN_ALPHA",
    "BenchmarkGateResult",
    "ComparisonFn",
    "validate_benchmark",
]

#: Excess return (as a fraction of the initial capital) the strategy must beat
#: the benchmark by for the gate to pass.  ``0.0`` means "any strictly positive
#: margin", so a tie (``alpha == 0.0``) does **not** beat the benchmark.
MIN_ALPHA: float = 0.0

#: A comparison function: ``(result, data, **variant/fee_rate/slippage) ->
#: BenchmarkComparison | None``.  Mirrors :data:`~trading_backtest.validation.
#: walk_forward.MetricFn`: it is the injectable seam that keeps this module
#: usable without the metrics layer.
ComparisonFn = Callable[..., "BenchmarkComparison | None"]


def _finite_or_none(value: float | None) -> float | None:
    """Return ``value`` as a finite plain ``float``, or ``None``.

    ``beta``/``correlation`` are optional by nature (a flat ``cash`` benchmark
    carries no exposure to measure) and the reporting layers must be able to
    serialise them as JSON ``null``: a non-finite candidate is therefore
    normalised to ``None`` instead of leaking ``NaN``/``inf`` into a report.
    """
    if value is None:
        return None
    candidate = float(value)
    return candidate if math.isfinite(candidate) else None


def _resolve_comparison_fn(comparison_fn: ComparisonFn | None, variant: str) -> ComparisonFn:
    """Return the comparison function to use, importing the metrics layer lazily.

    ``comparison_fn`` always wins.  Otherwise ``compare_benchmark`` is imported
    from :mod:`trading_backtest.metrics.benchmark` *inside this function*, so the
    validation layer stays importable while that module is unavailable.

    Raises
    ------
    ValidationLayerError
        If the metrics layer cannot be imported.
    """
    if comparison_fn is not None:
        return comparison_fn

    try:  # lazy on purpose: the metrics layer is a downstream package
        from trading_backtest.metrics.benchmark import compare_benchmark
    except ImportError as exc:
        raise ValidationLayerError(
            f"cannot compare against the {variant!r} benchmark: the metrics layer "
            f"(trading_backtest.metrics) is not importable ({exc})"
        ) from exc
    return compare_benchmark


@dataclass(frozen=True)
class BenchmarkGateResult:
    """Verdict of the benchmark gate: strategy vs buy & hold, side by side.

    The scalars are copied out of the :class:`BenchmarkComparison` so that the
    gate can be reported, asserted and serialised on its own; the full
    comparison (metrics of both sides, three flat rows, equity curves) stays
    available under :attr:`comparison` for the markdown/JSON writers.

    ``alpha`` is ``strategy_total_return - benchmark_total_return``, a fraction
    like both of them; ``beta``/``correlation`` are ``None`` whenever they cannot
    be measured and are never ``NaN``/``inf``.
    """

    variant: str
    comparison: BenchmarkComparison
    alpha: float
    beta: float | None
    correlation: float | None
    min_alpha: float
    initial_balance: float
    fee_rate: float
    slippage: float
    n_returns: int
    strategy_total_return: float
    benchmark_total_return: float
    strategy_beats_benchmark: bool

    def to_dict(self) -> dict[str, Any]:
        """Return the flat, fully JSON-serialisable payload of the gate.

        The payload is deliberately scalar-only (no equity curve, no nested
        metric mapping): the side-by-side rows are produced by the comparison
        itself (``comparison.comparison_rows()``) and never duplicated here.
        """
        return {
            "variant": str(self.variant),
            "strategy_beats_benchmark": bool(self.strategy_beats_benchmark),
            "alpha": float(self.alpha),
            "beta": _finite_or_none(self.beta),
            "correlation": _finite_or_none(self.correlation),
            "min_alpha": float(self.min_alpha),
            "n_returns": int(self.n_returns),
            "initial_balance": float(self.initial_balance),
            "fee_rate": float(self.fee_rate),
            "slippage": float(self.slippage),
            "strategy_total_return": float(self.strategy_total_return),
            "benchmark_total_return": float(self.benchmark_total_return),
        }


def validate_benchmark(
    result: BacktestResult,
    data: pd.DataFrame,
    *,
    variant: str = "buy_and_hold",
    fee_rate: float = 0.0,
    slippage: float = 0.0,
    min_alpha: float = MIN_ALPHA,
    comparison_fn: ComparisonFn | None = None,
) -> BenchmarkGateResult | None:
    """Compare ``result`` with its ``variant`` benchmark and gate the verdict.

    The benchmark is built on the very same window, the same initial capital and
    with the same ``fee_rate``/``slippage`` as the backtest, so ``alpha`` is a
    fair excess return: ``alpha`` is a **fraction** (``alpha * 100`` is an excess
    return in percentage points) and a negative alpha means the strategy
    destroyed value against doing nothing.

    Parameters
    ----------
    result:
        Completed backtest run.
    data:
        OHLCV frame the run was produced from (same window).
    variant:
        ``"buy_and_hold"`` (default, i.e.
        :data:`trading_backtest.metrics.benchmark.DEFAULT_BENCHMARK_VARIANT`),
        ``"cash"`` or ``"none"`` -- the literal is inlined on purpose so that
        this module never imports the metrics layer at import time.
    fee_rate, slippage:
        Costs applied to the benchmark side, identical to the backtest's.
    min_alpha:
        Minimum excess return the strategy must beat the benchmark by.
        Defaults to :data:`MIN_ALPHA` (``0.0``).
    comparison_fn:
        Injectable comparison function; ``None`` means "use
        :func:`trading_backtest.metrics.benchmark.compare_benchmark`".  It is
        called as ``comparison_fn(result, data, variant=..., fee_rate=...,
        slippage=...)``.

    Returns
    -------
    BenchmarkGateResult | None
        ``None`` if and only if ``variant == "none"`` -- or if the comparison
        itself yields no benchmark (a ``comparison_fn`` returning ``None``).
        The short-circuit happens *before* any import, so a disabled benchmark
        never needs the metrics layer.

    Notes
    -----
    ``strategy_beats_benchmark`` is ``bool(alpha > min_alpha)``: the comparison
    is **strictly** greater, so with the default ``min_alpha = 0.0`` a tie
    (``alpha == 0.0``) does **not** beat the benchmark -- reproducing buy & hold
    exactly, fees and slippage included, is not an edge.

    Raises
    ------
    ValidationLayerError
        If ``comparison_fn`` is ``None`` and the metrics layer is not importable.
    """
    if variant == "none":
        return None

    compare = _resolve_comparison_fn(comparison_fn, variant)
    comparison = compare(result, data, variant=variant, fee_rate=fee_rate, slippage=slippage)
    if comparison is None:
        return None

    alpha = float(comparison.alpha)
    return BenchmarkGateResult(
        variant=str(comparison.variant),
        comparison=comparison,
        alpha=alpha,
        beta=_finite_or_none(comparison.beta),
        correlation=_finite_or_none(comparison.correlation),
        min_alpha=float(min_alpha),
        initial_balance=float(comparison.initial_balance),
        fee_rate=float(comparison.fee_rate),
        slippage=float(comparison.slippage),
        n_returns=int(comparison.n_returns),
        strategy_total_return=float(comparison.strategy["total_return"]),
        benchmark_total_return=float(comparison.benchmark["total_return"]),
        strategy_beats_benchmark=bool(alpha > min_alpha),
    )
