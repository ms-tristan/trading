"""Deterministic target transform of the forecasting layer.

TimesFM 2.5 has **no calendar channel**: a plain ``forecast()`` call only sees
the numbers it is given, so a strong intraday seasonality would be learned as if
it were a price move.  The forecasting layer therefore hands the model a
deterministically *de-seasonalised* log price and rebuilds the seasonality for
the reported path.  This module is that transform, and it is the reason the
seasonality never has to be estimated by the model (and never leaks the future).

Every helper here is pure, causal and offline:

* causal — the value at candle ``t`` only uses candles ``<= t``, so an artifact
  built from a truncated candle frame is identical on the shared origins;
* pure — the inputs are never mutated and every result is freshly allocated;
* dependency free — standard library, ``numpy``, ``pandas`` and
  :mod:`trading_platform.core.errors` only.

Trap worth remembering: pandas 3 stores timestamps at microsecond resolution by
default, so ``DatetimeIndex.asi8`` is **not** a nanosecond count.  The
phase arithmetic below works on :class:`pandas.Timedelta` differences instead,
which is unit agnostic.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from trading_platform.core.constants import UTC
from trading_platform.core.errors import ForecastError

__all__ = [
    "EPOCH",
    "TIMEFRAME_SECONDS",
    "candle_phase",
    "deseasonalize_log_price",
    "timeframe_delta",
]

#: Reference instant of the phase arithmetic (a Thursday, 00:00 UTC).
EPOCH = pd.Timestamp("1970-01-01T00:00:00Z")

#: Duration of one candle, in seconds, for every supported timeframe.
TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "3d": 259200,
    "1w": 604800,
}

#: Name of the de-seasonalised log price, kept stable for the artifact schema.
DESEASONALIZED_NAME = "deseasonalized_log_price"


def _int_value(value: Any, *, name: str) -> int:
    """Return ``value`` as a plain ``int``, or raise :class:`ForecastError`."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ForecastError(f"{name} must be an integer, got {value!r}") from None
    if number != value:
        raise ForecastError(f"{name} must be an integer, got {value!r}")
    return number


def _positive_int(value: Any, *, name: str) -> int:
    """Return ``value`` as a plain ``int`` >= 1, or raise :class:`ForecastError`."""
    number = _int_value(value, name=name)
    if number < 1:
        raise ForecastError(f"{name} must be >= 1, got {number}")
    return number


def timeframe_delta(timeframe: str) -> pd.Timedelta:
    """Return the duration of one ``timeframe`` candle.

    Raises
    ------
    ForecastError
        If ``timeframe`` is empty or not one of :data:`TIMEFRAME_SECONDS`; the
        message lists the supported values.
    """
    supported = ", ".join(sorted(TIMEFRAME_SECONDS))
    try:
        seconds = TIMEFRAME_SECONDS[timeframe]
    except (KeyError, TypeError):
        raise ForecastError(
            f"unsupported timeframe: {timeframe!r} (supported: {supported})"
        ) from None
    return pd.Timedelta(seconds=seconds)


def candle_phase(index: pd.DatetimeIndex, *, timeframe: str, period: int) -> np.ndarray:
    """Return the seasonal phase (``int64``) of every candle of ``index``.

    The phase of a timestamp is the number of whole ``timeframe`` candles
    elapsed since :data:`EPOCH`, modulo ``period``.  A timezone-naive index is
    interpreted as UTC; a timezone-aware one is converted to UTC first, so the
    phases do not depend on the timezone the data was loaded with.

    Parameters
    ----------
    index:
        Timestamps of the candles.
    timeframe:
        Candle duration, a key of :data:`TIMEFRAME_SECONDS`.
    period:
        Number of phases of the cycle (``>= 1``).

    Returns
    -------
    numpy.ndarray
        An ``int64`` array of shape ``(len(index),)`` with values in
        ``[0, period)``.

    Raises
    ------
    ForecastError
        If ``timeframe`` is unsupported or ``period`` is smaller than one.
    """
    delta = timeframe_delta(timeframe)
    cycles = _positive_int(period, name="period")

    stamps = pd.DatetimeIndex(index)
    stamps = stamps.tz_localize(UTC) if stamps.tz is None else stamps.tz_convert(UTC)
    steps = (stamps - EPOCH) // delta
    phases = steps.to_numpy(dtype="int64") % cycles
    return np.asarray(phases, dtype="int64")


def deseasonalize_log_price(
    close: pd.Series,
    *,
    timeframe: str,
    period: int = 24,
    window: int = 168,
) -> pd.Series:
    """Return the deterministically de-seasonalised log price of ``close``.

    The seasonal component of candle ``t`` is the mean of ``log(close)`` over the
    ``k = max(1, window // period)`` most recent candles that share the phase of
    ``t`` (``t`` included), so it only ever uses candles ``<= t``: the transform
    is causal and an artifact built from a truncated frame is identical on the
    origins both frames share.

    Parameters
    ----------
    close:
        Close prices, strictly positive and finite.  Indexed by candle timestamp.
    timeframe:
        Candle duration, a key of :data:`TIMEFRAME_SECONDS`.
    period:
        Number of phases of the seasonal cycle (``>= 1``), for example ``24`` for
        a daily cycle on hourly candles.
    window:
        Length of the trailing window, in candles, used to estimate the seasonal
        component.  ``window <= 1`` disables the transform and returns
        ``log(close)`` unchanged.

    Returns
    -------
    pandas.Series
        A ``float64`` series named ``deseasonalized_log_price``, indexed exactly
        like ``close``.

    Raises
    ------
    ForecastError
        If ``close`` is not a :class:`pandas.Series`, if it holds a non-positive
        or non-finite value, if ``timeframe`` is unsupported or if ``period`` is
        smaller than one.
    """
    if not isinstance(close, pd.Series):
        raise ForecastError(f"close must be a pandas Series, got {type(close).__name__}")

    # Validate the arguments even when the transform is disabled, so that an
    # invalid call is always an invalid call.
    _positive_int(period, name="period")
    window_value = _int_value(window, name="window")
    timeframe_delta(timeframe)

    try:
        values = close.to_numpy(dtype="float64")
    except (TypeError, ValueError):
        raise ForecastError("close must hold numeric values") from None
    if values.size and not bool(np.all(np.isfinite(values))):
        raise ForecastError("close must not contain NaN or infinite values")
    if values.size and bool(np.any(values <= 0.0)):
        raise ForecastError("close must be strictly positive to define a log price")

    log_price = np.log(values)
    index = pd.DatetimeIndex(close.index)

    if window_value <= 1:
        return pd.Series(log_price, index=index, name=DESEASONALIZED_NAME, dtype="float64")

    phases = candle_phase(index, timeframe=timeframe, period=period)
    observations = max(1, window_value // int(period))
    frame = pd.DataFrame({"log_price": log_price, "phase": phases}, index=index)
    seasonal = frame.groupby("phase")["log_price"].transform(
        lambda series: series.rolling(observations, min_periods=1).mean()
    )
    de_seasonalized = log_price - seasonal.to_numpy(dtype="float64")
    return pd.Series(de_seasonalized, index=index, name=DESEASONALIZED_NAME, dtype="float64")
