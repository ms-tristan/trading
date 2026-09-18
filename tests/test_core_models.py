"""Contract tests for the frozen cross-package models."""

from __future__ import annotations

import dataclasses
import json

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.models import (
    BacktestResult,
    Direction,
    ExitReason,
    RunnerFn,
    TradeRecord,
)


def _trade(**overrides: object) -> TradeRecord:
    payload: dict[str, object] = {
        "entry_time": pd.Timestamp("2023-01-01T00:00:00Z"),
        "exit_time": pd.Timestamp("2023-01-02T03:30:00Z"),
        "entry_price": 101.25,
        "exit_price": 104.5,
        "size": 0.5,
        "direction": Direction.LONG,
        "pnl": 1.625,
        "pnl_pct": 0.01625,
        "fees": 0.15,
        "exit_reason": ExitReason.TAKE_PROFIT,
        "duration_minutes": 1650.0,
        "stop_price": 99.5,
        "take_profit_price": 104.5,
        "params_id": "fast=9|slow=21",
    }
    payload.update(overrides)
    return TradeRecord(**payload)  # type: ignore[arg-type]


def _equity(periods: int = 6) -> pd.Series:
    index = pd.date_range(
        "2024-01-01T00:00:00Z", periods=periods, freq="h", tz="UTC", name="timestamp"
    )
    values = np.linspace(10_000.0, 10_120.0, periods)
    return pd.Series(values, index=index, name="equity", dtype="float64")


def _result(**overrides: object) -> BacktestResult:
    equity = _equity()
    payload: dict[str, object] = {
        "strategy_name": "basic",
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "start": equity.index[0],
        "end": equity.index[-1],
        "initial_balance": 10_000.0,
        "final_balance": 10_120.0,
        "trades": [_trade()],
        "equity_curve": equity,
        "params": {"fast": 9, "slow": 21, "flag": True, "label": "ema", "nested": {"a": [1, 2]}},
        "metadata": {"seed": 42, "engine": "stub"},
    }
    payload.update(overrides)
    return BacktestResult(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TradeRecord
# ---------------------------------------------------------------------------


def test_trade_record_to_dict_keys_match_field_names() -> None:
    payload = _trade().to_dict()
    assert list(payload) == [field.name for field in dataclasses.fields(TradeRecord)]


def test_trade_record_to_dict_encodes_enums_and_timestamps() -> None:
    payload = _trade().to_dict()
    assert payload["direction"] == "long"
    assert payload["exit_reason"] == "take_profit"
    assert payload["entry_time"] == "2023-01-01T00:00:00+00:00"
    assert payload["exit_time"] == "2023-01-02T03:30:00+00:00"
    assert payload["entry_price"] == pytest.approx(101.25)
    assert payload["stop_price"] == pytest.approx(99.5)
    assert payload["params_id"] == "fast=9|slow=21"


def test_trade_record_from_dict_is_the_exact_inverse() -> None:
    trade = _trade()
    assert TradeRecord.from_dict(trade.to_dict()) == trade


@pytest.mark.parametrize("direction", [Direction.LONG, Direction.SHORT])
def test_trade_record_round_trip_for_every_direction(direction: Direction) -> None:
    trade = _trade(direction=direction)
    recovered = TradeRecord.from_dict(trade.to_dict())
    assert recovered.direction is direction


def test_trade_record_round_trip_with_none_stop_and_take_profit() -> None:
    trade = _trade(stop_price=None, take_profit_price=None)
    payload = trade.to_dict()
    assert payload["stop_price"] is None
    assert payload["take_profit_price"] is None
    recovered = TradeRecord.from_dict(payload)
    assert recovered.stop_price is None
    assert recovered.take_profit_price is None
    assert recovered == trade


def test_trade_record_round_trip_survives_json() -> None:
    trade = _trade(stop_price=None, exit_reason=ExitReason.STOP_LOSS)
    payload = json.loads(json.dumps(trade.to_dict(), default=str))
    recovered = TradeRecord.from_dict(payload)
    assert recovered == trade
    assert recovered.exit_reason is ExitReason.STOP_LOSS
    assert recovered.entry_time == trade.entry_time


def test_trade_record_is_frozen() -> None:
    trade = _trade()
    with pytest.raises(dataclasses.FrozenInstanceError):
        trade.pnl = 0.0  # type: ignore[misc]


def test_enum_members_match_the_contract() -> None:
    assert [member.value for member in Direction] == ["long", "short"]
    assert [member.value for member in ExitReason] == [
        "stop_loss",
        "take_profit",
        "signal",
        "end_of_data",
        "max_duration",
    ]
    assert Direction.LONG == "long"


# ---------------------------------------------------------------------------
# BacktestResult
# ---------------------------------------------------------------------------


def test_backtest_result_to_dict_keys_and_shapes() -> None:
    payload = _result().to_dict()
    assert list(payload) == [
        "strategy_name",
        "symbol",
        "timeframe",
        "start",
        "end",
        "initial_balance",
        "final_balance",
        "trades",
        "equity_curve",
        "params",
        "metadata",
    ]
    assert payload["start"] == "2024-01-01T00:00:00+00:00"
    assert isinstance(payload["trades"], list) and len(payload["trades"]) == 1
    assert set(payload["equity_curve"]) == {"timestamps", "values"}
    assert len(payload["equity_curve"]["timestamps"]) == 6
    assert len(payload["equity_curve"]["values"]) == 6
    assert payload["equity_curve"]["timestamps"][0] == "2024-01-01T00:00:00+00:00"


def test_backtest_result_from_dict_is_the_exact_inverse() -> None:
    result = _result()
    recovered = BacktestResult.from_dict(result.to_dict())
    assert recovered.strategy_name == result.strategy_name
    assert recovered.symbol == result.symbol
    assert recovered.timeframe == result.timeframe
    assert recovered.start == result.start
    assert recovered.end == result.end
    assert recovered.initial_balance == result.initial_balance
    assert recovered.final_balance == result.final_balance
    assert recovered.trades == result.trades
    assert recovered.params == result.params
    assert recovered.metadata == result.metadata
    pd.testing.assert_series_equal(recovered.equity_curve, result.equity_curve, check_freq=False)
    assert recovered == result


def test_backtest_result_round_trip_survives_json() -> None:
    result = _result()
    payload = json.loads(json.dumps(result.to_dict(), default=str))
    recovered = BacktestResult.from_dict(payload)
    assert recovered == result
    assert recovered.equity_curve.name == "equity"
    assert recovered.equity_curve.dtype == np.dtype("float64")
    assert isinstance(recovered.equity_curve.index, pd.DatetimeIndex)
    assert str(recovered.equity_curve.index.tz) == "UTC"
    assert recovered.equity_curve.index.name == "timestamp"
    assert recovered.equity_curve.index.is_monotonic_increasing


def test_backtest_result_to_dict_is_json_serialisable() -> None:
    text = json.dumps(_result().to_dict(), default=str)
    assert "equity_curve" in text


def test_backtest_result_n_trades_is_empty_and_equity_contract() -> None:
    result = _result()
    assert result.n_trades == 1
    assert result.is_empty is False
    curve = result.equity_curve
    assert curve.dtype == np.dtype("float64")
    assert curve.name == "equity"
    assert curve.index.name == "timestamp"
    assert str(curve.index.tz) == "UTC"
    assert curve.index.is_monotonic_increasing

    empty = _result(trades=[], equity_curve=_equity(1))
    assert empty.n_trades == 0
    assert empty.is_empty is True


def test_backtest_result_empty_round_trip() -> None:
    empty = _result(trades=[], final_balance=10_000.0, equity_curve=_equity(1).iloc[:0])
    recovered = BacktestResult.from_dict(empty.to_dict())
    assert recovered.is_empty
    assert recovered.to_dict()["equity_curve"] == {"timestamps": [], "values": []}
    pd.testing.assert_series_equal(recovered.equity_curve, empty.equity_curve, check_freq=False)


def test_backtest_result_normalises_a_naive_unsorted_equity_curve() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2024-01-01T02:00:00"),
            pd.Timestamp("2024-01-01T00:00:00"),
            pd.Timestamp("2024-01-01T01:00:00"),
        ]
    )
    naive = pd.Series([102.0, 100.0, 101.0], index=index)
    result = _result(equity_curve=naive)
    curve = result.equity_curve
    assert curve.index.name == "timestamp"
    assert str(curve.index.tz) == "UTC"
    assert curve.index.is_monotonic_increasing
    assert curve.name == "equity"
    assert curve.dtype == np.dtype("float64")
    assert curve.tolist() == [100.0, 101.0, 102.0]
    # the input series was not mutated
    assert naive.name is None


def test_backtest_result_drops_duplicated_equity_timestamps_keeping_last() -> None:
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2024-01-01T00:00:00Z"),
            pd.Timestamp("2024-01-01T00:00:00Z"),
            pd.Timestamp("2024-01-01T01:00:00Z"),
        ]
    )
    result = _result(equity_curve=pd.Series([1.0, 2.0, 3.0], index=index))
    assert result.equity_curve.tolist() == [2.0, 3.0]


def test_backtest_result_equality_compares_equity_values() -> None:
    result = _result()
    same = BacktestResult.from_dict(result.to_dict())
    other = _result(final_balance=1.0)
    assert result == same
    assert result != other
    assert (result == "not a result") is False


def test_backtest_result_inequality_on_a_different_equity_curve() -> None:
    result = _result()
    other = _result(equity_curve=_equity() * 2.0)
    assert result != other


def test_runner_fn_alias_and_stub(runner_stub: RunnerFn, ohlcv_frame: pd.DataFrame) -> None:
    assert callable(runner_stub)
    result = runner_stub(ohlcv_frame, None)
    assert isinstance(result, BacktestResult)
    assert result.n_trades == 3
    assert len(result.equity_curve) == len(ohlcv_frame)
    assert result.final_balance == pytest.approx(10_030.0)
    tagged = runner_stub(ohlcv_frame, {"fast": 9})
    assert tagged.params == {"fast": 9}
    assert tagged.trades[0].params_id == "fast=9"
    pd.testing.assert_series_equal(tagged.equity_curve, result.equity_curve, check_freq=False)
