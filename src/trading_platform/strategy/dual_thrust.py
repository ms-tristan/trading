"""The Dual Thrust intraday range-breakout rule — the ``dual_thrust`` strategy.

Canonical rule
    Michael Chu's *Dual Thrust* (the range-breakout rule popularised by the
    Chinese futures community): the day's trading range is estimated from the
    previous candles and two breakout lines are placed symmetrically around the
    **open** of the current one — a long trigger at ``open + k1 * range`` and a
    short trigger at ``open - k2 * range``.  The published rule is an
    **intraday** rule whose bars are usually minutes, so deploying it on a daily
    grid is an extrapolation of the rule rather than the rule itself.

Rules (they are frozen — the engine, the validation layer and the documentation
all rely on them):

``dual_thrust_range``
    ``max(rolling_max(high, range_period) - rolling_min(close, range_period),
    rolling_max(close, range_period) - rolling_min(low, range_period))``,
    computed on the **previous** candles: the raw range is shifted by one row so
    that the current candle never contributes to its own range.  That shift is
    the whole point of the rule — without it the breakout line would move with
    the very candle it is supposed to be tested against.

``dual_thrust_buy_line`` / ``dual_thrust_sell_line``
    ``open + k1 * dual_thrust_range`` and ``open - k2 * dual_thrust_range``: the
    lines of row ``t`` are therefore built from the open of row ``t`` and a
    range computed on rows ``< t`` only.

``entry_long`` / ``exit_long``
    ``close > dual_thrust_buy_line`` and ``close < dual_thrust_sell_line`` —
    strictly beyond the line; an exact equality fires nothing.

``entry_short`` / ``exit_short``
    the exact mirror images (``close < dual_thrust_sell_line`` and
    ``close > dual_thrust_buy_line``).  They are all ``False`` unless
    ``allow_short`` is ``True``.

``stop_loss``
    ``close - atr_stop_multiplier * atr`` (``NaN`` while the ATR is not yet
    defined, i.e. "no stop").  A multiplier of exactly ``0.0`` means "no stop at
    all": the whole column is then ``NaN``, never a stop sitting on the close.

``NaN`` never fires a signal
    While the range is still warming up — or while a missing price makes a
    rolling window undefined — the lines are ``NaN`` and every comparison
    against them is ``False`` (the series are deliberately **not** filled with
    ``0``).

Parameters carry only per-field bounds
    There is deliberately **no** cross-field validator: ``k1``, ``k2``,
    ``range_period`` and ``atr_period`` are independent, and no combination of
    them is incoherent — a large ``k1`` with a small ``k2`` is an asymmetric but
    perfectly well-defined Dual Thrust configuration.  ``StrategyParams`` is
    ``extra="forbid"``, so an unknown key is still rejected loudly.

Warm-up (declared, enforced, never silent)
    The range needs ``range_period`` rows for its rolling windows plus one more
    for the shift and the ATR is defined from candle ``atr_period + 1`` on, so
    the exact requirement is ``max(range_period + 1, atr_period + 1)`` rows and
    the declared floor is the contract's ``max(range_period, atr_period) + 2``
    candles — 16 rows on the default parameters, whatever the grid, one row above
    the exact requirement and never below it.
    :meth:`DualThrustStrategy.required_candles` declares that number (so the
    platform can refuse a profile it could never feed) and
    :meth:`DualThrustStrategy.prepare` logs one structured
    ``strategy.warmup_incomplete`` warning when it is handed a shorter frame.  A
    short frame is **not** an error — walk-forward windows and unit tests
    legitimately use them — so ``prepare`` never raises for that reason: it only
    makes the situation visible.

Status: hypothesis under test
    No Dual Thrust verdict exists in the repository's research ledger: the
    family has never been put through the validation protocol, so nothing here
    is validated.  The hypothesis this strategy encodes is that a breakout
    measured against a multiple of the *recent range* around the open carries
    information the trend families do not; that hypothesis is exactly what the
    protocol has yet to test.
"""

from __future__ import annotations

import logging
import math
from typing import ClassVar, cast

import numpy as np
import pandas as pd
from pydantic import Field

from trading_platform.core.constants import REQUIRED_OHLCV_COLUMNS
from trading_platform.core.errors import StrategyError
from trading_platform.strategy.base import (
    Strategy,
    StrategyParams,
    ensure_signal_frame,
    require_ohlcv_frame,
)
from trading_platform.strategy.indicators import atr, rolling_max, rolling_min
from trading_platform.strategy.registry import register_strategy

__all__ = ["DualThrustParams", "DualThrustStrategy", "DualThrustStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`DualThrustStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = (
    "dual_thrust_range",
    "dual_thrust_buy_line",
    "dual_thrust_sell_line",
    "atr",
)


class DualThrustStrategyParams(StrategyParams):
    """Validated parameters of :class:`DualThrustStrategy`.

    Every period is expressed in **candles**, never in days, so the declared
    warm-up is a fixed candle count on every grid.

    The model carries only per-field bounds and deliberately **no** cross-field
    validator: the four lookbacks and the two multipliers are independent, so
    there is no incoherent combination to reject.  The declaration order is part
    of the contract: it drives the order in which the Freqtrade adapter exposes
    the hyperoptable parameters.
    """

    range_period: int = Field(default=5, ge=2)
    k1: float = Field(default=0.5, gt=0.0)
    k2: float = Field(default=0.5, gt=0.0)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=3.0, ge=0.0)
    allow_short: bool = False


#: Short spelling of :class:`DualThrustStrategyParams`.
#:
#: ``DualThrustStrategyParams`` follows the house naming convention of
#: :class:`~trading_platform.strategy.basic.BasicStrategyParams`
#: (``<StrategyClass>Params``); ``DualThrustParams`` is kept as the equivalent
#: short public name.  The two names denote **the very same class**: there is
#: exactly one validated parameter model per strategy.
DualThrustParams = DualThrustStrategyParams


@register_strategy
class DualThrustStrategy(Strategy):
    """Intraday range-breakout strategy around the open, with an optional ATR stop."""

    name: ClassVar[str] = "dual_thrust"
    ParamsModel: ClassVar[type[StrategyParams]] = DualThrustStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "range_period": [3, 5, 10],
        "k1": [0.3, 0.5, 0.7],
        "k2": [0.3, 0.5, 0.7],
        "atr_stop_multiplier": [0.0, 3.0],
    }

    @property
    def _dual_thrust_params(self) -> DualThrustStrategyParams:
        """The parameters, typed as :class:`DualThrustStrategyParams`."""
        return cast(DualThrustStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the Dual Thrust columns and ``atr`` to a copy of ``data``.

        The range is built from the frame's own ``open`` / ``high`` / ``low`` /
        ``close`` and is then shifted by one row, so the buy and sell lines of
        row ``t`` use the open of row ``t`` and the range of the rows **before**
        it — the current candle never contributes to its own range.  The OHLCV
        columns of the input are copied unchanged and the index is preserved
        exactly; ``data`` itself is never mutated.

        A frame shorter than :meth:`required_candles` sits below the declared
        floor and may not be able to warm up at all — with the default
        parameters both lines stay ``NaN`` on every row and no entry or exit can
        ever fire.  That is legitimate in walk-forward windows and in unit tests,
        so the method logs a single structured ``strategy.warmup_incomplete``
        **warning** and keeps going — it never raises for a short frame, and it
        stays completely silent once the frame is long enough.  The comparison is
        made against the declared floor, which keeps one row of margin over the
        exact requirement.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._dual_thrust_params
        frame = require_ohlcv_frame(data, name="data")
        required = _warmup(params)
        if len(frame) < required:
            _LOGGER.warning(
                "strategy %s cannot warm up on this frame: %d row(s) received,"
                " %d required (range_period %d + 1 shift, atr_period %d + 1)",
                self.name,
                len(frame),
                required,
                params.range_period,
                params.atr_period,
                extra={
                    "event": "strategy.warmup_incomplete",
                    "strategy": self.name,
                    "rows": len(frame),
                    "required_candles": required,
                },
            )

        close = frame["close"]
        range_period = params.range_period
        upper_span = rolling_max(frame["high"], range_period) - rolling_min(close, range_period)
        lower_span = rolling_max(close, range_period) - rolling_min(frame["low"], range_period)
        # ``np.maximum`` propagates NaN (unlike ``np.fmax``): a window holding a
        # missing price must leave the range undefined, never silently use the
        # other span.
        raw_range = pd.Series(
            np.maximum(
                upper_span.to_numpy(dtype="float64"),
                lower_span.to_numpy(dtype="float64"),
            ),
            index=frame.index,
            dtype="float64",
        )
        # The range of the PREVIOUS candles: shifting here is the whole point of
        # the rule, because the current candle must not size its own breakout.
        thrust_range = raw_range.shift(1)
        frame["dual_thrust_range"] = thrust_range.to_numpy(dtype="float64")
        frame["dual_thrust_buy_line"] = (frame["open"] + params.k1 * thrust_range).to_numpy(
            dtype="float64"
        )
        frame["dual_thrust_sell_line"] = (frame["open"] - params.k2 * thrust_range).to_numpy(
            dtype="float64"
        )
        frame["atr"] = atr(frame["high"], frame["low"], close, params.atr_period).to_numpy(
            dtype="float64"
        )
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        The two rolling windows need ``range_period`` rows, the shift that keeps
        the current candle out of its own range needs one more, and the ATR is
        defined from candle ``atr_period + 1`` on, so the declared floor is
        ``max(range_period, atr_period) + 2`` candles: ``16`` on the default
        parameters (``range_period = 5``, ``atr_period = 14``), ``22`` with
        ``range_period = 20``.  Every period is already expressed in candles, so
        the answer is the same on the 1m, 1h, 4h and 1d grids.

        The method is pure and never raises: it reads nothing but the validated
        parameters and normalises ``candles_per_day`` without letting it scale
        the answer, because the periods of this strategy are already candle
        counts.  It shares :func:`_warmup` with :meth:`prepare`, so the declared
        warm-up can never drift from the computed lookbacks.

        Parameters
        ----------
        candles_per_day:
            How many candles of the target frame fit in one 24-hour day (1m
            ``1440``, 5m ``288``, 15m ``96``, 30m ``48``, 1h ``24``, 4h ``6``,
            1d ``1``).  Irrelevant here: the parameters are candle counts.

        Returns
        -------
        int
            ``max(range_period, atr_period) + 2`` candles.
        """
        return _warmup(self._dual_thrust_params, candles_per_day)

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a prepared frame.

        The input must carry the OHLCV columns **and** the indicator columns
        produced by :meth:`prepare`.  The computation is pure and repeatable:
        the input frame is never mutated, and a ``NaN`` line never compares
        ``True`` (it is never filled with ``0``).

        Raises
        ------
        StrategyError
            If ``data`` is not a prepared frame.
        """
        if not isinstance(data, pd.DataFrame):
            raise StrategyError(f"data must be a pandas DataFrame, got {type(data).__name__}")
        required = (*REQUIRED_OHLCV_COLUMNS, *INDICATOR_COLUMNS)
        missing = [column for column in required if column not in data.columns]
        if missing:
            raise StrategyError(
                "signals() must be called on a frame returned by prepare()",
                [f"missing column(s): {', '.join(missing)}"],
            )

        params = self._dual_thrust_params
        close = data["close"].astype("float64")
        buy_line = data["dual_thrust_buy_line"].astype("float64")
        sell_line = data["dual_thrust_sell_line"].astype("float64")
        true_range_average = data["atr"].astype("float64")
        multiplier = params.atr_stop_multiplier

        entry_long = close > buy_line
        exit_long = close < sell_line
        if params.allow_short:
            entry_short = close < sell_line
            exit_short = close > buy_line
        else:
            entry_short = pd.Series(False, index=data.index, dtype=bool)
            exit_short = pd.Series(False, index=data.index, dtype=bool)

        if multiplier == 0.0:
            # 0.0 means "no stop": the column must be entirely NaN, not an ATR
            # distance of zero (which would be a stop at the close).
            stop_loss = pd.Series(np.nan, index=data.index, dtype="float64")
        else:
            stop_loss = close - multiplier * true_range_average

        signals = pd.DataFrame(
            {
                "entry_long": entry_long.to_numpy(dtype=bool),
                "exit_long": exit_long.to_numpy(dtype=bool),
                "entry_short": entry_short.to_numpy(dtype=bool),
                "exit_short": exit_short.to_numpy(dtype=bool),
                "stop_loss": stop_loss.to_numpy(dtype="float64"),
            },
            index=data.index,
        )
        return ensure_signal_frame(signals, data.index)


def _warmup(params: DualThrustStrategyParams, candles_per_day: float = 1.0) -> int:
    """Return the candles ``params`` need before any signal can fire.

    The two rolling windows of the range need ``range_period`` rows, the shift
    that keeps the current candle out of its own range needs one more, and the
    ATR is defined from candle ``atr_period + 1`` on, so the exact requirement is
    ``max(range_period + 1, atr_period + 1)`` rows and the declared floor is
    ``max(range_period, atr_period) + 2`` — one row above it, never below it.
    :meth:`DualThrustStrategy.prepare` and
    :meth:`DualThrustStrategy.required_candles` both go through this single
    helper, so the warm-up actually applied and the warm-up declared can never
    drift apart.

    ``candles_per_day`` is normalised through :func:`_safe_candles_per_day` so
    that :meth:`DualThrustStrategy.required_candles` stays **total** — a
    degenerate grid never raises — but it deliberately does **not** scale the
    answer: every period of this strategy is already a candle count, so the
    declared warm-up is the same on the 1m, 1h, 4h and 1d grids.

    The function is pure and never raises for a validated
    :class:`DualThrustStrategyParams`.
    """
    _ = _safe_candles_per_day(candles_per_day)
    return max(params.range_period, params.atr_period) + 2


def _safe_candles_per_day(candles_per_day: float) -> float:
    """Return ``candles_per_day`` as a finite, strictly positive ``float``.

    The frozen strategy contract makes :meth:`Strategy.required_candles` total:
    a non-numeric, non-finite, zero or negative grid is treated as ``1.0``
    instead of raising.  A candle-based strategy ignores the value, but the
    normalisation is kept so the signature behaves exactly like the day-based
    ones.
    """
    try:
        per_day = float(candles_per_day)
    except (TypeError, ValueError):
        per_day = 1.0
    if not math.isfinite(per_day) or per_day <= 0.0:
        per_day = 1.0
    return per_day
