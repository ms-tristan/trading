"""The ``supertrend`` strategy: the ATR Supertrend trailing-flip trend follower.

Status — **hypothesis under test** (the repository's recorded verdict matters)
---------------------------------------------------------------------------
The research protocol of this repository (``docs/strategies.md`` and
``.scratch/research/FINDINGS*.md``) holds **no verdict at all** for the
Supertrend family: it was never swept, so there is no walk-forward result, no
Monte-Carlo result and nothing to clear.  The honest label is therefore
"hypothesis under test" — *not* research-validated, whatever the popularity of
the indicator.  The hypothesis encoded here is:

    a trailing volatility band that only ever ratchets in the direction of the
    trend is a good enough regime filter: staying long while the band trails
    below the close (and short while it caps the close) captures more of a trend
    than it gives back at the flips, on the coarse grids (1d, 4h).

The rule is **state-based, not event-based**: the signal is the *regime* the
band describes on this candle, not the candle where the band flipped.  A flip is
therefore visible as a change of state between two consecutive candles, never as
a one-candle boolean spike.

Canonical reference
    The ATR Supertrend indicator popularised by Olivier Seban: a trailing band
    ``(high + low) / 2 ± multiplier * ATR`` that ratchets towards the price and
    is only allowed to move in one direction until the close pierces it, at
    which point the band flips side.  This module implements exactly that rule
    with the platform's own
    :func:`~trading_platform.strategy.indicators.atr` (Wilder), the usual
    10-period ATR and a multiplier of 3.

Rules (they are frozen — the engine, the validation layer and the documentation
all rely on them):

``supertrend``
    The trailing line: the *final lower* band while the regime is long
    (``+1``), the *final upper* band while it is short (``-1``).

``supertrend_direction``
    ``+1.0`` in a long regime, ``-1.0`` in a short regime, ``NaN`` while the
    line is not defined yet (never ``0``: "flat" is not a state of this rule).

``atr``
    The Wilder ATR the bands are built from, stored so the rule stays auditable.

``entry_long``
    ``close > supertrend`` (equivalently: the regime is ``+1``).

``exit_long``
    ``close < supertrend`` (the regime flipped to ``-1``).

``entry_short`` / ``exit_short``
    the exact mirror images (``close < supertrend`` and ``close > supertrend``).
    They are all ``False`` unless ``allow_short`` is ``True``.

``stop_loss``
    the trailing line itself, and only while it sits **below** the close:
    ``supertrend.where(supertrend < close)``, ``NaN`` everywhere else.  The
    trailing long line *is* the stop of a long position — that is the whole
    point of the indicator — but when the line is above the close (a short
    regime) it is not a long stop at all, so the column is honestly ``NaN``
    ("no stop") instead of a price on the wrong side of the market.  For a short
    entry the engine mirrors the distance about the entry candle's close (see
    :mod:`trading_platform.strategy.engine`).

    There is deliberately **no** ``atr_stop_multiplier`` parameter on this
    strategy: the stop is the ATR band itself, already scaled by ``multiplier``,
    so the "a multiplier of exactly ``0.0`` means no stop" rule of every other
    house strategy has nothing to apply to here.

Why the bands are computed by an explicit loop (and why they live here)
----------------------------------------------------------------------
The final bands are **recursive**: ``final_upper[t]`` and ``final_lower[t]``
depend on their own value at ``t - 1``, and the direction at ``t`` depends on the
direction at ``t - 1``.  There is no closed form and no window that could be
vectorised: the row at ``t`` cannot be computed before the row at ``t - 1`` has
been.  The computation is therefore **sequential by nature** and is implemented
as one explicit, deterministic ``O(n)`` loop over ``numpy`` ``float64`` arrays —
never as an expanding ``apply`` over pandas rows, which would be orders of
magnitude slower on the 1m grids the platform runs.

It also lives in this module rather than in
:mod:`trading_platform.strategy.indicators` on purpose: every indicator of that
shared kit is stateless and independent per row, while this recursion carries a
cross-row state (the previous final band and the previous direction).  Keeping
it here avoids exporting a stateful primitive from a module whose contract is
"pure, per-row, no state".

Exactly which rule the loop implements
--------------------------------------
::

    basic_upper[t]  = (high[t] + low[t]) / 2 + multiplier * atr[t]
    basic_lower[t]  = (high[t] + low[t]) / 2 - multiplier * atr[t]

    final_upper[t]  = basic_upper[t]  if basic_upper[t] < final_upper[t-1]
                                        or close[t-1] > final_upper[t-1]
                      else final_upper[t-1]
    final_lower[t]  = basic_lower[t]  if basic_lower[t] > final_lower[t-1]
                                        or close[t-1] < final_lower[t-1]
                      else final_lower[t-1]

    direction[t]    = -1 if direction[t-1] == +1 and close[t] < final_lower[t]
                      +1 if direction[t-1] == +1 and close[t] >= final_lower[t]
                      +1 if direction[t-1] == -1 and close[t] > final_upper[t]
                      -1 if direction[t-1] == -1 and close[t] <= final_upper[t]

    supertrend[t]   = final_lower[t] if direction[t] == +1 else final_upper[t]

The recursion is **seeded** on the first candle whose ATR is defined, where
``final_upper = basic_upper``, ``final_lower = basic_lower`` and
``direction = +1``; every earlier candle is ``NaN`` on both output columns
(``direction`` stays ``float64`` ``NaN``, never ``0``).  A candle whose ATR is
not finite *after* the seed (a hole in the input, which a well-formed OHLCV
frame never has) carries the previous final bands forward and keeps the previous
direction: a hole in the data can never silently reset the trailing line.

All periods are expressed in **candles** (never in days), so the declared warm-up
is a small, grid-independent constant and the platform's default 200-candle
budget can feed most of the parameter grid.

Warm-up (declared, enforced, never silent)
    :func:`~trading_platform.strategy.indicators.atr` is Wilder-seeded on the
    ``atr_period``-th candle, so the first candle on which a band — and
    therefore the trailing line — can exist is the one right after it.  The
    declared warm-up is ``atr_period + 2`` candles (12 on the defaults): the
    ``atr_period`` candles the ATR consumes, the seed candle itself, and one more
    candle so that a signal never has to lean on the very candle the recursion
    was seeded with.  :meth:`SupertrendStrategy.required_candles` returns it and
    :meth:`SupertrendStrategy.prepare` warns about it.
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
from trading_platform.strategy.indicators import atr
from trading_platform.strategy.registry import register_strategy

__all__ = ["SupertrendParams", "SupertrendStrategy", "SupertrendStrategyParams"]

#: Module logger.  The strategy layer must not import
#: :mod:`trading_platform.realtime.observability` (``realtime`` may import
#: ``strategy``, never the reverse: the layer direction is frozen), so the
#: warm-up warning is emitted through a plain module logger carrying the same
#: ``event`` extra as the rest of the platform.
_LOGGER = logging.getLogger(__name__)

#: Indicator columns added by :meth:`SupertrendStrategy.prepare`.
INDICATOR_COLUMNS: tuple[str, ...] = ("supertrend", "supertrend_direction", "atr")


class SupertrendStrategyParams(StrategyParams):
    """Validated parameters of :class:`SupertrendStrategy`.

    The declaration order is part of the contract: it drives the order in which
    the Freqtrade adapter exposes the hyperoptable parameters.

    There is deliberately **no** ``atr_stop_multiplier``: the trailing band *is*
    the stop (see the module docstring), so the parameter that carries the
    "exactly ``0.0`` means no stop" rule elsewhere has no meaning here.  The two
    fields are independent — any ATR period works with any band width — so the
    model has no cross-field validator on purpose.
    """

    atr_period: int = Field(default=10, ge=2)
    multiplier: float = Field(default=3.0, gt=0.0)
    allow_short: bool = False


#: Short spelling of :class:`SupertrendStrategyParams`.
#:
#: ``SupertrendStrategyParams`` follows the house naming convention
#: (``<StrategyClass>Params``); ``SupertrendParams`` is kept as the equivalent
#: short public name.  The two names denote **the very same class**: there is
#: exactly one validated parameter model per strategy.
SupertrendParams = SupertrendStrategyParams


@register_strategy
class SupertrendStrategy(Strategy):
    """ATR Supertrend trend follower: long while the band trails below the close."""

    name: ClassVar[str] = "supertrend"
    ParamsModel: ClassVar[type[StrategyParams]] = SupertrendStrategyParams
    PARAM_SPACE: ClassVar[dict[str, list[float | int]]] = {
        "atr_period": [7, 10, 14],
        "multiplier": [2.0, 3.0, 4.0],
    }

    @property
    def _supertrend_params(self) -> SupertrendStrategyParams:
        """The parameters, typed as :class:`SupertrendStrategyParams`."""
        return cast(SupertrendStrategyParams, self.params)

    def prepare(self, data: pd.DataFrame) -> pd.DataFrame:
        """Add the Supertrend columns and ``atr`` to a copy of ``data``.

        The ATR is computed first (the bands are built from it), then the
        recursion of :func:`_supertrend_arrays` produces the trailing line and
        the direction.  The OHLCV columns of the input are copied unchanged and
        the index is preserved exactly; ``data`` itself is never mutated.

        A frame shorter than :meth:`required_candles` cannot warm up: the line
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
        params = self._supertrend_params
        frame = require_ohlcv_frame(data, name="data")
        required = _warmup(params)
        if len(frame) < required:
            _LOGGER.warning(
                "strategy %s cannot warm up on this frame: %d row(s) received,"
                " %d required (atr_period %d candles + seed + 1)",
                self.name,
                len(frame),
                required,
                params.atr_period,
                extra={
                    "event": "strategy.warmup_incomplete",
                    "strategy": self.name,
                    "rows": len(frame),
                    "required_candles": required,
                },
            )

        high = frame["high"]
        low = frame["low"]
        close = frame["close"]
        true_range_average = atr(high, low, close, params.atr_period)
        line, direction = _supertrend_arrays(
            high, low, close, true_range_average, params.multiplier
        )
        frame["supertrend"] = line
        frame["supertrend_direction"] = direction
        frame["atr"] = true_range_average.to_numpy(dtype="float64")
        return frame

    def required_candles(self, candles_per_day: float = 1.0) -> int:
        """Return the rows a frame must hold before this strategy can emit a signal.

        Every period of this strategy is expressed in **candles**, so the answer
        is grid-independent: ``atr_period + 2`` candles (12 on the default
        parameters) on a 1m, 1h, 4h or 1d grid alike.  The ``candles_per_day``
        argument only exists because
        :meth:`~trading_platform.strategy.base.Strategy.required_candles`
        declares it — it is deliberately **ignored** here, which also makes the
        method total: any value (a degenerate grid, ``NaN``, ``None``, a string)
        answers the same number and it never raises.

        The method is pure and shares :func:`_warmup` with :meth:`prepare`, so
        the declared warm-up can never drift from the computation actually
        performed.

        Parameters
        ----------
        candles_per_day:
            How many candles of the target frame fit in one 24-hour day.  It has
            no effect on a candle-based strategy and is accepted for interface
            compatibility only.

        Returns
        -------
        int
            ``atr_period + 2`` candles.
        """
        return _warmup(self._supertrend_params)

    def signals(self, data: pd.DataFrame) -> pd.DataFrame:
        """Return the signal frame of a prepared frame.

        The input must carry the OHLCV columns **and** the indicator columns
        produced by :meth:`prepare`.  The computation is pure and repeatable:
        the input frame is never mutated, and a ``NaN`` line never compares
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

        params = self._supertrend_params
        close = data["close"].astype("float64")
        line = data["supertrend"].astype("float64")

        # State, not event: the comparison holds while the regime lasts.
        entry_long = close > line
        exit_long = close < line
        if params.allow_short:
            entry_short = close < line
            exit_short = close > line
        else:
            entry_short = pd.Series(False, index=data.index, dtype=bool)
            exit_short = pd.Series(False, index=data.index, dtype=bool)

        # The trailing line is the long stop only while it sits below the close;
        # above the close (a short regime) the long stop is "no stop" -> NaN.
        stop_loss = line.where(line < close, np.nan)

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


def _supertrend_arrays(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_values: pd.Series,
    multiplier: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Run the canonical Supertrend recursion and return ``(line, direction)``.

    Both results are freshly allocated ``float64`` arrays indexed like the inputs
    (same length, same positions); the four input series are never mutated.

    The recursion is sequential by nature — ``final_upper[t]`` / ``final_lower[t]``
    depend on their own value at ``t - 1`` and ``direction[t]`` on ``direction[t - 1]``
    — so it is implemented as one explicit ``O(n)`` loop over ``numpy`` arrays
    (never an expanding ``apply`` over pandas rows).  The rule it implements, the
    seeding and the treatment of a non-finite ATR after the seed are documented,
    line by line, in the module docstring.

    The function is pure and deterministic: the same inputs always produce the
    same arrays, and the prefix of a run is independent of the frame length (the
    loop only ever reads rows up to ``t``).

    Parameters
    ----------
    high, low, close:
        The price columns the bands are built from, same length, ``float64``
        after conversion.
    atr_values:
        The ATR column, ``NaN`` on the warm-up prefix (the recursion is seeded on
        the first row where it is finite).
    multiplier:
        Band width in ATR units, a finite number ``> 0``.

    Returns
    -------
    tuple[numpy.ndarray, numpy.ndarray]
        ``(supertrend, direction)``: the trailing line (``float64``, ``NaN``
        before the seed) and the regime (``float64``, only ever ``NaN``, ``+1.0``
        or ``-1.0``).

    Raises
    ------
    StrategyError
        If an input is not a numeric :class:`pandas.Series` of the common length,
        or if ``multiplier`` is not a finite number ``> 0``.
    """
    for name, series in (
        ("high", high),
        ("low", low),
        ("close", close),
        ("atr_values", atr_values),
    ):
        if not isinstance(series, pd.Series):
            raise StrategyError(f"{name} must be a pandas Series, got {type(series).__name__}")
    lengths = {len(high), len(low), len(close), len(atr_values)}
    if len(lengths) > 1:
        raise StrategyError(
            f"high, low, close and atr_values must all have the same length, got {sorted(lengths)}"
        )
    try:
        width = float(multiplier)
    except (TypeError, ValueError):
        raise StrategyError(f"multiplier must be a finite number > 0, got {multiplier!r}") from None
    if not np.isfinite(width) or width <= 0.0:
        raise StrategyError(f"multiplier must be a finite number > 0, got {multiplier!r}")

    try:
        highs = np.asarray(pd.to_numeric(high, errors="raise"), dtype="float64")
        lows = np.asarray(pd.to_numeric(low, errors="raise"), dtype="float64")
        closes = np.asarray(pd.to_numeric(close, errors="raise"), dtype="float64")
        averages = np.asarray(pd.to_numeric(atr_values, errors="raise"), dtype="float64")
    except (TypeError, ValueError):
        raise StrategyError("high, low, close and atr_values must contain numeric values") from None

    count = closes.size
    line = np.full(count, np.nan, dtype="float64")
    direction = np.full(count, np.nan, dtype="float64")
    if count == 0:
        return line, direction

    midpoint = (highs + lows) / 2.0
    basic_upper = midpoint + width * averages
    basic_lower = midpoint - width * averages
    final_upper = np.full(count, np.nan, dtype="float64")
    final_lower = np.full(count, np.nan, dtype="float64")

    defined = np.flatnonzero(np.isfinite(averages))
    if defined.size == 0:
        # No ATR anywhere: no band can exist, so both columns stay NaN.
        return line, direction
    start = int(defined[0])
    final_upper[start] = basic_upper[start]
    final_lower[start] = basic_lower[start]
    direction[start] = 1.0
    line[start] = final_lower[start]

    for position in range(start + 1, count):
        previous_close = closes[position - 1]
        upper = basic_upper[position]
        lower = basic_lower[position]
        if np.isfinite(upper) and (
            upper < final_upper[position - 1] or previous_close > final_upper[position - 1]
        ):
            final_upper[position] = upper
        else:
            final_upper[position] = final_upper[position - 1]
        if np.isfinite(lower) and (
            lower > final_lower[position - 1] or previous_close < final_lower[position - 1]
        ):
            final_lower[position] = lower
        else:
            final_lower[position] = final_lower[position - 1]

        if direction[position - 1] == 1.0:
            direction[position] = -1.0 if closes[position] < final_lower[position] else 1.0
        else:
            direction[position] = 1.0 if closes[position] > final_upper[position] else -1.0
        line[position] = (
            final_lower[position] if direction[position] == 1.0 else final_upper[position]
        )
    return line, direction


def _warmup(params: SupertrendStrategyParams) -> int:
    """Return the candles :class:`SupertrendStrategy` needs before it can signal.

    ``atr_period + 2``: the ATR is ``NaN`` on the first ``atr_period`` candles and
    first defined on the candle that follows them (index ``atr_period``), the
    recursion is seeded on that same candle, and the declared floor adds one
    further candle so a signal never has to lean on the seed candle alone.  Both
    :meth:`SupertrendStrategy.prepare` and
    :meth:`SupertrendStrategy.required_candles` go through this single helper, so
    the declared warm-up can never drift from the computation actually
    performed.

    The function is pure and never raises for a validated
    :class:`SupertrendStrategyParams`.
    """
    return params.atr_period + 2
