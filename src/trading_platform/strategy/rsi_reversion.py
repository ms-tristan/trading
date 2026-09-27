"""The ``rsi_reversion`` strategy — a published rule *under test*.

Status: **hypothesis under test** — this strategy is delivered so the operator can
run it side by side with the research-validated ``momentum`` strategy and compare
them on the same data.  No Connors-style RSI(2) family was ever swept by the
repository's validation protocol (``docs/strategies.md`` §8 records the families
that were tested and rejected, and this is not one of them), so **no verdict may
be claimed for it, in either direction**: neither validated nor rejected.  The
hypothesis below is stated plainly, and any green backtest is a measurement to be
challenged, not a result.

The hypothesis
    A short-term oversold reading is a temporary liquidity-driven dislocation
    rather than information, **provided** the instrument is still in a longer-term
    uptrend.  The rule therefore buys a deep short-term dip (RSI(2) below
    ``entry_rsi``) but only while the close holds above a long trend average
    (``trend_ema``), and it exits into the snap-back as soon as the oscillator
    recovers above ``exit_rsi``.  The trend filter is the whole point: without it
    the rule would buy every dip of a downtrend, which is how mean reversion
    loses money.

The canonical rule implemented here
    Larry Connors' RSI(2) pullback (*Short Term Trading Strategies That Work*,
    with Cesar Alvarez): buy when the 2-period RSI closes below a low threshold
    while the instrument trades above its long moving average, and sell when the
    RSI closes back above a high threshold.  The published thresholds are
    ``entry_rsi = 10`` and ``exit_rsi = 70`` (the defaults here); the published
    trend filter is a 200-period average of the close, exposed as
    ``trend_ema_period`` (default ``200``).  The original rule uses a simple
    moving average of the close; this implementation uses the exponential one
    (:func:`trading_platform.strategy.indicators.ema`, Wilder/TA-Lib convention)
    because that is the trend primitive the rest of the platform shares — a
    deliberate deviation, recorded here rather than hidden.

Rules (frozen — the engine, the tests and the documentation rely on them):

``rsi``
    Wilder's RSI of the close over ``rsi_period`` candles (default ``2``, the
    Connors lookback).  ``NaN`` on the first ``rsi_period`` rows.

``trend_ema``
    The exponential moving average of the close over ``trend_ema_period``
    candles.  ``NaN`` on the first ``trend_ema_period - 1`` rows.

``atr``
    Wilder's Average True Range over ``atr_period`` candles, used only by the
    ``stop_loss`` column.

``entry_long``
    ``(rsi < entry_rsi) & (close > trend_ema)`` — a short-term oversold reading
    **inside** a long-term uptrend.  Both conditions must hold on the same
    candle.

``exit_long``
    ``rsi > exit_rsi`` — the oscillator recovering is the exit, whatever the
    trend average does.

``entry_short`` / ``exit_short``
    the exact mirror images (``(rsi > 100 - entry_rsi) & (close < trend_ema)``
    and ``rsi < 100 - exit_rsi``).  They are all ``False`` unless ``allow_short``
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
  on every candle that is oversold above the trend average;
* ``NaN`` never fires a signal: while the RSI or the trend average is still
  undefined both comparisons are ``False`` and neither indicator is filled with a
  neutral value.

Warm-up (declared, enforced, never silent)
    Every period of this strategy is expressed in **candles** — not in days, as
    ``momentum`` does — so the declared warm-up is grid-independent:
    ``max(trend_ema_period, atr_period, rsi_period) + 1`` candles before **any**
    signal can fire.  On the default parameters that is **201 candles**, one more
    than the platform's default 200-candle warm-up budget: an honest consequence
    of the published 200-period trend filter, documented here rather than tuned
    down to fit.  A profile of this strategy must therefore declare an explicit
    ``warmup_candles`` (the profile catalogue does); the platform reports the
    mismatch through ``realtime check`` instead of failing silently.
    :meth:`RsiReversionStrategy.prepare` logs one structured
    ``strategy.warmup_incomplete`` warning when it is handed a shorter frame; a
    short frame is **not** an error (walk-forward windows and unit tests
    legitimately use them), so it never raises for that reason.

Note on the module namespace
    ``rsi`` and ``ema`` are imported from the indicator module and are also the
    names of two indicator **columns**.  The columns live inside the frame; the
    computed series are bound to locals named ``rsi_values`` and
    ``trend_ema_values`` so the imported callables are never shadowed.
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
from trading_platform.strategy.indicators import atr, ema, rsi
from trading_platform.strategy.registry import register_strategy

__all__ = ["RsiReversionParams", "RsiReversionStrategy", "RsiReversionStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`RsiReversionStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = ("rsi", "trend_ema", "atr")


class RsiReversionStrategyParams(StrategyParams):
    """Validated parameters of :class:`RsiReversionStrategy`.

    The declaration order is part of the contract: it drives the order in which
    the Freqtrade adapter exposes the hyperoptable parameters.
    """

    rsi_period: int = Field(default=2, ge=2)
    entry_rsi: float = Field(default=10.0, gt=0, lt=100)
    exit_rsi: float = Field(default=70.0, gt=0, le=100)
    trend_ema_period: int = Field(default=200, ge=2)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=3.0, ge=0.0)
    allow_short: bool = False

    @model_validator(mode="after")
    def _check_coherence(self) -> RsiReversionStrategyParams:
        """Enforce the single cross-field invariant of the strategy."""
        if self.exit_rsi <= self.entry_rsi:
            raise ValueError(
                f"exit_rsi ({self.exit_rsi}) must be greater than entry_rsi ({self.entry_rsi})"
            )
        return self


#: Short spelling of :class:`RsiReversionStrategyParams`.
#:
#: ``RsiReversionStrategyParams`` follows the house naming convention
#: (``<StrategyClass>Params``); ``RsiReversionParams`` is kept as the equivalent
#: short public name.  The two names denote **the very same class**: there is
#: exactly one validated parameter model per strategy.
RsiReversionParams = RsiReversionStrategyParams


@register_strategy
class RsiReversionStrategy(Strategy):
    """Connors-style short-term RSI pullback inside a long-term uptrend."""

    name: ClassVar[str] = "rsi_reversion"
    ParamsModel: ClassVar[type[StrategyParams]] = RsiReversionStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "rsi_period": [2, 3, 4],
        "entry_rsi": [5.0, 10.0],
        "exit_rsi": [60.0, 70.0],
        "atr_stop_multiplier": [0.0, 3.0],
    }

    @property
    def _rsi_reversion_params(self) -> RsiReversionStrategyParams:
        """The parameters, typed as :class:`RsiReversionStrategyParams`."""
        return cast(RsiReversionStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add ``rsi``, ``trend_ema`` and ``atr`` to a copy of ``data``.

        The OHLCV columns of the input are copied unchanged and the index is
        preserved exactly; ``data`` itself is never mutated.

        A frame shorter than :meth:`required_candles` cannot warm up: the
        oscillator or the trend average stays ``NaN`` on every row and no entry
        or exit can ever fire.  That is legitimate in walk-forward windows and in
        unit tests, so the method logs a single structured
        ``strategy.warmup_incomplete`` **warning** and keeps going — it never
        raises for a short frame, and it stays completely silent once the frame
        is long enough.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._rsi_reversion_params
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
        # The imported callables ``rsi`` and ``ema`` share their names with the
        # columns below: the results are bound to distinct locals so the
        # callables are never shadowed (ruff F811) and the code stays readable.
        rsi_values = rsi(close, params.rsi_period)
        trend_ema_values = ema(close, params.trend_ema_period)
        frame["rsi"] = rsi_values.to_numpy(dtype="float64")
        frame["trend_ema"] = trend_ema_values.to_numpy(dtype="float64")
        frame["atr"] = atr(frame["high"], frame["low"], close, params.atr_period).to_numpy(
            dtype="float64"
        )
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        Every period of this strategy is expressed in **candles**, so the answer
        is grid-independent: ``max(trend_ema_period, atr_period, rsi_period) + 1``
        candles — **201** on the default parameters — on a 1m, 1h, 4h or 1d grid
        alike.  The ``candles_per_day`` argument only exists because
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
            ``max(trend_ema_period, atr_period, rsi_period) + 1`` candles.
        """
        return _warmup(self._rsi_reversion_params)

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

        params = self._rsi_reversion_params
        close = data["close"].astype("float64")
        oscillator = data["rsi"].astype("float64")
        trend = data["trend_ema"].astype("float64")
        true_range_average = data["atr"].astype("float64")
        multiplier = params.atr_stop_multiplier

        entry_long = (oscillator < params.entry_rsi) & (close > trend)
        exit_long = oscillator > params.exit_rsi
        if params.allow_short:
            entry_short = (oscillator > 100.0 - params.entry_rsi) & (close < trend)
            exit_short = oscillator < 100.0 - params.exit_rsi
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


def _warmup(params: RsiReversionStrategyParams) -> int:
    """Return the candles :class:`RsiReversionStrategy` needs before it can signal.

    The RSI is defined from the ``rsi_period``-th candle onwards, the ATR from
    the ``atr_period``-th and the trend average from the
    ``trend_ema_period``-th, so the first row on which **every** column the rule
    reads is defined is ``max(trend_ema_period, atr_period, rsi_period)``; the
    ``+ 1`` is the candle that follows that lookback, exactly like
    :meth:`~trading_platform.strategy.momentum.MomentumStrategy.required_candles`.
    Both :meth:`RsiReversionStrategy.prepare` and
    :meth:`RsiReversionStrategy.required_candles` go through this single helper,
    so the declared warm-up can never drift from the computed lookbacks.

    The function is pure and never raises for a validated
    :class:`RsiReversionStrategyParams`.
    """
    return max(params.trend_ema_period, params.atr_period, params.rsi_period) + 1
