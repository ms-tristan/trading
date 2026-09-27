"""Faber's long-term moving-average timing rule — the ``faber`` strategy.

Canonical rule
    Meb Faber, *A Quantitative Approach to Tactical Asset Allocation* (2007,
    revised 2013): an asset is held while its price sits **above** its long-term
    moving average and left alone — the position is closed, the capital stays in
    cash — while its price sits below it.  The published average is a
    **200-day** one (the "10-month moving average" of the paper, ``200`` daily
    closes ≈ 10 months of trading).

Rules (they are frozen — the engine, the validation layer and the documentation
all rely on them):

``faber_sma``
    ``sma(close, sma_period)`` — a simple moving average of the close, measured
    in **candles** and not in days.  On the default parameters that is a
    200-candle average.

``entry_long``
    ``close > faber_sma`` — strictly above; an exact equality fires nothing.

``exit_long``
    ``close < faber_sma`` — strictly below; an exact equality fires nothing.

``entry_short`` / ``exit_short``
    **always all ``False``.**  The published rule is long-only by construction
    (the portfolio is either *in* the asset or *out* of it, never short), so
    this module deliberately carries **no** ``allow_short`` parameter: the
    mirror idiom of the other house strategies collapses to the constant
    ``False`` here.  ``StrategyParams`` is ``extra="forbid"``, so a
    configuration that passes ``allow_short`` is rejected loudly instead of
    being silently ignored.

``stop_loss``
    ``close - atr_stop_multiplier * atr`` (``NaN`` while the ATR is not yet
    defined, i.e. "no stop").  The **default multiplier is ``0.0``**, which means
    "no stop at all": the default ``stop_loss`` column is therefore entirely
    ``NaN``.  An explicit multiplier of exactly ``0.0`` is always read that way —
    never as a stop sitting on the close.

``NaN`` never fires a signal
    While the moving average or the ATR is undefined the comparisons are
    ``False`` (the series are deliberately **not** filled with ``0``).

The published rule is a **200-DAY average**
    ``sma_period`` counts candles, so this implementation reproduces the
    published timing rule on the **1d** grid (200 candles = 200 days) and
    approximates it on the **4h** grid (200 candles = 33 days).  On a 1h grid
    200 candles are barely eight days and the rule is no longer the published
    one; the profile catalogue therefore only places ``faber`` on the 1d and 4h
    grids.

Warm-up (declared, enforced, never silent)
    The simple moving average of ``sma_period`` candles is defined from its
    ``sma_period``-th candle on and the ATR of ``atr_period`` candles from candle
    ``atr_period + 1`` on, so the declared floor is
    ``max(sma_period, atr_period) + 1`` candles — 201 rows on the default
    parameters, whatever the grid.  The floor is exactly the ATR requirement when
    the ATR is the longer lookback and one row above the moving average when the
    SMA is; it is never below either indicator.
    :meth:`FaberStrategy.required_candles` declares that number (so the platform
    can refuse a profile it could never feed) and
    :meth:`FaberStrategy.prepare` logs one structured
    ``strategy.warmup_incomplete`` warning when it is handed a shorter frame.  A
    short frame is **not** an error — walk-forward windows and unit tests
    legitimately use them — so ``prepare`` never raises for that reason: it only
    makes the situation visible.

Status: hypothesis under test — with the repository's recorded verdict
    A price-versus-long-moving-average filter (the Faber-style timing rule) was
    evaluated by the repository's validation protocol and **not carried**: it
    was never profitable in 2 of the 3 DEV folds, and it cleared 0 of the 143
    cells of the stability rule.  The reason is structural rather than
    accidental — a long-only filter is necessarily *in* the market during a bear
    fold once the average turns down only after the drawdown has started, so it
    cannot be positive in a bear fold.  The strategy ships so the operator can
    run it side by side with the validated ones; **it must not be presented as
    validated.**
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
from trading_platform.strategy.indicators import atr, sma
from trading_platform.strategy.registry import register_strategy

__all__ = ["FaberParams", "FaberStrategy", "FaberStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`FaberStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = ("faber_sma", "atr")


class FaberStrategyParams(StrategyParams):
    """Validated parameters of :class:`FaberStrategy`.

    Every period is expressed in **candles**, never in days, so the declared
    warm-up is a fixed candle count on every grid.

    There is deliberately **no** ``allow_short`` field: the published rule is
    long-only by construction, and because :class:`StrategyParams` forbids extra
    keys a configuration carrying ``allow_short`` is rejected loudly instead of
    being silently ignored.

    The declaration order is part of the contract: it drives the order in which
    the Freqtrade adapter exposes the hyperoptable parameters.
    """

    sma_period: int = Field(default=200, ge=2)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=0.0, ge=0.0)


#: Short spelling of :class:`FaberStrategyParams`.
#:
#: ``FaberStrategyParams`` follows the house naming convention of
#: :class:`~trading_platform.strategy.basic.BasicStrategyParams`
#: (``<StrategyClass>Params``); ``FaberParams`` is kept as the equivalent short
#: public name.  The two names denote **the very same class**: there is exactly
#: one validated parameter model per strategy.
FaberParams = FaberStrategyParams


@register_strategy
class FaberStrategy(Strategy):
    """Long-term moving-average timing rule (long-only, in or out of the asset)."""

    name: ClassVar[str] = "faber"
    ParamsModel: ClassVar[type[StrategyParams]] = FaberStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "sma_period": [100, 200],
        "atr_stop_multiplier": [0.0, 3.0],
    }

    @property
    def _faber_params(self) -> FaberStrategyParams:
        """The parameters, typed as :class:`FaberStrategyParams`."""
        return cast(FaberStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the ``faber_sma`` and ``atr`` columns to a copy of ``data``.

        The OHLCV columns of the input are copied unchanged and the index is
        preserved exactly; ``data`` itself is never mutated.

        A frame shorter than :meth:`required_candles` sits below the declared
        floor and may not be able to warm up at all — with the default
        parameters the moving average stays ``NaN`` on every row and no entry or
        exit can ever fire.  That is legitimate in walk-forward windows and in
        unit tests, so the method logs a single structured
        ``strategy.warmup_incomplete`` **warning** and keeps going — it never
        raises for a short frame, and it stays completely silent once the frame
        is long enough.  The comparison is made against the declared floor, so it
        can flag a frame that is one row short of it while the moving average is
        already defined.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._faber_params
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
        frame["faber_sma"] = sma(close, params.sma_period).to_numpy(dtype="float64")
        frame["atr"] = atr(frame["high"], frame["low"], close, params.atr_period).to_numpy(
            dtype="float64"
        )
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        The moving average of ``sma_period`` candles is defined from its
        ``sma_period``-th candle on and the ATR from candle ``atr_period + 1``
        on, so the declared floor is ``max(sma_period, atr_period) + 1``
        candles: ``201`` on the default parameters (``sma_period = 200``,
        ``atr_period = 14``), ``51`` with ``sma_period = 50``.  Every period is
        already expressed in candles, so the answer is the same on every grid.

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
            ``max(sma_period, atr_period) + 1`` candles.
        """
        return _warmup(self._faber_params, candles_per_day)

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a prepared frame.

        The input must carry the OHLCV columns **and** the indicator columns
        produced by :meth:`prepare`.  The computation is pure and repeatable:
        the input frame is never mutated, and a ``NaN`` average never compares
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

        params = self._faber_params
        close = data["close"].astype("float64")
        average = data["faber_sma"].astype("float64")
        true_range_average = data["atr"].astype("float64")
        multiplier = params.atr_stop_multiplier

        entry_long = close > average
        exit_long = close < average
        # The published rule is long-only by construction: there is no
        # ``allow_short`` parameter to read, so the mirror idiom collapses to a
        # constant ``False`` on both short-side columns.
        entry_short = pd.Series(False, index=data.index, dtype=bool)
        exit_short = pd.Series(False, index=data.index, dtype=bool)

        if multiplier == 0.0:
            # 0.0 means "no stop": the column must be entirely NaN, not an ATR
            # distance of zero (which would be a stop at the close).  This is
            # the DEFAULT, so the default stop_loss column carries no stop.
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


def _warmup(params: FaberStrategyParams, candles_per_day: float = 1.0) -> int:
    """Return the candles ``params`` need before any signal can fire.

    The moving average of ``sma_period`` candles is defined from its
    ``sma_period``-th candle on and the ATR of ``atr_period`` candles from candle
    ``atr_period + 1`` on, so the declared floor is
    ``max(sma_period, atr_period) + 1`` rows: exactly the ATR requirement when
    the ATR is the longer lookback, one row above the moving average when the
    SMA is, and never below either.
    :meth:`FaberStrategy.prepare` and
    :meth:`FaberStrategy.required_candles` both go through this single helper, so
    the warm-up actually applied and the warm-up declared can never drift apart.

    ``candles_per_day`` is normalised through :func:`_safe_candles_per_day` so
    that :meth:`FaberStrategy.required_candles` stays **total** — a degenerate
    grid never raises — but it deliberately does **not** scale the answer: every
    period of this strategy is already a candle count, so the declared warm-up
    is the same on the 1m, 1h, 4h and 1d grids.

    The function is pure and never raises for a validated
    :class:`FaberStrategyParams`.
    """
    _ = _safe_candles_per_day(candles_per_day)
    return max(params.sma_period, params.atr_period) + 1


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
