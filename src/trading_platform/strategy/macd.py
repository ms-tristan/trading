"""The ``macd`` strategy: classic MACD signal-line trend following.

Status — **hypothesis under test** (the repository's recorded verdict matters)
---------------------------------------------------------------------------
The research protocol of this repository (``docs/strategies.md`` and
``.scratch/research/FINDINGS*.md``) **never swept the MACD family**: there is no
walk-forward result, no Monte-Carlo result and no recorded verdict for it.  The
honest label is therefore "hypothesis under test" — *not* research-validated,
whatever the popularity of the rule.  The hypothesis encoded here is the
classical one:

    a trend that is strong enough to keep the MACD line above its own signal
    line for a sustained stretch also survives the platform's cost model on the
    coarse grids (1d, 4h).

The strategy exists so the operator can run it **side by side** with the
research-validated ``momentum`` and with the other house strategies on the very
same ledger, and so the claim above can eventually be measured instead of
assumed.

Canonical reference
    Gerald Appel's Moving Average Convergence Divergence (MACD, 1979):
    ``EMA(close, 12) - EMA(close, 26)`` plus a 9-period EMA of that difference,
    the "signal line".  This module implements that rule with the platform's own
    :func:`~trading_platform.strategy.indicators.ema` and the usual defaults
    (12 / 26 / 9).

Rules (they are frozen — the engine, the validation layer and the documentation
all rely on them):

``macd_line``
    ``ema(close, fast_period) - ema(close, slow_period)``.

``macd_signal``
    ``ema(macd_line, signal_period)`` — the signal line is the EMA of the *MACD
    line*, trailing it by construction.  The input already carries the ``NaN``
    prefix of the two EMAs it is built from, and the platform's ``ema`` simply
    skips that prefix (pandas' ``min_periods`` counts the **non-``NaN``**
    observations), exactly like every published MACD implementation: the first
    ``NaN`` rows of the difference never enter the smoothing.

``macd_histogram``
    ``macd_line - macd_signal`` — the signed distance between the two lines.

``entry_long``
    ``macd_line > macd_signal``.

``exit_long``
    ``macd_line < macd_signal``.

``entry_short`` / ``exit_short``
    the exact mirror images (``macd_line < macd_signal`` and
    ``macd_line > macd_signal``).  They are all ``False`` unless ``allow_short``
    is ``True``.

``stop_loss``
    ``close - atr_stop_multiplier * atr`` (``NaN`` while the ATR is not yet
    defined, i.e. "no stop").  A multiplier of exactly ``0.0`` means "no stop at
    all": the whole column is then ``NaN`` — never a stop at the close.  The
    value is the *long* stop price; for a short entry the engine mirrors the same
    distance about the entry candle's close (see
    :mod:`trading_platform.strategy.engine`).

Two properties matter for a faithful reading of the rules:

* the entries are **state**, not crossover events.  ``entry_long`` is simply
  "the MACD line is above its signal line on this candle", so it stays ``True``
  on **every** candle of a run and does not only fire on the candle where the
  two lines crossed — exactly like ``momentum``, and unlike ``basic`` whose
  entries really are crossover events;
* ``NaN`` never fires a signal: while either line is still undefined every
  comparison against it is ``False`` (the lines are deliberately **not** filled
  with ``0``).

All periods are expressed in **candles** (never in days), so the declared warm-up
is a small, grid-independent constant and the platform's default 200-candle
budget can feed most of the parameter grid.

Warm-up (declared, enforced, never silent)
    The three exponential averages are chained, and :func:`ema` only defines its
    first value once it has seen ``period`` observations, so the fast line needs
    ``fast_period`` rows, the slow line ``slow_period`` rows, and the signal line
    a further ``signal_period`` rows on top of the MACD difference.  The declared
    warm-up is therefore ``fast_period + slow_period + signal_period`` candles
    (47 on the defaults) — the number :meth:`MacdStrategy.required_candles`
    returns, the number :meth:`MacdStrategy.prepare` warns about, and the number
    the profile catalogue budgets ``warmup_candles`` with.

    That sum is a deliberately **conservative** stacking of the three lookbacks:
    it is a safe upper bound, not the exact first row on which the signal line
    becomes defined, and the difference is worth stating instead of glossing
    over.  Because :func:`ema` skips the ``NaN`` prefix of its input, the signal
    line is already defined at index ``slow_period + signal_period - 2``
    (``fast_period`` rows earlier than the declared floor).  Over-declaring is
    safe — a profile is only accepted when its warm-up covers the declared
    requirement — while under-declaring would silently starve it, so the floor
    keeps the conservative sum and the tests pin both numbers explicitly.
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

__all__ = ["MacdParams", "MacdStrategy", "MacdStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`MacdStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = ("macd_line", "macd_signal", "macd_histogram", "atr")


class MacdStrategyParams(StrategyParams):
    """Validated parameters of :class:`MacdStrategy`.

    The declaration order is part of the contract: it drives the order in which
    the Freqtrade adapter exposes the hyperoptable parameters.
    """

    fast_period: int = Field(default=12, ge=2)
    slow_period: int = Field(default=26, ge=3)
    signal_period: int = Field(default=9, ge=2)
    atr_period: int = Field(default=14, ge=2)
    atr_stop_multiplier: float = Field(default=3.0, ge=0.0)
    allow_short: bool = False

    @model_validator(mode="after")
    def _check_coherence(self) -> MacdStrategyParams:
        """Enforce the cross-field invariant: the slow EMA must be the slow one."""
        if self.slow_period <= self.fast_period:
            raise ValueError(
                f"slow_period ({self.slow_period}) must be greater than fast_period "
                f"({self.fast_period})"
            )
        return self


#: Short spelling of :class:`MacdStrategyParams`.
#:
#: ``MacdStrategyParams`` follows the house naming convention of
#: :class:`~trading_platform.strategy.basic.BasicStrategyParams`
#: (``<StrategyClass>Params``); ``MacdParams`` is kept as the equivalent short
#: public name.  The two names denote **the very same class**: there is exactly
#: one validated parameter model per strategy.
MacdParams = MacdStrategyParams


@register_strategy
class MacdStrategy(Strategy):
    """MACD signal-line trend follower with an optional ATR tail stop."""

    name: ClassVar[str] = "macd"
    ParamsModel: ClassVar[type[StrategyParams]] = MacdStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "fast_period": [8, 12],
        "slow_period": [21, 26],
        "signal_period": [9, 12],
        "atr_stop_multiplier": [0.0, 3.0],
    }

    @property
    def _macd_params(self) -> MacdStrategyParams:
        """The parameters, typed as :class:`MacdStrategyParams`."""
        return cast(MacdStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the MACD columns and ``atr`` to a copy of ``data``.

        ``macd_line`` is the difference of the fast and the slow EMA of the
        close, ``macd_signal`` the EMA of that difference and ``macd_histogram``
        their signed difference.  Every input column is copied unchanged and the
        index is preserved exactly; ``data`` itself is never mutated.

        A frame shorter than :meth:`required_candles` cannot warm up under the
        declared contract: the signal line is ``NaN`` on every row and no entry
        or exit can fire.  That is legitimate in walk-forward windows and in unit
        tests, so the method logs a single structured ``strategy.warmup_incomplete``
        **warning** and keeps going — it never raises for a short frame, and it
        stays completely silent once the frame is long enough.

        Raises
        ------
        StrategyError
            If ``data`` does not satisfy the OHLCV contract.
        """
        params = self._macd_params
        frame = require_ohlcv_frame(data, name="data")
        required = _warmup(params)
        if len(frame) < required:
            _LOGGER.warning(
                "strategy %s cannot warm up on this frame: %d row(s) received,"
                " %d required (fast %d + slow %d + signal %d candles)",
                self.name,
                len(frame),
                required,
                params.fast_period,
                params.slow_period,
                params.signal_period,
                extra={
                    "event": "strategy.warmup_incomplete",
                    "strategy": self.name,
                    "rows": len(frame),
                    "required_candles": required,
                },
            )

        close = frame["close"]
        fast = ema(close, params.fast_period)
        slow = ema(close, params.slow_period)
        line = fast - slow
        # The signal EMA is computed on the already-NaN-prefixed line, exactly
        # like the published rule: ``ema`` skips the undefined prefix instead of
        # treating it as a zero observation.
        signal_line = ema(line, params.signal_period)
        frame["macd_line"] = line.to_numpy(dtype="float64")
        frame["macd_signal"] = signal_line.to_numpy(dtype="float64")
        frame["macd_histogram"] = (line - signal_line).to_numpy(dtype="float64")
        frame["atr"] = atr(frame["high"], frame["low"], close, params.atr_period).to_numpy(
            dtype="float64"
        )
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        Every lookback of this strategy is expressed in **candles**, so the
        declared warm-up is the constant ``fast_period + slow_period +
        signal_period`` candles (47 on the defaults) whatever the grid: the
        argument is accepted for interface compatibility and deliberately
        ignored.  The value is a conservative stacking of the three lookbacks
        (see the module docstring), which is safe because the platform only ever
        compares it to ``warmup_candles`` as a *minimum*.

        The method is pure, grid-independent and never raises; it reads nothing
        but the validated parameters and shares :func:`_warmup` with
        :meth:`prepare`, so the declared warm-up can never drift from the
        lookbacks actually computed.

        Parameters
        ----------
        candles_per_day:
            How many candles of the target frame fit in one 24-hour day.  It is
            irrelevant here (all periods are candles) and is ignored.

        Returns
        -------
        int
            ``fast_period + slow_period + signal_period`` candles.
        """
        return _warmup(self._macd_params)

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

        params = self._macd_params
        line = data["macd_line"].astype("float64")
        signal_line = data["macd_signal"].astype("float64")
        close = data["close"].astype("float64")
        true_range_average = data["atr"].astype("float64")
        multiplier = params.atr_stop_multiplier

        # State, not event: the comparison holds on every candle of a run.
        entry_long = line > signal_line
        exit_long = line < signal_line
        if params.allow_short:
            entry_short = line < signal_line
            exit_short = line > signal_line
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


def _warmup(params: MacdStrategyParams) -> int:
    """Return the declared warm-up of ``params``, in candles.

    ``fast_period + slow_period + signal_period``: the conservative stacking of
    the three exponential lookbacks (see the module docstring).  Both
    :meth:`MacdStrategy.prepare` and :meth:`MacdStrategy.required_candles` go
    through this single helper, so the declared warm-up can never drift from the
    lookbacks actually computed.

    The function is pure and never raises for a validated
    :class:`MacdStrategyParams`.
    """
    return params.fast_period + params.slow_period + params.signal_period
