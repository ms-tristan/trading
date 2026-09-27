"""Behavioural tests of the ``rsi_reversion`` strategy.

Everything here is offline and deterministic: the frames are hand-built (or come
from the shared synthetic fixtures), the expected indicator values are
hand-computed or checked against the pandas expression the contract mandates, and
no test touches the network or a file outside the repository.

The ten groups of the file:

1. registration, defaults and the published parameter grid;
2. the OHLCV contract of :meth:`RsiReversionStrategy.prepare`;
3. the prepared-frame contract (index preserved exactly, input never mutated,
   the documented indicator columns added);
4. the indicator values themselves — including the namespace check that the
   imported ``rsi`` / ``ema`` callables are never shadowed by the columns;
5. the frozen signal-frame contract, through ``ensure_signal_frame``;
6. the entry / exit truth table on a hand-built frame that reproduces the three
   behaviours the rule is defined by: an accepted pullback, a pullback the trend
   filter **blocks**, and the exit that ignores the trend;
7. ``NaN`` never fires a signal;
8. the ``stop_loss`` column, including the exact-``0.0``-means-no-stop rule;
9. the parameter model: per-field bounds, unknown keys and the cross-field
   invariant ``exit_rsi > entry_rsi`` (checked over the whole published grid);
10. the declared warm-up (**201** candles on the default parameters, one above
    the platform's 200-candle default budget — an honest consequence of the
    published 200-period trend filter), the structured warning, determinism and
    the frozen layer direction (the module never imports
    ``trading_platform.realtime``).

One end-to-end run through
:func:`trading_platform.strategy.engine.run_backtest` closes the file, proving
the strategy really opens **and** closes trades.
"""

from __future__ import annotations

import ast
import itertools
import logging
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from trading_platform.core.constants import REQUIRED_OHLCV_COLUMNS, SIGNAL_COLUMNS
from trading_platform.core.errors import StrategyError
from trading_platform.core.models import ExitReason
from trading_platform.strategy import rsi_reversion as rsi_reversion_module
from trading_platform.strategy.base import (
    BOOL_SIGNAL_COLUMNS,
    StrategyParams,
    ensure_signal_frame,
)
from trading_platform.strategy.engine import run_backtest
from trading_platform.strategy.indicators import atr, ema, rsi
from trading_platform.strategy.registry import STRATEGIES, get_strategy
from trading_platform.strategy.rsi_reversion import (
    INDICATOR_COLUMNS,
    RsiReversionParams,
    RsiReversionStrategy,
    RsiReversionStrategyParams,
)

START = "2024-01-01T00:00:00Z"

#: The module logger the warm-up warning is emitted on.
RSI_LOGGER = "trading_platform.strategy.rsi_reversion"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

#: Candles per 24-hour day of the grids the warm-up is asserted on.
CANDLES_PER_DAY: dict[str, float] = {"1m": 1440.0, "1h": 24.0, "4h": 6.0, "1d": 1.0}

#: The hand-built parameters of the truth table: a short trend average (so the
#: frame can be small) with the published 2-period oscillator and thresholds.
TRUTH_PARAMS: dict[str, object] = {
    "rsi_period": 2,
    "trend_ema_period": 60,
    "atr_period": 2,
    "entry_rsi": 10.0,
    "exit_rsi": 70.0,
}

#: The row of the accepted pullback (``rsi < 10`` **and** ``close > trend_ema``).
ENTRY_ROW = 200

#: The row of the blocked pullback (``rsi < 10`` **but** ``close < trend_ema``).
BLOCKED_ROW = 241

#: The row on which the oscillator has recovered above ``exit_rsi``.
EXIT_ROW = 205

#: The rows of the bounce that is still below the trend average: the oscillator is
#: above ``100 - entry_rsi``, so this is the short entry of the frame.
SHORT_ENTRY_ROWS = [323, 324, 325, 326]


def _series(values: object, *, name: str = "close") -> pd.Series:
    """Build a tz-aware ``float64`` Series from ``values``."""
    array = np.asarray(values, dtype="float64")
    index = pd.date_range(START, periods=array.size, freq="h", tz="UTC", name="timestamp")
    return pd.Series(array, index=index, name=name, dtype="float64")


def _frame_from_closes(closes: object, *, freq: str = "h") -> pd.DataFrame:
    """Build an OHLCV frame whose opens chain the closes (``open[t] = close[t-1]``)."""
    values = np.asarray(closes, dtype="float64")
    index = pd.date_range(START, periods=values.size, freq=freq, tz="UTC", name="timestamp")
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


def _frame(count: int, *, freq: str = "h") -> pd.DataFrame:
    """Build a deterministic, NaN-free OHLCV frame of ``count`` candles."""
    steps = np.arange(count, dtype="float64")
    closes = 100.0 + 0.05 * steps + 10.0 * np.sin(steps / 17.0)
    return _frame_from_closes(closes, freq=freq)


def _rows(column: pd.Series) -> list[int]:
    """Return the integer positions where ``column`` is ``True``."""
    return [int(position) for position in np.flatnonzero(column.to_numpy(dtype=bool))]


def _trend_frame() -> pd.DataFrame:
    """Return the hand-built frame of the truth table.

    Four phases, each with a purpose:

    * rows 0-199: a steady uptrend of ``+2`` per candle, so the 60-candle trend
      average sits about 59 below the price and the oscillator is pinned at 100;
    * row 200: a pullback of 30 — deep enough to drive the 2-period RSI to
      ``6.25``, shallow enough to keep the close **above** the trend average:
      the accepted entry;
    * rows 201-240: the recovery, so the oscillator climbs back above 70 and the
      long exit fires;
    * row 241: a pullback of 120 — the oscillator is even lower (``1.64``) but the
      close is now **below** the trend average: the entry the filter blocks;
    * rows 242-281: the recovery out of that pullback;
    * rows 282-321: a sustained crash (``-3 %`` per candle);
    * rows 322-326: a sharp ``+12 %`` bounce that is still far below the falling
      trend average: the oscillator is above ``100 - entry_rsi``, which is the
      short entry of the frame.
    """
    closes = [100.0 + 2.0 * index for index in range(200)]
    closes.append(closes[-1] - 30.0)
    closes += [closes[-1] + 2.0 * index for index in range(1, 41)]
    closes.append(closes[-1] - 120.0)
    closes += [closes[-1] + 2.0 * index for index in range(1, 41)]
    crash = closes[-1]
    closes += [crash * (0.97**index) for index in range(1, 41)]
    closes += [closes[-1] * (1.12**index) for index in range(1, 6)]
    return _frame_from_closes(closes)


# ---------------------------------------------------------------------------
# 1. registration, defaults, parameter grid
# ---------------------------------------------------------------------------


def test_the_strategy_is_registered_under_its_published_name() -> None:
    assert RsiReversionStrategy.name == "rsi_reversion"
    assert STRATEGIES["rsi_reversion"] is RsiReversionStrategy
    assert get_strategy("rsi_reversion").name == "rsi_reversion"
    assert RsiReversionStrategy.__module__ == "trading_platform.strategy.rsi_reversion"


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = RsiReversionStrategy()
    params = strategy.params

    assert isinstance(params, RsiReversionStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert RsiReversionParams is RsiReversionStrategyParams
    assert isinstance(params, RsiReversionParams)
    assert params.model_dump() == {
        "rsi_period": 2,
        "entry_rsi": 10.0,
        "exit_rsi": 70.0,
        "trend_ema_period": 200,
        "atr_period": 14,
        "atr_stop_multiplier": 3.0,
        "allow_short": False,
    }
    assert RsiReversionStrategy.default_params() == params.model_dump()


def test_param_space_matches_the_documented_grid() -> None:
    space = RsiReversionStrategy().param_space()

    assert space == {
        "rsi_period": [2, 3, 4],
        "entry_rsi": [5.0, 10.0],
        "exit_rsi": [60.0, 70.0],
        "atr_stop_multiplier": [0.0, 3.0],
    }
    # the robustness sweep is bounded: the grid must stay <= 512 combinations
    assert math.prod(len(values) for values in space.values()) == 24


def test_every_cell_of_the_published_grid_is_a_valid_configuration() -> None:
    """No cell of ``PARAM_SPACE`` may be rejected by the cross-field validator."""
    space = RsiReversionStrategy().param_space()
    keys = sorted(space)
    for combination in itertools.product(*(space[key] for key in keys)):
        params = dict(zip(keys, combination, strict=True))
        strategy = RsiReversionStrategy(params)
        assert strategy.params.model_dump()["exit_rsi"] > params["entry_rsi"]


def test_indicator_columns_are_the_documented_tuple() -> None:
    assert INDICATOR_COLUMNS == ("rsi", "trend_ema", "atr")
    assert rsi_reversion_module.__all__ == [
        "RsiReversionParams",
        "RsiReversionStrategy",
        "RsiReversionStrategyParams",
    ]


def test_allow_short_defaults_to_off_and_can_be_enabled() -> None:
    assert RsiReversionStrategy().params.model_dump()["allow_short"] is False
    assert RsiReversionStrategy({"allow_short": True}).params.model_dump()["allow_short"] is True


def test_the_imported_indicators_are_never_shadowed_by_the_columns() -> None:
    """``rsi`` / ``ema`` are both callables and column names: the callables stay callable."""
    assert rsi_reversion_module.rsi is rsi
    assert rsi_reversion_module.ema is ema
    assert rsi_reversion_module.atr is atr
    assert not hasattr(rsi_reversion_module, "trend_ema")


# ---------------------------------------------------------------------------
# 2. the OHLCV contract of prepare
# ---------------------------------------------------------------------------


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    strategy = RsiReversionStrategy()

    with pytest.raises(StrategyError, match="missing required column"):
        strategy.prepare(_frame(30).drop(columns=["volume"]))

    with pytest.raises(StrategyError, match="empty frame"):
        strategy.prepare(_frame_from_closes([]))

    with pytest.raises(StrategyError, match="DatetimeIndex"):
        strategy.prepare(_frame(30).reset_index(drop=True))

    with pytest.raises(StrategyError, match="pandas DataFrame"):
        strategy.prepare([1.0, 2.0, 3.0])  # type: ignore[arg-type]

    with pytest.raises(StrategyError, match="not convertible to float64"):
        broken = _frame(30)
        broken["close"] = ["not-a-number"] * len(broken)
        strategy.prepare(broken)


# ---------------------------------------------------------------------------
# 3. the prepared-frame contract
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_indicator_columns_and_leaves_the_input_untouched() -> None:
    data = _frame(120)
    before = data.copy(deep=True)

    prepared = RsiReversionStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert len(prepared) == len(data)
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


def test_prepare_preserves_the_index_exactly_even_when_it_is_not_sorted() -> None:
    data = _frame(40).iloc[::-1]
    assert not data.index.is_monotonic_increasing

    prepared = RsiReversionStrategy().prepare(data)

    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    assert list(prepared.index) == list(data.index)


# ---------------------------------------------------------------------------
# 4. the indicator values
# ---------------------------------------------------------------------------


def test_the_indicator_columns_are_the_shared_primitives() -> None:
    data = _frame(120)
    prepared = RsiReversionStrategy({"rsi_period": 4, "trend_ema_period": 25}).prepare(data)

    pd.testing.assert_series_equal(prepared["rsi"], rsi(data["close"], 4), check_names=False)
    pd.testing.assert_series_equal(prepared["trend_ema"], ema(data["close"], 25), check_names=False)
    pd.testing.assert_series_equal(
        prepared["atr"], atr(data["high"], data["low"], data["close"], 14), check_names=False
    )


def test_the_rsi_column_is_wilder_smoothed_and_bounded() -> None:
    data = _frame(60)
    prepared = RsiReversionStrategy({"rsi_period": 2}).prepare(data)

    values = prepared["rsi"].to_numpy(dtype="float64")
    defined = values[~np.isnan(values)]
    assert defined.size > 0
    assert (defined >= 0.0).all()
    assert (defined <= 100.0).all()
    # rsi() loses its first `period` values: the first defined value sits at row 2
    assert int(prepared["rsi"].isna().sum()) == 2
    assert np.isfinite(prepared["rsi"].iloc[2])


def test_the_trend_average_loses_the_first_period_minus_one_rows() -> None:
    data = _frame(300)
    prepared = RsiReversionStrategy({"trend_ema_period": 200}).prepare(data)

    assert int(prepared["trend_ema"].isna().sum()) == 199
    assert pd.isna(prepared["trend_ema"].iloc[198])
    assert np.isfinite(prepared["trend_ema"].iloc[199])


def test_the_atr_column_loses_the_first_atr_period_rows() -> None:
    data = _frame(60)
    prepared = RsiReversionStrategy({"atr_period": 7}).prepare(data)

    assert int(prepared["atr"].isna().sum()) == 7
    assert np.isfinite(prepared["atr"].iloc[7])


# ---------------------------------------------------------------------------
# 5. the frozen signal-frame contract
# ---------------------------------------------------------------------------


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = RsiReversionStrategy(TRUTH_PARAMS)
    prepared = strategy.prepare(_trend_frame())

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)
    # The result really is a fixed point of the contract validator: the strategy
    # routed its frame through ``ensure_signal_frame``.
    pd.testing.assert_frame_equal(ensure_signal_frame(signals, prepared.index), signals)


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        RsiReversionStrategy().signals(_frame(300))

    with pytest.raises(StrategyError, match="pandas DataFrame"):
        RsiReversionStrategy().signals([1, 2, 3])  # type: ignore[arg-type]

    prepared = RsiReversionStrategy().prepare(_frame(300))
    with pytest.raises(StrategyError, match="trend_ema"):
        RsiReversionStrategy().signals(prepared.drop(columns=["trend_ema"]))


def test_a_second_signals_call_returns_an_identical_frame() -> None:
    strategy = RsiReversionStrategy({"allow_short": True})
    prepared = strategy.prepare(_frame(300))
    snapshot = prepared.copy(deep=True)

    first = strategy.signals(prepared)
    second = strategy.signals(prepared)

    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_frame_equal(prepared, snapshot)


# ---------------------------------------------------------------------------
# 6. the entry / exit truth table
# ---------------------------------------------------------------------------


def test_the_truth_table_accepts_one_pullback_and_blocks_the_next() -> None:
    """The three behaviours the rule is defined by, on :func:`_trend_frame`."""
    strategy = RsiReversionStrategy(dict(TRUTH_PARAMS, allow_short=True))

    prepared, signals = strategy.run(_trend_frame())

    # the accepted pullback: oversold *inside* the uptrend
    assert prepared["rsi"].iloc[ENTRY_ROW] == pytest.approx(6.25)
    assert prepared["close"].iloc[ENTRY_ROW] > prepared["trend_ema"].iloc[ENTRY_ROW]
    assert bool(signals["entry_long"].iloc[ENTRY_ROW]) is True
    assert _rows(signals["entry_long"]) == [ENTRY_ROW]

    # the blocked pullback: just as oversold, but below the trend average
    assert prepared["rsi"].iloc[BLOCKED_ROW] < 10.0
    assert prepared["close"].iloc[BLOCKED_ROW] <= prepared["trend_ema"].iloc[BLOCKED_ROW]
    assert bool(signals["entry_long"].iloc[BLOCKED_ROW]) is False

    # the exit is the oscillator recovering, whatever the trend average does
    assert prepared["rsi"].iloc[EXIT_ROW] > 70.0
    assert bool(signals["exit_long"].iloc[EXIT_ROW]) is True
    assert bool(signals["exit_long"].iloc[ENTRY_ROW]) is False

    # the short mirror: the bounce is still under the falling trend average
    assert prepared["rsi"].iloc[SHORT_ENTRY_ROWS[-1]] > 90.0
    assert (
        prepared["close"].iloc[SHORT_ENTRY_ROWS[-1]]
        < prepared["trend_ema"].iloc[SHORT_ENTRY_ROWS[-1]]
    )
    assert _rows(signals["entry_short"])[-len(SHORT_ENTRY_ROWS) :] == SHORT_ENTRY_ROWS


def test_entry_long_needs_both_the_oversold_reading_and_the_trend() -> None:
    strategy = RsiReversionStrategy(TRUTH_PARAMS)
    prepared, signals = strategy.run(_frame(300))

    expected = (prepared["rsi"] < 10.0) & (prepared["close"] > prepared["trend_ema"])
    pd.testing.assert_series_equal(signals["entry_long"], expected, check_names=False)
    assert int(signals["entry_long"].sum()) >= 1


def test_exit_long_fires_on_the_oscillator_alone() -> None:
    strategy = RsiReversionStrategy(TRUTH_PARAMS)
    prepared, signals = strategy.run(_frame(300))

    pd.testing.assert_series_equal(signals["exit_long"], prepared["rsi"] > 70.0, check_names=False)
    # an exit row whose close sits below the trend average still exits: the
    # threshold is read on the oscillator, never on the trend filter
    below = prepared["close"] < prepared["trend_ema"]
    assert bool((signals["exit_long"] & below).any())


def test_the_short_mirror_uses_the_complementary_thresholds() -> None:
    strategy = RsiReversionStrategy(dict(TRUTH_PARAMS, allow_short=True))
    prepared, signals = strategy.run(_frame(300))

    pd.testing.assert_series_equal(
        signals["entry_short"],
        (prepared["rsi"] > 100.0 - 10.0) & (prepared["close"] < prepared["trend_ema"]),
        check_names=False,
    )
    pd.testing.assert_series_equal(
        signals["exit_short"], prepared["rsi"] < 100.0 - 70.0, check_names=False
    )
    # the mirror really is symmetric about 50: the two entries cannot overlap
    assert not bool((signals["entry_long"] & signals["entry_short"]).any())
    assert not bool((signals["exit_long"] & signals["exit_short"]).any())


def test_without_allow_short_the_short_side_stays_false() -> None:
    strategy = RsiReversionStrategy(TRUTH_PARAMS)
    prepared, signals = strategy.run(_trend_frame())

    assert signals["entry_short"].dtype == np.dtype("bool")
    assert signals["exit_short"].dtype == np.dtype("bool")
    assert _rows(signals["entry_short"]) == []
    assert _rows(signals["exit_short"]) == []
    # the long side is untouched by the flag
    assert _rows(signals["entry_long"]) == [ENTRY_ROW]
    assert signals.index.equals(prepared.index)


def test_allow_short_leaves_the_long_side_byte_for_byte_identical() -> None:
    frame = _frame(300)
    long_only = RsiReversionStrategy(TRUTH_PARAMS)
    mirrored = RsiReversionStrategy(dict(TRUTH_PARAMS, allow_short=True))

    long_signals = long_only.run(frame)[1]
    short_signals = mirrored.run(frame)[1]

    for column in ("entry_long", "exit_long", "stop_loss"):
        pd.testing.assert_series_equal(long_signals[column], short_signals[column])


# ---------------------------------------------------------------------------
# 7. NaN never fires a signal
# ---------------------------------------------------------------------------


def test_a_nan_indicator_never_fires_a_signal() -> None:
    strategy = RsiReversionStrategy(dict(TRUTH_PARAMS, allow_short=True))
    prepared, signals = strategy.run(_frame(100))

    undefined_rsi = prepared["rsi"].isna()
    undefined_trend = prepared["trend_ema"].isna()
    assert int(undefined_rsi.sum()) == 2
    # the trend average's warm-up dominates: it is undefined on the first 59 rows
    assert int(undefined_trend.sum()) == 59

    # nothing may fire while the oscillator is undefined: it is never filled with
    # a neutral value (a fill of 0 would fire `exit_short` on every early candle)
    assert not signals.loc[undefined_rsi, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()
    # both entries read the trend filter, so neither can fire while it is undefined
    assert not signals.loc[undefined_trend, ["entry_long", "entry_short"]].to_numpy().any()
    # the exits read the oscillator alone and are therefore unaffected by the
    # trend average's warm-up -- that asymmetry is the rule, not an accident
    assert bool(signals.loc[undefined_trend, "exit_long"].any())


def test_a_nan_close_never_fires_an_entry() -> None:
    data = _frame(300)
    data.loc[data.index[-1], "close"] = np.nan

    prepared, signals = RsiReversionStrategy({"allow_short": True}).run(data)

    assert pd.isna(prepared["close"].iloc[-1])
    # both entries compare the close against something: a missing close can
    # therefore never open a position, it is not filled with a neutral value
    assert not bool(signals["entry_long"].iloc[-1])
    assert not bool(signals["entry_short"].iloc[-1])
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()


def test_the_stop_column_is_nan_wherever_the_atr_is_undefined() -> None:
    strategy = RsiReversionStrategy({"atr_period": 14})
    prepared, signals = strategy.run(_frame(300))

    pd.testing.assert_series_equal(
        signals["stop_loss"].isna(), prepared["atr"].isna(), check_names=False
    )
    assert int(signals["stop_loss"].isna().sum()) == 14


# ---------------------------------------------------------------------------
# 8. the stop loss, including the exact-0.0 rule
# ---------------------------------------------------------------------------


def test_stop_loss_is_all_nan_when_the_multiplier_is_zero() -> None:
    strategy = RsiReversionStrategy(dict(TRUTH_PARAMS, atr_stop_multiplier=0.0))

    prepared, signals = strategy.run(_trend_frame())

    assert signals["stop_loss"].isna().all()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    # 0.0 means "no stop", not "a stop at the close": the ATR itself is defined
    assert not prepared["atr"].isna().all()


def test_stop_loss_is_close_minus_the_atr_distance() -> None:
    strategy = RsiReversionStrategy(dict(TRUTH_PARAMS, atr_stop_multiplier=3.0))
    prepared, signals = strategy.run(_trend_frame())

    expected = prepared["close"] - 3.0 * prepared["atr"]
    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)


# ---------------------------------------------------------------------------
# 9. the parameter model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"rsi_period": 1}, "rsi_period"),
        ({"rsi_period": 0}, "rsi_period"),
        ({"entry_rsi": 0.0}, "entry_rsi"),
        ({"entry_rsi": -10.0}, "entry_rsi"),
        ({"entry_rsi": 100.0}, "entry_rsi"),
        ({"exit_rsi": 0.0}, "exit_rsi"),
        ({"exit_rsi": -1.0}, "exit_rsi"),
        ({"exit_rsi": 101.0}, "exit_rsi"),
        ({"trend_ema_period": 1}, "trend_ema_period"),
        ({"atr_period": 1}, "atr_period"),
        ({"atr_stop_multiplier": -0.1}, "atr_stop_multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        RsiReversionStrategy(params)

    assert field in str(error.value)


@pytest.mark.parametrize(
    "params",
    [
        {"exit_rsi": 10.0, "entry_rsi": 10.0},
        {"exit_rsi": 5.0},
        {"exit_rsi": 9.0, "entry_rsi": 10.0},
    ],
)
def test_the_cross_field_validator_rejects_an_exit_at_or_below_the_entry(
    params: dict[str, object],
) -> None:
    with pytest.raises(StrategyError) as error:
        RsiReversionStrategy(params)

    message = str(error.value)
    assert "exit_rsi" in message
    assert "entry_rsi" in message


def test_an_exit_just_above_the_entry_is_accepted() -> None:
    strategy = RsiReversionStrategy({"entry_rsi": 10.0, "exit_rsi": 10.000001})

    assert strategy.params.model_dump()["exit_rsi"] > strategy.params.model_dump()["entry_rsi"]


def test_unknown_parameter_raises() -> None:
    with pytest.raises(StrategyError, match="unknown_field"):
        RsiReversionStrategy({"unknown_field": 1})


def test_parameters_are_frozen_and_only_their_own_model_is_accepted() -> None:
    strategy = RsiReversionStrategy()

    with pytest.raises(ValidationError):  # the model is frozen
        strategy.params.rsi_period = 5  # type: ignore[misc]

    accepted = RsiReversionStrategy(RsiReversionStrategyParams(rsi_period=3))
    assert accepted.params.model_dump()["rsi_period"] == 3

    with pytest.raises(StrategyError, match="RsiReversionStrategyParams"):
        RsiReversionStrategy(StrategyParams())

    with pytest.raises(StrategyError, match="mapping"):
        RsiReversionStrategy(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 10. warm-up, warning, determinism, layer direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, 201),
        ({"trend_ema_period": 20}, 21),
        ({"trend_ema_period": 20, "atr_period": 50}, 51),
        ({"trend_ema_period": 20, "atr_period": 2, "rsi_period": 30}, 31),
        ({"trend_ema_period": 2, "atr_period": 2, "rsi_period": 2}, 3),
    ],
)
def test_required_candles_is_the_declared_arithmetic(
    params: dict[str, object], expected: int
) -> None:
    assert RsiReversionStrategy(params).required_candles() == expected


@pytest.mark.parametrize("timeframe", sorted(CANDLES_PER_DAY))
def test_required_candles_is_grid_independent(timeframe: str) -> None:
    """All periods are in candles: the declared warm-up never moves with the grid."""
    strategy = RsiReversionStrategy()

    assert strategy.required_candles(CANDLES_PER_DAY[timeframe]) == 201
    assert strategy.required_candles(CANDLES_PER_DAY[timeframe]) == strategy.required_candles()


def test_the_default_requirement_is_one_above_the_platform_warmup_budget() -> None:
    """The honest consequence of the published 200-period trend filter.

    The platform's default ``warmup_candles`` is 200, so a profile of this
    strategy must declare an explicit warm-up: the requirement is documented
    rather than tuned down to fit the default.
    """
    assert RsiReversionStrategy().required_candles() == 201
    assert RsiReversionStrategy().params.model_dump()["trend_ema_period"] == 200


@pytest.mark.parametrize(
    "candles_per_day",
    [0.0, -1.0, -1440.0, float("nan"), float("inf"), float("-inf"), None, "not-a-number"],
)
def test_required_candles_never_raises_for_a_degenerate_grid(candles_per_day: object) -> None:
    required = RsiReversionStrategy().required_candles(candles_per_day)  # type: ignore[arg-type]

    assert isinstance(required, int)
    assert not isinstance(required, bool)
    assert required == 201


def test_required_candles_is_pure_and_repeatable() -> None:
    strategy = RsiReversionStrategy({"trend_ema_period": 30, "atr_period": 12, "rsi_period": 3})
    before = strategy.params.model_dump()

    assert strategy.required_candles(24.0) == strategy.required_candles(24.0) == 31
    assert strategy.params.model_dump() == before


def test_the_declared_warmup_is_enough_for_every_indicator() -> None:
    """The declaration is honest: on ``required`` rows every column is defined."""
    strategy = RsiReversionStrategy()
    required = strategy.required_candles()
    assert required == 201

    prepared = strategy.prepare(_frame(required))
    for column in INDICATOR_COLUMNS:
        assert np.isfinite(prepared[column].iloc[-1]), column

    # the arithmetic behind the declaration: the 200-candle average is defined
    # from row 199 onwards and the declaration adds the candle that follows it,
    # exactly like momentum's `longest lookback + 1` convention
    one_less = strategy.prepare(_frame(required - 1))
    assert int(one_less["trend_ema"].isna().sum()) == 199
    assert np.isfinite(one_less["trend_ema"].iloc[-1])


def test_prepare_warns_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = RsiReversionStrategy()
    with caplog.at_level(logging.WARNING, logger=RSI_LOGGER):
        prepared = strategy.prepare(_frame(100))

    records = [record for record in caplog.records if record.name == RSI_LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert getattr(record, "event", None) == WARMUP_EVENT
    assert getattr(record, "strategy", None) == "rsi_reversion"
    assert getattr(record, "rows", None) == 100
    # the warning quotes the declared warm-up: one number, one helper, no drift
    assert getattr(record, "required_candles", None) == strategy.required_candles() == 201
    message = record.getMessage()
    assert "100 row(s) received" in message
    assert "201 required" in message
    # A warning, never an exception: the frame still comes back prepared.
    assert len(prepared) == 100
    assert "trend_ema" in prepared.columns


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = RsiReversionStrategy()
    with caplog.at_level(logging.DEBUG, logger=RSI_LOGGER):
        strategy.run(_frame(strategy.required_candles()))

    assert [record for record in caplog.records if record.name == RSI_LOGGER] == []


def test_the_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _frame(300)
    snapshot = data.copy(deep=True)
    strategy = RsiReversionStrategy({"allow_short": True})

    prepared_first = strategy.prepare(data)
    prepared_snapshot = prepared_first.copy(deep=True)
    signals_first = strategy.signals(prepared_first)
    signals_second = strategy.signals(prepared_first)
    prepared_second = strategy.prepare(data)

    pd.testing.assert_frame_equal(prepared_first, prepared_second)
    pd.testing.assert_frame_equal(signals_first, signals_second)
    pd.testing.assert_frame_equal(prepared_first, prepared_snapshot)
    pd.testing.assert_frame_equal(data, snapshot)


def test_the_module_never_imports_the_realtime_layer() -> None:
    """The layer direction is frozen: ``strategy`` never imports ``realtime``."""
    source = rsi_reversion_module.__file__
    assert source is not None
    tree = ast.parse(Path(source).read_text(encoding="utf-8"), filename=source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.append(node.module)
    assert imported  # the module really imports something
    assert not [name for name in imported if name.startswith("trading_platform.realtime")]

    # ... and the guard holds at runtime too: a fresh interpreter that imports
    # the module must not end up with any realtime module in sys.modules.
    code = (
        "import sys, trading_platform.strategy.rsi_reversion;"
        "leaked = sorted(n for n in sys.modules if n.startswith('trading_platform.realtime'));"
        "print(leaked)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=120
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


# ---------------------------------------------------------------------------
# end-to-end through the engine
# ---------------------------------------------------------------------------


def test_run_backtest_opens_and_closes_trades(trending_frame: pd.DataFrame) -> None:
    # a 50-candle trend filter keeps the warm-up inside the 600-candle fixture;
    # with the ATR stop disabled every exit comes from the rule itself
    strategy = RsiReversionStrategy({"trend_ema_period": 50, "atr_stop_multiplier": 0.0})

    first = run_backtest(strategy, trending_frame)
    second = run_backtest(strategy, trending_frame)

    assert first.strategy_name == "rsi_reversion"
    assert len(first.equity_curve) == len(trending_frame)
    assert len(first.trades) >= 1
    assert all(trade.exit_time > trade.entry_time for trade in first.trades)
    assert {trade.exit_reason for trade in first.trades} <= set(ExitReason)
    assert any(trade.exit_reason is ExitReason.SIGNAL for trade in first.trades)
    assert not any(trade.exit_reason is ExitReason.STOP_LOSS for trade in first.trades)

    # the run is fully deterministic and never mutates the input frame
    assert first.trades == second.trades
    assert first.final_balance == second.final_balance
    np.testing.assert_allclose(
        first.equity_curve.to_numpy(dtype="float64"),
        second.equity_curve.to_numpy(dtype="float64"),
    )
    assert len(strategy.prepare(trending_frame)) == len(trending_frame)
