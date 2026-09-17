"""Unit tests for the Monte Carlo layer (``trading_backtest.validation.monte_carlo``).

The input :class:`~trading_backtest.core.models.BacktestResult` objects are built
**directly** from ``TradeRecord`` / ``BacktestResult`` (the strategy engine is
never imported), and the expected statistics are recomputed independently from
the returned ``returns`` list — with one closed-form case where the ordering of
the resampled trades cannot change the result at all.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.constants import UTC
from trading_backtest.core.errors import ValidationLayerError
from trading_backtest.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    TradeRecord,
)
from trading_backtest.validation.monte_carlo import (
    PERCENTILE_LABELS,
    SERIALISED_SIMULATIONS,
    MonteCarloResult,
    monte_carlo,
)

INITIAL_BALANCE = 10_000.0

DOCUMENTED_KEYS = {
    "method",
    "n_simulations",
    "initial_balance",
    "n_trades",
    "final_balances",
    "returns",
    "max_drawdowns",
    "mean_return",
    "median_return",
    "std_return",
    "percentiles",
    "var_95",
    "cvar_95",
    "prob_profit",
    "worst_case_balance",
    "best_case_balance",
}

#: 20 trades with mixed, hand-picked P&L values (repeated 4 times).
MIXED_PNLS: tuple[float, ...] = (25.0, -50.0, 10.0, -5.0, 100.0) * 4


def _trade(pnl: float, index: pd.DatetimeIndex, position: int) -> TradeRecord:
    entry = index[min(position, len(index) - 1)]
    exit_ = index[min(position + 1, len(index) - 1)]
    return TradeRecord(
        entry_time=entry,
        exit_time=exit_,
        entry_price=100.0,
        exit_price=100.0 + pnl / 10.0,
        size=1.0,
        direction=Direction.LONG,
        pnl=pnl,
        pnl_pct=pnl / INITIAL_BALANCE,
        fees=0.0,
        exit_reason=ExitReason.SIGNAL,
        duration_minutes=60.0,
        stop_price=98.0,
        take_profit_price=102.0,
        params_id="monte-carlo",
    )


def _result(
    pnls: tuple[float, ...] = (),
    *,
    curve: np.ndarray | None = None,
    initial_balance: float = INITIAL_BALANCE,
) -> BacktestResult:
    """Build a completed backtest result directly from trades and a curve."""
    length = len(curve) if curve is not None else max(len(pnls) + 1, 2)
    index = pd.date_range(
        "2024-01-01T00:00:00Z", periods=length, freq="h", tz=UTC, name="timestamp"
    )
    trades = [_trade(pnl, index, position) for position, pnl in enumerate(pnls)]
    if curve is None:
        equity = pd.Series(
            np.full(length, initial_balance, dtype="float64"), index=index, name="equity"
        )
    else:
        equity = pd.Series(np.asarray(curve, dtype="float64"), index=index, name="equity")
    return BacktestResult(
        strategy_name="monte-carlo",
        symbol="BTC/USDT",
        timeframe="1h",
        start=index[0],
        end=index[-1],
        initial_balance=initial_balance,
        final_balance=float(equity.iloc[-1]),
        trades=trades,
        equity_curve=equity,
        params={},
        metadata={},
    )


# ---------------------------------------------------------------------------
# reproducibility and arguments
# ---------------------------------------------------------------------------


def test_same_seed_is_reproducible_and_other_seeds_differ() -> None:
    result = _result(MIXED_PNLS)

    first = monte_carlo(result, n_simulations=200, random_seed=7)
    second = monte_carlo(result, n_simulations=200, random_seed=7)
    other = monte_carlo(result, n_simulations=200, random_seed=8)

    assert first.returns == second.returns
    assert first.final_balances == second.final_balances
    assert first.max_drawdowns == second.max_drawdowns
    assert first.returns != other.returns
    assert first.final_balances != other.final_balances


def test_single_simulation_is_allowed() -> None:
    result = monte_carlo(_result(MIXED_PNLS), n_simulations=1, random_seed=3)

    assert result.n_simulations == 1
    assert len(result.returns) == 1
    assert len(result.final_balances) == 1
    assert len(result.max_drawdowns) == 1


@pytest.mark.parametrize("n_simulations", [0, -1])
def test_invalid_simulation_count_is_rejected(n_simulations: int) -> None:
    with pytest.raises(ValidationLayerError, match="n_simulations"):
        monte_carlo(_result(MIXED_PNLS), n_simulations=n_simulations)


def test_unknown_method_is_rejected() -> None:
    with pytest.raises(ValidationLayerError, match="unknown Monte Carlo method"):
        monte_carlo(_result(MIXED_PNLS), method="whatever")  # type: ignore[arg-type]


@pytest.mark.parametrize("initial_balance", [0.0, -100.0])
def test_non_positive_initial_balance_is_rejected(initial_balance: float) -> None:
    with pytest.raises(ValidationLayerError, match="initial_balance"):
        monte_carlo(_result(MIXED_PNLS), initial_balance=initial_balance)


# ---------------------------------------------------------------------------
# trade_resample
# ---------------------------------------------------------------------------


def test_constant_pnl_path_is_hand_computable() -> None:
    result = _result((10.0,) * 20)  # 20 trades of +10

    simulation = monte_carlo(result, n_simulations=50, random_seed=1)

    # whatever the resampling order, 20 draws of +10 add exactly 200
    assert simulation.n_trades == 20
    assert simulation.final_balances == [INITIAL_BALANCE + 200.0] * 50
    assert simulation.returns == [pytest.approx(0.02)] * 50
    assert simulation.max_drawdowns == [0.0] * 50
    assert simulation.mean_return == pytest.approx(0.02)
    assert simulation.median_return == pytest.approx(0.02)
    assert simulation.std_return == pytest.approx(0.0)
    assert simulation.percentiles == {label: pytest.approx(0.02) for label in PERCENTILE_LABELS}
    assert simulation.var_95 == pytest.approx(0.02)
    assert simulation.cvar_95 == pytest.approx(0.02)
    assert simulation.prob_profit == 1.0
    assert simulation.worst_case_balance == pytest.approx(INITIAL_BALANCE + 200.0)
    assert simulation.best_case_balance == pytest.approx(INITIAL_BALANCE + 200.0)
    assert simulation.initial_balance == INITIAL_BALANCE


def test_final_balances_match_an_independent_replay_of_the_same_draws() -> None:
    pnls = np.asarray(MIXED_PNLS, dtype="float64")
    n_simulations = 40
    seed = 11
    generator = np.random.default_rng(seed)
    expected: list[float] = []
    for _ in range(n_simulations):
        draws = pnls[generator.integers(0, pnls.size, size=pnls.size)]
        equity = INITIAL_BALANCE
        for value in draws:
            equity = max(0.0, equity + float(value))
        expected.append(equity)

    simulation = monte_carlo(_result(MIXED_PNLS), n_simulations=n_simulations, random_seed=seed)

    assert simulation.final_balances == pytest.approx(expected)
    assert simulation.returns == pytest.approx(
        [value / INITIAL_BALANCE - 1.0 for value in expected]
    )


def test_in_sample_balance_override_scales_the_paths() -> None:
    simulation = monte_carlo(
        _result((10.0,) * 20), n_simulations=5, random_seed=2, initial_balance=5_000.0
    )

    assert simulation.initial_balance == 5_000.0
    assert simulation.final_balances == [5_200.0] * 5
    assert simulation.returns == [pytest.approx(0.04)] * 5


def test_all_loss_trades_floor_the_equity_at_zero() -> None:
    result = _result((-1_000.0,) * 20)

    simulation = monte_carlo(result, n_simulations=200, random_seed=5)

    assert simulation.prob_profit == 0.0
    assert simulation.worst_case_balance == 0.0
    assert simulation.best_case_balance == 0.0
    assert all(value <= 0.0 for value in simulation.max_drawdowns)
    assert min(simulation.max_drawdowns) == pytest.approx(-1.0)
    assert all(value == pytest.approx(-1.0) for value in simulation.returns)


# ---------------------------------------------------------------------------
# tail statistics
# ---------------------------------------------------------------------------


def test_tail_statistics_are_recomputable_from_the_returns() -> None:
    simulation = monte_carlo(_result(MIXED_PNLS), n_simulations=500, random_seed=42)

    values = np.asarray(simulation.returns, dtype="float64")
    assert len(values) == 500
    var = float(np.percentile(values, 5.0))
    below = values[values <= var]
    expected_cvar = float(below.mean()) if below.size else var

    assert simulation.var_95 == pytest.approx(var, abs=1e-9)
    assert simulation.cvar_95 == pytest.approx(expected_cvar, abs=1e-9)
    assert set(simulation.percentiles) == set(PERCENTILE_LABELS)
    for label, quantile in (
        ("p05", 5.0),
        ("p25", 25.0),
        ("p50", 50.0),
        ("p75", 75.0),
        ("p95", 95.0),
    ):
        assert simulation.percentiles[label] == pytest.approx(
            float(np.percentile(values, quantile)), abs=1e-9
        )
    assert simulation.var_95 == simulation.percentiles["p05"]
    assert simulation.median_return == pytest.approx(float(np.median(values)), abs=1e-9)
    assert simulation.median_return == simulation.percentiles["p50"]
    assert simulation.mean_return == pytest.approx(float(values.mean()), abs=1e-9)
    assert simulation.std_return == pytest.approx(float(values.std()), abs=1e-9)
    assert simulation.prob_profit == pytest.approx(
        float(np.count_nonzero(np.asarray(simulation.final_balances) > INITIAL_BALANCE) / 500)
    )
    assert min(simulation.returns) <= simulation.var_95
    assert simulation.cvar_95 <= simulation.var_95


# ---------------------------------------------------------------------------
# no trades
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["trade_resample", "bootstrap_equity"])
def test_a_result_without_trades_yields_zeros(method: str) -> None:
    result = _result()

    simulation = monte_carlo(result, n_simulations=25, method=method, random_seed=9)  # type: ignore[arg-type]

    assert simulation.n_trades == 0
    assert simulation.final_balances == [INITIAL_BALANCE] * 25
    assert simulation.returns == [0.0] * 25
    assert simulation.max_drawdowns == [0.0] * 25
    assert simulation.prob_profit == 0.0
    assert simulation.mean_return == 0.0
    assert simulation.std_return == 0.0
    assert simulation.worst_case_balance == INITIAL_BALANCE
    assert simulation.best_case_balance == INITIAL_BALANCE
    assert simulation.histogram(bins=2)["counts"] == [0.0, 25.0]


# ---------------------------------------------------------------------------
# bootstrap_equity
# ---------------------------------------------------------------------------


def test_bootstrap_equity_on_a_flat_curve_returns_zero() -> None:
    result = _result((5.0,), curve=np.full(6, INITIAL_BALANCE, dtype="float64"))

    simulation = monte_carlo(result, n_simulations=30, method="bootstrap_equity", random_seed=4)

    assert simulation.method == "bootstrap_equity"
    assert simulation.returns == [0.0] * 30
    assert simulation.std_return == 0.0
    assert simulation.max_drawdowns == [0.0] * 30
    assert simulation.final_balances == [INITIAL_BALANCE] * 30


def test_bootstrap_equity_resamples_the_curve_and_is_reproducible() -> None:
    curve = np.array([10_000.0, 10_100.0, 9_900.0, 10_200.0, 10_050.0, 10_300.0])
    result = _result((5.0, 5.0), curve=curve)

    first = monte_carlo(result, n_simulations=100, method="bootstrap_equity", random_seed=13)
    second = monte_carlo(result, n_simulations=100, method="bootstrap_equity", random_seed=13)
    other = monte_carlo(result, n_simulations=100, method="bootstrap_equity", random_seed=14)

    assert first.returns == second.returns
    assert first.returns != other.returns
    assert first.std_return > 0.0
    assert any(value != 0.0 for value in first.returns)
    assert all(value <= 0.0 for value in first.max_drawdowns)
    assert first.n_trades == 2


def test_bootstrap_equity_with_a_one_point_curve_returns_zero() -> None:
    result = _result((5.0,), curve=np.array([INITIAL_BALANCE]))

    simulation = monte_carlo(result, n_simulations=3, method="bootstrap_equity", random_seed=6)

    assert simulation.returns == [0.0] * 3
    assert simulation.final_balances == [INITIAL_BALANCE] * 3


# ---------------------------------------------------------------------------
# histogram and serialisation
# ---------------------------------------------------------------------------


def test_histogram_bins_partition_the_simulations() -> None:
    simulation = monte_carlo(_result(MIXED_PNLS), n_simulations=300, random_seed=1)

    histogram = simulation.histogram(bins=5)

    assert set(histogram) == {"edges", "counts"}
    assert len(histogram["edges"]) == 6
    assert len(histogram["counts"]) == 5
    assert sum(histogram["counts"]) == 300
    assert histogram["edges"] == sorted(histogram["edges"])
    assert all(isinstance(value, float) for value in histogram["counts"])
    single = simulation.histogram(bins=1)
    assert len(single["edges"]) == 2 and single["counts"] == [300.0]


@pytest.mark.parametrize("bins", [0, -3])
def test_histogram_rejects_invalid_bin_counts(bins: int) -> None:
    simulation = monte_carlo(_result(MIXED_PNLS), n_simulations=5, random_seed=1)

    with pytest.raises(ValidationLayerError, match="bins"):
        simulation.histogram(bins=bins)


def test_to_dict_is_json_serialisable_and_truncates_the_lists() -> None:
    simulation = monte_carlo(_result(MIXED_PNLS), n_simulations=1500, random_seed=17)

    payload = json.loads(json.dumps(simulation.to_dict()))

    assert set(payload) == DOCUMENTED_KEYS
    assert set(payload["percentiles"]) == set(PERCENTILE_LABELS)
    assert payload["n_simulations"] == 1500
    assert payload["n_trades"] == 20
    assert payload["method"] == "trade_resample"
    assert len(payload["final_balances"]) == SERIALISED_SIMULATIONS
    assert len(payload["returns"]) == SERIALISED_SIMULATIONS
    assert len(payload["max_drawdowns"]) == SERIALISED_SIMULATIONS
    # the in-memory result keeps every simulation
    assert len(simulation.final_balances) == 1500
    assert len(simulation.returns) == 1500
    assert len(simulation.max_drawdowns) == 1500
    assert payload["final_balances"] == simulation.final_balances[:SERIALISED_SIMULATIONS]
    assert payload["var_95"] == simulation.var_95
    assert payload["cvar_95"] == simulation.cvar_95
    assert payload["prob_profit"] == simulation.prob_profit


def test_empty_monte_carlo_result_is_serialisable() -> None:
    simulation = MonteCarloResult()

    payload = json.loads(json.dumps(simulation.to_dict()))

    assert set(payload) == DOCUMENTED_KEYS
    assert payload["final_balances"] == []
    assert payload["percentiles"] == {}
    assert simulation.histogram(bins=3)["counts"] == [0.0, 0.0, 0.0]
