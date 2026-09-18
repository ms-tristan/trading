"""Engine tests: every locked execution rule of ``run_backtest`` is pinned here.

The strategy used is scripted (the exact candle of every signal is chosen by the
test), so the expected fills, fees, sizes and exit reasons are derived by hand
from the rules stated in :mod:`trading_platform.strategy.engine`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.constants import SIGNAL_COLUMNS
from trading_platform.core.errors import InsufficientDataError, StrategyError
from trading_platform.core.models import BacktestResult, Direction, ExitReason
from trading_platform.strategy.base import Strategy, StrategyParams, require_ohlcv_frame
from trading_platform.strategy.basic import BasicStrategy
from trading_platform.strategy.engine import make_runner, run_backtest, run_backtest_on_config

START = "2024-01-01T00:00:00Z"

#: 10 candles: opens 100..109 (+1), closes open+0.5, one-hour steps.
OPENS = [100.0, 101, 102, 103, 104, 105, 106, 107, 108, 109]
CLOSES = [value + 0.5 for value in OPENS]

INITIAL_BALANCE = 10_000.0
FEE_RATE = 0.001
SLIPPAGE = 0.01


def _frame(
    opens: Sequence[float] = tuple(OPENS),
    closes: Sequence[float] = tuple(CLOSES),
    *,
    highs: Sequence[float] | None = None,
    lows: Sequence[float] | None = None,
) -> pd.DataFrame:
    """Build a deterministic OHLCV frame from ``opens``/``closes``."""
    open_values = np.asarray(opens, dtype="float64")
    close_values = np.asarray(closes, dtype="float64")
    high_values = (
        np.maximum(open_values, close_values) + 0.5
        if highs is None
        else np.asarray(highs, dtype="float64")
    )
    low_values = (
        np.minimum(open_values, close_values) - 0.5
        if lows is None
        else np.asarray(lows, dtype="float64")
    )
    index = pd.date_range(START, periods=open_values.size, freq="h", tz="UTC", name="timestamp")
    return pd.DataFrame(
        {
            "open": open_values,
            "high": high_values,
            "low": low_values,
            "close": close_values,
            "volume": np.full(open_values.size, 10.0),
        },
        index=index,
    )


class _ScriptedParams(StrategyParams):
    """Parameters of the scripted strategy (only there to be echoed back)."""

    marker: int = 7


class ScriptedStrategy(Strategy):
    """Strategy whose signals are chosen row by row by the test."""

    name: ClassVar[str] = "scripted"
    ParamsModel: ClassVar[type[StrategyParams]] = _ScriptedParams

    def __init__(
        self,
        *,
        entry_long: Sequence[int] = (),
        exit_long: Sequence[int] = (),
        entry_short: Sequence[int] = (),
        exit_short: Sequence[int] = (),
        stop_loss: Mapping[int, float] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(params)
        self._rows: dict[str, tuple[int, ...]] = {
            "entry_long": tuple(entry_long),
            "exit_long": tuple(exit_long),
            "entry_short": tuple(entry_short),
            "exit_short": tuple(exit_short),
        }
        self._stops: dict[int, float] = dict(stop_loss or {})

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        return require_ohlcv_frame(data, name="data")

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        size = len(data)
        columns: dict[str, Any] = {}
        for name, rows in self._rows.items():
            values = np.zeros(size, dtype=bool)
            for row in rows:
                values[row] = True
            columns[name] = values
        stops = np.full(size, np.nan)
        for row, value in self._stops.items():
            stops[row] = value
        columns["stop_loss"] = stops
        return pd.DataFrame(columns, index=data.index)[list(SIGNAL_COLUMNS)]


class _WrongColumnsStrategy(ScriptedStrategy):
    """Strategy breaking the signal contract on purpose."""

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"entry_long": np.zeros(len(data), dtype=bool)}, index=data.index)


# ---------------------------------------------------------------------------
# fills, sizing, fees
# ---------------------------------------------------------------------------


def test_entry_fills_at_the_next_open_with_slippage_and_fees_on_both_legs() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_long=[1], exit_long=[4])

    result = run_backtest(
        strategy,
        data,
        initial_balance=INITIAL_BALANCE,
        fee_rate=FEE_RATE,
        slippage=SLIPPAGE,
    )

    entry_price = 102.0 * (1.0 + SLIPPAGE)  # open of candle 2 (signal on candle 1)
    exit_price = 105.0 * (1.0 - SLIPPAGE)  # open of candle 5 (signal on candle 4)
    size = INITIAL_BALANCE / (entry_price * (1.0 + FEE_RATE))
    fees = FEE_RATE * size * (entry_price + exit_price)
    pnl = size * (exit_price - entry_price) - fees

    assert result.n_trades == 1
    trade = result.trades[0]
    assert trade.entry_time == data.index[2]
    assert trade.exit_time == data.index[5]
    assert trade.entry_price == pytest.approx(entry_price)
    assert trade.exit_price == pytest.approx(exit_price)
    assert trade.direction is Direction.LONG
    assert trade.exit_reason is ExitReason.SIGNAL
    assert trade.size == pytest.approx(size)
    assert trade.fees == pytest.approx(fees)
    assert trade.pnl == pytest.approx(pnl)
    assert trade.pnl_pct == pytest.approx(pnl / (size * entry_price))
    assert trade.duration_minutes == pytest.approx(180.0)
    assert trade.stop_price is None
    assert trade.take_profit_price is None

    # slippage always works against the trade
    assert trade.entry_price > float(data["open"].iloc[2])
    assert trade.exit_price < float(data["open"].iloc[5])

    assert result.final_balance == pytest.approx(INITIAL_BALANCE + pnl)


def test_equity_curve_is_marked_to_market_on_every_candle() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_long=[1], exit_long=[4])

    result = run_backtest(
        strategy, data, initial_balance=INITIAL_BALANCE, fee_rate=FEE_RATE, slippage=SLIPPAGE
    )

    entry_price = 102.0 * (1.0 + SLIPPAGE)
    size = INITIAL_BALANCE / (entry_price * (1.0 + FEE_RATE))
    curve = result.equity_curve

    assert len(curve) == len(data)
    assert curve.index.equals(data.index)
    assert curve.name == "equity"
    assert curve.dtype == np.dtype("float64")
    assert float(curve.iloc[0]) == pytest.approx(INITIAL_BALANCE)
    # while the position is open the curve marks the close of the candle
    assert float(curve.iloc[2]) == pytest.approx(
        INITIAL_BALANCE + size * (float(data["close"].iloc[2]) - entry_price)
    )
    assert float(curve.iloc[1]) == pytest.approx(INITIAL_BALANCE)  # still flat
    assert float(curve.iloc[-1]) == pytest.approx(result.final_balance)
    assert result.start == data.index[0]
    assert result.end == data.index[-1]


def test_stake_amount_switches_from_full_equity_to_fixed_sizing() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_long=[1], exit_long=[4])

    full_equity = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)
    fixed = run_backtest(strategy, data, stake_amount=1000.0, fee_rate=0.0, slippage=0.0)

    assert full_equity.trades[0].size == pytest.approx(INITIAL_BALANCE / 102.0)
    assert fixed.trades[0].size == pytest.approx(1000.0 / 102.0)
    assert fixed.metadata["stake_amount"] == pytest.approx(1000.0)
    assert full_equity.metadata["stake_amount"] is None
    assert fixed.final_balance != pytest.approx(full_equity.final_balance)


def test_a_zero_stake_amount_is_rejected() -> None:
    with pytest.raises(StrategyError, match="stake_amount"):
        run_backtest(ScriptedStrategy(), _frame(), stake_amount=0.0)


def test_a_non_positive_entry_price_is_rejected() -> None:
    opens = list(OPENS)
    opens[2] = 0.0  # the candle the entry would fill on
    data = _frame(opens)

    with pytest.raises(StrategyError, match="non-positive price"):
        run_backtest(ScriptedStrategy(entry_long=[1]), data, slippage=0.0)


# ---------------------------------------------------------------------------
# exits
# ---------------------------------------------------------------------------


def test_intrabar_stop_fills_at_the_static_stop_price() -> None:
    # the stop column changes after the entry signal: the engine must keep the
    # value read on the entry candle (static stop).
    lows = [99.5, 100.5, 101.5, 99.5, 103.5, 104.5, 105.5, 106.5, 107.5, 108.5]
    data = _frame(lows=lows)
    strategy = ScriptedStrategy(entry_long=[1], stop_loss={1: 100.0, 2: 90.0})

    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    assert result.n_trades == 1
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price == pytest.approx(100.0)
    assert trade.stop_price == pytest.approx(100.0)
    assert trade.exit_time == data.index[3]
    assert trade.entry_time == data.index[2]
    assert trade.entry_price == pytest.approx(102.0)
    assert trade.pnl == pytest.approx((100.0 - 102.0) * trade.size)


def test_the_stop_is_not_armed_on_a_candle_that_precedes_the_entry() -> None:
    # the wick below the stop happens *before* the entry: nothing must be closed
    lows = [99.5, 99.5, 101.5, 102.5, 103.5, 104.5, 105.5, 106.5, 107.5, 108.5]
    data = _frame(lows=lows)
    strategy = ScriptedStrategy(entry_long=[1], stop_loss={1: 100.0})

    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    assert result.n_trades == 1
    assert result.trades[0].exit_reason is ExitReason.END_OF_DATA


def test_a_nan_stop_means_no_stop() -> None:
    data = _frame(lows=[99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 99.5])
    strategy = ScriptedStrategy(entry_long=[1], exit_long=[7])

    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    assert result.n_trades == 1
    assert result.trades[0].exit_reason is ExitReason.SIGNAL
    assert result.trades[0].exit_time == data.index[8]


def test_the_stop_wins_over_a_signal_exit_on_the_same_candle() -> None:
    lows = [99.5, 100.5, 101.5, 99.5, 103.5, 104.5, 105.5, 106.5, 107.5, 108.5]
    data = _frame(lows=lows)
    strategy = ScriptedStrategy(entry_long=[1], exit_long=[3], stop_loss={1: 100.0})

    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    assert result.trades[0].exit_reason is ExitReason.STOP_LOSS
    assert result.trades[0].exit_time == data.index[3]


def test_a_position_open_on_the_last_candle_is_closed_at_the_last_close() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_long=[1])

    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.END_OF_DATA
    assert trade.exit_time == data.index[-1]
    assert trade.exit_price == pytest.approx(float(data["close"].iloc[-1]))
    assert trade.pnl == pytest.approx(
        trade.size * (float(data["close"].iloc[-1]) - trade.entry_price)
    )
    assert float(result.equity_curve.iloc[-1]) == pytest.approx(result.final_balance)


def test_an_exit_signal_on_the_last_candle_still_ends_the_trade() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_long=[1], exit_long=[9])

    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    assert result.trades[0].exit_reason is ExitReason.END_OF_DATA
    assert result.trades[0].exit_time == data.index[-1]


def test_a_second_entry_signal_while_in_position_is_ignored() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_long=[1, 3, 5], exit_long=[6])

    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    assert result.n_trades == 1
    assert result.trades[0].entry_time == data.index[2]
    assert result.trades[0].entry_price == pytest.approx(102.0)
    assert result.trades[0].exit_time == data.index[7]


def test_an_entry_signal_on_the_last_candle_is_not_filled() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_long=[9])

    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    assert result.trades == []
    assert result.is_empty


# ---------------------------------------------------------------------------
# short side
# ---------------------------------------------------------------------------


def test_short_entry_fills_at_the_next_open_and_mirrors_the_stop() -> None:
    data = _frame()
    # signal candle 1: close 101.5, stop column 100.0 -> distance 1.5
    strategy = ScriptedStrategy(entry_short=[1], stop_loss={1: 100.0})

    result = run_backtest(strategy, data, allow_short=True, fee_rate=0.0, slippage=0.0)

    trade = result.trades[0]
    assert trade.direction is Direction.SHORT
    assert trade.entry_time == data.index[2]
    assert trade.entry_price == pytest.approx(102.0)
    assert trade.stop_price == pytest.approx(102.0 + (101.5 - 100.0))
    assert trade.exit_reason is ExitReason.STOP_LOSS
    assert trade.exit_price == pytest.approx(103.5)
    assert trade.exit_time == data.index[3]
    assert trade.pnl == pytest.approx((102.0 - 103.5) * trade.size)


def test_short_signal_exit_fills_at_the_following_open_with_slippage() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_short=[1], exit_short=[4])

    result = run_backtest(strategy, data, allow_short=True, fee_rate=FEE_RATE, slippage=SLIPPAGE)

    trade = result.trades[0]
    entry_price = 102.0 * (1.0 - SLIPPAGE)
    exit_price = 105.0 * (1.0 + SLIPPAGE)
    size = INITIAL_BALANCE / (entry_price * (1.0 + FEE_RATE))
    fees = FEE_RATE * size * (entry_price + exit_price)

    assert trade.direction is Direction.SHORT
    assert trade.exit_reason is ExitReason.SIGNAL
    assert trade.entry_price == pytest.approx(entry_price)
    assert trade.exit_price == pytest.approx(exit_price)
    assert trade.pnl == pytest.approx(size * (entry_price - exit_price) - fees)
    assert result.metadata["allow_short"] is True


def test_short_signals_are_ignored_when_allow_short_is_false() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_short=[1], exit_short=[4])

    result = run_backtest(strategy, data, allow_short=False, fee_rate=0.0, slippage=0.0)

    assert result.trades == []
    assert result.metadata["allow_short"] is False
    assert (result.equity_curve == INITIAL_BALANCE).all()


def test_allow_short_defaults_to_the_strategy_capability() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_short=[1], exit_short=[4], params={"marker": 3})

    # the scripted strategy has no ``allow_short`` parameter, so shorts stay off
    # unless the engine is told otherwise explicitly.
    result = run_backtest(strategy, data, fee_rate=0.0, slippage=0.0)

    assert result.trades == []
    assert result.metadata["allow_short"] is False
    assert result.params == {"marker": 3}


def test_the_basic_strategy_trades_shorts_when_allowed(trending_frame: pd.DataFrame) -> None:
    strategy = BasicStrategy({"allow_short": True})

    result = run_backtest(strategy, trending_frame)

    assert result.metadata["allow_short"] is True
    assert any(trade.direction is Direction.SHORT for trade in result.trades)


# ---------------------------------------------------------------------------
# empty runs, validation, determinism
# ---------------------------------------------------------------------------


def test_a_run_without_any_signal_produces_no_trade_and_a_flat_equity() -> None:
    data = _frame()

    result = run_backtest(ScriptedStrategy(), data)

    assert result.trades == []
    assert result.is_empty
    assert result.n_trades == 0
    assert len(result.equity_curve) == len(data)
    assert (result.equity_curve == INITIAL_BALANCE).all()
    assert result.final_balance == pytest.approx(INITIAL_BALANCE)


def test_a_single_candle_is_not_enough() -> None:
    with pytest.raises(InsufficientDataError):
        run_backtest(ScriptedStrategy(), _frame([100.0], [100.5]))


def test_a_frame_violating_the_ohlcv_contract_is_rejected() -> None:
    strategy = ScriptedStrategy()

    with pytest.raises(StrategyError, match="missing required column"):
        run_backtest(strategy, _frame().drop(columns=["volume"]))
    with pytest.raises(StrategyError, match="empty frame"):
        run_backtest(strategy, _frame([], []))
    with pytest.raises(StrategyError, match="DatetimeIndex"):
        run_backtest(strategy, _frame().reset_index(drop=True))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        run_backtest(strategy, [(1.0, 2.0)])  # type: ignore[arg-type]


def test_a_strategy_returning_wrong_signal_columns_is_rejected() -> None:
    with pytest.raises(StrategyError, match="signal column"):
        run_backtest(_WrongColumnsStrategy(), _frame())


def test_the_input_frame_is_never_mutated() -> None:
    data = _frame()
    before = data.copy(deep=True)

    run_backtest(ScriptedStrategy(entry_long=[1], exit_long=[4]), data)

    pd.testing.assert_frame_equal(data, before)


def test_results_are_fully_deterministic() -> None:
    data = _frame()
    strategy = ScriptedStrategy(entry_long=[1], exit_long=[4], stop_loss={1: 100.0})

    first = run_backtest(strategy, data, fee_rate=FEE_RATE, slippage=SLIPPAGE)
    second = run_backtest(strategy, data, fee_rate=FEE_RATE, slippage=SLIPPAGE)

    assert first == second
    pd.testing.assert_series_equal(first.equity_curve, second.equity_curve, check_exact=True)


def test_metadata_is_exactly_the_locked_set() -> None:
    result = run_backtest(
        ScriptedStrategy(entry_long=[1], exit_long=[4]),
        _frame(),
        symbol="BTC/USDT",
        timeframe="4h",
        fee_rate=FEE_RATE,
        slippage=SLIPPAGE,
    )

    assert set(result.metadata) == {
        "fee_rate",
        "slippage",
        "stake_amount",
        "allow_short",
        "symbol",
        "timeframe",
        "engine_version",
    }
    assert result.metadata["symbol"] == "BTC/USDT"
    assert result.metadata["timeframe"] == "4h"
    assert result.metadata["fee_rate"] == pytest.approx(FEE_RATE)
    assert result.metadata["slippage"] == pytest.approx(SLIPPAGE)
    assert result.metadata["engine_version"] == "1"
    assert result.symbol == "BTC/USDT"
    assert result.timeframe == "4h"
    assert result.initial_balance == pytest.approx(INITIAL_BALANCE)
    assert result.strategy_name == "scripted"
    assert result.params == {"marker": 7}


# ---------------------------------------------------------------------------
# configuration-driven entry points (the seam used by the validation layer)
# ---------------------------------------------------------------------------


def test_run_backtest_on_config_uses_the_configuration(
    app_config: Any, trending_frame: pd.DataFrame
) -> None:
    result = run_backtest_on_config(app_config, trending_frame)

    assert isinstance(result, BacktestResult)
    assert result.strategy_name == "basic"
    assert result.symbol == "UNKNOWN/USDT"
    assert result.timeframe == app_config.data.timeframe
    assert result.initial_balance == pytest.approx(app_config.backtest.initial_balance)
    assert result.params == BasicStrategy.default_params()
    assert result.metadata["fee_rate"] == pytest.approx(app_config.exchange.fee_rate)
    assert len(result.equity_curve) == len(trending_frame)
    assert float(result.equity_curve.iloc[0]) == pytest.approx(app_config.backtest.initial_balance)
    assert float(result.equity_curve.iloc[-1]) == pytest.approx(result.final_balance)
    assert result.n_trades >= 1


def test_run_backtest_on_config_merges_the_parameter_overrides(
    app_config: Any, trending_frame: pd.DataFrame
) -> None:
    result = run_backtest_on_config(app_config, trending_frame, params={"ema_fast": 5})

    assert result.params["ema_fast"] == 5
    assert result.params["ema_slow"] == 21
    assert all(trade.params_id == "ema_fast=5" for trade in result.trades)


def test_run_backtest_on_config_honours_the_short_switch(
    app_config: Any, trending_frame: pd.DataFrame
) -> None:
    result = run_backtest_on_config(app_config, trending_frame, params={"allow_short": True})

    assert result.metadata["allow_short"] is True
    assert any(trade.direction is Direction.SHORT for trade in result.trades)


def test_run_backtest_on_config_is_deterministic(
    app_config: Any, trending_frame: pd.DataFrame
) -> None:
    first = run_backtest_on_config(app_config, trending_frame)
    second = run_backtest_on_config(app_config, trending_frame)

    assert first == second
    assert [trade.to_dict() for trade in first.trades] == [
        trade.to_dict() for trade in second.trades
    ]


def test_make_runner_returns_a_runner_that_only_depends_on_the_frame(
    app_config: Any, trending_frame: pd.DataFrame
) -> None:
    runner = make_runner(app_config)

    window = trending_frame.iloc[100:300]
    result = runner(window, None)

    assert isinstance(result, BacktestResult)
    assert len(result.equity_curve) == len(window)
    assert result.start == window.index[0]
    assert result.end == window.index[-1]
    assert runner(window, None) == result


def test_make_runner_honours_a_parameter_override(
    app_config: Any, trending_frame: pd.DataFrame
) -> None:
    runner = make_runner(app_config)

    default_result = runner(trending_frame)
    overridden = runner(trending_frame, {"ema_fast": 13})

    assert default_result.params["ema_fast"] == 9
    assert overridden.params["ema_fast"] == 13


def test_make_runner_accepts_an_explicit_symbol(
    app_config: Any, trending_frame: pd.DataFrame
) -> None:
    runner = make_runner(app_config, symbol="ETH/USDT")

    assert runner(trending_frame).symbol == "ETH/USDT"
