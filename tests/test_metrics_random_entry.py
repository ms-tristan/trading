"""Tests for the random-entry benchmark (``trading_platform.metrics.random_entry``).

Everything here is offline and deterministic: the frames are synthetic, the
simulations are driven by an explicit seed and no strategy/validation/reporting
module is imported -- the random-entry benchmark only talks to ``core`` and the
sibling metric modules.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import FrozenInstanceError
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.constants import (
    DEFAULT_INITIAL_BALANCE,
    OHLCV_INDEX_NAME,
    UTC,
)
from trading_platform.core.errors import MetricsError
from trading_platform.core.models import BacktestResult, Direction, ExitReason, TradeRecord
from trading_platform.metrics import (
    DEFAULT_N_SIMULATIONS,
    DEFAULT_RANDOM_SEED,
    MAX_SIMULATIONS,
    PERCENTILE_LABELS,
    RANDOM_ENTRY_VARIANT,
    SERIALISED_SIMULATIONS,
    RandomEntryResult,
    compute_metrics,
    random_entry_benchmark,
)

TO_DICT_KEYS = (
    "variant",
    "n_simulations",
    "random_seed",
    "n_trades",
    "holding_periods",
    "exposure",
    "n_periods",
    "initial_balance",
    "timeframe",
    "strategy_total_return",
    "returns",
    "final_balances",
    "mean_return",
    "median_return",
    "std_return",
    "percentiles",
    "percentile",
    "p_value",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_frame(closes: Sequence[float], *, start: str = "2024-01-01T00:00:00Z") -> pd.DataFrame:
    """Return a minimal OHLCV frame whose closes are ``closes`` (hourly, UTC)."""
    values = np.asarray(closes, dtype="float64")
    index = pd.date_range(start, periods=len(values), freq="1h", tz=UTC, name=OHLCV_INDEX_NAME)
    return pd.DataFrame(
        {
            "open": values,
            "high": values,
            "low": values,
            "close": values,
            "volume": np.ones(len(values), dtype="float64"),
        },
        index=index,
    )


def make_trades(frame: pd.DataFrame, n_trades: int, duration_minutes: float) -> list[TradeRecord]:
    """Return ``n_trades`` identical long trades lasting ``duration_minutes``."""
    start = pd.Timestamp(frame.index[0])
    return [
        TradeRecord(
            entry_time=start,
            exit_time=start + pd.Timedelta(minutes=duration_minutes),
            entry_price=100.0,
            exit_price=100.0,
            size=1.0,
            direction=Direction.LONG,
            pnl=1.0,
            pnl_pct=0.01,
            fees=0.0,
            exit_reason=ExitReason.SIGNAL,
            duration_minutes=float(duration_minutes),
        )
        for _ in range(n_trades)
    ]


def make_result(
    frame: pd.DataFrame,
    values: Sequence[float],
    *,
    n_trades: int = 0,
    duration_minutes: float = 60.0,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
    timeframe: str = "1h",
) -> BacktestResult:
    """Return a completed run with a chosen equity curve and trade profile."""
    index = pd.DatetimeIndex(frame.index)
    equity = pd.Series(np.asarray(values, dtype="float64"), index=index, name="equity")
    trades = make_trades(frame, n_trades, duration_minutes) if n_trades else []
    return BacktestResult(
        strategy_name="unit-test",
        symbol="BTC/USDT",
        timeframe=timeframe,
        start=pd.Timestamp(index[0]),
        end=pd.Timestamp(index[-1]),
        initial_balance=initial_balance,
        final_balance=float(equity.iloc[-1]),
        trades=trades,
        equity_curve=equity,
        params={},
    )


def wiggle_frame(n: int = 60) -> pd.DataFrame:
    """Return a deterministically wiggly frame (many different entry outcomes)."""
    steps = np.arange(n, dtype="float64")
    return make_frame(100.0 + 3.0 * steps + 20.0 * np.sin(2.0 * np.pi * steps / 10.0))


def assert_json_native(value: Any) -> None:
    """Assert ``value`` only contains JSON-native types (no numpy scalar)."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str)
            assert_json_native(item)
    elif isinstance(value, list):
        for item in value:
            assert_json_native(item)
    else:
        assert not isinstance(value, np.generic), f"numpy scalar leaked: {value!r}"
        assert value is None or isinstance(value, (bool, int, float, str)), repr(value)


# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------
def test_module_constants_are_frozen() -> None:
    assert DEFAULT_N_SIMULATIONS == 1000
    assert DEFAULT_RANDOM_SEED == 42
    assert MAX_SIMULATIONS == 10_000
    assert PERCENTILE_LABELS == ("p05", "p25", "p50", "p75", "p95")
    assert RANDOM_ENTRY_VARIANT == "random_entry"


def test_defaults_are_the_documented_ones() -> None:
    frame = wiggle_frame(30)
    result = make_result(
        frame, np.full(30, DEFAULT_INITIAL_BALANCE), n_trades=3, duration_minutes=60
    )
    outcome = random_entry_benchmark(result, frame, n_simulations=5, holding_periods=1)
    assert outcome.random_seed == DEFAULT_RANDOM_SEED
    assert len(outcome.returns) == 5
    assert outcome.n_simulations == 5


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------
def test_same_seed_is_bit_identical_and_another_seed_differs() -> None:
    frame = wiggle_frame(40)
    result = make_result(frame, np.full(40, 10_000.0), n_trades=4, duration_minutes=60)

    first = random_entry_benchmark(result, frame, n_simulations=25, random_seed=42)
    second = random_entry_benchmark(result, frame, n_simulations=25, random_seed=42)
    other = random_entry_benchmark(result, frame, n_simulations=25, random_seed=43)

    assert first.to_dict() == second.to_dict()
    assert first.returns == second.returns
    assert first.final_balances == second.final_balances
    assert other.returns != first.returns
    assert other.to_dict() != first.to_dict()


def test_the_same_seed_reproduces_the_distribution_statistics() -> None:
    frame = wiggle_frame(50)
    result = make_result(frame, np.full(50, 10_000.0), n_trades=5, duration_minutes=120)
    first = random_entry_benchmark(result, frame, n_simulations=40)
    second = random_entry_benchmark(result, frame, n_simulations=40)
    assert first.percentile == second.percentile
    assert first.p_value == second.p_value
    assert first.percentiles == second.percentiles


# ---------------------------------------------------------------------------
# the simulated profile matches the real run
# ---------------------------------------------------------------------------
def test_holding_period_is_derived_from_the_frozen_exposure_metric() -> None:
    frame = make_frame(100.0 + np.arange(13, dtype="float64"))
    result = make_result(frame, np.full(13, 10_000.0), n_trades=3, duration_minutes=120.0)
    n_periods = len(frame) - 1
    exposure = float(compute_metrics(result, timeframe="1h")["exposure"])
    assert exposure == pytest.approx(3 * 120.0 / (n_periods * 60.0), rel=1e-12)

    outcome = random_entry_benchmark(result, frame, n_simulations=10)
    assert outcome.n_trades == 3
    assert outcome.n_periods == n_periods
    assert outcome.holding_periods == max(1, round(exposure * n_periods / 3)) == 2
    # exposure is not double counted: the simulated positions are disjoint windows
    assert outcome.exposure == pytest.approx(3 * 2 / n_periods, rel=1e-12)
    assert outcome.exposure == pytest.approx(exposure, rel=1e-12)
    assert outcome.initial_balance == 10_000.0
    assert outcome.timeframe == "1h"
    assert outcome.variant == RANDOM_ENTRY_VARIANT


def test_explicit_holding_period_overrides_the_exposure_derivation() -> None:
    frame = make_frame(100.0 + np.arange(13, dtype="float64"))
    result = make_result(frame, np.full(13, 10_000.0), n_trades=3, duration_minutes=120.0)
    derived = random_entry_benchmark(result, frame, n_simulations=5)
    explicit = random_entry_benchmark(result, frame, n_simulations=5, holding_periods=1)
    assert derived.holding_periods == 2
    assert explicit.holding_periods == 1
    assert explicit.exposure == pytest.approx(3 * 1 / 12, rel=1e-12)


def test_initial_balance_override_is_used_by_every_simulation() -> None:
    frame = make_frame(100.0 + np.arange(20, dtype="float64"))
    result = make_result(frame, np.full(20, 50_000.0), n_trades=2, duration_minutes=60.0)
    outcome = random_entry_benchmark(
        result, frame, n_simulations=10, initial_balance=1_000.0, holding_periods=1
    )
    assert outcome.initial_balance == 1_000.0
    assert all(balance > 0.0 for balance in outcome.final_balances)
    assert all(
        balance == pytest.approx(1_000.0 * (1.0 + total_return), rel=1e-9)
        for balance, total_return in zip(outcome.final_balances, outcome.returns, strict=True)
    )


# ---------------------------------------------------------------------------
# the simulated returns themselves
# ---------------------------------------------------------------------------
def test_strictly_rising_prices_make_every_simulation_profitable() -> None:
    frame = make_frame(100.0 + np.arange(30, dtype="float64"))
    result = make_result(frame, np.full(30, 10_000.0), n_trades=3, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=30, holding_periods=1)
    assert all(total_return > 0.0 for total_return in outcome.returns)
    assert all(balance > outcome.initial_balance for balance in outcome.final_balances)


def test_strictly_falling_prices_make_every_simulation_lose() -> None:
    frame = make_frame(200.0 - np.arange(30, dtype="float64"))
    result = make_result(frame, np.full(30, 10_000.0), n_trades=3, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=30, holding_periods=1)
    assert all(total_return < 0.0 for total_return in outcome.returns)
    assert all(balance < outcome.initial_balance for balance in outcome.final_balances)


def test_costs_reduce_every_simulated_return() -> None:
    frame = make_frame(100.0 + np.arange(30, dtype="float64"))
    result = make_result(frame, np.full(30, 10_000.0), n_trades=3, duration_minutes=60.0)
    free = random_entry_benchmark(result, frame, n_simulations=20, holding_periods=1)
    unchanged = random_entry_benchmark(
        result, frame, n_simulations=20, holding_periods=1, fee_rate=0.0, slippage=0.0
    )
    costly = random_entry_benchmark(
        result, frame, n_simulations=20, holding_periods=1, fee_rate=0.001, slippage=0.0005
    )
    # costs change the P&L, not the draws: the same seed yields the same entries
    assert unchanged.returns == free.returns
    assert all(charged < plain for charged, plain in zip(costly.returns, free.returns, strict=True))


def test_returns_and_final_balances_stay_consistent() -> None:
    frame = wiggle_frame(40)
    result = make_result(frame, np.full(40, 10_000.0), n_trades=4, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=15, holding_periods=1)
    assert len(outcome.returns) == len(outcome.final_balances) == outcome.n_simulations
    for total_return, balance in zip(outcome.returns, outcome.final_balances, strict=True):
        assert total_return == pytest.approx(balance / outcome.initial_balance - 1.0, rel=1e-12)


# ---------------------------------------------------------------------------
# percentile / p-value
# ---------------------------------------------------------------------------
def test_percentile_and_p_value_are_consistent() -> None:
    frame = wiggle_frame(45)
    result = make_result(frame, np.full(45, 10_000.0), n_trades=4, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=60, holding_periods=1)
    assert 0.0 <= outcome.p_value <= 1.0
    assert 0.0 <= outcome.percentile <= 100.0
    assert outcome.percentile / 100.0 + outcome.p_value >= 1.0 - 1e-12
    assert outcome.percentile == pytest.approx(
        100.0 * sum(1 for value in outcome.returns if value < outcome.strategy_total_return) / 60,
        rel=1e-12,
    )
    assert outcome.p_value == pytest.approx(
        sum(1 for value in outcome.returns if value >= outcome.strategy_total_return) / 60,
        rel=1e-12,
    )


def test_a_strategy_beating_every_simulation_is_at_the_top_percentile() -> None:
    frame = make_frame(200.0 - np.arange(30, dtype="float64"))
    result = make_result(
        frame, np.linspace(10_000.0, 20_000.0, 30), n_trades=3, duration_minutes=60.0
    )
    outcome = random_entry_benchmark(result, frame, n_simulations=40, holding_periods=1)
    assert outcome.strategy_total_return == pytest.approx(1.0, rel=1e-12)
    assert all(value < outcome.strategy_total_return for value in outcome.returns)
    assert outcome.percentile == 100.0
    assert outcome.p_value == 0.0


def test_a_strategy_losing_to_every_simulation_is_at_the_bottom_percentile() -> None:
    frame = make_frame(100.0 + np.arange(30, dtype="float64"))
    result = make_result(
        frame, np.linspace(10_000.0, 5_000.0, 30), n_trades=3, duration_minutes=60.0
    )
    outcome = random_entry_benchmark(result, frame, n_simulations=40, holding_periods=1)
    assert all(value > outcome.strategy_total_return for value in outcome.returns)
    assert outcome.percentile == 0.0
    assert outcome.p_value == 1.0


def test_a_strategy_inside_the_distribution_lands_strictly_between_the_extremes() -> None:
    frame = wiggle_frame(60)
    result = make_result(frame, np.full(60, 10_000.0), n_trades=4, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=200, holding_periods=1)
    assert 0.0 < outcome.percentile < 100.0
    assert 0.0 < outcome.p_value < 1.0
    assert outcome.percentile / 100.0 + outcome.p_value == pytest.approx(1.0, abs=1e-12)


# ---------------------------------------------------------------------------
# a run without any trade
# ---------------------------------------------------------------------------
def test_no_trade_run_needs_no_special_case() -> None:
    frame = make_frame(100.0 + np.arange(20, dtype="float64"))
    result = make_result(frame, np.full(20, 10_000.0))  # flat curve, 0 trade
    outcome = random_entry_benchmark(result, frame, n_simulations=25)

    assert outcome.n_trades == 0
    assert outcome.holding_periods == 1
    assert outcome.exposure == 0.0
    assert outcome.returns == [0.0] * 25
    assert outcome.final_balances == [10_000.0] * 25
    assert outcome.strategy_total_return == 0.0
    assert outcome.p_value == 1.0
    assert outcome.percentile == 0.0
    assert outcome.mean_return == 0.0
    assert outcome.std_return == 0.0


# ---------------------------------------------------------------------------
# the "more trades than windows" fallback
# ---------------------------------------------------------------------------
def test_more_trades_than_windows_falls_back_to_overlapping_windows() -> None:
    frame = make_frame(100.0 + np.arange(5, dtype="float64"))
    result = make_result(frame, np.full(5, 10_000.0), n_trades=6, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=12, holding_periods=1)

    assert outcome.n_periods == 4  # only 4 non-overlapping single-candle windows
    assert outcome.n_trades == 6  # more trades than windows -> replacement
    assert outcome.holding_periods == 1
    assert outcome.exposure == pytest.approx(6 * 1 / 4, rel=1e-12)  # overlapping, > 1
    assert len(outcome.returns) == 12
    assert all(np.isfinite(value) for value in outcome.returns)


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------
def test_single_candle_frame_raises() -> None:
    frame = make_frame((100.0,))
    result = make_result(frame, (10_000.0,))
    with pytest.raises(MetricsError, match="at least 2 candles"):
        random_entry_benchmark(result, frame)


@pytest.mark.parametrize("n_simulations", [0, -1, MAX_SIMULATIONS + 1])
def test_out_of_range_n_simulations_raises(n_simulations: int) -> None:
    frame = wiggle_frame(20)
    result = make_result(frame, np.full(20, 10_000.0), n_trades=2, duration_minutes=60.0)
    with pytest.raises(MetricsError, match="n_simulations"):
        random_entry_benchmark(result, frame, n_simulations=n_simulations)


def test_negative_random_seed_raises() -> None:
    frame = wiggle_frame(20)
    result = make_result(frame, np.full(20, 10_000.0), n_trades=2, duration_minutes=60.0)
    with pytest.raises(MetricsError, match="random_seed"):
        random_entry_benchmark(result, frame, n_simulations=5, random_seed=-1)


@pytest.mark.parametrize("balance", [0.0, -1.0])
def test_non_positive_initial_balance_raises(balance: float) -> None:
    frame = wiggle_frame(20)
    result = make_result(frame, np.full(20, 10_000.0), n_trades=2, duration_minutes=60.0)
    with pytest.raises(MetricsError, match="initial_balance"):
        random_entry_benchmark(result, frame, n_simulations=5, initial_balance=balance)


def test_non_positive_result_balance_raises() -> None:
    frame = wiggle_frame(20)
    result = make_result(frame, np.full(20, 10_000.0), n_trades=2, duration_minutes=60.0)
    broken = BacktestResult(
        strategy_name=result.strategy_name,
        symbol=result.symbol,
        timeframe=result.timeframe,
        start=result.start,
        end=result.end,
        initial_balance=0.0,
        final_balance=0.0,
        trades=result.trades,
        equity_curve=result.equity_curve,
        params={},
    )
    with pytest.raises(MetricsError, match="initial_balance"):
        random_entry_benchmark(broken, frame, n_simulations=5)


@pytest.mark.parametrize("holding", [0, -1, 13])
def test_out_of_range_holding_periods_raises(holding: int) -> None:
    frame = make_frame(100.0 + np.arange(13, dtype="float64"))  # 12 periods
    result = make_result(frame, np.full(13, 10_000.0), n_trades=3, duration_minutes=60.0)
    with pytest.raises(MetricsError, match="holding_periods"):
        random_entry_benchmark(result, frame, n_simulations=5, holding_periods=holding)


def test_holding_periods_equal_to_the_whole_window_is_accepted() -> None:
    frame = make_frame(100.0 + np.arange(7, dtype="float64"))  # 6 periods
    result = make_result(frame, np.full(7, 10_000.0), n_trades=1, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=5, holding_periods=6)
    assert outcome.holding_periods == 6
    assert outcome.exposure == pytest.approx(1.0, rel=1e-12)
    # a single window, so every simulation takes the very same trade
    assert len(set(outcome.returns)) == 1


def test_missing_close_column_raises() -> None:
    frame = wiggle_frame(20).drop(columns=["close"])
    result = make_result(wiggle_frame(20), np.full(20, 10_000.0), n_trades=2, duration_minutes=60.0)
    with pytest.raises(MetricsError, match="close"):
        random_entry_benchmark(result, frame)


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
def test_to_dict_key_order_is_frozen() -> None:
    frame = wiggle_frame(25)
    result = make_result(frame, np.full(25, 10_000.0), n_trades=3, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=10, holding_periods=1)
    payload = outcome.to_dict()
    assert tuple(payload) == TO_DICT_KEYS
    assert list(outcome.percentiles) == list(PERCENTILE_LABELS)
    assert payload["variant"] == RANDOM_ENTRY_VARIANT
    assert (
        payload["n_simulations"] == len(payload["returns"]) == len(payload["final_balances"]) == 10
    )
    assert payload["returns"] == outcome.returns
    assert payload["final_balances"] == outcome.final_balances
    assert_json_native(payload)
    assert json.loads(json.dumps(payload, sort_keys=True))["percentile"] == outcome.percentile


def test_to_dict_caps_the_raw_arrays_like_monte_carlo() -> None:
    """The serialised payload truncates the raw arrays, keeping every scalar.

    Same convention as ``MonteCarloResult.to_dict``: dumping one float per
    simulation into a markdown report is unreadable, so the arrays are capped at
    ``SERIALISED_SIMULATIONS`` while the scalars stay complete.
    """
    frame = wiggle_frame(40)
    result = make_result(frame, np.full(40, 10_000.0), n_trades=4, duration_minutes=60.0)
    n = SERIALISED_SIMULATIONS + 5
    outcome = random_entry_benchmark(result, frame, n_simulations=n, holding_periods=1)
    assert len(outcome.returns) == n == len(outcome.final_balances)

    payload = outcome.to_dict()
    assert len(payload["returns"]) == SERIALISED_SIMULATIONS
    assert len(payload["final_balances"]) == SERIALISED_SIMULATIONS
    assert payload["returns"] == outcome.returns[:SERIALISED_SIMULATIONS]
    assert payload["n_simulations"] == n
    assert payload["mean_return"] == pytest.approx(outcome.mean_return)
    assert payload["percentile"] == pytest.approx(outcome.percentile)
    assert payload["p_value"] == pytest.approx(outcome.p_value)
    assert_json_native(payload)


def test_percentiles_are_the_expected_quantiles_of_the_returns() -> None:
    frame = wiggle_frame(40)
    result = make_result(frame, np.full(40, 10_000.0), n_trades=4, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=50, holding_periods=1)
    values = np.asarray(outcome.returns, dtype="float64")
    expected = np.percentile(values, [5.0, 25.0, 50.0, 75.0, 95.0])
    for label, value in zip(PERCENTILE_LABELS, expected, strict=True):
        assert outcome.percentiles[label] == pytest.approx(float(value), rel=1e-12)
    assert outcome.mean_return == pytest.approx(float(values.mean()), rel=1e-12)
    assert outcome.median_return == pytest.approx(float(np.median(values)), rel=1e-12)
    assert outcome.std_return == pytest.approx(float(values.std()), rel=1e-12)
    assert outcome.percentiles["p05"] <= outcome.percentiles["p50"] <= outcome.percentiles["p95"]


def test_histogram_shapes_and_counts() -> None:
    frame = wiggle_frame(30)
    result = make_result(frame, np.full(30, 10_000.0), n_trades=3, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=20, holding_periods=1)
    histogram = outcome.histogram(20)
    assert set(histogram) == {"edges", "counts"}
    assert len(histogram["edges"]) == 21
    assert len(histogram["counts"]) == 20
    assert sum(histogram["counts"]) == 20
    assert histogram["edges"] == sorted(histogram["edges"])
    assert_json_native(histogram)
    assert histogram == outcome.histogram()  # 20 bins is the default
    assert len(outcome.histogram(3)["counts"]) == 3


@pytest.mark.parametrize("bins", [0, -1])
def test_histogram_rejects_a_non_positive_bin_count(bins: int) -> None:
    frame = wiggle_frame(20)
    result = make_result(frame, np.full(20, 10_000.0), n_trades=2, duration_minutes=60.0)
    outcome = random_entry_benchmark(result, frame, n_simulations=5, holding_periods=1)
    with pytest.raises(MetricsError, match="bins"):
        outcome.histogram(bins)


def test_result_is_frozen_and_value_comparable() -> None:
    frame = wiggle_frame(25)
    result = make_result(frame, np.full(25, 10_000.0), n_trades=3, duration_minutes=60.0)
    first = random_entry_benchmark(result, frame, n_simulations=10, holding_periods=1)
    second = random_entry_benchmark(result, frame, n_simulations=10, holding_periods=1)
    other = random_entry_benchmark(
        result, frame, n_simulations=10, holding_periods=1, random_seed=7
    )

    assert isinstance(first, RandomEntryResult)
    assert first == second
    assert first != other
    assert first != "not a result"
    with pytest.raises(FrozenInstanceError):
        first.percentile = 0.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.returns = []  # type: ignore[misc]
