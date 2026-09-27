"""The ``keltner`` volatility-breakout strategy, filtered by a long EMA trend.

Canonical reference
    Chester Keltner's channel rule, in its modern (Linda Raschke / ``ATR``)
    form: a moving average of the close, shifted up and down by a multiple of
    the Average True Range.  The published rule set is public and mechanical;
    the trend filter below is the usual discretionary addition that keeps the
    breakouts on the right side of the long-term trend.

Rules (they are frozen — the engine, the validation layer and the documentation
all rely on them):

``keltner_mid``
    :func:`~trading_platform.strategy.indicators.ema` of ``close`` over
    ``ema_period`` candles.

``keltner_upper`` / ``keltner_lower``
    ``keltner_mid ± atr_multiplier * atr`` — the channel the close must escape.

``keltner_trend``
    :func:`~trading_platform.strategy.indicators.ema` of ``close`` over
    ``trend_ema_period`` candles: the long-term filter.  It must be strictly
    longer than ``ema_period`` (a validator enforces it).

``atr``
    Wilder's Average True Range over ``atr_period`` candles.

``entry_long``
    ``(close > keltner_upper) & (close > keltner_trend)`` — a close above the
    upper band **and** above the long trend: a volatility breakout is only taken
    in the direction of the trend.

``exit_long``
    ``close < keltner_mid`` — the close falls back inside the channel.

``entry_short`` / ``exit_short``
    the exact mirror images (``(close < keltner_lower) & (close < keltner_trend)``
    and ``close > keltner_mid``).  They are all ``False`` unless ``allow_short``
    is ``True``.

``stop_loss``
    ``close - atr_stop_multiplier * atr`` (``NaN`` while the ATR is not yet
    defined, i.e. "no stop").  A multiplier of exactly ``0.0`` means "no stop at
    all": the whole column is then ``NaN``.  The value is the *long* stop price;
    for a short entry the engine mirrors the same distance about the entry
    candle's close (see :mod:`trading_platform.strategy.engine`).

Hypothesis under test — no recorded verdict exists
    ``entry_long`` encodes the belief that a close outside an ATR-scaled channel
    marks the start of a move, and that the long EMA filters out the breakouts
    that are only noise inside a downtrend.  The repository's protocol never
    swept a Keltner breakout family (see ``docs/strategies.md`` and
    ``.scratch/research/FINDINGS*.md``), so **no** verdict — positive or
    negative — may be claimed for it.  Its status is *hypothesis under test*.

Candle-based lookbacks
    Every period of this strategy is expressed in **candles**, not in days, so
    the very same parameters mean "the last 20 candles" on any grid and the
    declared warm-up stays small enough for the platform's 200-candle budget.
    (``momentum`` is the opposite case: its horizons are day-based and are
    converted with the frame's own candle grid.)

Two properties matter for a faithful reading of the rules:

* the entries are **state**, not crossover events — ``entry_long`` stays ``True``
  on every candle that closes outside the channel in the direction of the trend,
  it does not only fire on the candle that crossed the band;
* ``NaN`` never fires a signal: while the ATR or an average is still undefined
  every comparison against it is ``False`` (nothing is ever filled with ``0``).

Warm-up (declared, enforced, never silent)
    ``keltner_trend`` needs a full ``trend_ema_period`` window, the mid band a
    full ``ema_period`` window and the ATR a full ``atr_period`` window: a frame
    must hold ``max(trend_ema_period, ema_period, atr_period) + 1`` candles
    before this strategy can emit **any** signal.  On the default parameters
    (100 / 20 / 10) that is 101 candles on every grid.
    :meth:`KeltnerStrategy.required_candles` declares that number (so the
    platform can refuse a profile it could never feed, or report it through
    ``realtime check``), and :meth:`KeltnerStrategy.prepare` logs one structured
    ``strategy.warmup_incomplete`` warning when it is handed a shorter frame.  A
    short frame is **not** an error — walk-forward windows and unit tests
    legitimately use them — so ``prepare`` never raises for that reason: it only
    makes the situation visible.
"""

from __future__ import annotations

import logging
from typing import ClassVar, cast

import numpy as np
import pandas as pd
from pydantic import Field, model_validator

from trading_platform.core.constants import REQUIRED_OHLCV_COLUMNS
from trading_platform.core.errors import StrategyError
from trading_platform.strategy.base import (
    Strategy,
    StrategyParams,
    ensure_signal_frame,
    require_ohlcv_frame,
)
from trading_platform.strategy.indicators import atr, ema
from trading_platform.strategy.registry import register_strategy

__all__ = ["KeltnerParams", "KeltnerStrategy", "KeltnerStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`KeltnerStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = (
    "keltner_mid",
    "keltner_upper",
    "keltner_lower",
    "keltner_trend",
    "atr",
)


class KeltnerStrategyParams(StrategyParams):
    """Validated parameters of :class:`KeltnerStrategy`.

    The declaration order is part of the contract: it drives the order in which
    the Freqtrade adapter exposes the hyperoptable parameters.
    """

    ema_period: int = Field(default=20, ge=2)
    atr_period: int = Field(default=10, ge=2)
    atr_multiplier: float = Field(default=2.0, gt=0.0)
    trend_ema_period: int = Field(default=100, ge=2)
    atr_stop_multiplier: float = Field(default=3.0, ge=0.0)
    allow_short: bool = False

    @model_validator(mode="after")
    def _check_coherence(self) -> KeltnerStrategyParams:
        """Enforce the trend filter to be strictly longer than the channel EMA."""
        if self.trend_ema_period <= self.ema_period:
            raise ValueError(
                f"trend_ema_period ({self.trend_ema_period}) must be greater than "
                f"ema_period ({self.ema_period})"
            )
        return self


#: Short spelling of :class:`KeltnerStrategyParams`.
#:
#: ``KeltnerStrategyParams`` follows the house naming convention of
#: :class:`~trading_platform.strategy.basic.BasicStrategyParams`
#: (``<StrategyClass>Params``); ``KeltnerParams`` is kept as the equivalent
#: short public name.  The two names denote **the very same class**: there is
#: exactly one validated parameter model per strategy.
KeltnerParams = KeltnerStrategyParams


@register_strategy
class KeltnerStrategy(Strategy):
    """Keltner channel volatility breakout filtered by a long EMA trend."""

    name: ClassVar[str] = "keltner"
    ParamsModel: ClassVar[type[StrategyParams]] = KeltnerStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "ema_period": [20, 40],
        "trend_ema_period": [100, 150],
        "atr_multiplier": [1.5, 2.0, 3.0],
        "atr_stop_multiplier": [0.0, 3.0],
    }

    @property
    def _keltner_params(self) -> KeltnerStrategyParams:
        """The parameters, typed as :class:`KeltnerStrategyParams`."""
        return cast(KeltnerStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the Keltner channel, its trend filter and ``atr`` to a copy of ``data``.

        The OHLCV columns of the input are copied unchanged and the index is
        preserved exactly; ``data`` itself is never mutated.

        A frame shorter than :meth:`required_candles` cannot warm up: the bands
        stay ``NaN`` and no entry or exit can ever fire.  That is legitimate in
        walk-forward windows and in unit tests, so the method logs a single
        structured ``strategy.warmup_incomplete`` **warning** and keeps going —
        it never raises for a short frame, and it stays completely silent once
        the frame is long enough.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._keltner_params
        frame = require_ohlcv_frame(data, name="data")
        required = _warmup(params)
        if len(frame) < required:
            _LOGGER.warning(
                "strategy %s cannot warm up on this frame: %d row(s) received,"
                " %d required (trend EMA %d candles, channel EMA %d candles,"
                " ATR %d candles)",
                self.name,
                len(frame),
                required,
                params.trend_ema_period,
                params.ema_period,
                params.atr_period,
                extra={
                    "event": "strategy.warmup_incomplete",
                    "strategy": self.name,
                    "rows": len(frame),
                    "required_candles": required,
                },
            )

        close = frame["close"]
        true_range_average = atr(frame["high"], frame["low"], close, params.atr_period)
        middle_values = ema(close, params.ema_period)
        trend_values = ema(close, params.trend_ema_period)
        # Both operands carry the same index and the same NaN warm-up prefix, so
        # the sum is NaN exactly where either of them is.
        deviation = params.atr_multiplier * true_range_average
        upper_values = middle_values + deviation
        lower_values = middle_values - deviation
        frame["keltner_mid"] = middle_values.to_numpy(dtype="float64")
        frame["keltner_upper"] = upper_values.to_numpy(dtype="float64")
        frame["keltner_lower"] = lower_values.to_numpy(dtype="float64")
        frame["keltner_trend"] = trend_values.to_numpy(dtype="float64")
        frame["atr"] = true_range_average.to_numpy(dtype="float64")
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        Every period of this strategy is expressed in candles, so the declared
        warm-up is ``max(trend_ema_period, ema_period, atr_period) + 1`` **on
        every grid**: the trend EMA is the last series to become defined, and the
        bands additionally need the ATR.  On the default parameters (100 / 20 /
        10) that is 101 candles on a 1m, 1h, 4h and 1d grid alike — the
        ``candles_per_day`` argument is therefore irrelevant here and is accepted
        only to honour the signature of :meth:`Strategy.required_candles`.

        The method is pure and never raises, whatever the argument: it reads
        nothing but the validated parameters and shares :func:`_warmup` with
        :meth:`prepare`, so the declared warm-up can never drift from the
        computed lookbacks.

        Parameters
        ----------
        candles_per_day:
            How many candles of the target frame fit in one 24-hour day.  It is
            ignored: the lookbacks are already counted in candles.

        Returns
        -------
        int
            ``max(trend_ema_period, ema_period, atr_period) + 1`` candles.
        """
        return _warmup(self._keltner_params)

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a prepared frame.

        The input must carry the OHLCV columns **and** the indicator columns
        produced by :meth:`prepare`.  The computation is pure and repeatable:
        the input frame is never mutated, and a ``NaN`` band never compares
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

        params = self._keltner_params
        close = data["close"].astype("float64")
        middle_values = data["keltner_mid"].astype("float64")
        upper_values = data["keltner_upper"].astype("float64")
        lower_values = data["keltner_lower"].astype("float64")
        trend_values = data["keltner_trend"].astype("float64")
        true_range_average = data["atr"].astype("float64")
        multiplier = params.atr_stop_multiplier

        entry_long = (close > upper_values) & (close > trend_values)
        exit_long = close < middle_values
        if params.allow_short:
            entry_short = (close < lower_values) & (close < trend_values)
            exit_short = close > middle_values
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


def _warmup(params: KeltnerStrategyParams) -> int:
    """Return the candles :meth:`KeltnerStrategy.prepare` needs to warm up.

    ``ema(close, trend_ema_period)`` is the last series to become defined (its
    first value sits on row ``trend_ema_period - 1``); ``ema(close, ema_period)``
    defines the mid band, ``atr(...)`` the band width and the stop.  ``+ 1``
    makes the first fully defined row part of the requirement, so the declared
    number is a safe floor on every grid.

    :meth:`KeltnerStrategy.prepare` and
    :meth:`KeltnerStrategy.required_candles` both go through this single helper,
    so the warm-up actually enforced and the warm-up declared can never drift
    apart.  The function is pure and never raises for a validated
    :class:`KeltnerStrategyParams`.
    """
    return max(params.trend_ema_period, params.ema_period, params.atr_period) + 1
