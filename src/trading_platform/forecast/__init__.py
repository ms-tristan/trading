"""Offline forecasting layer: backends, normalised trajectories and their algebra.

This package is the *producer* side of the forecast-driven strategy.  It is
deliberately cheap to import and completely offline:

* the root only pulls in this package's pure modules (``types``, ``registry``,
  ``series`` and the two offline backends), plus
  :mod:`trading_platform.core.errors`;
* heavy optional dependencies (``torch``, ``timesfm``, ``jax``) are imported
  lazily by the backend module that needs them, behind the ``[timesfm]`` extra,
  and never appear here — ``forecast.backends.timesfm``, ``forecast.artifact``,
  ``forecast.skill`` and ``forecast.bootstrap`` are imported on demand, never by
  this ``__init__``;
* nothing here touches the configuration, the strategy, the validation layer,
  the reporting layer or the CLI: the dependency direction is
  ``core`` → ``forecast`` → (builder, strategy, CLI).

The unit of exchange is :class:`~trading_platform.forecast.types.ForecastTrajectory`:
a *normalised* log-price path (``0.0`` is the origin close) plus its quantiles,
which is what the artifact stores and what the strategy turns into entry and
exit decisions.

Typical offline use::

    from trading_platform.forecast import ForecastRequest, get_backend

    backend = get_backend("naive", timeframe="1h")
    trajectories = backend.predict([ForecastRequest(origin=ts, context=closes)],
                                   horizon=24)
"""

from __future__ import annotations

from trading_platform.core.errors import ForecastArtifactError, ForecastError
from trading_platform.forecast.backends.naive import NaiveBackend
from trading_platform.forecast.backends.naive import build_backend as build_naive_backend
from trading_platform.forecast.backends.seasonal import SeasonalBackend
from trading_platform.forecast.backends.seasonal import build_backend as build_seasonal_backend
from trading_platform.forecast.registry import (
    BACKENDS,
    BUILTIN_BACKEND_MODULES,
    ForecastBackend,
    ForecastBackendFactory,
    available_backends,
    get_backend,
    installed_backends,
    register_backend,
)
from trading_platform.forecast.series import (
    EPOCH,
    SECONDS_PER_DAY,
    TIMEFRAME_SECONDS,
    candle_phase,
    candles_per_day,
    deseasonalize_log_price,
    resolve_seasonal_period,
    timeframe_delta,
)
from trading_platform.forecast.types import (
    DEFAULT_QUANTILE_LEVELS,
    MEDIAN_LEVEL,
    ForecastRequest,
    ForecastTrajectory,
)

__all__ = [
    "BACKENDS",
    "BUILTIN_BACKEND_MODULES",
    "DEFAULT_QUANTILE_LEVELS",
    "EPOCH",
    "MEDIAN_LEVEL",
    "SECONDS_PER_DAY",
    "TIMEFRAME_SECONDS",
    "ForecastArtifactError",
    "ForecastBackend",
    "ForecastBackendFactory",
    "ForecastError",
    "ForecastRequest",
    "ForecastTrajectory",
    "NaiveBackend",
    "SeasonalBackend",
    "available_backends",
    "build_naive_backend",
    "build_seasonal_backend",
    "candle_phase",
    "candles_per_day",
    "deseasonalize_log_price",
    "get_backend",
    "installed_backends",
    "register_backend",
    "resolve_seasonal_period",
    "timeframe_delta",
]
