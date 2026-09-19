"""The ``seasonal`` backend: a drift-corrected deterministic baseline.

Where :mod:`trading_platform.forecast.backends.naive` holds the last value flat,
this backend extrapolates the **recent drift** of the context and keeps a
``sqrt(step)`` dispersion envelope around it:

* ``median[i] = drift * (i + 1)`` with ``drift`` the mean of the trailing log
  differences over ``trend_window`` candles;
* ``quantiles[j, i] = median[i] + sigma_step * sqrt(i + 1) * z(level_j)`` with
  ``sigma_step`` the population standard deviation of those same differences.

It is the second honest baseline: it captures a trending market that the random
walk cannot, and it is still fully deterministic, offline and dependency free
(the calendar-aware part of the layer lives in
:mod:`trading_platform.forecast.series`, which de-seasonalises the target before
any backend sees it).

The validation helpers are shared with the naive backend on purpose, so the two
offline backends accept exactly the same requests and levels.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np

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

__all__ = ["BACKEND_NAME", "SeasonalBackend", "build_backend"]

BACKEND_NAME: str = "seasonal"

_DEFAULT_TIMEFRAME = "1h"
_DEFAULT_TREND_WINDOW = 24


class SeasonalBackend:
    """Drift-corrected baseline with a ``sqrt(step)`` dispersion envelope.

    Parameters
    ----------
    timeframe:
        Candle duration stored on every produced trajectory (a label: the drift
        extrapolation itself is timeframe agnostic).
    quantile_levels:
        Levels to store, a strictly increasing subset of ``(0, 1)`` containing
        ``0.5``.  They are validated on the first :meth:`predict` call rather
        than in the constructor, so that registry introspection can never raise.
    trend_window:
        Number of trailing log differences used to estimate the drift and the
        per-step scale (``>= 1``).

    Examples
    --------
    With a context whose trailing differences are all ``1.0`` the drift is
    ``1.0`` and the dispersion is ``0.0``: the median path is ``1, 2, 3, ...``
    and every stored level collapses onto it.
    """

    name: str = BACKEND_NAME

    def __init__(
        self,
        *,
        timeframe: str = _DEFAULT_TIMEFRAME,
        quantile_levels: Sequence[float] = DEFAULT_QUANTILE_LEVELS,
        trend_window: int = _DEFAULT_TREND_WINDOW,
    ) -> None:
        self._timeframe = _validate_timeframe(timeframe)
        try:
            self._quantile_levels: tuple[float, ...] = tuple(quantile_levels)
        except TypeError:
            raise ForecastError("quantile_levels must be a sequence of numbers") from None
        self._trend_window = _validate_window(trend_window, name="trend_window")

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

    def is_available(self) -> bool:
        """Return ``True``: this backend only needs ``numpy`` (always available)."""
        return True

    def predict(
        self, requests: Sequence[ForecastRequest], *, horizon: int
    ) -> list[ForecastTrajectory]:
        """Return one drift-corrected trajectory per request, in request order.

        Parameters
        ----------
        requests:
            The requests to forecast.  They are read, never modified (the model
            wrappers of other backends are known to mutate their input list).
        horizon:
            Number of steps of every trajectory (``>= 1``).

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

        trajectories: list[ForecastTrajectory] = []
        for request in requests:
            context = _context_values(request)
            window = context[-(self._trend_window + 1) :]
            differences = np.diff(window)
            drift = float(np.mean(differences))
            sigma_step = float(np.std(differences, ddof=0))
            median = drift * step_index
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
    """Build a :class:`SeasonalBackend` from keyword ``options`` (registry factory)."""
    return SeasonalBackend(**options)
