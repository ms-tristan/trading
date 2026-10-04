"""Causality, forward-fill and fail-closed tests of the shared BTC regime gate.

The gate lives in ``user_data/strategies/market_regime.py`` and is exercised here
against a host strategy double rather than against a shipped strategy, so the
tests pin the mixin itself: which pair and timeframe it reads, that the daily
regime is shifted by one full day before it is forward-filled onto the signal
frame, and that it blocks every entry whenever the daily frame is unusable.

Two of these properties are safety-critical on a live system. A daily candle is
only complete at its close while freqtrade stamps it with the candle's open, so
an unshifted read is lookahead bias; and a missing daily frame -- exactly what a
fresh live start sees until 201 daily BTC candles exist -- must close the gate
instead of opening it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest
from pandas import DataFrame

STRATEGIES_DIR = Path(__file__).resolve().parents[2] / "user_data" / "strategies"
MARKET_REGIME_MODULE = "market_regime"

#: First day of the synthetic daily series the signal frames are aligned against.
DAILY_START = pd.Timestamp("2023-01-01", tz="UTC")
#: Length of that daily series; must stay above the gate window plus one candle.
DAILY_CANDLES = 400
#: Day the daily close steps from 100 to 1000 -- the day the raw regime turns on.
JUMP_DAY = 300
#: Day the daily close drops from 1000 to 50 -- the day the raw regime turns off.
DROP_DAY = 303
#: First hour of the signal frame, far enough into the daily series to be decisive.
SIGNAL_START = DAILY_START + pd.Timedelta(days=JUMP_DAY - 2)
#: One signal candle per hour over eight days.
SIGNAL_PERIODS = 8 * 24
#: The pair the gate must read, and the timeframe it must read it on.
BTC_PAIR = "BTC/USDT"
BTC_REGIME_TIMEFRAME = "1d"


def import_market_regime() -> ModuleType:
    """Import ``user_data/strategies/market_regime.py`` the way freqtrade's resolver does.

    freqtrade injects the strategy directory into ``sys.path`` while it executes a
    strategy module, which is how ``from market_regime import BtcRegimeGateMixin``
    resolves inside the six gated strategies. The import is registered under its
    own module name, so a strategy loaded later in the same session reuses this
    very class object.
    """
    module = sys.modules.get(MARKET_REGIME_MODULE)
    if module is not None:
        return module
    directory = str(STRATEGIES_DIR)
    sys.path.insert(0, directory)
    try:
        return importlib.import_module(MARKET_REGIME_MODULE)
    finally:
        sys.path.remove(directory)


MARKET_REGIME = import_market_regime()
BtcRegimeGateMixin = MARKET_REGIME.BtcRegimeGateMixin


def day(offset: int) -> pd.Timestamp:
    """Return the timestamp of day ``offset`` of the synthetic daily series."""
    return DAILY_START + pd.Timedelta(days=offset)


def build_daily_candles(step_day: int = JUMP_DAY, drop_day: int = DROP_DAY) -> DataFrame:
    """Return a flat, stepped BTC/USDT daily frame: 100, then 1000, then 50.

    The two steps are what make the one-day shift observable: the raw rule
    ``close > rolling(200).mean()`` flips on the step day itself, so an unshifted
    gate would allow the candles of that very day.
    """
    closes = np.full(DAILY_CANDLES, 100.0)
    closes[step_day:drop_day] = 1000.0
    closes[drop_day:] = 50.0
    return DataFrame(
        {
            "date": pd.date_range(DAILY_START, periods=DAILY_CANDLES, freq="1D", tz="UTC"),
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "volume": 1.0,
        }
    )


def build_signal_candles(periods: int = SIGNAL_PERIODS) -> DataFrame:
    """Return an hourly signal frame covering the decisive days of the daily series."""
    dates = pd.date_range(SIGNAL_START, periods=periods, freq="1h", tz="UTC")
    return DataFrame(
        {
            "date": dates,
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1.0,
        }
    )


def daily_frame_without(column: str) -> DataFrame:
    """Return a daily frame that is missing ``column``."""
    return build_daily_candles().drop(columns=[column])


def risk_on_at(risk_on: pd.Series, signals: DataFrame, moment: pd.Timestamp) -> bool:
    """Return the regime the gate reports for the candle stamped ``moment``."""
    return bool(risk_on[signals["date"] == moment].iloc[0])


class _AlwaysEnter:
    """Host strategy double: enters on every candle and exits on every candle."""

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["enter_long"] = 1
        dataframe["enter_tag"] = "always_enter"
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["exit_long"] = 1
        dataframe["exit_tag"] = "always_exit"
        return dataframe


class _GateProbe(BtcRegimeGateMixin, _AlwaysEnter):
    """The mixin under test, hosted by :class:`_AlwaysEnter`, with a settable provider."""

    def __init__(self, provider: object = None) -> None:
        self.dp = provider


class _ProbeWithoutProvider(BtcRegimeGateMixin, _AlwaysEnter):
    """A probe whose ``dp`` attribute was never attached at all."""


class _DailyProvider:
    """Minimal ``DataProvider`` double serving one cached frame."""

    def __init__(self, frame: object) -> None:
        self.frame = frame
        self.requested: list[tuple[str, str | None]] = []

    def get_pair_dataframe(
        self, pair: str, timeframe: str | None = None, candle_type: str = ""
    ) -> object:
        self.requested.append((pair, timeframe))
        return self.frame


class _RaisingProvider:
    """``DataProvider`` double whose klines cache is unavailable."""

    def __init__(self) -> None:
        self.requested: list[tuple[str, str | None]] = []

    def get_pair_dataframe(
        self, pair: str, timeframe: str | None = None, candle_type: str = ""
    ) -> object:
        self.requested.append((pair, timeframe))
        raise RuntimeError("no cached klines for the requested pair")


# ---------------------------------------------------------------------------
# The published interface
# ---------------------------------------------------------------------------
def test_module_publishes_the_gate_constants() -> None:
    assert MARKET_REGIME.BTC_PAIR == BTC_PAIR
    assert MARKET_REGIME.BTC_REGIME_TIMEFRAME == BTC_REGIME_TIMEFRAME
    assert MARKET_REGIME.BTC_REGIME_WINDOW == 200

    assert BtcRegimeGateMixin.BTC_PAIR == BTC_PAIR
    assert BtcRegimeGateMixin.BTC_REGIME_TIMEFRAME == BTC_REGIME_TIMEFRAME
    assert BtcRegimeGateMixin.BTC_REGIME_WINDOW == 200


def test_informative_pairs_declares_the_daily_btc_pair() -> None:
    """Without the declaration the daily frame is always empty in DRY_RUN/LIVE."""
    assert _GateProbe().informative_pairs() == [(BTC_PAIR, BTC_REGIME_TIMEFRAME)]


def test_the_gate_reads_the_declared_pair_and_timeframe() -> None:
    provider = _DailyProvider(build_daily_candles())

    _GateProbe(provider).btc_risk_on(build_signal_candles())

    assert provider.requested == [(BTC_PAIR, BTC_REGIME_TIMEFRAME)]


def test_btc_risk_on_returns_a_boolean_series_aligned_on_the_signal_frame() -> None:
    signals = build_signal_candles()
    risk_on = _GateProbe(_DailyProvider(build_daily_candles())).btc_risk_on(signals)

    assert isinstance(risk_on, pd.Series)
    assert risk_on.dtype == bool
    assert risk_on.index.equals(signals.index)
    assert len(risk_on) == len(signals)


# ---------------------------------------------------------------------------
# Causality: the daily regime is shifted by one full day
# ---------------------------------------------------------------------------
def test_daily_regime_is_shifted_by_one_full_day() -> None:
    """The candles of the day the regime turns on stay blocked until the next day."""
    daily = build_daily_candles()
    signals = build_signal_candles()
    risk_on = _GateProbe(_DailyProvider(daily)).btc_risk_on(signals)

    # The unshifted rule already turns on during the step day: without the one-day
    # shift, the candles of that day would pass the gate (the lookahead removed here).
    unshifted = daily["close"] > daily["close"].rolling(200).mean()
    assert bool(unshifted.loc[JUMP_DAY]) is True

    on_the_jump_day = signals["date"].dt.normalize() == day(JUMP_DAY)
    on_the_next_day = signals["date"].dt.normalize() == day(JUMP_DAY + 1)
    assert on_the_jump_day.any() and on_the_next_day.any()

    assert not risk_on[on_the_jump_day].any()
    assert risk_on[on_the_next_day].all()


def test_daily_regime_is_forward_filled_onto_every_candle_of_the_day() -> None:
    """A mid-day candle carries the regime of the previous day, not of its own day."""
    daily = build_daily_candles()
    signals = build_signal_candles()
    risk_on = _GateProbe(_DailyProvider(daily)).btc_risk_on(signals)

    # The step back down happens on DROP_DAY, so the unshifted rule turns off there
    # while the shifted rule still reports the previous day, which was risk-on.
    unshifted = daily["close"] > daily["close"].rolling(200).mean()
    assert bool(unshifted.loc[DROP_DAY]) is False

    mid_day = day(DROP_DAY) + pd.Timedelta(hours=12)
    assert risk_on_at(risk_on, signals, mid_day) is True

    # One daily row covers the whole day: no candle of DROP_DAY may transition early.
    on_the_drop_day = signals["date"].dt.normalize() == day(DROP_DAY)
    assert on_the_drop_day.any()
    assert risk_on[on_the_drop_day].all()

    # And the day after carries the regime the drop day itself produced.
    on_the_next_day = signals["date"].dt.normalize() == day(DROP_DAY + 1)
    assert on_the_next_day.any()
    assert not risk_on[on_the_next_day].any()


# ---------------------------------------------------------------------------
# Filtering: the host signals survive, the exit columns are untouched
# ---------------------------------------------------------------------------
def test_allowed_candles_keep_the_host_entry_signals() -> None:
    """``super()`` is called: a risk-on candle is exactly the host's own signal."""
    signals = build_signal_candles()
    probe = _GateProbe(_DailyProvider(build_daily_candles()))

    host = _AlwaysEnter().populate_entry_trend(signals.copy(), {"pair": BTC_PAIR})
    assert (host["enter_long"] == 1).all()

    risk_on = probe.btc_risk_on(signals)
    # Both regimes occur on the frame, otherwise the assertions below prove nothing.
    assert risk_on.any() and not risk_on.all()

    gated = probe.populate_entry_trend(signals.copy(), {"pair": BTC_PAIR})

    assert (gated.loc[risk_on, "enter_long"] == 1).all()
    assert (gated.loc[risk_on, "enter_tag"] == "always_enter").all()
    assert (gated.loc[~risk_on, "enter_long"] == 0).all()
    assert (gated.loc[~risk_on, "enter_tag"] == "").all()


def test_exit_columns_are_never_filtered() -> None:
    """A risk-off candle must still be able to close what it opened."""
    signals = build_signal_candles()
    probe = _GateProbe(_DailyProvider(build_daily_candles()))
    risk_on = probe.btc_risk_on(signals)
    assert not risk_on.all()

    dataframe = probe.populate_entry_trend(signals.copy(), {"pair": BTC_PAIR})
    dataframe = probe.populate_exit_trend(dataframe, {"pair": BTC_PAIR})

    assert (dataframe.loc[~risk_on, "exit_long"] == 1).all()
    assert (dataframe.loc[~risk_on, "exit_tag"] == "always_exit").all()


# ---------------------------------------------------------------------------
# Fail closed: an unusable daily frame blocks every entry
# ---------------------------------------------------------------------------
def test_the_gate_fails_closed_without_a_data_provider() -> None:
    signals = build_signal_candles()
    # A provider attribute that is explicitly None, and one that was never attached.
    probes = (_GateProbe(None), _ProbeWithoutProvider())

    for probe in probes:
        assert not probe.btc_risk_on(signals).any()
        dataframe = probe.populate_entry_trend(signals.copy(), {"pair": BTC_PAIR})
        assert (dataframe["enter_long"] == 0).all()
        assert (dataframe["enter_tag"] == "").all()


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(_RaisingProvider(), id="provider-raises"),
        pytest.param(_DailyProvider(None), id="provider-returns-none"),
        pytest.param(_DailyProvider("not a dataframe"), id="provider-returns-a-string"),
        pytest.param(_DailyProvider(DataFrame()), id="empty-frame"),
        pytest.param(_DailyProvider(daily_frame_without("close")), id="no-close-column"),
        pytest.param(_DailyProvider(daily_frame_without("date")), id="no-date-column"),
        pytest.param(_DailyProvider(build_daily_candles()[:200]), id="200-daily-candles"),
    ],
)
def test_the_gate_fails_closed_on_an_unusable_daily_frame(provider: object) -> None:
    """Every unusable daily frame -- including the 200 candles of a fresh live start."""
    signals = build_signal_candles(periods=48)
    probe = _GateProbe(provider)

    assert not probe.btc_risk_on(signals).any()

    dataframe = probe.populate_entry_trend(signals.copy(), {"pair": BTC_PAIR})
    assert (dataframe["enter_long"] == 0).all()
    assert (dataframe["enter_tag"] == "").all()
