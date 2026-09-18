"""Tests for the buy & hold benchmark (``trading_platform.metrics.benchmark``).

Everything here is offline and deterministic: the frames are synthetic and built
in the test, and no strategy/validation/reporting module is imported -- the
benchmark layer only talks to ``core`` and the sibling metric modules.
"""

from __future__ import annotations

import json
import math
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
from trading_platform.core.errors import ConfigError, MetricsError
from trading_platform.core.models import BacktestResult
from trading_platform.metrics import (
    BENCHMARK_METRIC_NAMES,
    BENCHMARK_VARIANTS,
    CURVE_VARIANTS,
    DEFAULT_BENCHMARK_VARIANT,
    MIN_RETURNS_FOR_BETA,
    BenchmarkComparison,
    BenchmarkResult,
    benchmark_alpha,
    buy_and_hold_equity,
    compare_benchmark,
    compute_benchmark,
    compute_metrics,
)

RAMP = (100.0, 110.0, 121.0)


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


def empty_frame() -> pd.DataFrame:
    """Return a frame with an OHLCV-shaped index but no candle."""
    index = pd.DatetimeIndex([], tz=UTC, name=OHLCV_INDEX_NAME)
    return pd.DataFrame(
        {
            "open": pd.Series([], dtype="float64", index=index),
            "high": pd.Series([], dtype="float64", index=index),
            "low": pd.Series([], dtype="float64", index=index),
            "close": pd.Series([], dtype="float64", index=index),
            "volume": pd.Series([], dtype="float64", index=index),
        }
    )


def make_strategy(
    frame: pd.DataFrame,
    values: Sequence[float],
    *,
    initial_balance: float = DEFAULT_INITIAL_BALANCE,
) -> BacktestResult:
    """Return a completed run whose equity curve is ``values`` over ``frame``."""
    index = pd.DatetimeIndex(frame.index)
    equity = pd.Series(np.asarray(values, dtype="float64"), index=index, name="equity")
    return BacktestResult(
        strategy_name="unit-test",
        symbol="BTC/USDT",
        timeframe="1h",
        start=pd.Timestamp(index[0]),
        end=pd.Timestamp(index[-1]),
        initial_balance=initial_balance,
        final_balance=float(values[-1]),
        trades=[],
        equity_curve=equity,
        params={},
    )


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


def rising_frame(n: int = 60) -> pd.DataFrame:
    """Return a deterministically rising frame with local dips (drawdowns)."""
    steps = np.arange(n, dtype="float64")
    closes = 100.0 + 3.0 * steps + 20.0 * np.sin(2.0 * np.pi * steps / 10.0)
    return make_frame(closes)


# ---------------------------------------------------------------------------
# constants & exports
# ---------------------------------------------------------------------------
def test_module_constants_are_frozen() -> None:
    assert BENCHMARK_VARIANTS == ("buy_and_hold", "cash", "risk_free", "random_entry", "none")
    assert CURVE_VARIANTS == ("buy_and_hold", "cash", "risk_free")
    assert DEFAULT_BENCHMARK_VARIANT == "buy_and_hold"
    assert BENCHMARK_METRIC_NAMES == (
        "total_return",
        "cagr",
        "volatility",
        "sharpe_ratio",
        "sortino_ratio",
        "max_drawdown",
        "max_drawdown_duration",
        "final_balance",
    )
    assert MIN_RETURNS_FOR_BETA == 3


def test_metrics_package_exposes_every_new_symbol() -> None:
    import trading_platform.metrics as metrics

    for name in (
        "BENCHMARK_METRIC_NAMES",
        "BENCHMARK_VARIANTS",
        "CURVE_VARIANTS",
        "DEFAULT_BENCHMARK_VARIANT",
        "DEFAULT_N_SIMULATIONS",
        "DEFAULT_RANDOM_SEED",
        "MAX_SIMULATIONS",
        "MIN_RETURNS_FOR_BETA",
        "PERCENTILE_LABELS",
        "RANDOM_ENTRY_VARIANT",
        "BenchmarkComparison",
        "BenchmarkResult",
        "RandomEntryResult",
        "benchmark_alpha",
        "buy_and_hold_equity",
        "compare_benchmark",
        "compute_benchmark",
        "random_entry_benchmark",
    ):
        assert name in metrics.__all__, name
        assert hasattr(metrics, name), name


# ---------------------------------------------------------------------------
# buy_and_hold_equity
# ---------------------------------------------------------------------------
def test_buy_and_hold_equity_matches_hand_computed_values() -> None:
    frame = make_frame(RAMP)
    fee = 0.001
    equity = buy_and_hold_equity(frame, initial_balance=10_000.0, fee_rate=fee)

    size = 10_000.0 / (100.0 * (1.0 + fee))
    assert equity.iloc[0] == size * 100.0
    assert equity.iloc[1] == size * 110.0
    assert equity.iloc[2] == size * 121.0 * (1.0 - fee)
    assert list(equity.index) == list(frame.index)
    assert equity.name == "equity"
    assert equity.dtype == np.float64
    assert equity.index.name == OHLCV_INDEX_NAME
    assert str(equity.index.tz) == UTC


def test_buy_and_hold_equity_without_costs_gives_the_exact_growth_multiple() -> None:
    frame = make_frame(RAMP)
    equity = buy_and_hold_equity(frame, initial_balance=10_000.0)
    assert equity.iloc[-1] / 10_000.0 == 1.21
    result = compute_benchmark(frame, initial_balance=10_000.0)
    assert result is not None
    assert result["final_balance"] == 12_100.0
    assert result["total_return"] == (12_100.0 / 10_000.0) - 1.0
    assert result["total_return"] == pytest.approx(0.21, rel=1e-12)


def test_entry_fee_is_charged_exactly_once() -> None:
    frame = make_frame(RAMP)
    fee = 0.001
    free = buy_and_hold_equity(frame, fee_rate=0.0)
    charged = buy_and_hold_equity(frame, fee_rate=fee)

    # entry fee only: the funded capital is divided by (1 + fee) exactly once, so
    # every point before the mark-to-market exit is the cost-free one scaled by a
    # single constant factor.
    for index in range(len(frame) - 1):
        assert charged.iloc[index] == pytest.approx(free.iloc[index] / (1.0 + fee), rel=1e-12)
    np.testing.assert_allclose(
        (charged / charged.iloc[0]).iloc[:-1].to_numpy(),
        (free / free.iloc[0]).iloc[:-1].to_numpy(),
        rtol=1e-12,
    )
    assert charged.iloc[-1] < free.iloc[-1] / (1.0 + fee)

    free_result = compute_benchmark(frame, fee_rate=0.0)
    charged_result = compute_benchmark(frame, fee_rate=fee)
    assert free_result is not None and charged_result is not None
    assert charged_result["total_return"] < free_result["total_return"]


def test_exit_costs_only_affect_the_last_point() -> None:
    frame = make_frame(RAMP)
    fee, slip = 0.002, 0.001
    equity = buy_and_hold_equity(frame, fee_rate=fee, slippage=slip)
    size = 10_000.0 / (100.0 * (1.0 + slip) * (1.0 + fee))
    closes = frame["close"].to_numpy(dtype="float64")

    # the mark-to-market exit adjustment is applied on the last candle only
    for index in range(len(frame) - 1):
        assert equity.iloc[index] == size * closes[index]
    assert equity.iloc[-1] == size * closes[-1] * (1.0 - slip) * (1.0 - fee)
    assert equity.iloc[-1] < size * closes[-1]


def test_exit_costs_are_applied_on_a_single_candle_frame() -> None:
    frame = make_frame((100.0,))
    fee = 0.001
    equity = buy_and_hold_equity(frame, initial_balance=10_000.0, fee_rate=fee)
    size = 10_000.0 / (100.0 * (1.0 + fee))
    assert len(equity) == 1
    assert equity.iloc[0] == size * 100.0 * (1.0 - fee)
    result = compute_benchmark(frame, fee_rate=fee)
    assert result is not None
    assert result.n_periods == 0
    assert result["total_return"] < 0.0


def test_cash_variant_is_a_flat_curve() -> None:
    frame = make_frame(RAMP)
    equity = buy_and_hold_equity(frame, initial_balance=5_000.0, variant="cash")
    assert (equity == 5_000.0).all()
    result = compute_benchmark(frame, variant="cash", initial_balance=5_000.0)
    assert result is not None
    assert result.variant == "cash"
    assert result["total_return"] == 0.0
    assert result["volatility"] == 0.0
    assert result["max_drawdown"] == 0.0
    assert result["final_balance"] == 5_000.0
    assert result.final_balance == 5_000.0


def test_cash_variant_ignores_fees_and_slippage() -> None:
    frame = make_frame(RAMP)
    plain = buy_and_hold_equity(frame, variant="cash")
    costly = buy_and_hold_equity(frame, variant="cash", fee_rate=0.01, slippage=0.02)
    pd.testing.assert_series_equal(plain, costly, check_names=True)


# ---------------------------------------------------------------------------
# risk_free variant
# ---------------------------------------------------------------------------
def test_curve_variants_are_exactly_the_passive_ones() -> None:
    assert set(CURVE_VARIANTS) < set(BENCHMARK_VARIANTS)
    assert set(BENCHMARK_VARIANTS) - set(CURVE_VARIANTS) == {"random_entry", "none"}


def test_risk_free_curve_compounds_the_annual_rate_on_every_candle() -> None:
    frame = make_frame(RAMP)
    period_rate = 0.05 / 8760.0  # one year of hourly candles
    equity = buy_and_hold_equity(
        frame, initial_balance=10_000.0, variant="risk_free", risk_free_rate=0.05
    )

    for position in range(len(RAMP)):
        expected = 10_000.0 * (1.0 + period_rate) ** position
        assert float(equity.iloc[position]) == pytest.approx(expected, rel=1e-12)
    assert float(equity.iloc[0]) == 10_000.0
    assert float(equity.iloc[-1]) > float(equity.iloc[0])
    assert list(equity.index) == list(frame.index)
    assert equity.name == "equity"
    assert equity.dtype == np.float64
    assert equity.index.name == OHLCV_INDEX_NAME
    assert str(equity.index.tz) == UTC


def test_risk_free_at_zero_rate_is_bit_identical_to_cash() -> None:
    frame = make_frame(RAMP)
    cash = buy_and_hold_equity(frame, initial_balance=5_000.0, variant="cash")
    free = buy_and_hold_equity(
        frame, initial_balance=5_000.0, variant="risk_free", risk_free_rate=0.0
    )
    pd.testing.assert_series_equal(cash, free, check_exact=True, check_names=True)
    assert (free == 5_000.0).all()


def test_risk_free_variant_ignores_fees_and_slippage() -> None:
    frame = make_frame(RAMP)
    plain = buy_and_hold_equity(frame, variant="risk_free", risk_free_rate=0.05)
    costly = buy_and_hold_equity(
        frame, variant="risk_free", risk_free_rate=0.05, fee_rate=0.01, slippage=0.02
    )
    pd.testing.assert_series_equal(plain, costly, check_names=True)


def test_cash_and_risk_free_differ_by_exactly_the_carry() -> None:
    frame = make_frame(RAMP)
    cash = compute_benchmark(frame, variant="cash", initial_balance=10_000.0)
    free = compute_benchmark(
        frame, variant="risk_free", initial_balance=10_000.0, risk_free_rate=0.05
    )
    assert cash is not None and free is not None
    assert cash["total_return"] == 0.0
    assert cash["final_balance"] == 10_000.0
    assert free["total_return"] > 0.0
    assert free["final_balance"] == pytest.approx(10_000.0 * (1.0 + 0.05 / 8760.0) ** 2, rel=1e-12)
    assert free["final_balance"] > cash["final_balance"]


def test_risk_free_on_a_full_year_is_a_zero_variance_curve() -> None:
    frame = make_frame(np.full(8761, 100.0))
    result = compute_benchmark(
        frame, variant="risk_free", risk_free_rate=0.05, initial_balance=10_000.0
    )
    assert result is not None
    compounded = (1.0 + 0.05 / 8760.0) ** 8760 - 1.0
    assert compounded == pytest.approx(0.05127095, abs=1e-8)

    assert result.n_periods == 8760
    assert result["total_return"] == pytest.approx(compounded, rel=1e-9)
    assert result["total_return"] == pytest.approx(0.05, abs=5e-3)
    assert result["cagr"] == pytest.approx(compounded, rel=1e-9)
    assert result["final_balance"] == pytest.approx(10_000.0 * (1.0 + compounded), rel=1e-9)
    assert result["final_balance"] == pytest.approx(10_500.0, rel=0.01)

    # a constant-rate curve has zero variance: these four are exactly 0.0, and the
    # variant's informative outputs are total_return / cagr / final_balance.
    assert result["volatility"] == 0.0
    assert result["sharpe_ratio"] == 0.0
    assert result["sortino_ratio"] == 0.0
    assert result["max_drawdown"] == 0.0
    assert result["max_drawdown_duration"] == 0


@pytest.mark.parametrize("rate", [-0.01, -1.0, float("nan"), float("inf"), float("-inf")])
def test_invalid_risk_free_rate_raises(rate: float) -> None:
    frame = make_frame(RAMP)
    with pytest.raises(MetricsError, match="risk_free_rate"):
        buy_and_hold_equity(frame, variant="risk_free", risk_free_rate=rate)
    with pytest.raises(MetricsError, match="risk_free_rate"):
        compute_benchmark(frame, variant="risk_free", risk_free_rate=rate)


def test_risk_free_rate_is_ignored_by_the_other_curve_variants() -> None:
    frame = make_frame(RAMP)
    plain = buy_and_hold_equity(frame, variant="buy_and_hold")
    priced = buy_and_hold_equity(frame, variant="buy_and_hold", risk_free_rate=0.05)
    pd.testing.assert_series_equal(plain, priced, check_exact=True, check_names=True)


def test_risk_free_variant_propagates_config_error_for_an_unsupported_timeframe() -> None:
    frame = make_frame(RAMP)
    with pytest.raises(ConfigError):
        buy_and_hold_equity(frame, variant="risk_free", risk_free_rate=0.05, timeframe="7h")
    with pytest.raises(ConfigError):
        compute_benchmark(frame, variant="risk_free", risk_free_rate=0.05, timeframe="7h")


def test_beta_is_none_for_a_risk_free_benchmark_curve() -> None:
    frame = rising_frame(30)
    strategy = make_strategy(frame, np.linspace(10_000.0, 12_000.0, 30))
    for rate in (0.0, 0.05):
        comparison = compare_benchmark(strategy, frame, variant="risk_free", risk_free_rate=rate)
        assert comparison is not None
        assert comparison.n_returns == len(frame) - 1
        assert comparison.n_returns >= MIN_RETURNS_FOR_BETA
        assert comparison.beta is None
        assert comparison.correlation is None
        assert comparison.benchmark["volatility"] == 0.0


def test_risk_free_rate_only_moves_the_sharpe_of_a_volatile_curve() -> None:
    frame = rising_frame(40)
    free = compute_benchmark(frame)
    with_rate = compute_benchmark(frame, risk_free_rate=0.05)
    assert free is not None and with_rate is not None
    # the documented property: subtracting the risk-free rate lowers the Sharpe of
    # a curve that actually has variance (buy & hold).
    assert with_rate["sharpe_ratio"] < free["sharpe_ratio"]

    # ... but it cannot move the risk_free variant's own Sharpe: its variance is
    # exactly zero, so the value stays 0.0 at any rate.
    zero = compute_benchmark(frame, variant="risk_free", risk_free_rate=0.0)
    priced = compute_benchmark(frame, variant="risk_free", risk_free_rate=0.05)
    assert zero is not None and priced is not None
    assert zero["sharpe_ratio"] == 0.0
    assert priced["sharpe_ratio"] == 0.0


@pytest.mark.parametrize("entrypoint", ["equity", "compute", "compare"])
def test_random_entry_variant_has_no_curve_and_points_at_its_distribution(
    entrypoint: str,
) -> None:
    frame = make_frame(RAMP)
    strategy = make_strategy(frame, (10_000.0, 11_000.0, 12_000.0))
    with pytest.raises(MetricsError) as excinfo:
        if entrypoint == "equity":
            buy_and_hold_equity(frame, variant="random_entry")
        elif entrypoint == "compute":
            compute_benchmark(frame, variant="random_entry")
        else:
            compare_benchmark(strategy, frame, variant="random_entry")
    message = str(excinfo.value)
    assert "random_entry" in message
    assert "random_entry_benchmark" in message
    for curve_variant in CURVE_VARIANTS:
        assert curve_variant in message
    assert "no deterministic equity curve" in message


def test_none_variant_returns_none_and_has_no_curve() -> None:
    frame = make_frame(RAMP)
    assert compute_benchmark(frame, variant="none") is None
    assert (
        compare_benchmark(
            make_strategy(frame, (10_000.0, 11_000.0, 12_000.0)), frame, variant="none"
        )
        is None
    )
    with pytest.raises(MetricsError) as excinfo:
        buy_and_hold_equity(frame, variant="none")
    message = str(excinfo.value)
    for variant in BENCHMARK_VARIANTS:
        assert variant in message


@pytest.mark.parametrize("entrypoint", ["equity", "compute", "compare"])
def test_unknown_variant_raises_metrics_error_listing_variants(entrypoint: str) -> None:
    frame = make_frame(RAMP)
    strategy = make_strategy(frame, (10_000.0, 11_000.0, 12_000.0))
    with pytest.raises(MetricsError) as excinfo:
        if entrypoint == "equity":
            buy_and_hold_equity(frame, variant="hodl")  # type: ignore[arg-type]
        elif entrypoint == "compute":
            compute_benchmark(frame, variant="hodl")  # type: ignore[arg-type]
        else:
            compare_benchmark(strategy, frame, variant="hodl")  # type: ignore[arg-type]
    message = str(excinfo.value)
    for variant in BENCHMARK_VARIANTS:
        assert variant in message


# ---------------------------------------------------------------------------
# input validation & index normalisation
# ---------------------------------------------------------------------------
def test_missing_close_column_raises() -> None:
    frame = make_frame(RAMP).drop(columns=["close"])
    with pytest.raises(MetricsError, match="close"):
        buy_and_hold_equity(frame)


def test_non_numeric_close_raises() -> None:
    frame = make_frame(RAMP).assign(close=["a", "b", "c"])
    with pytest.raises(MetricsError, match="numeric"):
        compute_benchmark(frame)


def test_non_frame_input_raises() -> None:
    with pytest.raises(MetricsError, match="DataFrame"):
        buy_and_hold_equity([1.0, 2.0, 3.0])  # type: ignore[arg-type]


def test_empty_frame_raises() -> None:
    with pytest.raises(MetricsError, match="at least one candle"):
        buy_and_hold_equity(empty_frame())


def test_all_nan_closes_raise() -> None:
    frame = make_frame((100.0, 110.0, 121.0)).assign(close=[np.nan, np.nan, np.nan])
    with pytest.raises(MetricsError, match="positive first close"):
        buy_and_hold_equity(frame)


@pytest.mark.parametrize("first", [0.0, -5.0])
def test_non_positive_first_close_raises(first: float) -> None:
    frame = make_frame((first, 110.0, 121.0))
    with pytest.raises(MetricsError, match="positive first close"):
        compute_benchmark(frame)


def test_nan_closes_are_forward_filled() -> None:
    frame = make_frame((100.0, 110.0, 121.0)).assign(close=[100.0, np.nan, 121.0])
    equity = buy_and_hold_equity(frame)
    size = 10_000.0 / 100.0
    assert equity.iloc[1] == size * 100.0
    assert equity.iloc[2] == size * 121.0


def test_index_is_normalised_to_utc_and_deduplicated() -> None:
    index = pd.DatetimeIndex(
        ["2024-01-01T02:00:00", "2024-01-01T00:00:00", "2024-01-01T01:00:00", "2024-01-01T02:00:00"]
    )
    frame = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=index)
    equity = buy_and_hold_equity(frame)

    assert isinstance(equity.index, pd.DatetimeIndex)
    assert str(equity.index.tz) == UTC
    assert equity.index.name == OHLCV_INDEX_NAME
    assert equity.index.is_monotonic_increasing
    assert not equity.index.has_duplicates
    assert len(equity) == 3
    size = 10_000.0 / 2.0  # last 02:00 candle wins the duplicate, closes become 2/3/4
    assert list(equity) == [size * 2.0, size * 3.0, size * 4.0]


# ---------------------------------------------------------------------------
# compute_benchmark
# ---------------------------------------------------------------------------
def test_benchmark_result_is_well_formed() -> None:
    frame = make_frame(RAMP)
    result = compute_benchmark(frame, initial_balance=10_000.0, symbol="BTC/USDT")
    assert result is not None
    assert isinstance(result, BenchmarkResult)
    assert result.variant == "buy_and_hold"
    assert result.initial_balance == 10_000.0
    assert result.final_balance == pytest.approx(12_100.0, rel=1e-12)
    assert result.n_periods == 2
    assert result.timeframe == "1h"
    assert tuple(result.keys()) == BENCHMARK_METRIC_NAMES
    assert list(result) == list(BENCHMARK_METRIC_NAMES)
    assert len(result) == len(BENCHMARK_METRIC_NAMES)
    assert "cagr" in result
    assert "not_a_metric" not in result
    assert result.get("cagr") == result["cagr"]
    assert result.get("not_a_metric") is None
    assert result.get("not_a_metric", 1.5) == 1.5
    assert result.equity_curve.name == "equity"
    assert result.equity_curve.dtype == np.float64
    assert result.equity_curve.index.name == OHLCV_INDEX_NAME


def test_benchmark_result_as_dict_is_a_copy() -> None:
    result = compute_benchmark(make_frame(RAMP))
    assert result is not None
    payload = result.as_dict()
    assert payload == result.metrics
    payload["total_return"] = 123.0
    assert result["total_return"] != 123.0


def test_benchmark_result_to_dict_has_no_equity_curve() -> None:
    result = compute_benchmark(make_frame(RAMP))
    assert result is not None
    payload = result.to_dict()
    assert set(payload) == {
        "variant",
        "initial_balance",
        "final_balance",
        "n_periods",
        "timeframe",
        "metrics",
    }
    assert "equity_curve" not in payload
    assert payload["metrics"] == result.metrics
    assert_json_native(payload)
    json.dumps(payload, sort_keys=True)


def test_benchmark_result_getitem_unknown_metric_lists_metric_names() -> None:
    result = compute_benchmark(make_frame(RAMP))
    assert result is not None
    with pytest.raises(MetricsError) as excinfo:
        result["n_trades"]
    message = str(excinfo.value)
    for name in BENCHMARK_METRIC_NAMES:
        assert name in message


def test_benchmark_result_is_frozen_and_hashable() -> None:
    result = compute_benchmark(make_frame(RAMP))
    assert result is not None
    with pytest.raises(FrozenInstanceError):
        result.variant = "cash"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.metrics = {}  # type: ignore[misc]
    assert isinstance(hash(result), int)


def test_benchmark_result_equality_compares_the_curve() -> None:
    frame = make_frame(RAMP)
    first = compute_benchmark(frame)
    second = compute_benchmark(frame)
    assert first is not None and second is not None
    assert first == second
    assert first != "not a result"
    other = compute_benchmark(frame, variant="cash")
    assert other is not None
    assert first != other

    # same scalars and same metrics, different curve -> not equal
    scaled = BenchmarkResult(
        variant=first.variant,
        initial_balance=first.initial_balance,
        final_balance=first.final_balance,
        n_periods=first.n_periods,
        timeframe=first.timeframe,
        metrics=first.as_dict(),
        equity_curve=first.equity_curve * 2.0,
    )
    assert first != scaled


def test_compute_benchmark_is_deterministic() -> None:
    frame = rising_frame(30)
    first = compute_benchmark(frame, fee_rate=0.001, slippage=0.0005)
    second = compute_benchmark(frame, fee_rate=0.001, slippage=0.0005)
    assert first is not None and second is not None
    pd.testing.assert_series_equal(first.equity_curve, second.equity_curve)
    assert first.metrics == second.metrics


def test_rising_frame_beats_nothing_and_draws_down() -> None:
    frame = rising_frame(60)
    result = compute_benchmark(frame)
    assert result is not None
    assert result["total_return"] > 0.0
    assert result["final_balance"] > result.initial_balance
    assert result["max_drawdown"] <= 0.0
    assert result["max_drawdown"] < 0.0  # the synthetic ramp has local dips
    assert result["volatility"] >= 0.0


def test_benchmark_forwards_risk_free_rate_to_the_metric_set() -> None:
    frame = rising_frame(40)
    free = compute_benchmark(frame)
    with_rate = compute_benchmark(frame, risk_free_rate=0.05)
    assert free is not None and with_rate is not None
    assert with_rate["sharpe_ratio"] < free["sharpe_ratio"]


def test_unsupported_timeframe_propagates_config_error() -> None:
    frame = make_frame(RAMP)
    with pytest.raises(ConfigError):
        compute_benchmark(frame, timeframe="7h")


# ---------------------------------------------------------------------------
# alpha
# ---------------------------------------------------------------------------
def test_benchmark_alpha_is_a_fraction() -> None:
    assert benchmark_alpha(0.5, 0.2) == pytest.approx(0.3, rel=1e-15)
    assert benchmark_alpha(0.2, 0.5) == pytest.approx(-0.3, rel=1e-15)
    assert benchmark_alpha(0.25, 0.25) == 0.0


def test_alpha_sign_and_magnitude_against_a_known_strategy() -> None:
    frame = make_frame(RAMP)
    winning = make_strategy(frame, (10_000.0, 10_500.0, 12_650.0))
    comparison = compare_benchmark(winning, frame)
    assert comparison is not None
    assert comparison.benchmark["total_return"] == pytest.approx(0.21, rel=1e-12)
    assert comparison.strategy["total_return"] == pytest.approx(0.265, rel=1e-12)
    assert comparison.alpha == pytest.approx(0.055, rel=1e-9)
    assert comparison.alpha == pytest.approx(
        comparison.strategy["total_return"] - comparison.benchmark["total_return"], rel=1e-15
    )

    losing = make_strategy(frame, (10_000.0, 9_000.0, 9_500.0))
    loser_comparison = compare_benchmark(losing, frame)
    assert loser_comparison is not None
    assert loser_comparison.alpha < 0.0
    assert loser_comparison.alpha == pytest.approx(-0.05 - 0.21, rel=1e-9)


# ---------------------------------------------------------------------------
# compare_benchmark
# ---------------------------------------------------------------------------
def test_compare_benchmark_uses_the_strategy_window_and_capital() -> None:
    frame = rising_frame(40)
    strategy = make_strategy(frame, np.linspace(10_000.0, 12_000.0, 40), initial_balance=10_000.0)
    comparison = compare_benchmark(strategy, frame, fee_rate=0.001)
    assert comparison is not None
    assert comparison.variant == "buy_and_hold"
    assert comparison.timeframe == "1h"
    assert comparison.initial_balance == 10_000.0
    assert comparison.benchmark.initial_balance == 10_000.0
    assert comparison.fee_rate == 0.001
    assert comparison.slippage == 0.0
    assert comparison.n_periods == len(frame) - 1
    assert comparison.n_returns == len(frame) - 1
    assert comparison.strategy["final_balance"] == 12_000.0
    assert comparison.benchmark.final_balance == comparison.benchmark.equity_curve.iloc[-1]
    pd.testing.assert_series_equal(comparison.strategy.equity_curve, strategy.equity_curve)


def test_compare_benchmark_accepts_precomputed_strategy_metrics() -> None:
    frame = rising_frame(30)
    strategy = make_strategy(frame, np.linspace(10_000.0, 11_000.0, 30))
    default = compare_benchmark(strategy, frame)
    precomputed = compare_benchmark(
        strategy, frame, strategy_metrics=compute_metrics(strategy, timeframe="1h")
    )
    assert default is not None and precomputed is not None
    assert default.to_dict() == precomputed.to_dict()


def test_compare_benchmark_initial_balance_override() -> None:
    frame = rising_frame(20)
    strategy = make_strategy(frame, np.linspace(10_000.0, 11_000.0, 20))
    comparison = compare_benchmark(
        strategy, frame, variant="cash", initial_balance=5_000.0, slippage=0.0005
    )
    assert comparison is not None
    assert comparison.initial_balance == 5_000.0
    assert comparison.benchmark.initial_balance == 5_000.0
    assert comparison.benchmark.final_balance == 5_000.0
    assert comparison.slippage == 0.0005
    assert comparison.strategy.initial_balance == 10_000.0


def test_beta_and_correlation_are_one_against_an_identical_curve() -> None:
    frame = rising_frame(30)
    benchmark = compute_benchmark(frame)
    assert benchmark is not None
    strategy = make_strategy(frame, benchmark.equity_curve.to_numpy(dtype="float64"))
    comparison = compare_benchmark(strategy, frame)
    assert comparison is not None
    assert comparison.beta == pytest.approx(1.0, rel=1e-12)
    assert comparison.correlation == pytest.approx(1.0, rel=1e-12)
    assert comparison.alpha == pytest.approx(0.0, abs=1e-12)


def test_beta_is_none_when_the_benchmark_has_no_variance() -> None:
    frame = rising_frame(30)
    strategy = make_strategy(frame, np.linspace(10_000.0, 12_000.0, 30))
    comparison = compare_benchmark(strategy, frame, variant="cash")
    assert comparison is not None
    assert comparison.n_returns == len(frame) - 1
    assert comparison.beta is None
    assert comparison.correlation is None


def test_beta_is_none_below_the_minimum_number_of_returns() -> None:
    frame = make_frame((100.0, 110.0))
    strategy = make_strategy(frame, (10_000.0, 11_000.0))
    comparison = compare_benchmark(strategy, frame)
    assert comparison is not None
    assert comparison.n_returns == 1
    assert comparison.n_returns < MIN_RETURNS_FOR_BETA
    assert comparison.beta is None
    assert comparison.correlation is None

    longer = make_frame((100.0, 105.0, 110.0, 121.0))
    strategy = make_strategy(longer, (10_000.0, 10_200.0, 10_400.0, 11_000.0))
    computed = compare_benchmark(strategy, longer)
    assert computed is not None
    assert computed.n_returns == 3
    assert computed.beta is not None
    assert computed.correlation is not None


def test_non_finite_returns_are_filtered_out() -> None:
    frame = make_frame((100.0, 110.0, 121.0, 90.0, 95.0, 99.0))
    strategy = make_strategy(frame, (10_000.0, 5_000.0, 0.0, 100.0, 200.0, 300.0))
    comparison = compare_benchmark(strategy, frame)
    assert comparison is not None
    # 5 percentage changes: one NaN (first point) and one +inf (recovery from 0).
    assert comparison.n_returns == 4
    assert comparison.beta is not None and math.isfinite(comparison.beta)
    assert comparison.correlation is not None and math.isfinite(comparison.correlation)
    payload = comparison.to_dict()
    assert_json_native(payload)
    json.dumps(payload, sort_keys=True)


def test_gap_is_the_row_by_row_difference() -> None:
    frame = rising_frame(25)
    strategy = make_strategy(frame, np.linspace(10_000.0, 12_500.0, 25))
    comparison = compare_benchmark(strategy, frame, fee_rate=0.001)
    assert comparison is not None
    gap = comparison.gap()
    assert list(gap) == list(BENCHMARK_METRIC_NAMES)
    for name in BENCHMARK_METRIC_NAMES:
        assert gap[name] == comparison.strategy[name] - comparison.benchmark[name]
    assert comparison.gap() == gap


def test_comparison_rows_are_three_flat_json_native_rows() -> None:
    frame = rising_frame(25)
    strategy = make_strategy(frame, np.linspace(10_000.0, 12_500.0, 25))
    comparison = compare_benchmark(strategy, frame)
    assert comparison is not None
    rows = comparison.comparison_rows()
    assert len(rows) == 3
    assert [row["variant"] for row in rows] == ["strategy", "buy_and_hold", "gap"]
    for row in rows:
        assert list(row) == ["variant", *BENCHMARK_METRIC_NAMES]
        assert_json_native(row)
    assert rows[0] == {"variant": "strategy", **comparison.strategy.metrics}
    assert rows[1] == {"variant": "buy_and_hold", **comparison.benchmark.metrics}
    assert rows[2] == {"variant": "gap", **comparison.gap()}
    assert rows[2]["total_return"] == pytest.approx(comparison.alpha, rel=1e-12)


def test_cash_comparison_rows_label_the_variant_literally() -> None:
    frame = rising_frame(15)
    strategy = make_strategy(frame, np.linspace(10_000.0, 10_500.0, 15))
    comparison = compare_benchmark(strategy, frame, variant="cash")
    assert comparison is not None
    assert comparison.comparison_rows()[1]["variant"] == "cash"


def test_comparison_to_dict_is_json_serialisable() -> None:
    frame = rising_frame(25)
    strategy = make_strategy(frame, np.linspace(10_000.0, 12_500.0, 25))
    comparison = compare_benchmark(strategy, frame, fee_rate=0.001, slippage=0.0005)
    assert comparison is not None
    payload = comparison.to_dict()
    assert set(payload) == {
        "variant",
        "timeframe",
        "initial_balance",
        "fee_rate",
        "slippage",
        "n_periods",
        "n_returns",
        "alpha",
        "beta",
        "correlation",
        "strategy",
        "benchmark",
        "gap",
    }
    assert list(payload["strategy"]) == list(BENCHMARK_METRIC_NAMES)
    assert list(payload["benchmark"]) == list(BENCHMARK_METRIC_NAMES)
    assert payload["gap"] == comparison.gap()
    assert payload["alpha"] == comparison.alpha
    assert "equity_curve" not in payload
    assert_json_native(payload)
    restored = json.loads(json.dumps(payload, sort_keys=True))
    assert restored["variant"] == "buy_and_hold"
    assert restored["strategy"]["total_return"] == pytest.approx(
        comparison.strategy["total_return"], rel=1e-15
    )


def test_comparison_to_dict_keeps_none_for_uncomputable_beta() -> None:
    frame = make_frame((100.0, 110.0))
    strategy = make_strategy(frame, (10_000.0, 11_000.0))
    comparison = compare_benchmark(strategy, frame)
    assert comparison is not None
    payload = comparison.to_dict()
    assert payload["beta"] is None and payload["correlation"] is None
    assert json.loads(json.dumps(payload, sort_keys=True))["beta"] is None


def test_comparison_is_frozen_and_hashable() -> None:
    frame = make_frame(RAMP)
    strategy = make_strategy(frame, (10_000.0, 11_000.0, 12_000.0))
    comparison = compare_benchmark(strategy, frame)
    assert comparison is not None
    assert isinstance(comparison, BenchmarkComparison)
    with pytest.raises(FrozenInstanceError):
        comparison.alpha = 0.0  # type: ignore[misc]
    assert isinstance(hash(comparison), int)


def test_comparison_equality_is_value_based() -> None:
    frame = rising_frame(20)
    strategy = make_strategy(frame, np.linspace(10_000.0, 11_000.0, 20))
    first = compare_benchmark(strategy, frame)
    second = compare_benchmark(strategy, frame)
    assert first is not None and second is not None
    assert first == second
    assert first.to_dict() == second.to_dict()
