"""Behavioural tests of the ``supertrend`` strategy (the ATR trailing-flip follower).

Everything here is offline and deterministic: the frames are hand-built (or come
from the shared synthetic fixtures), the expected band values are computed **by
hand** (they are written out literally, never re-derived from the
implementation), and no test touches the network or a file outside the
repository.

The file pins, group by group:

* the parameters, the declared grid, and the deliberate **absence** of an
  ``atr_stop_multiplier`` field (the trailing band *is* the stop, so a
  configuration carrying the field must be rejected loudly);
* the canonical recursion itself — the seed row, the band-hold, the band
  ratchet, the flip, the no-flip and the flip-back, all on a hand-computed
  example held by literal expected arrays;
* :meth:`SupertrendStrategy.prepare` — the OHLCV contract, the exact indicator
  columns, the preserved index, the untouched input and the structured
  ``strategy.warmup_incomplete`` warning;
* :meth:`SupertrendStrategy.required_candles` — ``atr_period + 2``, following
  the parameter rather than a constant, identical on the 1m / 1h / 4h / 1d grids
  and total on a degenerate one;
* :meth:`SupertrendStrategy.signals` — the frozen signal contract, the
  price-versus-line truth table, the ``NaN`` trap, the ``allow_short`` mirroring
  and the "the stop is the trailing line, and only below the close" rule;
* one end-to-end run through
  :func:`trading_platform.strategy.engine.run_backtest`.

The supertrend symbols are imported from
:mod:`trading_platform.strategy.supertrend` on purpose: the strategy is
deliberately **not** re-exported by the package namespace.
"""

from __future__ import annotations

import ast
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.constants import (
    REQUIRED_OHLCV_COLUMNS,
    SIGNAL_COLUMNS,
    SUPPORTED_TIMEFRAMES,
)
from trading_platform.core.errors import StrategyError
from trading_platform.core.models import ExitReason
from trading_platform.strategy import supertrend as supertrend_module
from trading_platform.strategy.base import BOOL_SIGNAL_COLUMNS, ensure_signal_frame
from trading_platform.strategy.engine import run_backtest
from trading_platform.strategy.indicators import atr
from trading_platform.strategy.registry import STRATEGIES
from trading_platform.strategy.supertrend import (
    INDICATOR_COLUMNS,
    SupertrendParams,
    SupertrendStrategy,
    SupertrendStrategyParams,
    _supertrend_arrays,
)

START = "2024-01-01T00:00:00Z"

#: The module logger the warm-up warning is emitted on (frozen contract).
SUPERTREND_LOGGER = "trading_platform.strategy.supertrend"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

#: The four grids the package contract asks the declaration to be pinned on.
GRIDS: dict[str, float] = {"1m": 1440.0, "1h": 24.0, "4h": 6.0, "1d": 1.0}

#: The default declared warm-up of the strategy.
DEFAULT_WARMUP = 10 + 2

#: The module path of the strategy, for the source-level guarantees.
MODULE_PATH = Path(supertrend_module.__file__)

# ---------------------------------------------------------------------------
# the hand-computed recursion example
#
# Eight candles, an ATR of 2.0 from the third candle on and a band width of 3.0,
# so ``basic_upper[t] = (high + low) / 2 + 6`` and
# ``basic_lower[t] = (high + low) / 2 - 6``.  Every final band below is computed
# by hand from the canonical rule (the arrays on the right of the comments):
# ---------------------------------------------------------------------------

HAND_HIGH = [10.0, 10.0, 12.0, 12.0, 20.0, 12.0, 10.0, 20.0]
HAND_LOW = [8.0, 8.0, 8.0, 8.0, 14.0, 6.0, 6.0, 14.0]
HAND_CLOSE = [9.0, 9.0, 10.0, 10.5, 18.0, 8.0, 7.0, 19.0]
HAND_ATR = [np.nan, np.nan, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0]

#: ``(high + low) / 2`` of each row of the example.
HAND_MIDPOINT = [9.0, 9.0, 10.0, 10.0, 17.0, 9.0, 8.0, 17.0]
#: ``basic_upper`` of each row (``midpoint + 3 * 2``).
HAND_BASIC_UPPER = [15.0, 15.0, 16.0, 16.0, 23.0, 15.0, 14.0, 23.0]
#: ``basic_lower`` of each row (``midpoint - 3 * 2``).
HAND_BASIC_LOWER = [3.0, 3.0, 4.0, 4.0, 11.0, 3.0, 2.0, 11.0]

#: The expected trailing line, hand-computed row by row in the test below.
HAND_LINE = [np.nan, np.nan, 4.0, 4.0, 11.0, 15.0, 14.0, 11.0]
#: The expected regime, hand-computed row by row in the test below.
HAND_DIRECTION = [np.nan, np.nan, 1.0, 1.0, 1.0, -1.0, -1.0, 1.0]


def _series(values: object, *, name: str = "close", freq: str = "h") -> pd.Series:
    """Build a tz-aware ``float64`` Series from ``values``."""
    array = np.asarray(values, dtype="float64")
    index = pd.date_range(START, periods=array.size, freq=freq, tz="UTC", name="timestamp")
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


def _relabelled(frame: pd.DataFrame, *, freq: str) -> pd.DataFrame:
    """Return ``frame`` with the same values on a fresh regularly-spaced grid."""
    moved = frame.copy(deep=True)
    moved.index = pd.date_range(START, periods=len(frame), freq=freq, tz="UTC", name="timestamp")
    return moved


def _rows(column: pd.Series) -> list[int]:
    """Return the integer positions where ``column`` is ``True``."""
    return [int(position) for position in np.flatnonzero(column.to_numpy(dtype=bool))]


def _hand_arrays(
    *,
    atr_values: object = None,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Return the four hand-built input series of the recursion example."""
    raw = HAND_ATR if atr_values is None else atr_values
    return (
        _series(HAND_HIGH, name="high"),
        _series(HAND_LOW, name="low"),
        _series(HAND_CLOSE, name="close"),
        _series(raw, name="atr"),
    )


def _hand_prepared() -> pd.DataFrame:
    """Build the hand-built *prepared* frame the signals truth table uses."""
    frame = _frame_from_closes(HAND_CLOSE)
    frame["supertrend"] = np.asarray(HAND_LINE, dtype="float64")
    frame["supertrend_direction"] = np.asarray(HAND_DIRECTION, dtype="float64")
    frame["atr"] = np.asarray(HAND_ATR, dtype="float64")
    return frame


# ---------------------------------------------------------------------------
# the canonical recursion, pinned by hand
# ---------------------------------------------------------------------------


def test_the_recursion_reproduces_the_hand_computed_arrays() -> None:
    """Pin the four states of the rule: hold, ratchet, flip and flip-back.

    The rows are computed by hand from the canonical rule
    (``multiplier = 3.0``, ``atr = 2.0`` from row 2 on):

    * row 2 is the **seed** (the first row with a defined ATR):
      ``final_upper = basic_upper = 16``, ``final_lower = basic_lower = 4``,
      ``direction = +1`` and the line is the lower band, ``4``;
    * row 3 **holds** both bands: ``basic_upper = 16`` is not below
      ``final_upper[2] = 16`` and ``close[2] = 10`` is not above it, so the
      upper band stays ``16``; the lower band stays ``4`` for the same reason.
      The regime is unchanged, the line is ``4``;
    * row 4 **ratchets**: ``basic_lower = 11 > final_lower[3] = 4``, so
      ``final_lower[4] = 11``; ``final_upper`` stays ``16``.  ``close[4] = 18``
      is above ``11``, so the regime is still ``+1`` and the line is ``11``;
    * row 5 **flips**: ``basic_upper = 15 < final_upper[4] = 16``, so
      ``final_upper[5] = 15``; ``final_lower`` stays ``11``.  ``close[5] = 8``
      is below ``final_lower[5] = 11``, so the direction becomes ``-1`` and the
      line is the upper band, ``15``;
    * row 6 does **not** flip: ``final_upper[6] = 14``, ``final_lower[6] = 2``
      (``close[5] = 8 < final_lower[5] = 11`` ratchets the lower band down) and
      ``close[6] = 7`` is not above ``final_upper[6] = 14``, so the regime stays
      ``-1`` and the line is ``14``;
    * row 7 **flips back**: ``final_upper[7]`` stays ``14`` (``close[6] = 7`` is
      not above it), ``final_lower[7] = 11`` and ``close[7] = 19`` is above
      ``14``, so the direction is ``+1`` again and the line is ``11``.
    """
    high, low, close, average = _hand_arrays()

    line, direction = _supertrend_arrays(high, low, close, average, 3.0)

    # the hand-built basic bands are what the rule is fed with
    midpoints = [
        (high + low_value) / 2.0 for high, low_value in zip(HAND_HIGH, HAND_LOW, strict=True)
    ]
    assert midpoints == HAND_MIDPOINT
    assert [value + 6.0 for value in midpoints] == HAND_BASIC_UPPER
    assert [value - 6.0 for value in midpoints] == HAND_BASIC_LOWER

    np.testing.assert_allclose(line, HAND_LINE, rtol=0.0, atol=1e-12)
    np.testing.assert_allclose(direction, HAND_DIRECTION, rtol=0.0, atol=1e-12)
    assert line.dtype == np.dtype("float64")
    assert direction.dtype == np.dtype("float64")
    # the four states the rule can be in are all exercised
    assert np.isnan(line[0]) and np.isnan(line[1])
    assert direction[2] == 1.0 and direction[5] == -1.0 and direction[7] == 1.0


def test_the_seed_is_the_first_row_with_a_defined_atr_and_direction_is_plus_one() -> None:
    high, low, close, average = _hand_arrays()

    line, direction = _supertrend_arrays(high, low, close, average, 3.0)

    # the ATR is defined from row 2 on, so the recursion starts there
    assert np.isnan(average.to_numpy(dtype="float64")[:2]).all()
    assert direction[2] == 1.0
    # every earlier row is NaN in *both* columns -- never 0.0
    assert np.isnan(line[:2]).all()
    assert np.isnan(direction[:2]).all()


def test_the_direction_column_only_ever_holds_nan_plus_one_or_minus_one() -> None:
    prepared = SupertrendStrategy().prepare(_frame(200))

    values = prepared["supertrend_direction"].to_numpy(dtype="float64")

    assert prepared["supertrend_direction"].dtype == np.dtype("float64")
    assert set(np.unique(values[~np.isnan(values)])) == {-1.0, 1.0}
    # the undefined prefix is NaN, and it is the *only* other value
    assert np.isnan(values).sum() == 10
    # no zero ever appears: "flat" is not a state of this rule
    assert not (values == 0.0).any()


def test_the_recursion_is_deterministic_and_independent_of_the_frame_length() -> None:
    high, low, close, average = _hand_arrays()
    longer_high = _series([*HAND_HIGH, 22.0, 23.0, 24.0], name="high")
    longer_low = _series([*HAND_LOW, 16.0, 17.0, 18.0], name="low")
    longer_close = _series([*HAND_CLOSE, 21.0, 22.0, 23.0], name="close")
    longer_average = _series([*HAND_ATR, 2.0, 2.0, 2.0], name="atr")

    first = _supertrend_arrays(high, low, close, average, 3.0)
    second = _supertrend_arrays(high, low, close, average, 3.0)
    extended = _supertrend_arrays(longer_high, longer_low, longer_close, longer_average, 3.0)

    np.testing.assert_allclose(first[0], second[0], rtol=0.0, atol=0.0)
    np.testing.assert_allclose(first[1], second[1], rtol=0.0, atol=0.0)
    # the prefix of a longer run is the shorter run, bit for bit
    np.testing.assert_allclose(extended[0][:8], first[0], rtol=0.0, atol=0.0)
    np.testing.assert_allclose(extended[1][:8], first[1], rtol=0.0, atol=0.0)


def test_the_recursion_never_mutates_its_inputs() -> None:
    high, low, close, average = _hand_arrays()
    snapshots = [series.copy(deep=True) for series in (high, low, close, average)]

    _supertrend_arrays(high, low, close, average, 3.0)

    for series, snapshot in zip((high, low, close, average), snapshots, strict=True):
        pd.testing.assert_series_equal(series, snapshot)


def test_a_hole_in_the_atr_after_the_seed_carries_the_bands_forward() -> None:
    # the ATR disappears on row 4: a hole in the input must never reset the
    # trailing line, so the previous final bands and the regime are carried over
    high, low, close, _average = _hand_arrays()
    holed = _series([np.nan, np.nan, 2.0, 2.0, np.nan, 2.0, 2.0, 2.0], name="atr")

    line, direction = _supertrend_arrays(high, low, close, holed, 3.0)

    assert line[4] == line[3] == 4.0
    assert direction[4] == direction[3] == 1.0


def test_the_recursion_is_an_explicit_loop_and_never_a_row_wise_apply() -> None:
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden = {"apply", "expanding", "iterrows", "itertuples"}
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    assert not (called & forbidden), f"forbidden pandas call(s): {sorted(called & forbidden)}"

    # ...and the recursion really is a loop over the arrays
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_supertrend_arrays"
    )
    assert any(isinstance(node, ast.For) for node in ast.walk(function))


@pytest.mark.parametrize("multiplier", [0.0, -3.0, float("nan"), float("inf"), "wide", None])
def test_the_recursion_rejects_an_invalid_multiplier(multiplier: object) -> None:
    high, low, close, average = _hand_arrays()

    with pytest.raises(StrategyError, match="multiplier"):
        _supertrend_arrays(high, low, close, average, multiplier)  # type: ignore[arg-type]


def test_the_recursion_rejects_a_bad_input() -> None:
    high, low, close, average = _hand_arrays()

    with pytest.raises(StrategyError, match="pandas Series"):
        _supertrend_arrays(list(HAND_HIGH), low, close, average, 3.0)  # type: ignore[arg-type]
    with pytest.raises(StrategyError, match="same length"):
        _supertrend_arrays(high, low, close, average.iloc[:4], 3.0)
    with pytest.raises(StrategyError, match="numeric"):
        _supertrend_arrays(high, low, pd.Series(["a"] * 8), average, 3.0)


def test_the_recursion_is_total_on_empty_and_undefined_input() -> None:
    empty = _series([])
    line, direction = _supertrend_arrays(empty, empty, empty, empty, 3.0)
    assert line.size == 0 and direction.size == 0
    assert line.dtype == np.dtype("float64") and direction.dtype == np.dtype("float64")

    high, low, close, _average = _hand_arrays()
    all_nan = _series([np.nan] * 8, name="atr")
    line, direction = _supertrend_arrays(high, low, close, all_nan, 3.0)
    assert np.isnan(line).all()
    assert np.isnan(direction).all()


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = SupertrendStrategy()
    params = strategy.params

    assert strategy.name == "supertrend"
    assert isinstance(params, SupertrendStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert SupertrendParams is SupertrendStrategyParams
    assert isinstance(params, SupertrendParams)
    assert params.model_dump() == {
        "atr_period": 10,
        "multiplier": 3.0,
        "allow_short": False,
    }
    assert SupertrendStrategy.default_params() == params.model_dump()
    assert SupertrendStrategy.ParamsModel is SupertrendStrategyParams


def test_the_strategy_declares_the_frozen_registry_metadata() -> None:
    assert STRATEGIES["supertrend"] is SupertrendStrategy
    assert SupertrendStrategy.name == "supertrend"
    assert SupertrendStrategy.ParamsModel is SupertrendParams


def test_param_space_matches_the_documented_grid() -> None:
    space = SupertrendStrategy().param_space()

    assert space == {"atr_period": [7, 10, 14], "multiplier": [2.0, 3.0, 4.0]}
    # the robustness sweep is bounded: the grid must stay <= 512 combinations
    assert math.prod(len(values) for values in space.values()) == 9


def test_every_documented_combination_validates() -> None:
    space = SupertrendStrategy().param_space()

    for period in space["atr_period"]:
        for width in space["multiplier"]:
            params = SupertrendStrategyParams(atr_period=int(period), multiplier=float(width))
            assert params.atr_period >= 2
            assert params.multiplier > 0.0


def test_the_params_model_has_no_atr_stop_multiplier_field() -> None:
    # the trailing band *is* the stop, so the field has no meaning here -- and a
    # configuration carrying it is rejected loudly instead of being ignored
    assert "atr_stop_multiplier" not in SupertrendStrategyParams.model_fields

    with pytest.raises(StrategyError, match="atr_stop_multiplier"):
        SupertrendStrategy({"atr_stop_multiplier": 3.0})


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"atr_period": 1}, "atr_period"),
        ({"atr_period": 0}, "atr_period"),
        ({"multiplier": 0.0}, "multiplier"),
        ({"multiplier": -1.0}, "multiplier"),
        ({"multiplier": float("nan")}, "multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        SupertrendStrategy(params)

    assert field in str(error.value)


def test_an_infinite_multiplier_is_refused_loudly_by_the_computation() -> None:
    # pydantic's ``gt=0.0`` admits +inf, so the recursion is the last line of
    # defence: it refuses a non-finite width instead of emitting NaNs
    strategy = SupertrendStrategy({"multiplier": float("inf")})
    assert strategy.params.model_dump()["multiplier"] == float("inf")

    with pytest.raises(StrategyError, match="multiplier"):
        strategy.prepare(_frame(40))


def test_unknown_parameter_raises() -> None:
    with pytest.raises(StrategyError, match="unknown_field"):
        SupertrendStrategy({"unknown_field": 1})


def test_allow_short_defaults_to_off_and_can_be_enabled() -> None:
    assert SupertrendStrategy().params.model_dump()["allow_short"] is False
    assert SupertrendStrategy({"allow_short": True}).params.model_dump()["allow_short"] is True


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_indicator_columns_and_leaves_the_input_untouched() -> None:
    data = _frame(120)
    before = data.copy(deep=True)

    prepared = SupertrendStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    assert list(INDICATOR_COLUMNS) == ["supertrend", "supertrend_direction", "atr"]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


def test_prepare_computes_the_documented_indicators() -> None:
    strategy = SupertrendStrategy({"atr_period": 3, "multiplier": 2.0})
    data = _frame(60)

    prepared = strategy.prepare(data)

    average = atr(prepared["high"], prepared["low"], prepared["close"], 3)
    pd.testing.assert_series_equal(prepared["atr"], average, check_names=False)
    line, direction = _supertrend_arrays(
        prepared["high"], prepared["low"], prepared["close"], average, 2.0
    )
    pd.testing.assert_series_equal(
        prepared["supertrend"], pd.Series(line, index=data.index), check_names=False
    )
    pd.testing.assert_series_equal(
        prepared["supertrend_direction"], pd.Series(direction, index=data.index), check_names=False
    )


def test_prepare_seeds_the_recursion_on_the_first_defined_atr_row() -> None:
    strategy = SupertrendStrategy()

    prepared = strategy.prepare(_frame(40))

    first_defined = int(prepared["supertrend"].notna().to_numpy().nonzero()[0][0])
    assert first_defined == strategy.params.model_dump()["atr_period"]
    assert prepared["atr"].iloc[:first_defined].isna().all()
    assert np.isfinite(prepared["atr"].iloc[first_defined])
    assert prepared["supertrend"].iloc[:first_defined].isna().all()
    assert prepared["supertrend_direction"].iloc[:first_defined].isna().all()
    assert prepared["supertrend_direction"].iloc[first_defined] == 1.0


def test_prepare_uses_the_frame_index_it_was_given() -> None:
    data = _frame(80)
    data.index = pd.date_range(START, periods=80, freq="4h", tz="UTC", name="timestamp")

    prepared = SupertrendStrategy().prepare(data)

    assert prepared.index.equals(data.index)
    assert list(prepared.index) == list(data.index)


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    with pytest.raises(StrategyError, match="missing required column"):
        SupertrendStrategy().prepare(_frame(30).drop(columns=["volume"]))
    with pytest.raises(StrategyError, match="empty frame"):
        SupertrendStrategy().prepare(_frame_from_closes([]))
    with pytest.raises(StrategyError, match="DatetimeIndex"):
        SupertrendStrategy().prepare(_frame(30).reset_index(drop=True))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        SupertrendStrategy().prepare([1.0, 2.0, 3.0])  # type: ignore[arg-type]


def test_prepare_warns_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = SupertrendStrategy()

    with caplog.at_level(logging.WARNING, logger=SUPERTREND_LOGGER):
        prepared = strategy.prepare(_frame(DEFAULT_WARMUP - 1))

    assert len(prepared) == DEFAULT_WARMUP - 1
    records = [record for record in caplog.records if record.name == SUPERTREND_LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("strategy supertrend cannot warm up on this frame")
    assert record.__dict__["event"] == WARMUP_EVENT
    assert record.__dict__["strategy"] == "supertrend"
    assert record.__dict__["rows"] == DEFAULT_WARMUP - 1
    assert record.__dict__["required_candles"] == DEFAULT_WARMUP


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=SUPERTREND_LOGGER):
        prepared = SupertrendStrategy().prepare(_frame(DEFAULT_WARMUP))

    assert caplog.records == []
    assert np.isfinite(prepared["supertrend"].iloc[-1])


def test_prepare_never_raises_on_a_short_frame(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=SUPERTREND_LOGGER):
        single = SupertrendStrategy().prepare(_frame(1))

    assert len(single) == 1
    assert single["supertrend"].isna().all()
    assert single["supertrend_direction"].isna().all()


# ---------------------------------------------------------------------------
# required_candles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(GRIDS))
def test_required_candles_is_the_declared_floor_on_every_grid(timeframe: str) -> None:
    strategy = SupertrendStrategy()

    # every period is a candle count, so the answer never scales with the grid
    assert strategy.required_candles(GRIDS[timeframe]) == DEFAULT_WARMUP
    assert SUPPORTED_TIMEFRAMES[timeframe] > 0.0


def test_required_candles_defaults_to_the_declared_floor() -> None:
    assert SupertrendStrategy().required_candles() == 12


def test_required_candles_follows_the_parameters_not_a_constant() -> None:
    assert SupertrendStrategy({"atr_period": 7}).required_candles(24.0) == 9
    assert SupertrendStrategy({"atr_period": 14}).required_candles() == 16
    assert SupertrendStrategy({"atr_period": 200}).required_candles() == 202


@pytest.mark.parametrize("candles_per_day", [None, "3", float("nan"), 0.0, -5.0, float("inf")])
def test_required_candles_is_total_on_a_degenerate_grid(candles_per_day: object) -> None:
    # the argument is ignored (every period is a candle count) and never raises
    assert SupertrendStrategy().required_candles(candles_per_day) == DEFAULT_WARMUP  # type: ignore[arg-type]


def test_required_candles_is_pure_and_repeatable() -> None:
    strategy = SupertrendStrategy()

    assert strategy.required_candles() == strategy.required_candles()
    before = strategy.params.model_dump()
    strategy.required_candles(1440.0)
    assert strategy.params.model_dump() == before


def test_the_declared_floor_covers_the_first_defined_row() -> None:
    strategy = SupertrendStrategy()
    prepared = strategy.prepare(_frame(40))

    first_defined = int(prepared["supertrend"].notna().to_numpy().nonzero()[0][0])

    # the ATR is defined at index atr_period and the recursion seeds on that very
    # row, so the first row that can carry a signal is atr_period + 1 candles in
    # -- and the declared floor adds one row of margin on top of it
    assert first_defined == strategy.params.model_dump()["atr_period"]
    assert first_defined + 1 <= strategy.required_candles() == first_defined + 2


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def _hand_strategy(**overrides: object) -> SupertrendStrategy:
    """A strategy configured like the hand-built prepared frame."""
    return SupertrendStrategy({"atr_period": 3, "multiplier": 3.0, **overrides})


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = _hand_strategy()
    prepared = _hand_prepared()

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)


def test_the_signal_frame_survives_ensure_signal_frame() -> None:
    prepared = _hand_prepared()

    signals = _hand_strategy().signals(prepared)

    validated = ensure_signal_frame(signals, prepared.index)
    pd.testing.assert_frame_equal(validated, signals)


def test_the_truth_table_is_the_close_against_the_trailing_line() -> None:
    prepared = _hand_prepared()
    line = prepared["supertrend"]

    signals = _hand_strategy().signals(prepared)

    # close = [9, 9, 10, 10.5, 18, 8, 7, 19] against
    # line  = [nan, nan, 4, 4, 11, 15, 14, 11]
    assert signals["entry_long"].tolist() == [False, False, True, True, True, False, False, True]
    assert signals["exit_long"].tolist() == [False, False, False, False, False, True, True, False]
    assert _rows(signals["entry_long"]) == _rows(prepared["close"] > line)
    assert _rows(signals["exit_long"]) == _rows(prepared["close"] < line)
    # the two sides never fire together
    assert not (signals["entry_long"] & signals["exit_long"]).any()


def test_a_nan_line_never_fires_a_signal() -> None:
    prepared = _hand_prepared()

    signals = _hand_strategy().signals(prepared)

    undefined = prepared["supertrend"].isna()
    assert int(undefined.sum()) == 2
    assert not signals.loc[undefined, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()
    assert signals.loc[undefined, "stop_loss"].isna().all()

    # the trap: a NaN line filled with 0.0 would have fired an entry on every
    # undefined row (close > 0.0)
    assert (prepared.loc[undefined, "close"] > 0.0).all()


def test_a_nan_injected_in_the_middle_of_the_line_fires_nothing() -> None:
    prepared = _hand_prepared()
    broken = prepared.copy(deep=True)
    broken.loc[broken.index[4], "supertrend"] = np.nan

    signals = _hand_strategy().signals(broken)

    for column in BOOL_SIGNAL_COLUMNS:
        assert not bool(signals[column].iloc[4]), column
    assert np.isnan(signals["stop_loss"].iloc[4])
    # the neighbouring rows are unaffected by the hole
    assert bool(signals["exit_long"].iloc[5])


def test_without_allow_short_the_two_short_columns_stay_false() -> None:
    signals = _hand_strategy().signals(_hand_prepared())

    assert signals["entry_short"].tolist() == [False] * 8
    assert signals["exit_short"].tolist() == [False] * 8
    assert signals["entry_short"].dtype == np.dtype("bool")
    assert signals["exit_short"].dtype == np.dtype("bool")


def test_allow_short_mirrors_the_signals_exactly() -> None:
    prepared = _hand_prepared()
    line = prepared["supertrend"]
    long_signals = _hand_strategy().signals(prepared)
    short_signals = _hand_strategy(allow_short=True).signals(prepared)

    assert _rows(short_signals["entry_short"]) == _rows(prepared["close"] < line)
    assert _rows(short_signals["exit_short"]) == _rows(prepared["close"] > line)
    # where the line is defined the short side is the exact mirror image
    defined = line.notna()
    pd.testing.assert_series_equal(
        short_signals.loc[defined, "entry_short"],
        ~long_signals.loc[defined, "entry_long"],
        check_names=False,
    )
    pd.testing.assert_series_equal(
        short_signals.loc[defined, "exit_short"],
        ~long_signals.loc[defined, "exit_long"],
        check_names=False,
    )
    # ...and while the line is undefined both sides are False: the mirror of a
    # NaN comparison is not a signal
    assert not short_signals.loc[~defined, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()
    assert short_signals["entry_short"].tolist() == [
        False,
        False,
        False,
        False,
        False,
        True,
        True,
        False,
    ]


def test_the_stop_is_the_trailing_line_and_only_while_it_sits_below_the_close() -> None:
    prepared = _hand_prepared()
    line = prepared["supertrend"]

    signals = _hand_strategy().signals(prepared)

    expected = line.where(line < prepared["close"], np.nan)
    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)
    # rows 5 and 6 are a short regime: the line caps the close, so the *long*
    # stop is "no stop" there rather than a price above the market
    assert np.isnan(signals["stop_loss"].iloc[5])
    assert np.isnan(signals["stop_loss"].iloc[6])
    assert signals["stop_loss"].iloc[4] == pytest.approx(11.0)
    assert signals["stop_loss"].iloc[7] == pytest.approx(11.0)
    # the stop is not an ATR distance: the strategy has no stop multiplier
    assert "atr_stop_multiplier" not in SupertrendStrategyParams.model_fields


def test_the_signals_of_a_prepared_frame_are_the_price_against_the_line() -> None:
    # on a realistic prepared frame the four order columns are exactly the
    # comparison of the close with the trailing line
    strategy = SupertrendStrategy()
    prepared = strategy.prepare(_frame(300))

    signals = strategy.signals(prepared)

    line = prepared["supertrend"]
    pd.testing.assert_series_equal(
        signals["entry_long"], prepared["close"] > line, check_names=False
    )
    pd.testing.assert_series_equal(
        signals["exit_long"], prepared["close"] < line, check_names=False
    )
    # the regime and the side of the line agree on every defined row
    defined = line.notna()
    long_rows = defined & (prepared["supertrend_direction"] == 1.0)
    short_rows = defined & (prepared["supertrend_direction"] == -1.0)
    assert (prepared.loc[long_rows, "close"] >= line[long_rows]).all()
    assert (prepared.loc[short_rows, "close"] <= line[short_rows]).all()


def test_the_line_ratchets_inside_a_regime_and_never_gives_up_ground() -> None:
    strategy = SupertrendStrategy()
    prepared = strategy.prepare(_frame(300))

    direction = prepared["supertrend_direction"]
    line = prepared["supertrend"]
    runs = (direction != direction.shift(1)).cumsum()
    for _run, positions in prepared.groupby(runs).groups.items():
        regime = direction.loc[positions].iloc[0]
        if pd.isna(regime):
            continue
        steps = line.loc[positions].diff().dropna()
        if regime == 1.0:
            assert (steps >= 0.0).all()
        else:
            assert (steps <= 0.0).all()


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        _hand_strategy().signals(_frame_from_closes(HAND_CLOSE))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        _hand_strategy().signals([1, 2, 3])  # type: ignore[arg-type]


def test_signals_rejects_a_frame_missing_one_indicator_column() -> None:
    prepared = _hand_prepared()

    with pytest.raises(StrategyError, match="supertrend_direction"):
        _hand_strategy().signals(prepared.drop(columns=["supertrend_direction"]))


def test_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _frame(200)
    snapshot = data.copy(deep=True)
    strategy = SupertrendStrategy()

    prepared_first = strategy.prepare(data)
    prepared_snapshot = prepared_first.copy(deep=True)
    signals_first = strategy.signals(prepared_first)
    signals_second = strategy.signals(prepared_first)
    prepared_second = strategy.prepare(data)

    pd.testing.assert_frame_equal(prepared_first, prepared_second)
    pd.testing.assert_frame_equal(signals_first, signals_second)
    pd.testing.assert_frame_equal(prepared_first, prepared_snapshot)
    pd.testing.assert_frame_equal(data, snapshot)


def test_the_declared_warm_up_prefix_is_identical_on_a_longer_frame() -> None:
    short, short_signals = SupertrendStrategy().run(_frame(60))
    long, long_signals = SupertrendStrategy().run(_frame(200))

    pd.testing.assert_frame_equal(short, long.iloc[: len(short)])
    pd.testing.assert_frame_equal(short_signals, long_signals.iloc[: len(short)])


# ---------------------------------------------------------------------------
# end-to-end through the engine
# ---------------------------------------------------------------------------


def test_run_backtest_opens_and_closes_trades(trending_frame: pd.DataFrame) -> None:
    frame = _relabelled(trending_frame, freq="D")
    strategy = SupertrendStrategy()

    first = run_backtest(strategy, frame)
    second = run_backtest(strategy, frame)

    assert first.strategy_name == "supertrend"
    assert len(first.equity_curve) == len(frame)
    assert len(first.trades) >= 1
    assert all(trade.exit_time > trade.entry_time for trade in first.trades)
    assert {trade.exit_reason for trade in first.trades} <= set(ExitReason)

    # the run is fully deterministic and never mutates the input frame
    assert first.trades == second.trades
    assert first.final_balance == second.final_balance
    np.testing.assert_allclose(
        first.equity_curve.to_numpy(dtype="float64"),
        second.equity_curve.to_numpy(dtype="float64"),
    )
