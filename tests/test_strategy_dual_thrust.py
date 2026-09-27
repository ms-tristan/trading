"""Behavioural tests of the ``dual_thrust`` strategy (the range-breakout rule).

Everything here is offline and deterministic: the frames are hand-built (or come
from the shared synthetic fixtures), the expected indicator values are
hand-computed, and no test touches the network or a file outside the repository.

The file pins, group by group:

* the parameters, the declared grid and the **deliberate absence** of a
  cross-field validator (only per-field bounds and ``extra="forbid"``);
* :meth:`DualThrustStrategy.prepare` — the OHLCV contract, the exact indicator
  columns, the preserved index, the untouched input, the **shift** that keeps the
  current candle out of its own range and the structured
  ``strategy.warmup_incomplete`` warning;
* :meth:`DualThrustStrategy.required_candles` — ``16`` by default, following the
  lookbacks, identical on the 1m / 1h / 4h / 1d grids and total on a degenerate
  one;
* :meth:`DualThrustStrategy.signals` — the frozen signal contract, the strict
  breakout truth table, the ``NaN`` trap, the ``allow_short`` mirror and the
  exact-``0.0``-means-no-stop rule;
* one end-to-end run through
  :func:`trading_platform.strategy.engine.run_backtest`.

The Dual Thrust symbols are imported from
:mod:`trading_platform.strategy.dual_thrust` on purpose: the strategy is
deliberately **not** re-exported by the package namespace.
"""

from __future__ import annotations

import logging
import math

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
from trading_platform.strategy.base import BOOL_SIGNAL_COLUMNS
from trading_platform.strategy.dual_thrust import (
    INDICATOR_COLUMNS,
    DualThrustParams,
    DualThrustStrategy,
    DualThrustStrategyParams,
)
from trading_platform.strategy.engine import run_backtest

START = "2024-01-01T00:00:00Z"

#: The module logger the warm-up warning is emitted on (frozen contract).
DUAL_THRUST_LOGGER = "trading_platform.strategy.dual_thrust"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

#: The four grids the package contract asks the declaration to be pinned on.
GRIDS: dict[str, float] = {"1m": 1440.0, "1h": 24.0, "4h": 6.0, "1d": 1.0}

#: The hand-built frame of :func:`_hand_frame`, exactly as the module computed it.
HAND_OPEN = [10.0] * 8
HAND_HIGH = [12.0, 11.0, 15.0, 13.0, 13.0, 13.0, 13.0, 13.0]
HAND_LOW = [8.0, 9.0, 5.0, 9.0, 9.0, 9.0, 4.0, 9.0]
HAND_CLOSE = [11.0, 10.0, 14.0, 12.0, 12.0, 12.0, 3.0, 12.0]
HAND_RANGE = [np.nan, np.nan, 3.0, 9.0, 9.0, 3.0, 3.0, 10.0]
HAND_BUY_LINE = [np.nan, np.nan, 11.5, 14.5, 14.5, 11.5, 11.5, 15.0]
HAND_SELL_LINE = [np.nan, np.nan, 8.5, 5.5, 5.5, 8.5, 8.5, 5.0]
HAND_ENTRY_LONG = [False, False, True, False, False, True, False, False]
HAND_EXIT_LONG = [False, False, False, False, False, False, True, False]


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


def _hand_frame() -> pd.DataFrame:
    """The eight-candle frame whose Dual Thrust lines are hand-computed above."""
    index = pd.date_range(START, periods=8, freq="h", tz="UTC", name="timestamp")
    return pd.DataFrame(
        {
            "open": HAND_OPEN,
            "high": HAND_HIGH,
            "low": HAND_LOW,
            "close": HAND_CLOSE,
            "volume": np.full(8, 1.0),
        },
        index=index,
    )


def _relabelled(frame: pd.DataFrame, *, freq: str) -> pd.DataFrame:
    """Return ``frame`` with the same values on a fresh regularly-spaced grid."""
    moved = frame.copy(deep=True)
    moved.index = pd.date_range(START, periods=len(frame), freq=freq, tz="UTC", name="timestamp")
    return moved


def _rows(column: pd.Series) -> list[int]:
    """Return the integer positions where ``column`` is ``True``."""
    return [int(position) for position in np.flatnonzero(column.to_numpy(dtype=bool))]


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = DualThrustStrategy()
    params = strategy.params

    assert strategy.name == "dual_thrust"
    assert isinstance(params, DualThrustStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert DualThrustParams is DualThrustStrategyParams
    assert isinstance(params, DualThrustParams)
    assert params.model_dump() == {
        "range_period": 5,
        "k1": 0.5,
        "k2": 0.5,
        "atr_period": 14,
        "atr_stop_multiplier": 3.0,
        "allow_short": False,
    }
    assert DualThrustStrategy.default_params() == params.model_dump()
    assert DualThrustStrategy.ParamsModel is DualThrustStrategyParams


def test_the_strategy_is_registered_under_its_name() -> None:
    from trading_platform.strategy.registry import STRATEGIES, get_strategy

    assert STRATEGIES["dual_thrust"] is DualThrustStrategy
    assert isinstance(get_strategy("dual_thrust"), DualThrustStrategy)


def test_param_space_matches_the_documented_grid() -> None:
    space = DualThrustStrategy().param_space()

    assert space == {
        "range_period": [3, 5, 10],
        "k1": [0.3, 0.5, 0.7],
        "k2": [0.3, 0.5, 0.7],
        "atr_stop_multiplier": [0.0, 3.0],
    }
    # the robustness sweep is bounded: the grid must stay <= 512 combinations
    assert math.prod(len(values) for values in space.values()) == 54


def test_the_bounds_admit_the_whole_declared_grid() -> None:
    for field, values in DualThrustStrategy.PARAM_SPACE.items():
        for value in values:
            DualThrustStrategy({field: value})


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"range_period": 1}, "range_period"),
        ({"range_period": 0}, "range_period"),
        ({"k1": 0.0}, "k1"),
        ({"k1": -0.5}, "k1"),
        ({"k2": 0.0}, "k2"),
        ({"k2": -0.5}, "k2"),
        ({"atr_period": 1}, "atr_period"),
        ({"atr_stop_multiplier": -0.1}, "atr_stop_multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        DualThrustStrategy(params)

    assert field in str(error.value)


@pytest.mark.parametrize("params", [{"unknown_field": 1}, {"range_days": 5}, {"k3": 0.5}])
def test_unknown_parameters_raise(params: dict[str, object]) -> None:
    # StrategyParams is extra="forbid", so a typo is reported instead of ignored.
    with pytest.raises(StrategyError):
        DualThrustStrategy(params)


@pytest.mark.parametrize(
    "params",
    [
        # asymmetric multipliers are a well-defined Dual Thrust configuration
        {"k1": 0.9, "k2": 0.1},
        {"k1": 0.1, "k2": 0.9},
        # the range lookback and the ATR lookback are independent
        {"range_period": 30, "atr_period": 2},
        {"range_period": 2, "atr_period": 60},
    ],
)
def test_the_model_carries_no_cross_field_validator(params: dict[str, object]) -> None:
    # The absence of a cross-field rule is part of the frozen specification:
    # wrapping these values in a validator would be a silent contract change.
    strategy = DualThrustStrategy(params)

    assert strategy.params.model_dump()[next(iter(params))] == params[next(iter(params))]


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_indicator_columns_and_leaves_the_input_untouched() -> None:
    data = _frame(120)
    before = data.copy(deep=True)

    prepared = DualThrustStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    assert list(INDICATOR_COLUMNS) == [
        "dual_thrust_range",
        "dual_thrust_buy_line",
        "dual_thrust_sell_line",
        "atr",
    ]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


def test_the_range_is_the_shifted_maximum_of_the_two_spans() -> None:
    data = _frame(80)
    strategy = DualThrustStrategy({"range_period": 5, "atr_period": 14})

    prepared = strategy.prepare(data)

    upper = (
        data["high"].rolling(5, min_periods=5).max() - data["close"].rolling(5, min_periods=5).min()
    )
    lower = (
        data["close"].rolling(5, min_periods=5).max() - data["low"].rolling(5, min_periods=5).min()
    )
    expected = pd.concat([upper, lower], axis=1).max(axis=1).shift(1)
    pd.testing.assert_series_equal(prepared["dual_thrust_range"], expected, check_names=False)
    # the rolling window closes on its range_period-th candle and the shift costs
    # one more row, so the range is undefined on rows 0 to 4 and defined on row 5
    assert prepared["dual_thrust_range"].iloc[0:5].isna().all()
    assert np.isfinite(prepared["dual_thrust_range"].iloc[5])


def test_the_buy_and_sell_lines_are_hand_computed_row_by_row() -> None:
    strategy = DualThrustStrategy({"range_period": 2, "k1": 0.5, "k2": 0.5, "atr_period": 2})

    prepared = strategy.prepare(_hand_frame())

    np.testing.assert_allclose(
        prepared["dual_thrust_range"].to_numpy(dtype="float64"), HAND_RANGE, rtol=0.0, atol=1e-12
    )
    np.testing.assert_allclose(
        prepared["dual_thrust_buy_line"].to_numpy(dtype="float64"),
        HAND_BUY_LINE,
        rtol=0.0,
        atol=1e-12,
    )
    np.testing.assert_allclose(
        prepared["dual_thrust_sell_line"].to_numpy(dtype="float64"),
        HAND_SELL_LINE,
        rtol=0.0,
        atol=1e-12,
    )
    # the first row has no previous candle to size a breakout with
    assert np.isnan(prepared["dual_thrust_range"].iloc[0])
    assert np.isnan(prepared["dual_thrust_buy_line"].iloc[0])
    assert np.isnan(prepared["dual_thrust_sell_line"].iloc[0])


def test_dropping_the_shift_would_move_the_breakout_lines() -> None:
    strategy = DualThrustStrategy({"range_period": 2, "k1": 0.5, "k2": 0.5, "atr_period": 2})
    prepared = strategy.prepare(_hand_frame())

    # ``dual_thrust_range`` is already shifted by one, so shifting it back
    # reconstructs the range of the CURRENT candle: the definition the rule
    # rejects.  The two lines must differ, which is what makes the shift real.
    current_range = prepared["dual_thrust_range"].shift(-1)
    unshifted_buy = prepared["open"] + 0.5 * current_range
    unshifted_sell = prepared["open"] - 0.5 * current_range

    for unshifted, line in (
        (unshifted_buy, prepared["dual_thrust_buy_line"]),
        (unshifted_sell, prepared["dual_thrust_sell_line"]),
    ):
        comparable = unshifted.iloc[2:7]
        assert comparable.notna().all()
        assert not np.allclose(
            comparable.to_numpy(dtype="float64"), line.iloc[2:7].to_numpy(dtype="float64")
        )


def test_prepare_computes_the_atr_column() -> None:
    prepared = DualThrustStrategy({"range_period": 3, "atr_period": 6}).prepare(_frame(60))

    assert int(prepared["atr"].isna().sum()) == 6
    assert np.isfinite(prepared["atr"].iloc[6])
    assert prepared["atr"].dropna().ge(0.0).all()


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    with pytest.raises(StrategyError, match="missing required column"):
        DualThrustStrategy().prepare(_frame(30).drop(columns=["open"]))
    with pytest.raises(StrategyError, match="empty frame"):
        DualThrustStrategy().prepare(_frame_from_closes([]))
    with pytest.raises(StrategyError, match="DatetimeIndex"):
        DualThrustStrategy().prepare(_frame(30).reset_index(drop=True))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        DualThrustStrategy().prepare([1.0, 2.0, 3.0])  # type: ignore[arg-type]


def test_prepare_warns_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = DualThrustStrategy()

    with caplog.at_level(logging.WARNING, logger=DUAL_THRUST_LOGGER):
        prepared = strategy.prepare(_frame(10))

    assert len(prepared) == 10
    # the declared floor is the LONGEST lookback, so on this frame the range is
    # already usable while the ATR (and therefore the stop) is still undefined
    assert np.isfinite(prepared["dual_thrust_range"].iloc[-1])
    assert prepared["atr"].isna().all()
    records = [record for record in caplog.records if record.name == DUAL_THRUST_LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("strategy dual_thrust cannot warm up on this frame")
    assert record.__dict__["event"] == WARMUP_EVENT
    assert record.__dict__["strategy"] == "dual_thrust"
    assert record.__dict__["rows"] == 10
    assert record.__dict__["required_candles"] == 16


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=DUAL_THRUST_LOGGER):
        prepared = DualThrustStrategy().prepare(_frame(16))

    assert caplog.records == []
    assert np.isfinite(prepared["dual_thrust_buy_line"].iloc[-1])


def test_prepare_never_raises_on_a_short_frame(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=DUAL_THRUST_LOGGER):
        single = DualThrustStrategy().prepare(_frame(1))

    assert len(single) == 1
    assert single["dual_thrust_range"].isna().all()


# ---------------------------------------------------------------------------
# required_candles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(GRIDS))
def test_required_candles_is_the_declared_floor_on_every_grid(timeframe: str) -> None:
    strategy = DualThrustStrategy()

    # every period is a candle count, so the answer never scales with the grid
    assert strategy.required_candles(GRIDS[timeframe]) == 16
    assert SUPPORTED_TIMEFRAMES[timeframe] > 0.0


def test_required_candles_defaults_to_the_daily_grid() -> None:
    assert DualThrustStrategy().required_candles() == 16


def test_required_candles_follows_the_parameters_not_a_constant() -> None:
    assert DualThrustStrategy({"range_period": 20}).required_candles(24.0) == 22
    assert DualThrustStrategy({"range_period": 5, "atr_period": 30}).required_candles() == 32
    assert DualThrustStrategy({"range_period": 2, "atr_period": 2}).required_candles() == 4


@pytest.mark.parametrize("candles_per_day", [0.0, -1.0, float("nan"), float("inf"), None, "x"])
def test_required_candles_is_total_on_a_degenerate_grid(candles_per_day: object) -> None:
    # the frozen contract makes this method never raise
    assert DualThrustStrategy().required_candles(candles_per_day) == 16  # type: ignore[arg-type]


def test_required_candles_is_pure_and_repeatable() -> None:
    strategy = DualThrustStrategy({"range_period": 10})

    first = strategy.required_candles(6.0)
    second = strategy.required_candles(6.0)

    assert first == second == 16
    assert strategy.params.model_dump()["range_period"] == 10


def test_the_shifted_range_is_undefined_until_range_period_plus_one_candles() -> None:
    strategy = DualThrustStrategy({"range_period": 5, "atr_period": 2})

    exact = strategy.prepare(_frame(6))
    one_short = strategy.prepare(_frame(5))

    # rolling windows close on the range_period-th candle; the shift costs one row
    assert np.isnan(one_short["dual_thrust_range"].iloc[-1])
    assert np.isfinite(exact["dual_thrust_range"].iloc[-1])


def test_the_declared_floor_covers_both_the_shift_and_the_atr() -> None:
    strategy = DualThrustStrategy({"range_period": 3, "atr_period": 20})
    required = strategy.required_candles()

    at_the_floor = strategy.prepare(_frame(required))

    assert required == 22
    assert np.isfinite(at_the_floor["dual_thrust_range"].iloc[-1])
    assert np.isfinite(at_the_floor["atr"].iloc[-1])


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def _hand_params(**overrides: object) -> dict[str, object]:
    """Short candle lookbacks that stay defined on :func:`_hand_frame`."""
    return {"range_period": 2, "k1": 0.5, "k2": 0.5, "atr_period": 2, **overrides}


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = DualThrustStrategy(_hand_params())
    prepared = strategy.prepare(_hand_frame())

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)


def test_the_truth_table_is_a_strict_breakout_of_the_lines() -> None:
    strategy = DualThrustStrategy(_hand_params())
    prepared, signals = strategy.run(_hand_frame())

    assert signals["entry_long"].tolist() == HAND_ENTRY_LONG
    assert signals["exit_long"].tolist() == HAND_EXIT_LONG
    assert _rows(signals["entry_long"]) == _rows(
        prepared["close"] > prepared["dual_thrust_buy_line"]
    )
    assert _rows(signals["exit_long"]) == _rows(
        prepared["close"] < prepared["dual_thrust_sell_line"]
    )
    # row 2 breaks out above the buy line, row 6 breaks down below the sell line
    assert bool(signals["entry_long"].iloc[2])
    assert bool(signals["exit_long"].iloc[6])
    # and the two sides never fire together
    assert not (signals["entry_long"] & signals["exit_long"]).any()


@pytest.mark.parametrize(
    ("last_close", "expected_entry", "expected_exit"),
    [
        (11.0, False, False),  # exactly on the buy line: no breakout
        (9.0, False, False),  # exactly on the sell line: no breakout
        (11.0 + 1e-9, True, False),
        (9.0 - 1e-9, False, True),
    ],
)
def test_an_exact_equality_with_a_line_fires_nothing(
    last_close: float, expected_entry: bool, expected_exit: bool
) -> None:
    # The line of the last row is built from the previous candles only, so the
    # close of that very row can be set freely without moving its own lines.
    closes = [10.0, 10.0, 10.0, 10.0, last_close]
    index = pd.date_range(START, periods=5, freq="h", tz="UTC", name="timestamp")
    frame = pd.DataFrame(
        {
            "open": np.full(5, 10.0),
            "high": np.full(5, 12.0),
            "low": np.full(5, 8.0),
            "close": closes,
            "volume": np.full(5, 1.0),
        },
        index=index,
    )
    strategy = DualThrustStrategy(_hand_params())

    prepared, signals = strategy.run(frame)

    assert prepared["dual_thrust_buy_line"].iloc[4] == pytest.approx(11.0)
    assert prepared["dual_thrust_sell_line"].iloc[4] == pytest.approx(9.0)
    assert bool(signals["entry_long"].iloc[4]) is expected_entry
    assert bool(signals["exit_long"].iloc[4]) is expected_exit


def test_the_short_side_is_all_false_without_allow_short() -> None:
    strategy = DualThrustStrategy(_hand_params())
    _prepared, signals = strategy.run(_hand_frame())

    assert _rows(signals["entry_short"]) == []
    assert _rows(signals["exit_short"]) == []
    assert not signals["entry_short"].any()
    assert not signals["exit_short"].any()


def test_allow_short_mirrors_the_signals() -> None:
    strategy = DualThrustStrategy(_hand_params(allow_short=True))
    prepared, signals = strategy.run(_hand_frame())

    # the break is the same event seen from both sides: a downward break below
    # the sell line closes a long AND opens a short, and the upward break does
    # the symmetric thing
    assert signals["entry_short"].tolist() == HAND_EXIT_LONG
    assert signals["exit_short"].tolist() == HAND_ENTRY_LONG
    assert _rows(signals["entry_short"]) == _rows(
        prepared["close"] < prepared["dual_thrust_sell_line"]
    )
    assert _rows(signals["exit_short"]) == _rows(
        prepared["close"] > prepared["dual_thrust_buy_line"]
    )
    assert _rows(signals["entry_short"]) == _rows(signals["exit_long"])
    assert _rows(signals["exit_short"]) == _rows(signals["entry_long"])
    # the stop column is shared by both directions
    assert signals["stop_loss"].iloc[2] == pytest.approx(
        prepared["close"].iloc[2] - 3.0 * prepared["atr"].iloc[2]
    )


def test_a_nan_range_never_fires_a_signal() -> None:
    frame = _hand_frame()
    # a missing price makes the rolling window -- and therefore the range of the
    # NEXT candles -- undefined; those rows must not fire anything
    frame.loc[frame.index[3], "high"] = np.nan
    strategy = DualThrustStrategy(_hand_params())

    prepared, signals = strategy.run(frame)

    assert int(prepared["dual_thrust_range"].isna().sum()) == 4  # rows 0, 1, 4 and 5
    for row in (4, 5):
        assert np.isfinite(prepared["close"].iloc[row])
        assert np.isnan(prepared["dual_thrust_buy_line"].iloc[row])
        assert np.isnan(prepared["dual_thrust_sell_line"].iloc[row])
        assert not signals["entry_long"].iloc[row]
        assert not signals["exit_long"].iloc[row]
        assert not signals["entry_short"].iloc[row]
        assert not signals["exit_short"].iloc[row]


def test_the_warm_up_rows_never_fire_a_signal() -> None:
    _prepared, signals = DualThrustStrategy(_hand_params()).run(_hand_frame())

    # rows 0 and 1 have no completed range yet
    assert not signals.iloc[0:2][list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()


def test_stop_loss_is_all_nan_when_the_multiplier_is_zero() -> None:
    strategy = DualThrustStrategy(_hand_params(atr_stop_multiplier=0.0))
    prepared, signals = strategy.run(_hand_frame())

    assert signals["stop_loss"].isna().all()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    # 0.0 means "no stop", not "a stop at the close"
    assert not prepared["atr"].isna().all()
    assert (signals["stop_loss"] != prepared["close"]).all()


def test_stop_loss_is_close_minus_the_atr_distance() -> None:
    strategy = DualThrustStrategy(_hand_params())
    prepared, signals = strategy.run(_hand_frame())

    expected = prepared["close"] - 3.0 * prepared["atr"]
    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)
    pd.testing.assert_series_equal(
        signals["stop_loss"].isna(), prepared["atr"].isna(), check_names=False
    )


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        DualThrustStrategy(_hand_params()).signals(_hand_frame())
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        DualThrustStrategy(_hand_params()).signals([1, 2, 3])  # type: ignore[arg-type]


def test_signals_rejects_a_frame_missing_one_indicator_column() -> None:
    prepared = DualThrustStrategy(_hand_params()).prepare(_hand_frame())

    with pytest.raises(StrategyError, match="dual_thrust_buy_line"):
        DualThrustStrategy(_hand_params()).signals(prepared.drop(columns=["dual_thrust_buy_line"]))


def test_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _frame(60)
    snapshot = data.copy(deep=True)
    strategy = DualThrustStrategy({"range_period": 3})

    prepared_first = strategy.prepare(data)
    prepared_snapshot = prepared_first.copy(deep=True)
    signals_first = strategy.signals(prepared_first)
    signals_second = strategy.signals(prepared_first)
    prepared_second = strategy.prepare(data)

    pd.testing.assert_frame_equal(prepared_first, prepared_second)
    pd.testing.assert_frame_equal(signals_first, signals_second)
    pd.testing.assert_frame_equal(prepared_first, prepared_snapshot)
    pd.testing.assert_frame_equal(data, snapshot)


# ---------------------------------------------------------------------------
# end-to-end through the engine
# ---------------------------------------------------------------------------


def test_run_backtest_opens_and_closes_trades(trending_frame: pd.DataFrame) -> None:
    frame = _relabelled(trending_frame, freq="h")
    strategy = DualThrustStrategy()

    first = run_backtest(strategy, frame)
    second = run_backtest(strategy, frame)

    assert first.strategy_name == "dual_thrust"
    assert len(first.equity_curve) == len(frame)
    assert len(first.trades) >= 1
    assert all(trade.exit_time > trade.entry_time for trade in first.trades)
    assert {trade.exit_reason for trade in first.trades} <= set(ExitReason)

    # the run is fully deterministic and never mutates the input frame
    assert first.trades == second.trades
    assert first.final_balance == second.final_balance
    assert len(strategy.prepare(frame)) == len(frame)
