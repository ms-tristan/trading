"""The ``bollinger`` mean-reversion strategy — a published rule *under test*.

Status: **hypothesis under test** — this strategy is delivered so the operator can
run it side by side with the research-validated ``momentum`` strategy and compare
them on the same data.  It is **not** validated, and nothing here may be read as
evidence that it is: the repository's own protocol evaluated the family and
recorded it as **rejected** (see "The recorded verdict" below).

The hypothesis
    A close below the lower Bollinger band is a short-term over-extension of the
    price around its own moving average, and the price tends to revert to that
    average.  The rule is therefore a *counter-trend* one: it buys weakness and
    takes its profit at the middle band instead of waiting for a symmetric
    overshoot of the upper band.  It is long-only spot by default
    (``allow_short = False``), which is the shape the family was measured in.

The canonical rule implemented here
    John Bollinger's bands: a **20-period simple moving average** of the close,
    plus and minus **two population standard deviations** of the same window
    (John Bollinger, *Bollinger on Bollinger Bands*, 2001).  The published entry
    is a tag of the lower band, and the published exit is the return of the price
    to the middle band; both are implemented literally, with the band width
    exposed as ``num_std`` (default ``2.0``) and the window as ``period``
    (default ``20``).

The recorded verdict (``.scratch/research/FINDINGS2.md``, ``STATS_AUDIT.md``)
    The Bollinger reversion variant V1 (1h grid, long-only) reached a DEV Sharpe
    of **0.706** with a maximum drawdown of **-0.32**; on the holdout its basket
    returned **+14.4 %** against **+37.5 %** for buy & hold (Sharpe **0.404**).
    It also failed the pre-registered parameter-plateau rule on DEV
    (**0/108** cells) and did not recover out of sample.  The family is therefore
    recorded as **rejected by the protocol**: this module ships the rule, not a
    claim of edge, and the operator must treat any green backtest as a
    measurement rather than a result.

Rules (frozen — the engine, the tests and the documentation rely on them):

``bollinger_mid`` / ``bollinger_std`` / ``bollinger_upper`` / ``bollinger_lower``
    ``sma(close, period)`` and the rolling population standard deviation of the
    close over the same window, combined into
    ``upper = mid + num_std * std`` and ``lower = mid - num_std * std``.
    ``bollinger_std`` is the deviation the band width is built on, stored as its
    own column so the rule can be read (and audited) without recomputing it.
    The estimator is the **population** one (``ddof = 0``): it is pandas' own
    default for a rolling standard deviation, it is the width the published rule
    is defined with, and it is what
    :func:`trading_platform.strategy.indicators.bollinger_bands` — the primitive
    this module consumes — computes.  The sample estimator (``ddof = 1``) would
    widen every band by ``sqrt(period / (period - 1))`` and is deliberately **not**
    used, so the width is decided in exactly two places: the indicator module and
    this docstring.

``atr``
    Wilder's Average True Range over ``atr_period`` candles, used only by the
    ``stop_loss`` column.

``entry_long``
    ``close < bollinger_lower`` — strictly below the lower band, never *at* it.

``exit_long``
    ``close >= bollinger_mid`` — the return of the price to the middle band.

``entry_short`` / ``exit_short``
    the exact mirror images (``close > bollinger_upper`` and
    ``close <= bollinger_mid``).  They are all ``False`` unless ``allow_short``
    is ``True``.

``stop_loss``
    ``close - atr_stop_multiplier * atr`` (``NaN`` while the ATR is not yet
    defined, i.e. "no stop").  A multiplier of exactly ``0.0`` means "no stop at
    all": the whole column is then ``NaN`` — never a stop at the close.  The
    value is the *long* stop price; for a short entry the engine mirrors the same
    distance about the entry candle's close (see
    :mod:`trading_platform.strategy.engine`).

Two properties matter for a faithful reading of the rules:

* the entries are **state**, not crossover events — ``entry_long`` stays ``True``
  on every candle that closes below the lower band, it does not only fire on the
  candle that crossed it;
* ``NaN`` never fires a signal: while the bands are still undefined the
  comparisons are ``False`` and the indicator is deliberately **not** filled with
  a neutral value.

Warm-up (declared, enforced, never silent)
    Every period of this strategy is expressed in **candles** — not in days, as
    ``momentum`` does — so the declared warm-up is grid-independent: the bands
    need ``period`` candles and the ATR needs ``atr_period + 1``, hence
    ``max(period, atr_period) + 1`` candles before **any** signal can fire (21 on
    the default parameters).  Nothing is converted with the frame's candle grid,
    and ``required_candles`` ignores its ``candles_per_day`` argument for exactly
    that reason.  :meth:`BollingerStrategy.prepare` logs one structured
    ``strategy.warmup_incomplete`` warning when it is handed a shorter frame; a
    short frame is **not** an error (walk-forward windows and unit tests
    legitimately use them), so it never raises for that reason.

Parameters carry per-field bounds only
    ``period``, ``num_std``, ``atr_period``, ``atr_stop_multiplier`` and
    ``allow_short`` are mutually independent: no combination of them is
    incoherent (any window works with any width and any stop distance), so
    :class:`BollingerStrategyParams` deliberately has **no cross-field
    validator** — unlike the ordered lookbacks of
    :class:`~trading_platform.strategy.momentum.MomentumStrategyParams`.
"""

from __future__ import annotations

import logging
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
from trading_platform.strategy.indicators import atr, bollinger_bands, rolling_std
from trading_platform.strategy.registry import register_strategy

__all__ = ["BollingerParams", "BollingerStrategy", "BollingerStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`BollingerStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = (
    "bollinger_mid",
    "bollinger_std",
    "bollinger_upper",
    "bollinger_lower",
    "atr",
)


class BollingerStrategyParams(StrategyParams):
    """Validated parameters of :class:`BollingerStrategy`.

    The declaration order is part of the contract: it drives the order in which
    the Freqtrade adapter exposes the hyperoptable parameters.

    Every field carries a per-field bound and nothing else.  The five parameters
    are mutually independent — any window works with any band width, any ATR
    period and any stop distance — so this model has **no cross-field
    validator** on purpose.
    """

    period: int = Field(default=20, ge=2)
    num_std: float = Field(default=2.0, gt=0.0)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=3.0, ge=0.0)
    allow_short: bool = False


#: Short spelling of :class:`BollingerStrategyParams`.
#:
#: ``BollingerStrategyParams`` follows the house naming convention
#: (``<StrategyClass>Params``); ``BollingerParams`` is kept as the equivalent
#: short public name.  The two names denote **the very same class**: there is
#: exactly one validated parameter model per strategy.
BollingerParams = BollingerStrategyParams


@register_strategy
class BollingerStrategy(Strategy):
    """Bollinger band mean reversion: buy the lower band, exit at the middle band."""

    name: ClassVar[str] = "bollinger"
    ParamsModel: ClassVar[type[StrategyParams]] = BollingerStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "period": [10, 20, 30],
        "num_std": [1.5, 2.0, 2.5],
        "atr_stop_multiplier": [0.0, 3.0],
    }

    @property
    def _bollinger_params(self) -> BollingerStrategyParams:
        """The parameters, typed as :class:`BollingerStrategyParams`."""
        return cast(BollingerStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the Bollinger columns and ``atr`` to a copy of ``data``.

        The OHLCV columns of the input are copied unchanged and the index is
        preserved exactly; ``data`` itself is never mutated.  ``bollinger_std``
        is the very deviation column the band width is built on (the population
        estimator, ``ddof = 0``), stored so the rule stays auditable.

        A frame shorter than :meth:`required_candles` cannot warm up: the bands
        stay ``NaN`` on every row and no entry or exit can ever fire.  That is
        legitimate in walk-forward windows and in unit tests, so the method logs
        a single structured ``strategy.warmup_incomplete`` **warning** and keeps
        going — it never raises for a short frame, and it stays completely
        silent once the frame is long enough.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._bollinger_params
        frame = require_ohlcv_frame(data, name="data")
        required = _warmup(params)
        if len(frame) < required:
            _LOGGER.warning(
                "strategy %s cannot warm up on this frame: %d row(s) received,"
                " %d required (longest lookback %d candles + 1)",
                self.name,
                len(frame),
                required,
                required - 1,
                extra={
                    "event": "strategy.warmup_incomplete",
                    "strategy": self.name,
                    "rows": len(frame),
                    "required_candles": required,
                },
            )

        close = frame["close"]
        bands = bollinger_bands(close, params.period, params.num_std)
        frame["bollinger_mid"] = bands.middle.to_numpy(dtype="float64")
        frame["bollinger_std"] = rolling_std(close, params.period).to_numpy(dtype="float64")
        frame["bollinger_upper"] = bands.upper.to_numpy(dtype="float64")
        frame["bollinger_lower"] = bands.lower.to_numpy(dtype="float64")
        frame["atr"] = atr(frame["high"], frame["low"], close, params.atr_period).to_numpy(
            dtype="float64"
        )
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        Every period of this strategy is expressed in **candles**, so the answer
        is grid-independent: ``max(period, atr_period) + 1`` candles (21 on the
        default parameters) on a 1m, 1h, 4h or 1d grid alike.  The
        ``candles_per_day`` argument only exists because
        :meth:`~trading_platform.strategy.base.Strategy.required_candles`
        declares it — it is deliberately **ignored** here, which also makes the
        method total: any value (a degenerate grid, ``NaN``, ``None``, a string)
        answers the same number and it never raises.

        The method is pure and shares :func:`_warmup` with :meth:`prepare`, so
        the declared warm-up can never drift from the lookbacks actually
        computed.

        Parameters
        ----------
        candles_per_day:
            How many candles of the target frame fit in one 24-hour day.  It has
            no effect on a candle-based strategy and is accepted for interface
            compatibility only.

        Returns
        -------
        int
            ``max(period, atr_period) + 1`` candles.
        """
        return _warmup(self._bollinger_params)

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a prepared frame.

        The input must carry the OHLCV columns **and** the indicator columns
        produced by :meth:`prepare`.  The computation is pure and repeatable:
        the input frame is never mutated, and a ``NaN`` indicator never compares
        ``True`` (it is never filled with a neutral value).

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

        params = self._bollinger_params
        close = data["close"].astype("float64")
        middle = data["bollinger_mid"].astype("float64")
        upper = data["bollinger_upper"].astype("float64")
        lower = data["bollinger_lower"].astype("float64")
        true_range_average = data["atr"].astype("float64")
        multiplier = params.atr_stop_multiplier

        entry_long = close < lower
        exit_long = close >= middle
        if params.allow_short:
            entry_short = close > upper
            exit_short = close <= middle
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


def _warmup(params: BollingerStrategyParams) -> int:
    """Return the candles :class:`BollingerStrategy` needs before it can signal.

    The bands are defined from the ``period``-th candle onwards and the ATR from
    the ``atr_period``-th, so the first row on which **every** column the rule
    reads is defined is ``max(period, atr_period)``; the ``+ 1`` is the candle
    that follows that lookback, exactly like
    :meth:`~trading_platform.strategy.momentum.MomentumStrategy.required_candles`.
    Both :meth:`BollingerStrategy.prepare` and
    :meth:`BollingerStrategy.required_candles` go through this single helper, so
    the declared warm-up can never drift from the computed lookbacks.

    The function is pure and never raises for a validated
    :class:`BollingerStrategyParams`.
    """
    return max(params.period, params.atr_period) + 1
