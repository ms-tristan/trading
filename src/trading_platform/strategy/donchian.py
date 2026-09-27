"""The ``donchian`` channel-breakout strategy — the Turtle traders' entry rule.

Canonical reference
    Richard Donchian's price channels, traded by the Turtle programme as a
    20-candle *entry* channel (buy a new ``entry_period`` high) and a 10-candle
    *exit* channel (leave on a new ``exit_period`` low).  The rule set is
    public, mechanical and has been replicated for four decades; it is the
    archetypal trend-following breakout.

Rules (they are frozen — the engine, the validation layer and the documentation
all rely on them):

``donchian_upper``
    :func:`~trading_platform.strategy.indicators.rolling_max` of ``high`` over
    ``entry_period`` candles: the highest high of the completed window.

``donchian_lower``
    :func:`~trading_platform.strategy.indicators.rolling_min` of ``low`` over
    ``exit_period`` candles: the lowest low of the completed window.

``atr``
    Wilder's Average True Range over ``atr_period`` candles, used by the stop.

``entry_long``
    ``close > donchian_upper.shift(1)`` — the close takes out the **previous**
    completed channel.  The shift is part of the rule, not an optimisation
    detail: comparing the close against a window that already contains the
    current candle would make the breakout self-fulfilling and untradeable.

``exit_long``
    ``close < donchian_lower.shift(1)``.

``entry_short`` / ``exit_short``
    the exact mirror images (``close < donchian_lower.shift(1)`` and
    ``close > donchian_upper.shift(1)``).  They are all ``False`` unless
    ``allow_short`` is ``True``.

``stop_loss``
    ``close - atr_stop_multiplier * atr`` (``NaN`` while the ATR is not yet
    defined, i.e. "no stop").  A multiplier of exactly ``0.0`` means "no stop at
    all": the whole column is then ``NaN``.  The value is the *long* stop price;
    for a short entry the engine mirrors the same distance about the entry
    candle's close (see :mod:`trading_platform.strategy.engine`).

Hypothesis under test
    ``entry_long`` encodes the belief that a market making a new N-candle high
    is entering a persistent move, i.e. that the breakout *event* carries the
    same edge as the momentum *state*.  The exit channel is the trailing stop
    that turns the trade off when the move has given back its own low.

Recorded verdict — hypothesis under test, NOT validated
    The repository's own protocol (see ``docs/strategies.md`` and
    ``.scratch/research/FINDINGS*.md``) evaluated the Donchian breakout family
    and did **not** validate it: DEV Sharpe ``0.797`` and, on the holdout, an
    equal-weight basket of ``-8.2 %`` with a Sharpe of ``0.161``.  The breakout
    entry was strictly worse than the state-based ``momentum`` entry the
    platform already ships.  This module therefore carries the status
    *hypothesis under test*: it is a faithful, testable implementation of a
    published rule set, never a claim of edge.

Candle-based lookbacks
    Every period of this strategy is expressed in **candles**, not in days, so
    the very same parameters mean "the last 20 candles" on any grid and the
    declared warm-up stays small enough for the platform's 200-candle budget.
    (``momentum`` is the opposite case: its horizons are day-based and are
    converted with the frame's own candle grid.)

Two properties matter for a faithful reading of the rules:

* the entries are **state**, not crossover events — ``entry_long`` stays ``True``
  on every candle that closes above the previous channel, it does not only fire
  on the candle that crossed it;
* ``NaN`` never fires a signal: while a channel is still undefined every
  comparison against it is ``False`` (nothing is ever filled with ``0``).

Warm-up (declared, enforced, never silent)
    ``donchian_upper`` needs a full ``entry_period`` window and is then compared
    through ``shift(1)``, ``donchian_lower`` a full ``exit_period`` window, and
    the ATR is needed by the stop: a frame must hold
    ``max(entry_period, exit_period) + 2`` candles before this strategy can emit
    **any** signal.  On the default parameters (20 / 10) that is 22 candles on
    every grid.
    :meth:`DonchianStrategy.required_candles` declares that number (so the
    platform can refuse a profile it could never feed, or report it through
    ``realtime check``), and :meth:`DonchianStrategy.prepare` logs one structured
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
from trading_platform.strategy.indicators import atr, rolling_max, rolling_min
from trading_platform.strategy.registry import register_strategy

__all__ = ["DonchianParams", "DonchianStrategy", "DonchianStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`DonchianStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = (
    "donchian_upper",
    "donchian_lower",
    "atr",
)


class DonchianStrategyParams(StrategyParams):
    """Validated parameters of :class:`DonchianStrategy`.

    The declaration order is part of the contract: it drives the order in which
    the Freqtrade adapter exposes the hyperoptable parameters.
    """

    entry_period: int = Field(default=20, ge=2)
    exit_period: int = Field(default=10, ge=1)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=2.0, ge=0.0)
    allow_short: bool = False

    @model_validator(mode="after")
    def _check_coherence(self) -> DonchianStrategyParams:
        """Enforce the exit channel to be shorter than the entry channel."""
        if self.exit_period >= self.entry_period:
            raise ValueError(
                f"exit_period ({self.exit_period}) must be lower than entry_period "
                f"({self.entry_period})"
            )
        return self


#: Short spelling of :class:`DonchianStrategyParams`.
#:
#: ``DonchianStrategyParams`` follows the house naming convention of
#: :class:`~trading_platform.strategy.basic.BasicStrategyParams`
#: (``<StrategyClass>Params``); ``DonchianParams`` is kept as the equivalent
#: short public name.  The two names denote **the very same class**: there is
#: exactly one validated parameter model per strategy.
DonchianParams = DonchianStrategyParams


@register_strategy
class DonchianStrategy(Strategy):
    """Donchian channel breakout with an optional ATR tail stop."""

    name: ClassVar[str] = "donchian"
    ParamsModel: ClassVar[type[StrategyParams]] = DonchianStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "entry_period": [20, 40, 55],
        "exit_period": [5, 10],
        "atr_stop_multiplier": [0.0, 2.0, 4.0],
    }

    @property
    def _donchian_params(self) -> DonchianStrategyParams:
        """The parameters, typed as :class:`DonchianStrategyParams`."""
        return cast(DonchianStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the two channels and ``atr`` to a copy of ``data``.

        The OHLCV columns of the input are copied unchanged and the index is
        preserved exactly; ``data`` itself is never mutated.

        A frame shorter than :meth:`required_candles` cannot warm up: both
        channels stay ``NaN`` and no entry or exit can ever fire.  That is
        legitimate in walk-forward windows and in unit tests, so the method logs
        a single structured ``strategy.warmup_incomplete`` **warning** and keeps
        going — it never raises for a short frame, and it stays completely
        silent once the frame is long enough.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._donchian_params
        frame = require_ohlcv_frame(data, name="data")
        required = _warmup(params)
        if len(frame) < required:
            _LOGGER.warning(
                "strategy %s cannot warm up on this frame: %d row(s) received,"
                " %d required (entry channel %d candles, exit channel %d candles)",
                self.name,
                len(frame),
                required,
                params.entry_period,
                params.exit_period,
                extra={
                    "event": "strategy.warmup_incomplete",
                    "strategy": self.name,
                    "rows": len(frame),
                    "required_candles": required,
                },
            )

        close = frame["close"]
        upper_values = rolling_max(frame["high"], params.entry_period)
        lower_values = rolling_min(frame["low"], params.exit_period)
        true_range_average = atr(frame["high"], frame["low"], close, params.atr_period)
        frame["donchian_upper"] = upper_values.to_numpy(dtype="float64")
        frame["donchian_lower"] = lower_values.to_numpy(dtype="float64")
        frame["atr"] = true_range_average.to_numpy(dtype="float64")
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        Every period of this strategy is expressed in candles, so the declared
        warm-up is ``max(entry_period, exit_period) + 2`` **on every grid**: the
        entry channel needs a full window before it can be shifted, the exit
        channel a full window of its own, and the stop needs the first defined
        ATR.  On the default parameters (20 / 10) that is 22 candles on a 1m, 1h,
        4h and 1d grid alike — the ``candles_per_day`` argument is therefore
        irrelevant here and is accepted only to honour the signature of
        :meth:`Strategy.required_candles`.

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
            ``max(entry_period, exit_period) + 2`` candles.
        """
        return _warmup(self._donchian_params)

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a prepared frame.

        The input must carry the OHLCV columns **and** the indicator columns
        produced by :meth:`prepare`.  The computation is pure and repeatable:
        the input frame is never mutated, and a ``NaN`` channel never compares
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

        params = self._donchian_params
        close = data["close"].astype("float64")
        # ``shift(1)`` compares the close against the PREVIOUS completed channel:
        # a window that already contains the current candle would be circular.
        upper_values = data["donchian_upper"].astype("float64").shift(1)
        lower_values = data["donchian_lower"].astype("float64").shift(1)
        true_range_average = data["atr"].astype("float64")
        multiplier = params.atr_stop_multiplier

        entry_long = close > upper_values
        exit_long = close < lower_values
        if params.allow_short:
            entry_short = close < lower_values
            exit_short = close > upper_values
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


def _warmup(params: DonchianStrategyParams) -> int:
    """Return the candles :meth:`DonchianStrategy.prepare` needs to warm up.

    ``donchian_upper`` produces its first value on row ``entry_period - 1`` and
    is consumed through ``shift(1)``, so the first comparable upper channel sits
    on row ``entry_period``; ``donchian_lower`` is defined from row
    ``exit_period - 1``; the ATR from row ``atr_period``.  ``+ 2`` covers the
    shifted entry channel and the exit channel with one row of slack, so the
    declared number is a safe floor on every grid.

    :meth:`DonchianStrategy.prepare` and
    :meth:`DonchianStrategy.required_candles` both go through this single
    helper, so the warm-up actually enforced and the warm-up declared can never
    drift apart.  The function is pure and never raises for a validated
    :class:`DonchianStrategyParams`.
    """
    return max(params.entry_period, params.exit_period) + 2
