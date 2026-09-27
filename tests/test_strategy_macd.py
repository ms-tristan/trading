"""Behavioural tests of the ``macd`` strategy (classic signal-line trend following).

Everything here is offline and deterministic: the frames are hand-built (or come
from the shared synthetic fixtures), the expected indicator values are
hand-computed or checked against the pandas expression the contract mandates, and
no test touches the network or a file outside the repository.

The file pins, group by group:

* the parameters, the declared grid and the cross-field validator (the slow EMA
  must really be the slow one);
* :meth:`MacdStrategy.prepare` — the OHLCV contract, the exact indicator columns,
  the preserved index, the untouched input, the documented indicator arithmetic
  and the structured ``strategy.warmup_incomplete`` warning;
* :meth:`MacdStrategy.required_candles` — ``47`` by default (``fast + slow +
  signal``), following the parameters rather than a constant, identical on the
  1m / 1h / 4h / 1d grids and total on a degenerate one;
* :meth:`MacdStrategy.signals` — the frozen signal contract, the truth table on a
  hand-built frame, the **state** semantics (an entry holds on a whole run, it is
  not a one-candle crossover event), the ``NaN`` trap, the ``allow_short``
  mirroring and the exact-``0.0``-means-no-stop rule;
* one end-to-end run through
  :func:`trading_platform.strategy.engine.run_backtest`.

The macd symbols are imported from :mod:`trading_platform.strategy.macd` on
purpose: the strategy is deliberately **not** re-exported by the package
namespace.
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
from trading_platform.strategy.base import BOOL_SIGNAL_COLUMNS, ensure_signal_frame
from trading_platform.strategy.engine import run_backtest
from trading_platform.strategy.indicators import atr, ema
from trading_platform.strategy.macd import (
    INDICATOR_COLUMNS,
    MacdParams,
    MacdStrategy,
    MacdStrategyParams,
)
from trading_platform.strategy.registry import STRATEGIES

START = "2024-01-01T00:00:00Z"

#: The module logger the warm-up warning is emitted on (frozen contract).
MACD_LOGGER = "trading_platform.strategy.macd"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

#: The four grids the package contract asks the declaration to be pinned on.
GRIDS: dict[str, float] = {"1m": 1440.0, "1h": 24.0, "4h": 6.0, "1d": 1.0}

#: The default declared warm-up: ``fast + slow + signal`` candles.
DEFAULT_WARMUP = 12 + 26 + 9


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


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = MacdStrategy()
    params = strategy.params

    assert strategy.name == "macd"
    assert isinstance(params, MacdStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert MacdParams is MacdStrategyParams
    assert isinstance(params, MacdParams)
    assert params.model_dump() == {
        "fast_period": 12,
        "slow_period": 26,
        "signal_period": 9,
        "atr_period": 14,
        "atr_stop_multiplier": 3.0,
        "allow_short": False,
    }
    assert MacdStrategy.default_params() == params.model_dump()
    assert MacdStrategy.ParamsModel is MacdStrategyParams


def test_the_strategy_declares_the_frozen_registry_metadata() -> None:
    assert STRATEGIES["macd"] is MacdStrategy
    assert MacdStrategy.name == "macd"
    assert MacdStrategy.ParamsModel is MacdParams


def test_param_space_matches_the_documented_grid() -> None:
    space = MacdStrategy().param_space()

    assert space == {
        "fast_period": [8, 12],
        "slow_period": [21, 26],
        "signal_period": [9, 12],
        "atr_stop_multiplier": [0.0, 3.0],
    }
    # the robustness sweep is bounded: the grid must stay <= 512 combinations
    assert math.prod(len(values) for values in space.values()) == 16


def test_every_documented_combination_satisfies_the_cross_field_invariant() -> None:
    space = MacdStrategy().param_space()

    for fast in space["fast_period"]:
        for slow in space["slow_period"]:
            params = MacdStrategyParams(fast_period=int(fast), slow_period=int(slow))
            assert params.slow_period > params.fast_period


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({"fast_period": 26, "slow_period": 26}, ("fast_period", "slow_period")),
        ({"slow_period": 12}, ("fast_period", "slow_period")),
        ({"fast_period": 30, "slow_period": 21}, ("fast_period", "slow_period")),
    ],
)
def test_incoherent_parameters_raise(params: dict[str, object], fields: tuple[str, str]) -> None:
    with pytest.raises(StrategyError) as error:
        MacdStrategy(params)

    message = str(error.value)
    for field in fields:
        assert field in message, f"the error does not name {field!r}: {message}"


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"fast_period": 1}, "fast_period"),
        ({"fast_period": 0}, "fast_period"),
        ({"slow_period": 2}, "slow_period"),
        ({"signal_period": 1}, "signal_period"),
        ({"signal_period": 0}, "signal_period"),
        ({"atr_period": 1}, "atr_period"),
        ({"atr_stop_multiplier": -0.1}, "atr_stop_multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        MacdStrategy(params)

    assert field in str(error.value)


def test_unknown_parameter_raises() -> None:
    with pytest.raises(StrategyError, match="unknown_field"):
        MacdStrategy({"unknown_field": 1})


def test_allow_short_defaults_to_off_and_can_be_enabled() -> None:
    assert MacdStrategy().params.model_dump()["allow_short"] is False
    assert MacdStrategy({"allow_short": True}).params.model_dump()["allow_short"] is True


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_indicator_columns_and_leaves_the_input_untouched() -> None:
    data = _frame(120)
    before = data.copy(deep=True)

    prepared = MacdStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    assert list(INDICATOR_COLUMNS) == ["macd_line", "macd_signal", "macd_histogram", "atr"]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


def test_prepare_computes_the_documented_indicators() -> None:
    params = MacdStrategyParams(fast_period=3, slow_period=5, signal_period=3, atr_period=3)
    strategy = MacdStrategy(params)
    data = _frame(60)

    prepared = strategy.prepare(data)

    close = prepared["close"]
    line = ema(close, 3) - ema(close, 5)
    pd.testing.assert_series_equal(prepared["macd_line"], line, check_names=False)
    # the signal line is the EMA of the MACD line, NaN prefix included
    pd.testing.assert_series_equal(prepared["macd_signal"], ema(line, 3), check_names=False)
    pd.testing.assert_series_equal(
        prepared["macd_histogram"], line - ema(line, 3), check_names=False
    )
    pd.testing.assert_series_equal(
        prepared["atr"], atr(prepared["high"], prepared["low"], close, 3), check_names=False
    )


def test_the_histogram_is_numerically_the_line_minus_the_signal() -> None:
    prepared = MacdStrategy().prepare(_frame(200))

    difference = prepared["macd_line"] - prepared["macd_signal"]
    pd.testing.assert_series_equal(prepared["macd_histogram"], difference, check_names=False)
    np.testing.assert_allclose(
        prepared["macd_histogram"].dropna().to_numpy(dtype="float64"),
        difference.dropna().to_numpy(dtype="float64"),
        rtol=1e-12,
        atol=1e-12,
    )


def test_prepare_uses_the_frame_index_it_was_given() -> None:
    data = _frame(80)
    data.index = pd.date_range(START, periods=80, freq="4h", tz="UTC", name="timestamp")

    prepared = MacdStrategy().prepare(data)

    assert prepared.index.equals(data.index)
    assert list(prepared.index) == list(data.index)


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    with pytest.raises(StrategyError, match="missing required column"):
        MacdStrategy().prepare(_frame(30).drop(columns=["volume"]))
    with pytest.raises(StrategyError, match="empty frame"):
        MacdStrategy().prepare(_frame_from_closes([]))
    with pytest.raises(StrategyError, match="DatetimeIndex"):
        MacdStrategy().prepare(_frame(30).reset_index(drop=True))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        MacdStrategy().prepare([1.0, 2.0, 3.0])  # type: ignore[arg-type]


def test_prepare_warns_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = MacdStrategy()

    with caplog.at_level(logging.WARNING, logger=MACD_LOGGER):
        prepared = strategy.prepare(_frame(DEFAULT_WARMUP - 1))

    assert len(prepared) == DEFAULT_WARMUP - 1
    records = [record for record in caplog.records if record.name == MACD_LOGGER]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage().startswith("strategy macd cannot warm up on this frame")
    assert record.__dict__["event"] == WARMUP_EVENT
    assert record.__dict__["strategy"] == "macd"
    assert record.__dict__["rows"] == DEFAULT_WARMUP - 1
    assert record.__dict__["required_candles"] == DEFAULT_WARMUP


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=MACD_LOGGER):
        prepared = MacdStrategy().prepare(_frame(DEFAULT_WARMUP))

    assert caplog.records == []
    assert np.isfinite(prepared["macd_signal"].iloc[-1])


def test_prepare_never_raises_on_a_short_frame(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=MACD_LOGGER):
        single = MacdStrategy().prepare(_frame(1))

    assert len(single) == 1
    assert single["macd_line"].isna().all()
    assert single["macd_signal"].isna().all()


# ---------------------------------------------------------------------------
# required_candles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(GRIDS))
def test_required_candles_is_the_declared_floor_on_every_grid(timeframe: str) -> None:
    strategy = MacdStrategy()

    # every period is a candle count, so the answer never scales with the grid
    assert strategy.required_candles(GRIDS[timeframe]) == DEFAULT_WARMUP
    assert SUPPORTED_TIMEFRAMES[timeframe] > 0.0


def test_required_candles_defaults_to_the_declared_sum() -> None:
    assert MacdStrategy().required_candles() == 47


def test_required_candles_follows_the_parameters_not_a_constant() -> None:
    assert (
        MacdStrategy({"fast_period": 3, "slow_period": 5, "signal_period": 2}).required_candles(
            24.0
        )
        == 10
    )
    assert (
        MacdStrategy({"fast_period": 8, "slow_period": 21, "signal_period": 12}).required_candles()
        == 41
    )
    # the declared number is exactly fast + slow + signal on every parameter set
    for fast, slow, signal in ((2, 3, 2), (12, 26, 9), (8, 21, 12), (50, 200, 30)):
        strategy = MacdStrategy({"fast_period": fast, "slow_period": slow, "signal_period": signal})
        assert strategy.required_candles() == fast + slow + signal


@pytest.mark.parametrize("candles_per_day", [None, "3", float("nan"), 0.0, -5.0, float("inf")])
def test_required_candles_is_total_on_a_degenerate_grid(candles_per_day: object) -> None:
    # the argument is ignored (every period is a candle count) and never raises
    assert MacdStrategy().required_candles(candles_per_day) == DEFAULT_WARMUP  # type: ignore[arg-type]


def test_required_candles_is_pure_and_repeatable() -> None:
    strategy = MacdStrategy()

    assert strategy.required_candles() == strategy.required_candles()
    before = strategy.params.model_dump()
    strategy.required_candles(1440.0)
    assert strategy.params.model_dump() == before


def test_the_declared_floor_is_a_safe_upper_bound_over_the_first_defined_row() -> None:
    strategy = MacdStrategy(_hand_params())
    prepared, signals = strategy.run(_hand_frame())

    required = strategy.required_candles()
    first_defined = int(prepared["macd_signal"].notna().to_numpy().nonzero()[0][0])

    # ema() skips the NaN prefix of the MACD line, so the signal line is already
    # defined at index slow + signal - 2 -- earlier than the declared floor
    assert first_defined == 5 + 3 - 2
    assert required == 3 + 5 + 3
    assert first_defined < required - 1
    # the declared floor itself is defined, so a frame of exactly `required`
    # candles can always warm up (the declaration is conservative, never unsafe)
    assert np.isfinite(prepared["macd_signal"].iloc[required - 1])
    # and every row before the *actual* first defined row fires nothing at all
    assert prepared["macd_signal"].iloc[:first_defined].isna().all()
    assert not signals.iloc[:first_defined][list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()


def test_a_frame_of_exactly_the_declared_floor_can_signal() -> None:
    strategy = MacdStrategy(_hand_params())
    closes = np.concatenate(
        (np.full(7, 10.0), np.linspace(11.0, 40.0, strategy.required_candles() - 7))
    )
    frame = _frame_from_closes(closes)
    assert len(frame) == strategy.required_candles()

    _prepared, signals = strategy.run(frame)

    assert bool(signals["entry_long"].iloc[-1])
    assert not bool(signals["exit_long"].iloc[-1])


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def _hand_closes() -> list[float]:
    """Twenty candles: flat, up, down, then a straight decline."""
    return (
        [10.0] * 5
        + [11.0, 12.0, 13.0, 14.0, 15.0]
        + [14.0, 13.0, 12.0, 11.0, 10.0]
        + [9.0, 8.0, 7.0, 6.0, 5.0]
    )


def _hand_frame() -> pd.DataFrame:
    """The hand-built frame the truth table is pinned on."""
    return _frame_from_closes(_hand_closes())


def _hand_params(**overrides: object) -> dict[str, object]:
    """Short candle lookbacks that stay defined on :func:`_hand_frame`."""
    return {
        "fast_period": 3,
        "slow_period": 5,
        "signal_period": 3,
        "atr_period": 3,
        "atr_stop_multiplier": 2.0,
        **overrides,
    }


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = MacdStrategy(_hand_params())
    prepared = strategy.prepare(_hand_frame())

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)


def test_the_signal_frame_survives_ensure_signal_frame() -> None:
    strategy = MacdStrategy(_hand_params())
    prepared = strategy.prepare(_hand_frame())

    signals = strategy.signals(prepared)

    validated = ensure_signal_frame(signals, prepared.index)
    pd.testing.assert_frame_equal(validated, signals)


def test_the_truth_table_is_the_sign_of_the_macd_difference() -> None:
    strategy = MacdStrategy(_hand_params())
    prepared, signals = strategy.run(_hand_frame())

    line = prepared["macd_line"]
    signal_line = prepared["macd_signal"]
    # the hand-computed first rows of the difference (fast 3 / slow 5 / signal 3)
    assert line.iloc[4] == pytest.approx(0.0)
    assert line.iloc[5] == pytest.approx(0.1666666666666667)
    assert np.isnan(signal_line.iloc[5])
    assert signal_line.iloc[6] == pytest.approx(0.2222222222222222)
    assert prepared["macd_histogram"].iloc[6] == pytest.approx(0.13888888888888884)

    expected_entry = [False] * 6 + [True] * 4 + [False] * 10
    expected_exit = [False] * 10 + [True] * 10
    assert signals["entry_long"].tolist() == expected_entry
    assert signals["exit_long"].tolist() == expected_exit
    assert _rows(signals["entry_long"]) == _rows(line > signal_line)
    assert _rows(signals["exit_long"]) == _rows(line < signal_line)


def test_the_entries_are_state_based_not_crossover_events() -> None:
    strategy = MacdStrategy(_hand_params())
    _prepared, signals = strategy.run(_hand_frame())

    # `entry_long` holds on a run of four consecutive candles...
    assert _rows(signals["entry_long"]) == [6, 7, 8, 9]
    assert signals["entry_long"].iloc[6:10].to_numpy(dtype=bool).all()
    # ...and therefore does NOT fire on the crossing candle alone: a crossover
    # event rule would have produced exactly one `True` here
    assert len(_rows(signals["entry_long"])) == 4
    # the same holds for the mirror side, which is a run of ten candles
    assert _rows(signals["exit_long"]) == list(range(10, 20))
    assert signals["exit_long"].iloc[10:].to_numpy(dtype=bool).all()


def test_a_nan_indicator_never_fires_a_signal() -> None:
    strategy = MacdStrategy(_hand_params())
    prepared, signals = strategy.run(_hand_frame())

    undefined = prepared["macd_signal"].isna()
    assert int(undefined.sum()) == 6
    assert not signals.loc[undefined, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()
    # the trap: row 4 has a finite MACD line and an undefined signal line, so a
    # fillna(0) would have fired an entry there (line > 0.0)
    assert prepared["macd_line"].iloc[4] == pytest.approx(0.0)
    assert not bool(signals["entry_long"].iloc[4])


def test_a_nan_injected_in_the_middle_of_the_lines_fires_nothing() -> None:
    strategy = MacdStrategy(_hand_params())
    prepared = strategy.prepare(_hand_frame())
    broken = prepared.copy(deep=True)
    broken.loc[broken.index[12], ["macd_line", "macd_signal"]] = np.nan

    signals = strategy.signals(broken)

    for column in BOOL_SIGNAL_COLUMNS:
        assert not bool(signals[column].iloc[12]), column
    # the stop column does not read the lines: the ATR is defined there
    assert np.isfinite(signals["stop_loss"].iloc[12])
    # the neighbouring rows are unaffected by the hole
    assert bool(signals["entry_long"].iloc[11]) == bool(
        prepared["macd_line"].iloc[11] > prepared["macd_signal"].iloc[11]
    )


def test_without_allow_short_the_two_short_columns_stay_false() -> None:
    strategy = MacdStrategy(_hand_params())
    _prepared, signals = strategy.run(_hand_frame())

    assert signals["entry_short"].tolist() == [False] * 20
    assert signals["exit_short"].tolist() == [False] * 20
    assert signals["entry_short"].dtype == np.dtype("bool")
    assert signals["exit_short"].dtype == np.dtype("bool")


def test_allow_short_mirrors_the_signals_exactly() -> None:
    long_strategy = MacdStrategy(_hand_params())
    short_strategy = MacdStrategy(_hand_params(allow_short=True))
    prepared, long_signals = long_strategy.run(_hand_frame())
    _prepared, short_signals = short_strategy.run(_hand_frame())

    line = prepared["macd_line"]
    signal_line = prepared["macd_signal"]
    assert _rows(short_signals["entry_short"]) == _rows(line < signal_line)
    assert _rows(short_signals["exit_short"]) == _rows(line > signal_line)
    # where the lines are defined the short side is the exact mirror image
    defined = signal_line.notna()
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
    # ...and while the signal line is still undefined both sides are False: the
    # mirror of a NaN comparison is not a signal
    assert not short_signals.loc[~defined, list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()
    assert _rows(short_signals["entry_short"]) == list(range(10, 20))
    assert _rows(short_signals["exit_short"]) == list(range(6, 10))
    assert not bool(short_signals["entry_short"].iloc[6])


def test_stop_loss_is_all_nan_when_the_multiplier_is_zero() -> None:
    strategy = MacdStrategy(_hand_params(atr_stop_multiplier=0.0))
    prepared, signals = strategy.run(_hand_frame())

    assert signals["stop_loss"].isna().all()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    # 0.0 means "no stop", not "a stop at the close"
    assert not prepared["atr"].isna().all()
    assert (signals["stop_loss"] != prepared["close"]).all()


def test_stop_loss_is_close_minus_the_atr_distance() -> None:
    strategy = MacdStrategy(_hand_params())
    prepared, signals = strategy.run(_hand_frame())

    expected = prepared["close"] - 2.0 * prepared["atr"]
    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)
    pd.testing.assert_series_equal(
        signals["stop_loss"].isna(), prepared["atr"].isna(), check_names=False
    )
    assert int(signals["stop_loss"].isna().sum()) == 3


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        MacdStrategy(_hand_params()).signals(_hand_frame())
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        MacdStrategy(_hand_params()).signals([1, 2, 3])  # type: ignore[arg-type]


def test_signals_rejects_a_frame_missing_one_indicator_column() -> None:
    prepared = MacdStrategy(_hand_params()).prepare(_hand_frame())

    with pytest.raises(StrategyError, match="macd_signal"):
        MacdStrategy(_hand_params()).signals(prepared.drop(columns=["macd_signal"]))


def test_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _frame(200)
    snapshot = data.copy(deep=True)
    strategy = MacdStrategy()

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
    short, short_signals = MacdStrategy().run(_frame(60))
    long, long_signals = MacdStrategy().run(_frame(200))

    pd.testing.assert_frame_equal(short, long.iloc[: len(short)])
    pd.testing.assert_frame_equal(short_signals, long_signals.iloc[: len(short)])


# ---------------------------------------------------------------------------
# end-to-end through the engine
# ---------------------------------------------------------------------------


def test_run_backtest_opens_and_closes_trades(trending_frame: pd.DataFrame) -> None:
    frame = _relabelled(trending_frame, freq="D")
    strategy = MacdStrategy()

    first = run_backtest(strategy, frame)
    second = run_backtest(strategy, frame)

    assert first.strategy_name == "macd"
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
