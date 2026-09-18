"""Tests of the per-candle stop-loss bridge (:mod:`trading_platform.strategy.freqtrade_stoploss`).

Everything here is **offline and deterministic**:

* the unit tests are hand-built frames and timestamps, no market data involved;
* the module is checked to be ``freqtrade``-free at import time with an AST test
  (the whole suite must stay green with the ``.[dev]`` extra only);
* the only tests that touch the real Freqtrade interface are guarded by
  ``pytest.importorskip("freqtrade")`` *inside* the test body;
* one cross-check runs the reference ``basic`` strategy over the cached
  BTC/USDT 1h frame (skipped when the cache is absent) and replays Freqtrade's
  one-candle signal shift, which is the whole point of the mapping.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import trading_platform.strategy.freqtrade_stoploss as stoploss_module
from trading_platform.core.constants import OHLCV_INDEX_NAME, SIGNAL_COLUMNS
from trading_platform.core.errors import ConfigError, StrategyError
from trading_platform.strategy.freqtrade_stoploss import (
    DEFAULT_FREQTRADE_STOPLOSS,
    FREQTRADE_STOPLOSS_COLUMN,
    candle_floor,
    entry_stop_map,
    lookup_entry_stop,
    signal_candle_for,
    stoploss_ratio_from_absolute,
)
from trading_platform.strategy.registry import get_strategy

MODULE_PATH = Path(str(stoploss_module.__file__))
CACHE_PATH = Path("data/cache/binance/BTC_USDT/1h.parquet")

#: Measured on freqtrade 2026.8 / the frozen BTC/USDT 1h cache of the repository.
FREQTRADE_LONG_RATIO = 0.09090909090909094
CACHE_CANDLES = 17_543
CACHE_CANDLES_WITH_STOP = 17_529
CACHE_ENTRY_LONG = 395
CACHE_EXIT_LONG = 400


def _frame(stops: list[float], *, tz_aware: bool = True) -> pd.DataFrame:
    """A minimal house signal frame whose ``stop_loss`` column holds ``stops``."""
    index = pd.date_range(
        "2023-01-01T00:00:00", periods=len(stops), freq="h", tz="UTC" if tz_aware else None
    )
    index.name = OHLCV_INDEX_NAME
    frame = pd.DataFrame(dict.fromkeys(SIGNAL_COLUMNS[:-1], False), index=index)
    frame[FREQTRADE_STOPLOSS_COLUMN] = stops
    return frame[list(SIGNAL_COLUMNS)]


# ---------------------------------------------------------------------------
# frozen constants and public API
# ---------------------------------------------------------------------------


def test_frozen_constants() -> None:
    """The two module constants are part of the frozen contract."""
    assert DEFAULT_FREQTRADE_STOPLOSS == -0.99
    assert FREQTRADE_STOPLOSS_COLUMN == "stop_loss"
    assert FREQTRADE_STOPLOSS_COLUMN in SIGNAL_COLUMNS


def test_public_api_is_alphabetical_and_frozen() -> None:
    """``__all__`` is exactly the frozen, alphabetically sorted surface."""
    assert stoploss_module.__all__ == sorted(stoploss_module.__all__)
    assert stoploss_module.__all__ == [
        "DEFAULT_FREQTRADE_STOPLOSS",
        "FREQTRADE_STOPLOSS_COLUMN",
        "candle_floor",
        "entry_stop_map",
        "lookup_entry_stop",
        "signal_candle_for",
        "stoploss_ratio_from_absolute",
    ]


# ---------------------------------------------------------------------------
# the module must stay importable without freqtrade
# ---------------------------------------------------------------------------


def test_no_top_level_freqtrade_import() -> None:
    """The optional dependency must never be imported at module import time."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert imported, "the module is expected to import pandas/core at least"
    assert not [name for name in imported if name.split(".")[0] == "freqtrade"]


def test_freqtrade_is_imported_lazily_inside_the_ratio_helper() -> None:
    """The single Freqtrade import lives inside ``stoploss_ratio_from_absolute``."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    helper = next(node for node in functions if node.name == "stoploss_ratio_from_absolute")
    modules = [
        node.module
        for node in ast.walk(helper)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert "freqtrade.strategy" in modules


def test_import_of_the_module_does_not_load_freqtrade() -> None:
    """Importing the adapter support module must not pull the optional extra in."""
    code = (
        "import sys, trading_platform.strategy.freqtrade_stoploss as m;"
        "print('freqtrade' in sys.modules, m.FREQTRADE_STOPLOSS_COLUMN)"
    )
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == f"False {FREQTRADE_STOPLOSS_COLUMN}"


# ---------------------------------------------------------------------------
# entry_stop_map
# ---------------------------------------------------------------------------


def test_entry_stop_map_keeps_only_candles_with_a_stop() -> None:
    """Two valid stops and one ``NaN`` -> exactly the two expected entries."""
    frame = _frame([100.0, float("nan"), 102.5])
    stops = entry_stop_map(frame)
    assert list(stops) == [frame.index[0], frame.index[2]]
    assert stops == {frame.index[0]: 100.0, frame.index[2]: 102.5}
    assert {type(value) for value in stops.values()} == {float}


def test_entry_stop_map_without_the_column_is_empty() -> None:
    """A frame with no ``stop_loss`` column at all maps to ``{}``."""
    frame = _frame([100.0, 101.0]).drop(columns=[FREQTRADE_STOPLOSS_COLUMN])
    assert entry_stop_map(frame) == {}


def test_entry_stop_map_normalises_a_naive_index_to_utc() -> None:
    """A naive house index is read as UTC, so live lookups hit the same keys."""
    frame = _frame([100.0], tz_aware=False)
    stops = entry_stop_map(frame)
    (key,) = stops
    assert key == pd.Timestamp("2023-01-01T00:00:00Z")
    assert str(key.tz) == "UTC"


def test_entry_stop_map_skips_non_numeric_values() -> None:
    """Anything that is not a number (``None``, text) is treated as "no stop"."""
    frame = _frame([100.0, 101.0, 102.0])
    frame[FREQTRADE_STOPLOSS_COLUMN] = pd.Series(
        [100.0, None, "not-a-number"], index=frame.index, dtype="object"
    )
    stops = entry_stop_map(frame)
    assert stops == {frame.index[0]: 100.0}


def test_entry_stop_map_empty_frame() -> None:
    """An empty frame is legal and yields an empty map."""
    assert entry_stop_map(_frame([])) == {}


def test_entry_stop_keys_are_hashable_across_equivalent_timezones() -> None:
    """``Z`` and ``+01:00`` spellings of the same instant are the same key."""
    frame = _frame([100.0])
    stops = entry_stop_map(frame)
    equivalent = pd.Timestamp("2023-01-01T01:00:00+01:00")
    assert equivalent == frame.index[0]
    assert stops[equivalent] == 100.0


# ---------------------------------------------------------------------------
# candle_floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timestamp", "timeframe", "expected"),
    [
        ("2023-01-01T12:34:56Z", "1h", "2023-01-01T12:00:00Z"),
        ("2023-01-01T12:34:56Z", "4h", "2023-01-01T12:00:00Z"),
        ("2023-01-01T12:34:56Z", "1d", "2023-01-01T00:00:00Z"),
        ("2023-01-01T12:34:56Z", "15m", "2023-01-01T12:30:00Z"),
        ("2023-01-01T12:00:00Z", "1h", "2023-01-01T12:00:00Z"),
        ("2023-01-01T12:34:56", "1h", "2023-01-01T12:00:00Z"),
        ("2023-01-01T13:34:56+01:00", "1h", "2023-01-01T12:00:00Z"),
        (pd.Timestamp("2023-01-01T12:34:56Z"), "1h", "2023-01-01T12:00:00Z"),
    ],
)
def test_candle_floor(timestamp: object, timeframe: str, expected: str) -> None:
    """``candle_floor`` floors on the timeframe grid, in UTC."""
    floored = candle_floor(timestamp, timeframe)
    assert floored == pd.Timestamp(expected)
    assert str(floored.tz) == "UTC"


@pytest.mark.parametrize("timeframe", ["2h", "1w", "", "1H"])
def test_candle_floor_rejects_an_unsupported_timeframe(timeframe: str) -> None:
    """The timeframe table of :mod:`trading_platform.core.constants` is the only source."""
    with pytest.raises(ConfigError):
        candle_floor("2023-01-01T12:00:00Z", timeframe)


# ---------------------------------------------------------------------------
# signal_candle_for
# ---------------------------------------------------------------------------


def test_signal_candle_for_subtracts_one_candle() -> None:
    """A trade filled on the 13:00 candle was signalled on the 12:00 candle."""
    assert signal_candle_for("2023-01-01T13:00:00Z", "1h") == pd.Timestamp("2023-01-01T12:00:00Z")


def test_signal_candle_for_offsets_by_the_timeframe() -> None:
    """The offset follows the timeframe, not a hard-coded hour."""
    assert signal_candle_for("2023-01-01T16:00:00Z", "4h") == pd.Timestamp("2023-01-01T12:00:00Z")
    assert signal_candle_for("2023-01-02T00:00:00Z", "1d") == pd.Timestamp("2023-01-01T00:00:00Z")


def test_signal_candle_for_rejects_an_unsupported_timeframe() -> None:
    """An unsupported timeframe propagates ``ConfigError``."""
    with pytest.raises(ConfigError):
        signal_candle_for("2023-01-01T13:00:00Z", "3h")


# ---------------------------------------------------------------------------
# lookup_entry_stop
# ---------------------------------------------------------------------------


def test_lookup_entry_stop_hits_the_signal_candle() -> None:
    """The fill candle is one candle after the signal candle that captured the stop."""
    frame = _frame([100.0, 101.0, 102.0])
    stops = entry_stop_map(frame)
    assert lookup_entry_stop(stops, open_date=frame.index[1], timeframe="1h") == 100.0
    assert lookup_entry_stop(stops, open_date=frame.index[2], timeframe="1h") == 101.0


def test_lookup_entry_stop_falls_back_on_the_fill_candle() -> None:
    """Second key: a map that happens to be keyed by the fill candle itself."""
    frame = _frame([100.0, 101.0, 102.0])
    stops = {frame.index[1]: 101.0}
    assert lookup_entry_stop(stops, open_date=frame.index[1], timeframe="1h") == 101.0


def test_lookup_entry_stop_falls_back_on_the_raw_timestamp() -> None:
    """Third key: the caller's own timestamp, when it is not on a candle boundary."""
    raw = pd.Timestamp("2023-01-01T12:00:37Z")
    assert lookup_entry_stop({raw: 42.0}, open_date=raw, timeframe="1h") == 42.0


def test_lookup_entry_stop_on_a_mid_candle_live_timestamp() -> None:
    """A live fill at 12:00:37 reacts to the last *closed* candle (11:00)."""
    stops = {pd.Timestamp("2023-01-01T11:00:00Z"): 111.0}
    open_date = pd.Timestamp("2023-01-01T12:00:37Z")
    assert signal_candle_for(open_date, "1h") == pd.Timestamp("2023-01-01T11:00:00Z")
    assert lookup_entry_stop(stops, open_date=open_date, timeframe="1h") == 111.0


def test_lookup_entry_stop_accepts_a_naive_open_date() -> None:
    """A naive live timestamp is read as UTC."""
    stops = {pd.Timestamp("2023-01-01T11:00:00Z"): 111.0}
    assert lookup_entry_stop(stops, open_date="2023-01-01T12:00:37", timeframe="1h") == 111.0


def test_lookup_entry_stop_returns_none_for_an_unknown_candle() -> None:
    """An unknown candle (or pair) means "no captured stop" -> ``None``."""
    frame = _frame([100.0])
    stops = entry_stop_map(frame)
    assert lookup_entry_stop(stops, open_date="2023-06-01T00:00:00Z", timeframe="1h") is None


def test_lookup_entry_stop_returns_none_for_an_empty_map() -> None:
    """An empty map short-circuits to ``None``."""
    assert lookup_entry_stop({}, open_date="2023-01-01T12:00:00Z", timeframe="1h") is None


def test_lookup_entry_stop_on_real_data_matches_the_house_stop_of_the_signal_candle() -> None:
    """Cross-check on the frozen BTC/USDT 1h cache, replaying Freqtrade's shift.

    ``signals`` is the house signal frame; Freqtrade shifts ``enter_long`` by one
    candle before filling, so the trade opened on candle ``t + 1`` must be handed
    **exactly** the ``stop_loss`` value the house engine read on candle ``t``.
    """
    if not CACHE_PATH.exists():  # pragma: no cover - the cache ships with the repo
        pytest.skip(f"missing data cache: {CACHE_PATH}")
    data = pd.read_parquet(CACHE_PATH)
    _, signals = get_strategy("basic").run(data)

    assert len(signals) == CACHE_CANDLES
    stops = entry_stop_map(signals)
    assert len(stops) == CACHE_CANDLES_WITH_STOP

    entry_candles = signals.index[signals["entry_long"].to_numpy()]
    exit_candles = signals.index[signals["exit_long"].to_numpy()]
    assert len(entry_candles) == CACHE_ENTRY_LONG
    assert len(exit_candles) == CACHE_EXIT_LONG

    matched = 0
    for signal_candle in entry_candles:
        open_date = signal_candle + pd.Timedelta(hours=1)
        if open_date > signals.index[-1]:  # pragma: no cover - the last signal never fills
            continue
        expected = float(signals.at[signal_candle, FREQTRADE_STOPLOSS_COLUMN])
        assert lookup_entry_stop(stops, open_date=open_date, timeframe="1h") == expected
        matched += 1
    assert matched == CACHE_ENTRY_LONG

    # The same holds for a live fill landing in the middle of the fill candle.
    first = entry_candles[0]
    live_fill = first + pd.Timedelta(hours=1, minutes=37)
    assert lookup_entry_stop(stops, open_date=live_fill, timeframe="1h") == float(
        signals.at[first, FREQTRADE_STOPLOSS_COLUMN]
    )


# ---------------------------------------------------------------------------
# stoploss_ratio_from_absolute — failure path (no freqtrade installed)
# ---------------------------------------------------------------------------


def _simulate_missing_freqtrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``freqtrade`` unimportable, exactly as on a ``.[dev]``-only checkout.

    ``sys.modules["freqtrade"] = None`` alone is **not** enough once the real
    library has been imported anywhere in the session: ``from freqtrade.strategy
    import stoploss_from_absolute`` is then served straight from ``sys.modules``
    without walking through the parent, so the simulated absence silently stops
    biting.  The cached submodules are dropped too, through ``monkeypatch`` so
    every one of them is put back — the very same objects, hence a single
    ``IStrategy`` class — at teardown.
    """
    cached = [name for name in sys.modules if name == "freqtrade" or name.startswith("freqtrade.")]
    for name in cached:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "freqtrade", None)


def test_ratio_helper_without_freqtrade_raises_strategy_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulating ``ImportError`` proves the suite stays green without the extra."""
    _simulate_missing_freqtrade(monkeypatch)
    with pytest.raises(StrategyError) as excinfo:
        stoploss_ratio_from_absolute(100.0, 110.0, is_short=False)
    message = str(excinfo.value)
    assert "freqtrade is not installed" in message
    assert "trading-platform[freqtrade]" in message


# ---------------------------------------------------------------------------
# stoploss_ratio_from_absolute — real Freqtrade interface (skipped in CI)
# ---------------------------------------------------------------------------


def test_ratio_helper_delegates_to_freqtrade() -> None:
    """The helper must return exactly what ``stoploss_from_absolute`` returns."""
    pytest.importorskip("freqtrade")
    from freqtrade.strategy import stoploss_from_absolute

    assert (
        stoploss_ratio_from_absolute(100.0, 110.0, is_short=False)
        == stoploss_from_absolute(100.0, 110.0)
        == FREQTRADE_LONG_RATIO
    )
    assert stoploss_ratio_from_absolute(120.0, 110.0, is_short=False) == (
        stoploss_from_absolute(120.0, 110.0)
    )


def test_ratio_helper_short_side_is_mirrored_by_freqtrade() -> None:
    """For a short, the adverse stop sits *above* the rate and yields the ratio.

    Measured on freqtrade 2026.8: the function negates the long formula and then
    clamps the result at ``0.0``, so a stop *below* the rate on a short (the
    favourable side) is reported as ``0.0``, not as a negative ratio.
    """
    pytest.importorskip("freqtrade")
    from freqtrade.strategy import stoploss_from_absolute

    assert stoploss_ratio_from_absolute(120.0, 110.0, is_short=True) == (
        stoploss_from_absolute(120.0, 110.0, is_short=True)
    )
    assert stoploss_ratio_from_absolute(120.0, 110.0, is_short=True) == pytest.approx(
        FREQTRADE_LONG_RATIO
    )
    assert stoploss_ratio_from_absolute(100.0, 110.0, is_short=True) == 0.0


def test_ratio_helper_clamps_a_breached_long_stop_at_zero() -> None:
    """A stop already above the current rate becomes an immediate exit at market."""
    pytest.importorskip("freqtrade")
    assert stoploss_ratio_from_absolute(120.0, 110.0, is_short=False) == 0.0


def test_ratio_helper_edges_of_the_freqtrade_formula() -> None:
    """Degenerate inputs are handled by Freqtrade itself, not re-implemented here."""
    pytest.importorskip("freqtrade")
    assert stoploss_ratio_from_absolute(100.0, 0.0, is_short=False) == 1.0
    assert stoploss_ratio_from_absolute(200.0, 100.0, is_short=False) == 0.0
    assert stoploss_ratio_from_absolute(50.0, 100.0, is_short=False) == 0.5


def test_ratio_helper_on_the_real_cache_matches_the_signal_candle_stop() -> None:
    """End-to-end: the ratio of a captured absolute stop is Freqtrade's own ratio."""
    pytest.importorskip("freqtrade")
    if not CACHE_PATH.exists():  # pragma: no cover - the cache ships with the repo
        pytest.skip(f"missing data cache: {CACHE_PATH}")
    from freqtrade.strategy import stoploss_from_absolute

    data = pd.read_parquet(CACHE_PATH)
    _, signals = get_strategy("basic").run(data)
    stops = entry_stop_map(signals)
    first = signals.index[signals["entry_long"].to_numpy()][0]
    fill = first + pd.Timedelta(hours=1)
    absolute = lookup_entry_stop(stops, open_date=fill, timeframe="1h")
    assert absolute is not None
    current_rate = float(data.at[fill, "open"])
    assert stoploss_ratio_from_absolute(absolute, current_rate, is_short=False) == (
        stoploss_from_absolute(absolute, current_rate, is_short=False)
    )
    # A stop that is 50% below the current rate is a 0.5 ratio for a long.
    assert stoploss_ratio_from_absolute(absolute, absolute * 2.0, is_short=False) == 0.5


def test_ratio_helper_is_pure_and_recomputes_every_call() -> None:
    """The stop stays absolute: the ratio follows the rate, it never trails."""
    pytest.importorskip("freqtrade")
    stop = 100.0
    assert stoploss_ratio_from_absolute(stop, 100.0, is_short=False) == 0.0
    assert stoploss_ratio_from_absolute(stop, 200.0, is_short=False) == 0.5
    # ... and back: no state is kept between calls.
    assert stoploss_ratio_from_absolute(stop, 200.0, is_short=False) == 0.5


def test_stop_is_not_read_from_the_freqtrade_frame() -> None:
    """The house column is audited only: Freqtrade's own headers never read it."""
    pytest.importorskip("freqtrade")
    from freqtrade.optimize.backtesting import HEADERS

    assert FREQTRADE_STOPLOSS_COLUMN not in HEADERS
    assert "enter_long" in HEADERS  # the translated signal columns, not the house ones
    assert "entry_long" not in HEADERS


def test_house_and_freqtrade_ratio_agree_on_a_synthetic_frame() -> None:
    """The mapped stop of a synthetic house frame survives the ratio round-trip."""
    pytest.importorskip("freqtrade")
    frame = _frame([99.0, 100.0, 101.0])
    stops = entry_stop_map(frame)
    absolute = lookup_entry_stop(stops, open_date=frame.index[1], timeframe="1h")
    assert absolute == 99.0
    ratio = stoploss_ratio_from_absolute(absolute, 110.0, is_short=False)
    assert ratio == pytest.approx(1.0 - 99.0 / 110.0)
    assert np.isclose(ratio, 0.1)
