"""Behavioural tests of the ``donchian`` channel-breakout strategy.

Everything here is offline and deterministic: the frames are hand-built from an
explicit arithmetic series, the expected signals are written out row by row and
the indicator columns are checked against the pandas expression the contract
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

The strategy symbols are imported from :mod:`trading_platform.strategy.donchian`
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
from trading_platform.strategy.donchian import (
    INDICATOR_COLUMNS,
    DonchianParams,
    DonchianStrategy,
    DonchianStrategyParams,
)
from trading_platform.strategy.engine import run_backtest
from trading_platform.strategy.indicators import atr

#: The module logger the warm-up warning is emitted on (frozen contract).
DONCHIAN_LOGGER = "trading_platform.strategy.donchian"

#: The frozen event name of the "this frame cannot warm up" warning.
WARMUP_EVENT = "strategy.warmup_incomplete"

START = "2024-01-01T00:00:00Z"

#: Candles per 24-hour day of the grids the platform supports, computed from the
#: core authority (``trading_platform.realtime.warmup`` is NOT importable from
#: the strategy layer, and the strategy must not need it anyway).
CANDLES_PER_DAY: dict[str, float] = {
    timeframe: 1440.0 / timeframe_minutes(timeframe) for timeframe in ("1m", "1h", "4h", "1d")
}

#: The declared warm-up of the default parameters (entry 20 / exit 10) on EVERY grid.
EXPECTED_WARMUP = 22

#: The small, fully defined parameters used by the hand-built frames.
HAND_PARAMS: dict[str, Any] = {
    "entry_period": 3,
    "exit_period": 2,
    "atr_period": 2,
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
    """30 hourly candles whose Donchian behaviour is obvious by construction.

    Five blocks: an up-ramp (rows 0-4, +1 a candle), a plateau at 14 (5-9), a
    down-ramp (10-14, -1 a candle), a flat bottom at 9 (15-19) and a second
    up-ramp followed by a plateau (20-29).
    """
    closes = (
        [10.0, 11.0, 12.0, 13.0, 14.0]
        + [14.0] * 5
        + [13.0, 12.0, 11.0, 10.0, 9.0]
        + [9.0] * 5
        + [10.0, 11.0, 12.0, 13.0, 14.0]
        + [14.0] * 5
    )
    return _frame_from_closes(closes)


def _rows(column: pd.Series) -> list[int]:
    """Return the integer positions where ``column`` is ``True``."""
    return [int(position) for position in np.flatnonzero(column.to_numpy(dtype=bool))]


def _warning_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return the structured warm-up warnings captured for this module."""
    return [record for record in caplog.records if record.name == DONCHIAN_LOGGER]


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------


def test_default_parameters_are_the_documented_ones() -> None:
    strategy = DonchianStrategy()
    params = strategy.params

    assert strategy.name == "donchian"
    assert isinstance(params, DonchianStrategyParams)
    # The parameter model has exactly one class behind its two public spellings.
    assert DonchianParams is DonchianStrategyParams
    assert isinstance(params, DonchianParams)
    assert params.model_dump() == {
        "entry_period": 20,
        "exit_period": 10,
        "atr_period": 14,
        "atr_stop_multiplier": 2.0,
        "allow_short": False,
    }
    assert DonchianStrategy.default_params() == params.model_dump()


def test_param_space_matches_the_documented_grid() -> None:
    space = DonchianStrategy().param_space()

    assert space == {
        "entry_period": [20, 40, 55],
        "exit_period": [5, 10],
        "atr_stop_multiplier": [0.0, 2.0, 4.0],
    }
    assert math.prod(len(values) for values in space.values()) == 18


def test_every_cartesian_combination_of_the_param_space_validates() -> None:
    # The robustness sweep builds the full product of PARAM_SPACE: every single
    # combination must be a valid parameter set, otherwise the sweep crashes.
    space = DonchianStrategy.PARAM_SPACE
    combinations = list(itertools.product(*space.values()))

    assert len(combinations) == 18
    for combination in combinations:
        params = dict(zip(space, combination, strict=True))
        strategy = DonchianStrategy(params)
        model = DonchianStrategyParams(**params)
        assert isinstance(strategy.params, DonchianStrategyParams)
        # every combination keeps the exit channel shorter than the entry channel
        assert model.exit_period < model.entry_period


@pytest.mark.parametrize(
    ("params", "fields"),
    [
        ({"entry_period": 10, "exit_period": 10}, ("entry_period", "exit_period")),
        ({"entry_period": 5, "exit_period": 10}, ("entry_period", "exit_period")),
        ({"entry_period": 20, "exit_period": 20}, ("entry_period", "exit_period")),
    ],
)
def test_incoherent_parameters_raise(params: dict[str, object], fields: tuple[str, str]) -> None:
    with pytest.raises(StrategyError) as error:
        DonchianStrategy(params)

    message = str(error.value)
    for field in fields:
        assert field in message, f"the error does not name {field!r}: {message}"


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"entry_period": 1}, "entry_period"),
        ({"entry_period": 0}, "entry_period"),
        ({"exit_period": 0}, "exit_period"),
        ({"exit_period": -3}, "exit_period"),
        ({"atr_period": 1}, "atr_period"),
        ({"atr_period": 0}, "atr_period"),
        ({"atr_stop_multiplier": -0.1}, "atr_stop_multiplier"),
    ],
)
def test_out_of_range_parameters_raise(params: dict[str, object], field: str) -> None:
    with pytest.raises(StrategyError) as error:
        DonchianStrategy(params)

    assert field in str(error.value)


def test_unknown_parameter_raises() -> None:
    with pytest.raises(StrategyError, match="unknown_field"):
        DonchianStrategy({"unknown_field": 1})


def test_allow_short_defaults_to_off_and_can_be_enabled() -> None:
    assert DonchianStrategy().params.model_dump()["allow_short"] is False
    assert DonchianStrategy({"allow_short": True}).params.model_dump()["allow_short"] is True


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def test_prepare_adds_exactly_the_indicator_columns_and_leaves_the_input_untouched() -> None:
    data = _ramp_frame(120)
    before = data.copy(deep=True)

    prepared = DonchianStrategy().prepare(data)

    assert list(prepared.columns) == [*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS]
    assert list(INDICATOR_COLUMNS) == ["donchian_upper", "donchian_lower", "atr"]
    pd.testing.assert_frame_equal(prepared[list(before.columns)], before)
    pd.testing.assert_frame_equal(data, before)
    assert prepared.index.equals(data.index)
    assert prepared.index.name == "timestamp"
    for column in INDICATOR_COLUMNS:
        assert prepared[column].dtype == np.dtype("float64")


def test_prepare_computes_the_two_channels_and_the_atr() -> None:
    data = _ramp_frame(120)

    prepared = DonchianStrategy().prepare(data)

    expected_upper = data["high"].rolling(window=20, min_periods=20).max()
    expected_lower = data["low"].rolling(window=10, min_periods=10).min()
    expected_atr = atr(data["high"], data["low"], data["close"], 14)
    pd.testing.assert_series_equal(prepared["donchian_upper"], expected_upper, check_names=False)
    pd.testing.assert_series_equal(prepared["donchian_lower"], expected_lower, check_names=False)
    pd.testing.assert_series_equal(prepared["atr"], expected_atr, check_names=False)
    # full windows only: the first 19 upper values and the first 9 lower ones are NaN
    assert int(prepared["donchian_upper"].isna().sum()) == 19
    assert int(prepared["donchian_lower"].isna().sum()) == 9


def test_prepare_rejects_a_frame_that_violates_the_ohlcv_contract() -> None:
    with pytest.raises(StrategyError, match="missing required column"):
        DonchianStrategy().prepare(_ramp_frame(30).drop(columns=["volume"]))
    with pytest.raises(StrategyError, match="index must be a DatetimeIndex"):
        DonchianStrategy().prepare(_ramp_frame(30).reset_index(drop=True))
    with pytest.raises(StrategyError, match="empty frame"):
        DonchianStrategy().prepare(_frame_from_closes([]))
    with pytest.raises(StrategyError, match="not convertible to float64"):
        DonchianStrategy().prepare(_ramp_frame(30).assign(close="not-a-number"))
    with pytest.raises(StrategyError, match="must be a pandas DataFrame"):
        DonchianStrategy().prepare([1.0, 2.0, 3.0])  # type: ignore[arg-type]


def test_prepare_warns_once_when_the_frame_cannot_warm_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = DonchianStrategy()

    with caplog.at_level(logging.WARNING, logger=DONCHIAN_LOGGER):
        prepared = strategy.prepare(_ramp_frame(EXPECTED_WARMUP - 1))

    records = _warning_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.__dict__["event"] == WARMUP_EVENT
    assert record.__dict__["strategy"] == "donchian"
    assert record.__dict__["rows"] == EXPECTED_WARMUP - 1
    assert record.__dict__["required_candles"] == EXPECTED_WARMUP
    # a short frame is legitimate: the strategy never raises for it
    assert len(prepared) == EXPECTED_WARMUP - 1


def test_prepare_is_silent_when_the_frame_is_long_enough(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=DONCHIAN_LOGGER):
        DonchianStrategy().prepare(_ramp_frame(EXPECTED_WARMUP))

    assert _warning_records(caplog) == []


# ---------------------------------------------------------------------------
# required_candles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("timeframe", sorted(CANDLES_PER_DAY, key=timeframe_minutes))
def test_required_candles_declares_the_documented_requirement_on_every_grid(
    timeframe: str,
) -> None:
    required = DonchianStrategy().required_candles(CANDLES_PER_DAY[timeframe])

    assert isinstance(required, int)
    assert not isinstance(required, bool)
    # the periods are counted in candles, so the declared warm-up is grid-independent
    assert required == EXPECTED_WARMUP


def test_required_candles_follows_the_parameters_not_a_constant() -> None:
    assert DonchianStrategy({"entry_period": 55, "exit_period": 20}).required_candles(1440.0) == 57
    assert DonchianStrategy({"entry_period": 4, "exit_period": 3}).required_candles(1440.0) == 6
    assert DonchianStrategy({"entry_period": 20, "exit_period": 20 - 1}).required_candles() == 22


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
    strategy = DonchianStrategy()

    assert strategy.required_candles(candles_per_day) == EXPECTED_WARMUP  # type: ignore[arg-type]


def test_required_candles_is_pure_and_repeatable() -> None:
    strategy = DonchianStrategy({"entry_period": 40, "exit_period": 5})
    before = strategy.params.model_dump()

    assert strategy.required_candles(24.0) == strategy.required_candles(24.0) == 42
    assert strategy.params.model_dump() == before


# ---------------------------------------------------------------------------
# signals
# ---------------------------------------------------------------------------


def test_signal_frame_has_exactly_the_five_contract_columns() -> None:
    strategy = DonchianStrategy(HAND_PARAMS)
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

    ``entry_long`` fires on every candle of the two up-ramps that closes above
    the previous 3-candle high (rows 3-4 and 20-24); ``exit_long`` on every
    candle of the down-ramp that closes below the previous 2-candle low
    (rows 10-14).  Every plateau and every flat stretch stays silent.
    """
    strategy = DonchianStrategy(HAND_PARAMS)
    prepared, signals = strategy.run(_hand_frame())

    assert _rows(signals["entry_long"]) == [3, 4, 20, 21, 22, 23, 24]
    assert _rows(signals["exit_long"]) == [10, 11, 12, 13, 14]
    assert _rows(signals["entry_short"]) == []
    assert _rows(signals["exit_short"]) == []

    # the entries are a state, not a one-shot event: rows 21-24 keep firing
    assert bool(signals["entry_long"].iloc[24]) is True
    assert bool(signals["entry_long"].iloc[25]) is False
    # row 3 is the first row the shifted channel is defined on
    assert bool(signals["entry_long"].iloc[2]) is False
    assert np.isnan(prepared["donchian_upper"].iloc[1])
    assert np.isfinite(prepared["donchian_upper"].iloc[2])

    # the rule, re-derived from the raw frame: the PREVIOUS completed channel only
    close = prepared["close"]
    expected_entry = close > prepared["donchian_upper"].shift(1)
    expected_exit = close < prepared["donchian_lower"].shift(1)
    pd.testing.assert_series_equal(signals["entry_long"], expected_entry, check_names=False)
    pd.testing.assert_series_equal(signals["exit_long"], expected_exit, check_names=False)


def test_a_nan_channel_never_fires() -> None:
    strategy = DonchianStrategy(HAND_PARAMS)
    prepared = strategy.prepare(_hand_frame())

    # the channel the close is compared with is the SHIFTED one: the first rows
    # of the frame carry no shifted channel at all
    shifted_upper = prepared["donchian_upper"].shift(1)
    shifted_lower = prepared["donchian_lower"].shift(1)
    assert shifted_upper.isna().to_numpy().nonzero()[0].tolist() == [0, 1, 2]
    assert shifted_lower.isna().to_numpy().nonzero()[0].tolist() == [0, 1]

    blank = prepared.copy(deep=True)
    blank[list(INDICATOR_COLUMNS)] = np.nan
    signals = strategy.signals(blank)

    assert not signals[list(BOOL_SIGNAL_COLUMNS)].to_numpy().any()
    assert signals["stop_loss"].isna().all()


def test_a_nan_high_suppresses_the_breakout_it_would_otherwise_trigger() -> None:
    params = {**HAND_PARAMS, "allow_short": True}
    strategy = DonchianStrategy(params)
    frame = _hand_frame()
    clean = strategy.run(frame)[1]
    assert _rows(clean["entry_long"]) == [3, 4, 20, 21, 22, 23, 24]
    assert _rows(clean["exit_short"]) == [3, 4, 20, 21, 22, 23, 24]

    # poisoning high[1] makes the 3-candle window undefined on rows 1-3 and
    # therefore the SHIFTED channel undefined on rows 2-4: the two ramp
    # breakouts disappear instead of firing on a NaN
    poisoned = frame.copy(deep=True)
    poisoned.iloc[1, poisoned.columns.get_loc("high")] = np.nan
    poisoned_signals = strategy.run(poisoned)[1]

    assert _rows(poisoned_signals["entry_long"]) == [20, 21, 22, 23, 24]
    assert _rows(poisoned_signals["exit_short"]) == [20, 21, 22, 23, 24]
    # the exit channel of the low side is untouched by the poisoned high
    assert _rows(poisoned_signals["exit_long"]) == [10, 11, 12, 13, 14]


def test_without_allow_short_the_two_short_columns_stay_false() -> None:
    strategy = DonchianStrategy(HAND_PARAMS)
    prepared, signals = strategy.run(_hand_frame())

    for column in ("entry_short", "exit_short"):
        assert signals[column].dtype == np.dtype("bool")
        assert not signals[column].any()
        assert signals[column].index.equals(prepared.index)


def test_allow_short_is_the_exact_mirror_of_the_long_rule() -> None:
    strategy = DonchianStrategy({**HAND_PARAMS, "allow_short": True})
    prepared, signals = strategy.run(_hand_frame())

    # the mirror of the Turtle rule: the short enters on the exit channel and
    # leaves on the entry channel
    pd.testing.assert_series_equal(signals["entry_short"], signals["exit_long"], check_names=False)
    pd.testing.assert_series_equal(signals["exit_short"], signals["entry_long"], check_names=False)
    assert _rows(signals["entry_short"]) == [10, 11, 12, 13, 14]
    assert _rows(signals["exit_short"]) == [3, 4, 20, 21, 22, 23, 24]
    # the long side is unchanged by allow_short
    assert _rows(signals["entry_long"]) == [3, 4, 20, 21, 22, 23, 24]
    # the stop column is shared by both directions
    assert signals["stop_loss"].iloc[20] == pytest.approx(
        prepared["close"].iloc[20] - 2.0 * prepared["atr"].iloc[20]
    )


def test_stop_loss_is_all_nan_when_the_multiplier_is_zero() -> None:
    strategy = DonchianStrategy({**HAND_PARAMS, "atr_stop_multiplier": 0.0})
    prepared, signals = strategy.run(_hand_frame())

    assert signals["stop_loss"].isna().all()
    assert signals["stop_loss"].dtype == np.dtype("float64")
    # 0.0 means "no stop", not "a stop at the close"
    assert not prepared["atr"].isna().all()


@pytest.mark.parametrize("multiplier", [2.0, 3.0])
def test_stop_loss_is_close_minus_the_atr_distance(multiplier: float) -> None:
    strategy = DonchianStrategy({**HAND_PARAMS, "atr_stop_multiplier": multiplier})
    prepared, signals = strategy.run(_hand_frame())

    expected = prepared["close"] - multiplier * atr(
        prepared["high"], prepared["low"], prepared["close"], 2
    )

    pd.testing.assert_series_equal(signals["stop_loss"], expected, check_names=False)
    # NaN wherever the ATR is not defined yet (the first atr_period candles)
    pd.testing.assert_series_equal(
        signals["stop_loss"].isna(), prepared["atr"].isna(), check_names=False
    )
    assert int(signals["stop_loss"].isna().sum()) == 2


def test_signals_requires_a_prepared_frame() -> None:
    with pytest.raises(StrategyError, match="prepare"):
        DonchianStrategy().signals(_ramp_frame(40))
    with pytest.raises(StrategyError, match="missing column"):
        DonchianStrategy().signals(_ramp_frame(40).assign(donchian_upper=1.0))
    with pytest.raises(StrategyError, match="pandas DataFrame"):
        DonchianStrategy().signals([1, 2, 3])  # type: ignore[arg-type]


def test_pipeline_is_deterministic_and_leaves_both_inputs_untouched() -> None:
    data = _ramp_frame(120)
    snapshot = data.copy(deep=True)
    strategy = DonchianStrategy()

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
    strategy = DonchianStrategy({"entry_period": 20, "exit_period": 10, "atr_period": 14})

    first = run_backtest(strategy, frame)
    second = run_backtest(strategy, frame)

    assert first.strategy_name == "donchian"
    assert len(first.equity_curve) == len(frame)
    assert all(trade.exit_time > trade.entry_time for trade in first.trades)
    assert {trade.exit_reason for trade in first.trades} <= set(ExitReason)
    # the run is fully deterministic and never mutates the input frame
    assert first.trades == second.trades
    assert first.final_balance == second.final_balance
    assert len(strategy.prepare(frame)) == len(frame)
