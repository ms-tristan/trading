"""Behavioural tests of the ``faber`` strategy (the long-term MA timing rule).

Everything here is offline and deterministic: the frames are hand-built (or come
from the shared synthetic fixtures), the expected indicator values are
hand-computed or checked against the pandas expression the contract mandates, and
no test touches the network or a file outside the repository.

The file pins, group by group:

* the parameters, the declared grid, and the **deliberate absence** of an
  ``allow_short`` field (a configuration carrying it must be rejected loudly);
* :meth:`FaberStrategy.prepare` — the OHLCV contract, the exact indicator
  columns, the preserved index, the untouched input and the structured
  ``strategy.warmup_incomplete`` warning;
* :meth:`FaberStrategy.required_candles` — ``201`` by default, following
  ``sma_period``, identical on the 1m / 1h / 4h / 1d grids and total on a
  degenerate one;
* :meth:`FaberStrategy.signals` — the frozen signal contract, the strict
  price-versus-average truth table, the ``NaN`` trap, the always-``False`` short
  side and the exact-``0.0``-means-no-stop rule;
* one end-to-end run through
  :func:`trading_platform.strategy.engine.run_backtest`.

The faber symbols are imported from :mod:`trading_platform.strategy.faber` on
purpose: the strategy is deliberately **not** re-exported by the package
namespace.
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
from trading_platform.strategy import faber as faber_module
from trading_platform.strategy.base import BOOL_SIGNAL_COLUMNS
from trading_platform.strategy.engine import run_backtest
from trading_platform.strategy.faber import (
    INDICATOR_COLUMNS,
    FaberParams,
    FaberStrategy,
    FaberStrategyParams,
)

START = "2024-01-01T00:00:00Z"

#: The module logger the warm-up warning is emitted on (frozen contract).
FABER_LOGGER = "trading_platform.strategy.faber"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

#: The four grids the package contract asks the declaration to be pinned on.
GRIDS: dict[str, float] = {"1m": 1440.0, "1h": 24.0, "4h": 6.0, "1d": 1.0}


def _series(values: object, *, name: str = "close", freq: str = "D") -> pd.Series:
    """Build a tz-aware ``float64`` Series from ``values``."""
    array = np.asarray(values, dtype="float64")
    index = pd.date_range(START, periods=array.size, freq=freq, tz="UTC", name="timestamp")
    return pd.Series(array, index=index, name=name, dtype="float64")


def _frame_from_closes(closes: object, *, freq: str = "D") -> pd.DataFrame:
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


def _frame(count: int, *, freq: str = "D") -> pd.DataFrame:
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


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = FaberStrategy()
    params = strategy.params

    assert strategy.name == "faber"
    assert isinstance(params, FaberStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert FaberParams is FaberStrategyParams
    assert isinstance(params, FaberParams)
    assert params.model_dump() == {
        "sma_period": 200,
        "atr_period": 14,
        "atr_stop_multiplier": 0.0,
    }
    assert FaberStrategy.default_params() == params.model_dump()
    assert FaberStrategy.ParamsModel is FaberStrategyParams


def test_the_strategy_is_registered_under_its_name() -> None:
    from trading_platform.strategy.registry import STRATEGIES, get_strategy

    assert STRATEGIES["faber"] is FaberStrategy
    assert isinstance(get_strategy("faber"), FaberStrategy)


def test_param_space_matches_the_documented_grid() -> None:
    space = FaberStrategy().param_space()

    assert space == {"sma_period": [100, 200], "atr_stop_multiplier": [0.0, 3.0]}
    # the robustness sweep is bounded: the grid must stay <= 512 combinations
    assert math.prod(len(values) for values in space.values()) == 4


def test_the_params_model_has_no_allow_short_field() -> None:
    # The published rule is long-only by construction, so the field does not
    # exist at all -- it is not a flag that defaults to False.
    assert "allow_short" not in FaberStrategyParams.model_fields
    assert "allow_short" not in FaberStrategy.default_params()


def test_a_configuration_carrying_allow_short_is_rejected_loudly() -> None:
    # StrategyParams is extra="forbid": passing the field of every other house
    # strategy fails instead of being silently ignored.
    with pytest.raises(StrategyError) as error:
        FaberStrategy({"allow_short": True})

    message = str(error.value)
    assert "allow_short" in message
    assert "faber" in message


def test_the_module_never_reads_an_allow_short_attribute() -> None:
    source = Path(faber_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    read: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "allow_short":
            read.append("name")
        elif isinstance(node, ast.Attribute) and node.attr == "allow_short":
            read.append("attribute")
        elif isinstance(node, ast.Constant) and node.value == "allow_short":
            read.append("key")

    # The only place the identifier may appear is the prose documenting its
    # absence (a docstring is a Constant, so it is counted above): the module
    # must never *read* it.
    assert "name" not in read
    assert "attribute" not in read
    assert "allow_short" in (faber_module.__doc__ or "")


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"sma_period": 1}, "sma_period"),
        ({"sma_period": 0}, "sma_period"),
        ({"atr_period": 1}, "atr_period"),
        ({"atr_period": 0}, "atr_period"),
        ({"atr_stop_multiplier": -0.1}, "atr_stop_multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        FaberStrategy(params)

    assert field in str(error.value)


@pytest.mark.parametrize(
    "params",
    [
        {"unknown_field": 1},
        {"short_days": 5},
        {"allow_short": False},
    ],
)
def test_unknown_parameters_raise(params: dict[str, object]) -> None:
    with pytest.raises(StrategyError):
        FaberStrategy(params)


def test_the_bounds_admit_the_whole_declared_grid() -> None:
    # Every value of PARAM_SPACE must be a legal parameter: the robustness sweep
    # would otherwise spend its budget on validation errors.
    for field, values in FaberStrategy.PARAM_SPACE.items():
        for value in values:
            FaberStrategy({field: value})


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_indicator_columns_and_leaves_the_input_untouched() -> None:
    data = _frame(250)
    before = data.copy(deep=True)

    prepared = FaberStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    assert list(INDICATOR_COLUMNS) == ["faber_sma", "atr"]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


def test_prepare_computes_the_documented_indicators() -> None:
    data = _frame(120)
    strategy = FaberStrategy({"sma_period": 20, "atr_period": 14})

    prepared = strategy.prepare(data)

    pd.testing.assert_series_equal(
        prepared["faber_sma"],
        data["close"].rolling(window=20, min_periods=20).mean(),
        check_names=False,
    )
    # the moving average loses its first sma_period - 1 values
    assert int(prepared["faber_sma"].isna().sum()) == 19
    assert np.isfinite(prepared["faber_sma"].iloc[19])
    # the Wilder ATR is defined from candle atr_period + 1 on
    assert int(prepared["atr"].isna().sum()) == 14
    assert np.isfinite(prepared["atr"].iloc[14])
    assert prepared["atr"].dropna().ge(0.0).all()


def test_prepare_uses_the_frame_index_it_was_given() -> None:
    data = _frame(60)
    data.index = pd.date_range(START, periods=60, freq="4h", tz="UTC", name="timestamp")

    prepared = FaberStrategy({"sma_period": 10}).prepare(data)

    assert prepared.index.equals(data.index)
    assert prepared.index.freqstr == "4h"


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    with pytest.raises(StrategyError, match="missing required column"):
        FaberStrategy().prepare(_frame(30).drop(columns=["volume"]))
    with pytest.raises(StrategyError, match="empty frame"):
        FaberStrategy().prepare(_frame_from_closes([]))
    with pytest.raises(StrategyError, match="DatetimeIndex"):
        FaberStrategy().prepare(_frame(30).reset_index(drop=True))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        FaberStrategy().prepare([1.0, 2.0, 3.0])  # type: ignore[arg-type]


def test_prepare_warns_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = FaberStrategy()

    with caplog.at_level(logging.WARNING, logger=FABER_LOGGER):
        prepared = strategy.prepare(_frame(50))

    assert len(prepared) == 50
    assert prepared["faber_sma"].isna().all()
    records = [record for record in caplog.records if record.name == FABER_LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("strategy faber cannot warm up on this frame")
    assert record.__dict__["event"] == WARMUP_EVENT
    assert record.__dict__["strategy"] == "faber"
    assert record.__dict__["rows"] == 50
    assert record.__dict__["required_candles"] == 201


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=FABER_LOGGER):
        prepared = FaberStrategy().prepare(_frame(201))

    assert caplog.records == []
    assert np.isfinite(prepared["faber_sma"].iloc[-1])


def test_prepare_never_raises_on_a_short_frame(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=FABER_LOGGER):
        single = FaberStrategy().prepare(_frame(1))

    assert len(single) == 1
    assert single["faber_sma"].isna().all()


# ---------------------------------------------------------------------------
# required_candles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(GRIDS))
def test_required_candles_is_the_declared_floor_on_every_grid(timeframe: str) -> None:
    strategy = FaberStrategy()

    # every period is a candle count, so the answer never scales with the grid
    assert strategy.required_candles(GRIDS[timeframe]) == 201
    assert SUPPORTED_TIMEFRAMES[timeframe] > 0.0


def test_required_candles_defaults_to_the_daily_grid() -> None:
    assert FaberStrategy().required_candles() == 201


def test_required_candles_follows_the_parameters_not_a_constant() -> None:
    assert FaberStrategy({"sma_period": 50}).required_candles(24.0) == 51
    assert FaberStrategy({"sma_period": 200, "atr_period": 20}).required_candles() == 201
    # the ATR can be the longer lookback
    assert FaberStrategy({"sma_period": 50, "atr_period": 60}).required_candles() == 61


@pytest.mark.parametrize("candles_per_day", [0.0, -1.0, float("nan"), float("inf"), None, "x"])
def test_required_candles_is_total_on_a_degenerate_grid(candles_per_day: object) -> None:
    # the frozen contract makes this method never raise
    assert FaberStrategy().required_candles(candles_per_day) == 201  # type: ignore[arg-type]


def test_required_candles_is_pure_and_repeatable() -> None:
    strategy = FaberStrategy({"sma_period": 30})

    first = strategy.required_candles(6.0)
    second = strategy.required_candles(6.0)

    assert first == second == 31
    assert strategy.params.model_dump()["sma_period"] == 30


def test_the_declared_floor_covers_both_indicators() -> None:
    strategy = FaberStrategy({"sma_period": 8, "atr_period": 4})
    required = strategy.required_candles()

    prepared = strategy.prepare(_frame(required))

    assert required == 9
    assert np.isfinite(prepared["faber_sma"].iloc[-1])
    assert np.isfinite(prepared["atr"].iloc[-1])


def test_the_declared_floor_is_never_below_the_atr_requirement() -> None:
    # the ATR of atr_period candles is defined from candle atr_period + 1 on,
    # which is exactly the declared floor when the ATR is the longer lookback
    strategy = FaberStrategy({"sma_period": 4, "atr_period": 8})
    required = strategy.required_candles()

    at_the_floor = strategy.prepare(_frame(required))
    one_below = strategy.prepare(_frame(required - 1))

    assert required == 9
    assert np.isfinite(at_the_floor["atr"].iloc[-1])
    assert np.isnan(one_below["atr"].iloc[-1])


def test_the_declared_floor_keeps_one_row_of_margin_over_the_moving_average() -> None:
    # sma() is already defined on its sma_period-th candle, so with an
    # SMA-dominated configuration the declared floor is one row conservative --
    # the documented margin, never a fence that hides a working frame
    strategy = FaberStrategy({"sma_period": 8, "atr_period": 4})

    one_below = strategy.prepare(_frame(strategy.required_candles() - 1))

    assert np.isfinite(one_below["faber_sma"].iloc[-1])
    assert np.isfinite(one_below["atr"].iloc[-1])


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def _hand_frame() -> pd.DataFrame:
    """Nine daily candles whose close crosses a 3-candle average both ways."""
    return _frame_from_closes([10.0, 10.0, 10.0, 11.0, 13.0, 9.0, 7.0, 7.0, 11.0])


def _hand_params(**overrides: object) -> dict[str, object]:
    """Short candle lookbacks that stay defined on :func:`_hand_frame`."""
    return {"sma_period": 3, "atr_period": 3, "atr_stop_multiplier": 3.0, **overrides}


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = FaberStrategy(_hand_params())
    prepared = strategy.prepare(_hand_frame())

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)


def test_the_truth_table_is_a_strict_crossing_of_the_moving_average() -> None:
    strategy = FaberStrategy(_hand_params())
    prepared, signals = strategy.run(_hand_frame())

    average = prepared["faber_sma"]
    assert average.iloc[2] == pytest.approx(10.0)
    assert prepared["close"].iloc[2] == pytest.approx(10.0)
    assert average.iloc[4] == pytest.approx(11.333333333333334)

    expected_entry = [False, False, False, True, True, False, False, False, True]
    expected_exit = [False, False, False, False, False, True, True, True, False]
    assert signals["entry_long"].tolist() == expected_entry
    assert signals["exit_long"].tolist() == expected_exit

    # equality fires nothing: row 2 sits exactly on the average
    assert not bool(signals["entry_long"].iloc[2])
    assert not bool(signals["exit_long"].iloc[2])
    # and the two sides are strict complements of each other
    assert not (signals["entry_long"] & signals["exit_long"]).any()
    assert _rows(signals["entry_long"]) == _rows(prepared["close"] > average)
    assert _rows(signals["exit_long"]) == _rows(prepared["close"] < average)


def test_the_short_side_is_always_false_even_though_there_is_no_allow_short() -> None:
    strategy = FaberStrategy(_hand_params())
    _prepared, signals = strategy.run(_hand_frame())

    assert signals["entry_short"].tolist() == [False] * 9
    assert signals["exit_short"].tolist() == [False] * 9
    assert signals["entry_short"].dtype == np.dtype("bool")
    assert signals["exit_short"].dtype == np.dtype("bool")


def _nan_close_frame() -> pd.DataFrame:
    """Twenty daily candles with one missing close in the middle."""
    values = 100.0 + np.arange(20, dtype="float64")
    values[10] = np.nan
    index = pd.date_range(START, periods=20, freq="D", tz="UTC", name="timestamp")
    close = pd.Series(values, index=index, dtype="float64")
    return pd.DataFrame(
        {
            "open": close.ffill().to_numpy(dtype="float64"),
            "high": np.full(20, 130.0),
            "low": np.full(20, 90.0),
            "close": close,
            "volume": np.full(20, 1.0),
        },
        index=index,
    )


def test_a_nan_indicator_never_fires_a_signal() -> None:
    strategy = FaberStrategy(_hand_params())
    prepared, signals = strategy.run(_nan_close_frame())

    undefined = prepared["faber_sma"].isna()
    assert int(undefined.sum()) == 5  # rows 0, 1 and the three windows holding row 10
    assert not signals.loc[undefined, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()

    # the trap: row 12 has a finite close and a NaN average, so a fillna(0) would
    # have fired an entry there
    assert np.isfinite(prepared["close"].iloc[12])
    assert np.isnan(prepared["faber_sma"].iloc[12])
    assert not bool(signals["entry_long"].iloc[12])


def test_stop_loss_is_all_nan_with_the_default_multiplier() -> None:
    strategy = FaberStrategy(_hand_params(atr_stop_multiplier=0.0))
    prepared, signals = strategy.run(_hand_frame())

    # the default multiplier is 0.0, which means "no stop at all"
    assert strategy.params.model_dump()["atr_stop_multiplier"] == 0.0
    assert signals["stop_loss"].isna().all()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    # 0.0 means "no stop", not "a stop at the close"
    assert not prepared["atr"].isna().all()
    assert (signals["stop_loss"] != prepared["close"]).all()


def test_stop_loss_is_close_minus_the_atr_distance() -> None:
    strategy = FaberStrategy(_hand_params())
    prepared, signals = strategy.run(_hand_frame())

    expected = prepared["close"] - 3.0 * prepared["atr"]
    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)
    # NaN wherever the ATR is not defined yet (the first atr_period candles)
    pd.testing.assert_series_equal(
        signals["stop_loss"].isna(), prepared["atr"].isna(), check_names=False
    )


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        FaberStrategy(_hand_params()).signals(_hand_frame())
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        FaberStrategy(_hand_params()).signals([1, 2, 3])  # type: ignore[arg-type]


def test_signals_rejects_a_frame_missing_one_indicator_column() -> None:
    prepared = FaberStrategy(_hand_params()).prepare(_hand_frame())

    with pytest.raises(StrategyError, match="faber_sma"):
        FaberStrategy(_hand_params()).signals(prepared.drop(columns=["faber_sma"]))


def test_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _frame(60)
    snapshot = data.copy(deep=True)
    strategy = FaberStrategy({"sma_period": 10})

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
    frame = _relabelled(trending_frame, freq="D")
    strategy = FaberStrategy({"sma_period": 20, "atr_period": 14, "atr_stop_multiplier": 3.0})

    first = run_backtest(strategy, frame)
    second = run_backtest(strategy, frame)

    assert first.strategy_name == "faber"
    assert len(first.equity_curve) == len(frame)
    assert len(first.trades) >= 1
    assert all(trade.exit_time > trade.entry_time for trade in first.trades)
    assert {trade.exit_reason for trade in first.trades} <= set(ExitReason)

    # the run is fully deterministic and never mutates the input frame
    assert first.trades == second.trades
    assert first.final_balance == second.final_balance
    assert len(strategy.prepare(frame)) == len(frame)
