"""Integration tests of the adapter against **the real Freqtrade interface**.

This is the proof of deliverable (b): the class produced by
:func:`~trading_backtest.strategy.make_freqtrade_strategy` is instantiated for
real, driven over **real market data**, and its signals are cross-checked
candle by candle against the ones the **house** engine renders on the very same
data.  Nothing is simulated here: the objects under test are
``freqtrade.strategy.IStrategy``, ``freqtrade.strategy.stoploss_from_absolute``,
``freqtrade.resolvers.strategy_resolver.StrategyResolver`` and
``freqtrade.strategy.strategy_validation.StrategyResultValidator``, validated
against the installed **Freqtrade 2026.8**.

Why the guards below are mandatory
----------------------------------
``docs/testing-policy.md`` §1.2/§5.2: the CI installs ``-e ".[dev]"`` **only**,
so Freqtrade is *absent* there and ``data/`` is not tracked by git
(``git ls-files data`` returns nothing).  The module therefore starts with an
``importorskip`` — the CI reports one *skipped* module, never a red build — and
carries a ``skipif`` on the fixture file, which lets a developer without the
network-filled cache still run the suite.  Locally (Freqtrade + cache present)
every test below **runs**.

The fixture, pinned
-------------------
``data/cache/binance/BTC_USDT/1h.parquet`` — **17543** candles, from
``2023-01-01T00:00:00+00:00`` to ``2025-01-01T00:00:00+00:00``, tz-aware UTC,
``DatetimeIndex`` named ``timestamp``, columns ``open/high/low/close/volume``.
On it, and on it only, the measured signal counts are:

* house ``entry_long`` -> Freqtrade ``enter_long``: **395** signals;
* house ``exit_long`` -> Freqtrade ``exit_long``: **400** signals;
* house ``entry_short`` -> Freqtrade ``enter_short``: **0** signal;
* house ``exit_short`` -> Freqtrade ``exit_short``: **0** signal.

Read as tuples, ``(enter_long, exit_long, enter_short, exit_short)`` is
**``(395, 400, 0, 0)``** for the adapter *and* ``(395, 400, 0, 0)`` for the
house strategy — the equality of the two tuples is the proof that the
``entry_*`` -> ``enter_*`` translation is faithful.  ``basic`` cannot short by
default (``allow_short`` is ``False``), hence the two zeros: they are asserted,
not skipped, because a non-zero value would mean the adapter invented trades.
The ``stop_loss`` column carries **14 NaN** candles (positions 0..13, the
indicator warm-up): a raw ``==`` comparison on that column is therefore
**invalid** — the equality below uses ``np.array_equal(..., equal_nan=True)``.
The first entry lands on position **74** (``2023-01-04 02:00 UTC``) and its
fill candle is position 75; the adapter replays its absolute stop
(``16647.686891914564``, captured on the signal candle) through
``custom_stoploss`` as the ratio ``0.012710998331483259`` of the rate
``16862.02`` (the fill candle's close) — i.e. ``1 - stop / rate``, the
non-negative convention of Freqtrade 2026.8 (positive means "below the price").

These numbers are **frozen on purpose**: if the parquet is replaced or the house
rules change, this module fails loudly instead of drifting silently.  The
scenarios, and only these files are touched, are the six of the work package:
empty-config instantiation, the three ``populate_*``, the count cross-check, the
real ``RangeIndex`` + ``date`` frame shape (hand-built *and* through
``ohlcv_to_dataframe``), ``custom_stoploss`` on a real signal, and the load-by-name
resolution of ``BasicStrategy``.

The two engines are **not** compared on PnL — only on the signal contract; the
execution-model gap is documented in ``docs/architecture.md`` §4.9.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_backtest.core.constants import OHLCV_INDEX_NAME, REQUIRED_OHLCV_COLUMNS, UTC
from trading_backtest.strategy import (
    FREQTRADE_INTERFACE_VERSION,
    SIGNAL_TO_FREQTRADE_COLUMNS,
    get_strategy,
    make_freqtrade_strategy,
    validate_freqtrade_adapter_class,
)

freqtrade = pytest.importorskip("freqtrade", reason="freqtrade is not part of the dev extra")

MARKET_DATA = (
    Path(__file__).resolve().parents[1] / "data" / "cache" / "binance" / "BTC_USDT" / "1h.parquet"
)

pytestmark = pytest.mark.skipif(
    not MARKET_DATA.is_file(),
    reason="the real BTC/USDT 1h cache fixture is not present",
)

# The Freqtrade objects are imported *after* the guard: that ordering is what
# keeps the CI (no Freqtrade) green, and it is the documented reason for the
# `noqa` markers below.
from freqtrade.data.converter.converter import ohlcv_to_dataframe  # noqa: E402
from freqtrade.resolvers.strategy_resolver import StrategyResolver  # noqa: E402
from freqtrade.strategy import IStrategy, stoploss_from_absolute  # noqa: E402
from freqtrade.strategy.strategy_validation import StrategyResultValidator  # noqa: E402

# ---------------------------------------------------------------------------
# frozen expectations (measured on the fixture, see the module docstring)
# ---------------------------------------------------------------------------

#: Trading pair and timeframe of the fixture.
PAIR = "BTC/USDT"
TIMEFRAME = "1h"

#: Exact size of ``data/cache/binance/BTC_USDT/1h.parquet``.
CANDLE_COUNT = 17543

#: First and last candle of the fixture (tz-aware UTC).
FIXTURE_START = pd.Timestamp("2023-01-01T00:00:00Z")
FIXTURE_END = pd.Timestamp("2025-01-01T00:00:00Z")

#: ``(enter_long, exit_long, enter_short, exit_short)`` candles carrying a signal.
EXPECTED_SIGNALS = (395, 400, 0, 0)

#: ``stop_loss`` candles that are ``NaN`` (the warm-up: positions ``0..13``).
NAN_STOP_CANDLES = 14

#: Position of the first ``enter_long`` candle (``2023-01-04 02:00 UTC``).
FIRST_ENTRY_POSITION = 74

#: House signal columns, in the order the counts are reported.
HOUSE_COLUMNS = ("entry_long", "exit_long", "entry_short", "exit_short")

#: The four columns Freqtrade really reads.
FREQTRADE_COLUMNS = ("enter_long", "exit_long", "enter_short", "exit_short")

#: House-to-Freqtrade column pairs, taken from the adapter's own mapping
#: (single source of truth of the ``entry_*`` -> ``enter_*`` rename).
COLUMN_PAIRS = tuple((house, SIGNAL_TO_FREQTRADE_COLUMNS[house]) for house in HOUSE_COLUMNS)

#: Name of the per-candle stop column, identical on both sides (audit only for
#: Freqtrade, which has no such column: the adapter replays it through
#: ``custom_stoploss``).
STOP_COLUMN = "stop_loss"

#: ``user_data/strategies``: the directory Freqtrade resolves by class name.
STRATEGIES_DIR = Path(__file__).resolve().parents[1] / "user_data" / "strategies"

#: A millisecond in nanoseconds — the unit ``ohlcv_to_dataframe`` expects.
NANOSECONDS_PER_MILLISECOND = 1_000_000

#: The per-candle metadata Freqtrade hands to every ``populate_*`` call.
METADATA: dict[str, Any] = {"pair": PAIR, "timeframe": TIMEFRAME}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Advice:
    """One adapter instance plus the frames its three ``populate_*`` returned.

    The instance is kept because it carries the per-candle stops captured during
    ``populate_entry_trend``, which ``custom_stoploss`` replays afterwards: the
    two calls must be made on the **same** object.
    """

    strategy: Any
    source: pd.DataFrame
    indicators: pd.DataFrame
    entries: pd.DataFrame
    exits: pd.DataFrame


@dataclass(frozen=True)
class FakeTrade:
    """The two attributes ``custom_stoploss`` reads off a Freqtrade ``Trade``."""

    open_date_utc: pd.Timestamp
    is_short: bool = False


def _advise(strategy: Any, source: pd.DataFrame) -> Advice:
    """Run the three ``populate_*`` of ``strategy`` in Freqtrade's own order.

    Each frame is passed as a **copy of the previous one**, exactly like
    ``IStrategy.advise_*`` does, so a missing column would surface here.
    """
    indicators = strategy.populate_indicators(source.copy(), METADATA)
    entries = strategy.populate_entry_trend(indicators.copy(), METADATA)
    exits = strategy.populate_exit_trend(entries.copy(), METADATA)
    return Advice(
        strategy=strategy,
        source=source,
        indicators=indicators,
        entries=entries,
        exits=exits,
    )


def _counts(frame: pd.DataFrame, columns: tuple[str, ...] | list[str]) -> tuple[int, ...]:
    """Return the number of ``True`` of every boolean column of ``frame``."""
    return tuple(int(frame[column].sum()) for column in columns)


def _converted_frame(market_frame: pd.DataFrame) -> pd.DataFrame:
    """Return the frame Freqtrade builds from raw exchange rows.

    ``ohlcv_to_dataframe(..., fill_missing=False, drop_incomplete=False)`` never
    reindexes and never invents a candle, so the result holds exactly the 17543
    fixture rows — but as a **``RangeIndex`` plus a ``date`` column**, the shape
    the adapter has to survive.
    """
    rows = [
        [
            int(stamp.value // NANOSECONDS_PER_MILLISECOND),
            float(open_),
            float(high),
            float(low),
            float(close),
            float(volume),
        ]
        for stamp, open_, high, low, close, volume in zip(
            market_frame.index,
            market_frame["open"],
            market_frame["high"],
            market_frame["low"],
            market_frame["close"],
            market_frame["volume"],
            strict=True,
        )
    ]
    return ohlcv_to_dataframe(rows, TIMEFRAME, PAIR, fill_missing=False, drop_incomplete=False)


def _assert_same_signals(house_signals: pd.DataFrame, adapter_frame: pd.DataFrame) -> None:
    """Assert the four signal columns and the stop column are candle-identical."""
    for house_column, freqtrade_column in COLUMN_PAIRS:
        assert np.array_equal(
            house_signals[house_column].to_numpy(dtype=bool),
            adapter_frame[freqtrade_column].to_numpy(dtype=bool),
        ), f"{house_column} -> {freqtrade_column} differs at some candle"
    assert np.array_equal(
        house_signals[STOP_COLUMN].to_numpy(dtype="float64"),
        adapter_frame[STOP_COLUMN].to_numpy(dtype="float64"),
        equal_nan=True,
    ), "the per-candle stop differs"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def market_frame() -> pd.DataFrame:
    """The real 17543-candle BTC/USDT 1h fixture, as the house layer loads it."""
    return pd.read_parquet(MARKET_DATA)


@pytest.fixture(scope="module")
def freqtrade_frame(market_frame: pd.DataFrame) -> pd.DataFrame:
    """The same data in the shape Freqtrade's backtesting loop really passes.

    The house frame carries a ``DatetimeIndex``; Freqtrade hands the strategy a
    positional ``RangeIndex`` plus a ``date`` column.  This fixture is the
    hand-built version of that shape (scenario 4a); the ``ohlcv_to_dataframe``
    version is built inside scenario 4b.
    """
    return market_frame.reset_index().rename(columns={OHLCV_INDEX_NAME: "date"})


@pytest.fixture(scope="module")
def house_shaped_advice(market_frame: pd.DataFrame) -> Advice:
    """Adapter instance advised on the house-shaped (indexed) frame."""
    return _advise(make_freqtrade_strategy("basic")({}), market_frame)


@pytest.fixture(scope="module")
def freqtrade_shaped_advice(freqtrade_frame: pd.DataFrame) -> Advice:
    """Adapter instance advised on the real Freqtrade-shaped (``date``) frame."""
    return _advise(make_freqtrade_strategy("basic")({}), freqtrade_frame)


# ---------------------------------------------------------------------------
# provenance: the fixture must not drift silently
# ---------------------------------------------------------------------------


def test_fixture_provenance(market_frame: pd.DataFrame) -> None:
    """Pin path, size, boundaries and timezone of the parquet fixture."""
    assert market_frame.shape == (CANDLE_COUNT, len(REQUIRED_OHLCV_COLUMNS))
    assert list(market_frame.columns) == list(REQUIRED_OHLCV_COLUMNS)
    index = market_frame.index
    assert isinstance(index, pd.DatetimeIndex)
    assert index.name == OHLCV_INDEX_NAME
    assert index.tz is not None
    assert str(index.tz) == UTC
    assert index[0] == FIXTURE_START
    assert index[-1] == FIXTURE_END
    assert index.is_monotonic_increasing
    assert not bool(market_frame.isna().to_numpy().any())


# ---------------------------------------------------------------------------
# scenario 1 -- the generated class instantiates and is a valid IStrategy
# ---------------------------------------------------------------------------


def test_scenario_1_generated_class_is_a_valid_istrategy() -> None:
    """``make_freqtrade_strategy('basic')({})`` works with the empty config.

    The empty mapping is what Freqtrade passes when only ``--strategy`` is given,
    so the adapter must not depend on any config key.
    """
    obj = make_freqtrade_strategy("basic")({})

    assert validate_freqtrade_adapter_class(type(obj)) is None
    assert isinstance(obj, IStrategy)
    # Guards against a shadowed / duplicated Freqtrade installation: the class
    # the adapter subclassed must be the very one this module imported.
    assert IStrategy is freqtrade.strategy.IStrategy
    assert type(obj).INTERFACE_VERSION == FREQTRADE_INTERFACE_VERSION == 3

    # The interface attributes the live/dry-run bot reads.
    assert obj.timeframe == TIMEFRAME
    assert isinstance(obj.stoploss, float)
    assert -1.0 < obj.stoploss < 0.0
    assert isinstance(obj.minimal_roi, dict)
    assert obj.can_short is False
    assert isinstance(obj.startup_candle_count, int)
    assert obj.startup_candle_count >= 0
    assert obj.use_custom_stoploss is True
    assert isinstance(obj.process_only_new_candles, bool)
    assert obj.house_strategy_name == "basic"


# ---------------------------------------------------------------------------
# scenario 2 -- the three populate_* produce the Freqtrade columns
# ---------------------------------------------------------------------------


def test_scenario_2_populate_methods_produce_the_freqtrade_columns(
    house_shaped_advice: Advice,
) -> None:
    """The three ``populate_*`` add the right columns and never the house spelling."""
    advice = house_shaped_advice

    # populate_indicators: the house indicators, on the incoming frame.
    assert len(advice.indicators) == CANDLE_COUNT
    assert advice.indicators.index.name == OHLCV_INDEX_NAME
    assert advice.indicators.index.equals(advice.source.index)
    assert list(advice.indicators.columns) == [
        *REQUIRED_OHLCV_COLUMNS,
        "ema_fast",
        "ema_slow",
        "rsi",
        "atr",
    ]
    for column in ("ema_fast", "ema_slow", "rsi", "atr"):
        assert advice.indicators[column].dtype == "float64"

    # populate_entry_trend: Freqtrade's enter_* spelling plus the audit stop.
    assert list(advice.entries.columns[len(advice.indicators.columns) :]) == [
        "enter_long",
        "enter_short",
        STOP_COLUMN,
    ]
    assert advice.entries["enter_long"].dtype == bool
    assert advice.entries["enter_short"].dtype == bool
    assert advice.entries[STOP_COLUMN].dtype == "float64"

    # populate_exit_trend: its own two columns only.
    assert list(advice.exits.columns[len(advice.entries.columns) :]) == [
        "exit_long",
        "exit_short",
    ]
    assert advice.exits["exit_long"].dtype == bool
    assert advice.exits["exit_short"].dtype == bool

    # The four columns Freqtrade reads are all there ...
    assert set(FREQTRADE_COLUMNS) <= set(advice.exits.columns)
    # ... and the house spelling never leaks: 'entry_long' means *nothing* to
    # Freqtrade, so seeing it here would be a silent no-trade bug.
    for frame in (advice.indicators, advice.entries, advice.exits):
        assert "entry_long" not in frame.columns
        assert "entry_short" not in frame.columns


# ---------------------------------------------------------------------------
# scenario 3 -- entry_* -> enter_*: same candles, same counts
# ---------------------------------------------------------------------------


def test_scenario_3_translation_produces_the_house_signals(
    market_frame: pd.DataFrame, house_shaped_advice: Advice
) -> None:
    """The adapter's signals are the house's signals, candle by candle.

    This is the deliverable-(b) proof: the two engines disagree on PnL, never on
    the signal contract.
    """
    _, house_signals = get_strategy("basic").run(market_frame)

    adapter_counts = _counts(house_shaped_advice.exits, [column for _, column in COLUMN_PAIRS])
    house_counts = _counts(house_signals, HOUSE_COLUMNS)

    assert adapter_counts == EXPECTED_SIGNALS
    assert house_counts == EXPECTED_SIGNALS
    assert adapter_counts == house_counts

    _assert_same_signals(house_signals, house_shaped_advice.exits)

    house_stops = house_signals[STOP_COLUMN].to_numpy(dtype="float64")
    adapter_stops = house_shaped_advice.exits[STOP_COLUMN].to_numpy(dtype="float64")
    assert int(np.isnan(house_stops).sum()) == NAN_STOP_CANDLES
    assert int(np.isnan(adapter_stops).sum()) == NAN_STOP_CANDLES
    # ... and a raw `==` comparison would have been a false negative here: NaN is
    # never equal to NaN, which is exactly why `equal_nan=True` is mandatory.
    assert not np.array_equal(house_stops, adapter_stops)


# ---------------------------------------------------------------------------
# scenario 4 -- the frame shape Freqtrade really passes
# ---------------------------------------------------------------------------


def test_scenario_4a_hand_built_freqtrade_frame(
    market_frame: pd.DataFrame,
    freqtrade_frame: pd.DataFrame,
    freqtrade_shaped_advice: Advice,
) -> None:
    """A positional index plus a ``date`` column changes nothing to the signals."""
    ft = freqtrade_frame
    assert isinstance(ft.index, pd.RangeIndex)
    assert len(ft) == CANDLE_COUNT
    assert list(ft.columns) == ["date", *REQUIRED_OHLCV_COLUMNS]

    advice = freqtrade_shaped_advice
    # The incoming index and the date column are kept, not rebuilt: Freqtrade's
    # validator compares the length, the last close and the last date.
    assert isinstance(advice.indicators.index, pd.RangeIndex)
    assert advice.indicators.index.equals(ft.index)
    assert advice.entries.index.equals(ft.index)
    assert advice.exits.index.equals(ft.index)
    for frame in (advice.indicators, advice.entries, advice.exits):
        assert "date" in frame.columns
        assert len(frame) == CANDLE_COUNT

    assert _counts(advice.exits, list(FREQTRADE_COLUMNS)) == EXPECTED_SIGNALS
    assert advice.exits["date"].iloc[-1] == FIXTURE_END
    assert float(advice.exits["close"].iloc[-1]) == float(market_frame["close"].iloc[-1])

    _, house_signals = get_strategy("basic").run(market_frame)
    _assert_same_signals(house_signals, advice.exits)


def test_scenario_4b_real_converter_frame_passes_the_result_validator(
    market_frame: pd.DataFrame,
) -> None:
    """The frame built by ``ohlcv_to_dataframe`` survives the chain untouched.

    ``StrategyResultValidator(warn_only=False).assert_df`` raises as soon as the
    strategy reindexes, shortens or re-dates the frame, so this is the assertion
    that forces the adapter to keep the incoming ``RangeIndex`` and ``date``.
    """
    ft = _converted_frame(market_frame)
    assert isinstance(ft.index, pd.RangeIndex)
    assert len(ft) == CANDLE_COUNT
    assert list(ft.columns) == ["date", *REQUIRED_OHLCV_COLUMNS]
    assert ft["date"].iloc[0] == FIXTURE_START
    assert ft["date"].iloc[-1] == FIXTURE_END

    obj = make_freqtrade_strategy("basic")({})
    validator = StrategyResultValidator(ft, warn_only=False)

    indicators = obj.advise_indicators(ft.copy(), dict(METADATA))
    validator.assert_df(indicators)
    entries = obj.advise_entry(indicators.copy(), dict(METADATA))
    validator.assert_df(entries)
    exits = obj.advise_exit(entries.copy(), dict(METADATA))
    validator.assert_df(exits)

    # Same length, same last close, same last date -- checked here too, so the
    # test states the invariant instead of only relying on the validator.
    assert len(exits) == len(ft)
    assert float(exits["close"].iloc[-1]) == float(ft["close"].iloc[-1])
    assert exits["date"].iloc[-1] == ft["date"].iloc[-1]
    assert isinstance(exits.index, pd.RangeIndex)
    assert exits.index.equals(ft.index)

    assert _counts(exits, list(FREQTRADE_COLUMNS)) == EXPECTED_SIGNALS
    _, house_signals = get_strategy("basic").run(market_frame)
    _assert_same_signals(house_signals, exits)


# ---------------------------------------------------------------------------
# scenario 5 -- custom_stoploss replays the per-candle house stop
# ---------------------------------------------------------------------------


def test_scenario_5_custom_stoploss_replays_the_house_stop(
    freqtrade_shaped_advice: Advice,
) -> None:
    """The stop of a real signal is replayed, and a stop-less signal returns ``None``."""
    exits = freqtrade_shaped_advice.exits
    positions = np.flatnonzero(exits["enter_long"].to_numpy(dtype=bool))
    assert positions.size == EXPECTED_SIGNALS[0]
    position = int(positions[0])
    assert position == FIRST_ENTRY_POSITION

    # The house engine (like Freqtrade) fills at the open of the candle *after*
    # the signal one, so ``position + 1`` is the fill candle; its close is the
    # ``current_rate`` handed to ``custom_stoploss``.  What matters is that the
    # *absolute* stop replayed below was captured on the signal candle
    # (``position``) and is never recomputed from the current rate.
    fill_date = pd.Timestamp(exits["date"].iloc[position + 1])
    fill_rate = float(exits["close"].iloc[position + 1])
    absolute_stop = float(exits[STOP_COLUMN].iloc[position])
    assert not np.isnan(absolute_stop)
    assert absolute_stop < fill_rate

    trade = FakeTrade(open_date_utc=fill_date)
    ratio = freqtrade_shaped_advice.strategy.custom_stoploss(
        PAIR, trade, fill_date, fill_rate, 0.0, False
    )

    # Freqtrade 2026.8 returns a non-negative ratio (positive = below the price).
    assert ratio == stoploss_from_absolute(absolute_stop, fill_rate)
    assert ratio == pytest.approx(1.0 - absolute_stop / fill_rate)
    assert ratio == pytest.approx(0.012710998331483259)
    assert 0.0 < ratio < 1.0

    # A signal candle carrying a NaN stop records no stop at all (the first 14
    # candles are the indicator warm-up): Freqtrade must then fall back to its own
    # `stoploss` attribute instead of trading a wrong one.
    warm_up_date = pd.Timestamp(exits["date"].iloc[1])
    assert np.isnan(float(exits[STOP_COLUMN].iloc[0]))
    assert (
        freqtrade_shaped_advice.strategy.custom_stoploss(
            PAIR,
            FakeTrade(open_date_utc=warm_up_date),
            warm_up_date,
            float(exits["close"].iloc[1]),
            0.0,
            False,
        )
        is None
    )


# ---------------------------------------------------------------------------
# scenario 6 -- Freqtrade resolves the shipped strategy by name
# ---------------------------------------------------------------------------


def test_scenario_6_real_resolver_loads_basic_strategy_by_name() -> None:
    """``--strategy BasicStrategy`` resolves through the real ``StrategyResolver``."""
    found, path = StrategyResolver._search_object(STRATEGIES_DIR, object_name="BasicStrategy")

    assert found is not None
    assert path is not None
    assert path == STRATEGIES_DIR / "BasicStrategy.py"
    # Freqtrade keeps the class whose __module__ equals the file stem: an alias
    # (`BasicStrategy = make_freqtrade_strategy("basic")`) would resolve to
    # (None, None) instead.
    assert found.__name__ == "BasicStrategy"
    assert found.__module__ == "BasicStrategy"
    assert issubclass(found, IStrategy)

    # A literal `class` statement, no business logic: the resolved class behaves
    # exactly like the one the factory builds.
    obj = found({})
    assert validate_freqtrade_adapter_class(type(obj)) is None
    assert isinstance(obj, IStrategy)
