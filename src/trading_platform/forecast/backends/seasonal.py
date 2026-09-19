"""The ``seasonal`` backend: a phase-aware, drift-corrected baseline.

This is the offline baseline that actually knows about **time**: where
:mod:`trading_platform.forecast.backends.naive` holds the last value flat with a
flat median, this backend predicts the seasonal profile of the upcoming candles
plus the recent drift, and wraps a ``sqrt(step)`` dispersion envelope around it:

* the **seasonal shape** of the context is the average log offset of every candle
  relative to the candles that share its *phase* — the phase being the candle's
  position inside its own cycle, derived from the origin timestamp and the
  configured ``timeframe`` (see :meth:`SeasonalBackend.seasonal_profile`).  A
  daily cycle therefore means 1440 candles on ``1m``, 288 on ``5m``, 96 on
  ``15m``, 24 on ``1h``, 6 on ``4h`` and 1 on ``1d``: **no assumption of 24
  candles per day is made anywhere in this module**;
* the **drift** is the mean of the trailing log differences over ``trend_window``
  candles, and the dispersion is their population standard deviation;
* the forecast is the *seasonal shape carried forward* for one cycle, added to
  the drift: ``median[i] = drift * (i + 1) + profile[(p0 + i + 1) % period]``,
  with ``p0`` the phase of the origin candle.  A periodic context (a sawtooth
  with period ``period``) is therefore reproduced exactly, which is what makes
  this a genuine seasonal-naive baseline rather than a second random walk;
* ``quantiles[j, i] = median[i] + sigma_step * sqrt(i + 1) * z(level_j)``.

The backend is fully deterministic, offline and dependency free; the
calendar-aware **de-seasonalisation of the artifact target** lives one layer up,
in :mod:`trading_platform.forecast.series`, which removes the calendar cycle
before any backend sees the series.

The validation helpers are shared with the naive backend on purpose, so the two
offline backends accept exactly the same requests and levels, and both are
equally safe on a caller-owned request list: nothing is mutated.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from trading_platform.core.errors import ForecastError
from trading_platform.forecast.backends.naive import (
    _context_values,
    _gaussian_z,
    _validate_horizon,
    _validate_quantile_levels,
    _validate_timeframe,
    _validate_window,
)
from trading_platform.forecast.types import (
    DEFAULT_QUANTILE_LEVELS,
    ForecastRequest,
    ForecastTrajectory,
)

__all__ = ["BACKEND_NAME", "CANDLES_PER_DAY", "SeasonalBackend", "build_backend", "candles_per_day"]

BACKEND_NAME: str = "seasonal"

_DEFAULT_TIMEFRAME = "1h"
_DEFAULT_TREND_WINDOW = 24

#: Number of candles of each timeframe in one **daily** cycle.  It is the only
#: place in this package that maps a timeframe to its phase count, and it is
#: what keeps the seasonal shape correct on every supported timeframe instead of
#: the 1h-only ``24``.
CANDLES_PER_DAY: dict[str, int] = {
    "1m": 1440,
    "3m": 480,
    "5m": 288,
    "15m": 96,
    "30m": 48,
    "1h": 24,
    "2h": 12,
    "4h": 6,
    "6h": 4,
    "8h": 3,
    "12h": 2,
    "1d": 1,
    "3d": 1,
    "1w": 1,
}


def candles_per_day(timeframe: str) -> int:
    """Return the number of ``timeframe`` candles that make up one daily cycle.

    Parameters
    ----------
    timeframe:
        Candle duration (``"1m"``, ``"15m"``, ``"1h"``, ``"4h"``, ``"1d"``, ...).

    Returns
    -------
    int
        The number of candles of one day, always ``>= 1``.

    Raises
    ------
    ForecastError
        If ``timeframe`` is not a supported candle duration.  The supported
        durations are exactly those of
        :data:`trading_platform.forecast.series.TIMEFRAME_SECONDS`, so an
        unsupported label is refused here rather than silently mis-phased.

    Examples
    --------
    >>> candles_per_day("1h")
    24
    >>> candles_per_day("4h")
    6
    >>> candles_per_day("1d")
    1
    """
    label = _validate_timeframe(timeframe)
    try:
        return CANDLES_PER_DAY[label]
    except KeyError:
        supported = ", ".join(sorted(CANDLES_PER_DAY))
        raise ForecastError(f"unsupported timeframe: {label!r} (supported: {supported})") from None


def _phase_count(period: Any) -> int:
    """Return an optional phase-count override as an ``int`` >= 1.

    ``None`` means "no override": the caller then uses
    :func:`candles_per_day`.  A non-positive, non-integer or non-numeric value is
    refused with :class:`ForecastError` rather than silently ignored.
    """
    if period is None:
        raise ForecastError("seasonal_period must be an integer >= 1, got None")
    return _validate_window(period, name="seasonal_period")


def _coerced_origin(origin: Any) -> pd.Timestamp:
    """Return ``origin`` as a UTC :class:`pandas.Timestamp`.

    Raises
    ------
    ForecastError
        If ``origin`` is not a timestamp or is ``NaT``.
    """
    try:
        stamp = pd.Timestamp(origin)
    except (TypeError, ValueError, OverflowError):
        raise ForecastError(f"origin is not a timestamp: {origin!r}") from None
    if pd.isna(stamp):
        raise ForecastError("origin must be a valid timestamp, got NaT")
    return stamp.tz_localize("UTC") if stamp.tz is None else stamp.tz_convert("UTC")


def _phase_of(origin: Any, *, timeframe: str, period: int) -> int:
    """Return the phase of ``origin`` inside the cycle of ``period`` candles.

    The phase is the number of whole ``timeframe`` candles elapsed since the
    UNIX epoch, modulo ``period``.  It is derived from the timestamp alone, so
    the backend needs no candle index and behaves identically whatever slice of
    history a context was cut from.  A timezone-naive timestamp is read as UTC.
    """
    stamp = _coerced_origin(origin)
    epoch = pd.Timestamp("1970-01-01T00:00:00Z")
    delta = pd.Timedelta(seconds=_TIMEFRAME_SECONDS[timeframe])
    steps = int((stamp - epoch) // delta)
    return steps % period


def _backward_phases(origin: Any, *, timeframe: str, period: int, length: int) -> np.ndarray:
    """Return the phases of the ``length`` candles ending at ``origin``.

    The last element is the phase of ``origin`` and every earlier element steps
    one candle back, modulo ``period``; the result never depends on a candle
    index, only on the origin timestamp and the candle duration.
    """
    last = _phase_of(origin, timeframe=timeframe, period=period)
    return (last - np.arange(length - 1, -1, -1, dtype="int64")) % period


def _forward_phases(origin: Any, *, timeframe: str, period: int, steps: int) -> np.ndarray:
    """Return the phases of the ``steps`` candles that follow ``origin``."""
    last = _phase_of(origin, timeframe=timeframe, period=period)
    return (last + np.arange(1, steps + 1, dtype="int64")) % period


#: Seconds of one candle.  It mirrors
#: :data:`trading_platform.forecast.series.TIMEFRAME_SECONDS` (and
#: :data:`CANDLES_PER_DAY`) so this module stays importable on its own; the
#: timeframe labels are the contract, not the numbers.
_TIMEFRAME_SECONDS: dict[str, int] = {
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


class SeasonalBackend:
    """Phase-aware seasonal-naive backend with a ``sqrt(step)`` envelope.

    Parameters
    ----------
    timeframe:
        Candle duration stored on every produced trajectory **and** the label
        that fixes the number of phases of the cycle (:func:`candles_per_day`):
        one daily cycle is 1440 candles on ``1m``, 96 on ``15m``, 24 on ``1h``
        and 1 on ``1d``.
    quantile_levels:
        Levels to store, a strictly increasing subset of ``(0, 1)`` containing
        ``0.5``.  They are validated on the first :meth:`predict` call rather
        than in the constructor, so that registry introspection can never raise.
    trend_window:
        Number of trailing log differences used to estimate the drift and the
        per-step scale (``>= 1``).
    seasonal_period:
        Explicit number of phases of the seasonal cycle, overriding the
        timeframe default.  ``None`` (the default) derives it from
        :func:`candles_per_day`, which is what keeps ``1m``/``5m``/``15m``/``4h``
        correct; it must be ``>= 1`` and is refused otherwise.

    Attributes
    ----------
    name:
        Registry identifier of the backend, always :data:`BACKEND_NAME`.

    Raises
    ------
    ForecastError
        If ``timeframe`` is empty or unsupported, if ``trend_window`` or an
        explicit ``seasonal_period`` is not an integer ``>= 1``, or if
        ``quantile_levels`` is not a sequence.

    Examples
    --------
    A context that repeats one daily cycle is reproduced by the seasonal shape;
    on hourly candles the cycle holds 24 candles:

    >>> backend = SeasonalBackend(timeframe="1h", quantile_levels=(0.1, 0.5, 0.9))
    >>> backend.seasonal_period
    24
    >>> backend.seasonal_period_on("4h")
    6
    """

    name: str = BACKEND_NAME

    def __init__(
        self,
        *,
        timeframe: str = _DEFAULT_TIMEFRAME,
        quantile_levels: Sequence[float] = DEFAULT_QUANTILE_LEVELS,
        trend_window: int = _DEFAULT_TREND_WINDOW,
        seasonal_period: int | None = None,
    ) -> None:
        self._timeframe = _validate_timeframe(timeframe)
        self._cycle = candles_per_day(self._timeframe)
        try:
            self._quantile_levels: tuple[float, ...] = tuple(quantile_levels)
        except TypeError:
            raise ForecastError("quantile_levels must be a sequence of numbers") from None
        self._trend_window = _validate_window(trend_window, name="trend_window")
        self._override = None if seasonal_period is None else _phase_count(seasonal_period)
        self._seasonal_period = self._cycle if self._override is None else self._override

    @property
    def timeframe(self) -> str:
        """Candle duration stored on the produced trajectories."""
        return self._timeframe

    @property
    def quantile_levels(self) -> tuple[float, ...]:
        """Levels as passed to the constructor (validated by :meth:`predict`)."""
        return self._quantile_levels

    @property
    def trend_window(self) -> int:
        """Number of trailing log differences used for drift and dispersion."""
        return self._trend_window

    @property
    def seasonal_period(self) -> int:
        """Number of phases of the seasonal cycle actually used."""
        return self._seasonal_period

    def seasonal_period_on(self, timeframe: str) -> int:
        """Return the cycle length this backend would use on ``timeframe``.

        This is the timeframe-genericity probe of the class: with no explicit
        ``seasonal_period`` the answer is the number of ``timeframe`` candles of a
        daily cycle (:func:`candles_per_day`), so a ``1m`` series gets ``1440``
        where a ``1h`` series gets ``24``.  An explicit ``seasonal_period`` wins
        whatever the timeframe, so an operator who knows the true cycle of the
        instrument can force it.

        Parameters
        ----------
        timeframe:
            Candle duration to resolve (:func:`candles_per_day`).

        Returns
        -------
        int
            The number of phases of the cycle on ``timeframe``, always ``>= 1``.

        Raises
        ------
        ForecastError
            If ``timeframe`` is not a supported candle duration.

        Examples
        --------
        >>> SeasonalBackend(timeframe="1h").seasonal_period_on("4h")
        6
        >>> SeasonalBackend(timeframe="1h", seasonal_period=12).seasonal_period_on("4h")
        12
        """
        if self._override is None:
            return candles_per_day(timeframe)
        candles_per_day(timeframe)  # an unsupported label is refused, never guessed
        return self._seasonal_period

    def seasonal_profile(self, context: Sequence[float], *, origin: Any) -> np.ndarray:
        """Return the seasonal deviation of every candle of ``context``.

        The *seasonal shape* is the per-phase deviation from the level of the
        context: candle ``t`` contributes ``value[t] - mean(context)`` to its own
        phase bucket.  Subtracting the context mean (rather than the per-phase
        mean) is what makes the shape a **deviation**, so a periodic series is
        reproduced by carrying the shape forward instead of being flattened to
        zero.  Every candle of the context is used, so the estimate is causal by
        construction: it only ever reads candles up to and including ``origin``.

        Parameters
        ----------
        context:
            Log-price values, oldest first, at least two finite values.
        origin:
            Timestamp of the **last** context candle; it fixes the phase of the
            whole context, so the profile depends on the clock, never on an
            implicit ``24`` candles per day.

        Returns
        -------
        numpy.ndarray
            A ``float64`` array of shape ``(len(context),)`` holding the seasonal
            deviation of every candle.

        Raises
        ------
        ForecastError
            If the context is not a finite series of at least two values, if
            ``origin`` is not a timestamp, or if it is ``NaT``.
        """
        values = _context_values(
            ForecastRequest(origin=_coerced_origin(origin), context=tuple(context))
        )
        return np.asarray(values - float(np.mean(values)), dtype="float64")

    def forward_profile(self, context: Sequence[float], *, origin: Any, horizon: int) -> np.ndarray:
        """Return the seasonal shape expected over the ``horizon`` candles after ``origin``.

        The phases of the context and the phases of the forecast steps are
        derived from the **same clock** (``origin`` and the configured
        ``timeframe``), so step ``i`` is assigned the average deviation of every
        context candle that shares its phase.  The shape wraps around the cycle
        instead of being extrapolated linearly, which is what makes this backend
        reproduce a periodic context; a phase the context never covered
        contributes a neutral ``0.0``.

        Parameters
        ----------
        context:
            Log-price values of the context, oldest first.
        origin:
            Timestamp of the last context candle.
        horizon:
            Number of steps to return (``>= 1``).

        Returns
        -------
        numpy.ndarray
            A ``float64`` array of shape ``(horizon,)``.

        Raises
        ------
        ForecastError
            If the context or the origin is invalid, or if ``horizon`` is smaller
            than one.
        """
        steps = _validate_horizon(horizon)
        profile = self.seasonal_profile(context, origin=origin)
        period = self._seasonal_period
        phases = _backward_phases(
            origin, timeframe=self._timeframe, period=period, length=int(profile.size)
        )
        totals = np.zeros(period, dtype="float64")
        counts = np.zeros(period, dtype="float64")
        np.add.at(totals, phases, profile)
        np.add.at(counts, phases, 1.0)
        shape = np.zeros(period, dtype="float64")
        np.divide(totals, counts, out=shape, where=counts > 0.0)
        wanted = _forward_phases(origin, timeframe=self._timeframe, period=period, steps=steps)
        return np.asarray(shape[wanted], dtype="float64")

    def is_available(self) -> bool:
        """Return ``True``: this backend only needs ``numpy`` (always available)."""
        return True

    def license_note(self) -> str:
        """Return an empty licence note: this backend ships no third-party weights."""
        return ""

    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]:
        """Return one seasonal-naive trajectory per request, in request order.

        Parameters
        ----------
        requests:
            The requests to forecast.  They are read, never modified (the model
            wrappers of other backends are known to mutate their input list), so
            ``predict`` may be handed a caller-owned sequence directly.
        horizon:
            Number of steps of every trajectory (``>= 1``).

        Returns
        -------
        list[ForecastTrajectory]
            Exactly one trajectory per request, in request order, each carrying
            the request origin, the configured timeframe, the requested
            ``horizon`` and the configured quantile levels.  The median is the
            seasonal shape carried forward for one cycle plus the drift; every
            value is finite.

        Raises
        ------
        ForecastError
            If ``horizon`` is smaller than one, if a context is not a finite
            sequence of at least two values, or if the configured quantile levels
            are invalid.
        """
        steps = _validate_horizon(horizon)
        levels = _validate_quantile_levels(self._quantile_levels)
        z_scores = np.array([_gaussian_z(level) for level in levels], dtype="float64")
        step_index = np.arange(1, steps + 1, dtype="float64")
        time_scale = np.sqrt(step_index)

        # Iterate over a snapshot: a caller-owned (possibly mutable) sequence is
        # never traversed in place, and `_context_values` copies every series.
        trajectories: list[ForecastTrajectory] = []
        for request in list(requests):
            context = _context_values(request)
            window = context[-(self._trend_window + 1) :]
            differences = np.diff(window)
            drift = float(np.mean(differences))
            sigma_step = float(np.std(differences, ddof=0))

            forward = self.forward_profile(
                [float(value) for value in context], origin=request.origin, horizon=steps
            )
            median = drift * step_index + forward
            quantiles = median[np.newaxis, :] + sigma_step * np.outer(z_scores, time_scale)
            trajectories.append(
                ForecastTrajectory(
                    origin=request.origin,
                    timeframe=self._timeframe,
                    horizon=steps,
                    quantile_levels=levels,
                    quantiles=quantiles.astype("float32"),
                )
            )
        return trajectories


def build_backend(**options: Any) -> SeasonalBackend:
    """Build a :class:`SeasonalBackend` from keyword ``options`` (registry factory).

    Parameters
    ----------
    **options:
        Forwarded verbatim to :class:`SeasonalBackend` (``timeframe``,
        ``quantile_levels``, ``trend_window``, ``seasonal_period``).

    Returns
    -------
    SeasonalBackend
        A fresh, deterministic backend instance.

    Examples
    --------
    >>> build_backend(timeframe="5m").seasonal_period
    288
    """
    return SeasonalBackend(**options)
