"""The research-validated multi-horizon ``momentum`` strategy.

Rules (they are frozen — the engine, the validation layer and the documentation
all rely on them):

``momentum_fast`` / ``momentum_mid`` / ``momentum_slow``
    ``roc(close, horizon)`` — the rate of change of the close over three
    distinct lookbacks.  The horizons are expressed in **days** and converted
    into candle counts from the frame's own candle grid, so the very same
    parameters apply to a 1h, 4h or 1d series (see :func:`_candles_per_day`).

``momentum_score``
    ``(sign(momentum_fast) + sign(momentum_mid) + sign(momentum_slow)) / 3``,
    i.e. a value in ``{-1, -2/3, -1/3, 0, 1/3, 2/3, 1}`` counting how many of the
    three horizons agree on the direction of the market.  ``enter_score = 0.6``
    therefore means "at least two of the three horizons agree".

``entry_long``
    ``momentum_score >= enter_score``.

``exit_long``
    ``momentum_score <= exit_score``.

``entry_short`` / ``exit_short``
    the exact mirror images with the opposite sign (``momentum_score <=
    -enter_score`` and ``momentum_score >= -exit_score``).  They are all
    ``False`` unless ``allow_short`` is ``True``.

``stop_loss``
    ``close - atr_stop_multiplier * atr`` (``NaN`` while the ATR is not yet
    defined, i.e. "no stop").  A multiplier of exactly ``0.0`` means "no stop at
    all": the whole column is then ``NaN``.  The value is the *long* stop price;
    for a short entry the engine mirrors the same distance about the entry
    candle's close (see :mod:`trading_platform.strategy.engine`).

Two properties matter for a faithful reading of the rules:

* the entries are **state**, not crossover events — ``entry_long`` stays ``True``
  on every candle whose score holds, it does not only fire on the candle that
  crossed the threshold;
* ``NaN`` never fires a signal: while any of the three horizons is still
  undefined the score is ``NaN`` and every comparison against it is ``False``
  (the score is deliberately **not** filled with ``0``).

Warm-up (declared, enforced, never silent)
    ``roc(close, horizon)`` loses its first ``horizon`` values, so the score is
    defined only from the candle that follows the longest lookback: a frame
    must hold ``max(fast, mid, slow) + 1`` candles before this strategy can emit
    **any** signal.  On the default parameters that is 40 321 rows on a 1m grid,
    673 on 1h, 169 on 4h and 29 on 1d.
    :meth:`MomentumStrategy.required_candles` declares that number (so the
    platform can refuse a profile it could never feed, or report it through
    ``realtime check``), and :meth:`MomentumStrategy.prepare` logs one
    structured ``strategy.warmup_incomplete`` warning when it is handed a
    shorter frame.  A short frame is **not** an error — walk-forward windows and
    unit tests legitimately use them — so ``prepare`` never raises for that
    reason: it only makes the situation visible.
"""

from __future__ import annotations

import logging
import math
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
from trading_platform.strategy.indicators import atr, roc

__all__ = ["MomentumParams", "MomentumStrategy", "MomentumStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`MomentumStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = (
    "momentum_fast",
    "momentum_mid",
    "momentum_slow",
    "momentum_score",
    "atr",
    "candles_per_day",
)


class MomentumStrategyParams(StrategyParams):
    """Validated parameters of :class:`MomentumStrategy`.

    The declaration order is part of the contract: it drives the order in which
    the Freqtrade adapter exposes the hyperoptable parameters.
    """

    fast_days: int = Field(default=7, ge=1)
    mid_days: int = Field(default=14, ge=2)
    slow_days: int = Field(default=28, ge=3)
    enter_score: float = Field(default=0.6, gt=0.0, le=1.0)
    exit_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=4.0, ge=0.0)
    allow_short: bool = False

    @model_validator(mode="after")
    def _check_coherence(self) -> MomentumStrategyParams:
        """Enforce the three cross-field invariants of the strategy."""
        issues: list[str] = []
        if self.mid_days <= self.fast_days:
            issues.append(
                f"mid_days ({self.mid_days}) must be greater than fast_days ({self.fast_days})"
            )
        if self.slow_days <= self.mid_days:
            issues.append(
                f"slow_days ({self.slow_days}) must be greater than mid_days ({self.mid_days})"
            )
        if self.exit_score >= self.enter_score:
            issues.append(
                f"exit_score ({self.exit_score}) must be lower than enter_score "
                f"({self.enter_score})"
            )
        if issues:
            raise ValueError("; ".join(issues))
        return self


#: Short spelling of :class:`MomentumStrategyParams`.
#:
#: ``MomentumStrategyParams`` follows the house naming convention of
#: :class:`~trading_platform.strategy.basic.BasicStrategyParams`
#: (``<StrategyClass>Params``); ``MomentumParams`` is kept as the equivalent
#: short public name.  The two names denote **the very same class**: there is
#: exactly one validated parameter model per strategy.
MomentumParams = MomentumStrategyParams


class MomentumStrategy(Strategy):
    """Multi-horizon momentum strategy with an optional ATR tail stop."""

    name: ClassVar[str] = "momentum"
    ParamsModel: ClassVar[type[StrategyParams]] = MomentumStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "fast_days": [5, 7, 10],
        "mid_days": [14, 20],
        "slow_days": [28, 40, 56],
        "atr_stop_multiplier": [0.0, 4.0],
    }

    @property
    def _momentum_params(self) -> MomentumStrategyParams:
        """The parameters, typed as :class:`MomentumStrategyParams`."""
        return cast(MomentumStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the momentum columns and ``atr`` to a copy of ``data``.

        The three day-based lookbacks are converted into candle counts from the
        frame's own grid, so the same parameters mean the same economic horizon
        on every timeframe.  The OHLCV columns of the input are copied unchanged
        and the index is preserved exactly; ``data`` itself is never mutated.

        A frame shorter than :meth:`required_candles` cannot warm up: the score
        stays ``NaN`` on every row and no entry or exit can ever fire.  That is
        legitimate in walk-forward windows and in unit tests, so the method logs
        a single structured ``strategy.warmup_incomplete`` **warning** and keeps
        going — it never raises for a short frame, and it stays completely
        silent once the frame is long enough.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._momentum_params
        frame = require_ohlcv_frame(data, name="data")
        # ``require_ohlcv_frame`` guarantees a DatetimeIndex; the cast only
        # restates that guarantee for the type checker.
        per_day = _candles_per_day(cast(pd.DatetimeIndex, frame.index))
        fast_horizon, mid_horizon, slow_horizon = _horizons(params, per_day)
        required = max(fast_horizon, mid_horizon, slow_horizon) + 1
        if len(frame) < required:
            _LOGGER.warning(
                "strategy %s cannot warm up on this frame: %d row(s) received,"
                " %d required (%g candles/day; longest lookback %d candles + 1)",
                self.name,
                len(frame),
                required,
                per_day,
                required - 1,
                extra={
                    "event": "strategy.warmup_incomplete",
                    "strategy": self.name,
                    "rows": len(frame),
                    "required_candles": required,
                    "candles_per_day": per_day,
                },
            )

        close = frame["close"]
        fast = roc(close, fast_horizon)
        mid = roc(close, mid_horizon)
        slow = roc(close, slow_horizon)
        frame["momentum_fast"] = fast.to_numpy(dtype="float64")
        frame["momentum_mid"] = mid.to_numpy(dtype="float64")
        frame["momentum_slow"] = slow.to_numpy(dtype="float64")
        # ``sign(NaN)`` is ``NaN``, so the score stays ``NaN`` while any horizon
        # is undefined and can never be mistaken for the neutral ``0``.
        score = (np.sign(fast) + np.sign(mid) + np.sign(slow)) / 3.0
        frame["momentum_score"] = score.to_numpy(dtype="float64")
        frame["atr"] = atr(frame["high"], frame["low"], close, params.atr_period).to_numpy(
            dtype="float64"
        )
        frame["candles_per_day"] = np.full(len(frame), per_day, dtype="float64")
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        ``roc(close, horizon)`` is undefined on the first ``horizon`` rows, so
        the score — and therefore every entry and every exit — needs
        ``max(fast, mid, slow) + 1`` candles, the three lookbacks being the
        day-based parameters converted with the frame's candle grid.  On the
        default parameters (7 / 14 / 28 days) that is 40 321 rows on a 1m grid,
        8 065 on 5m, 2 689 on 15m, 1 345 on 30m, 673 on 1h, 169 on 4h and 29 on
        1d; with the default ``candles_per_day`` (a daily grid) it is 29.

        The method is pure and never raises: a non-finite, zero or negative
        ``candles_per_day`` is treated as ``1.0``, and it reads nothing but the
        validated parameters.  It shares :func:`_horizons` with :meth:`prepare`,
        so the declared warm-up can never drift from the computed lookbacks.

        Parameters
        ----------
        candles_per_day:
            How many candles of the target frame fit in one 24-hour day (1m
            ``1440``, 5m ``288``, 15m ``96``, 30m ``48``, 1h ``24``, 4h ``6``,
            1d ``1``).

        Returns
        -------
        int
            ``max(fast, mid, slow) + 1`` candles.
        """
        try:
            per_day = float(candles_per_day)
        except (TypeError, ValueError):
            per_day = 1.0
        if not math.isfinite(per_day) or per_day <= 0.0:
            per_day = 1.0
        return max(_horizons(self._momentum_params, per_day)) + 1

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a prepared frame.

        The input must carry the OHLCV columns **and** the indicator columns
        produced by :meth:`prepare`.  The computation is pure and repeatable:
        the input frame is never mutated, and a ``NaN`` score never compares
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

        params = self._momentum_params
        score = data["momentum_score"].astype("float64")
        true_range_average = data["atr"].astype("float64")
        close = data["close"].astype("float64")
        multiplier = params.atr_stop_multiplier

        entry_long = score >= params.enter_score
        exit_long = score <= params.exit_score
        if params.allow_short:
            entry_short = score <= -params.enter_score
            exit_short = score >= -params.exit_score
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


def _horizons(params: MomentumStrategyParams, per_day: float) -> tuple[int, int, int]:
    """Return the ``(fast, mid, slow)`` lookbacks of ``params`` in candles.

    Each day-based parameter is multiplied by ``per_day`` (the number of candles
    the frame holds in one 24-hour day), rounded to the nearest candle and
    floored at ``1`` so that a very coarse grid still compares two distinct
    candles.  :meth:`MomentumStrategy.prepare` and
    :meth:`MomentumStrategy.required_candles` both go through this single
    helper, so the lookbacks actually computed and the warm-up declared can never
    drift apart.

    The function is pure and never raises for a validated
    :class:`MomentumStrategyParams`, whatever the (finite, positive) grid.
    """
    return (
        max(1, round(params.fast_days * per_day)),
        max(1, round(params.mid_days * per_day)),
        max(1, round(params.slow_days * per_day)),
    )


def _candles_per_day(index: pd.DatetimeIndex) -> float:
    """Return how many candles of ``index`` fit in one 24-hour day.

    The grid is inferred from the frame itself (median spacing of the index), so
    no timeframe has to be passed around: a 1h grid yields ``24.0``, a 4h grid
    ``6.0``, a 1d grid ``1.0``.  A single-row index, a degenerate spacing or a
    grid coarser than one candle a day (e.g. weekly) safely yields ``1.0``.
    """
    if len(index) < 2:
        return 1.0
    spacing = pd.Series(index).diff().dropna().median()
    if pd.isna(spacing) or spacing <= pd.Timedelta(0):
        return 1.0
    minutes = float(spacing.total_seconds() / 60.0)
    if minutes <= 0.0:
        return 1.0
    return float(max(1, round(1440.0 / minutes)))
