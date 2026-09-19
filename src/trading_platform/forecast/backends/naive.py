"""The ``naive`` backend: a random walk with a dispersion envelope.

This backend is the honest baseline every other backend must beat.  It assumes
the normalised log price is a martingale:

* ``median[i] == 0.0`` for every step — the origin close is held flat, exactly
  what "no forecast skill" means for a log-price path;
* the spread grows with ``sqrt(step)`` around that flat median, with a per-step
  scale estimated from the mean absolute trailing log difference of the context
  (the mean absolute deviation of a random walk is ``sigma * sqrt(2 / pi)``, so
  the envelope is deliberately a *scale*, not a calibrated density).

It is a pure ``numpy`` backend: no randomness, no I/O, no optional dependency,
and two calls with the same requests return the same trajectories.  It also
hosts the small validation helpers shared by the offline backends (see
:mod:`trading_platform.forecast.backends.seasonal`), so both agree bit for bit
on what a valid request and a valid level sequence are.

Because the two offline backends are the only forecasters a ``.[dev]``
installation can run, they carry the whole *offline* proof of the forecasting
layer: every documented recipe that needs an artifact without a model download
(``trading forecast-build --backend naive``) is served here, which is also how
the realtime integration of the ``timesfm`` strategy is regression-tested
without ``torch``.

Non-mutation guarantee
----------------------
The input list and every context it holds are **read-only** for both offline
backends.  A model wrapper is known to pad its input list in place, so both
implementations iterate over a snapshot and copy each context into ``float64``
before touching it; ``tests/test_forecast_backends_offline.py`` pins that the
caller's sequence and its context lists are byte-identical after a call.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist
from typing import Any

import numpy as np

from trading_platform.core.errors import ForecastError
from trading_platform.forecast.types import (
    DEFAULT_QUANTILE_LEVELS,
    MEDIAN_LEVEL,
    ForecastRequest,
    ForecastTrajectory,
)

__all__ = ["BACKEND_NAME", "NaiveBackend", "build_backend"]

BACKEND_NAME: str = "naive"

#: Standard normal distribution used to turn a quantile level into a z-score.
#: ``scipy`` is **not** a dependency of this package, so the standard library is
#: the only legitimate source of the inverse CDF.
_STANDARD_NORMAL = NormalDist(0.0, 1.0)

_DEFAULT_TIMEFRAME = "1h"
_DEFAULT_DISPERSION_WINDOW = 100


# ---------------------------------------------------------------------------
# validation helpers shared with the other offline backends
# ---------------------------------------------------------------------------


def _int_value(value: Any, *, name: str) -> int:
    """Return ``value`` as a plain ``int``, or raise :class:`ForecastError`."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ForecastError(f"{name} must be an integer, got {value!r}") from None
    if number != value:
        raise ForecastError(f"{name} must be an integer, got {value!r}")
    return number


def _validate_horizon(horizon: Any) -> int:
    """Return ``horizon`` as an ``int`` >= 1, or raise :class:`ForecastError`."""
    steps = _int_value(horizon, name="horizon")
    if steps < 1:
        raise ForecastError(f"horizon must be >= 1, got {steps}")
    return steps


def _validate_quantile_levels(levels: Sequence[float]) -> tuple[float, ...]:
    """Return ``levels`` as a validated tuple of floats.

    Raises
    ------
    ForecastError
        If the levels are not a strictly increasing subset of ``(0, 1)`` that
        contains the median level ``0.5``.
    """
    try:
        values = tuple(float(level) for level in levels)
    except (TypeError, ValueError):
        raise ForecastError("quantile_levels must be a sequence of numbers") from None
    if not values:
        raise ForecastError("quantile_levels must not be empty")
    previous: float | None = None
    for level in values:
        if not math.isfinite(level) or not 0.0 < level < 1.0:
            raise ForecastError(f"quantile level must lie strictly inside (0, 1), got {level!r}")
        if previous is not None and level <= previous:
            raise ForecastError(
                f"quantile_levels must be strictly increasing, got {list(values)!r}"
            )
        previous = level
    if MEDIAN_LEVEL not in values:
        raise ForecastError(f"quantile_levels must contain the median level {MEDIAN_LEVEL}")
    return values


def _context_values(request: ForecastRequest) -> np.ndarray:
    """Return the finite ``float64`` context of ``request``.

    Raises
    ------
    ForecastError
        If the context is not numeric, holds fewer than two values or holds a
        non-finite value.  The message tells the caller to drop the NaNs first:
        trailing NaNs are not handled by the model wrappers either.
    """
    try:
        values = np.asarray(request.context, dtype="float64").reshape(-1)
    except (TypeError, ValueError):
        raise ForecastError("context must be a sequence of numbers") from None
    if values.size < 2 or not bool(np.all(np.isfinite(values))):
        raise ForecastError(
            "context must hold at least 2 finite values; drop leading/trailing NaNs "
            "before forecasting"
        )
    return values


def _gaussian_z(level: float) -> float:
    """Return the standard-normal quantile of ``level`` (``level`` in ``(0, 1)``)."""
    return float(_STANDARD_NORMAL.inv_cdf(level))


def _validate_timeframe(timeframe: Any) -> str:
    """Return ``timeframe`` as a non-empty string, or raise :class:`ForecastError`."""
    if not isinstance(timeframe, str) or not timeframe:
        raise ForecastError(f"timeframe must be a non-empty string, got {timeframe!r}")
    return timeframe


def _validate_window(value: Any, *, name: str) -> int:
    """Return a window length as an ``int`` >= 1, or raise :class:`ForecastError`."""
    window = _int_value(value, name=name)
    if window < 1:
        raise ForecastError(f"{name} must be >= 1, got {window}")
    return window


class NaiveBackend:
    """Random-walk backend with a ``sqrt(step)`` dispersion envelope.

    Parameters
    ----------
    timeframe:
        Candle duration stored on every produced trajectory (a label: the
        random walk itself is timeframe agnostic).
    quantile_levels:
        Levels to store, a strictly increasing subset of ``(0, 1)`` containing
        ``0.5``.  They are validated on the first :meth:`predict` call rather
        than in the constructor, so that registry introspection can never raise.
    dispersion_window:
        Number of trailing log differences used to estimate the per-step scale
        (``>= 1``).

    Attributes
    ----------
    name:
        Registry identifier of the backend, always :data:`BACKEND_NAME`.

    Examples
    --------
    With a context of ``(0.0, 1.0, 2.0)`` the trailing log differences are both
    ``1.0``, so the per-step scale is ``1.0``, the median path is flat and the
    level ``q`` of step ``i`` is ``sqrt(i) * z(q)``.

    >>> import pandas as pd
    >>> backend = NaiveBackend(quantile_levels=(0.1, 0.5, 0.9))
    >>> request = ForecastRequest(
    ...     origin=pd.Timestamp("2024-01-01T00:00:00Z"), context=(0.0, 1.0, 2.0)
    ... )
    >>> trajectory = backend.predict([request], horizon=2)[0]
    >>> [round(float(value), 4) for value in trajectory.median]
    [0.0, 0.0]
    """

    name: str = BACKEND_NAME

    def __init__(
        self,
        *,
        timeframe: str = _DEFAULT_TIMEFRAME,
        quantile_levels: Sequence[float] = DEFAULT_QUANTILE_LEVELS,
        dispersion_window: int = _DEFAULT_DISPERSION_WINDOW,
    ) -> None:
        self._timeframe = _validate_timeframe(timeframe)
        try:
            self._quantile_levels: tuple[float, ...] = tuple(quantile_levels)
        except TypeError:
            raise ForecastError("quantile_levels must be a sequence of numbers") from None
        self._dispersion_window = _validate_window(dispersion_window, name="dispersion_window")

    @property
    def timeframe(self) -> str:
        """Candle duration stored on the produced trajectories."""
        return self._timeframe

    @property
    def quantile_levels(self) -> tuple[float, ...]:
        """Levels as passed to the constructor (validated by :meth:`predict`)."""
        return self._quantile_levels

    @property
    def dispersion_window(self) -> int:
        """Number of trailing log differences used for the per-step scale."""
        return self._dispersion_window

    def is_available(self) -> bool:
        """Return ``True``: this backend only needs ``numpy`` (always available)."""
        return True

    def license_note(self) -> str:
        """Return an empty licence note: this backend ships no third-party weights.

        The artifact metadata records this string, so a model-backed backend
        publishes the licence of its checkpoint while a pure-numpy baseline
        publishes nothing.
        """
        return ""

    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]:
        """Return one random-walk trajectory per request, in request order.

        Parameters
        ----------
        requests:
            The requests to forecast.  They are read, never modified (the model
            wrappers of other backends are known to mutate their input list), so
            ``predict`` may be handed a caller-owned sequence directly.

        Returns
        -------
        list[ForecastTrajectory]
            Exactly one trajectory per request, in request order, each carrying
            the request :attr:`~trading_platform.forecast.types.ForecastRequest.origin`,
            the configured timeframe, the requested ``horizon`` and the
            configured quantile levels.  The median (``0.5``) row is flat at
            ``0.0`` and the remaining rows grow like ``sqrt(step)``; every value
            is finite.

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
        time_scale = np.sqrt(np.arange(1, steps + 1, dtype="float64"))

        # Iterate over a snapshot: a caller-owned (possibly mutable) sequence is
        # never traversed in place, and `_context_values` copies every series.
        trajectories: list[ForecastTrajectory] = []
        for request in list(requests):
            context = _context_values(request)
            window = context[-(self._dispersion_window + 1) :]
            sigma_step = float(np.mean(np.abs(np.diff(window))))
            quantiles = np.outer(z_scores, time_scale) * sigma_step
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


def build_backend(**options: Any) -> NaiveBackend:
    """Build a :class:`NaiveBackend` from keyword ``options`` (registry factory).

    Parameters
    ----------
    **options:
        Forwarded verbatim to :class:`NaiveBackend` (``timeframe``,
        ``quantile_levels``, ``dispersion_window``).

    Returns
    -------
    NaiveBackend
        A fresh, deterministic backend instance.

    Examples
    --------
    >>> build_backend(timeframe="4h").timeframe
    '4h'
    """
    return NaiveBackend(**options)
