"""Behavioural tests of the reference "basic" strategy.

The crossover frames below are hand-built (12 candles each) so the exact candle
the crossover happens on is known a priori:

* :func:`_up_frame` falls from 100 to 95 then rises to 101: the fast EMA crosses
  **above** the slow one on candle **7**;
* :func:`_down_frame` rises from 95 to 101 then falls back to 96: the fast EMA
  crosses **below** the slow one on candle **8**.

The parameters used with them are small (``ema_fast=2``, ``ema_slow=3``,
``rsi_period=2``, ``atr_period=2``) so that every indicator is defined on such a
short frame, and the RSI band is widened (``rsi_min=1``, ``rsi_max=99``) unless a
test explicitly wants the filter to block.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.constants import SIGNAL_COLUMNS
from trading_platform.core.errors import StrategyError
from trading_platform.strategy.base import BOOL_SIGNAL_COLUMNS
from trading_platform.strategy.basic import BasicStrategy, BasicStrategyParams
from trading_platform.strategy.indicators import atr

START = "2024-01-01T00:00:00Z"

#: Hand-designed frames: the crossover candle is known exactly.
UP_CLOSES = [100.0, 99, 98, 97, 96, 95, 96, 97, 98, 99, 100, 101]
DOWN_CLOSES = [95.0, 96, 97, 98, 99, 100, 101, 100, 99, 98, 97, 96]
UP_CROSS_ROW = 7
DOWN_CROSS_ROW = 8

#: Widened RSI band, so that only the crossover logic decides on the short frames.
SHORT_FRAME_PARAMS: dict[str, object] = {
    "ema_fast": 2,
    "ema_slow": 3,
    "rsi_period": 2,
    "atr_period": 2,
    "rsi_min": 1.0,
    "rsi_max": 99.0,
}


def _frame(closes: list[float]) -> pd.DataFrame:
    """Build an OHLCV frame whose opens chain the closes (open[t] = close[t-1])."""
    values = np.asarray(closes, dtype="float64")
    index = pd.date_range(START, periods=values.size, freq="h", tz="UTC", name="timestamp")
    opens = np.concatenate(([values[0]], values[:-1])) if values.size else values
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, values) + 0.5,
            "low": np.minimum(opens, values) - 0.5,
            "close": values,
            "volume": np.full(values.size, 1.0),
        },
        index=index,
    )


def _up_frame() -> pd.DataFrame:
    return _frame(UP_CLOSES)


def _down_frame() -> pd.DataFrame:
    return _frame(DOWN_CLOSES)


def _rows(column: pd.Series) -> list[int]:
    return [int(position) for position in np.flatnonzero(column.to_numpy(dtype=bool))]


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_default_parameters_are_the_documented_ones() -> None:
    params = BasicStrategy().params

    assert isinstance(params, BasicStrategyParams)
    assert params.model_dump() == {
        "ema_fast": 9,
        "ema_slow": 21,
        "rsi_period": 14,
        "rsi_min": 30.0,
        "rsi_max": 70.0,
        "atr_period": 14,
        "atr_stop_multiplier": 2.0,
        "allow_short": False,
    }
    assert BasicStrategy.default_params() == params.model_dump()


def test_param_space_matches_the_documented_grid() -> None:
    assert BasicStrategy().param_space() == {
        "ema_fast": [5, 9, 13],
        "ema_slow": [21, 34, 55],
        "rsi_max": [65.0, 70.0, 75.0],
        "atr_stop_multiplier": [1.5, 2.0, 3.0],
    }


@pytest.mark.parametrize(
    "params",
    [
        {"ema_fast": 21, "ema_slow": 21},
        {"ema_fast": 30, "ema_slow": 10},
        {"rsi_min": 80.0, "rsi_max": 40.0},
        {"rsi_min": 70.0, "rsi_max": 70.0},
    ],
)
def test_incoherent_parameters_raise(params: dict[str, object]) -> None:
    with pytest.raises(StrategyError) as error:
        BasicStrategy(params)

    message = str(error.value)
    assert "ema_slow" in message or "rsi_min" in message


@pytest.mark.parametrize(
    "params",
    [
        {"ema_fast": 0},
        {"ema_slow": 1},
        {"rsi_period": 1},
        {"rsi_min": -1.0},
        {"rsi_max": 101.0},
        {"atr_period": 1},
        {"atr_stop_multiplier": 0.0},
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object]) -> None:
    with pytest.raises(StrategyError):
        BasicStrategy(params)


def test_unknown_parameter_raises() -> None:
    with pytest.raises(StrategyError, match="unknown_field"):
        BasicStrategy({"unknown_field": 1})


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_the_indicator_columns_and_preserves_the_ohlcv_columns() -> None:
    data = _up_frame()
    before = data.copy(deep=True)

    prepared = BasicStrategy().prepare(data)

    assert list(prepared.columns) == [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "ema_fast",
        "ema_slow",
        "rsi",
        "atr",
    ]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert prepared.index.equals(data.index)
    for column in ("ema_fast", "ema_slow", "rsi", "atr"):
        assert prepared[column].dtype == np.dtype("float64")


def test_prepare_matches_the_indicator_layer() -> None:
    data = _up_frame()
    strategy = BasicStrategy(SHORT_FRAME_PARAMS)

    prepared = strategy.prepare(data)

    np.testing.assert_allclose(
        prepared["ema_fast"].to_numpy(dtype="float64")[1:],
        data["close"].ewm(span=2, adjust=False, min_periods=2).mean().to_numpy()[1:],
    )
    assert prepared["rsi"].iloc[7] == pytest.approx(75.0)
    assert prepared["atr"].iloc[2:].eq(2.0).all()


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    with pytest.raises(StrategyError, match="missing required column"):
        BasicStrategy().prepare(_up_frame().drop(columns=["volume"]))
    with pytest.raises(StrategyError, match="empty frame"):
        BasicStrategy().prepare(_frame([]))


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def test_signals_have_exactly_the_expected_columns_and_dtypes() -> None:
    strategy = BasicStrategy(SHORT_FRAME_PARAMS)
    prepared = strategy.prepare(_up_frame())

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)


def test_entry_long_fires_only_on_the_crossover_candle() -> None:
    strategy = BasicStrategy(SHORT_FRAME_PARAMS)
    prepared, signals = strategy.run(_up_frame())

    # sanity check of the crossover itself
    fast = prepared["ema_fast"]
    slow = prepared["ema_slow"]
    assert fast.iloc[UP_CROSS_ROW] > slow.iloc[UP_CROSS_ROW]
    assert fast.iloc[UP_CROSS_ROW - 1] <= slow.iloc[UP_CROSS_ROW - 1]

    assert _rows(signals["entry_long"]) == [UP_CROSS_ROW]


def test_the_first_candle_is_never_an_entry_and_nan_rows_never_fire() -> None:
    strategy = BasicStrategy(SHORT_FRAME_PARAMS)
    prepared, signals = strategy.run(_up_frame())

    assert not signals["entry_long"].iloc[0]
    assert not signals["exit_long"].iloc[0]
    assert not signals["entry_short"].iloc[0]
    assert not signals["exit_short"].iloc[0]
    undefined = prepared["ema_slow"].isna()
    assert undefined.any()
    assert not signals.loc[undefined, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()


def test_exit_long_fires_on_the_reverse_crossover() -> None:
    strategy = BasicStrategy(SHORT_FRAME_PARAMS)
    prepared, signals = strategy.run(_down_frame())

    fast = prepared["ema_fast"]
    slow = prepared["ema_slow"]
    assert fast.iloc[DOWN_CROSS_ROW] < slow.iloc[DOWN_CROSS_ROW]
    assert fast.iloc[DOWN_CROSS_ROW - 1] >= slow.iloc[DOWN_CROSS_ROW - 1]

    assert _rows(signals["exit_long"]) == [DOWN_CROSS_ROW]


def test_the_rsi_filter_blocks_an_entry_out_of_band() -> None:
    data = _up_frame()

    # RSI(2) is 75 on the crossover candle, so neither a 70 upper bound nor a
    # 90 lower bound lets the entry through.
    for override in ({"rsi_max": 70.0}, {"rsi_min": 90.0}):
        strategy = BasicStrategy({**SHORT_FRAME_PARAMS, **override})
        prepared = strategy.prepare(data)
        assert prepared["rsi"].iloc[UP_CROSS_ROW] == pytest.approx(75.0)
        signals = strategy.signals(prepared)
        assert _rows(signals["entry_long"]) == []

    permissive = BasicStrategy({**SHORT_FRAME_PARAMS, "rsi_min": 74.0, "rsi_max": 76.0})
    prepared = permissive.prepare(data)
    assert _rows(permissive.signals(prepared)["entry_long"]) == [UP_CROSS_ROW]


def test_stop_loss_is_close_minus_the_atr_distance() -> None:
    data = _up_frame()
    params = {**SHORT_FRAME_PARAMS, "atr_stop_multiplier": 1.5}
    strategy = BasicStrategy(params)
    prepared, signals = strategy.run(data)

    expected = prepared["close"] - 1.5 * atr(
        prepared["high"], prepared["low"], prepared["close"], 2
    )

    pd.testing.assert_series_equal(
        signals["stop_loss"], expected, check_names=False, check_exact=True
    )
    assert int(signals["stop_loss"].isna().sum()) == 2
    assert signals["stop_loss"].iloc[2] == pytest.approx(prepared["close"].iloc[2] - 3.0)


def test_stop_loss_is_nan_wherever_the_atr_is_nan() -> None:
    strategy = BasicStrategy()  # atr_period = 14
    prepared, signals = strategy.run(_up_frame())

    assert prepared["atr"].isna().all()
    assert signals["stop_loss"].isna().all()


def test_short_columns_are_all_false_without_allow_short() -> None:
    strategy = BasicStrategy(SHORT_FRAME_PARAMS)
    _prepared, signals = strategy.run(_down_frame())

    assert _rows(signals["entry_long"]) == []
    assert _rows(signals["entry_short"]) == []
    assert _rows(signals["exit_short"]) == []
    assert _rows(signals["exit_long"]) == [DOWN_CROSS_ROW]


def test_allow_short_mirrors_the_signals() -> None:
    strategy = BasicStrategy({**SHORT_FRAME_PARAMS, "allow_short": True})

    down_prepared, down = strategy.run(_down_frame())
    assert _rows(down["entry_short"]) == [DOWN_CROSS_ROW]
    assert _rows(down["exit_short"]) == []
    assert _rows(down["entry_long"]) == []
    assert _rows(down["exit_long"]) == [DOWN_CROSS_ROW]

    up_prepared, up = strategy.run(_up_frame())
    assert _rows(up["exit_short"]) == [UP_CROSS_ROW]
    assert _rows(up["entry_short"]) == []
    assert _rows(up["entry_long"]) == [UP_CROSS_ROW]
    # the stop column is shared by both directions
    assert np.isnan(down["stop_loss"].iloc[0]) and np.isnan(down_prepared["atr"].iloc[0])
    assert up["stop_loss"].iloc[UP_CROSS_ROW] == pytest.approx(
        up_prepared["close"].iloc[UP_CROSS_ROW] - 2.0 * up_prepared["atr"].iloc[UP_CROSS_ROW]
    )


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        BasicStrategy(SHORT_FRAME_PARAMS).signals(_up_frame())
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        BasicStrategy().signals([1, 2, 3])  # type: ignore[arg-type]


def test_signals_is_deterministic_and_leaves_the_frame_untouched() -> None:
    strategy = BasicStrategy(SHORT_FRAME_PARAMS)
    prepared = strategy.prepare(_up_frame())
    snapshot = prepared.copy(deep=True)

    first = strategy.signals(prepared)
    second = strategy.signals(prepared)

    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_frame_equal(prepared, snapshot)


# ---------------------------------------------------------------------------
# shared fixture
# ---------------------------------------------------------------------------


def test_default_parameters_produce_entries_on_the_trending_frame(
    trending_frame: pd.DataFrame,
) -> None:
    strategy = BasicStrategy()

    prepared_first, signals_first = strategy.run(trending_frame)
    prepared_second, signals_second = strategy.run(trending_frame)

    pd.testing.assert_frame_equal(prepared_first, prepared_second)
    pd.testing.assert_frame_equal(signals_first, signals_second)
    assert len(trending_frame) == 600
    assert int(signals_first["entry_long"].sum()) >= 1
    assert int(signals_first["exit_long"].sum()) >= 1
    assert not signals_first["entry_short"].to_numpy().any()
    assert prepared_first["rsi"].between(0.0, 100.0, inclusive="both").sum() == 600 - 14
