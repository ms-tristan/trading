"""Tests for the locked performance metric set (``trading_backtest.metrics.performance``).

The results are built **directly** from :class:`TradeRecord` /
:class:`BacktestResult` -- no strategy, engine or validation module is imported --
so the tests stay offline and independent from the other work packages.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.constants import OHLCV_INDEX_NAME, UTC
from trading_backtest.core.errors import ConfigError, MetricsError
from trading_backtest.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    TradeRecord,
)
from trading_backtest.metrics import (
    METRIC_NAMES,
    MetricSet,
    compute_metrics,
    metric_value,
    risk_free_rate_to_period,
)

PERIODS_PER_YEAR_1H = 8760.0

EXPECTED_METRIC_NAMES = (
    "total_return",
    "cagr",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "max_drawdown_duration",
    "calmar_ratio",
    "volatility",
    "win_rate",
    "profit_factor",
    "expectancy",
    "avg_trade_pnl",
    "avg_win",
    "avg_loss",
    "largest_win",
    "largest_loss",
    "n_trades",
    "exposure",
    "best_trade_pct",
    "worst_trade_pct",
    "recovery_factor",
    "total_fees",
    "final_balance",
)

#: Metrics that are meaningless without at least one trade.
TRADE_DEPENDENT = (
    "win_rate",
    "profit_factor",
    "expectancy",
    "avg_trade_pnl",
    "avg_win",
    "avg_loss",
    "largest_win",
    "largest_loss",
    "n_trades",
    "best_trade_pct",
    "worst_trade_pct",
    "total_fees",
)

BASE_TIMESTAMP = pd.Timestamp("2024-01-01T00:00:00Z")


def make_equity(values: list[float]) -> pd.Series:
    """Hourly equity curve starting 2024-01-01T00:00:00Z, one point per value."""
    index = pd.date_range(
        BASE_TIMESTAMP,
        periods=len(values),
        freq="h",
        tz=UTC,
        name=OHLCV_INDEX_NAME,
    )
    return pd.Series(
        [float(value) for value in values], index=index, name="equity", dtype="float64"
    )


def make_trade(
    *,
    entry_hour: int = 0,
    exit_hour: int = 1,
    pnl: float = 0.0,
    pnl_pct: float = 0.0,
    fees: float = 0.0,
    duration_minutes: float | None = None,
    direction: Direction = Direction.LONG,
    exit_reason: ExitReason = ExitReason.SIGNAL,
) -> TradeRecord:
    """Build a hand-controlled :class:`TradeRecord` on the shared hourly timeline."""
    entry_time = BASE_TIMESTAMP + pd.Timedelta(hours=entry_hour)
    exit_time = BASE_TIMESTAMP + pd.Timedelta(hours=exit_hour)
    if duration_minutes is None:
        duration_minutes = float((exit_time - entry_time) / pd.Timedelta(minutes=1))
    return TradeRecord(
        entry_time=entry_time,
        exit_time=exit_time,
        entry_price=100.0,
        exit_price=100.0 + pnl,
        size=1.0,
        direction=direction,
        pnl=float(pnl),
        pnl_pct=float(pnl_pct),
        fees=float(fees),
        exit_reason=exit_reason,
        duration_minutes=float(duration_minutes),
    )


def make_result(
    *,
    equity_values: list[float],
    trades: list[TradeRecord] | None = None,
    initial_balance: float = 1000.0,
    final_balance: float | None = None,
    timeframe: str = "1h",
) -> BacktestResult:
    """Build a :class:`BacktestResult` without touching any other layer."""
    index = pd.date_range(
        BASE_TIMESTAMP,
        periods=len(equity_values),
        freq="h",
        tz=UTC,
        name=OHLCV_INDEX_NAME,
    )
    equity = pd.Series(
        [float(value) for value in equity_values], index=index, name="equity", dtype="float64"
    )
    return BacktestResult(
        strategy_name="basic",
        symbol="BTC/USDT",
        timeframe=timeframe,
        start=index[0],
        end=index[-1],
        initial_balance=float(initial_balance),
        final_balance=float(equity_values[-1] if final_balance is None else final_balance),
        trades=list(trades or []),
        equity_curve=equity,
        params={},
        metadata={},
    )


def basic_result() -> BacktestResult:
    """One winner (+100) and one loser (-50) on a 4-candle curve ending at 1200."""
    return make_result(
        equity_values=[1000.0, 1100.0, 1050.0, 1200.0],
        trades=[
            make_trade(entry_hour=0, exit_hour=1, pnl=100.0, pnl_pct=0.1, fees=1.0),
            make_trade(
                entry_hour=1,
                exit_hour=2,
                pnl=-50.0,
                pnl_pct=-0.05,
                fees=1.0,
                direction=Direction.SHORT,
                exit_reason=ExitReason.STOP_LOSS,
            ),
        ],
    )


# --- METRIC_NAMES -----------------------------------------------------------


def test_metric_names_tuple_is_frozen() -> None:
    assert isinstance(METRIC_NAMES, tuple)
    assert METRIC_NAMES == EXPECTED_METRIC_NAMES
    assert len(METRIC_NAMES) == 23


# --- return / risk metrics --------------------------------------------------


def test_total_return_is_exact() -> None:
    metrics = compute_metrics(basic_result())

    assert metrics["total_return"] == pytest.approx(0.2)


def test_cagr_is_exact_when_the_curve_spans_exactly_one_year() -> None:
    # 8761 hourly points -> 8760 periods -> ppy / n_periods == 1 -> cagr == ratio - 1.
    values = list(np.linspace(1000.0, 1200.0, 8761))
    result = make_result(equity_values=values)

    metrics = compute_metrics(result, timeframe="1h")

    assert metrics["cagr"] == pytest.approx(0.2)
    assert metrics["total_return"] == pytest.approx(0.2)


def test_cagr_is_zero_on_a_single_candle_curve() -> None:
    result = make_result(equity_values=[1000.0])

    assert compute_metrics(result)["cagr"] == 0.0


def test_cagr_is_minus_one_when_the_final_balance_is_not_positive() -> None:
    result = make_result(equity_values=[1000.0, 900.0, 0.0], final_balance=-500.0)

    assert compute_metrics(result)["cagr"] == -1.0


def test_cagr_stays_finite_on_a_short_but_profitable_curve() -> None:
    metrics = compute_metrics(basic_result())

    assert math.isfinite(metrics["cagr"])


def test_volatility_matches_the_annualised_sample_standard_deviation() -> None:
    result = basic_result()
    returns = np.array([0.1, 1050.0 / 1100.0 - 1.0, 1200.0 / 1050.0 - 1.0])

    metrics = compute_metrics(result)

    expected = float(returns.std(ddof=1) * np.sqrt(PERIODS_PER_YEAR_1H))
    assert metrics["volatility"] == pytest.approx(expected, rel=1e-12)
    assert metrics["volatility"] > 0.0


def test_volatility_and_sharpe_are_zero_when_the_standard_deviation_is_zero() -> None:
    result = make_result(equity_values=[1000.0, 1000.0, 1000.0, 1000.0])

    metrics = compute_metrics(result)

    assert metrics["volatility"] == 0.0
    assert metrics["sharpe_ratio"] == 0.0


def test_volatility_and_sharpe_are_zero_with_fewer_than_two_returns() -> None:
    result = make_result(equity_values=[1000.0, 1100.0])

    metrics = compute_metrics(result)

    assert metrics["volatility"] == 0.0
    assert metrics["sharpe_ratio"] == 0.0
    assert metrics["sortino_ratio"] == 0.0


def test_sharpe_ratio_matches_the_locked_formula() -> None:
    result = basic_result()
    returns = np.array([0.1, 1050.0 / 1100.0 - 1.0, 1200.0 / 1050.0 - 1.0])

    metrics = compute_metrics(result)

    expected = float(returns.mean() * PERIODS_PER_YEAR_1H) / float(
        returns.std(ddof=1) * np.sqrt(PERIODS_PER_YEAR_1H)
    )
    assert metrics["sharpe_ratio"] == pytest.approx(expected, rel=1e-12)


def test_sharpe_ratio_with_a_non_zero_risk_free_rate() -> None:
    result = basic_result()
    free_metrics = compute_metrics(result)
    risky_metrics = compute_metrics(result, risk_free_rate=876.0)

    expected = free_metrics["sharpe_ratio"] - 876.0 / free_metrics["volatility"]
    assert risky_metrics["sharpe_ratio"] == pytest.approx(expected, rel=1e-12)
    assert risky_metrics["sharpe_ratio"] < free_metrics["sharpe_ratio"]


def test_sortino_ratio_matches_the_locked_formula() -> None:
    result = basic_result()
    returns = np.array([0.1, 1050.0 / 1100.0 - 1.0, 1200.0 / 1050.0 - 1.0])

    metrics = compute_metrics(result)

    downside = float(np.sqrt(np.mean(np.minimum(returns, 0.0) ** 2)) * np.sqrt(PERIODS_PER_YEAR_1H))
    expected = float(returns.mean() * PERIODS_PER_YEAR_1H) / downside
    assert metrics["sortino_ratio"] == pytest.approx(expected, rel=1e-12)
    assert metrics["sortino_ratio"] != metrics["sharpe_ratio"]


def test_sortino_ratio_is_zero_without_any_downside_return() -> None:
    result = make_result(equity_values=[1000.0, 1010.0, 1020.0, 1050.0])

    metrics = compute_metrics(result)

    assert metrics["sortino_ratio"] == 0.0
    assert metrics["sharpe_ratio"] > 0.0


def test_max_drawdown_and_duration_are_taken_from_the_drawdown_layer() -> None:
    result = make_result(equity_values=[1000.0, 1100.0, 1050.0, 900.0, 950.0, 1150.0])

    metrics = compute_metrics(result)

    assert metrics["max_drawdown"] == pytest.approx(900.0 / 1100.0 - 1.0, rel=1e-12)
    assert metrics["max_drawdown_duration"] == 3.0
    assert metrics["max_drawdown"] <= 0.0


def test_calmar_ratio_divides_cagr_by_the_absolute_max_drawdown() -> None:
    # A one-year ramp with a controlled dip: cagr is non-zero and so is the drawdown.
    values = np.linspace(1000.0, 1300.0, 8761)
    values[4000:4500] *= 0.8
    result = make_result(equity_values=list(values))

    metrics = compute_metrics(result)

    assert metrics["max_drawdown"] < 0.0
    expected = metrics["cagr"] / abs(metrics["max_drawdown"])
    assert metrics["calmar_ratio"] == pytest.approx(expected, rel=1e-12)


def test_calmar_ratio_is_zero_without_drawdown() -> None:
    result = make_result(equity_values=list(np.linspace(1000.0, 1300.0, 8761)))

    metrics = compute_metrics(result)

    assert metrics["max_drawdown"] == 0.0
    assert metrics["calmar_ratio"] == 0.0


# --- trade metrics ----------------------------------------------------------


def test_win_rate_on_mixed_winners_and_losers() -> None:
    assert compute_metrics(basic_result())["win_rate"] == pytest.approx(0.5)


def test_win_rate_is_one_when_every_trade_wins() -> None:
    result = make_result(
        equity_values=[1000.0, 1100.0],
        trades=[make_trade(pnl=10.0, pnl_pct=0.01), make_trade(pnl=20.0, pnl_pct=0.02)],
    )

    assert compute_metrics(result)["win_rate"] == 1.0


def test_win_rate_is_zero_when_every_trade_loses() -> None:
    result = make_result(
        equity_values=[1000.0, 900.0],
        trades=[make_trade(pnl=-10.0, pnl_pct=-0.01), make_trade(pnl=-20.0, pnl_pct=-0.02)],
    )

    assert compute_metrics(result)["win_rate"] == 0.0


def test_profit_factor_is_gross_profit_over_gross_loss() -> None:
    assert compute_metrics(basic_result())["profit_factor"] == pytest.approx(2.0)


def test_profit_factor_is_infinite_when_there_is_no_loss_at_all() -> None:
    result = make_result(
        equity_values=[1000.0, 1100.0],
        trades=[make_trade(pnl=10.0, pnl_pct=0.01), make_trade(pnl=20.0, pnl_pct=0.02)],
    )

    metrics = compute_metrics(result)

    assert metrics["profit_factor"] == float("inf")
    assert math.isinf(metrics["profit_factor"])


def test_profit_factor_is_zero_when_there_is_neither_profit_nor_loss() -> None:
    result = make_result(equity_values=[1000.0, 1000.0], trades=[make_trade(pnl=0.0)])

    assert compute_metrics(result)["profit_factor"] == 0.0


def test_expectancy_and_average_trade_statistics() -> None:
    metrics = compute_metrics(basic_result())

    assert metrics["expectancy"] == pytest.approx(25.0)
    assert metrics["avg_trade_pnl"] == pytest.approx(25.0)
    assert metrics["avg_win"] == pytest.approx(100.0)
    assert metrics["avg_loss"] == pytest.approx(-50.0)
    assert metrics["largest_win"] == pytest.approx(100.0)
    assert metrics["largest_loss"] == pytest.approx(-50.0)


def test_n_trades_total_fees_and_final_balance() -> None:
    metrics = compute_metrics(basic_result())

    assert metrics["n_trades"] == 2.0
    assert metrics["total_fees"] == pytest.approx(2.0)
    assert metrics["final_balance"] == pytest.approx(1200.0)


def test_best_and_worst_trade_pct() -> None:
    metrics = compute_metrics(basic_result())

    assert metrics["best_trade_pct"] == pytest.approx(0.1)
    assert metrics["worst_trade_pct"] == pytest.approx(-0.05)


def test_exposure_is_half_for_a_trade_spanning_half_of_the_timeline() -> None:
    # 11 hourly candles -> 600 minutes of timeline, one 300-minute trade.
    result = make_result(
        equity_values=[1000.0] * 11,
        trades=[make_trade(exit_hour=10, pnl=10.0, duration_minutes=300.0)],
    )

    assert compute_metrics(result)["exposure"] == pytest.approx(0.5)


def test_exposure_is_clamped_to_one() -> None:
    result = make_result(
        equity_values=[1000.0] * 11,
        trades=[make_trade(exit_hour=10, pnl=10.0, duration_minutes=1200.0)],
    )

    assert compute_metrics(result)["exposure"] == 1.0


def test_exposure_is_zero_without_a_timeline() -> None:
    result = make_result(
        equity_values=[1000.0], trades=[make_trade(pnl=10.0, duration_minutes=60.0)]
    )

    assert compute_metrics(result)["exposure"] == 0.0


def test_recovery_factor_is_net_profit_over_the_absolute_drawdown_amount() -> None:
    metrics = compute_metrics(basic_result())

    expected = (1200.0 - 1000.0) / abs(metrics["max_drawdown"] * 1000.0)
    assert metrics["recovery_factor"] == pytest.approx(expected, rel=1e-12)


def test_recovery_factor_is_zero_without_drawdown() -> None:
    result = make_result(equity_values=list(np.linspace(1000.0, 1200.0, 8761)))

    metrics = compute_metrics(result)

    assert metrics["max_drawdown"] == 0.0
    assert metrics["recovery_factor"] == 0.0


# --- degenerate results -----------------------------------------------------


def test_no_trades_result_returns_zero_for_every_trade_dependent_metric() -> None:
    result = make_result(equity_values=[1000.0, 1100.0, 1050.0])

    metrics = compute_metrics(result)

    for name in TRADE_DEPENDENT:
        assert metrics[name] == 0.0, name
    assert metrics["max_drawdown"] == pytest.approx(1050.0 / 1100.0 - 1.0)


def test_every_metric_is_finite_and_the_key_set_matches_metric_names() -> None:
    result = make_result(
        equity_values=[1000.0, 1100.0, 1050.0, 1200.0],
        trades=[
            make_trade(pnl=100.0, pnl_pct=0.1, fees=0.5),
            make_trade(pnl=-40.0, pnl_pct=-0.04, fees=0.5),
            make_trade(pnl=0.0, pnl_pct=0.0),
        ],
    )

    metrics = compute_metrics(result)

    assert tuple(metrics.keys()) == METRIC_NAMES
    assert set(metrics.keys()) == set(METRIC_NAMES)
    for name in METRIC_NAMES:
        value = metrics[name]
        assert isinstance(value, float), name
        if name == "profit_factor":
            continue
        assert math.isfinite(value), name


def test_metrics_do_not_raise_on_an_empty_equity_curve() -> None:
    index = pd.DatetimeIndex([], tz=UTC, name=OHLCV_INDEX_NAME)
    empty = pd.Series([], index=index, name="equity", dtype="float64")
    result = BacktestResult(
        strategy_name="basic",
        symbol="BTC/USDT",
        timeframe="1h",
        start=BASE_TIMESTAMP,
        end=BASE_TIMESTAMP,
        initial_balance=1000.0,
        final_balance=1000.0,
        trades=[],
        equity_curve=empty,
        params={},
        metadata={},
    )

    metrics = compute_metrics(result)

    assert metrics["max_drawdown"] == 0.0
    assert metrics["cagr"] == 0.0
    assert all(math.isfinite(metrics[name]) for name in METRIC_NAMES)


def test_zero_initial_balance_does_not_raise_and_stays_finite() -> None:
    result = make_result(equity_values=[0.0, 100.0, 200.0], initial_balance=0.0)

    metrics = compute_metrics(result)

    assert metrics["total_return"] == 0.0
    assert metrics["recovery_factor"] == 0.0
    assert all(math.isfinite(metrics[name]) for name in METRIC_NAMES)


def test_unsupported_timeframe_raises_config_error() -> None:
    with pytest.raises(ConfigError):
        compute_metrics(basic_result(), timeframe="7m")


# --- MetricSet --------------------------------------------------------------


def test_metric_set_getitem_raises_metrics_error_on_unknown_name() -> None:
    metrics = compute_metrics(basic_result())

    with pytest.raises(MetricsError) as error:
        metrics["not_a_metric"]

    message = str(error.value)
    assert "not_a_metric" in message
    assert "total_return" in message


def test_metric_set_get_returns_the_default_on_unknown_name() -> None:
    metrics = compute_metrics(basic_result())

    assert metrics.get("total_return") == pytest.approx(0.2)
    assert metrics.get("not_a_metric") is None
    assert metrics.get("not_a_metric", 1.5) == 1.5


def test_metric_set_as_dict_returns_a_copy() -> None:
    metrics = compute_metrics(basic_result())

    copied = metrics.as_dict()
    copied["total_return"] = 42.0

    assert metrics["total_return"] == pytest.approx(0.2)
    assert copied is not metrics.values


def test_metric_set_to_dict_is_sorted() -> None:
    metrics = MetricSet(values={"b": 2.0, "a": 1.0})

    assert metrics.to_dict() == {"values": {"a": 1.0, "b": 2.0}}
    assert list(metrics.to_dict()["values"]) == ["a", "b"]


def test_metric_set_mapping_protocol() -> None:
    metrics = compute_metrics(basic_result())

    assert len(metrics) == len(METRIC_NAMES)
    assert "sharpe_ratio" in metrics
    assert "not_a_metric" not in metrics
    assert list(iter(metrics)) == list(METRIC_NAMES)


# --- metric_value / risk_free_rate_to_period ---------------------------------


def test_metric_value_matches_compute_metrics() -> None:
    result = basic_result()

    assert metric_value(result, "sharpe_ratio") == pytest.approx(
        compute_metrics(result)["sharpe_ratio"]
    )


def test_metric_value_forwards_timeframe_and_risk_free_rate() -> None:
    result = basic_result()

    assert metric_value(result, "sharpe_ratio", risk_free_rate=100.0) == pytest.approx(
        compute_metrics(result, risk_free_rate=100.0)["sharpe_ratio"]
    )


def test_metric_value_raises_metrics_error_on_unknown_name() -> None:
    with pytest.raises(MetricsError) as error:
        metric_value(basic_result(), "nope")

    assert "nope" in str(error.value)


def test_risk_free_rate_to_period_for_one_hour() -> None:
    assert risk_free_rate_to_period(0.05, "1h") == pytest.approx(0.05 / PERIODS_PER_YEAR_1H)
    assert risk_free_rate_to_period(0.0, "1h") == 0.0


def test_risk_free_rate_to_period_rejects_an_unsupported_timeframe() -> None:
    with pytest.raises(ConfigError):
        risk_free_rate_to_period(0.05, "3h")
