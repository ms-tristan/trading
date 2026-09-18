"""Bridge the per-candle house stop-loss onto the Freqtrade stop-loss contract.

DECISION (frozen — do not revisit without rewriting this docstring)
------------------------------------------------------------------
Freqtrade has **no per-candle stop-loss column**: a strategy owns a single
``stoploss`` class attribute (a ratio, the hard maximum loss) plus an optional
``custom_stoploss()`` callback.  The house engine, on the other hand, stores one
absolute stop price per candle in the ``stop_loss`` column of the signal frame.

The adapter therefore:

1. sets ``use_custom_stoploss = True``;
2. **captures the absolute stop of every candle** in a
   ``{(pair, signal-candle timestamp): stop}`` map (:func:`entry_stop_map`);
   the stop of a trade is the one read on the **signal candle**, exactly like
   the house engine (see :mod:`trading_platform.strategy.engine`);
3. on every ``custom_stoploss()`` call converts it back to a ratio with
   :func:`stoploss_ratio_from_absolute`, i.e.
   ``freqtrade.strategy.stoploss_from_absolute(entry_stop, current_rate, is_short=...)``
   — recomputed on **every** call, so the stop stays **absolute** and never
   becomes a trailing stop;
4. returns ``None`` when the captured stop is ``NaN`` ("no stop"): Freqtrade then
   keeps its own ``self.stoploss`` attribute, which the adapter initialises to
   :data:`DEFAULT_FREQTRADE_STOPLOSS`.

REJECTED alternative (kept on record): mapping the column onto a single static
class-level ``stoploss`` ratio.  It is trivial, but it is also *wrong*: the ratio
is relative to the entry price, so it loses the per-trade ATR distance of the
house strategy and cannot express "no stop" per trade.

WHY :func:`signal_candle_for` SUBTRACTS ONE CANDLE (verified in freqtrade 2026.8
source, ``freqtrade/optimize/backtesting.py``): ``Backtesting._get_ohlcv_as_lists``
shifts ``enter_long``/``exit_long``/``enter_short``/``exit_short`` by ONE candle
before the loop ("To avoid using data from future, we use entry/exit signals
shifted from the previous candle") and drops the first row, and the loop then
enters at ``row[OPEN_IDX]``.  ``trade.open_date_utc`` is therefore the **fill**
candle = signal candle + 1 timeframe.  In live / dry-run ``open_date_utc`` is a
real fill timestamp which is *not* on a candle boundary, hence the
:func:`candle_floor` normalisation before the subtraction and the two fallback
keys tried by :func:`lookup_entry_stop`.

SHORTS
------
The house engine reads the ``stop_loss`` column as the *long* formula and
mirrors it for a short entry: the applied stop price is
``fill_price + (signal_close - stop_loss(signal candle))``.  The map therefore
has to be filled with the **actual stop price of the direction**; the adapter
mirrors the column before calling :func:`entry_stop_map` (or stores the two
directions separately), and passes ``is_short=trade.is_short`` so
``stoploss_from_absolute`` mirrors the ratio back.

DOCUMENTED LIMITS
-----------------
(i) the house stop is **static** and read on the signal candle — this module
    reproduces it exactly, but Freqtrade decides when and how the stop is
    executed (``order_types['stoploss'] = 'limit'`` by default,
    ``stoploss_on_exchange = False``);
(ii) ``stoploss_from_absolute`` clamps the ratio at ``0.0``, so a stop that is
    already breached (a long stop above ``current_rate``) becomes an immediate
    exit at market instead of a negative ratio;
(iii) for shorts the ratio is mirrored via ``is_short=trade.is_short``;
(iv) ``after_fill`` is accepted and **ignored** (Freqtrade itself sets
    ``_ft_stop_uses_after_fill = True`` by inspecting the argspec of
    ``custom_stoploss``);
(v) **MEMORY**: the map keeps one float per candle per pair for the lifetime of
    the process (~1 float/candle/pair, no pruning in this brick) — a known limit
    for very long 1-minute live runs;
(vi) (verified in freqtrade 2026.8 ``Trade.adjust_stop_loss``) the ratio is
    re-derived from the frozen absolute stop on every call, but Freqtrade only
    applies the update when it *tightens* the live stop (``allow_refresh`` is
    only true on the after-fill path).  The executed stop can therefore end up
    **tighter** than — never looser than — the house stop.

This module is **freqtrade-free at import time**: the only Freqtrade import lives
inside :func:`stoploss_ratio_from_absolute`, so the package (and its whole test
suite) keeps working with the ``.[dev]`` extra only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd

from trading_platform.core.constants import candle_delta
from trading_platform.core.errors import StrategyError

__all__ = [
    "DEFAULT_FREQTRADE_STOPLOSS",
    "FREQTRADE_STOPLOSS_COLUMN",
    "candle_floor",
    "entry_stop_map",
    "lookup_entry_stop",
    "signal_candle_for",
    "stoploss_ratio_from_absolute",
]

#: Defensive hard maximum loss handed to Freqtrade's ``stoploss`` attribute.
#: A custom stoploss can never be *tighter* than ``self.stoploss``, so keeping it
#: at the widest legal value leaves the whole decision to the captured absolute
#: stop instead of letting the class attribute cut trades short.
DEFAULT_FREQTRADE_STOPLOSS: float = -0.99

#: House column carried into the Freqtrade frame for auditing only.  Freqtrade
#: ignores it (verified: it is not part of
#: ``freqtrade.optimize.backtesting.HEADERS``, which is what the backtest loop
#: reads -- the stop itself travels through the map and ``custom_stoploss()``).
FREQTRADE_STOPLOSS_COLUMN: str = "stop_loss"

#: Error message raised when the optional Freqtrade extra is missing.  Byte for
#: byte the same wording as the one raised by
#: :mod:`trading_platform.strategy.freqtrade_parameters` and
#: :mod:`trading_platform.strategy.freqtrade_adapter`, so a caller catching the
#: missing extra sees one message whichever seam it touched.
FREQTRADE_MISSING_MESSAGE = (
    "freqtrade is not installed: install the optional extra with "
    "pip install 'trading-platform[freqtrade]'"
)


def candle_floor(timestamp: Any, timeframe: str) -> pd.Timestamp:
    """Return the opening timestamp of the ``timeframe`` candle holding ``timestamp``.

    The value is normalised to UTC first (naive timestamps are *assumed* UTC, so a
    naive house frame and a tz-aware live timestamp hash to the same key), then
    floored with :func:`~trading_platform.core.constants.candle_delta`.

    Raises
    ------
    ConfigError
        If ``timeframe`` is not supported (propagated from ``candle_delta``).
    """
    delta = candle_delta(timeframe)
    stamp = pd.Timestamp(timestamp)
    stamp = stamp.tz_localize("UTC") if stamp.tz is None else stamp.tz_convert("UTC")
    return stamp.floor(delta)


def entry_stop_map(signals: pd.DataFrame) -> dict[pd.Timestamp, float]:
    """Map every candle of ``signals`` carrying a stop to its **absolute** stop price.

    Parameters
    ----------
    signals:
        A house signal frame (see
        :data:`~trading_platform.core.constants.SIGNAL_COLUMNS`), indexed by the
        candle timestamps.  The index is normalised to UTC so that live lookups
        match; it is *not* floored (the house frame is already on candle
        boundaries).

    Returns
    -------
    dict
        One ``{candle: stop}`` entry per candle whose
        :data:`FREQTRADE_STOPLOSS_COLUMN` is not ``NaN`` ("no stop"), as plain
        floats.  An empty dict when the frame has no ``stop_loss`` column at all.

    Notes
    -----
    For a *short* entry the house column holds the long-form price: mirror it
    first (``fill_price + (signal_close - raw_stop)``, see the module docstring)
    so that the value stored here really is the stop price of the trade.
    """
    if FREQTRADE_STOPLOSS_COLUMN not in signals.columns:
        return {}

    stamps = pd.DatetimeIndex(signals.index)
    stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
    stops = pd.to_numeric(signals[FREQTRADE_STOPLOSS_COLUMN], errors="coerce")
    return {
        stamp: float(stop) for stamp, stop in zip(stamps, stops, strict=True) if not pd.isna(stop)
    }


def signal_candle_for(open_date: Any, timeframe: str) -> pd.Timestamp:
    """Return the candle that produced the signal of a trade opened at ``open_date``.

    Freqtrade fills a trade on the candle *after* the signal candle (see the
    module docstring), so this is ``candle_floor(open_date, timeframe) - candle_delta(timeframe)``.
    """
    return candle_floor(open_date, timeframe) - candle_delta(timeframe)


def lookup_entry_stop(
    stops: Mapping[pd.Timestamp, float],
    *,
    open_date: Any,
    timeframe: str,
) -> float | None:
    """Return the absolute stop captured for the trade opened at ``open_date``.

    Three keys are tried, in order:

    1. :func:`signal_candle_for` — the backtest (fill candle = signal candle + 1)
       and the live/dry-run case (a fill inside a candle reacts to the last
       *closed* candle, which is the previous one) both land here;
    2. :func:`candle_floor` of ``open_date`` itself — defensive, for callers that
       feed the signal candle straight in;
    3. the raw ``pd.Timestamp(open_date)`` — for a map keyed exactly like the
       caller's timestamps.

    Returns ``None`` when nothing matches (unknown candle, unknown pair or an
    empty map), which makes Freqtrade fall back to its own ``stoploss`` attribute.
    """
    if not stops:
        return None
    candidates = (
        signal_candle_for(open_date, timeframe),
        candle_floor(open_date, timeframe),
        pd.Timestamp(open_date),
    )
    for candidate in candidates:
        stop = stops.get(candidate)
        if stop is not None:
            return stop
    return None


def stoploss_ratio_from_absolute(
    stop_rate: float,
    current_rate: float,
    *,
    is_short: bool,
) -> float:
    """Convert an absolute stop price into the ratio Freqtrade's ``custom_stoploss`` returns.

    Thin, stateless delegation to ``freqtrade.strategy.stoploss_from_absolute``:
    the ratio is recomputed from the *absolute* stop on every call, so nothing is
    cached and the stop never drifts into a trailing stop.  For a long the result
    is ``1 - stop_rate / current_rate``; Freqtrade itself mirrors it for a short
    and clamps it at ``0.0`` when the requested stop is already breached.

    Raises
    ------
    StrategyError
        If the optional ``freqtrade`` dependency is not installed.
    """
    try:
        from freqtrade.strategy import stoploss_from_absolute
    except ImportError as exc:
        raise StrategyError(FREQTRADE_MISSING_MESSAGE) from exc
    return float(stoploss_from_absolute(stop_rate, current_rate, is_short=is_short))
