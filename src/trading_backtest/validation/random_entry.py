"""Random-entry gate: was the strategy skilled, or merely lucky?

Beating *buy & hold* proves the strategy timed **something**; it does not prove
the strategy has an **edge** -- a rising asset makes almost any long-only run
look clever, and the benchmark gate
(:mod:`trading_backtest.validation.benchmark`) answers that weaker question.  The
sharp question is the one a coin flip answers too: *if the same number of trades
had been entered at random, how often would chance alone have done as well?*

This module turns the random-entry distribution produced by
:mod:`trading_backtest.metrics.random_entry` into a single boolean verdict,
surfaced exactly like the other validation gates and never woven into them:

===============  ==========================================================
gate             question
===============  ==========================================================
``is_robust``    does the edge survive a parameter sweep?
``is_consistent``does the edge survive out of sample?
``strategy_beats_benchmark``  does the edge survive *inaction*?
``strategy_beats_random``     does the edge survive *luck*?
===============  ==========================================================

How to read the two numbers (the reading is the whole point of the gate):

* ``percentile`` in ``[0, 100]`` -- the share of simulations the strategy
  **strictly beat**.  ``50`` means "no better than a coin flip", ``95`` means "a
  real signal";
* ``p_value`` in ``[0, 1]`` -- the share of simulations doing **as well or
  better** (ties count *against* the strategy, so the test is conservative);
* ``strategy_beats_random`` is the **strict** verdict ``p_value < min_p_value``
  with :data:`MIN_P_VALUE` = ``0.05``: the strategy must beat at least 95 % of
  the random entries, so ``p_value == 0.05`` does **not** pass.  A strategy at
  the 50th percentile, or one of a hundred lucky draws, fails.

The metrics layer (``trading_backtest.metrics``) is imported **lazily** inside
:func:`_resolve_entry_fn` and the entry point accepts an explicit ``entry_fn``
override, so this module stays importable *and* testable while that package is
unavailable -- the very same contract as
:func:`trading_backtest.validation.benchmark.validate_benchmark`.  ``risk_free_rate``
is deliberately **not** a parameter of this gate: the comparison is made on
``total_return``, which is risk-free-rate free by construction
(``final / initial - 1``), so a riskless rate could not change a single
simulation -- it belongs to the Sharpe/Sortino metrics, not to this test.

Dependency direction: ``core`` / ``config`` / ``data`` -> ``validation``; the
metrics layer is referenced under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.core.models import BacktestResult

if TYPE_CHECKING:  # pragma: no cover - types only, keeps this module importable
    from trading_backtest.metrics.random_entry import RandomEntryResult

__all__ = [
    "MIN_P_VALUE",
    "RandomEntryFn",
    "RandomEntryGateResult",
    "validate_random_entry",
]

#: Empirical p-value the strategy must stay **strictly** below for the gate to
#: pass: the random entries must beat the strategy in less than 5 % of the
#: simulations, i.e. the strategy must beat at least 95 % of them.  The
#: inequality is strict on purpose -- ``p_value == 0.05`` does **not** pass --
#: exactly like ``alpha > MIN_ALPHA`` in the benchmark gate.  This constant is
#: the single source of truth for the threshold, gate-side; the metrics layer
#: deliberately holds none.
MIN_P_VALUE: float = 0.05

#: A random-entry function: ``(result, data, **n_simulations/random_seed/
#: fee_rate/slippage) -> RandomEntryResult``.  Mirrors
#: :data:`trading_backtest.validation.benchmark.ComparisonFn`: it is the
#: injectable seam that keeps this module usable without the metrics layer.
RandomEntryFn = Callable[..., "RandomEntryResult"]


def _resolve_entry_fn(entry_fn: RandomEntryFn | None, variant: str) -> RandomEntryFn:
    """Return the random-entry function to use, importing the metrics layer lazily.

    ``entry_fn`` always wins.  Otherwise ``random_entry_benchmark`` is imported
    from :mod:`trading_backtest.metrics.random_entry` *inside this function*, so
    the validation layer stays importable while that module is unavailable.

    Raises
    ------
    ValidationLayerError
        If the metrics layer cannot be imported.
    """
    if entry_fn is not None:
        return entry_fn

    try:  # lazy on purpose: the metrics layer is a downstream package
        from trading_backtest.metrics.random_entry import random_entry_benchmark
    except ImportError as exc:
        raise ValidationLayerError(
            f"cannot run the {variant!r} benchmark: the metrics layer "
            f"(trading_backtest.metrics) is not importable ({exc})"
        ) from exc
    return random_entry_benchmark


@dataclass(frozen=True)
class RandomEntryGateResult:
    """Verdict of the random-entry gate: the strategy inside its luck distribution.

    The scalars are copied out of the
    :class:`~trading_backtest.metrics.random_entry.RandomEntryResult` so that the
    gate can be reported, asserted and serialised on its own; the full
    distribution -- including the per-simulation ``returns`` and
    ``final_balances`` -- stays available under :attr:`distribution`.

    ``p_value`` is the share of simulations doing **as well or better** than the
    strategy, ties included, so it is conservative; ``percentile`` is the share it
    **strictly beat**.  ``strategy_beats_random`` is ``bool(p_value <
    min_p_value)``, a strict inequality: ``p_value == min_p_value`` is not a
    victory.

    There is no ``risk_free_rate`` here, by design: this comparison is made on
    ``total_return`` values, which do not involve a riskless rate at all.
    """

    n_simulations: int
    random_seed: int
    n_trades: int
    holding_periods: int
    exposure: float
    initial_balance: float
    timeframe: str
    strategy_total_return: float
    mean_return: float
    median_return: float
    std_return: float
    percentiles: dict[str, float]
    percentile: float
    p_value: float
    min_p_value: float
    strategy_beats_random: bool
    distribution: RandomEntryResult

    def to_dict(self) -> dict[str, Any]:
        """Return the fully JSON-serialisable payload of the gate.

        Unlike the frozen 12-key payload of
        :meth:`trading_backtest.validation.benchmark.BenchmarkGateResult.to_dict`,
        this is a **new** payload and it carries the whole distribution under
        ``"distribution"`` (per-simulation lists included), because the brief
        requires the distribution to reach the JSON report as well as the
        markdown one.  The scalars are duplicated at the top level so a consumer
        that only wants the verdict never has to walk into the nested mapping.
        """
        return {
            "n_simulations": int(self.n_simulations),
            "random_seed": int(self.random_seed),
            "n_trades": int(self.n_trades),
            "holding_periods": int(self.holding_periods),
            "exposure": float(self.exposure),
            "initial_balance": float(self.initial_balance),
            "timeframe": str(self.timeframe),
            "strategy_total_return": float(self.strategy_total_return),
            "mean_return": float(self.mean_return),
            "median_return": float(self.median_return),
            "std_return": float(self.std_return),
            "percentile": float(self.percentile),
            "p_value": float(self.p_value),
            "min_p_value": float(self.min_p_value),
            "strategy_beats_random": bool(self.strategy_beats_random),
            "percentiles": {key: float(value) for key, value in self.percentiles.items()},
            "distribution": self.distribution.to_dict(),
        }


def validate_random_entry(
    result: BacktestResult,
    data: pd.DataFrame,
    *,
    n_simulations: int = 1000,
    random_seed: int = 42,
    fee_rate: float = 0.0,
    slippage: float = 0.0,
    min_p_value: float = MIN_P_VALUE,
    entry_fn: RandomEntryFn | None = None,
) -> RandomEntryGateResult:
    """Benchmark ``result`` against random entries and gate the verdict.

    ``n_simulations`` random-entry strategies are built on the very same window,
    with the same number of trades, the same holding period and the same
    ``fee_rate``/``slippage`` as the backtest; their total returns form the
    distribution the strategy is situated in.

    Parameters
    ----------
    result:
        Completed backtest run.
    data:
        OHLCV frame the run was produced from (same window).
    n_simulations:
        Number of random-entry simulations (default ``1000``, mirroring
        :data:`trading_backtest.metrics.random_entry.DEFAULT_N_SIMULATIONS`; the
        literal is inlined so that this module never imports the metrics layer at
        import time).
    random_seed:
        Seed of the simulation generator (default ``42``, mirroring
        :data:`trading_backtest.metrics.random_entry.DEFAULT_RANDOM_SEED`): the
        same seed always yields the same distribution.
    fee_rate, slippage:
        Costs charged on both legs of every simulated trade, identical to the
        backtest's.
    min_p_value:
        Maximum p-value the strategy may have and still beat randomness.
        Defaults to :data:`MIN_P_VALUE` (``0.05``).
    entry_fn:
        Injectable random-entry function; ``None`` means "use
        :func:`trading_backtest.metrics.random_entry.random_entry_benchmark`".
        It is called as ``entry_fn(result, data, n_simulations=..., random_seed=...,
        fee_rate=..., slippage=...)``.

    Returns
    -------
    RandomEntryGateResult
        ``strategy_beats_random`` is ``bool(p_value < min_p_value)``; the whole
        distribution is reachable under ``distribution``.

    Notes
    -----
    A strategy whose p-value sits exactly at the threshold does **not** pass: the
    inequality is strict, like ``alpha > MIN_ALPHA`` in the benchmark gate.  The
    test is conservative in the other direction too -- simulations doing exactly
    as well as the strategy count *against* it.

    There is no ``risk_free_rate`` parameter: the ranked quantity is
    ``total_return``, which never involves a riskless rate.

    Raises
    ------
    ValidationLayerError
        If ``entry_fn`` is ``None`` and the metrics layer is not importable.
    MetricsError
        Propagated untouched from the metrics layer (bad frame, out-of-range
        simulation count or seed, ...).
    """
    variant = "random_entry"  # inlined: never import the metrics layer at import time
    entry = _resolve_entry_fn(entry_fn, variant)
    distribution = entry(
        result,
        data,
        n_simulations=n_simulations,
        random_seed=random_seed,
        fee_rate=fee_rate,
        slippage=slippage,
    )

    p_value = float(distribution.p_value)
    return RandomEntryGateResult(
        n_simulations=int(distribution.n_simulations),
        random_seed=int(distribution.random_seed),
        n_trades=int(distribution.n_trades),
        holding_periods=int(distribution.holding_periods),
        exposure=float(distribution.exposure),
        initial_balance=float(distribution.initial_balance),
        timeframe=str(distribution.timeframe),
        strategy_total_return=float(distribution.strategy_total_return),
        mean_return=float(distribution.mean_return),
        median_return=float(distribution.median_return),
        std_return=float(distribution.std_return),
        percentiles={key: float(value) for key, value in distribution.percentiles.items()},
        percentile=float(distribution.percentile),
        p_value=p_value,
        min_p_value=float(min_p_value),
        strategy_beats_random=bool(p_value < min_p_value),
        distribution=distribution,
    )
