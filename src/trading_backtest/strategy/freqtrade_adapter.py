"""Freqtrade ``IStrategy`` adapter: **translate** the house contract, never duplicate it.

**Layer 3** (``trading_backtest.strategy``).  The dependency rule of
``docs/architecture.md`` §3 is non-negotiable: *imports only ever point downwards*
(``core`` <- ``config``/``data``/``freqtrade`` <- ``strategy``/``metrics`` <-
``reporting`` <- ``validation`` <- ``cli``).  This module therefore lives in
``strategy`` and not in ``trading_backtest.freqtrade``: ``strategy ->
freqtrade`` is a **downward** import (layer 3 -> layer 2) and is legal, whereas
moving the adapter into ``trading_backtest.freqtrade`` would be an *upward*
import (layer 2 -> layer 3) and would falsify the docstring of that package
(which states it never imports anything but ``core``).  No edge is added to the
graph by this module.  ``freqtrade`` itself is imported **lazily, inside
functions** — never at module level — so the whole package (and its whole test
suite) keeps working with the ``.[dev]`` extra only, without Freqtrade installed.

What this module does
---------------------
A house strategy is written **once**: indicators and entry/exit rules live in
:mod:`trading_backtest.strategy`, and its parameters are validated by a pydantic
``ParamsModel``.  Freqtrade knows none of that: it wants an
``IStrategy`` subclass exposing three ``populate_*`` methods, hyperoptable
parameter *objects* as class attributes and a signal vocabulary of its own.  This
module is the translation seam:

* :func:`house_frame` turns a Freqtrade frame (positional ``RangeIndex`` plus a
  ``date`` column) into the house OHLCV contract (``DatetimeIndex``);
* :func:`freqtrade_indicator_frame` / :func:`freqtrade_entry_frame` /
  :func:`freqtrade_exit_frame` do the opposite: they return a **copy of the
  incoming frame** with the house columns appended **positionally**;
* :func:`freqtrade_strategy_namespace` builds the class namespace and
  :func:`make_freqtrade_strategy` assembles the concrete ``IStrategy`` subclass;
* :func:`validate_freqtrade_adapter_class` proves that the produced class really
  honours the Freqtrade contract (every violated rule is reported at once).

The three methods of the generated class are thin wrappers
(:func:`adapter_populate_indicators`, :func:`adapter_populate_entry_trend`,
:func:`adapter_populate_exit_trend`, plus
:func:`adapter_custom_stoploss`); **no indicator and no entry/exit rule is
recopied here** — they all come from the house ``Strategy.prepare`` and
``Strategy.signals``.

The three contract differences, and how each one is settled
-----------------------------------------------------------
(a) **Signal columns — ``entry_*`` vs ``enter_*``.**  The house speaks
    ``entry_long`` / ``exit_long`` / ``entry_short`` / ``exit_short``; Freqtrade
    reads ``enter_long`` / ``exit_long`` / ``enter_short`` / ``exit_short``.
    ``entry_long`` silently does *nothing* on the Freqtrade side: it is the
    classic silent bug of this contract.  The rename is a single source of truth,
    :data:`SIGNAL_TO_FREQTRADE_COLUMNS`, and the translation is pinned by tests
    that assert the ``entry_*`` names are **absent** from the produced frames.
(b) **Parameters — pydantic models vs native Freqtrade parameters.**  Delegated
    to :mod:`trading_backtest.strategy.freqtrade_parameters`; this module reads
    the grid through the strategy *instance* (``strategy.param_space()``) and
    never hard-codes a strategy name.
(c) **Stop-loss — a per-candle column vs one global attribute.**  Freqtrade has
    no per-candle stop column, so the decision is: the global ``stoploss``
    attribute is set to :data:`~trading_backtest.strategy.freqtrade_stoploss.DEFAULT_FREQTRADE_STOPLOSS`
    (``-0.99``) as a *hard bound that is never the used stop*, the absolute stop
    of a trade is captured on its **signal candle** into ``_entry_stops`` and
    replayed through ``custom_stoploss()`` (``use_custom_stoploss = True``).  The
    house column is still carried into the Freqtrade frame under
    :data:`~trading_backtest.strategy.freqtrade_stoploss.FREQTRADE_STOPLOSS_COLUMN`
    **for audit only** — Freqtrade ignores it.  The rejected alternative (mapping
    the column onto a single static ratio) is on record in the docstring of
    :mod:`trading_backtest.strategy.freqtrade_stoploss`.

Writing a strategy once, exposing it twice
------------------------------------------
1. write the house strategy (``prepare`` + ``signals`` + ``ParamsModel``);
2. register it with ``@register_strategy``;
3. validate it with the house engine (backtest, walk-forward, robustness, …);
4. expose it to Freqtrade with **one line**, valid for **any** registered name:

   .. code-block:: python

      MyFreqtradeStrategy = make_freqtrade_strategy("my_strategy")

The parameter bridge is resolved at **call** time, never in ``__init__``:
Freqtrade applies its own params JSON file at ``ft_bot_start()``, *after*
``__init__``, so reading ``<param>.value`` in a constructor would silently ignore
it.  Every ``populate_*`` call therefore rebuilds ``tuple(sorted((name,
self.<name>.value)))``, caches the house instance on that tuple (``_house_cache``)
and rebuilds it through ``get_strategy(name, values)`` — which re-runs the house
``ParamsModel`` (cross-field ``@model_validator`` included).  An invalid
hyperopt/params-file combination therefore raises ``StrategyError`` at use time
instead of trading silently.

Two backtests, one signal contract — the execution model gap
------------------------------------------------------------
**The two backtests do not produce the same numbers, and that is expected.**
What is shared is the *signal* contract, and it is shared exactly:

* house engine (``strategy.engine``): signals are evaluated **at the close of
  candle ``t``** and filled **at the open of candle ``t + 1``**;
* Freqtrade 2026.8: ``Backtesting._get_ohlcv_as_lists`` shifts the signal columns
  by one candle and the loop enters at ``row[OPEN_IDX]`` — the same convention.

Everything else differs: order types (limit vs "open + slippage"), fees,
take-profit (the house engine never emits ``TAKE_PROFIT``, so the adapter
disables Freqtrade's ROI with ``minimal_roi = FREQTRADE_DISABLED_ROI``),
stop-loss execution, trade counts, ``startup_candle_count`` warm-up, unfilled
order timeouts, exchange precision, exit priority order and end-of-data handling.
The full comparison is written down in ``docs/architecture.md`` §4.9.4: a
divergence between the two engines is **not** a bug; what must coincide is the
signal columns, candle by candle (that is what the integration test checks).

Documented limits (this module only)
------------------------------------
(i) **A house parameter whose name collides with one of the adapter's own class
    attributes** (``timeframe``, ``stoploss``, ``can_short``, the six methods,
    ``_house_cache``, …) is **not injected**: it would silently break the
    Freqtrade contract (``IStrategy.__init__`` calls
    ``timeframe_to_minutes(self.timeframe)``).  Such a parameter keeps its house
    default and stays out of the hyperopt space; rename it in the house model to
    expose it.
(ii) **``startup_candle_count`` is derived, not measured** — it is the largest
    integer parameter of the model (``ema_slow``, hence ``21``, for ``basic``),
    which is an upper bound of the house look-back, not a proof.
(iii) **The stop of a *short* trade is mirrored at frame-translation time.**  The
    house engine anchors the mirrored distance on the **fill** price; the adapter
    uses the next candle's open (the fill candle of the house engine) and falls
    back to the signal close on the last candle, where the fill does not exist
    yet.  The distance is therefore right, the anchor is right in backtest and
    approximated on the live last candle.
(iv) **The values of the parameters are read at every analysis cycle**, so a
    hyperopt run that samples parameters while indicators are computed only once
    per epoch sees the *static* start values — Freqtrade itself warns about this
    (``use --analyze-per-epoch``).  Nothing is cached across cycles except the
    house instance keyed on the effective values.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Mapping
from typing import Any, cast

import numpy as np
import pandas as pd

from trading_backtest.core.constants import DEFAULT_TIMEFRAME, SUPPORTED_TIMEFRAMES
from trading_backtest.core.errors import StrategyError
from trading_backtest.strategy.base import Strategy
from trading_backtest.strategy.freqtrade_parameters import (
    FreqtradeParamSpec,
    freqtrade_param_specs,
    freqtrade_parameter_attributes,
    startup_candle_count_for,
)
from trading_backtest.strategy.freqtrade_stoploss import (
    DEFAULT_FREQTRADE_STOPLOSS,
    FREQTRADE_STOPLOSS_COLUMN,
    entry_stop_map,
    lookup_entry_stop,
    stoploss_ratio_from_absolute,
)
from trading_backtest.strategy.registry import STRATEGIES, get_strategy

__all__ = [
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

#: ``INTERFACE_VERSION`` declared by the generated class (verified against
#: Freqtrade 2026.8: version 3 is the first version with short support and the
#: ``metadata`` argument of the ``populate_*`` methods).
FREQTRADE_INTERFACE_VERSION: int = 3

#: The signal columns Freqtrade actually reads, in its own spelling.  Note the
#: ``enter_*`` prefix: ``entry_long`` does **not** exist on the Freqtrade side.
FREQTRADE_ORDER_COLUMNS: tuple[str, ...] = ("enter_long", "exit_long", "enter_short", "exit_short")

#: Minimal ROI table handed to Freqtrade.  A ROI of 100 at minute 0 is
#: unreachable, which *disables* the ROI exit: the house engine never produces
#: ``TAKE_PROFIT`` (see :class:`~trading_backtest.core.models.ExitReason`), so
#: letting Freqtrade take profits would make the two executions diverge for a
#: reason no house rule justifies.
FREQTRADE_DISABLED_ROI: dict[str, float] = {"0": 100.0}

#: House signal column -> Freqtrade signal column.  The ``entry_*`` -> ``enter_*``
#: rename is the single source of truth of the translation (and the classic
#: silent-bug source of this contract).
SIGNAL_TO_FREQTRADE_COLUMNS: dict[str, str] = {
    "entry_long": "enter_long",
    "exit_long": "exit_long",
    "entry_short": "enter_short",
    "exit_short": "exit_short",
}

#: Error message raised when the optional ``freqtrade`` extra is missing.
_FREQTRADE_MISSING = (
    "freqtrade is not installed: install the optional extra with "
    "pip install 'trading-backtest[freqtrade]'"
)

#: The three ``populate_*`` methods Freqtrade calls (interface version 3), with
#: the exact signature ``(self, dataframe, metadata)``.
_POPULATE_METHODS: tuple[str, ...] = (
    "populate_indicators",
    "populate_entry_trend",
    "populate_exit_trend",
)

#: Instance attribute holding the lazily-created ``{(pair, candle): stop}`` map.
_ENTRY_STOPS_ATTRIBUTE = "_entry_stops"

#: Instance attribute holding the ``(values, Strategy)`` cache of the house instance.
_HOUSE_CACHE_ATTRIBUTE = "_house_cache"

#: Attributes of the generated class that a house parameter must never shadow:
#: they *are* the Freqtrade contract (``IStrategy.__init__`` reads ``timeframe``,
#: Freqtrade reads ``stoploss``/``can_short``/``minimal_roi``/… and calls the six
#: methods).  A colliding parameter is skipped — see limit (i) of the module
#: docstring.
_PROTECTED_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "INTERFACE_VERSION",
        "can_short",
        "custom_stoploss",
        "exit_profit_only",
        "house_frame",
        "house_strategy_name",
        "ignore_roi_if_entry_signal",
        "minimal_roi",
        "process_only_new_candles",
        "startup_candle_count",
        "stoploss",
        "timeframe",
        "use_custom_stoploss",
        "use_exit_signal",
        *_POPULATE_METHODS,
        _ENTRY_STOPS_ATTRIBUTE,
        _HOUSE_CACHE_ATTRIBUTE,
        "_house_parameter_names",
        "_house_strategy",
    }
)


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------


def freqtrade_available() -> bool:
    """Return whether the optional ``freqtrade`` extra is importable.

    The import happens **inside** this function (never at module level), so a
    checkout with the ``.[dev]`` extra only can still import — and test — the
    whole package: this function then simply returns ``False``.

    Examples
    --------
    >>> freqtrade_available() in (True, False)
    True
    """
    try:
        importlib.import_module("freqtrade")
    except ImportError:
        return False
    return True


def _istrategy_base() -> Any:
    """Return the real ``freqtrade.strategy.IStrategy`` class.

    Returns
    -------
    Any
        The Freqtrade base class.  It is typed ``Any`` on purpose: Freqtrade
        ships no ``py.typed``, so ``mypy --strict`` would refuse to subclass it
        (``cast("type[Any]", type(...))`` in :func:`make_freqtrade_strategy` is
        the equivalent of a forbidden ignore comment).

    Raises
    ------
    StrategyError
        If the optional ``freqtrade`` extra is not installed; the ``ImportError``
        is never leaked to the caller.
    """
    try:
        from freqtrade.strategy import IStrategy
    except ImportError as error:
        raise StrategyError(_FREQTRADE_MISSING) from error
    return IStrategy


# ---------------------------------------------------------------------------
# frame translation: Freqtrade -> house
# ---------------------------------------------------------------------------


def house_frame(dataframe: pd.DataFrame) -> pd.DataFrame:
    """Return ``dataframe`` in the house OHLCV contract (a ``DatetimeIndex``).

    Freqtrade hands the ``populate_*`` methods a frame with a **positional**
    ``RangeIndex`` and the timestamps in a **``date`` column**; the house contract
    (``require_ohlcv_frame``) rejects that frame outright.  Two cases, and only
    two:

    * the frame already carries a :class:`pandas.DatetimeIndex` — it is returned
      **unchanged** (the *name* of the index does not matter: the house accepts
      ``timestamp`` as well as ``date``);
    * otherwise the ``date`` column becomes the index
      (``dataframe.set_index("date")``).

    Parameters
    ----------
    dataframe:
        A frame coming from Freqtrade (or any frame of the same shape).

    Returns
    -------
    pandas.DataFrame
        The frame in the house contract — the *same object* in the
        ``DatetimeIndex`` case, a new object in the ``date`` column case.  No
        column is dropped and no row is reordered.

    Raises
    ------
    StrategyError
        If the frame has neither a :class:`pandas.DatetimeIndex` nor a ``date``
        column: it cannot be translated, and guessing an index would silently
        misalign every signal.
    """
    if isinstance(dataframe.index, pd.DatetimeIndex):
        return dataframe
    if "date" in dataframe.columns:
        return dataframe.set_index("date")
    raise StrategyError(
        "freqtrade dataframe has neither a DatetimeIndex nor a 'date' column",
        [
            f"index type: {type(dataframe.index).__name__}",
            f"columns: {', '.join(map(str, dataframe.columns)) or '<none>'}",
        ],
    )


# ---------------------------------------------------------------------------
# frame translation: house -> Freqtrade
# ---------------------------------------------------------------------------


def freqtrade_indicator_frame(dataframe: pd.DataFrame, strategy: Strategy) -> pd.DataFrame:
    """Return a copy of ``dataframe`` with the house indicator columns appended.

    ``strategy.prepare(house_frame(dataframe))`` computes the indicators; every
    column of that prepared frame which is **not already** in the incoming frame
    is appended **positionally** (``out[column] = prepared[column].to_numpy()``),
    so the returned frame keeps ``dataframe``'s exact index, order and columns.

    Parameters
    ----------
    dataframe:
        The frame Freqtrade passed to ``populate_indicators``.
    strategy:
        The house strategy producing the indicators.

    Returns
    -------
    pandas.DataFrame
        A new frame: the incoming columns (never overwritten) plus the missing
        indicator columns, same length.  ``dataframe`` is never mutated —
        Freqtrade's ``StrategyResultValidator.assert_df`` requires the returned
        frame to keep the same length, the same last ``close`` and the same
        ``date`` column (verified in ``freqtrade/strategy/strategy_validation.py``).
    """
    prepared = strategy.prepare(house_frame(dataframe))
    out = dataframe.copy()
    for column in prepared.columns:
        if column not in out.columns:
            out[column] = prepared[column].to_numpy()
    return out


def freqtrade_entry_frame(dataframe: pd.DataFrame, strategy: Strategy) -> pd.DataFrame:
    """Return a copy of ``dataframe`` with the Freqtrade **entry** signal columns.

    The house signal frame is ``strategy.signals(strategy.prepare(house_frame(dataframe)))``.
    Its columns are translated with :data:`SIGNAL_TO_FREQTRADE_COLUMNS`:

    ======================  ==========================
    house column            Freqtrade column
    ======================  ==========================
    ``entry_long``          ``enter_long`` (renamed!)
    ``entry_short``         ``enter_short`` (renamed!)
    ``stop_loss``           ``stop_loss`` (audit only)
    ======================  ==========================

    ``enter_tag`` — Freqtrade's own column — is deliberately left untouched.

    Parameters
    ----------
    dataframe:
        The frame Freqtrade passed to ``populate_entry_trend``.
    strategy:
        The house strategy producing the signals.

    Returns
    -------
    pandas.DataFrame
        A new frame with ``enter_long`` / ``enter_short`` as ``bool`` and
        ``stop_loss`` as ``float64`` (``NaN`` = "no stop"), positionally aligned
        with the incoming frame.  ``entry_long`` / ``entry_short`` are **never**
        present in the result: Freqtrade does not read them.
    """
    signals = strategy.signals(strategy.prepare(house_frame(dataframe)))
    out = dataframe.copy()
    out[SIGNAL_TO_FREQTRADE_COLUMNS["entry_long"]] = signals["entry_long"].to_numpy(bool)
    out[SIGNAL_TO_FREQTRADE_COLUMNS["entry_short"]] = signals["entry_short"].to_numpy(bool)
    out[FREQTRADE_STOPLOSS_COLUMN] = signals[FREQTRADE_STOPLOSS_COLUMN].to_numpy("float64")
    return out


def freqtrade_exit_frame(dataframe: pd.DataFrame, strategy: Strategy) -> pd.DataFrame:
    """Return a copy of ``dataframe`` with the Freqtrade **exit** signal columns.

    Mirror image of :func:`freqtrade_entry_frame` for the exits: ``exit_long``
    keeps its name, ``exit_short`` keeps its name, and no stop column is added
    (the exits carry no stop).

    Parameters
    ----------
    dataframe:
        The frame Freqtrade passed to ``populate_exit_trend``.
    strategy:
        The house strategy producing the signals.

    Returns
    -------
    pandas.DataFrame
        A new frame with ``exit_long`` / ``exit_short`` as ``bool``, positionally
        aligned with the incoming frame, which is never mutated.
    """
    signals = strategy.signals(strategy.prepare(house_frame(dataframe)))
    out = dataframe.copy()
    out[SIGNAL_TO_FREQTRADE_COLUMNS["exit_long"]] = signals["exit_long"].to_numpy(bool)
    out[SIGNAL_TO_FREQTRADE_COLUMNS["exit_short"]] = signals["exit_short"].to_numpy(bool)
    return out


# ---------------------------------------------------------------------------
# internal helpers -- numeric / timeframe guards
# ---------------------------------------------------------------------------


def _plain_value(value: Any) -> Any:
    """Return a plain Python scalar for a numpy scalar (pydantic rejects the latter)."""
    if isinstance(value, np.generic):
        return value.item()
    return value


def _checked_timeframe(value: Any) -> str:
    """Return ``value`` if it is a supported timeframe, raise :class:`StrategyError` otherwise.

    This guard is applied by **every** adapter method: a Freqtrade configuration
    overriding ``timeframe`` with an unsupported value must fail here with a
    strategy-level error, not later with a bare ``ConfigError`` raised by
    ``candle_delta`` (deep inside the stop-loss lookup).
    """
    if not isinstance(value, str) or value not in SUPPORTED_TIMEFRAMES:
        raise StrategyError(
            f"unsupported timeframe for the Freqtrade adapter: {value!r}",
            [f"supported timeframes: {', '.join(sorted(SUPPORTED_TIMEFRAMES))}"],
        )
    return value


def _checked_stoploss(value: Any) -> float:
    """Return ``value`` as a float ratio in ``]-1, 0[``, raise :class:`StrategyError` otherwise."""
    valid = (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and -1.0 < float(value) < 0.0
    )
    if not valid:
        raise StrategyError(
            f"stoploss must be a ratio in ]-1, 0[, got {value!r}",
            ["the global stoploss is a hard bound, not the used stop (see freqtrade_stoploss)"],
        )
    return float(value)


def _validated_timeframe(self: Any) -> str:
    """Return the (validated) ``timeframe`` attribute of a generated adapter instance."""
    return _checked_timeframe(getattr(self, "timeframe", None))


# ---------------------------------------------------------------------------
# internal helpers -- house strategy bridge
# ---------------------------------------------------------------------------


def _instance_stops(self: Any) -> dict[tuple[str, Any], float]:
    """Return the per-instance ``{(pair, signal candle): absolute stop}`` map.

    The map is created on first use in ``self.__dict__`` so that two instances of
    the same generated class never share one: the class-level default is an
    immutable-by-convention empty dict that is never written to.
    """
    stops = self.__dict__.get(_ENTRY_STOPS_ATTRIBUTE)
    if not isinstance(stops, dict):
        stops = {}
        setattr(self, _ENTRY_STOPS_ATTRIBUTE, stops)
    return cast("dict[tuple[str, Any], float]", stops)


def _house_strategy(self: Any) -> Strategy:
    """Return the house :class:`Strategy` instance of the current parameter values.

    The parameters are read **at call time** from the Freqtrade parameter objects
    (``tuple(sorted((name, self.<name>.value)))``): Freqtrade applies a params
    JSON file at ``ft_bot_start()``, *after* ``__init__``, so a constructor-time
    read would silently ignore it.  The instance is cached on that value tuple
    (``_house_cache``) and rebuilt through :func:`~trading_backtest.strategy.registry.get_strategy`,
    which re-validates the effective values through the house ``ParamsModel``
    (cross-field invariants included): an invalid hyperopt/params-file combination
    raises :class:`StrategyError` here instead of trading silently.

    Non-mapped parameters (see :mod:`trading_backtest.strategy.freqtrade_parameters`)
    keep their house defaults.
    """
    names: tuple[str, ...] = tuple(getattr(self, "_house_parameter_names", ()))
    values = tuple(sorted((name, _plain_value(getattr(self, name).value)) for name in names))
    cached = self.__dict__.get(_HOUSE_CACHE_ATTRIBUTE)
    if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == values:
        return cast("Strategy", cached[1])
    strategy = get_strategy(str(getattr(self, "house_strategy_name", "")), dict(values))
    setattr(self, _HOUSE_CACHE_ATTRIBUTE, (values, strategy))
    return strategy


def _directional_signals(signals: pd.DataFrame, prepared: pd.DataFrame) -> pd.DataFrame:
    """Return ``signals`` with the stop column expressed in the *entry direction*.

    The house ``stop_loss`` column is the **long** formula.  The house engine reads
    it as a mirror for a short entry: the distance ``close(t) - stop_loss(t)`` is
    applied **above** the fill price, i.e. the short stop is
    ``fill + (close(t) - stop_loss(t))`` (see :mod:`trading_backtest.strategy.engine`).
    ``custom_stoploss`` receives a *price* of the direction, so the column has to
    be mirrored before it is mapped (this is what
    :mod:`trading_backtest.strategy.freqtrade_stoploss` prescribes).

    The fill price of candle ``t`` is the open of candle ``t + 1`` in both engines;
    on the last candle it does not exist yet, so the signal close is used as the
    fallback anchor (limit (iii) of the module docstring).  A candle carrying both
    an entry long and an entry short keeps the long value, exactly like the house
    engine (``entry_long`` wins).

    The input frames are never mutated: the result is a copy, and ``signals``
    itself is returned unchanged when no candle carries a short-only entry.
    """
    raw = signals[FREQTRADE_STOPLOSS_COLUMN].to_numpy(dtype="float64")
    short_entry = signals["entry_short"].to_numpy(dtype=bool) & ~signals["entry_long"].to_numpy(
        dtype=bool
    )
    if not bool(short_entry.any()):
        return signals
    close = prepared["close"].to_numpy(dtype="float64")
    opens = prepared["open"].to_numpy(dtype="float64")
    anchors = np.concatenate((opens[1:], close[-1:])) if close.size else close
    mirrored = np.where(short_entry, anchors + (close - raw), raw)
    out = signals.copy()
    out[FREQTRADE_STOPLOSS_COLUMN] = mirrored
    return out


def _record_entry_stops(
    self: Any,
    signals: pd.DataFrame,
    prepared: pd.DataFrame,
    metadata: Mapping[str, Any],
) -> None:
    """Record the absolute stop of every candle of ``signals`` into ``_entry_stops``.

    One ``{(pair, signal candle): stop}`` entry per candle whose stop is not
    ``NaN`` (the ``NaN`` filter is :func:`~trading_backtest.strategy.freqtrade_stoploss.entry_stop_map`'s).
    ``custom_stoploss`` later reads it back with
    :func:`~trading_backtest.strategy.freqtrade_stoploss.lookup_entry_stop`, whose
    three candidate keys absorb the "fill candle = signal candle + 1" shift.
    """
    pair = str(metadata.get("pair", "")) if isinstance(metadata, Mapping) else ""
    stops = _instance_stops(self)
    for stamp, stop in entry_stop_map(_directional_signals(signals, prepared)).items():
        stops[(pair, stamp)] = stop


# ---------------------------------------------------------------------------
# adapter methods -- the methods of the generated class
# ---------------------------------------------------------------------------


def adapter_populate_indicators(
    self: Any, dataframe: pd.DataFrame, metadata: dict[str, Any]
) -> pd.DataFrame:
    """``IStrategy.populate_indicators``: the house indicators, translated.

    Delegates to :func:`freqtrade_indicator_frame` with the house strategy of the
    **current** parameter values (:func:`_house_strategy`).
    """
    _validated_timeframe(self)
    return freqtrade_indicator_frame(dataframe, _house_strategy(self))


def adapter_populate_entry_trend(
    self: Any, dataframe: pd.DataFrame, metadata: dict[str, Any]
) -> pd.DataFrame:
    """``IStrategy.populate_entry_trend``: the house entries, renamed.

    Adds ``enter_long`` / ``enter_short`` (``entry_*`` names never appear) and —
    for audit only — the ``stop_loss`` column of the signal candle
    (:func:`freqtrade_entry_frame`), and records the absolute stops of that pair
    in ``_entry_stops`` for :func:`adapter_custom_stoploss`.

    ``prepare`` + ``signals`` are recomputed here (and again inside
    :func:`freqtrade_entry_frame`) because the house ``Strategy`` exposes no
    indicator-name list: recomputing is the only generic option, and the strategy
    contract makes it deterministic.
    """
    _validated_timeframe(self)
    strategy = _house_strategy(self)
    prepared = strategy.prepare(house_frame(dataframe))
    signals = strategy.signals(prepared)
    out = freqtrade_entry_frame(dataframe, strategy)
    _record_entry_stops(self, signals, prepared, metadata)
    return out


def adapter_populate_exit_trend(
    self: Any, dataframe: pd.DataFrame, metadata: dict[str, Any]
) -> pd.DataFrame:
    """``IStrategy.populate_exit_trend``: the house exits, renamed (names unchanged).

    Delegates to :func:`freqtrade_exit_frame`; the exits carry no stop, so nothing
    is recorded here.
    """
    _validated_timeframe(self)
    return freqtrade_exit_frame(dataframe, _house_strategy(self))


def adapter_custom_stoploss(
    self: Any,
    pair: str,
    trade: Any,
    current_time: Any,
    current_rate: float,
    current_profit: float,
    after_fill: bool,
    **kwargs: Any,
) -> float | None:
    """``IStrategy.custom_stoploss``: the per-candle house stop, replayed.

    Looks the absolute stop captured on the **signal candle** of ``trade`` up in
    ``_entry_stops`` and converts it to the ratio Freqtrade expects, recomputed on
    every call (so the stop stays absolute and never becomes a trailing stop).

    Returns
    -------
    float | None
        The ratio, or ``None`` when no stop was captured for that trade (unknown
        pair, candle outside the analysed window, or a ``NaN`` stop): Freqtrade
        then keeps its own ``stoploss`` attribute, i.e.
        :data:`~trading_backtest.strategy.freqtrade_stoploss.DEFAULT_FREQTRADE_STOPLOSS`.

    Notes
    -----
    ``after_fill``, ``current_time``, ``current_profit`` and ``kwargs`` are part
    of the Freqtrade signature and are deliberately ignored (Freqtrade itself
    detects ``after_fill`` by inspecting the argspec of this method).
    """
    _validated_timeframe(self)
    stops = self.__dict__.get(_ENTRY_STOPS_ATTRIBUTE)
    if not isinstance(stops, dict) or not stops:
        return None
    pair_stops = {stamp: stop for (entry_pair, stamp), stop in stops.items() if entry_pair == pair}
    open_date = getattr(trade, "open_date_utc", None)
    if open_date is None:
        open_date = getattr(trade, "open_date", None)
    stop = lookup_entry_stop(pair_stops, open_date=open_date, timeframe=_validated_timeframe(self))
    if stop is None:
        return None
    return stoploss_ratio_from_absolute(
        stop,
        float(current_rate),
        is_short=bool(getattr(trade, "is_short", False)),
    )


# ---------------------------------------------------------------------------
# class assembly
# ---------------------------------------------------------------------------


def default_class_name(house_name: str) -> str:
    """Return the Freqtrade class name of the house strategy ``house_name``.

    ``"basic"`` -> ``"BasicStrategy"``, which is exactly
    ``trading_backtest.freqtrade.DEFAULT_STRATEGY_NAME`` — the name declared by
    ``config/freqtrade_config.json`` and ``config/freqtrade_dryrun.json``.

    Examples
    --------
    >>> default_class_name("basic")
    'BasicStrategy'
    >>> default_class_name("ema_rsi_combo")
    'EmaRsiComboStrategy'
    """
    return "".join(part.capitalize() for part in str(house_name).split("_")) + "Strategy"


def freqtrade_strategy_namespace(
    house_name: str,
    *,
    timeframe: str,
    stoploss: float,
    params: Mapping[str, Any] | None = None,
    startup_candle_count: int | None = None,
    can_short: bool | None = None,
    param_overrides: Mapping[str, FreqtradeParamSpec] | None = None,
) -> dict[str, Any]:
    """Build the class namespace of the Freqtrade adapter of ``house_name``.

    The house strategy is resolved through
    :func:`~trading_backtest.strategy.registry.get_strategy` — **nothing is
    hard-coded for a particular strategy**, so the factory works for every name
    of :data:`~trading_backtest.strategy.registry.STRATEGIES`.  Its grid is read
    from the instance (``strategy.param_space()``) and handed explicitly to
    :func:`~trading_backtest.strategy.freqtrade_parameters.freqtrade_param_specs`.

    Parameters
    ----------
    house_name:
        A key of :data:`~trading_backtest.strategy.registry.STRATEGIES`.
    timeframe:
        Freqtrade timeframe of the profile; it has **no default** on
        ``IStrategy`` and ``IStrategy.__init__`` calls
        ``timeframe_to_minutes(self.timeframe)``, so it is required here.
    stoploss:
        Global ``stoploss`` bound (a ratio in ``]-1, 0[``).  It is **not** the
        stop used by the strategy: that one travels through ``_entry_stops`` and
        :func:`adapter_custom_stoploss`.
    params:
        Parameter overrides validated by the house ``ParamsModel`` (``None`` =
        house defaults).  They only define the *defaults* of the generated
        parameters (Freqtrade parameters are class attributes).
    startup_candle_count:
        Warm-up candles; defaults to
        :func:`~trading_backtest.strategy.freqtrade_parameters.startup_candle_count_for`.
    can_short:
        Short support; defaults to the house ``allow_short`` parameter.
    param_overrides:
        Manual mapping applied to the parameter specs (the documented escape
        hatch of :mod:`trading_backtest.strategy.freqtrade_parameters`).  Only
        the names the house ``ParamsModel`` declares are bridged back to the
        house strategy at run time; an override adding a Freqtrade-only
        parameter is still hyperoptable, but the house engine cannot consume it.

    Returns
    -------
    dict
        The namespace to hand to ``type(name, (IStrategy,), namespace)``: the
        frozen Freqtrade attributes, the parameter attributes, the six methods
        (``populate_indicators``, ``populate_entry_trend``, ``populate_exit_trend``,
        ``custom_stoploss``, ``house_frame``, ``_house_strategy``) and the two
        bridge attributes (``_house_parameter_names``, ``_house_cache``).

    Raises
    ------
    StrategyError
        If ``timeframe`` is unsupported, if ``stoploss`` is not a ratio in
        ``]-1, 0[``, if ``house_name`` is unknown (the registry message listing
        the available strategies is passed through unchanged), if ``params`` is
        rejected by the house model, or if the optional ``freqtrade`` extra is
        missing (the parameter objects cannot be built without it).

    Notes
    -----
    The timeframe and the stop-loss are validated **before** anything needs
    Freqtrade: the failure of a malformed profile never depends on whether the
    optional extra happens to be installed.
    """
    validated_timeframe = _checked_timeframe(timeframe)
    validated_stoploss = _checked_stoploss(stoploss)
    strategy = get_strategy(house_name, params)
    attributes = freqtrade_parameter_attributes(
        freqtrade_param_specs(strategy, grid=strategy.param_space(), overrides=param_overrides)
    )
    injected = {
        name: value for name, value in attributes.items() if name not in _PROTECTED_ATTRIBUTES
    }
    # Only fields the house model declares are handed back to it at run time: an
    # override may add a Freqtrade-only parameter (hyperopt), but a name the
    # house `ParamsModel` does not know would be rejected by its `extra="forbid"`
    # at every analysis cycle.
    house_fields = frozenset(strategy.ParamsModel.model_fields)
    bridged = tuple(name for name in injected if name in house_fields)
    short = (
        bool(getattr(strategy.params, "allow_short", False))
        if can_short is None
        else bool(can_short)
    )
    startup = (
        startup_candle_count_for(strategy)
        if startup_candle_count is None
        else max(0, int(startup_candle_count))
    )

    namespace: dict[str, Any] = {
        "INTERFACE_VERSION": FREQTRADE_INTERFACE_VERSION,
        "timeframe": validated_timeframe,
        "stoploss": validated_stoploss,
        "minimal_roi": dict(FREQTRADE_DISABLED_ROI),
        "use_custom_stoploss": True,
        "use_exit_signal": True,
        "exit_profit_only": False,
        "ignore_roi_if_entry_signal": False,
        "process_only_new_candles": True,
        "can_short": short,
        "startup_candle_count": startup,
        "house_strategy_name": str(house_name),
        "populate_indicators": adapter_populate_indicators,
        "populate_entry_trend": adapter_populate_entry_trend,
        "populate_exit_trend": adapter_populate_exit_trend,
        "custom_stoploss": adapter_custom_stoploss,
        "house_frame": house_frame,
        "_house_strategy": _house_strategy,
        "_house_parameter_names": bridged,
        _HOUSE_CACHE_ATTRIBUTE: None,
        _ENTRY_STOPS_ATTRIBUTE: {},
    }
    namespace.update(injected)
    return namespace


def make_freqtrade_strategy(
    house_name: str,
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
    stoploss: float = DEFAULT_FREQTRADE_STOPLOSS,
    params: Mapping[str, Any] | None = None,
    class_name: str | None = None,
    startup_candle_count: int | None = None,
    can_short: bool | None = None,
    param_overrides: Mapping[str, FreqtradeParamSpec] | None = None,
) -> type[Any]:
    """Return the ``IStrategy`` subclass exposing the house strategy ``house_name``.

    One line exposes any registered strategy to Freqtrade — backtest, dry-run and
    live — without recopying a single indicator or rule:

    .. code-block:: python

        BasicFreqtradeStrategy = make_freqtrade_strategy("basic")
        instance = BasicFreqtradeStrategy({})  # Freqtrade passes its own config

    Parameters
    ----------
    house_name:
        A key of :data:`~trading_backtest.strategy.registry.STRATEGIES`.
    timeframe, stoploss, params, startup_candle_count, can_short, param_overrides:
        See :func:`freqtrade_strategy_namespace`.
    class_name:
        Override of the generated class name; defaults to
        :func:`default_class_name` (``"basic"`` -> ``"BasicStrategy"``).

    Returns
    -------
    type
        The generated subclass, typed ``Any`` because Freqtrade ships no
        ``py.typed``: ``cast("type[Any]", type(...))`` keeps ``mypy --strict``
        happy without silencing mypy with an inline ignore comment.

    Raises
    ------
    StrategyError
        From :func:`freqtrade_strategy_namespace` (unknown strategy, unsupported
        timeframe, invalid stop-loss, invalid parameters, missing optional extra)
        or from :func:`_istrategy_base` when ``freqtrade`` is not installed.

    Examples
    --------
    The class name of ``basic`` is the name Freqtrade looks up in its
    configuration:

    >>> default_class_name("basic")
    'BasicStrategy'
    """
    namespace = freqtrade_strategy_namespace(
        house_name,
        timeframe=timeframe,
        stoploss=stoploss,
        params=params,
        startup_candle_count=startup_candle_count,
        can_short=can_short,
        param_overrides=param_overrides,
    )
    generated = type(class_name or default_class_name(house_name), (_istrategy_base(),), namespace)
    return cast("type[Any]", generated)


# ---------------------------------------------------------------------------
# validation of a produced class
# ---------------------------------------------------------------------------


def _signature_issues(name: str, method: Any) -> list[str]:
    """Return one issue per way ``method`` departs from the ``(self, dataframe, metadata)`` shape."""
    try:
        parameters = list(inspect.signature(method).parameters.values())
    except (TypeError, ValueError):
        return [f"{name} has no inspectable signature"]
    issues: list[str] = []
    if any(
        parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
        for parameter in parameters
    ):
        return [f"{name} must take exactly (self, dataframe, metadata), got *args/**kwargs"]
    if len(parameters) != 3:
        expected = "(self, dataframe, metadata)"
        got = f"({', '.join(parameter.name for parameter in parameters)})"
        issues.append(f"{name} must take {expected}, got {got}")
    return issues


def validate_freqtrade_adapter_class(cls: type[Any]) -> None:
    """Check that ``cls`` honours the Freqtrade adapter contract; return ``None``.

    Every rule is checked, and **every** violated rule is reported at once in the
    ``issues`` of the raised :class:`StrategyError` (never the first one only), so
    a broken shim is diagnosed in a single run:

    ===========================================  ==========================================
    rule                                         why it matters
    ===========================================  ==========================================
    ``freqtrade`` is importable                  otherwise nothing below can be checked
    ``issubclass(cls, IStrategy)``                Freqtrade only loads ``IStrategy`` subclasses
    ``cls.INTERFACE_VERSION == 3``                otherwise the ``metadata`` signature is wrong
    the three ``populate_*`` are overridden       all three are called by Freqtrade's analysis
    their ``(self, dataframe, metadata)`` shape    called positionally by Freqtrade
    ``cls.timeframe`` in ``SUPPORTED_TIMEFRAMES``  ``IStrategy.__init__`` converts it to minutes
    ``cls.stoploss`` is a real ratio in ``[-1, 0[`` ``IStrategy`` has no default for it
    ``cls.use_custom_stoploss is True``            the only way to replay a per-trade stop
    ``cls.use_exit_signal`` is a ``bool``          Freqtrade reads it as a boolean flag
    ``cls.house_strategy_name`` is registered      it *is* the house strategy of the class
    ===========================================  ==========================================

    Parameters
    ----------
    cls:
        The class produced by :func:`make_freqtrade_strategy` (or any candidate).

    Returns
    -------
    None
        The class is a valid adapter.

    Raises
    ------
    StrategyError
        If the optional ``freqtrade`` extra is missing (immediately, with the
        "install the optional extra" message), or if at least one rule is
        violated — the details list one line per violation.
    """
    base = _istrategy_base()
    if not isinstance(cls, type):
        raise StrategyError(
            "validate_freqtrade_adapter_class expects a class", [f"got {type(cls).__name__}"]
        )

    issues: list[str] = []
    if not issubclass(cls, base):
        issues.append(f"{cls.__name__} is not a subclass of freqtrade.strategy.IStrategy")

    version = getattr(cls, "INTERFACE_VERSION", None)
    if version != FREQTRADE_INTERFACE_VERSION:
        issues.append(f"INTERFACE_VERSION must be {FREQTRADE_INTERFACE_VERSION}, got {version!r}")

    for name in _POPULATE_METHODS:
        own = getattr(cls, name, None)
        if own is None:
            issues.append(f"{name} is missing")
        elif own is getattr(base, name, None):
            issues.append(f"{name} is not overridden (still IStrategy.{name})")
        else:
            issues.extend(_signature_issues(name, own))

    timeframe = getattr(cls, "timeframe", None)
    if not isinstance(timeframe, str) or timeframe not in SUPPORTED_TIMEFRAMES:
        issues.append(f"timeframe must be a supported timeframe, got {timeframe!r}")

    stoploss = getattr(cls, "stoploss", None)
    valid_stoploss = (
        not isinstance(stoploss, bool)
        and isinstance(stoploss, (int, float))
        and -1.0 <= float(stoploss) < 0.0
    )
    if not valid_stoploss:
        issues.append(f"stoploss must be a real ratio in [-1, 0[, got {stoploss!r}")

    if getattr(cls, "use_custom_stoploss", None) is not True:
        issues.append("use_custom_stoploss must be True")

    if not isinstance(getattr(cls, "use_exit_signal", None), bool):
        issues.append("use_exit_signal must be a bool")

    house_name = getattr(cls, "house_strategy_name", None)
    if not isinstance(house_name, str) or house_name not in STRATEGIES:
        issues.append(
            f"house_strategy_name {house_name!r} is not registered "
            f"(available: {', '.join(sorted(STRATEGIES))})"
        )

    if issues:
        raise StrategyError(f"{cls.__name__} is not a valid Freqtrade adapter class", issues)
