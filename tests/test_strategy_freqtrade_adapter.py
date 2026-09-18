"""Tests of the Freqtrade ``IStrategy`` adapter (``strategy.freqtrade_adapter``).

Two groups, and the split is deliberate (``docs/testing-policy.md`` §1.2):

* everything that can run **offline** does: the frame translation, the
  ``entry_*`` -> ``enter_*`` rename, the non-destructive contract, the factory's
  validation errors, the parameter bridge and the stop-loss bookkeeping are all
  exercised through hand-built stand-ins — **no Freqtrade import is needed**;
* everything that needs the *real* Freqtrade interface is guarded by
  ``pytest.importorskip("freqtrade")`` **inside the test body**, because the CI
  installs the ``.[dev]`` extra only, where Freqtrade is absent: the suite must
  stay green there.

The module is imported through its **own path**
(``trading_platform.strategy.freqtrade_adapter``) rather than through the
``trading_platform.strategy`` public namespace, which is owned by another work
package.

The last guarded test is the integration one: it instantiates the adapter class
produced by :func:`make_freqtrade_strategy` and cross-checks, candle by candle,
the signals it renders against the ones the **house** strategy renders on the
**same real data** (``data/cache/binance/BTC_USDT/1h.parquet``, 17543 candles).
That equality is the proof that the ``entry_*`` -> ``enter_*`` translation is
correct; the PnL of the two engines is *not* comparable and is not compared
(``docs/architecture.md`` §4.9.4).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import numpy as np
import pandas as pd
import pytest
from pydantic import Field

from trading_platform.core.errors import StrategyError
from trading_platform.freqtrade import DEFAULT_STRATEGY_NAME
from trading_platform.strategy.base import (
    Strategy,
    StrategyParams,
    ensure_signal_frame,
    require_ohlcv_frame,
)
from trading_platform.strategy.freqtrade_adapter import (
    FREQTRADE_DISABLED_ROI,
    FREQTRADE_INTERFACE_VERSION,
    FREQTRADE_ORDER_COLUMNS,
    SIGNAL_TO_FREQTRADE_COLUMNS,
    _house_strategy,
    _record_entry_stops,
    _signature_issues,
    adapter_custom_stoploss,
    adapter_populate_entry_trend,
    adapter_populate_exit_trend,
    adapter_populate_indicators,
    default_class_name,
    freqtrade_available,
    freqtrade_entry_frame,
    freqtrade_exit_frame,
    freqtrade_indicator_frame,
    freqtrade_strategy_namespace,
    house_frame,
    make_freqtrade_strategy,
    validate_freqtrade_adapter_class,
)
from trading_platform.strategy.freqtrade_parameters import FreqtradeParamSpec
from trading_platform.strategy.registry import STRATEGIES, get_strategy, register_strategy

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The module under test — read as text by the AST tests.
MODULE_PATH = REPO_ROOT / "src" / "trading_platform" / "strategy" / "freqtrade_adapter.py"

#: Real Binance cache used by the integration test (git-ignored: the test skips
#: when it is absent, so a fresh clone stays green).
CACHE_PATH = REPO_ROOT / "data" / "cache" / "binance" / "BTC_USDT" / "1h.parquet"

#: The three arguments ``populate_*`` receives from Freqtrade.
METADATA = {"pair": "BTC/USDT", "timeframe": "1h", "dataframe": None}

#: Parameter defaults of the ``basic`` strategy, used to build stand-ins.
BASIC_VALUES: dict[str, Any] = {
    "ema_fast": 9,
    "ema_slow": 21,
    "rsi_period": 14,
    "rsi_min": 30.0,
    "rsi_max": 70.0,
    "atr_period": 14,
    "atr_stop_multiplier": 2.0,
    "allow_short": False,
}


# ---------------------------------------------------------------------------
# module-scoped hygiene: deliberately none
# ---------------------------------------------------------------------------
#
# This module imports the real library, and it must **not** purge ``freqtrade*``
# from ``sys.modules`` on the way out.  Purging makes the next
# ``import freqtrade.strategy`` re-execute the module and build a **second**
# ``IStrategy`` class object; every class built on the first one — the generated
# adapter classes, and ``user_data/strategies/BasicStrategy.py`` as
# ``StrategyResolver`` resolves it — then fails Freqtrade's own
# ``issubclass(obj, IStrategy)`` check, and ``isinstance`` checks in a module that
# imported the first generation at collection time fail too.
#
# The failure-path tests of the sibling modules do not need anyone to clean up
# after this module: each one installs its own simulation
# (``_simulate_missing_freqtrade``, or a subprocess with a meta-path blocker) and
# purges the cache *inside* ``monkeypatch``, which puts the very same module
# objects back at teardown.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _freqtrade_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Return ``frame`` the way Freqtrade hands it to ``populate_*``.

    A positional ``RangeIndex`` plus the timestamps in a ``date`` column — the
    shape the house OHLCV contract rejects, and therefore the shape
    :func:`house_frame` has to translate.
    """
    out = frame.copy()
    out.index = pd.RangeIndex(len(out))
    out["date"] = frame.index.to_numpy()
    return out


def _house_signals(frame: pd.DataFrame, name: str = "basic") -> pd.DataFrame:
    """Return the house signal frame of ``frame`` for the strategy ``name``."""
    strategy = get_strategy(name)
    return strategy.signals(strategy.prepare(frame))


def _house_stub(**overrides: Any) -> Any:
    """Return a minimal stand-in for a generated adapter instance.

    It carries exactly what the adapter methods read — ``timeframe``,
    ``house_strategy_name``, ``_house_parameter_names``, ``_house_cache``,
    ``_entry_stops`` — and one ``.value``-carrying object per mapped parameter,
    like a Freqtrade parameter.  No Freqtrade import is involved, so every code
    path that does not *build* parameter objects can be tested offline.
    """
    values = dict(BASIC_VALUES)
    values.update(overrides)
    stub = SimpleNamespace(
        timeframe="1h",
        house_strategy_name="basic",
        _house_parameter_names=tuple(values),
        _house_cache=None,
        _entry_stops={},
    )
    for name, value in values.items():
        setattr(stub, name, SimpleNamespace(value=value))
    return stub


def _simulate_missing_freqtrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``freqtrade`` unimportable, exactly as on a ``.[dev]``-only checkout.

    ``sys.modules['freqtrade'] = None`` alone is **not** enough once the package
    has been imported for real in the same process: ``from freqtrade.strategy
    import ...`` is then served straight from ``sys.modules`` without walking
    through the parent.  The cached submodules are therefore dropped too, which
    turns every following ``freqtrade`` import into the usual
    ``ImportError``/``ModuleNotFoundError``.  Everything is restored at teardown
    (``monkeypatch`` undoes the reverse of the order below, so the real module
    ends up where it was).
    """
    cached = [name for name in sys.modules if name == "freqtrade" or name.startswith("freqtrade.")]
    for name in cached:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "freqtrade", None)


# ---------------------------------------------------------------------------
# throwaway strategies -- the genericity proof
# ---------------------------------------------------------------------------


class ToggleParams(StrategyParams):
    """Parameters of :class:`ToggleStrategy`.

    ``label`` is a ``str`` field: Freqtrade has no scalar parameter object for it,
    so :mod:`trading_platform.strategy.freqtrade_parameters` leaves it unmapped —
    it is the documented candidate for a manual ``param_overrides`` entry.
    """

    window: int = Field(default=2, ge=1)
    allow_short: bool = False
    label: str = "fast"


class ToggleStrategy(Strategy):
    """A trivial two-parameter strategy: close above a rolling mean, or below."""

    name = "wp3_toggle"
    ParamsModel = ToggleParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {"window": [1, 2, 4]}

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the ``mean`` indicator column."""
        frame = require_ohlcv_frame(data, name="data")
        frame["mean"] = (
            frame["close"].rolling(self.params.window, min_periods=1).mean().to_numpy("float64")
        )
        return frame

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Cross the close and its rolling mean."""
        close = data["close"].to_numpy("float64")
        mean = data["mean"].to_numpy("float64")
        above = close > mean
        allow_short = bool(self.params.allow_short)
        signals = pd.DataFrame(
            {
                "entry_long": above,
                "exit_long": ~above,
                "entry_short": (~above) if allow_short else np.zeros(len(data), dtype=bool),
                "exit_short": above if allow_short else np.zeros(len(data), dtype=bool),
                "stop_loss": close - close * 0.01,
            },
            index=data.index,
        )
        return ensure_signal_frame(signals, data.index)


class ClashingParams(StrategyParams):
    """A model carrying a parameter named like a Freqtrade class attribute."""

    timeframe: int = Field(default=5, ge=1)
    window: int = Field(default=2, ge=1)


class ClashingStrategy(Strategy):
    """Strategy whose ``timeframe`` parameter collides with the adapter attribute."""

    name = "wp3_clashing"
    ParamsModel = ClashingParams

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the frame unchanged (no indicator needed)."""
        return require_ohlcv_frame(data, name="data")

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return a flat signal frame (nothing ever fires)."""
        index = data.index
        signals = pd.DataFrame(
            {
                "entry_long": np.zeros(len(data), dtype=bool),
                "exit_long": np.zeros(len(data), dtype=bool),
                "entry_short": np.zeros(len(data), dtype=bool),
                "exit_short": np.zeros(len(data), dtype=bool),
                "stop_loss": np.full(len(data), np.nan),
            },
            index=index,
        )
        return ensure_signal_frame(signals, index)


@pytest.fixture
def registered_toggle() -> Iterator[type[Strategy]]:
    """Register the throwaway strategies for one test and unregister them after."""
    register_strategy(ToggleStrategy)
    register_strategy(ClashingStrategy)
    try:
        yield ToggleStrategy
    finally:
        STRATEGIES.pop(ToggleStrategy.name, None)
        STRATEGIES.pop(ClashingStrategy.name, None)


# ---------------------------------------------------------------------------
# frozen constants and module shape
# ---------------------------------------------------------------------------


def test_public_api_is_the_frozen_alphabetical_list() -> None:
    """``__all__`` is exactly the frozen API, and it is sorted."""
    import trading_platform.strategy.freqtrade_adapter as module

    frozen = [
        "FREQTRADE_DISABLED_ROI",
        "FREQTRADE_INTERFACE_VERSION",
        "FREQTRADE_ORDER_COLUMNS",
        "SIGNAL_TO_FREQTRADE_COLUMNS",
        "adapter_custom_stoploss",
        "adapter_populate_entry_trend",
        "adapter_populate_exit_trend",
        "adapter_populate_indicators",
        "freqtrade_available",
        "freqtrade_entry_frame",
        "freqtrade_exit_frame",
        "freqtrade_indicator_frame",
        "freqtrade_strategy_namespace",
        "house_frame",
        "make_freqtrade_strategy",
        "validate_freqtrade_adapter_class",
    ]
    assert module.__all__ == frozen
    assert module.__all__ == sorted(module.__all__)


def test_constants_pin_the_freqtrade_contract() -> None:
    """The version and the Freqtrade-side column names are the verified ones."""
    assert FREQTRADE_INTERFACE_VERSION == 3
    assert FREQTRADE_ORDER_COLUMNS == ("enter_long", "exit_long", "enter_short", "exit_short")
    assert FREQTRADE_DISABLED_ROI == {"0": 100.0}


def test_signal_to_freqtrade_columns_is_the_entry_enter_rename() -> None:
    """The rename table is the single source of truth of the translation."""
    assert SIGNAL_TO_FREQTRADE_COLUMNS == {
        "entry_long": "enter_long",
        "exit_long": "exit_long",
        "entry_short": "enter_short",
        "exit_short": "exit_short",
    }


def test_module_has_no_top_level_freqtrade_import() -> None:
    """The optional extra is never imported at module level (AST proof)."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    top_level: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            top_level.append(node.module)
    assert top_level, "the module is expected to import pandas/numpy/strategy at least"
    assert not [name for name in top_level if name.split(".")[0] == "freqtrade"]


def test_freqtrade_is_imported_lazily_inside_functions() -> None:
    """The two guarded imports live in the bodies of their functions."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    base = functions["_istrategy_base"]
    modules = [
        node.module
        for node in ast.walk(base)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ]
    assert "freqtrade.strategy" in modules


def test_importing_the_module_does_not_load_freqtrade() -> None:
    """Importing the adapter must not pull the optional extra in."""
    code = (
        "import sys, trading_platform.strategy.freqtrade_adapter as m;"
        "print('freqtrade' in sys.modules, m.FREQTRADE_INTERFACE_VERSION)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120
    )
    assert result.stdout.strip() == f"False {FREQTRADE_INTERFACE_VERSION}"


# ---------------------------------------------------------------------------
# house_frame -- Freqtrade frame -> house frame
# ---------------------------------------------------------------------------


def test_house_frame_indexes_a_date_column_frame(ohlcv_frame: pd.DataFrame) -> None:
    """A ``RangeIndex`` + ``date`` frame is re-indexed on its ``date`` column."""
    frame = _freqtrade_frame(ohlcv_frame)
    translated = house_frame(frame)
    assert isinstance(translated.index, pd.DatetimeIndex)
    assert translated.index.equals(pd.DatetimeIndex(frame["date"]))
    assert "date" not in translated.columns
    assert list(translated.columns) == ["open", "high", "low", "close", "volume"]


def test_house_frame_leaves_a_timestamp_indexed_frame_untouched(
    ohlcv_frame: pd.DataFrame,
) -> None:
    """A house frame (``DatetimeIndex`` named ``timestamp``) is returned as is."""
    assert ohlcv_frame.index.name == "timestamp"
    assert house_frame(ohlcv_frame) is ohlcv_frame


def test_house_frame_leaves_a_date_indexed_frame_untouched(ohlcv_frame: pd.DataFrame) -> None:
    """The *name* of the ``DatetimeIndex`` does not matter: both are accepted."""
    frame = ohlcv_frame.copy()
    frame.index = frame.index.rename("date")
    assert house_frame(frame) is frame


def test_house_frame_rejects_a_frame_with_neither(ohlcv_frame: pd.DataFrame) -> None:
    """A frame with a ``RangeIndex`` and no ``date`` column cannot be translated."""
    broken = ohlcv_frame.copy()
    broken.index = pd.RangeIndex(len(broken))
    with pytest.raises(StrategyError) as excinfo:
        house_frame(broken)
    assert "neither a DatetimeIndex nor a 'date' column" in str(excinfo.value)
    assert excinfo.value.issues


# ---------------------------------------------------------------------------
# frame translation -- house frame -> Freqtrade frame
# ---------------------------------------------------------------------------


def test_freqtrade_indicator_frame_adds_the_house_indicator_columns(
    ohlcv_frame: pd.DataFrame,
) -> None:
    """The four indicator columns of ``basic`` are appended, nothing else changes."""
    frame = _freqtrade_frame(ohlcv_frame)
    out = freqtrade_indicator_frame(frame, get_strategy("basic"))
    for column in ("ema_fast", "ema_slow", "rsi", "atr"):
        assert column in out.columns
    assert list(out.columns)[: len(frame.columns)] == list(frame.columns)
    prepared = get_strategy("basic").prepare(ohlcv_frame)
    for column in ("ema_fast", "ema_slow", "rsi", "atr"):
        np.testing.assert_allclose(
            out[column].to_numpy(), prepared[column].to_numpy(), equal_nan=True
        )


def test_freqtrade_entry_frame_renames_entry_long_and_entry_short(
    ohlcv_frame: pd.DataFrame,
) -> None:
    """``entry_*`` -> ``enter_*``: the house names never reach the Freqtrade frame."""
    frame = _freqtrade_frame(ohlcv_frame)
    signals = _house_signals(ohlcv_frame)
    out = freqtrade_entry_frame(frame, get_strategy("basic"))

    assert "enter_long" in out.columns
    assert "enter_short" in out.columns
    assert "stop_loss" in out.columns
    assert "entry_long" not in out.columns
    assert "entry_short" not in out.columns

    np.testing.assert_array_equal(out["enter_long"].to_numpy(), signals["entry_long"].to_numpy())
    np.testing.assert_array_equal(out["enter_short"].to_numpy(), signals["entry_short"].to_numpy())
    np.testing.assert_allclose(
        out["stop_loss"].to_numpy(), signals["stop_loss"].to_numpy(), equal_nan=True
    )
    assert out["enter_long"].dtype == bool
    assert out["stop_loss"].dtype == np.dtype("float64")


def test_freqtrade_exit_frame_renames_the_exit_columns(ohlcv_frame: pd.DataFrame) -> None:
    """``exit_long`` / ``exit_short`` keep their names and carry the house values."""
    frame = _freqtrade_frame(ohlcv_frame)
    signals = _house_signals(ohlcv_frame)
    out = freqtrade_exit_frame(frame, get_strategy("basic"))

    assert "exit_long" in out.columns
    assert "exit_short" in out.columns
    assert "stop_loss" not in out.columns
    np.testing.assert_array_equal(out["exit_long"].to_numpy(), signals["exit_long"].to_numpy())
    np.testing.assert_array_equal(out["exit_short"].to_numpy(), signals["exit_short"].to_numpy())
    assert out["exit_long"].dtype == bool


def test_frame_helpers_never_mutate_the_incoming_frame(ohlcv_frame: pd.DataFrame) -> None:
    """The incoming frame keeps its object identity, its columns and its values."""
    frame = _freqtrade_frame(ohlcv_frame)
    before = frame.copy(deep=True)
    strategy = get_strategy("basic")

    for out in (
        freqtrade_indicator_frame(frame, strategy),
        freqtrade_entry_frame(frame, strategy),
        freqtrade_exit_frame(frame, strategy),
    ):
        assert out is not frame
        assert isinstance(out.index, pd.RangeIndex)
        pd.testing.assert_frame_equal(frame, before)


def test_frame_helpers_preserve_length_close_and_date(ohlcv_frame: pd.DataFrame) -> None:
    """Offline mirror of Freqtrade's ``StrategyResultValidator.assert_df``.

    ``assert_df`` requires the returned frame to keep the same length, the same
    last ``close`` and the same ``date``; those three checks are reproduced here
    so that a regression is caught even where Freqtrade is not installed.
    """
    frame = _freqtrade_frame(ohlcv_frame)
    strategy = get_strategy("basic")
    for out in (
        freqtrade_indicator_frame(frame, strategy),
        freqtrade_entry_frame(frame, strategy),
        freqtrade_exit_frame(frame, strategy),
    ):
        assert len(out) == len(frame)
        assert out["close"].iloc[-1] == frame["close"].iloc[-1]
        assert out["date"].iloc[-1] == frame["date"].iloc[-1]


# ---------------------------------------------------------------------------
# the adapter methods themselves -- offline stand-ins
# ---------------------------------------------------------------------------


def test_adapter_populate_indicators_is_the_house_prepare(ohlcv_frame: pd.DataFrame) -> None:
    """``populate_indicators`` renders the house indicators on a Freqtrade frame."""
    frame = _freqtrade_frame(ohlcv_frame)
    out = adapter_populate_indicators(_house_stub(), frame, METADATA)
    assert list(out.columns) == [*frame.columns, "ema_fast", "ema_slow", "rsi", "atr"]
    assert len(out) == len(frame)


def test_adapter_populate_entry_trend_renames_and_records_the_stops(
    ohlcv_frame: pd.DataFrame,
) -> None:
    """``populate_entry_trend`` renames the entries **and** fills ``_entry_stops``."""
    frame = _freqtrade_frame(ohlcv_frame)
    stub = _house_stub()
    out = adapter_populate_entry_trend(stub, frame, METADATA)
    signals = _house_signals(ohlcv_frame)

    assert "enter_long" in out.columns
    assert "enter_short" in out.columns
    assert "entry_long" not in out.columns
    np.testing.assert_array_equal(out["enter_long"].to_numpy(), signals["entry_long"].to_numpy())

    stops = signals["stop_loss"]
    assert len(stub._entry_stops) == int(stops.notna().sum())
    first = stops.index[stops.notna()][0]
    assert stub._entry_stops[("BTC/USDT", first)] == float(stops.loc[first])
    assert all(key[0] == "BTC/USDT" for key in stub._entry_stops)


def test_instance_stops_is_created_on_demand_and_never_shared(
    ohlcv_frame: pd.DataFrame,
) -> None:
    """The stop map lives in the instance: two instances never share one."""
    first = _house_stub()
    second = _house_stub()
    first._entry_stops = None
    second._entry_stops = None
    frame = _freqtrade_frame(ohlcv_frame)
    adapter_populate_entry_trend(first, frame, METADATA)
    adapter_populate_entry_trend(second, frame, METADATA)
    assert first._entry_stops
    assert first._entry_stops is not second._entry_stops
    assert first._entry_stops == second._entry_stops


def test_signature_issues_pins_the_three_argument_shape() -> None:
    """The arity helper accepts the Freqtrade shape and rejects every other one."""

    def correct(self: Any, dataframe: pd.DataFrame, metadata: dict[str, Any]) -> pd.DataFrame:
        return dataframe

    def too_short(self: Any, dataframe: pd.DataFrame) -> pd.DataFrame:
        return dataframe

    def variadic(self: Any, *args: Any, **kwargs: Any) -> None:
        return None

    assert _signature_issues("populate_indicators", correct) == []
    assert _signature_issues("populate_indicators", too_short) == [
        "populate_indicators must take (self, dataframe, metadata), got (self, dataframe)"
    ]
    assert (
        "must take exactly (self, dataframe, metadata), got *args/**kwargs"
        in (_signature_issues("populate_indicators", variadic)[0])
    )
    assert _signature_issues("populate_indicators", 42) == [
        "populate_indicators has no inspectable signature"
    ]


def test_adapter_populate_exit_trend_renames_the_exits(ohlcv_frame: pd.DataFrame) -> None:
    """``populate_exit_trend`` adds the exit columns and records nothing."""
    frame = _freqtrade_frame(ohlcv_frame)
    stub = _house_stub()
    out = adapter_populate_exit_trend(stub, frame, METADATA)
    signals = _house_signals(ohlcv_frame)
    np.testing.assert_array_equal(out["exit_long"].to_numpy(), signals["exit_long"].to_numpy())
    assert stub._entry_stops == {}


def test_adapter_methods_reject_an_unsupported_timeframe(ohlcv_frame: pd.DataFrame) -> None:
    """A bad timeframe fails early, in **every** method, with a strategy-level error."""
    frame = _freqtrade_frame(ohlcv_frame)
    stub = _house_stub(timeframe="3m")
    calls = (
        lambda: adapter_populate_indicators(stub, frame, METADATA),
        lambda: adapter_populate_entry_trend(stub, frame, METADATA),
        lambda: adapter_populate_exit_trend(stub, frame, METADATA),
        lambda: adapter_custom_stoploss(stub, "BTC/USDT", None, None, 100.0, 0.0, False),
    )
    for call in calls:
        with pytest.raises(StrategyError, match="unsupported timeframe for the Freqtrade adapter"):
            call()


def test_adapter_custom_stoploss_without_a_recorded_stop_returns_none() -> None:
    """No recorded stop means "let Freqtrade use its own ``stoploss`` attribute"."""
    stub = _house_stub()
    assert adapter_custom_stoploss(stub, "BTC/USDT", None, None, 100.0, 0.0, False) is None

    stub._entry_stops[("ETH/USDT", pd.Timestamp("2024-01-01T00:00:00Z"))] = 90.0
    assert adapter_custom_stoploss(stub, "BTC/USDT", None, None, 100.0, 0.0, False) is None


# ---------------------------------------------------------------------------
# the parameter bridge -- lazy resolution and re-validation
# ---------------------------------------------------------------------------


def test_house_strategy_is_resolved_lazily_and_cached() -> None:
    """The house instance follows the *current* parameter values, and is cached."""
    stub = _house_stub()
    first = _house_strategy(stub)
    assert first.name == "basic"
    assert first.params.ema_fast == 9
    assert _house_strategy(stub) is first

    stub.ema_fast.value = 13
    second = _house_strategy(stub)
    assert second is not first
    assert second.params.ema_fast == 13


def test_house_strategy_accepts_numpy_scalars() -> None:
    """Freqtrade/optuna values may be numpy scalars: they are unwrapped for pydantic."""
    stub = _house_stub(ema_fast=np.int64(13), rsi_max=np.float64(75.0))
    strategy = _house_strategy(stub)
    assert strategy.params.ema_fast == 13
    assert strategy.params.rsi_max == 75.0


def test_house_strategy_revalidates_the_house_model_at_use_time() -> None:
    """An invalid effective combination raises instead of trading silently."""
    stub = _house_stub(ema_fast=30)
    with pytest.raises(StrategyError) as excinfo:
        _house_strategy(stub)
    message = str(excinfo.value)
    assert "invalid parameters for strategy 'basic'" in message
    assert "ema_slow" in message


def test_house_strategy_keeps_the_house_defaults_of_unmapped_parameters() -> None:
    """Only mapped names travel to the house model; the others keep their defaults."""
    stub = _house_stub()
    strategy = _house_strategy(stub)
    assert strategy.params.atr_period == 14
    assert strategy.params.allow_short is False


def test_house_strategy_error_of_an_unknown_name_passes_through() -> None:
    """The registry message is not wrapped: it already lists the available names."""
    stub = _house_stub()
    stub.house_strategy_name = "nope"
    with pytest.raises(StrategyError) as excinfo:
        _house_strategy(stub)
    assert "unknown strategy: 'nope'" in str(excinfo.value)
    assert "available: " in str(excinfo.value)


# ---------------------------------------------------------------------------
# stop bookkeeping -- the short mirror
# ---------------------------------------------------------------------------


def test_record_entry_stops_mirrors_the_stop_of_a_short_entry() -> None:
    """A short-only candle stores the stop **price of the direction**.

    The house column is the long formula; the house engine applies
    ``fill + (close - stop_loss)`` above the fill price for a short, and the
    adapter reproduces it with the next candle's open as the fill.
    """
    index = pd.date_range("2024-01-01T00:00:00Z", periods=3, freq="h", name="timestamp")
    prepared = pd.DataFrame(
        {"open": [100.0, 110.0, 120.0], "close": [105.0, 115.0, 125.0]},
        index=index,
    )
    signals = pd.DataFrame(
        {
            "entry_long": [False, False, False],
            "exit_long": [False, False, False],
            "entry_short": [True, False, False],
            "exit_short": [False, False, False],
            "stop_loss": [100.0, float("nan"), 110.0],
        },
        index=index,
    )
    stub = _house_stub()
    _record_entry_stops(stub, signals, prepared, {"pair": "BTC/USDT"})

    # candle 0 is a short entry: mirrored about the fill (open of candle 1 = 110).
    assert stub._entry_stops[("BTC/USDT", index[0])] == 110.0 + (105.0 - 100.0)
    # candle 1 has no stop at all.
    assert ("BTC/USDT", index[1]) not in stub._entry_stops
    # candle 2 is not an entry: the raw column is kept as is.
    assert stub._entry_stops[("BTC/USDT", index[2])] == 110.0


def test_record_entry_stops_prefers_the_long_direction_like_the_engine() -> None:
    """A candle carrying both directions keeps the long value (engine precedence)."""
    index = pd.date_range("2024-01-01T00:00:00Z", periods=2, freq="h", name="timestamp")
    prepared = pd.DataFrame({"open": [100.0, 110.0], "close": [105.0, 115.0]}, index=index)
    signals = pd.DataFrame(
        {
            "entry_long": [True, False],
            "exit_long": [False, False],
            "entry_short": [True, False],
            "exit_short": [False, False],
            "stop_loss": [100.0, float("nan")],
        },
        index=index,
    )
    stub = _house_stub()
    _record_entry_stops(stub, signals, prepared, {"pair": "BTC/USDT"})
    assert stub._entry_stops[("BTC/USDT", index[0])] == 100.0


def test_record_entry_stops_falls_back_to_the_close_on_the_last_candle() -> None:
    """The fill of the last candle does not exist yet: the signal close is used."""
    index = pd.date_range("2024-01-01T00:00:00Z", periods=2, freq="h", name="timestamp")
    prepared = pd.DataFrame({"open": [100.0, 110.0], "close": [105.0, 115.0]}, index=index)
    signals = pd.DataFrame(
        {
            "entry_long": [False, False],
            "exit_long": [False, False],
            "entry_short": [False, True],
            "exit_short": [False, False],
            "stop_loss": [float("nan"), 112.0],
        },
        index=index,
    )
    stub = _house_stub()
    _record_entry_stops(stub, signals, prepared, {"pair": "BTC/USDT"})
    assert stub._entry_stops[("BTC/USDT", index[1])] == 115.0 + (115.0 - 112.0)


def test_record_entry_stops_accepts_an_empty_metadata() -> None:
    """A missing ``pair`` degrades to the empty string instead of raising."""
    index = pd.date_range("2024-01-01T00:00:00Z", periods=1, freq="h", name="timestamp")
    prepared = pd.DataFrame({"open": [100.0], "close": [105.0]}, index=index)
    signals = pd.DataFrame(
        {
            "entry_long": [False],
            "exit_long": [False],
            "entry_short": [False],
            "exit_short": [False],
            "stop_loss": [100.0],
        },
        index=index,
    )
    stub = _house_stub()
    _record_entry_stops(stub, signals, prepared, {})
    assert stub._entry_stops[("", index[0])] == 100.0


# ---------------------------------------------------------------------------
# make_freqtrade_strategy / freqtrade_strategy_namespace -- offline failures
# ---------------------------------------------------------------------------


def test_namespace_requires_timeframe_and_stoploss() -> None:
    """``timeframe`` and ``stoploss`` have no default: ``IStrategy`` declares none."""
    with pytest.raises(TypeError):
        freqtrade_strategy_namespace("basic")  # type: ignore[call-arg]


def test_unknown_strategy_name_lists_the_available_ones() -> None:
    """The registry error passes through unchanged (and needs no Freqtrade)."""
    with pytest.raises(StrategyError) as excinfo:
        make_freqtrade_strategy("nope")
    message = str(excinfo.value)
    assert "unknown strategy: 'nope'" in message
    assert "available: " in message
    assert "basic" in message


def test_unsupported_timeframe_is_rejected_before_importing_freqtrade() -> None:
    """Validation happens before the optional extra is needed."""
    with pytest.raises(StrategyError, match="unsupported timeframe for the Freqtrade adapter"):
        make_freqtrade_strategy("basic", timeframe="3m")
    with pytest.raises(StrategyError, match="unsupported timeframe for the Freqtrade adapter"):
        freqtrade_strategy_namespace("basic", timeframe="1w", stoploss=-0.5)


@pytest.mark.parametrize("stoploss", [0.0, 0.5, 1.0, -1.0, -1.5, -10.0])
def test_invalid_stoploss_is_rejected(stoploss: float) -> None:
    """``stoploss`` is a hard bound: a ratio in ``]-1, 0[`` and nothing else."""
    with pytest.raises(StrategyError, match="stoploss must be a ratio in"):
        make_freqtrade_strategy("basic", stoploss=stoploss)


def test_invalid_parameters_are_rejected_on_the_house_model() -> None:
    """``params`` goes through the house ``ParamsModel`` (cross-field rules included)."""
    with pytest.raises(StrategyError) as excinfo:
        make_freqtrade_strategy("basic", params={"ema_fast": 30, "ema_slow": 21})
    assert "invalid parameters for strategy 'basic'" in str(excinfo.value)


def test_default_class_name_matches_the_freqtrade_layer() -> None:
    """``basic`` -> ``BasicStrategy``, the name the Freqtrade configs declare."""
    assert default_class_name("basic") == "BasicStrategy"
    assert default_class_name("basic") == DEFAULT_STRATEGY_NAME
    assert default_class_name("ema_rsi_combo") == "EmaRsiComboStrategy"


# ---------------------------------------------------------------------------
# failure path -- no freqtrade installed (this is what CI runs)
# ---------------------------------------------------------------------------


def test_freqtrade_available_is_false_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """The availability probe answers ``False`` instead of raising."""
    _simulate_missing_freqtrade(monkeypatch)
    assert freqtrade_available() is False


def test_make_freqtrade_strategy_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a class without the extra raises the documented ``StrategyError``."""
    _simulate_missing_freqtrade(monkeypatch)
    with pytest.raises(StrategyError) as excinfo:
        make_freqtrade_strategy("basic")
    message = str(excinfo.value)
    assert "freqtrade is not installed" in message
    assert "trading-platform[freqtrade]" in message


def test_validate_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation reports the missing extra instead of leaking an ``ImportError``."""
    _simulate_missing_freqtrade(monkeypatch)
    with pytest.raises(StrategyError) as excinfo:
        validate_freqtrade_adapter_class(object)
    message = str(excinfo.value)
    assert "freqtrade is not installed" in message
    assert "trading-platform[freqtrade]" in message


def test_availability_is_restored_after_the_simulation() -> None:
    """The simulation of the previous tests left nothing behind."""
    assert freqtrade_available() is True or "freqtrade" not in sys.modules


# ---------------------------------------------------------------------------
# real Freqtrade interface (skipped where the optional extra is absent)
# ---------------------------------------------------------------------------


def test_generated_class_is_a_real_istrategy(ohlcv_frame: pd.DataFrame) -> None:
    """The produced class really is an ``IStrategy`` subclass, and it instantiates."""
    pytest.importorskip("freqtrade")
    from freqtrade.strategy import IStrategy

    cls = make_freqtrade_strategy("basic")
    assert cls.__name__ == "BasicStrategy"
    assert issubclass(cls, IStrategy)
    instance = cls({})
    assert isinstance(instance, IStrategy)
    assert instance.house_strategy_name == "basic"
    assert instance.house_frame.__func__ is house_frame


def test_generated_class_name_can_be_overridden() -> None:
    """``class_name`` wins over the derived name."""
    pytest.importorskip("freqtrade")
    cls = make_freqtrade_strategy("basic", class_name="MyOwnName")
    assert cls.__name__ == "MyOwnName"


def test_namespace_contains_the_frozen_attributes() -> None:
    """Every attribute Freqtrade reads is present, with the documented value."""
    pytest.importorskip("freqtrade")
    namespace = freqtrade_strategy_namespace("basic", timeframe="4h", stoploss=-0.5)
    assert namespace["INTERFACE_VERSION"] == 3
    assert namespace["timeframe"] == "4h"
    assert namespace["stoploss"] == -0.5
    assert namespace["minimal_roi"] == {"0": 100.0}
    assert namespace["minimal_roi"] is not FREQTRADE_DISABLED_ROI
    assert namespace["use_custom_stoploss"] is True
    assert namespace["use_exit_signal"] is True
    assert namespace["exit_profit_only"] is False
    assert namespace["ignore_roi_if_entry_signal"] is False
    assert namespace["process_only_new_candles"] is True
    assert namespace["can_short"] is False
    assert namespace["startup_candle_count"] == 21
    assert namespace["house_strategy_name"] == "basic"
    assert namespace["_house_parameter_names"] == (
        "ema_fast",
        "ema_slow",
        "rsi_period",
        "rsi_min",
        "rsi_max",
        "atr_period",
        "atr_stop_multiplier",
        "allow_short",
    )
    for name in (
        "populate_indicators",
        "populate_entry_trend",
        "populate_exit_trend",
        "custom_stoploss",
        "house_frame",
        "_house_strategy",
    ):
        assert callable(namespace[name])


def test_can_short_follows_the_house_parameter() -> None:
    """``can_short`` defaults to the house ``allow_short`` flag."""
    pytest.importorskip("freqtrade")
    assert make_freqtrade_strategy("basic", params={"allow_short": True}).can_short is True
    assert make_freqtrade_strategy("basic").can_short is False
    assert make_freqtrade_strategy("basic", can_short=True).can_short is True


def test_parameter_values_are_read_at_call_time(ohlcv_frame: pd.DataFrame) -> None:
    """The bridge reads ``<param>.value`` on every call, and revalidates it.

    Freqtrade applies its params JSON file at ``ft_bot_start()``, **after**
    ``__init__``: a bridge that captured the parameters in a constructor would
    silently ignore it.  The test mutates the class-level parameter objects the
    way Freqtrade does and checks that the *next* analysis uses the new values —
    and that an invalid combination raises instead of trading silently.
    """
    pytest.importorskip("freqtrade")
    cls = make_freqtrade_strategy("basic", class_name="LateParams")
    instance = cls({})
    frame = _freqtrade_frame(ohlcv_frame)
    before = instance.populate_indicators(frame, METADATA)

    cls.ema_slow.value = 55
    after = instance.populate_indicators(frame, METADATA)
    assert not after["ema_slow"].equals(before["ema_slow"])

    cls.ema_slow.value = 5  # ema_slow <= ema_fast: the house model forbids it
    with pytest.raises(StrategyError, match="invalid parameters for strategy 'basic'"):
        instance.populate_indicators(frame, METADATA)


def test_parameter_overrides_extend_the_hyperopt_space(
    registered_toggle: type[Strategy],
) -> None:
    """``param_overrides`` exposes an unmapped house field (and only bridges house fields)."""
    pytest.importorskip("freqtrade")
    from freqtrade.strategy import CategoricalParameter

    manual = {
        "label": FreqtradeParamSpec(
            name="label", kind="categorical", default="fast", categories=("fast", "slow")
        ),
        "extra": FreqtradeParamSpec(name="extra", kind="int", default=1, low=1, high=3),
    }
    cls = make_freqtrade_strategy("wp3_toggle", timeframe="1h", param_overrides=manual)
    assert isinstance(cls.label, CategoricalParameter)
    assert cls.label.value == "fast"
    assert "label" in cls._house_parameter_names  # declared by the house model: bridged
    assert "extra" not in cls._house_parameter_names  # Freqtrade-only: never bridged

    instance = cls({})
    cls.label.value = "slow"
    assert instance._house_strategy().params.label == "slow"


def test_startup_candle_count_can_be_overridden() -> None:
    """The warm-up window is derived by default and explicitly overridable."""
    pytest.importorskip("freqtrade")
    assert make_freqtrade_strategy("basic").startup_candle_count == 21
    assert make_freqtrade_strategy("basic", startup_candle_count=200).startup_candle_count == 200


def test_parameter_attributes_are_real_freqtrade_parameters() -> None:
    """The pydantic bridge really produced native Freqtrade parameter objects."""
    pytest.importorskip("freqtrade")
    from freqtrade.strategy import (
        BooleanParameter,
        CategoricalParameter,
        DecimalParameter,
        IntParameter,
    )

    cls = make_freqtrade_strategy("basic")
    assert isinstance(cls.rsi_period, IntParameter)
    assert isinstance(cls.rsi_min, DecimalParameter)
    assert isinstance(cls.allow_short, BooleanParameter)
    # ``basic`` declares a grid for these two, so they are hyperoptable categories.
    assert isinstance(cls.ema_fast, CategoricalParameter)
    assert cls.ema_fast.value == 9
    assert list(cls.ema_fast.opt_range) == [5, 9, 13]
    assert cls.rsi_period.value == 14
    assert cls.allow_short.value is False


def test_validate_accepts_the_generated_class() -> None:
    """The factory output passes the contract check."""
    pytest.importorskip("freqtrade")
    assert validate_freqtrade_adapter_class(make_freqtrade_strategy("basic")) is None


def test_validate_rejects_a_plain_istrategy_subclass() -> None:
    """Every violated rule is reported at once."""
    pytest.importorskip("freqtrade")
    from freqtrade.strategy import IStrategy

    class Plain(IStrategy):
        """A bare ``IStrategy`` subclass: nothing of the adapter contract is honoured."""

    with pytest.raises(StrategyError) as excinfo:
        validate_freqtrade_adapter_class(Plain)
    assert "Plain is not a valid Freqtrade adapter class" in str(excinfo.value)
    issues = "\n".join(excinfo.value.issues)
    assert "not overridden" in issues
    assert "timeframe must be a supported timeframe" in issues
    assert "stoploss must be a real ratio" in issues
    assert "use_custom_stoploss must be True" in issues
    assert "use_exit_signal must be a bool" in issues
    assert "house_strategy_name" in issues


@pytest.mark.parametrize(
    ("attribute", "value", "expected"),
    [
        ("INTERFACE_VERSION", 2, "INTERFACE_VERSION must be 3"),
        ("timeframe", "3m", "timeframe must be a supported timeframe"),
        ("stoploss", 0.5, "stoploss must be a real ratio"),
        ("stoploss", -1.5, "stoploss must be a real ratio"),
        ("use_custom_stoploss", False, "use_custom_stoploss must be True"),
        ("use_exit_signal", "yes", "use_exit_signal must be a bool"),
        ("house_strategy_name", "nope", "house_strategy_name 'nope' is not registered"),
    ],
)
def test_validate_rejects_each_broken_rule(attribute: str, value: Any, expected: str) -> None:
    """Each rule of the contract is exercised on its own."""
    pytest.importorskip("freqtrade")
    cls = make_freqtrade_strategy("basic", class_name="BrokenStrategy")
    setattr(cls, attribute, value)
    with pytest.raises(StrategyError) as excinfo:
        validate_freqtrade_adapter_class(cls)
    assert expected in "\n".join(excinfo.value.issues)


def test_validate_rejects_a_wrong_populate_signature() -> None:
    """The three ``populate_*`` must keep the ``(self, dataframe, metadata)`` shape."""
    pytest.importorskip("freqtrade")
    cls = make_freqtrade_strategy("basic", class_name="WrongArity")

    def populate_indicators(self: Any, dataframe: pd.DataFrame) -> pd.DataFrame:
        return dataframe

    cls.populate_indicators = populate_indicators
    with pytest.raises(StrategyError) as excinfo:
        validate_freqtrade_adapter_class(cls)
    assert "populate_indicators must take (self, dataframe, metadata)" in "\n".join(
        excinfo.value.issues
    )


def test_validate_rejects_a_non_class() -> None:
    """The helper takes a class; anything else is reported, not crashed on."""
    pytest.importorskip("freqtrade")
    with pytest.raises(StrategyError, match="expects a class"):
        validate_freqtrade_adapter_class(object())  # type: ignore[arg-type]


def test_validate_reports_every_missing_rule_at_once() -> None:
    """A candidate carrying nothing is not an ``IStrategy`` and misses all three methods."""
    pytest.importorskip("freqtrade")
    bare = type(
        "Bare",
        (),
        {
            "INTERFACE_VERSION": FREQTRADE_INTERFACE_VERSION,
            "timeframe": "1h",
            "stoploss": -0.5,
            "use_custom_stoploss": True,
            "use_exit_signal": True,
            "house_strategy_name": "basic",
        },
    )
    with pytest.raises(StrategyError) as excinfo:
        validate_freqtrade_adapter_class(bare)
    issues = "\n".join(excinfo.value.issues)
    assert "Bare is not a subclass of freqtrade.strategy.IStrategy" in issues
    assert issues.count("is missing") == 3


def test_adapter_is_generic_over_the_registry(
    registered_toggle: type[Strategy], ohlcv_frame: pd.DataFrame
) -> None:
    """A second, locally registered strategy is exposed without a single line of glue."""
    pytest.importorskip("freqtrade")
    cls = make_freqtrade_strategy("wp3_toggle", timeframe="1h")
    assert cls.__name__ == "Wp3ToggleStrategy"
    assert cls.house_strategy_name == "wp3_toggle"
    assert cls.can_short is False
    assert validate_freqtrade_adapter_class(cls) is None

    instance = cls({})
    frame = _freqtrade_frame(ohlcv_frame)
    indicators = instance.populate_indicators(frame, METADATA)
    entries = instance.populate_entry_trend(indicators, METADATA)
    exits = instance.populate_exit_trend(entries, METADATA)
    signals = _house_signals(ohlcv_frame, "wp3_toggle")
    assert "mean" in indicators.columns
    np.testing.assert_array_equal(
        entries["enter_long"].to_numpy(), signals["entry_long"].to_numpy()
    )
    np.testing.assert_array_equal(exits["exit_long"].to_numpy(), signals["exit_long"].to_numpy())


def test_a_clashing_parameter_name_is_not_injected(
    registered_toggle: type[Strategy],
) -> None:
    """Limit (i): a house parameter named like an adapter attribute is skipped."""
    pytest.importorskip("freqtrade")
    namespace = freqtrade_strategy_namespace("wp3_clashing", timeframe="1h", stoploss=-0.5)
    assert namespace["timeframe"] == "1h"
    assert "window" in namespace["_house_parameter_names"]
    assert "timeframe" not in namespace["_house_parameter_names"]
    cls = make_freqtrade_strategy("wp3_clashing", timeframe="1h")
    assert validate_freqtrade_adapter_class(cls) is None


def test_adapter_chain_passes_the_real_strategy_result_validator(
    ohlcv_frame: pd.DataFrame,
) -> None:
    """The three ``populate_*`` satisfy Freqtrade's own frame validator."""
    pytest.importorskip("freqtrade")
    from freqtrade.strategy.strategy_validation import StrategyResultValidator

    frame = _freqtrade_frame(ohlcv_frame)
    instance = make_freqtrade_strategy("basic")({})
    validator = StrategyResultValidator(frame)
    out = instance.populate_indicators(frame, METADATA)
    out = instance.populate_entry_trend(out, METADATA)
    out = instance.populate_exit_trend(out, METADATA)
    validator.assert_df(out)
    assert "enter_tag" not in out.columns
    assert {"enter_long", "enter_short", "exit_long", "exit_short"} <= set(out.columns)


def test_custom_stoploss_returns_the_ratio_of_the_recorded_stop(
    ohlcv_frame: pd.DataFrame,
) -> None:
    """The captured absolute stop is converted on every call, for both directions."""
    pytest.importorskip("freqtrade")
    frame = _freqtrade_frame(ohlcv_frame)
    instance = make_freqtrade_strategy("basic")({})
    entries = instance.populate_entry_trend(instance.populate_indicators(frame, METADATA), METADATA)
    signals = _house_signals(ohlcv_frame)

    candle = signals.index[signals["entry_long"].to_numpy()][0]
    fill = candle + pd.Timedelta("1h")
    trade = SimpleNamespace(open_date_utc=fill, is_short=False)
    current_rate = float(ohlcv_frame.at[fill, "close"])
    ratio = instance.custom_stoploss("BTC/USDT", trade, None, current_rate, 0.0, False)
    assert ratio is not None
    assert ratio == pytest.approx(
        1 - float(signals.at[candle, "stop_loss"]) / current_rate, abs=1e-12
    )
    # A pair with nothing recorded falls back to Freqtrade's own stoploss.
    assert instance.custom_stoploss("ETH/USDT", trade, None, current_rate, 0.0, False) is None
    assert entries["stop_loss"].notna().sum() == len(instance._entry_stops)


def test_adapter_on_the_real_btc_cache_matches_the_house_signals() -> None:
    """Integration: the adapter renders **exactly** the house signals on real data.

    Cross-check of the two engines on ``data/cache/binance/BTC_USDT/1h.parquet``
    (17543 candles).  With the shipped defaults the house strategy produces 395
    ``entry_long`` and 400 ``exit_long`` signals; the adapter must produce the
    same count under its Freqtrade names (``enter_long`` / ``exit_long``).  This
    equality — candle by candle, not just in total — is the proof that the
    ``entry_*`` -> ``enter_*`` translation is correct.  The returns of the two
    backtests are a different matter entirely (``docs/architecture.md`` §4.9.4).
    """
    pytest.importorskip("freqtrade")
    if not CACHE_PATH.exists():
        pytest.skip(f"real market data cache is absent: {CACHE_PATH}")
    from freqtrade.strategy.strategy_validation import StrategyResultValidator

    data = pd.read_parquet(CACHE_PATH)
    frame = _freqtrade_frame(data)
    instance = make_freqtrade_strategy("basic")({})
    validator = StrategyResultValidator(frame)
    out = instance.populate_indicators(frame, METADATA)
    out = instance.populate_entry_trend(out, METADATA)
    out = instance.populate_exit_trend(out, METADATA)
    validator.assert_df(out)

    signals = _house_signals(data)
    np.testing.assert_array_equal(out["enter_long"].to_numpy(), signals["entry_long"].to_numpy())
    np.testing.assert_array_equal(out["enter_short"].to_numpy(), signals["entry_short"].to_numpy())
    np.testing.assert_array_equal(out["exit_long"].to_numpy(), signals["exit_long"].to_numpy())
    np.testing.assert_array_equal(out["exit_short"].to_numpy(), signals["exit_short"].to_numpy())
    np.testing.assert_allclose(
        out["stop_loss"].to_numpy(), signals["stop_loss"].to_numpy(), equal_nan=True
    )

    entries = int(out["enter_long"].sum())
    exits = int(out["exit_long"].sum())
    assert entries == int(signals["entry_long"].sum())
    assert exits == int(signals["exit_long"].sum())
    assert entries > 0
    assert exits > 0
    assert len(instance._entry_stops) == int(signals["stop_loss"].notna().sum())
    assert "entry_long" not in out.columns
