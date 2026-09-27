"""Behavioural tests of the ``keltner`` volatility-breakout strategy.

Everything here is offline and deterministic: the frames are hand-built from an
explicit arithmetic series, the expected signals are written out row by row and
the indicator columns are checked against the indicator helpers the contract
mandates.  No test touches the network, the wall clock or a checked-in binary.

Six groups live in this file:

* the **parameters** — the documented defaults, the frozen ``PARAM_SPACE`` grid,
  the cross-field validator, the unknown-key rejection and the proof that every
  cartesian combination of the grid validates (this is what keeps the
  ``robustness`` sweep runnable);
* **prepare** — the OHLCV contract, the exact indicator columns, the preserved
  index, the untouched input and the structured warm-up warning;
* **required_candles** — the declared warm-up on the 1m / 1h / 4h / 1d grids and
  its totality for every degenerate argument;
* **signals** — the frozen signal frame, the entry / exit truth table, the
  ``NaN`` rule, the short mirroring and the ATR stop;
* the **end-to-end** run through :func:`trading_platform.strategy.engine.run_backtest`,
  proving the strategy really opens and closes trades.

The strategy symbols are imported from :mod:`trading_platform.strategy.keltner`
on purpose: the strategy is deliberately **not** re-exported by the package
namespace.
"""

from __future__ import annotations

import itertools
import logging
import math
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_platform.core.constants import (
    REQUIRED_OHLCV_COLUMNS,
    SIGNAL_COLUMNS,
    timeframe_minutes,
)
from trading_platform.core.errors import StrategyError
from trading_platform.core.models import ExitReason
from trading_platform.strategy.base import BOOL_SIGNAL_COLUMNS
from trading_platform.strategy.engine import run_backtest
from trading_platform.strategy.indicators import atr, ema
from trading_platform.strategy.keltner import (
    INDICATOR_COLUMNS,
    KeltnerParams,
    KeltnerStrategy,
    KeltnerStrategyParams,
)

#: The module logger the warm-up warning is emitted on (frozen contract).
KELTNER_LOGGER = "trading_platform.strategy.keltner"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

START = "2024-01-01T00:00:00Z"

#: Candles per 24-hour day of the grids the platform supports, computed from the
#: core authority (``trading_platform.realtime.warmup`` is NOT importable from
#: the strategy layer, and the strategy must not need it anyway).
CANDLES_PER_DAY: dict[str, float] = {
    timeframe: 1440.0 / timeframe_minutes(timeframe) for timeframe in ("1m", "1h", "4h", "1d")
}

#: The declared warm-up of the default parameters (trend 100 / channel 20 / ATR 10).
EXPECTED_WARMUP = 101

#: Small, fully defined parameters whose bands are wide enough to be broken by a
#: single candle on the hand-built frame.
HAND_PARAMS: dict[str, Any] = {
    "ema_period": 5,
    "atr_period": 5,
    "atr_multiplier": 2.0,
    "trend_ema_period": 8,
    "atr_stop_multiplier": 2.0,
}


def _index(count: int, *, freq: str = "h") -> pd.DatetimeIndex:
    """Build the tz-aware, named index every hand-built frame carries."""
    return pd.date_range(START, periods=count, freq=freq, tz="UTC", name="timestamp")


def _frame_from_closes(closes: object, *, freq: str = "h") -> pd.DataFrame:
    """Build an OHLCV frame whose candles straddle the open and the close.

    ``open[t]`` is the previous close (the first row opens on its own close),
    ``high`` is ``max(open, close) + 0.5`` and ``low`` is
    ``min(open, close) - 0.5``, so a gap of size ``G`` really produces a candle
    of range ``G + 1`` and the hand-computed expectations stay exact.
    """
    values = np.asarray(closes, dtype="float64")
    opens = np.concatenate(([values[0]], values[:-1])) if values.size else values
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, values) + 0.5,
            "low": np.minimum(opens, values) - 0.5,
            "close": values,
            "volume": np.full(values.size, 1.0),
        },
        index=_index(values.size, freq=freq),
    )


def _ramp_frame(count: int, *, freq: str = "h") -> pd.DataFrame:
    """Build a deterministic, NaN-free OHLCV frame of ``count`` rising candles."""
    steps = np.arange(count, dtype="float64")
    return _frame_from_closes(100.0 + 0.05 * steps + 10.0 * np.sin(steps / 17.0), freq=freq)


def _hand_frame() -> pd.DataFrame:
    """30 hourly candles whose Keltner behaviour is obvious by construction.

    Twenty flat candles at 100 are interrupted by an upward gap to 110 (row 10)
    and a downward gap to 85 (row 20); every other candle is the flat 100.  The
    channel is therefore only ever broken by the two gaps, and the mid band is
    only ever re-crossed on the candle that follows them.
    """
    closes = [100.0] * 10 + [110.0] + [100.0] * 9 + [85.0] + [100.0] * 9
    return _frame_from_closes(closes)


def _rows(column: pd.Series) -> list[int]:
    """Return the integer positions where ``column`` is ``True``."""
    return [int(position) for position in np.flatnonzero(column.to_numpy(dtype=bool))]


def _warning_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the structured warm-up warnings captured for this module."""
    return [record for record in caplog.records if record.name == KELTNER_LOGGER]


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = KeltnerStrategy()
    params = strategy.params

    assert strategy.name == "keltner"
    assert isinstance(params, KeltnerStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert KeltnerParams is KeltnerStrategyParams
    assert isinstance(params, KeltnerParams)
    assert params.model_dump() == {
        "ema_period": 20,
        "atr_period": 10,
        "atr_multiplier": 2.0,
        "trend_ema_period": 100,
        "atr_stop_multiplier": 3.0,
        "allow_short": False,
    }
    assert KeltnerStrategy.default_params() == params.model_dump()


def test_param_space_matches_the_documented_grid() -> None:
    space = KeltnerStrategy().param_space()

    assert space == {
        "ema_period": [20, 40],
        "trend_ema_period": [100, 150],
        "atr_multiplier": [1.5, 2.0, 3.0],
        "atr_stop_multiplier": [0.0, 3.0],
    }
    assert math.prod(len(values) for values in space.values()) == 24


def test_every_cartesian_combination_of_the_param_space_validates() -> None:
    # The robustness sweep builds the full product of PARAM_SPACE: every single
    # combination must be a valid parameter set, otherwise the sweep crashes.
    space = KeltnerStrategy.PARAM_SPACE
    combinations = list(itertools.product(*space.values()))

    assert len(combinations) == 24
    for combination in combinations:
        params = dict(zip(space, combination, strict=True))
        strategy = KeltnerStrategy(params)
        model = KeltnerStrategyParams(**params)
        assert isinstance(strategy.params, KeltnerStrategyParams)
        # every combination keeps the trend filter longer than the channel EMA
        assert model.trend_ema_period > model.ema_period


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({"ema_period": 20, "trend_ema_period": 20}, ("ema_period", "trend_ema_period")),
        ({"ema_period": 40, "trend_ema_period": 20}, ("ema_period", "trend_ema_period")),
        ({"ema_period": 100, "trend_ema_period": 100}, ("ema_period", "trend_ema_period")),
    ],
)
def test_incoherent_parameters_raise(params: dict[str, object], fields: tuple[str, str]) -> None:
    with pytest.raises(StrategyError) as error:
        KeltnerStrategy(params)

    message = str(error.value)
    for field in fields:
        assert field in message, f"the error does not name {field!r}: {message}"


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"ema_period": 1}, "ema_period"),
        ({"ema_period": 0}, "ema_period"),
        ({"atr_period": 1}, "atr_period"),
        ({"atr_period": 0}, "atr_period"),
        ({"trend_ema_period": 1}, "trend_ema_period"),
        ({"atr_multiplier": 0.0}, "atr_multiplier"),
        ({"atr_multiplier": -1.0}, "atr_multiplier"),
        ({"atr_stop_multiplier": -0.1}, "atr_stop_multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        KeltnerStrategy(params)

    assert field in str(error.value)


def test_unknown_parameter_raises() -> None:
    with pytest.raises(StrategyError, match="unknown_field"):
        KeltnerStrategy({"unknown_field": 1})


def test_allow_short_defaults_to_off_and_can_be_enabled() -> None:
    assert KeltnerStrategy().params.model_dump()["allow_short"] is False
    assert KeltnerStrategy({"allow_short": True}).params.model_dump()["allow_short"] is True


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_indicator_columns_and_leaves_the_input_untouched() -> None:
    data = _ramp_frame(200)
    before = data.copy(deep=True)

    prepared = KeltnerStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    assert list(INDICATOR_COLUMNS) == [
        "keltner_mid",
        "keltner_upper",
        "keltner_lower",
        "keltner_trend",
        "atr",
    ]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


def test_prepare_computes_the_channel_the_trend_filter_and_the_atr() -> None:
    data = _ramp_frame(200)
    strategy = KeltnerStrategy()

    prepared = strategy.prepare(data)

    middle = ema(data["close"], 20)
    trend = ema(data["close"], 100)
    average = atr(data["high"], data["low"], data["close"], 10)
    pd.testing.assert_series_equal(prepared["keltner_mid"], middle, check_names=False)
    pd.testing.assert_series_equal(
        prepared["keltner_upper"], middle + 2.0 * average, check_names=False
    )
    pd.testing.assert_series_equal(
        prepared["keltner_lower"], middle - 2.0 * average, check_names=False
    )
    pd.testing.assert_series_equal(prepared["keltner_trend"], trend, check_names=False)
    pd.testing.assert_series_equal(prepared["atr"], average, check_names=False)
    # ema() loses its first period - 1 values, the ATR its first atr_period ones
    assert int(prepared["keltner_mid"].isna().sum()) == 19
    assert int(prepared["keltner_trend"].isna().sum()) == 99
    assert int(prepared["atr"].isna().sum()) == 10


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    with pytest.raises(StrategyError, match="missing required column"):
        KeltnerStrategy().prepare(_ramp_frame(30).drop(columns=["volume"]))
    with pytest.raises(StrategyError, match="index must be a DatetimeIndex"):
        KeltnerStrategy().prepare(_ramp_frame(30).reset_index(drop=True))
    with pytest.raises(StrategyError, match="empty frame"):
        KeltnerStrategy().prepare(_frame_from_closes([]))
    with pytest.raises(StrategyError, match="not convertible to float64"):
        KeltnerStrategy().prepare(_ramp_frame(30).assign(close="not-a-number"))
    with pytest.raises(StrategyError, match="must be a pandas DataFrame"):
        KeltnerStrategy().prepare([1.0, 2.0, 3.0])  # type: ignore[arg-type]


def test_prepare_warns_once_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = KeltnerStrategy()

    with caplog.at_level(logging.WARNING, logger=KELTNER_LOGGER):
        prepared = strategy.prepare(_ramp_frame(EXPECTED_WARMUP - 1))

    records = _warning_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.__dict__["event"] == WARMUP_EVENT
    assert record.__dict__["strategy"] == "keltner"
    assert record.__dict__["rows"] == EXPECTED_WARMUP - 1
    assert record.__dict__["required_candles"] == EXPECTED_WARMUP
    # a short frame is legitimate: the strategy never raises for it
    assert len(prepared) == EXPECTED_WARMUP - 1


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=KELTNER_LOGGER):
        KeltnerStrategy().prepare(_ramp_frame(EXPECTED_WARMUP))

    assert _warning_records(caplog) == []


# ---------------------------------------------------------------------------
# required_candles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(CANDLES_PER_DAY, key=timeframe_minutes))
def test_required_candles_declares_the_documented_requirement_on_every_grid(
    timeframe: str,
) -> None:
    required = KeltnerStrategy().required_candles(CANDLES_PER_DAY[timeframe])

    assert isinstance(required, int)
    assert not isinstance(required, bool)
    # the periods are counted in candles, so the declared warm-up is grid-independent
    assert required == EXPECTED_WARMUP


def test_required_candles_follows_the_parameters_not_a_constant() -> None:
    wide_trend = KeltnerStrategy({"ema_period": 40, "trend_ema_period": 150})
    long_atr = KeltnerStrategy({"ema_period": 5, "atr_period": 60, "trend_ema_period": 8})
    tiny = KeltnerStrategy({"ema_period": 2, "atr_period": 2, "trend_ema_period": 3})

    assert wide_trend.required_candles() == 151
    assert long_atr.required_candles(1440.0) == 61
    assert tiny.required_candles(6.0) == 4


@pytest.mark.parametrize(
    "candles_per_day",
    [
        1.0,
        6.0,
        24.0,
        1440.0,
        0.0,
        -1.0,
        -1440.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        None,
        "x",
    ],
)
def test_required_candles_never_raises_and_ignores_the_grid(candles_per_day: Any) -> None:
    strategy = KeltnerStrategy()

    assert strategy.required_candles(candles_per_day) == EXPECTED_WARMUP  # type: ignore[arg-type]


def test_required_candles_is_pure_and_repeatable() -> None:
    strategy = KeltnerStrategy({"ema_period": 20, "trend_ema_period": 50})
    before = strategy.params.model_dump()

    assert strategy.required_candles(24.0) == strategy.required_candles(24.0) == 51
    assert strategy.params.model_dump() == before


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = KeltnerStrategy(HAND_PARAMS)
    prepared = strategy.prepare(_hand_frame())

    signals = strategy.signals(prepared)

    assert list(signals.columns) == list(SIGNAL_COLUMNS)
    for column in BOOL_SIGNAL_COLUMNS:
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].isna().any()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    assert signals.index.equals(prepared.index)
    assert signals.index.name == "timestamp"


def test_entry_and_exit_truth_table_row_by_row() -> None:
    """The expected signals of :func:`_hand_frame`, written out row by row.

    ``entry_long`` fires on the upward gap only (row 10): the close is above the
    upper band *and* above the long trend EMA.  ``entry_short`` fires on the
    downward gap only (row 20).  ``exit_long`` is the state "close below the mid
    band", which the reversal of row 11 opens and the downward gap of row 20
    keeps true; ``exit_short`` is its mirror image, opened by the upward gap of
    row 10 and by the recovery that follows row 20.  Every flat candle of the
    frame stays silent.
    """
    strategy = KeltnerStrategy({**HAND_PARAMS, "allow_short": True})
    prepared, signals = strategy.run(_hand_frame())

    assert _rows(signals["entry_long"]) == [10]
    assert _rows(signals["exit_long"]) == [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]
    assert _rows(signals["entry_short"]) == [20]
    assert _rows(signals["exit_short"]) == [10, 21, 22, 23, 24, 25, 26, 27, 28, 29]

    close = prepared["close"]
    assert float(prepared["close"].iloc[10]) == 110.0
    assert float(prepared["close"].iloc[20]) == 85.0
    assert float(prepared["keltner_upper"].iloc[10]) < 110.0
    assert float(prepared["keltner_lower"].iloc[20]) > 85.0
    # the trend filter really is on the right side of both gaps
    assert float(prepared["keltner_trend"].iloc[10]) < 110.0
    assert float(prepared["keltner_trend"].iloc[20]) > 85.0

    # the rules, re-derived from the prepared columns
    long_entry = (close > prepared["keltner_upper"]) & (close > prepared["keltner_trend"])
    short_entry = (close < prepared["keltner_lower"]) & (close < prepared["keltner_trend"])
    pd.testing.assert_series_equal(signals["entry_long"], long_entry, check_names=False)
    pd.testing.assert_series_equal(signals["entry_short"], short_entry, check_names=False)
    pd.testing.assert_series_equal(
        signals["exit_long"], close < prepared["keltner_mid"], check_names=False
    )
    pd.testing.assert_series_equal(
        signals["exit_short"], close > prepared["keltner_mid"], check_names=False
    )


def test_the_trend_filter_is_what_suppresses_the_breakout() -> None:
    # the very same band breakout, but with the trend EMA above the close of the
    # breakout candle: the entry must not fire, which is the whole point of the
    # filter.  Only that one row of the trend column is changed.
    strategy = KeltnerStrategy(HAND_PARAMS)
    prepared = strategy.prepare(_hand_frame())
    clean = strategy.signals(prepared)
    assert bool(clean["entry_long"].iloc[10]) is True
    assert float(prepared["close"].iloc[10]) > float(prepared["keltner_upper"].iloc[10])

    filtered = prepared.copy(deep=True)
    filtered.iloc[10, filtered.columns.get_loc("keltner_trend")] = 111.0

    signals = strategy.signals(filtered)

    assert not signals["entry_long"].any()
    # the band breakout itself is still there: only the filter changed
    assert float(prepared["keltner_upper"].iloc[10]) < float(prepared["close"].iloc[10]) < 111.0
    assert _rows(signals["exit_long"]) == [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]


def test_a_nan_band_never_fires() -> None:
    strategy = KeltnerStrategy({**HAND_PARAMS, "allow_short": True})
    prepared = strategy.prepare(_hand_frame())

    # the warm-up rows carry at least one undefined indicator
    undefined = prepared[list(INDICATOR_COLUMNS)].isna().any(axis=1)
    assert undefined.to_numpy().nonzero()[0].tolist() == [0, 1, 2, 3, 4, 5, 6]

    blank = prepared.copy(deep=True)
    blank[list(INDICATOR_COLUMNS)] = np.nan
    signals = strategy.signals(blank)

    assert not signals[list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()
    assert signals["stop_loss"].isna().all()

    # a NaN trend filter suppresses the entries without inventing an exit
    trendless = prepared.copy(deep=True)
    trendless["keltner_trend"] = np.nan
    trendless_signals = strategy.signals(trendless)
    assert not trendless_signals["entry_long"].any()
    assert not trendless_signals["entry_short"].any()
    assert _rows(trendless_signals["exit_long"]) == [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]


def test_without_allow_short_the_two_short_columns_stay_false() -> None:
    strategy = KeltnerStrategy(HAND_PARAMS)
    prepared, signals = strategy.run(_hand_frame())

    for column in ("entry_short", "exit_short"):
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].any()
        assert signals[column].index.equals(prepared.index)
    # the long side is exactly what the short-enabled run produces
    assert _rows(signals["entry_long"]) == [10]
    assert _rows(signals["exit_long"]) == [11, 12, 13, 14, 15, 16, 17, 18, 19, 20]


def test_allow_short_is_the_exact_mirror_of_the_long_rule() -> None:
    strategy = KeltnerStrategy({**HAND_PARAMS, "allow_short": True})
    prepared = strategy.prepare(_hand_frame())

    mirrored = strategy.signals(prepared)
    close = prepared["close"]
    upper = prepared["keltner_upper"]
    lower = prepared["keltner_lower"]
    middle = prepared["keltner_mid"]
    trend = prepared["keltner_trend"]

    # flipping every comparison of the long rule yields the short rule exactly
    pd.testing.assert_series_equal(
        mirrored["entry_long"], (close > upper) & (close > trend), check_names=False
    )
    pd.testing.assert_series_equal(
        mirrored["entry_short"], (close < lower) & (close < trend), check_names=False
    )
    pd.testing.assert_series_equal(mirrored["exit_long"], close < middle, check_names=False)
    pd.testing.assert_series_equal(mirrored["exit_short"], close > middle, check_names=False)
    # the stop column is shared by both directions
    assert mirrored["stop_loss"].iloc[10] == pytest.approx(
        prepared["close"].iloc[10] - 2.0 * prepared["atr"].iloc[10]
    )


def test_stop_loss_is_all_nan_when_the_multiplier_is_zero() -> None:
    strategy = KeltnerStrategy({**HAND_PARAMS, "atr_stop_multiplier": 0.0})
    prepared, signals = strategy.run(_hand_frame())

    assert signals["stop_loss"].isna().all()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    # 0.0 means "no stop", not "a stop at the close"
    assert not prepared["atr"].isna().all()


@pytest.mark.parametrize("multiplier", [2.0, 3.0])
def test_stop_loss_is_close_minus_the_atr_distance(multiplier: float) -> None:
    strategy = KeltnerStrategy({**HAND_PARAMS, "atr_stop_multiplier": multiplier})
    prepared, signals = strategy.run(_hand_frame())

    expected = prepared["close"] - multiplier * atr(
        prepared["high"], prepared["low"], prepared["close"], 5
    )

    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)
    # NaN wherever the ATR is not defined yet (the first atr_period candles)
    pd.testing.assert_series_equal(
        signals["stop_loss"].isna(), prepared["atr"].isna(), check_names=False
    )
    assert int(signals["stop_loss"].isna().sum()) == 5


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        KeltnerStrategy().signals(_ramp_frame(40))
    with pytest.raises(StrategyError, match="missing column"):
        KeltnerStrategy().signals(_ramp_frame(40).assign(keltner_mid=1.0))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        KeltnerStrategy().signals([1, 2, 3])  # type: ignore[arg-type]


def test_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _ramp_frame(200)
    snapshot = data.copy(deep=True)
    strategy = KeltnerStrategy()

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
    frame = trending_frame.copy(deep=True)
    frame.index = _index(len(frame), freq="h")
    strategy = KeltnerStrategy({"ema_period": 20, "trend_ema_period": 50, "atr_period": 14})

    first = run_backtest(strategy, frame)
    second = run_backtest(strategy, frame)

    assert first.strategy_name == "keltner"
    assert len(first.equity_curve) == len(frame)
    assert all(trade.exit_time > trade.entry_time for trade in first.trades)
    assert {trade.exit_reason for trade in first.trades} <= set(ExitReason)
    # the run is fully deterministic and never mutates the input frame
    assert first.trades == second.trades
    assert first.final_balance == second.final_balance
    assert len(strategy.prepare(frame)) == len(frame)
