"""Monte Carlo simulation of an *already computed* backtest result.

A single backtest is one sample of one ordering of one history.  Monte Carlo
answers the follow-up question: *how much of this result is luck?*

Two complementary resampling schemes are provided, both driven by
``numpy.random.default_rng(random_seed)`` so that a simulation is exactly
reproducible for a given seed:

``trade_resample``
    Bootstrap the realised trade sequence: every simulation draws
    ``n_trades`` P&L values **with replacement** in the order they are drawn and
    replays them on an additive equity path.  This measures the dispersion that
    comes from the *ordering / sampling* of the trades that were actually taken.

``bootstrap_equity``
    Bootstrap the realised per-candle returns of ``result.equity_curve`` and
    compound them.  This keeps the temporal profile of the equity curve and
    measures the dispersion of the path itself.

Both schemes share the same output statistics, and both floor the equity at
``0.0`` (a blown-up account cannot come back).  Nothing here ever re-runs a
backtest: this module only consumes a :class:`BacktestResult`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from trading_platform.core.errors import ValidationLayerError
from trading_platform.core.models import BacktestResult

__all__ = [
    "METHODS",
    "MonteCarloResult",
    "monte_carlo",
]

#: Supported resampling schemes.
METHODS: tuple[str, ...] = ("trade_resample", "bootstrap_equity")

#: Number of per-simulation values kept in :meth:`MonteCarloResult.to_dict`.
SERIALISED_SIMULATIONS = 1000

#: Percentile labels exposed by :attr:`MonteCarloResult.percentiles`.
PERCENTILE_LABELS: tuple[str, ...] = ("p05", "p25", "p50", "p75", "p95")


@dataclass(frozen=True)
class MonteCarloResult:
    """Outcome of a Monte Carlo simulation over one backtest result.

    Per-simulation values are kept in full (``final_balances``, ``returns`` and
    ``max_drawdowns`` all have exactly ``n_simulations`` entries) while
    ``to_dict`` truncates them to the first
    :data:`SERIALISED_SIMULATIONS` entries to keep the serialised report small.
    """

    method: str = "trade_resample"
    n_simulations: int = 0
    initial_balance: float = 0.0
    n_trades: int = 0
    final_balances: list[float] = field(default_factory=list)
    returns: list[float] = field(default_factory=list)
    max_drawdowns: list[float] = field(default_factory=list)
    mean_return: float = 0.0
    median_return: float = 0.0
    std_return: float = 0.0
    percentiles: dict[str, float] = field(default_factory=dict)
    var_95: float = 0.0
    cvar_95: float = 0.0
    prob_profit: float = 0.0
    worst_case_balance: float = 0.0
    best_case_balance: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable mapping of every statistic.

        The three per-simulation lists are truncated to their first
        :data:`SERIALISED_SIMULATIONS` entries; every scalar and percentile is
        emitted in full.
        """
        return {
            "method": str(self.method),
            "n_simulations": int(self.n_simulations),
            "initial_balance": float(self.initial_balance),
            "n_trades": int(self.n_trades),
            "final_balances": [
                float(value) for value in self.final_balances[:SERIALISED_SIMULATIONS]
            ],
            "returns": [float(value) for value in self.returns[:SERIALISED_SIMULATIONS]],
            "max_drawdowns": [
                float(value) for value in self.max_drawdowns[:SERIALISED_SIMULATIONS]
            ],
            "mean_return": float(self.mean_return),
            "median_return": float(self.median_return),
            "std_return": float(self.std_return),
            "percentiles": {key: float(value) for key, value in self.percentiles.items()},
            "var_95": float(self.var_95),
            "cvar_95": float(self.cvar_95),
            "prob_profit": float(self.prob_profit),
            "worst_case_balance": float(self.worst_case_balance),
            "best_case_balance": float(self.best_case_balance),
        }

    def histogram(self, bins: int = 20) -> dict[str, list[float]]:
        """Return the histogram of simulated returns as ``{"edges", "counts"}``.

        Raises
        ------
        ValidationLayerError
            If ``bins < 1``.
        """
        if bins < 1:
            raise ValidationLayerError(f"bins must be an integer >= 1, got {bins!r}")
        counts, edges = np.histogram(np.asarray(self.returns, dtype="float64"), bins=bins)
        return {
            "edges": [float(edge) for edge in edges],
            "counts": [float(count) for count in counts],
        }


# ---------------------------------------------------------------------------
# equity paths
# ---------------------------------------------------------------------------


def _additive_path(initial_balance: float, pnls: np.ndarray) -> np.ndarray:
    """Replay ``pnls`` on an additive equity path floored at ``0.0``."""
    path = np.empty(pnls.size + 1, dtype="float64")
    path[0] = initial_balance
    equity = float(initial_balance)
    for position, pnl in enumerate(pnls):
        equity = max(0.0, equity + float(pnl))
        path[position + 1] = equity
    return path


def _compound_path(initial_balance: float, returns: np.ndarray) -> np.ndarray:
    """Compound ``returns`` on a multiplicative equity path floored at ``0.0``."""
    path = np.empty(returns.size + 1, dtype="float64")
    path[0] = initial_balance
    equity = float(initial_balance)
    for position, period_return in enumerate(returns):
        equity = max(0.0, equity * (1.0 + float(period_return)))
        path[position + 1] = equity
    return path


def _max_drawdown(path: np.ndarray) -> float:
    """Return the deepest peak-to-trough decline of ``path`` (negative or 0.0)."""
    peaks = np.maximum.accumulate(path)
    safe_peaks = np.where(peaks > 0.0, peaks, 1.0)
    drawdowns = np.where(peaks > 0.0, path / safe_peaks - 1.0, 0.0)
    return float(np.min(drawdowns))


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def monte_carlo(
    result: BacktestResult,
    *,
    n_simulations: int = 1000,
    method: Literal["trade_resample", "bootstrap_equity"] = "trade_resample",
    random_seed: int = 42,
    initial_balance: float | None = None,
) -> MonteCarloResult:
    """Simulate ``n_simulations`` alternative equity paths for ``result``.

    Parameters
    ----------
    result:
        A *completed* backtest result.  No backtest is ever re-run here.
    n_simulations:
        Number of simulated paths; must be ``>= 1``.
    method:
        ``"trade_resample"`` (bootstrap the realised trades) or
        ``"bootstrap_equity"`` (bootstrap the per-candle returns of the equity
        curve).
    random_seed:
        Seed of the ``numpy.random.default_rng`` generator: the same seed always
        yields the same simulation, different seeds yield different ones.
    initial_balance:
        Balance the simulated paths start from; defaults to
        ``result.initial_balance`` and must be strictly positive.

    Returns
    -------
    MonteCarloResult
        ``percentiles`` / ``var_95`` / ``cvar_95`` are plain statistics of the
        returned ``returns`` list and can therefore always be recomputed from
        it.

    Notes
    -----
    A result without any trade carries no resampleable evidence: every
    simulation then returns ``0.0`` with a flat balance at ``initial_balance``
    and a zero drawdown, instead of raising.

    Raises
    ------
    ValidationLayerError
        If ``n_simulations < 1``, if ``method`` is unknown or if
        ``initial_balance`` is not strictly positive.
    """
    if n_simulations < 1:
        raise ValidationLayerError(f"n_simulations must be an integer >= 1, got {n_simulations!r}")
    if method not in METHODS:
        raise ValidationLayerError(
            f"unknown Monte Carlo method: {method!r} (available: {', '.join(METHODS)})"
        )

    balance = float(result.initial_balance if initial_balance is None else initial_balance)
    if balance <= 0.0:
        raise ValidationLayerError(f"initial_balance must be > 0, got {balance!r}")

    n_trades = int(result.n_trades)
    rng = np.random.default_rng(random_seed)
    final_balances: list[float] = []
    returns: list[float] = []
    max_drawdowns: list[float] = []

    if n_trades == 0:
        final_balances = [balance] * n_simulations
        returns = [0.0] * n_simulations
        max_drawdowns = [0.0] * n_simulations
    elif method == "trade_resample":
        pnls = np.asarray([float(trade.pnl) for trade in result.trades], dtype="float64")
        for _ in range(n_simulations):
            draws = pnls[rng.integers(0, n_trades, size=n_trades)]
            path = _additive_path(balance, draws)
            final_balances.append(float(path[-1]))
            returns.append(float(path[-1]) / balance - 1.0)
            max_drawdowns.append(_max_drawdown(path))
    else:
        curve = result.equity_curve
        period_returns = (
            curve.pct_change().dropna().to_numpy(dtype="float64") if len(curve) >= 2 else None
        )
        if period_returns is None or period_returns.size == 0:
            final_balances = [balance] * n_simulations
            returns = [0.0] * n_simulations
            max_drawdowns = [0.0] * n_simulations
        else:
            for _ in range(n_simulations):
                draws = period_returns[
                    rng.integers(0, period_returns.size, size=period_returns.size)
                ]
                path = _compound_path(balance, draws)
                final_balances.append(float(path[-1]))
                returns.append(float(path[-1]) / balance - 1.0)
                max_drawdowns.append(_max_drawdown(path))

    final_array = np.asarray(final_balances, dtype="float64")
    return_array = np.asarray(returns, dtype="float64")
    p05, p25, p50, p75, p95 = (
        float(value) for value in np.percentile(return_array, [5.0, 25.0, 50.0, 75.0, 95.0])
    )
    percentiles = {"p05": p05, "p25": p25, "p50": p50, "p75": p75, "p95": p95}
    var_95 = p05
    if return_array.size == 0:  # pragma: no cover - n_simulations >= 1 guarantees data
        cvar_95 = 0.0
    else:
        below = return_array[return_array <= var_95]
        cvar_95 = float(below.mean()) if below.size else var_95

    return MonteCarloResult(
        method=str(method),
        n_simulations=int(n_simulations),
        initial_balance=balance,
        n_trades=n_trades,
        final_balances=final_balances,
        returns=returns,
        max_drawdowns=max_drawdowns,
        mean_return=float(return_array.mean()),
        median_return=float(np.median(return_array)),
        std_return=float(return_array.std()),
        percentiles=percentiles,
        var_95=float(var_95),
        cvar_95=float(cvar_95),
        prob_profit=float(np.count_nonzero(final_array > balance) / final_array.size),
        worst_case_balance=float(final_array.min()),
        best_case_balance=float(final_array.max()),
    )
