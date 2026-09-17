"""Pure technical indicators used by the strategy layer.

Design rules (they are part of the package contract):

* **pure**: no state, no I/O, no randomness — the same input always yields the
  same output;
* **non-mutating**: the input ``Series`` are never modified (every result is a
  freshly allocated ``float64`` ``Series``);
* **index preserving**: every result is indexed *exactly* like the input (and
  keeps its ``name`` when pandas does, see the individual docstrings);
* **finite**: infinite values are mapped to ``NaN`` so that a result never
  contains ``inf``;
* **typed errors**: an invalid ``period`` raises
  :class:`~trading_backtest.core.errors.StrategyError`.

The smoothing conventions follow Wilder (and TA-Lib, hence Freqtrade): the
exponential recursion ``y_t = (1 - 1/period) * y_{t-1} + (1/period) * x_t`` is
*seeded* with the simple moving average of the first ``period`` observations,
which is why the first ``period`` values of :func:`rsi` and :func:`atr` are
``NaN``.
"""

from __future__ import annotations

from collections.abc import Hashable

import numpy as np
import pandas as pd

from trading_backtest.core.errors import StrategyError

__all__ = ["atr", "ema", "rsi", "true_range"]


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _validate_period(period: int, *, name: str = "period") -> int:
    """Return ``period`` as a plain ``int``, or raise :class:`StrategyError`.

    Raises
    ------
    StrategyError
        If ``period`` is not an integer or is smaller than 1.
    """
    try:
        value = int(period)
    except (TypeError, ValueError):
        raise StrategyError(f"{name} must be an integer >= 1, got {period!r}") from None
    if value != period:
        raise StrategyError(f"{name} must be an integer, got {period!r}")
    if value < 1:
        raise StrategyError(f"{name} must be >= 1, got {value}")
    return value


def _as_float_series(series: pd.Series, *, name: str) -> pd.Series:
    """Return a new ``float64`` view of ``series`` (never the input itself)."""
    if not isinstance(series, pd.Series):
        raise StrategyError(f"{name} must be a pandas Series, got {type(series).__name__}")
    if pd.api.types.is_numeric_dtype(series.dtype):
        return series.astype("float64")
    try:
        converted = pd.to_numeric(series, errors="raise")
    except (TypeError, ValueError):
        raise StrategyError(f"{name} must contain numeric values") from None
    return converted.astype("float64")


def _finite(values: np.ndarray, *, index: pd.Index, name: Hashable | None) -> pd.Series:
    """Wrap ``values`` into a ``float64`` Series, mapping ``±inf`` to ``NaN``."""
    numeric = np.asarray(values, dtype="float64")
    cleaned = np.where(np.isfinite(numeric), numeric, np.nan)
    return pd.Series(cleaned, index=index, name=name, dtype="float64")


def _same_length(*series: pd.Series) -> None:
    lengths = {len(item) for item in series}
    if len(lengths) > 1:
        raise StrategyError(
            "high, low, close and previous_close must all have the same length, "
            f"got {sorted(lengths)}"
        )


def _wilder(seeded: np.ndarray, period: int) -> np.ndarray:
    """Apply Wilder's recursion to an already *seeded* array.

    ``seeded`` must carry the SMA seed as its first non-``NaN`` value; every
    subsequent value is the raw observation fed to the recursion.  Leading
    ``NaN`` are preserved, so the result has exactly the same ``NaN`` prefix as
    the seed.
    """
    series = pd.Series(seeded, dtype="float64")
    smoothed = series.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()
    return smoothed.to_numpy(dtype="float64")


def _seed_sma(observations: np.ndarray, period: int) -> np.ndarray:
    """Build the Wilder input array seeded with an SMA.

    ``observations[i]`` is the raw observation entering the recursion at output
    position ``i + 1`` (both the RSI deltas and the ATR true ranges start one
    position after the first candle, because they need two prices).  The result
    therefore has ``observations.size + 1`` points: position ``period`` holds
    the SMA of the first ``period`` observations, every later position holds the
    matching raw observation and the first ``period`` positions stay ``NaN``.

    When fewer than ``period`` observations are available the result is entirely
    ``NaN`` (no seed can be computed).
    """
    length = observations.size + 1
    seeded = np.full(length, np.nan, dtype="float64")
    extra = observations.size - period
    if extra >= 0:
        seeded[period] = float(np.mean(observations[0:period]))
        if extra:
            seeded[period + 1 :] = observations[period:]
    return seeded


# ---------------------------------------------------------------------------
# public indicators
# ---------------------------------------------------------------------------


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average of ``series``.

    Equivalent to ``series.ewm(span=period, adjust=False, min_periods=period).mean()``:
    the first ``period - 1`` values are ``NaN``.

    Parameters
    ----------
    series:
        Input values (any numeric dtype).  Never mutated.
    period:
        Span of the average, must be ``>= 1``.

    Raises
    ------
    StrategyError
        If ``period`` is not an integer ``>= 1``, or if ``series`` is not a
        numeric :class:`pandas.Series`.
    """
    span = _validate_period(period)
    values = _as_float_series(series, name="series")
    smoothed = values.ewm(span=span, adjust=False, min_periods=span).mean()
    return _finite(smoothed.to_numpy(dtype="float64"), index=values.index, name=smoothed.name)


def true_range(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    previous_close: pd.Series | None = None,
) -> pd.Series:
    """True range: ``max(high - low, |high - prev_close|, |low - prev_close|)``.

    ``previous_close`` defaults to ``close.shift(1)``.  On the very first candle
    (where no previous close exists) the term falls back to ``high - low`` — the
    TA-Lib convention — so the returned series is never ``NaN`` for a
    well-formed input.

    The result is a ``float64`` :class:`pandas.Series` named ``"true_range"``,
    indexed like ``high``; ``high``, ``low``, ``close`` and ``previous_close``
    are never mutated.

    Raises
    ------
    StrategyError
        If an input is not a numeric Series, or if the inputs do not all have
        the same length.
    """
    high_values = _as_float_series(high, name="high")
    low_values = _as_float_series(low, name="low")
    close_values = _as_float_series(close, name="close")
    if previous_close is None:
        previous = close_values.shift(1)
        _same_length(high_values, low_values, close_values)
    else:
        previous = _as_float_series(previous_close, name="previous_close")
        _same_length(high_values, low_values, close_values, previous)

    highs = high_values.to_numpy(dtype="float64")
    lows = low_values.to_numpy(dtype="float64")
    previous_values = previous.to_numpy(dtype="float64")

    # ``np.fmax`` ignores NaN, which is exactly the first-candle fallback.
    with np.errstate(invalid="ignore"):
        ranges = np.fmax(
            np.fmax(highs - lows, np.abs(highs - previous_values)),
            np.abs(lows - previous_values),
        )
    return _finite(ranges, index=high_values.index, name="true_range")


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder), in ``[0, 100]``.

    The average gain and average loss are Wilder-smoothed and *seeded* with the
    simple moving average of the first ``period`` deltas, so the first ``period``
    values of the result are ``NaN`` (the first value sits at position
    ``period``).

    Conventions:

    * a strictly rising series yields ``100.0`` (zero average loss, positive
      average gain);
    * a strictly falling series yields ``0.0``;
    * a flat series (zero average gain **and** zero average loss) yields ``50.0``
      — the neutral value, instead of the undefined ``0 / 0``.

    The result is a ``float64`` :class:`pandas.Series` named ``"rsi"``, indexed
    exactly like ``series``; the input is never mutated.

    Raises
    ------
    StrategyError
        If ``period`` is not an integer ``>= 1``, or if ``series`` is not a
        numeric :class:`pandas.Series`.
    """
    length = _validate_period(period)
    values = _as_float_series(series, name="series")
    prices = values.to_numpy(dtype="float64")
    if prices.size == 0:
        return _finite(prices, index=values.index, name="rsi")
    deltas = np.diff(prices)  # delta at position p = prices[p + 1] - prices[p]
    valid = ~np.isnan(deltas)
    gains = np.where(valid, np.maximum(deltas, 0.0), np.nan)
    losses = np.where(valid, np.maximum(-deltas, 0.0), np.nan)

    average_gain = _wilder(_seed_sma(gains, length), length)
    average_loss = _wilder(_seed_sma(losses, length), length)

    result = np.full(prices.size, np.nan, dtype="float64")
    known = ~np.isnan(average_gain) & ~np.isnan(average_loss)
    if known.any():
        gain = average_gain[known]
        loss = average_loss[known]
        with np.errstate(divide="ignore", invalid="ignore"):
            relative_strength = gain / loss
            computed = 100.0 - (100.0 / (1.0 + relative_strength))
        zero_loss = loss == 0.0
        computed = np.where(
            zero_loss,
            np.where(gain > 0.0, 100.0, 50.0),
            computed,
        )
        result[known] = np.clip(computed, 0.0, 100.0)
    return _finite(result, index=values.index, name="rsi")


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Average True Range (Wilder), always ``>= 0``.

    The true range is Wilder-smoothed and seeded with the simple moving average
    of the first ``period`` true ranges, so the first ``period`` values are
    ``NaN`` (the first value sits at position ``period``) — the TA-Lib /
    Freqtrade convention.

    The result is a ``float64`` :class:`pandas.Series` named ``"atr"``, indexed
    exactly like ``high``; the inputs are never mutated.

    Raises
    ------
    StrategyError
        If ``period`` is not an integer ``>= 1``, or if the inputs are not
        numeric Series of identical length.
    """
    length = _validate_period(period)
    ranges = true_range(high, low, close)
    true_ranges = ranges.to_numpy(dtype="float64")
    if true_ranges.size == 0:
        return _finite(true_ranges, index=ranges.index, name="atr")
    # The true range of the very first candle has no previous close to work
    # with, so the Wilder recursion starts on the second observation.
    smoothed = _wilder(_seed_sma(true_ranges[1:], length), length)
    return _finite(
        np.maximum(smoothed, 0.0),
        index=ranges.index,
        name="atr",
    )
