"""Behavioural tests of the ``bollinger`` mean-reversion strategy.

Everything here is offline and deterministic: the frames are hand-built (or come
from the shared synthetic fixtures), the expected indicator values are
hand-computed or checked against the pandas expression the contract mandates, and
no test touches the network or a file outside the repository.

The ten groups of the file:

1. registration, defaults and the published parameter grid;
2. the OHLCV contract of :meth:`BollingerStrategy.prepare`;
3. the prepared-frame contract (index preserved exactly, input never mutated,
   the documented indicator columns added);
4. the indicator values themselves — the **population** standard deviation the
   published rule is defined with, pinned against ``rolling_std`` and shown to
   differ from the sample estimator;
5. the frozen signal-frame contract, through ``ensure_signal_frame``;
6. the entry / exit truth table on a small hand-built frame that walks the close
   from the lower band to the upper band;
7. ``NaN`` never fires a signal;
8. the ``stop_loss`` column, including the exact-``0.0``-means-no-stop rule;
9. the parameter model: per-field bounds, unknown keys, and the deliberate
   **absence** of a cross-field validator;
10. the declared warm-up, the structured warning, determinism and the frozen
    layer direction (the module never imports ``trading_platform.realtime``).

One end-to-end run through
:func:`trading_platform.strategy.engine.run_backtest` closes the file, proving
the strategy really opens **and** closes trades.
"""

from __future__ import annotations

import ast
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
from trading_platform.strategy import bollinger as bollinger_module
from trading_platform.strategy.base import (
    BOOL_SIGNAL_COLUMNS,
    StrategyParams,
    ensure_signal_frame,
)
from trading_platform.strategy.bollinger import (
    INDICATOR_COLUMNS,
    BollingerParams,
    BollingerStrategy,
    BollingerStrategyParams,
)
from trading_platform.strategy.engine import run_backtest
from trading_platform.strategy.indicators import atr, bollinger_bands, rolling_std
from trading_platform.strategy.registry import STRATEGIES, get_strategy

START = "2024-01-01T00:00:00Z"

#: The module logger the warm-up warning is emitted on.
BOLLINGER_LOGGER = "trading_platform.strategy.bollinger"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

#: Candles per 24-hour day of the grids the warm-up is asserted on.
CANDLES_PER_DAY: dict[str, float] = {"1m": 1440.0, "1h": 24.0, "4h": 6.0, "1d": 1.0}

#: The hand-built frame of the truth table: four flat candles, one deep dip, a
#: full recovery and one spike above the upper band.
TRUTH_CLOSES: tuple[float, ...] = (
    100.0,
    100.0,
    100.0,
    100.0,
    40.0,
    100.0,
    100.0,
    100.0,
    160.0,
    100.0,
    100.0,
)

#: The truth-table parameters: a four-candle window and a one-deviation band make
#: every value hand-computable (see ``test_the_truth_table_...``).
TRUTH_PARAMS: dict[str, object] = {"period": 4, "num_std": 1.0, "atr_period": 2}


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


def _truth_frame() -> pd.DataFrame:
    """Return the hand-built frame of :data:`TRUTH_CLOSES`."""
    return _frame_from_closes(TRUTH_CLOSES)


# ---------------------------------------------------------------------------
# 1. registration, defaults, parameter grid
# ---------------------------------------------------------------------------


def test_the_strategy_is_registered_under_its_published_name() -> None:
    assert BollingerStrategy.name == "bollinger"
    assert STRATEGIES["bollinger"] is BollingerStrategy
    assert get_strategy("bollinger").name == "bollinger"
    assert BollingerStrategy.__module__ == "trading_platform.strategy.bollinger"


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = BollingerStrategy()
    params = strategy.params

    assert isinstance(params, BollingerStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert BollingerParams is BollingerStrategyParams
    assert isinstance(params, BollingerParams)
    assert params.model_dump() == {
        "period": 20,
        "num_std": 2.0,
        "atr_period": 14,
        "atr_stop_multiplier": 3.0,
        "allow_short": False,
    }
    assert BollingerStrategy.default_params() == params.model_dump()


def test_param_space_matches_the_documented_grid() -> None:
    space = BollingerStrategy().param_space()

    assert space == {
        "period": [10, 20, 30],
        "num_std": [1.5, 2.0, 2.5],
        "atr_stop_multiplier": [0.0, 3.0],
    }
    # the robustness sweep is bounded: the grid must stay <= 512 combinations
    assert math.prod(len(values) for values in space.values()) == 18


def test_indicator_columns_are_the_documented_tuple() -> None:
    assert INDICATOR_COLUMNS == (
        "bollinger_mid",
        "bollinger_std",
        "bollinger_upper",
        "bollinger_lower",
        "atr",
    )
    assert bollinger_module.__all__ == [
        "BollingerParams",
        "BollingerStrategy",
        "BollingerStrategyParams",
    ]


def test_allow_short_defaults_to_off_and_can_be_enabled() -> None:
    assert BollingerStrategy().params.model_dump()["allow_short"] is False
    assert BollingerStrategy({"allow_short": True}).params.model_dump()["allow_short"] is True


# ---------------------------------------------------------------------------
# 2. the OHLCV contract of prepare
# ---------------------------------------------------------------------------


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    strategy = BollingerStrategy()

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

    prepared = BollingerStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert len(prepared) == len(data)
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


def test_prepare_preserves_the_index_exactly_even_when_it_is_not_sorted() -> None:
    data = _frame(40).iloc[::-1]
    assert not data.index.is_monotonic_increasing

    prepared = BollingerStrategy().prepare(data)

    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    assert list(prepared.index) == list(data.index)


# ---------------------------------------------------------------------------
# 4. the indicator values
# ---------------------------------------------------------------------------


def test_the_bands_match_the_shared_primitives() -> None:
    data = _frame(80)
    prepared = BollingerStrategy({"period": 20, "num_std": 2.0}).prepare(data)

    bands = bollinger_bands(data["close"], 20, 2.0)
    expected_std = rolling_std(data["close"], 20)

    pd.testing.assert_series_equal(prepared["bollinger_mid"], bands.middle, check_names=False)
    pd.testing.assert_series_equal(prepared["bollinger_upper"], bands.upper, check_names=False)
    pd.testing.assert_series_equal(prepared["bollinger_lower"], bands.lower, check_names=False)
    pd.testing.assert_series_equal(prepared["bollinger_std"], expected_std, check_names=False)


def test_the_bands_are_the_middle_band_plus_or_minus_num_std_deviations() -> None:
    data = _frame(80)
    prepared = BollingerStrategy({"period": 20, "num_std": 2.5}).prepare(data)

    deviation = 2.5 * prepared["bollinger_std"]
    pd.testing.assert_series_equal(
        (prepared["bollinger_mid"] + deviation), prepared["bollinger_upper"], check_names=False
    )
    pd.testing.assert_series_equal(
        (prepared["bollinger_mid"] - deviation), prepared["bollinger_lower"], check_names=False
    )


def test_the_standard_deviation_column_is_the_population_estimator() -> None:
    """``ddof=0`` is the published width — pinned, and shown to differ from ``ddof=1``."""
    data = _frame(60)
    prepared = BollingerStrategy({"period": 10}).prepare(data)

    population = rolling_std(data["close"], 10, ddof=0)
    sample = rolling_std(data["close"], 10, ddof=1)

    pd.testing.assert_series_equal(prepared["bollinger_std"], population, check_names=False)
    # The two estimators differ by exactly sqrt(period / (period - 1)), so the
    # bands cannot be the same: this is why the choice is documented, not implicit.
    assert not prepared["bollinger_std"].iloc[9:].equals(sample.iloc[9:])
    np.testing.assert_allclose(
        population.to_numpy(dtype="float64")[9:],
        sample.to_numpy(dtype="float64")[9:] * np.sqrt(9.0 / 10.0),
        rtol=1e-12,
        atol=1e-12,
    )


def test_the_band_prefix_is_nan_for_exactly_the_first_period_minus_one_rows() -> None:
    data = _frame(50)
    prepared = BollingerStrategy({"period": 20, "atr_period": 14}).prepare(data)

    for column in ("bollinger_mid", "bollinger_std", "bollinger_upper", "bollinger_lower"):
        assert int(prepared[column].isna().sum()) == 19
        assert pd.isna(prepared[column].iloc[18])
        assert np.isfinite(prepared[column].iloc[19])


def test_the_atr_column_is_the_shared_atr_indicator() -> None:
    data = _frame(60)
    prepared = BollingerStrategy({"atr_period": 7}).prepare(data)

    expected = atr(data["high"], data["low"], data["close"], 7)
    pd.testing.assert_series_equal(prepared["atr"], expected, check_names=False)
    assert int(prepared["atr"].isna().sum()) == 7


# ---------------------------------------------------------------------------
# 5. the frozen signal-frame contract
# ---------------------------------------------------------------------------


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = BollingerStrategy(TRUTH_PARAMS)
    prepared = strategy.prepare(_truth_frame())

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
        BollingerStrategy().signals(_frame(60))

    with pytest.raises(StrategyError, match="pandas DataFrame"):
        BollingerStrategy().signals([1, 2, 3])  # type: ignore[arg-type]

    prepared = BollingerStrategy().prepare(_frame(60))
    with pytest.raises(StrategyError, match="bollinger_upper"):
        BollingerStrategy().signals(prepared.drop(columns=["bollinger_upper"]))


def test_a_second_signals_call_returns_an_identical_frame() -> None:
    strategy = BollingerStrategy({"allow_short": True})
    prepared = strategy.prepare(_frame(120))
    snapshot = prepared.copy(deep=True)

    first = strategy.signals(prepared)
    second = strategy.signals(prepared)

    pd.testing.assert_frame_equal(first, second)
    pd.testing.assert_frame_equal(prepared, snapshot)


# ---------------------------------------------------------------------------
# 6. the entry / exit truth table
# ---------------------------------------------------------------------------


def test_the_truth_table_walks_the_close_from_the_lower_band_to_the_upper_band() -> None:
    """The hand-computed signals of :data:`TRUTH_CLOSES`.

    With ``period = 4`` and ``num_std = 1.0`` every band is hand-computable:

    * rows 0-2: the window is not full yet, every indicator is ``NaN`` and no
      signal fires;
    * row 3: the window is the flat ``100 100 100 100`` block, so the bands
      collapse onto the middle (``std = 0``) — the close is *at* the middle, not
      below it, so there is no entry;
    * row 4: the deep dip to ``40`` makes the window ``100 100 100 40``
      (``mid = 85``, ``std = 25.9808``, ``lower = 59.0192``) — the only long
      entry of the frame;
    * rows 5-7: the close is back at ``100``, above the middle band, so the long
      exit fires (and no new entry does);
    * row 8: the spike to ``160`` makes the window ``100 100 100 160``
      (``mid = 115``, ``upper = 140.9808``) — the only short entry of the frame;
    * rows 9-10: the close is back at ``100``, below the middle band, so the
      short exit fires.
    """
    strategy = BollingerStrategy(dict(TRUTH_PARAMS, allow_short=True))

    prepared, signals = strategy.run(_truth_frame())

    assert prepared["bollinger_mid"].iloc[3] == pytest.approx(100.0)
    assert prepared["bollinger_std"].iloc[3] == pytest.approx(0.0)
    assert prepared["bollinger_mid"].iloc[4] == pytest.approx(85.0)
    assert prepared["bollinger_std"].iloc[4] == pytest.approx(25.98076211, rel=1e-8)
    assert prepared["bollinger_mid"].iloc[8] == pytest.approx(115.0)

    assert _rows(signals["entry_long"]) == [4]
    assert _rows(signals["exit_long"]) == [3, 5, 6, 7, 8]
    assert _rows(signals["entry_short"]) == [8]
    assert _rows(signals["exit_short"]) == [3, 4, 9, 10]


def test_entry_long_fires_only_strictly_below_the_lower_band() -> None:
    strategy = BollingerStrategy(TRUTH_PARAMS)
    prepared, signals = strategy.run(_frame(80))

    expected = prepared["close"] < prepared["bollinger_lower"]
    pd.testing.assert_series_equal(signals["entry_long"], expected, check_names=False)
    # row 3 of the truth table sits exactly *on* the lower band: a tag is not an
    # entry, and the strict comparison is the whole difference.
    assert not bool(strategy.run(_truth_frame())[1]["entry_long"].iloc[3])


def test_exit_long_fires_at_or_above_the_middle_band() -> None:
    strategy = BollingerStrategy(TRUTH_PARAMS)
    prepared, signals = strategy.run(_frame(80))

    expected = prepared["close"] >= prepared["bollinger_mid"]
    pd.testing.assert_series_equal(signals["exit_long"], expected, check_names=False)
    # equality counts: the flat block of the truth table closes exactly on the
    # middle band and does exit.
    assert bool(strategy.run(_truth_frame())[1]["exit_long"].iloc[3])


def test_entry_short_fires_only_strictly_above_the_upper_band() -> None:
    strategy = BollingerStrategy(dict(TRUTH_PARAMS, allow_short=True))
    prepared, signals = strategy.run(_frame(80))

    pd.testing.assert_series_equal(
        signals["entry_short"], prepared["close"] > prepared["bollinger_upper"], check_names=False
    )
    pd.testing.assert_series_equal(
        signals["exit_short"], prepared["close"] <= prepared["bollinger_mid"], check_names=False
    )


def test_without_allow_short_the_short_side_stays_false() -> None:
    strategy = BollingerStrategy(TRUTH_PARAMS)
    prepared, signals = strategy.run(_truth_frame())

    assert signals["entry_short"].dtype == np.dtype("bool")
    assert signals["exit_short"].dtype == np.dtype("bool")
    assert _rows(signals["entry_short"]) == []
    assert _rows(signals["exit_short"]) == []
    # the long side is untouched by the flag
    assert _rows(signals["entry_long"]) == [4]
    assert signals.index.equals(prepared.index)


def test_allow_short_mirrors_the_long_side_about_the_middle_band() -> None:
    frame = _frame(120)
    long_only = BollingerStrategy(TRUTH_PARAMS)
    mirrored = BollingerStrategy(dict(TRUTH_PARAMS, allow_short=True))

    long_signals = long_only.run(frame)[1]
    short_signals = mirrored.run(frame)[1]

    for column in ("entry_long", "exit_long", "stop_loss"):
        pd.testing.assert_series_equal(long_signals[column], short_signals[column])
    # the long and the short side are the two sides of the same band pair
    short_prepared = mirrored.prepare(frame)
    pd.testing.assert_series_equal(
        short_signals["entry_short"],
        short_prepared["close"] > short_prepared["bollinger_upper"],
        check_names=False,
    )


# ---------------------------------------------------------------------------
# 7. NaN never fires a signal
# ---------------------------------------------------------------------------


def test_a_nan_indicator_never_fires_a_signal() -> None:
    strategy = BollingerStrategy(dict(TRUTH_PARAMS, allow_short=True))
    prepared, signals = strategy.run(_truth_frame())

    undefined = prepared["bollinger_lower"].isna()
    assert int(undefined.sum()) == 3
    # the trap: a band filled with a neutral value would satisfy `exit_long`
    # (close >= mid) on every early candle, so NaN rows must carry nothing at all
    assert not signals.loc[undefined, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()


def test_a_nan_close_never_fires_a_signal_either() -> None:
    data = _frame(40)
    data.loc[data.index[-1], "close"] = np.nan

    prepared, signals = BollingerStrategy({"allow_short": True}).run(data)

    assert pd.isna(prepared["bollinger_mid"].iloc[-1])
    assert not bool(signals["entry_long"].iloc[-1])
    assert not bool(signals["exit_long"].iloc[-1])
    assert not bool(signals["entry_short"].iloc[-1])
    assert not bool(signals["exit_short"].iloc[-1])


def test_the_stop_column_is_nan_wherever_the_atr_is_undefined() -> None:
    strategy = BollingerStrategy({"atr_period": 14})
    prepared, signals = strategy.run(_frame(60))

    pd.testing.assert_series_equal(
        signals["stop_loss"].isna(), prepared["atr"].isna(), check_names=False
    )
    assert int(signals["stop_loss"].isna().sum()) == 14


# ---------------------------------------------------------------------------
# 8. the stop loss, including the exact-0.0 rule
# ---------------------------------------------------------------------------


def test_stop_loss_is_all_nan_when_the_multiplier_is_zero() -> None:
    strategy = BollingerStrategy(dict(TRUTH_PARAMS, atr_stop_multiplier=0.0))

    prepared, signals = strategy.run(_truth_frame())

    assert signals["stop_loss"].isna().all()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    # 0.0 means "no stop", not "a stop at the close": the ATR itself is defined
    assert not prepared["atr"].isna().all()


def test_stop_loss_is_close_minus_the_atr_distance() -> None:
    strategy = BollingerStrategy(dict(TRUTH_PARAMS, atr_stop_multiplier=3.0))
    prepared, signals = strategy.run(_frame(80))

    expected = prepared["close"] - 3.0 * prepared["atr"]
    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)


# ---------------------------------------------------------------------------
# 9. the parameter model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"period": 1}, "period"),
        ({"period": 0}, "period"),
        ({"period": -20}, "period"),
        ({"num_std": 0.0}, "num_std"),
        ({"num_std": -2.0}, "num_std"),
        ({"atr_period": 1}, "atr_period"),
        ({"atr_period": 0}, "atr_period"),
        ({"atr_stop_multiplier": -0.1}, "atr_stop_multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        BollingerStrategy(params)

    assert field in str(error.value)


def test_unknown_parameter_raises() -> None:
    with pytest.raises(StrategyError, match="unknown_field"):
        BollingerStrategy({"unknown_field": 1})


@pytest.mark.parametrize(
    "params",
    [
        {"period": 2},
        {"period": 2, "num_std": 0.5, "atr_period": 200},
        {"period": 200, "atr_period": 2, "atr_stop_multiplier": 0.0},
        {"period": 3, "num_std": 4.0, "atr_period": 3, "allow_short": True},
    ],
)
def test_the_model_has_no_cross_field_validator(params: dict[str, object]) -> None:
    """The five fields are independent: no combination is refused as incoherent.

    ``bollinger`` has no ordering constraint between its lookbacks (unlike
    ``momentum``), so the model carries per-field bounds only and every legal
    combination of them must build.
    """
    strategy = BollingerStrategy(params)

    assert strategy.name == "bollinger"
    for key, value in params.items():
        assert strategy.params.model_dump()[key] == value


def test_parameters_are_frozen_and_only_their_own_model_is_accepted() -> None:
    strategy = BollingerStrategy()

    with pytest.raises(ValidationError):  # the model is frozen
        strategy.params.period = 5  # type: ignore[misc]

    # the matching model is accepted as-is ...
    assert BollingerStrategy(BollingerStrategyParams(period=10)).params.model_dump()["period"] == 10
    # ... a model of another strategy is not
    with pytest.raises(StrategyError, match="BollingerStrategyParams"):
        BollingerStrategy(StrategyParams())
    # ... and neither is a value that is neither a mapping nor a model
    with pytest.raises(StrategyError, match="mapping"):
        BollingerStrategy(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 10. warm-up, warning, determinism, layer direction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, 21),
        ({"period": 10, "atr_period": 14}, 15),
        ({"period": 30, "atr_period": 5}, 31),
        ({"period": 2, "atr_period": 2}, 3),
    ],
)
def test_required_candles_is_the_declared_arithmetic(
    params: dict[str, object], expected: int
) -> None:
    assert BollingerStrategy(params).required_candles() == expected


@pytest.mark.parametrize("timeframe", sorted(CANDLES_PER_DAY))
def test_required_candles_is_grid_independent(timeframe: str) -> None:
    """All periods are in candles: the declared warm-up never moves with the grid."""
    strategy = BollingerStrategy()

    assert strategy.required_candles(CANDLES_PER_DAY[timeframe]) == 21
    assert strategy.required_candles(CANDLES_PER_DAY[timeframe]) == strategy.required_candles()


@pytest.mark.parametrize(
    "candles_per_day",
    [0.0, -1.0, -1440.0, float("nan"), float("inf"), float("-inf"), None, "not-a-number"],
)
def test_required_candles_never_raises_for_a_degenerate_grid(candles_per_day: object) -> None:
    required = BollingerStrategy().required_candles(candles_per_day)  # type: ignore[arg-type]

    assert isinstance(required, int)
    assert not isinstance(required, bool)
    assert required == 21


def test_required_candles_is_pure_and_repeatable() -> None:
    strategy = BollingerStrategy({"period": 7, "atr_period": 9})
    before = strategy.params.model_dump()

    assert strategy.required_candles(24.0) == strategy.required_candles(24.0) == 10
    assert strategy.params.model_dump() == before


def test_the_declared_warmup_is_enough_for_every_indicator() -> None:
    """The declaration is honest: on ``required`` rows every column is defined.

    ``required = max(period, atr_period) + 1``, so the longest window is exactly
    full on the second-to-last row and one row less leaves the band column
    entirely ``NaN`` — the declaration is the arithmetic, not a loose bound.
    """
    strategy = BollingerStrategy({"period": 20, "atr_period": 14})
    required = strategy.required_candles()
    assert required == 21

    prepared = strategy.prepare(_frame(required))
    for column in INDICATOR_COLUMNS:
        assert np.isfinite(prepared[column].iloc[-1]), column

    longest = strategy.prepare(_frame(20))
    assert np.isfinite(longest["bollinger_mid"].iloc[-1])
    assert np.isfinite(longest["atr"].iloc[-1])

    too_short = strategy.prepare(_frame(10))
    assert too_short["bollinger_mid"].isna().all()
    assert too_short["atr"].isna().all()


def test_prepare_warns_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = BollingerStrategy()
    with caplog.at_level(logging.WARNING, logger=BOLLINGER_LOGGER):
        prepared = strategy.prepare(_frame(10))

    records = [record for record in caplog.records if record.name == BOLLINGER_LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert getattr(record, "event", None) == WARMUP_EVENT
    assert getattr(record, "strategy", None) == "bollinger"
    assert getattr(record, "rows", None) == 10
    # the warning quotes the declared warm-up: one number, one helper, no drift
    assert getattr(record, "required_candles", None) == strategy.required_candles() == 21
    message = record.getMessage()
    assert "10 row(s) received" in message
    assert "21 required" in message
    # A warning, never an exception: the frame still comes back prepared.
    assert len(prepared) == 10
    assert "bollinger_lower" in prepared.columns


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = BollingerStrategy()
    with caplog.at_level(logging.DEBUG, logger=BOLLINGER_LOGGER):
        strategy.run(_frame(strategy.required_candles()))

    assert [record for record in caplog.records if record.name == BOLLINGER_LOGGER] == []


def test_the_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _frame(150)
    snapshot = data.copy(deep=True)
    strategy = BollingerStrategy({"allow_short": True})

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
    source = bollinger_module.__file__
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
        "import sys, trading_platform.strategy.bollinger;"
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
    # with the ATR stop disabled every exit must come from the rule itself, which
    # is what makes this an end-to-end test of the signal columns rather than of
    # the stop column
    strategy = BollingerStrategy({"atr_stop_multiplier": 0.0})

    first = run_backtest(strategy, trending_frame)
    second = run_backtest(strategy, trending_frame)

    assert first.strategy_name == "bollinger"
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


def test_run_backtest_with_the_default_atr_stop_really_stops_out(
    trending_frame: pd.DataFrame,
) -> None:
    """The default stop is not decorative: on this fixture it is what closes the trades."""
    result = run_backtest(BollingerStrategy(), trending_frame)

    assert len(result.trades) >= 1
    assert any(trade.exit_reason is ExitReason.STOP_LOSS for trade in result.trades)
    for trade in result.trades:
        assert trade.stop_price is not None
        assert trade.stop_price < trade.entry_price  # a long stop sits below the entry
