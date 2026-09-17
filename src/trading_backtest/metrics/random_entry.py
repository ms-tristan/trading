"""Random-entry benchmark: the skill test -- is the strategy better than luck?

Why it matters
--------------
Beating *buy & hold* proves the strategy timed something; it does not prove the
strategy has an edge.  The sharpest question is the one a coin-flip answers too:

    "if I had entered the market **at random**, with exactly the same number of
    trades, held exactly as long and been exposed exactly as much, how often
    would chance alone have done as well or better?"

This module answers it.  It builds ``n_simulations`` *random-entry* strategies on
the very same OHLCV window, measures the distribution of their total returns and
locates the real strategy inside it:

* ``percentile`` -- the share (in ``0..100``) of simulations the real strategy
  **strictly beat**.  ``50`` means "a coin flip", ``95`` means "a real signal";
* ``p_value`` -- the share (in ``0..1``) of simulations that did **as well or
  better**, i.e. the empirical probability of observing such a return by chance
  alone.  Ties count *against* the strategy, so the p-value is conservative.

Nothing here decides anything: there is deliberately **no threshold and no
verdict**.  The ``strategy_beats_random`` boolean belongs to the validation layer,
exactly like ``strategy_beats_benchmark`` derives from ``alpha``.

Design rules
------------
* layering: this module imports ``core`` and its sibling metric modules only
  (``metrics.benchmark`` for the shared price frame, ``metrics.performance`` for
  the metric maths) -- never ``config``/``data``/``strategy``/``validation``/
  ``reporting``.  In particular it never calls ``run_backtest``: the random
  entries are simulated with a self-contained numpy loop over the ``close``
  column;
* no metric maths is duplicated: the ``exposure`` of the real run and the
  ``total_return`` of every simulation come from the frozen
  :func:`~trading_backtest.metrics.performance.compute_metrics` (each simulated
  leg is wrapped in a synthetic :class:`~trading_backtest.core.models.BacktestResult`);
* determinism: a single ``numpy.random.default_rng(random_seed)`` generator drives
  the whole run (one draw per simulation, in simulation order), so the same seed
  always yields the same distribution.  As in
  :mod:`trading_backtest.validation.monte_carlo`, determinism is guaranteed for a
  given numpy version;
* no two simulated positions ever overlap: entries are drawn *without
  replacement* among the disjoint windows of ``holding_periods`` candles, so the
  reported exposure is never double counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from trading_backtest.core.errors import MetricsError
from trading_backtest.core.models import BacktestResult
from trading_backtest.metrics.benchmark import _benchmark_prices
from trading_backtest.metrics.performance import compute_metrics

__all__ = [
    "DEFAULT_N_SIMULATIONS",
    "DEFAULT_RANDOM_SEED",
    "MAX_SIMULATIONS",
    "PERCENTILE_LABELS",
    "RANDOM_ENTRY_VARIANT",
    "SERIALISED_SIMULATIONS",
    "RandomEntryResult",
    "random_entry_benchmark",
]

#: Number of random-entry simulations run when the caller does not choose one.
DEFAULT_N_SIMULATIONS: int = 1000

#: Seed of the ``numpy.random.default_rng`` generator (deterministic by default).
DEFAULT_RANDOM_SEED: int = 42

#: Hard ceiling on ``n_simulations``: beyond this the run is a typo, not a study.
MAX_SIMULATIONS: int = 10_000

#: Percentile labels exposed by :attr:`RandomEntryResult.percentiles`.
PERCENTILE_LABELS: tuple[str, ...] = ("p05", "p25", "p50", "p75", "p95")

#: How many per-simulation entries :meth:`RandomEntryResult.to_dict` keeps.
#:
#: Mirrors :data:`trading_backtest.validation.monte_carlo.SERIALISED_SIMULATIONS`
#: so the two simulation-based reports stay the same size: the raw arrays are
#: capped in the serialised payload (they are an implementation detail, and
#: dumping 10 000 floats into a markdown report is unreadable), while every
#: scalar -- mean, median, std, percentiles, percentile, p-value -- is always
#: reported in full.
SERIALISED_SIMULATIONS: int = 1000

#: Canonical name of the variant, as it appears in configuration and reports.
RANDOM_ENTRY_VARIANT: str = "random_entry"


@dataclass(frozen=True)
class RandomEntryResult:
    """Outcome of a random-entry benchmark over one backtest result.

    Every per-simulation value is kept in full on the instance --
    ``returns`` and ``final_balances`` both have exactly ``n_simulations``
    entries -- while :meth:`to_dict` caps the raw arrays at
    :data:`SERIALISED_SIMULATIONS` entries, exactly like
    :class:`~trading_backtest.validation.monte_carlo.MonteCarloResult`, so a
    markdown report stays readable.  Every scalar (mean, median, std,
    percentiles, percentile, p-value) is always serialised in full.

    Reading guide:

    * ``percentile`` in ``[0, 100]``: ``50`` = no better than chance,
      ``95`` = a genuine edge;
    * ``p_value`` in ``[0, 1]``: the share of simulations doing **as well or
      better** than the strategy (ties count against the strategy);
    * ``percentile / 100 + p_value >= 1`` always holds, with equality as soon as
      the strategy's total return appears in the distribution (then the two split
      the simulations into "strictly worse" and "as good or better").
    """

    variant: str
    n_simulations: int
    random_seed: int
    n_trades: int
    holding_periods: int
    exposure: float
    n_periods: int
    initial_balance: float
    timeframe: str
    strategy_total_return: float
    returns: list[float]
    final_balances: list[float]
    mean_return: float
    median_return: float
    std_return: float
    percentiles: dict[str, float]
    percentile: float
    p_value: float

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of every statistic.

        The two per-simulation lists are emitted **in full** (unlike
        :class:`~trading_backtest.validation.monte_carlo.MonteCarloResult`, which
        truncates them), so a report can rebuild the whole distribution.
        """
        return {
            "variant": str(self.variant),
            "n_simulations": int(self.n_simulations),
            "random_seed": int(self.random_seed),
            "n_trades": int(self.n_trades),
            "holding_periods": int(self.holding_periods),
            "exposure": float(self.exposure),
            "n_periods": int(self.n_periods),
            "initial_balance": float(self.initial_balance),
            "timeframe": str(self.timeframe),
            "strategy_total_return": float(self.strategy_total_return),
            "returns": [float(value) for value in self.returns[:SERIALISED_SIMULATIONS]],
            "final_balances": [
                float(value) for value in self.final_balances[:SERIALISED_SIMULATIONS]
            ],
            "mean_return": float(self.mean_return),
            "median_return": float(self.median_return),
            "std_return": float(self.std_return),
            "percentiles": {key: float(value) for key, value in self.percentiles.items()},
            "percentile": float(self.percentile),
            "p_value": float(self.p_value),
        }

    def histogram(self, bins: int = 20) -> dict[str, list[float]]:
        """Return the histogram of simulated returns as ``{"edges", "counts"}``.

        Mirrors :meth:`~trading_backtest.validation.monte_carlo.MonteCarloResult.histogram`
        so the two distributions are plotted the same way.

        Raises
        ------
        MetricsError
            If ``bins < 1``.
        """
        if bins < 1:
            raise MetricsError(f"bins must be an integer >= 1, got {bins!r}")
        counts, edges = np.histogram(np.asarray(self.returns, dtype="float64"), bins=bins)
        return {
            "edges": [float(edge) for edge in edges],
            "counts": [float(count) for count in counts],
        }


def _simulation_entries(
    rng: np.random.Generator, n_slots: int, n_trades: int, holding: int
) -> np.ndarray:
    """Return the sorted entry indices of one random-entry simulation.

    ``n_slots`` disjoint windows of ``holding`` candles tile the backtest window;
    the entries are the first candle of ``n_trades`` of them, so two simulated
    positions can never share a candle.

    With more trades than windows the "one window each" rule is impossible, so the
    fallback draws windows **with replacement** (positions may then overlap).  The
    real strategy took more trades than the exposure profile leaves room for; that
    is reported through ``exposure`` and is the documented approximation.
    """
    if n_trades == 0:
        return np.empty(0, dtype="int64")
    if n_trades <= n_slots:
        slots = np.asarray(rng.choice(n_slots, size=n_trades, replace=False), dtype="int64")
    else:
        slots = np.asarray(rng.integers(0, n_slots, size=n_trades), dtype="int64")
    return np.sort(slots * np.int64(holding))


def _simulated_balance(
    closes: np.ndarray,
    entries: np.ndarray,
    holding: int,
    balance: float,
    fee_rate: float,
    slippage: float,
) -> float:
    """Replay ``entries`` all-in on ``closes`` and return the final balance.

    The sizing rule is the engine's own, applied leg by leg:
    ``size = balance / (entry_fill * (1 + fee_rate))`` then
    ``balance = size * exit_fill * (1 - fee_rate)`` with ``entry_fill =
    close[e] * (1 + slippage)`` and ``exit_fill = close[e + holding] * (1 -
    slippage)``.  Fees **and** slippage are therefore charged on both legs, and
    every trade is all-in (the whole balance is redeployed), exactly like
    :func:`trading_backtest.metrics.benchmark.buy_and_hold_equity`.

    Fills are close-to-close: the engine fills on the **open** of the candle
    following the signal, which is not observable from a random entry index; using
    the close of the entry candle is the documented approximation of this module
    (and the reason only the ``close`` column is required).
    """
    total = balance
    for entry in entries.tolist():
        start = int(entry)
        stop = start + holding
        entry_fill = float(closes[start]) * (1.0 + slippage)
        exit_fill = float(closes[stop]) * (1.0 - slippage)
        size = total / (entry_fill * (1.0 + fee_rate))
        total = size * exit_fill * (1.0 - fee_rate)
    return total


def random_entry_benchmark(
    result: BacktestResult,
    data: pd.DataFrame,
    *,
    n_simulations: int = DEFAULT_N_SIMULATIONS,
    random_seed: int = DEFAULT_RANDOM_SEED,
    initial_balance: float | None = None,
    fee_rate: float = 0.0,
    slippage: float = 0.0,
    holding_periods: int | None = None,
) -> RandomEntryResult:
    """Benchmark ``result`` against ``n_simulations`` random-entry strategies.

    The comparison matches the real run on three dimensions:

    * **number of trades** -- every simulation takes exactly ``result.n_trades``
      trades;
    * **holding period** -- every simulated position is held ``holding_periods``
      candles, derived from the real run's frozen ``exposure`` metric:
      ``holding = max(1, round(exposure * n_periods / n_trades))``;
    * **exposure** -- the realised simulated exposure is
      ``n_trades * holding_periods / n_periods`` and is reported as ``exposure``.
      It matches the real exposure *by construction, up to the rounding of the
      holding period*; the residual gap is exactly why the random-entry
      distribution is a fair, not an identical, yardstick.

    Every simulation is all-in on each trade, close-to-close, and pays
    ``fee_rate`` + ``slippage`` on both legs (see
    :func:`_simulated_balance`).  The ``total_return`` of a simulation is never
    recomputed by hand: it is read from the frozen
    :func:`~trading_backtest.metrics.performance.compute_metrics` on a synthetic
    :class:`~trading_backtest.core.models.BacktestResult` holding
    ``[initial_balance, final_balance]`` (``total_return`` is
    ``final / initial - 1``, so it is risk-free-rate independent and this
    comparison deliberately ignores the risk-free rate).

    Parameters
    ----------
    result:
        A *completed* backtest run.  No backtest is ever re-run here.
    data:
        OHLCV frame the run was produced from (same window); only ``close`` is
        read, and it must hold at least 2 candles.
    n_simulations:
        Number of random-entry simulations; must be ``>= 1`` and
        ``<= MAX_SIMULATIONS``.
    random_seed:
        Seed of the ``numpy.random.default_rng`` generator (``>= 0``): the same
        seed always yields the same distribution.
    initial_balance:
        Balance every simulation starts from; defaults to
        ``result.initial_balance`` and must be strictly positive.
    fee_rate, slippage:
        Per-side costs charged on both legs of every simulated trade.
    holding_periods:
        Explicit holding period, overriding the value derived from ``exposure``;
        must be ``>= 1`` and ``<= n_periods``.

    Returns
    -------
    RandomEntryResult
        ``percentile`` / ``p_value`` / ``returns`` / ``final_balances`` all
        describe the same ``n_simulations`` simulations and can be recomputed
        from ``returns``.

    Notes
    -----
    A run without any trade carries no evidence to resample: every simulation then
    returns ``0.0`` on an unchanged balance, so ``p_value`` is ``1.0`` and
    ``percentile`` is ``0.0`` as soon as the strategy's own total return is not
    positive -- no special case, exactly the "no resampleable evidence" stance of
    :func:`trading_backtest.validation.monte_carlo.monte_carlo`.

    Raises
    ------
    MetricsError
        If ``data`` holds fewer than 2 candles, if ``n_simulations`` is out of
        range, if ``random_seed`` is not a non-negative integer, if the balance is
        not strictly positive, or if an explicit ``holding_periods`` is out of
        range.
    ConfigError
        Propagated untouched from :func:`compute_metrics` for an unsupported
        timeframe.
    """
    prices = _benchmark_prices(data)
    n_periods = len(prices) - 1
    if n_periods < 1:
        raise MetricsError(f"random_entry benchmark requires at least 2 candles, got {len(prices)}")
    if n_simulations < 1:
        raise MetricsError(f"n_simulations must be an integer >= 1, got {n_simulations!r}")
    if n_simulations > MAX_SIMULATIONS:
        raise MetricsError(f"n_simulations must be <= {MAX_SIMULATIONS}, got {n_simulations!r}")
    if not isinstance(random_seed, int) or random_seed < 0:
        raise MetricsError(f"random_seed must be a non-negative integer, got {random_seed!r}")

    balance = float(result.initial_balance if initial_balance is None else initial_balance)
    if balance <= 0.0:
        raise MetricsError(f"initial_balance must be > 0, got {balance!r}")

    timeframe = str(result.timeframe)
    n_trades = int(result.n_trades)
    real_metrics = compute_metrics(result, timeframe=timeframe)
    exposure = float(real_metrics["exposure"])
    strategy_total_return = float(real_metrics["total_return"])

    if holding_periods is None:
        holding = max(1, round(exposure * n_periods / n_trades)) if n_trades > 0 else 1
    else:
        holding = int(holding_periods)
        if holding < 1 or holding > n_periods:
            raise MetricsError(
                f"holding_periods must be >= 1 and <= {n_periods}, got {holding_periods!r}"
            )

    fee = float(fee_rate)
    slip = float(slippage)
    closes = prices.to_numpy(dtype="float64")
    index = prices.index
    rng = np.random.default_rng(random_seed)
    returns: list[float] = []
    final_balances: list[float] = []

    for _ in range(n_simulations):
        entries = _simulation_entries(rng, n_periods // holding, n_trades, holding)
        final = _simulated_balance(closes, entries, holding, balance, fee, slip)
        synthetic = BacktestResult(
            strategy_name=RANDOM_ENTRY_VARIANT,
            symbol=str(result.symbol),
            timeframe=timeframe,
            start=pd.Timestamp(index[0]),
            end=pd.Timestamp(index[-1]),
            initial_balance=balance,
            final_balance=final,
            trades=[],
            equity_curve=pd.Series(
                [balance, final],
                index=pd.DatetimeIndex([index[0], index[-1]]),
                name="equity",
            ),
            params={},
            metadata={"variant": RANDOM_ENTRY_VARIANT},
        )
        returns.append(float(compute_metrics(synthetic, timeframe=timeframe)["total_return"]))
        final_balances.append(final)

    return_array = np.asarray(returns, dtype="float64")
    percentile_values = np.percentile(return_array, [5.0, 25.0, 50.0, 75.0, 95.0])
    percentiles: dict[str, float] = {
        label: float(value)
        for label, value in zip(PERCENTILE_LABELS, percentile_values, strict=True)
    }
    return RandomEntryResult(
        variant=RANDOM_ENTRY_VARIANT,
        n_simulations=int(n_simulations),
        random_seed=int(random_seed),
        n_trades=n_trades,
        holding_periods=holding,
        exposure=float(n_trades * holding / n_periods),
        n_periods=n_periods,
        initial_balance=balance,
        timeframe=timeframe,
        strategy_total_return=strategy_total_return,
        returns=returns,
        final_balances=final_balances,
        mean_return=float(return_array.mean()),
        median_return=float(np.median(return_array)),
        std_return=float(return_array.std()),
        percentiles=percentiles,
        percentile=float(
            100.0 * np.count_nonzero(return_array < strategy_total_return) / n_simulations
        ),
        p_value=float(np.count_nonzero(return_array >= strategy_total_return) / n_simulations),
    )
